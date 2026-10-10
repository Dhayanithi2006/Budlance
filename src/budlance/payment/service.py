"""Budlance Payment & Trip Pass Service.

Supports:
- Razorpay: Primary production-oriented India rail (hosted payment links / orders)
- Stripe: Sandbox and test rail (hosted checkout sessions)
- Demo/Judge Bypass: Controlled, auditable demo bypass for evaluation

Security Invariants:
- All secret keys remain strictly server-side.
- Payment amounts are fixed service fees (₹49) and NEVER mixed with travel budget buckets.
- Idempotent callback processing prevents duplicate passes or double state transitions.
- Authoritative backend verification: Telegram 'paid' claims are verified before unlocking.
"""

import asyncio
from decimal import Decimal
import hmac
import hashlib
import json
import logging
from typing import Any
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field

from budlance.config import get_settings
from budlance.db.models import PassStatus, TripPass
from budlance.db.repositories.trip_pass_repo import TripPassRepository

logger = logging.getLogger(__name__)

DEFAULT_PASS_FEE_INR = Decimal("49.00")


class CheckoutSession(BaseModel):
    """Container representing a created checkout session for a Trip Pass."""
    model_config = ConfigDict(from_attributes=True)

    trip_id: UUID
    chat_id: int
    amount: Decimal
    currency: str
    provider: str
    payment_reference: str
    checkout_url: str
    status: PassStatus


class PaymentVerificationResult(BaseModel):
    """Authoritative result of a payment verification attempt."""
    model_config = ConfigDict(from_attributes=True)

    success: bool
    status: PassStatus
    trip_id: UUID | None = None
    chat_id: int | None = None
    payment_reference: str | None = None
    provider: str = "razorpay"
    error_message: str | None = None
    error: str | None = None
    message: str | None = None
    is_duplicate: bool = False


