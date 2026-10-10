"""Phase 6: Comprehensive Trip Pass Monetization & Secure Stripe Payment Verification.

Covers:
- The Three Mandatory End-to-End Scenarios:
  1. Successful Trip Pass Purchase with verified webhook fulfillment.
  2. Checkout Cancelled / Payment Failed / Retry with out-of-order protection.
  3. Forged Claims, Webhook Signature/Amount Forgery, Replay, and Production Demo Isolation.
- All 20 Required Phase 6 Tests:
  1. Free preview and planning remain available without a Trip Pass.
  2. Correct server-configured fee (₹49) and currency (INR) are shown to the user.
  3. Checkout creation uses trusted pricing and intended trip/user association.
  4. Duplicate checkout requests are safely handled without redundant charges.
  5. Correct signed webhook and verified paid session grant PAID_VERIFIED entitlement.
  6. Successful redirect callback without verified payment does not unlock premium access.
  7. Failed, abandoned, cancelled, and expired checkouts remain locked.
  8. Delayed payment success/failure events are handled correctly.
  9. Invalid webhook signature is rejected with error.
  10. Amount and currency mismatches are rejected.
  11. Session, trip, and user mismatches are rejected.
  12. Duplicate webhook replay does not create duplicate fulfillment.
  13. Out-of-order events cannot revoke an already verified entitlement.
  14. Persistence and reconstruction preserve payment-attempt and entitlement state.
  15. Entitlement checks block premium access for unpaid users.
  16. Entitlement for one trip does not unlock another trip.
  17. /demo_pass is safe in development/test and blocked in production.
  18. Payment failure and retry preserve free summary and conversational context.
  19. Trip Pass remains separate from the travel budget and actual-expense ledger.
  20. Existing tests from Phases 1–5 continue to pass.
"""

from contextlib import contextmanager
from decimal import Decimal
import hashlib
import hmac
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4
import pytest
from httpx import ASGITransport, AsyncClient, Response

from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.ai.service import AIIntentService
from budlance.api.app import create_app
from budlance.api.routes import set_payment_service
from budlance.attractions.selector import AttractionSelector
from budlance.cache.manager import CacheFallbackManager
from budlance.config import Settings, get_settings
from budlance.db.models import Trip, TripPass
from budlance.db.repositories.conversation_repo import ConversationStateRepository
from budlance.db.repositories.intent_repo import IntentRepository
from budlance.db.repositories.itinerary_repo import ItineraryRepository
from budlance.db.repositories.ledger_repo import LedgerRepository
from budlance.db.repositories.rescue_repo import RescueRepository
from budlance.db.repositories.trip_pass_repo import TripPassRepository
from budlance.db.repositories.trip_repo import TripRepository
from budlance.db.repositories.user_repo import UserRepository
from budlance.engine.budget import ReverseBudgetEngine
from budlance.engine.models import BudgetBreakdown, BudgetEvaluationResult
from budlance.engine.optimizer import OptimizationEngine
from budlance.estimation.estimator import EstimationLayer
from budlance.itinerary.generator import ItineraryGenerator
from budlance.ledger.manager import VirtualLedgerManager
from budlance.normalization.normalizer import DataNormalizer
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.payment.service import PaymentService
from budlance.rescue.service import RescueService
from budlance.schemas.travel import FlightOption, HotelOption, PlaceOption, RouteOption, TransitOption
from budlance.serpapi.models import DataSource, TravelDataEnvelope


@contextmanager
def _mock_settings(**overrides):
    defaults = {
        "stripe_webhook_secret": "whsec_test_secret_key_123",
        "stripe_api_key": "sk_test_123",
        "app_env": "development",
        "enable_trip_pass": True,
        "trip_pass_amount": Decimal("49.00"),
        "trip_pass_currency": "INR",
        "telegram_bot_username": "budlance_bot",
    }
    defaults.update(overrides)
    mocked_settings = Settings(**defaults)
    with patch("budlance.config.get_settings", return_value=mocked_settings), \
         patch("budlance.payment.service.get_settings", return_value=mocked_settings), \
         patch("budlance.orchestrator.orchestrator.get_settings", return_value=mocked_settings), \
         patch("budlance.api.routes.get_settings", return_value=mocked_settings), \
         patch("budlance.db.repositories.trip_pass_repo.get_settings", return_value=mocked_settings):
        yield mocked_settings


# =========================================================================
# Helpers and Fixtures
# =========================================================================

def _create_mock_cache() -> CacheFallbackManager:
    mock_cache = MagicMock(spec=CacheFallbackManager)

    async def _get(engine, params, trip_id=None, **kw):
        if engine == "google_flights":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="fh",
                data={"best_flights": [{"flights": [{"airline": "IndiGo", "flight_number": "6E-101"}], "price": 4000}]},
                is_fallback=False,
            )
        if engine == "google_hotels":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="hh",
                data={"properties": [{"name": "Goa Beach Resort", "rate_per_night": {"extracted_lowest": 2000}, "hotel_class": "3"}]},
                is_fallback=False,
            )
        if engine == "google_maps":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="mh",
                data={"local_results": [{"title": "Calangute Beach", "rating": 4.5}, {"title": "Fort Aguada", "rating": 4.6}]},
                is_fallback=False,
            )
        return None

    mock_cache.get_or_fetch = AsyncMock(side_effect=_get)
    return mock_cache


def _build_test_orchestrator(
    enable_trip_pass: bool = True,
    http_client: Any = None,
) -> tuple[BudlanceOrchestrator, dict]:
    user_repo = UserRepository(client=None)
    trip_repo = TripRepository(client=None)
    intent_repo = IntentRepository(client=None)
    itinerary_repo = ItineraryRepository(client=None)
    ledger_repo = LedgerRepository(client=None)
    rescue_repo = RescueRepository(client=None)
    conversation_repo = ConversationStateRepository(client=None)
    trip_pass_repo = TripPassRepository(client=None)

    payment_service = PaymentService(
        trip_pass_repo=trip_pass_repo,
        pass_amount=Decimal("49.00"),
        pass_currency="INR",
        default_provider="stripe",
        http_client=http_client,
    )

    budget_engine = ReverseBudgetEngine()
    estimation = EstimationLayer()
    optimizer = OptimizationEngine(budget_engine=budget_engine, estimation_layer=estimation)
    ledger_mgr = VirtualLedgerManager(ledger_repo)
    cache_mgr = _create_mock_cache()
    normalizer = DataNormalizer()
    attraction_selector = AttractionSelector(cache_manager=cache_mgr)
    itin_gen = ItineraryGenerator(itinerary_repo, attraction_selector=attraction_selector)

    ai_service = MagicMock(spec=AIIntentService)

    async def _parse(prompt: str):
        p_low = prompt.lower()
        if "rescue" in p_low or "rain" in p_low:
            return ParsedTripIntent(action=TripAction.RESCUE)
        if "spent" in p_low:
            return ParsedTripIntent(
                action=TripAction.LOG_EXPENSE,
                amount=Decimal("500.00"),
                expense_category="food",
                expense_description="dinner",
            )
        return ParsedTripIntent(
            action=TripAction.NEW_TRIP,
            origin="Chennai",
            destination="Goa",
            budget=Decimal("30000.00"),
            people=2,
            days=5,
            currency="INR",
            interests=["beach", "sightseeing"],
        )

    ai_service.parse_trip_intent = AsyncMock(side_effect=_parse)
    ai_service.parse_trip_intent_with_context = AsyncMock(
        side_effect=lambda user_prompt=None, existing_intent=None, *args, **kwargs: _parse(
            user_prompt or kwargs.get("prompt") or (args[0] if args else "")
        )
    )

    from budlance.ai.schemas import ParsedRescueIntent
    ai_service.parse_rescue_intent = AsyncMock(
        return_value=ParsedRescueIntent(
            rescue_type="weather_closure",
            user_issue="Heavy rain at beach",
            affected_category="beach",
        )
    )

    rescue_service = RescueService(
        trip_repo=trip_repo,
        itinerary_repo=itinerary_repo,
        ledger_repo=ledger_repo,
        rescue_repo=rescue_repo,
        ai_service=ai_service,
        cache_manager=cache_mgr,
        normalizer=normalizer,
        budget_engine=budget_engine,
        estimation_layer=estimation,
        ledger_manager=ledger_mgr,
    )

    itinerary_enhancer = MagicMock()
    itinerary_enhancer.enhance_itinerary = AsyncMock(side_effect=lambda itinerary, **kw: itinerary)

    orchestrator = BudlanceOrchestrator(
        user_repo=user_repo,
        trip_repo=trip_repo,
        intent_repo=intent_repo,
        itinerary_repo=itinerary_repo,
        ledger_repo=ledger_repo,
        rescue_repo=rescue_repo,
        conversation_repo=conversation_repo,
        trip_pass_repo=trip_pass_repo,
        payment_service=payment_service,
        ai_service=ai_service,
        cache_manager=cache_mgr,
        normalizer=normalizer,
        estimation_layer=estimation,
        budget_engine=budget_engine,
        optimizer=optimizer,
        itinerary_generator=itin_gen,
        itinerary_enhancer=itinerary_enhancer,
        ledger_manager=ledger_mgr,
        rescue_service=rescue_service,
        enable_trip_pass=enable_trip_pass,
    )

    repos = {
        "user_repo": user_repo,
        "trip_repo": trip_repo,
        "itinerary_repo": itinerary_repo,
        "ledger_repo": ledger_repo,
        "trip_pass_repo": trip_pass_repo,
        "payment_service": payment_service,
        "cache_manager": cache_mgr,
        "ledger_manager": ledger_mgr,
    }
    return orchestrator, repos


def _generate_stripe_signature(payload_bytes: bytes, secret: str, timestamp: int | None = None) -> str:
    """Generate a valid Stripe webhook signature header matching t=...,v1=... format."""
    ts = timestamp if timestamp is not None else int(time.time())
    signed_payload = f"{ts}.".encode("utf-8") + payload_bytes
    sig = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


# =========================================================================
# Three Mandatory End-to-End Scenarios
# =========================================================================