class PaymentService:
    """Manages Trip Pass payment lifecycle, verification, and demo bypasses."""

    def __init__(
        self,
        trip_pass_repo: TripPassRepository | None = None,
        pass_amount: Decimal | None = None,
        pass_currency: str = "INR",
        default_provider: str = "demo",
        http_client: Any = None,
    ) -> None:
        self.repo = trip_pass_repo or TripPassRepository()
        self.trip_pass_repo = self.repo
        settings = get_settings()
        self.pass_amount = pass_amount or settings.trip_pass_amount or DEFAULT_PASS_FEE_INR
        self.pass_currency = pass_currency or settings.trip_pass_currency or "INR"
        self.default_provider = default_provider
        self._http_client = http_client
        self._event_locks: dict[str, asyncio.Lock] = {}

    def _get_event_lock(self, key: str) -> asyncio.Lock:
        """Get or initialize an asyncio.Lock for a given event/pass key to prevent concurrent races."""
        if key not in self._event_locks:
            self._event_locks[key] = asyncio.Lock()
        return self._event_locks[key]

    async def _create_stripe_checkout_session(
        self,
        trip_id: UUID,
        chat_id: int,
        amount: Decimal,
        currency: str,
    ) -> tuple[str, str]:
        """Create a real Stripe test-mode Checkout Session via Stripe API.

        Returns (session_id, session_url) issued directly by Stripe.
        """
        import httpx
        settings = get_settings()
        api_key = settings.stripe_api_key.strip()

        # Guard: reject live keys in demo / non-production mode
        if (not settings.is_production or self.default_provider == "demo") and api_key.startswith("sk_live_"):
            raise ValueError(
                "Live Stripe API keys ('sk_live_...') are strictly prohibited while demo/non-production mode is active."
            )

        bot_username = (settings.telegram_bot_username or "budlance_bot").strip().lstrip("@")
        unit_amount = int(amount * 100)
        curr = currency.lower()

        data = {
            "mode": "payment",
            "success_url": f"https://t.me/{bot_username}?start=paid_{trip_id.hex[:12]}",
            "cancel_url": f"https://t.me/{bot_username}?start=cancel_{trip_id.hex[:12]}",
            "client_reference_id": str(trip_id),
            "metadata[trip_id]": str(trip_id),
            "metadata[chat_id]": str(chat_id),
            "metadata[amount]": str(amount),
            "metadata[currency]": curr,
            "line_items[0][price_data][currency]": curr,
            "line_items[0][price_data][unit_amount]": str(unit_amount),
            "line_items[0][price_data][product_data][name]": "Budlance Trip Pass",
            "line_items[0][price_data][product_data][description]": f"Trip Pass unlock for trip {trip_id}",
            "line_items[0][quantity]": "1",
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Idempotency-Key": f"checkout_{trip_id.hex[:12]}_{chat_id}",
        }

        if self._http_client is not None:
            resp = await self._http_client.post(
                "https://api.stripe.com/v1/checkout/sessions",
                headers=headers,
                data=data,
            )
            if hasattr(resp, "status_code") and resp.status_code >= 400:
                raise RuntimeError(f"Stripe API error: {resp.status_code} {getattr(resp, 'text', '')}")
            res_data = resp.json()
            return res_data["id"], res_data["url"]

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.stripe.com/v1/checkout/sessions",
                headers=headers,
                data=data,
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"Stripe API error: {resp.status_code} {resp.text}")
            res_data = resp.json()
            return res_data["id"], res_data["url"]

    async def check_stripe_checkout_status(self, payment_reference: str) -> bool:
        """Fetch the Stripe Checkout Session and verify if payment_status == 'paid'.

        Enables real-time confirmation when the user says 'paid' without requiring webhooks.
        """
        settings = get_settings()
        if not settings.has_stripe_credentials:
            return False

        api_key = settings.stripe_api_key.strip()
        session_id = payment_reference.strip()
        if not session_id.startswith("cs_"):
            return False

        url = f"https://api.stripe.com/v1/checkout/sessions/{session_id}"
        headers = {"Authorization": f"Bearer {api_key}"}

        try:
            if self._http_client is not None:
                resp = await self._http_client.get(url, headers=headers)
            else:
                import httpx
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(url, headers=headers)

            if resp.status_code == 200:
                data = resp.json()
                is_paid = data.get("payment_status") == "paid"
                amount_total = data.get("amount_total")
                if amount_total is not None and int(amount_total) != int(self.pass_amount * 100):
                    logger.warning("[PAYMENT] Stripe Checkout Session amount mismatch: %s", amount_total)
                    return False
                currency_val = data.get("currency")
                if currency_val is not None and currency_val.strip().lower() != self.pass_currency.lower():
                    logger.warning("[PAYMENT] Stripe Checkout Session currency mismatch: %s", currency_val)
                    return False
                return is_paid
        except Exception as exc:
            logger.warning("[PAYMENT] Failed to fetch Stripe Checkout Session %s: %s", session_id, exc)

        return False

    def get_or_create_pass(
        self,
        trip_id: UUID,
        chat_id: int | None = None,
        user_id: Any = None,
        telegram_chat_id: int | None = None,
        telegram_user_id: int | None = None,
    ) -> TripPass:
        """Retrieve existing pass for trip or create a FREE pass record."""
        existing = self.repo.get_pass_by_trip(trip_id)
        if existing:
            return existing
        c_id = chat_id if chat_id is not None else (telegram_chat_id or 0)
        u_id = user_id if user_id is not None else (telegram_user_id or c_id)
        if isinstance(u_id, UUID):
            u_id = c_id
        return self.repo.create_pass(
            trip_id=trip_id,
            telegram_user_id=int(u_id),
            telegram_chat_id=int(c_id),
            amount=self.pass_amount,
            currency=self.pass_currency,
            provider=self.default_provider,
            status="FREE",
        )

    async def create_checkout_session(
        self,
        trip_id: UUID,
        chat_id: int | None = None,
        user_id: Any = None,
        telegram_chat_id: int | None = None,
        telegram_user_id: int | None = None,
        amount: Decimal | None = None,
        currency: str | None = None,
        provider: str | None = None,
    ) -> CheckoutSession:
        """Create or initialize a checkout session for purchasing a Trip Pass.

        - If STRIPE_API_KEY exists (and provider is stripe or auto-selected), create a real
          Stripe test-mode Checkout Session via Stripe API and return its real URL.
        - Otherwise, return a clearly labeled simulated demo flow.
        - Never return a URL that was not issued by the provider.
        """
        settings = get_settings()
        c_id = chat_id if chat_id is not None else (telegram_chat_id or 0)
        u_id = user_id if user_id is not None else (telegram_user_id or c_id)
        if isinstance(u_id, UUID):
            u_id = c_id
        # Enforce server-authoritative pricing for Trip Pass (reject untrusted client amounts)
        fee = self.pass_amount or DEFAULT_PASS_FEE_INR
        curr = self.pass_currency or DEFAULT_PASS_CURRENCY

        chosen_provider = provider or self.default_provider

        # Real Stripe checkout if STRIPE_API_KEY exists and chosen provider is stripe
        should_use_stripe = (
            chosen_provider == "stripe"
        )

        stripe_session_created = False
        payment_reference = f"sim_{trip_id.hex[:12]}_{c_id}"
        checkout_url = f"https://budlance.travel/pay/{payment_reference}?trip_id={trip_id}"

        if should_use_stripe and settings.has_stripe_credentials:
            try:
                stripe_id, stripe_url = await self._create_stripe_checkout_session(
                    trip_id=trip_id,
                    chat_id=c_id,
                    amount=fee,
                    currency=curr,
                )
                chosen_provider = "stripe"
                payment_reference = stripe_id
                checkout_url = stripe_url
                stripe_session_created = True
            except Exception as exc:
                logger.warning("[PAYMENT] Stripe Checkout API creation failed (%s), falling back to simulated flow", exc)

        if not stripe_session_created:
            chosen_provider = "demo"
            payment_reference = f"sim_{trip_id.hex[:12]}_{c_id}"
            checkout_url = f"https://budlance.travel/pay/{payment_reference}?trip_id={trip_id}"

        existing = self.repo.get_pass_by_trip(trip_id)
        if existing and existing.is_unlocked:
            return CheckoutSession(
                trip_id=trip_id,
                chat_id=c_id,
                amount=existing.amount,
                currency=existing.currency,
                provider=existing.provider,
                payment_reference=existing.payment_reference or payment_reference,
                checkout_url=checkout_url,
                status=existing.status,
            )

        if existing:
            pass_record = self.repo.update_pass_status(
                trip_id=trip_id,
                status="CHECKOUT_PENDING",
                payment_reference=payment_reference,
                provider=chosen_provider,
                metadata={"checkout_url": checkout_url, "amount": float(fee)},
            )
        else:
            pass_record = self.repo.create_pass(
                trip_id=trip_id,
                telegram_user_id=int(u_id),
                telegram_chat_id=int(c_id),
                amount=fee,
                currency=curr,
                provider=chosen_provider,
                payment_reference=payment_reference,
                status="CHECKOUT_PENDING",
                metadata={"checkout_url": checkout_url},
            )

        logger.info(
            "[PAYMENT] Created checkout session for trip_id=%s chat_id=%s ref=%s amount=%s",
            trip_id,
            c_id,
            payment_reference,
            fee,
        )

        return CheckoutSession(
            trip_id=trip_id,
            chat_id=c_id,
            amount=fee,
            currency=curr,
            provider=chosen_provider,
            payment_reference=payment_reference,
            checkout_url=checkout_url,
            status=pass_record.status if pass_record else "CHECKOUT_PENDING",
        )

    async def bypass_trip_pass(
        self,
        trip_id: UUID,
        chat_id: int | None = None,
        user_id: Any = None,
        telegram_chat_id: int | None = None,
        telegram_user_id: int | None = None,
    ) -> TripPass:
        """Alias for applying demo bypass asynchronously."""
        c_id = chat_id if chat_id is not None else (telegram_chat_id or 0)
        u_id = user_id if user_id is not None else (telegram_user_id or c_id)
        if isinstance(u_id, UUID):
            u_id = c_id
        return self.apply_demo_bypass(
            trip_id=trip_id,
            telegram_chat_id=int(c_id),
            telegram_user_id=int(u_id),
        )

    async def verify_webhook_event(
        self,
        provider: str,
        payload: dict[str, Any],
        signature: str | None = None,
        raw_body: bytes | str | None = None,
    ) -> PaymentVerificationResult:
        """Process and verify incoming provider webhook events idempotently.

        Enforces:
        - Exact raw-request HMAC signature verification for Stripe and Razorpay.
        - Timestamp tolerance check for Stripe webhook signatures.
        - Strict amount and currency validation against authoritative server prices.
        - Trip and user association validation.
        - Durable idempotency using provider event IDs to avoid duplicate fulfillments.
        - Out-of-order event protection (handled in repository and state resolution).
        """
        import time
        settings = get_settings()

        # 1. Signature Verification
        if provider == "razorpay":
            if not settings.razorpay_key_secret or not settings.razorpay_key_secret.strip():
                logger.error("[PAYMENT] Razorpay webhook secret not configured. Failing closed.")
                return PaymentVerificationResult(
                    success=False,
                    status="PAYMENT_FAILED",
                    provider=provider,
                    error="CONFIG_ERROR",
                    error_message="Razorpay webhook secret is not configured.",
                )
            if not signature:
                return PaymentVerificationResult(
                    success=False,
                    status="PAYMENT_FAILED",
                    provider=provider,
                    error="MISSING_SIGNATURE",
                    error_message="Missing Razorpay signature",
                )
            body_bytes = raw_body if isinstance(raw_body, bytes) else (
                raw_body.encode("utf-8") if isinstance(raw_body, str) else json.dumps(payload, separators=(",", ":")).encode("utf-8")
            )
            expected_sig = hmac.new(
                settings.razorpay_key_secret.encode("utf-8"),
                body_bytes,
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected_sig, signature):
                logger.warning("[PAYMENT] Razorpay signature mismatch.")
                return PaymentVerificationResult(
                    success=False,
                    status="PAYMENT_FAILED",
                    provider=provider,
                    error="INVALID_SIGNATURE",
                    error_message="Invalid signature",
                )

        if provider == "stripe":
            if not settings.stripe_webhook_secret or not settings.stripe_webhook_secret.strip():
                logger.error("[PAYMENT] Stripe webhook secret not configured. Failing closed.")
                return PaymentVerificationResult(
                    success=False,
                    status="PAYMENT_FAILED",
                    provider=provider,
                    error="CONFIG_ERROR",
                    error_message="Stripe webhook secret is not configured.",
                )
            if not signature:
                return PaymentVerificationResult(
                    success=False,
                    status="PAYMENT_FAILED",
                    provider=provider,
                    error="MISSING_SIGNATURE",
                    error_message="Missing Stripe signature",
                )
            try:
                sig_parts = dict(part.split("=", 1) for part in signature.split(",") if "=" in part)
                timestamp = sig_parts.get("t", "")
                v1_sig = sig_parts.get("v1", "")
                if not timestamp or not v1_sig:
                    return PaymentVerificationResult(
                        success=False,
                        status="PAYMENT_FAILED",
                        provider=provider,
                        error="INVALID_SIGNATURE",
                        error_message="Malformed Stripe signature header",
                    )

                # Tolerance check (300 seconds)
                now_ts = int(time.time())
                try:
                    ts_val = int(timestamp)
                    if abs(now_ts - ts_val) > 300:
                        logger.warning("[PAYMENT] Stripe signature timestamp out of tolerance: %s vs %s", ts_val, now_ts)
                        return PaymentVerificationResult(
                            success=False,
                            status="PAYMENT_FAILED",
                            provider=provider,
                            error="TIMESTAMP_OUT_OF_TOLERANCE",
                            error_message="Stripe webhook signature expired",
                        )
                except ValueError:
                    return PaymentVerificationResult(
                        success=False,
                        status="PAYMENT_FAILED",
                        provider=provider,
                        error="INVALID_SIGNATURE",
                        error_message="Invalid timestamp in Stripe signature header",
                    )

                if raw_body is not None:
                    body_str = raw_body.decode("utf-8") if isinstance(raw_body, bytes) else str(raw_body)
                else:
                    body_str = json.dumps(payload, separators=(",", ":"))

                signed_payload = f"{timestamp}.{body_str}"
                expected_sig = hmac.new(
                    settings.stripe_webhook_secret.encode("utf-8"),
                    signed_payload.encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()
                if not hmac.compare_digest(expected_sig, v1_sig):
                    logger.warning("[PAYMENT] Stripe signature mismatch.")
                    return PaymentVerificationResult(
                        success=False,
                        status="PAYMENT_FAILED",
                        provider=provider,
                        error="INVALID_SIGNATURE",
                        error_message="Invalid Stripe signature",
                    )
            except Exception as exc:
                logger.warning("[PAYMENT] Stripe signature verification error: %s", exc)
                return PaymentVerificationResult(
                    success=False,
                    status="PAYMENT_FAILED",
                    provider=provider,
                    error="INVALID_SIGNATURE",
                    error_message=f"Stripe signature verification failed: {exc}",
                )

        # 2. Extract event metadata
        event_id = payload.get("id")
        event_type = payload.get("event") or payload.get("type") or payload.get("status") or "payment.captured"
        data_obj = payload.get("data", {}).get("object", {}) if isinstance(payload.get("data"), dict) else payload

        payment_ref = (
            data_obj.get("id")
            or payload.get("payment_reference")
            or payload.get("payment_id")
            or payload.get("order_id")
            or payload.get("id")
        )

        metadata_dict = data_obj.get("metadata", {}) if isinstance(data_obj.get("metadata"), dict) else {}
        trip_id_raw = (
            metadata_dict.get("trip_id")
            or data_obj.get("client_reference_id")
            or payload.get("trip_id")
        )

        # Concurrency guard: serialize event processing on (provider, event_id or ref)
        lock_key = f"{provider}_{event_id or payment_ref or trip_id_raw or 'global'}"
        async with self._get_event_lock(lock_key):
            pass_record = None
            if trip_id_raw:
                try:
                    pass_record = self.repo.get_pass_by_trip(UUID(str(trip_id_raw)))
                except Exception:
                    pass_record = None
                if not pass_record:
                    return PaymentVerificationResult(
                        success=False,
                        status="PAYMENT_FAILED",
                        provider=provider,
                        error="TRIP_NOT_FOUND",
                        error_message=f"Trip ID {trip_id_raw} not found",
                    )

            if not pass_record and payment_ref:
                pass_record = self.repo.get_pass_by_reference(str(payment_ref))

            if not pass_record and not payment_ref:
                return PaymentVerificationResult(
                    success=False,
                    status="PAYMENT_FAILED",
                    provider=provider,
                    error="INVALID_PAYLOAD",
                    error_message="Missing payment reference or trip_id in webhook payload",
                )

            if not pass_record:
                logger.warning("[PAYMENT] Webhook reference not found: %s", payment_ref)
                return PaymentVerificationResult(
                    success=False,
                    status="PAYMENT_FAILED",
                    provider=provider,
                    payment_reference=str(payment_ref),
                    error="REFERENCE_NOT_FOUND",
                    error_message="Payment reference not associated with any Trip Pass",
                )

            # 3. Durable Idempotency Check (executed inside synchronized lock)
            processed_events = pass_record.metadata.get("processed_events", [])
            if event_id and str(event_id) in processed_events:
                logger.info("[PAYMENT] Durable idempotency: event_id=%s already fulfilled.", event_id)
                return PaymentVerificationResult(
                    success=pass_record.is_unlocked,
                    status=pass_record.status,
                    trip_id=pass_record.trip_id,
                    chat_id=pass_record.telegram_chat_id,
                    payment_reference=pass_record.payment_reference,
                    provider=provider,
                    is_duplicate=True,
                )

            if pass_record.is_verified_paid and str(event_type).lower() in (
                "paid", "payment.captured", "payment.authorized", "checkout.session.completed", "charge.succeeded", "payment_intent.succeeded"
            ):
                logger.info("[PAYMENT] Idempotent webhook received for already paid pass: %s", pass_record.payment_reference)
                return PaymentVerificationResult(
                    success=True,
                    status=pass_record.status,
                    trip_id=pass_record.trip_id,
                    chat_id=pass_record.telegram_chat_id,
                    payment_reference=pass_record.payment_reference,
                    provider=provider,
                    is_duplicate=True,
                )

            # 4. Classify event outcome and validate details on success
            ev_low = str(event_type).lower()
            stripe_payment_status = str(data_obj.get("payment_status", "")).lower() if isinstance(data_obj, dict) else ""

            # Specific guard for Stripe checkout sessions:
            if ev_low == "checkout.session.completed":
                if stripe_payment_status in ("unpaid", "pending"):
                    logger.info(
                        "[PAYMENT] Checkout session %s is completed but payment_status='%s' (delayed payment). Pass remains pending.",
                        payment_ref,
                        stripe_payment_status,
                    )
                    updated_events = list(processed_events)
                    if event_id and str(event_id) not in updated_events:
                        updated_events.append(str(event_id))
                    self.repo.update_pass_status(
                        trip_id=pass_record.trip_id,
                        status="CHECKOUT_PENDING",
                        payment_reference=str(payment_ref) if payment_ref else pass_record.payment_reference,
                        provider=provider if provider != "demo" else pass_record.provider,
                        metadata={
                            "last_event": event_type,
                            "payment_status": stripe_payment_status,
                            "processed_events": updated_events,
                        },
                    )
                    return PaymentVerificationResult(
                        success=False,
                        status="CHECKOUT_PENDING",
                        trip_id=pass_record.trip_id,
                        chat_id=pass_record.telegram_chat_id,
                        payment_reference=pass_record.payment_reference,
                        provider=provider,
                        error="PAYMENT_UNPAID",
                        error_message=f"Checkout session completed but payment is unpaid/pending ({stripe_payment_status}).",
                    )
                elif stripe_payment_status == "no_payment_required":
                    logger.warning("[PAYMENT] Checkout session %s has payment_status='no_payment_required'. Rejecting.", payment_ref)
                    return PaymentVerificationResult(
                        success=False,
                        status="CHECKOUT_PENDING",
                        trip_id=pass_record.trip_id,
                        chat_id=pass_record.telegram_chat_id,
                        payment_reference=pass_record.payment_reference,
                        provider=provider,
                        error="NO_PAYMENT_REQUIRED",
                        error_message="Trip Pass requires paid checkout; no_payment_required is not supported.",
                    )

            is_success = ev_low in (
                "paid",
                "payment.captured",
                "payment.authorized",
                "checkout.session.completed",
                "checkout.session.async_payment_succeeded",
                "charge.succeeded",
                "payment_intent.succeeded",
            )

            if is_success:
                # Validate amount
                amount_val = data_obj.get("amount_total")
                if amount_val is None:
                    amount_val = data_obj.get("amount")
                if provider == "stripe" and amount_val is None:
                    logger.warning("[PAYMENT] Missing amount in Stripe success event")
                    return PaymentVerificationResult(
                        success=False,
                        status="PAYMENT_FAILED",
                        trip_id=pass_record.trip_id,
                        provider=provider,
                        error="MISSING_AMOUNT",
                        error_message="Stripe payment event missing amount",
                    )
                if amount_val is not None:
                    expected_units = int(self.pass_amount * 100)
                    if int(amount_val) != expected_units:
                        logger.warning("[PAYMENT] Amount mismatch: expected %s, got %s", expected_units, amount_val)
                        return PaymentVerificationResult(
                            success=False,
                            status="PAYMENT_FAILED",
                            trip_id=pass_record.trip_id,
                            provider=provider,
                            error="AMOUNT_MISMATCH",
                            error_message=f"Amount mismatch: expected {expected_units}, got {amount_val}",
                        )

                # Validate currency
                currency_val = data_obj.get("currency")
                if provider == "stripe" and currency_val is None:
                    logger.warning("[PAYMENT] Missing currency in Stripe success event")
                    return PaymentVerificationResult(
                        success=False,
                        status="PAYMENT_FAILED",
                        trip_id=pass_record.trip_id,
                        provider=provider,
                        error="MISSING_CURRENCY",
                        error_message="Stripe payment event missing currency",
                    )
                if currency_val is not None:
                    if str(currency_val).strip().lower() != self.pass_currency.lower():
                        logger.warning("[PAYMENT] Currency mismatch: expected %s, got %s", self.pass_currency.lower(), currency_val)
                        return PaymentVerificationResult(
                            success=False,
                            status="PAYMENT_FAILED",
                            trip_id=pass_record.trip_id,
                            provider=provider,
                            error="CURRENCY_MISMATCH",
                            error_message=f"Currency mismatch: expected {self.pass_currency.lower()}, got {currency_val}",
                        )

                # Validate trip association
                if trip_id_raw and str(pass_record.trip_id) != str(trip_id_raw):
                    logger.warning("[PAYMENT] Trip association mismatch: pass=%s, webhook=%s", pass_record.trip_id, trip_id_raw)
                    return PaymentVerificationResult(
                        success=False,
                        status="PAYMENT_FAILED",
                        trip_id=pass_record.trip_id,
                        provider=provider,
                        error="TRIP_MISMATCH",
                        error_message="Payment associated with different trip",
                    )

                new_status: PassStatus = "PAID_VERIFIED" if provider == "stripe" else "PAID"
                success = True
            elif ev_low in (
                "failed",
                "payment.failed",
                "charge.failed",
                "payment_intent.payment_failed",
                "checkout.session.async_payment_failed",
            ):
                new_status = "PAYMENT_FAILED"
                success = False
            elif ev_low in ("abandoned", "payment.abandoned", "payment_abandoned"):
                new_status = "PAYMENT_ABANDONED"
                success = False
            elif ev_low in ("expired", "checkout.session.expired"):
                new_status = "PAYMENT_EXPIRED"
                success = False
            elif ev_low in ("cancelled", "canceled", "payment.cancelled", "payment_intent.canceled"):
                new_status = "PAYMENT_CANCELLED"
                success = False
            else:
                new_status = "CHECKOUT_PENDING"
                success = False

            if event_id and hasattr(self.repo, "claim_and_fulfill_event"):
                claim_succ, is_dup, claim_err, updated_pass = self.repo.claim_and_fulfill_event(
                    trip_id=pass_record.trip_id,
                    event_id=str(event_id),
                    event_type=str(event_type),
                    target_status=new_status,
                    provider=provider if provider != "demo" else pass_record.provider,
                    payment_reference=str(payment_ref) if payment_ref else pass_record.payment_reference,
                    metadata={
                        "last_event": event_type,
                        "event_payload": payload,
                    },
                )
                if is_dup:
                    logger.info("[PAYMENT] Webhook event %s claimed/processed by concurrent worker. Deduplicating.", event_id)
                    curr = updated_pass or pass_record
                    return PaymentVerificationResult(
                        success=True,
                        status=curr.status,
                        trip_id=curr.trip_id,
                        chat_id=curr.telegram_chat_id,
                        payment_reference=curr.payment_reference,
                        provider=provider,
                        is_duplicate=True,
                    )
                if not claim_succ and claim_err:
                    return PaymentVerificationResult(
                        success=False,
                        status="PAYMENT_FAILED",
                        trip_id=pass_record.trip_id,
                        provider=provider,
                        error=claim_err,
                        error_message=f"Event fulfillment failed: {claim_err}",
                    )
                current_target_status = updated_pass.status if updated_pass else new_status
            else:
                updated_events = list(processed_events)
                if event_id and str(event_id) not in updated_events:
                    updated_events.append(str(event_id))

                self.repo.update_pass_status(
                    trip_id=pass_record.trip_id,
                    status=new_status,
                    payment_reference=str(payment_ref) if payment_ref else pass_record.payment_reference,
                    provider=provider if provider != "demo" else pass_record.provider,
                    metadata={
                        "last_event": event_type,
                        "event_payload": payload,
                        "processed_events": updated_events,
                    },
                )
                current_target_status = new_status

            logger.info(
                "[PAYMENT] Webhook processed: trip_id=%s ref=%s status=%s",
                pass_record.trip_id,
                payment_ref,
                current_target_status,
            )

            return PaymentVerificationResult(
                success=success,
                status=current_target_status,
                trip_id=pass_record.trip_id,
                chat_id=pass_record.telegram_chat_id,
                payment_reference=pass_record.payment_reference,
                provider=provider,
            )

    def apply_demo_bypass(
        self,
        trip_id: UUID,
        telegram_chat_id: int,
        telegram_user_id: int | None = None,
    ) -> TripPass:
        """Apply explicit, controlled judge/demo bypass to unlock a Trip Pass without external payment.

        Safety Invariants:
        - Strictly rejected if running in production mode (`is_production is True`).
        - Sets canonical status to `DEMO_ACCESS` (never `PAID` or `PAID_VERIFIED`).
        - Uses explicit demo payment reference `demo_access_...` (never fake Stripe session `cs_...`).
        """
        settings = get_settings()
        if settings.is_production:
            raise PermissionError("Judge/Demo bypass (/demo_pass) is strictly disabled in production environments.")

        uid = telegram_user_id or telegram_chat_id
        payment_reference = f"demo_access_{trip_id.hex[:10]}"

        existing = self.repo.get_pass_by_trip(trip_id)
        if existing:
            updated = self.repo.update_pass_status(
                trip_id=trip_id,
                status="DEMO_ACCESS",
                payment_reference=payment_reference,
                provider="demo",
                metadata={
                    "demo_bypass": True,
                    "bypassed_by": "judge_demo",
                    "app_env": settings.app_env,
                },
            )
            logger.info("[PAYMENT] Demo bypass applied to existing pass for trip_id=%s", trip_id)
            return updated or existing

        new_pass = self.repo.create_pass(
            trip_id=trip_id,
            telegram_user_id=uid,
            telegram_chat_id=telegram_chat_id,
            amount=DEFAULT_PASS_FEE_INR,
            currency="INR",
            provider="demo",
            payment_reference=payment_reference,
            status="DEMO_ACCESS",
            metadata={
                "demo_bypass": True,
                "bypassed_by": "judge_demo",
                "app_env": settings.app_env,
            },
        )
        logger.info("[PAYMENT] Created and demo-bypassed pass for trip_id=%s", trip_id)
        return new_pass

    def is_trip_unlocked(self, trip_id: UUID) -> bool:
        """Check if a specific trip has an authoritative unlocked pass."""
        pass_record = self.repo.get_pass_by_trip(trip_id)
        return pass_record is not None and pass_record.is_unlocked