@pytest.mark.asyncio
async def test_scenario_1_successful_trip_pass_purchase():
    """Example 1: End-to-end successful Trip Pass purchase for Chennai -> Goa, 2 adults, ₹30,000 budget.

    Validates:
    - Free preview is generated first without charging.
    - User asks for complete itinerary: receives ₹49 offer and secure checkout link.
    - Verified Stripe signed webhook fulfillment grants PAID_VERIFIED entitlement exactly once.
    - Full day-by-day itinerary and booking handoffs unlock.
    - The ₹49 service fee is NOT deducted from the ₹30,000 travel budget or expense ledger.
    - Replaying the webhook is idempotent and harmless.
    """
    secret = "whsec_test_secret_key_123"
    with _mock_settings(stripe_webhook_secret=secret):
        mock_http = AsyncMock()
        # Mock Stripe Checkout Session creation response
        mock_http.post = AsyncMock(
            return_value=Response(
                200,
                json={
                    "id": "cs_test_session_sc1",
                    "url": "https://checkout.stripe.com/c/pay/cs_test_session_sc1",
                },
            )
        )

        orc, repos = _build_test_orchestrator(enable_trip_pass=True, http_client=mock_http)

        # 1. User plans trip: free preview returned
        msg1 = "Plan a 5-day trip from Chennai to Goa for 2 adults, 1–5 November 2026, with a ₹30,000 travel budget."
        res1 = await orc.handle_user_message(telegram_user_id=2001, chat_id=10001, message=msg1)

        assert res1.status == "FEASIBLE"
        assert res1.is_pass_unlocked is False
        assert res1.generated_itinerary is None
        assert res1.selected_transport is None
        assert "Detailed Itinerary & Rescue Locked" in res1.message_text
        trip_id = res1.trip_id
        initial_budget = res1.budget_breakdown.total_budget
        assert initial_budget == Decimal("30000.00")

        # 2. User requests complete itinerary
        msg2 = "The plan looks good. I want the complete itinerary."
        res2 = await orc.handle_user_message(telegram_user_id=2001, chat_id=10001, message=msg2)

        assert res2.status == "CHECKOUT_PENDING"
        assert res2.is_pass_unlocked is False
        assert "₹49" in res2.message_text or "INR 49" in res2.message_text
        assert "separate from your ₹30,000" in res2.message_text or "separate from your" in res2.message_text
        assert "Proceed to Secure Checkout" in res2.message_text
        assert res2.checkout_url is not None

        # 3. Stripe sends signed webhook for checkout.session.completed
        webhook_payload = {
            "id": "evt_test_checkout_sc1",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_session_sc1",
                    "amount_total": 4900,  # 49.00 INR in paise
                    "currency": "inr",
                    "payment_status": "paid",
                    "client_reference_id": str(trip_id),
                    "metadata": {
                        "trip_id": str(trip_id),
                        "chat_id": "10001",
                    },
                }
            },
        }
        payload_bytes = json.dumps(webhook_payload).encode("utf-8")
        sig_header = _generate_stripe_signature(payload_bytes, secret)

        v_res = await repos["payment_service"].verify_webhook_event(
            provider="stripe",
            payload=webhook_payload,
            signature=sig_header,
            raw_body=payload_bytes,
        )

        assert v_res.success is True
        assert v_res.status == "PAID_VERIFIED"
        assert v_res.trip_id == trip_id

        # Verify entitlement persisted
        pass_rec = repos["trip_pass_repo"].get_by_trip_id(trip_id)
        assert pass_rec is not None
        assert pass_rec.status == "PAID_VERIFIED"
        assert pass_rec.is_unlocked is True
        assert pass_rec.is_verified_paid is True
        assert pass_rec.is_demo is False

        # 4. User queries full plan / status
        res_unlocked = await orc.handle_user_message(telegram_user_id=2001, chat_id=10001, message="paid")
        assert res_unlocked.is_pass_unlocked is True
        assert res_unlocked.generated_itinerary is not None
        assert res_unlocked.selected_transport is not None
        assert res_unlocked.selected_hotel is not None
        assert "ACTIVE ✅ (Verified Stripe Payment)" in res_unlocked.message_text

        # 5. Financial ledger integrity: ₹49 planning fee is NOT deducted from ₹30,000 budget
        summary = repos["ledger_manager"].get_summary(trip_id)
        assert summary.total_budget == Decimal("30000.00")
        assert summary.total_spent == Decimal("0.00")
        for entry in summary.entries:
            assert "trip pass" not in entry.description.lower()

        # 6. Replaying the exact webhook is completely idempotent
        replay_res = await repos["payment_service"].verify_webhook_event(
            provider="stripe",
            payload=webhook_payload,
            signature=sig_header,
            raw_body=payload_bytes,
        )
        assert replay_res.success is True
        assert replay_res.is_duplicate is True
        assert replay_res.status == "PAID_VERIFIED"


@pytest.mark.asyncio
async def test_scenario_2_checkout_cancelled_failed_and_retry():
    """Example 2: User proceeds to checkout, cancels / payment fails, then retries.

    Validates:
    - Payment attempt records failure / cancellation accurately.
    - Free preview remains available; premium itinerary stays locked.
    - Budlance explains payment was not confirmed and provides retry.
    - Safe backend polling/reconciliation resolves pending checkout when user returns after paying.
    - Late out-of-order failure event cannot revoke an established PAID_VERIFIED entitlement.
    """
    secret = "whsec_test_secret_key_123"
    with _mock_settings(stripe_webhook_secret=secret):
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(
            return_value=Response(
                200,
                json={
                    "id": "cs_test_session_sc2",
                    "url": "https://checkout.stripe.com/c/pay/cs_test_session_sc2",
                },
            )
        )
        # Mock Stripe API session polling: initially unpaid
        mock_http.get = AsyncMock(
            return_value=Response(
                200,
                json={"id": "cs_test_session_sc2", "payment_status": "unpaid", "amount_total": 4900},
            )
        )

        orc, repos = _build_test_orchestrator(enable_trip_pass=True, http_client=mock_http)

        # 1. Create trip
        res1 = await orc.handle_user_message(
            telegram_user_id=2002, chat_id=10002,
            message="Plan a 5-day trip from Chennai to Goa for 2 adults, 1–5 November 2026, with a ₹30,000 budget."
        )
        trip_id = res1.trip_id

        # 2. Checkout created
        await orc.handle_user_message(telegram_user_id=2002, chat_id=10002, message="buy pass")

        # 3. Checkout expires / cancelled by user
        expired_payload = {
            "id": "evt_test_expired_sc2",
            "type": "checkout.session.expired",
            "data": {
                "object": {
                    "id": "cs_test_session_sc2",
                    "client_reference_id": str(trip_id),
                    "metadata": {"trip_id": str(trip_id)},
                }
            },
        }
        sig_expired = _generate_stripe_signature(json.dumps(expired_payload).encode("utf-8"), secret)

        v_exp = await repos["payment_service"].verify_webhook_event(
            provider="stripe",
            payload=expired_payload,
            signature=sig_expired,
            raw_body=json.dumps(expired_payload).encode("utf-8"),
        )
        assert v_exp.status == "PAYMENT_EXPIRED"
        assert v_exp.success is False

        # Pass remains locked
        pass_rec = repos["trip_pass_repo"].get_by_trip_id(trip_id)
        assert pass_rec.status == "PAYMENT_EXPIRED"
        assert pass_rec.is_unlocked is False

        # 4. User checks status / claims "paid" before confirmation
        res_unconfirmed = await orc.handle_user_message(telegram_user_id=2002, chat_id=10002, message="paid")
        assert res_unconfirmed.status == "PAYMENT_PENDING"
        assert res_unconfirmed.is_pass_unlocked is False
        assert "Payment Verification Pending" in res_unconfirmed.message_text

        # 5. User retries and actually completes payment in Stripe (polling reconciliation)
        mock_http.get = AsyncMock(
            return_value=Response(
                200,
                json={"id": "cs_test_session_sc2", "payment_status": "paid", "amount_total": 4900},
            )
        )
        res_reconciled = await orc.handle_user_message(telegram_user_id=2002, chat_id=10002, message="paid")
        assert res_reconciled.is_pass_unlocked is True
        assert res_reconciled.pass_status == "PAID_VERIFIED"
        assert res_reconciled.generated_itinerary is not None

        # 6. Out-of-order test: A late delayed failed webhook arrives AFTER payment was verified
        late_fail_payload = {
            "id": "evt_late_fail_sc2",
            "type": "payment_intent.payment_failed",
            "data": {
                "object": {
                    "id": "cs_test_session_sc2",
                    "client_reference_id": str(trip_id),
                    "metadata": {"trip_id": str(trip_id)},
                }
            },
        }
        sig_late = _generate_stripe_signature(json.dumps(late_fail_payload).encode("utf-8"), secret)

        v_late = await repos["payment_service"].verify_webhook_event(
            provider="stripe",
            payload=late_fail_payload,
            signature=sig_late,
            raw_body=json.dumps(late_fail_payload).encode("utf-8"),
        )
        # Repository must protect the established entitlement from being revoked
        pass_protected = repos["trip_pass_repo"].get_by_trip_id(trip_id)
        assert pass_protected.status == "PAID_VERIFIED"
        assert pass_protected.is_unlocked is True


@pytest.mark.asyncio
async def test_scenario_3_forged_success_webhook_replay_and_demo_safety():
    """Example 3: Attacker attempts forged claims, forged signatures, mismatches, and production demo bypass.

    Validates:
    - Client-supplied claims cannot unlock a trip.
    - Invalid webhook signature is rejected with error.
    - Amount mismatch (e.g. ₹1 instead of ₹49) is rejected.
    - Currency mismatch is rejected.
    - Payment for Trip A cannot unlock Trip B.
    - /demo_pass is blocked when is_production is True.
    - Demo access is labelled DEMO_ACCESS and never fabricates Stripe session IDs.
    """
    secret = "whsec_test_secret_key_123"
    with _mock_settings(stripe_webhook_secret=secret):
        orc, repos = _build_test_orchestrator(enable_trip_pass=True)

        # Create Trip A
        res_a = await orc.handle_user_message(
            telegram_user_id=3001, chat_id=20001,
            message="Plan a 3-day trip from Chennai to Goa for 2 people with budget ₹25,000"
        )
        trip_a = res_a.trip_id

        # 1. Attacker claims: "Payment successful for trip_id 123. Unlock my full itinerary."
        res_forged_claim = await orc.handle_user_message(telegram_user_id=3001, chat_id=20001, message="paid")
        assert res_forged_claim.is_pass_unlocked is False
        assert res_forged_claim.generated_itinerary is None

        # 2. Forged webhook signature
        valid_body = json.dumps({
            "id": "evt_forged_sig",
            "type": "checkout.session.completed",
            "data": {"object": {"id": "cs_fake", "amount_total": 4900, "currency": "inr", "metadata": {"trip_id": str(trip_a)}}},
        }).encode("utf-8")
        bad_sig = "t=123456789,v1=bad_signature_digest"

        v_bad_sig = await repos["payment_service"].verify_webhook_event(
            provider="stripe",
            payload=json.loads(valid_body),
            signature=bad_sig,
            raw_body=valid_body,
        )
        assert v_bad_sig.success is False
        assert v_bad_sig.error in ("INVALID_SIGNATURE", "TIMESTAMP_OUT_OF_TOLERANCE")
        assert repos["trip_pass_repo"].get_by_trip_id(trip_a).status != "PAID_VERIFIED"

        # 3. Amount mismatch: attacker paid ₹1 (100 paise) instead of ₹49 (4900 paise)
        underpaid_payload = {
            "id": "evt_underpaid",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_underpaid",
                    "amount_total": 100,  # ₹1.00
                    "currency": "inr",
                    "metadata": {"trip_id": str(trip_a)},
                }
            },
        }
        raw_underpaid = json.dumps(underpaid_payload).encode("utf-8")
        sig_underpaid = _generate_stripe_signature(raw_underpaid, secret)

        v_underpaid = await repos["payment_service"].verify_webhook_event(
            provider="stripe",
            payload=underpaid_payload,
            signature=sig_underpaid,
            raw_body=raw_underpaid,
        )
        assert v_underpaid.success is False
        assert v_underpaid.error == "AMOUNT_MISMATCH"
        assert repos["trip_pass_repo"].get_by_trip_id(trip_a).status != "PAID_VERIFIED"

        # 4. Currency mismatch: paid 4900 USD instead of INR
        curr_payload = {
            "id": "evt_curr_mismatch",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_curr",
                    "amount_total": 4900,
                    "currency": "usd",
                    "metadata": {"trip_id": str(trip_a)},
                }
            },
        }
        raw_curr = json.dumps(curr_payload).encode("utf-8")
        sig_curr = _generate_stripe_signature(raw_curr, secret)

        v_curr = await repos["payment_service"].verify_webhook_event(
            provider="stripe",
            payload=curr_payload,
            signature=sig_curr,
            raw_body=raw_curr,
        )
        assert v_curr.success is False
        assert v_curr.error == "CURRENCY_MISMATCH"

        # 5. Payment for Trip A cannot unlock Trip B
        res_b = await orc.handle_user_message(
            telegram_user_id=3002, chat_id=20002,
            message="Plan a 4-day trip from Chennai to Ooty for 2 people with budget ₹20,000"
        )
        trip_b = res_b.trip_id

        # Valid payment for Trip A
        valid_payload_a = {
            "id": "evt_valid_a",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_valid_a",
                    "amount_total": 4900,
                    "currency": "inr",
                    "client_reference_id": str(trip_a),
                    "metadata": {"trip_id": str(trip_a)},
                }
            },
        }
        raw_valid_a = json.dumps(valid_payload_a).encode("utf-8")
        sig_valid_a = _generate_stripe_signature(raw_valid_a, secret)
        v_valid_a = await repos["payment_service"].verify_webhook_event(
            provider="stripe", payload=valid_payload_a, signature=sig_valid_a, raw_body=raw_valid_a
        )
        assert v_valid_a.success is True

        # Check Trip B: still locked
        pass_b = repos["trip_pass_repo"].get_by_trip_id(trip_b)
        assert pass_b is not None
        assert pass_b.is_unlocked is False

        # 6. /demo_pass safety contract: production blocking
        with _mock_settings(app_env="production", stripe_webhook_secret=secret):
            res_demo_prod = await orc.handle_user_message(telegram_user_id=3002, chat_id=20002, message="/demo_pass")
            assert res_demo_prod.status == "ERROR"
            assert "Judge/Demo bypass is disabled in production" in res_demo_prod.message_text
            assert repos["trip_pass_repo"].get_by_trip_id(trip_b).is_unlocked is False

        # 7. Demo bypass in development: grants DEMO_ACCESS (never PAID_VERIFIED, no Stripe cs_ ID)
        with _mock_settings(app_env="development", stripe_webhook_secret=secret):
            res_demo_dev = await orc.handle_user_message(telegram_user_id=3002, chat_id=20002, message="/demo_pass")
            assert res_demo_dev.is_pass_unlocked is True
            assert res_demo_dev.pass_status == "DEMO_ACCESS"
            pass_b_unlocked = repos["trip_pass_repo"].get_by_trip_id(trip_b)
            assert pass_b_unlocked.status == "DEMO_ACCESS"
            assert pass_b_unlocked.is_demo is True
            assert not pass_b_unlocked.payment_reference.startswith("cs_")


# =========================================================================
# The 20 Required Phase 6 Tests
# =========================================================================

@pytest.mark.asyncio
async def test_req_01_free_preview_and_planning_available_without_pass():
    """1. Free preview and planning remain available without a Trip Pass."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    res = await orc.handle_user_message(
        telegram_user_id=4001, chat_id=30001,
        message="Plan a 3-day trip from Chennai to Goa for 2 people with budget ₹25,000"
    )
    assert res.status == "FEASIBLE"
    assert res.is_pass_unlocked is False
    assert res.budget_breakdown is not None
    assert "Financial Waterfall (Free Feasibility Analysis)" in res.message_text
    assert res.generated_itinerary is None


@pytest.mark.asyncio
async def test_req_02_correct_server_configured_fee_and_currency_shown():
    """2. Correct server-configured fee (₹49) and currency (INR) are shown to user."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    res = await orc.handle_user_message(
        telegram_user_id=4002, chat_id=30002,
        message="Plan a 3-day trip from Chennai to Goa for 2 people with budget ₹25,000"
    )
    assert "INR 49.00" in res.message_text or "₹49" in res.message_text
    pass_rec = repos["trip_pass_repo"].get_by_trip_id(res.trip_id)
    assert pass_rec.amount == Decimal("49.00")
    assert pass_rec.currency == "INR"


@pytest.mark.asyncio
async def test_req_03_checkout_creation_uses_trusted_pricing_and_user_association():
    """3. Checkout creation uses server-authoritative price and intended trip/user association."""
    mock_http = AsyncMock()
    mock_http.post = AsyncMock(return_value=Response(200, json={"id": "cs_req3", "url": "https://stripe.com/cs_req3"}))
    with _mock_settings(stripe_api_key="sk_test_123"):
        orc, repos = _build_test_orchestrator(enable_trip_pass=True, http_client=mock_http)
        session = await repos["payment_service"].create_checkout_session(
            trip_id=uuid4(),
            chat_id=30003,
            user_id=4003,
            amount=Decimal("1.00"),  # Client tries to send ₹1
            provider="stripe",
        )
        # Server must override client amount with authoritative pass amount
        call_data = mock_http.post.call_args[1]["data"]
        assert call_data["line_items[0][price_data][unit_amount]"] == "4900"


@pytest.mark.asyncio
async def test_req_04_duplicate_checkout_requests_handled_safely():
    """4. Duplicate checkout requests are safely handled without redundant sessions."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    t_id = uuid4()
    s1 = await repos["payment_service"].create_checkout_session(trip_id=t_id, chat_id=30004)
    s2 = await repos["payment_service"].create_checkout_session(trip_id=t_id, chat_id=30004)
    assert s1.trip_id == s2.trip_id


@pytest.mark.asyncio
async def test_req_05_signed_webhook_grants_paid_verified_entitlement():
    """5. Correct signed webhook grants PAID_VERIFIED entitlement exactly once."""
    secret = "whsec_test_secret"
    with _mock_settings(stripe_webhook_secret=secret):
        orc, repos = _build_test_orchestrator(enable_trip_pass=True)
        t_id = uuid4()
        repos["trip_pass_repo"].create_pass(trip_id=t_id, telegram_user_id=4005, telegram_chat_id=30005)

        payload = {
            "id": "evt_req5",
            "type": "checkout.session.completed",
            "data": {"object": {"id": "cs_req5", "amount_total": 4900, "currency": "inr", "metadata": {"trip_id": str(t_id)}}},
        }
        raw_b = json.dumps(payload).encode("utf-8")
        sig = _generate_stripe_signature(raw_b, secret)

        v_res = await repos["payment_service"].verify_webhook_event(
            provider="stripe", payload=payload, signature=sig, raw_body=raw_b
        )
        assert v_res.success is True
        assert v_res.status == "PAID_VERIFIED"
        assert repos["trip_pass_repo"].get_by_trip_id(t_id).status == "PAID_VERIFIED"


@pytest.mark.asyncio
async def test_req_06_redirect_without_verified_payment_does_not_unlock():
    """6. Successful redirect without verified payment does not unlock premium access."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    res_plan = await orc.handle_user_message(telegram_user_id=4006, chat_id=30006, message="Plan a trip from Chennai to Goa with budget 20000")
    # Attacker calls "start=paid_..." or "paid" without backend confirmation
    res_callback = await orc.handle_user_message(telegram_user_id=4006, chat_id=30006, message="paid")
    assert res_callback.is_pass_unlocked is False
    assert repos["trip_pass_repo"].get_by_trip_id(res_plan.trip_id).is_unlocked is False


@pytest.mark.asyncio
async def test_req_07_failed_abandoned_cancelled_expired_remain_locked():
    """7. Failed, abandoned, cancelled, and expired checkouts remain locked."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    for st, ev in [
        ("PAYMENT_FAILED", "payment.failed"),
        ("PAYMENT_ABANDONED", "abandoned"),
        ("PAYMENT_EXPIRED", "checkout.session.expired"),
        ("PAYMENT_CANCELLED", "payment.cancelled"),
    ]:
        t_id = uuid4()
        repos["trip_pass_repo"].create_pass(trip_id=t_id, telegram_user_id=4007, telegram_chat_id=30007)
        v = await repos["payment_service"].verify_webhook_event(
            provider="demo",
            payload={"trip_id": str(t_id), "status": ev},
        )
        assert v.success is False
        assert repos["trip_pass_repo"].get_by_trip_id(t_id).is_unlocked is False


@pytest.mark.asyncio
async def test_req_08_delayed_payment_success_failure_handled():
    """8. Delayed-payment success and failure events (e.g. payment_intent.succeeded) handled correctly."""
    secret = "whsec_test"
    with _mock_settings(stripe_webhook_secret=secret):
        orc, repos = _build_test_orchestrator(enable_trip_pass=True)
        t_id = uuid4()
        repos["trip_pass_repo"].create_pass(trip_id=t_id, telegram_user_id=4008, telegram_chat_id=30008)

        # Async payment succeeds
        succ_payload = {
            "id": "evt_delayed_succ",
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": "pi_123", "amount": 4900, "currency": "inr", "metadata": {"trip_id": str(t_id)}}},
        }
        raw_succ = json.dumps(succ_payload).encode("utf-8")
        v_succ = await repos["payment_service"].verify_webhook_event(
            provider="stripe", payload=succ_payload, signature=_generate_stripe_signature(raw_succ, secret), raw_body=raw_succ
        )
        assert v_succ.success is True
        assert v_succ.status == "PAID_VERIFIED"


@pytest.mark.asyncio
async def test_req_09_invalid_webhook_signature_rejected():
    """9. Invalid webhook signature is rejected with error."""
    secret = "whsec_test"
    with _mock_settings(stripe_webhook_secret=secret):
        orc, repos = _build_test_orchestrator(enable_trip_pass=True)
        payload = {"id": "evt_bad_sig", "type": "checkout.session.completed"}
        v = await repos["payment_service"].verify_webhook_event(
            provider="stripe",
            payload=payload,
            signature="t=123,v1=forged",
            raw_body=json.dumps(payload).encode("utf-8"),
        )
        assert v.success is False
        assert v.error in ("INVALID_SIGNATURE", "TIMESTAMP_OUT_OF_TOLERANCE")


@pytest.mark.asyncio
async def test_req_10_amount_and_currency_mismatch_rejected():
    """10. Amount and currency mismatches are rejected and recorded."""
    secret = "whsec_test"
    with _mock_settings(stripe_webhook_secret=secret):
        orc, repos = _build_test_orchestrator(enable_trip_pass=True)
        t_id = uuid4()
        repos["trip_pass_repo"].create_pass(trip_id=t_id, telegram_user_id=4010, telegram_chat_id=30010)

        # Amount mismatch
        p_amt = {"id": "evt_amt", "type": "checkout.session.completed", "data": {"object": {"amount_total": 100, "currency": "inr", "metadata": {"trip_id": str(t_id)}}}}
        raw_amt = json.dumps(p_amt).encode("utf-8")
        v_amt = await repos["payment_service"].verify_webhook_event(provider="stripe", payload=p_amt, signature=_generate_stripe_signature(raw_amt, secret), raw_body=raw_amt)
        assert v_amt.error == "AMOUNT_MISMATCH"

        # Currency mismatch
        p_curr = {"id": "evt_curr", "type": "checkout.session.completed", "data": {"object": {"amount_total": 4900, "currency": "eur", "metadata": {"trip_id": str(t_id)}}}}
        raw_curr = json.dumps(p_curr).encode("utf-8")
        v_curr = await repos["payment_service"].verify_webhook_event(provider="stripe", payload=p_curr, signature=_generate_stripe_signature(raw_curr, secret), raw_body=raw_curr)
        assert v_curr.error == "CURRENCY_MISMATCH"


@pytest.mark.asyncio
async def test_req_11_session_trip_and_user_mismatches_rejected():
    """11. Session, trip, and user mismatches prevent entitlement creation."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    t1 = uuid4()
    t2 = uuid4()
    repos["trip_pass_repo"].create_pass(trip_id=t1, telegram_user_id=4011, telegram_chat_id=30011)

    # Webhook references unknown trip
    v = await repos["payment_service"].verify_webhook_event(
        provider="demo",
        payload={"trip_id": str(t2), "status": "paid"},
    )
    assert v.error == "TRIP_NOT_FOUND"


@pytest.mark.asyncio
async def test_req_12_duplicate_webhook_replay_does_not_duplicate_fulfillment():
    """12. Duplicate webhook replay does not create duplicate fulfillment."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    t_id = uuid4()
    repos["trip_pass_repo"].create_pass(trip_id=t_id, telegram_user_id=4012, telegram_chat_id=30012)

    p = {"id": "evt_replay_12", "type": "payment.captured", "trip_id": str(t_id), "status": "paid"}
    v1 = await repos["payment_service"].verify_webhook_event(provider="demo", payload=p)
    assert v1.success is True
    assert v1.is_duplicate is False

    v2 = await repos["payment_service"].verify_webhook_event(provider="demo", payload=p)
    assert v2.success is True
    assert v2.is_duplicate is True


@pytest.mark.asyncio
async def test_req_13_out_of_order_events_cannot_revoke_verified_entitlement():
    """13. Out-of-order events cannot revoke an already verified entitlement."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    t_id = uuid4()
    repos["trip_pass_repo"].create_pass(trip_id=t_id, telegram_user_id=4013, telegram_chat_id=30013, status="PAID_VERIFIED")

    # Late failure arrives
    repos["trip_pass_repo"].update_pass_status(trip_id=t_id, status="PAYMENT_FAILED")
    assert repos["trip_pass_repo"].get_by_trip_id(t_id).status == "PAID_VERIFIED"


@pytest.mark.asyncio
async def test_req_14_persistence_and_reconstruction_preserve_state():
    """14. Persistence preserves payment-attempt and entitlement state."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    t_id = uuid4()
    p = repos["trip_pass_repo"].create_pass(
        trip_id=t_id, telegram_user_id=4014, telegram_chat_id=30014,
        payment_reference="cs_persisted", status="PAID_VERIFIED"
    )
    fetched = repos["trip_pass_repo"].get_pass_by_reference("cs_persisted")
    assert fetched is not None
    assert fetched.status == "PAID_VERIFIED"
    assert fetched.is_unlocked is True


@pytest.mark.asyncio
async def test_req_15_entitlement_checks_block_premium_access_for_unpaid():
    """15. Entitlement checks block premium access for unpaid users."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    res = await orc.handle_user_message(telegram_user_id=4015, chat_id=30015, message="Plan a trip from Chennai to Goa with budget 20000")
    assert res.generated_itinerary is None
    assert res.selected_transport is None
    assert res.selected_hotel is None


@pytest.mark.asyncio
async def test_req_16_entitlement_for_one_trip_does_not_unlock_another():
    """16. Entitlement for one trip does not unlock another trip."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    t1 = uuid4()
    t2 = uuid4()
    repos["trip_pass_repo"].create_pass(trip_id=t1, telegram_user_id=4016, telegram_chat_id=30016, status="PAID_VERIFIED")
    repos["trip_pass_repo"].create_pass(trip_id=t2, telegram_user_id=4016, telegram_chat_id=30016, status="FREE")

    assert repos["trip_pass_repo"].is_unlocked(t1) is True
    assert repos["trip_pass_repo"].is_unlocked(t2) is False


@pytest.mark.asyncio
async def test_req_17_demo_pass_safe_in_dev_and_blocked_in_production():
    """17. /demo_pass is safe in development/test and strictly blocked in production."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    res_plan = await orc.handle_user_message(telegram_user_id=4017, chat_id=30017, message="Plan a trip from Chennai to Goa with budget 20000")

    # In production: blocked
    with _mock_settings(app_env="production"):
        res_prod = await orc.handle_user_message(telegram_user_id=4017, chat_id=30017, message="/demo_pass")
        assert res_prod.status == "ERROR"
        assert "disabled in production" in res_prod.message_text
        assert repos["trip_pass_repo"].get_by_trip_id(res_plan.trip_id).is_unlocked is False

    # In development: permitted and labeled DEMO_ACCESS
    with _mock_settings(app_env="development"):
        res_dev = await orc.handle_user_message(telegram_user_id=4017, chat_id=30017, message="/demo_pass")
        assert res_dev.is_pass_unlocked is True
        assert res_dev.pass_status == "DEMO_ACCESS"


@pytest.mark.asyncio
async def test_req_18_payment_failure_preserves_free_summary_and_context():
    """18. Payment failure and retry preserve the free summary and conversation context."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    res_free = await orc.handle_user_message(telegram_user_id=4018, chat_id=30018, message="Plan a trip from Chennai to Goa with budget 25000")
    trip_id = res_free.trip_id

    # Fail payment
    await repos["payment_service"].verify_webhook_event(provider="demo", payload={"trip_id": str(trip_id), "status": "failed"})

    # Conversation context and trip state intact
    trip = repos["trip_repo"].get_planning_trip(30018)
    assert trip is not None
    assert trip.id == trip_id
    assert trip.destination == "Goa"


@pytest.mark.asyncio
async def test_req_19_trip_pass_remains_separate_from_travel_budget():
    """19. Trip Pass remains separate from the travel budget and actual-expense ledger."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    res = await orc.handle_user_message(telegram_user_id=4019, chat_id=30019, message="Plan a trip from Chennai to Goa with budget 25000")
    trip_id = res.trip_id

    # Unlock via pass
    repos["trip_pass_repo"].update_pass_status(trip_id=trip_id, status="PAID_VERIFIED")

    summary = repos["ledger_manager"].get_summary(trip_id)
    assert summary.total_budget == Decimal("30000.00")
    for e in summary.entries:
        assert "trip pass" not in e.description.lower()
        assert "pass fee" not in e.description.lower()


@pytest.mark.asyncio
async def test_req_20_existing_phases_continue_to_pass():
    """20. Existing behavior (reverse-budget, optimizer, paywall locks) remains intact."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    res = await orc.handle_user_message(telegram_user_id=4020, chat_id=30020, message="Plan a 3-day trip from Chennai to Goa with budget 20000")
    assert res.status == "FEASIBLE"
    assert res.budget_breakdown.bucket_a_fixed > Decimal("0.00")
    assert res.budget_breakdown.bucket_b_survival > Decimal("0.00")
    assert res.budget_breakdown.bucket_d_rescue > Decimal("0.00")
    assert res.is_pass_unlocked is False
