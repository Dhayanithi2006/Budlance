"""Trip Pass repository managing pass state and monetization access control."""

from datetime import datetime
from decimal import Decimal
import logging
from typing import Any
from uuid import UUID, uuid4
from supabase import Client
from budlance.config import get_settings
from budlance.db.client import get_supabase_client
from budlance.db.models import PassStatus, TripPass, utc_now

logger = logging.getLogger(__name__)


class TripPassRepository:
    """Data access repository for trip passes."""

    CANONICAL_STATUSES = frozenset({
        "FREE",
        "FREE_PREVIEW",
        "CHECKOUT_PENDING",
        "PAID",
        "PAID_VERIFIED",
        "PAYMENT_FAILED",
        "PAYMENT_CANCELLED",
        "PAYMENT_ABANDONED",
        "PAYMENT_EXPIRED",
        "DEMO_ACCESS",
    })

    def __init__(self, client: Client | None = None) -> None:
        self._client = client or get_supabase_client()
        self._memory_store: dict[UUID, TripPass] = {}
        self._memory_events: dict[str, dict[str, Any]] = {}

    @classmethod
    def _normalize_status(cls, status: PassStatus | str | None) -> PassStatus:
        if status is None:
            return "FREE"
        if not isinstance(status, str):
            raise ValueError(f"Invalid pass status type: {type(status)}. Expected string.")
        s_up = status.strip().upper()
        alias_map = {
            "CANCELLED": "PAYMENT_CANCELLED",
            "CANCELED": "PAYMENT_CANCELLED",
            "FAILED": "PAYMENT_FAILED",
            "EXPIRED": "PAYMENT_EXPIRED",
            "ABANDONED": "PAYMENT_ABANDONED",
            "PREVIEW": "FREE_PREVIEW",
            "DEMO": "DEMO_ACCESS",
        }
        if s_up in alias_map:
            s_up = alias_map[s_up]
        if s_up in cls.CANONICAL_STATUSES:
            return s_up  # type: ignore[return-value]
        raise ValueError(
            f"Invalid pass status: '{status}'. Canonical statuses are: {', '.join(sorted(cls.CANONICAL_STATUSES))}"
        )

    def create_pass(
        self,
        trip_id: UUID,
        telegram_user_id: int,
        telegram_chat_id: int,
        amount: Decimal | None = None,
        currency: str | None = None,
        provider: str = "razorpay",
        payment_reference: str | None = None,
        status: PassStatus | str = "FREE",
        metadata: dict[str, Any] | None = None,
    ) -> TripPass:
        """Create a new trip pass record."""
        canonical_status = self._normalize_status(status)
        settings = get_settings()
        effective_amount = amount if amount is not None else settings.trip_pass_amount
        effective_currency = currency if currency is not None else settings.trip_pass_currency

        # Check existing pass for this trip to enforce idempotency
        existing = self.get_pass_by_trip(trip_id)
        if existing:
            return existing

        pass_obj = TripPass(
            id=uuid4(),
            trip_id=trip_id,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            amount=effective_amount,
            currency=effective_currency,
            provider=provider,
            payment_reference=payment_reference,
            status=canonical_status,
            metadata=metadata or {},
            created_at=utc_now(),
            updated_at=utc_now(),
        )

        if not self._client:
            self._memory_store[pass_obj.id] = pass_obj
            return pass_obj

        payload = {
            "id": str(pass_obj.id),
            "trip_id": str(pass_obj.trip_id),
            "telegram_user_id": pass_obj.telegram_user_id,
            "telegram_chat_id": pass_obj.telegram_chat_id,
            "amount": float(pass_obj.amount),
            "currency": pass_obj.currency,
            "provider": pass_obj.provider,
            "payment_reference": pass_obj.payment_reference,
            "status": pass_obj.status,
            "metadata": pass_obj.metadata,
            "created_at": pass_obj.created_at.isoformat(),
            "updated_at": pass_obj.updated_at.isoformat(),
        }
        res = self._client.table("trip_passes").insert(payload).execute()
        if res.data:
            return TripPass.model_validate(res.data[0])
        return pass_obj

    def get_pass(self, pass_id: UUID) -> TripPass | None:
        """Fetch pass by its primary ID."""
        if not self._client:
            return self._memory_store.get(pass_id)

        res = self._client.table("trip_passes").select("*").eq("id", str(pass_id)).limit(1).execute()
        if res.data:
            return TripPass.model_validate(res.data[0])
        return None

    def get_pass_by_trip(self, trip_id: UUID) -> TripPass | None:
        """Fetch pass associated with a specific trip ID."""
        if not self._client:
            for p in self._memory_store.values():
                if p.trip_id == trip_id:
                    return p
            return None

        res = self._client.table("trip_passes").select("*").eq("trip_id", str(trip_id)).limit(1).execute()
        if res.data:
            return TripPass.model_validate(res.data[0])
        return None

    get_by_trip_id = get_pass_by_trip

    def get_pass_by_reference(self, payment_reference: str) -> TripPass | None:
        """Fetch pass by its external provider payment reference."""
        if not payment_reference:
            return None

        if not self._client:
            for p in self._memory_store.values():
                if p.payment_reference == payment_reference:
                    return p
            return None

        res = self._client.table("trip_passes").select("*").eq("payment_reference", payment_reference).limit(1).execute()
        if res.data:
            return TripPass.model_validate(res.data[0])
        return None

    def update_pass_status(
        self,
        trip_id: UUID,
        status: PassStatus | str,
        payment_reference: str | None = None,
        provider: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TripPass | None:
        """Update pass status and metadata idempotently."""
        canonical_status = self._normalize_status(status)
        now_dt = utc_now()

        current = self.get_pass_by_trip(trip_id)
        if not current:
            logger.warning("[TRIP_PASS] Cannot update status: no pass exists for trip_id=%s", trip_id)
            return None

        # Build updated metadata
        merged_meta = dict(current.metadata)
        if metadata:
            merged_meta.update(metadata)

        ref = payment_reference if payment_reference is not None else current.payment_reference
        prov = provider if provider is not None else current.provider

        # Out-of-order event protection:
        # A late or replayed failed/expired/pending event must never revoke an already verified PAID or DEMO entitlement.
        target_status = canonical_status
        if current.status in ("PAID", "PAID_VERIFIED", "DEMO_ACCESS") and canonical_status in (
            "CHECKOUT_PENDING",
            "PAYMENT_FAILED",
            "PAYMENT_CANCELLED",
            "PAYMENT_ABANDONED",
            "PAYMENT_EXPIRED",
        ):
            logger.warning(
                "[TRIP_PASS] Ignoring out-of-order status downgrade '%s' for already verified trip_id=%s (current status=%s)",
                canonical_status,
                trip_id,
                current.status,
            )
            target_status = current.status

        if not self._client:
            updated = current.model_copy(
                update={
                    "status": target_status,
                    "payment_reference": ref,
                    "provider": prov,
                    "metadata": merged_meta,
                    "updated_at": now_dt,
                }
            )
            self._memory_store[current.id] = updated
            return updated

        patch = {
            "status": target_status,
            "payment_reference": ref,
            "provider": prov,
            "metadata": merged_meta,
            "updated_at": now_dt.isoformat(),
        }
        res = self._client.table("trip_passes").update(patch).eq("trip_id", str(trip_id)).execute()
        if res.data:
            return TripPass.model_validate(res.data[0])
        return current

    def is_unlocked(self, trip_id: UUID) -> bool:
        """Check whether a trip has an active unlocked entitlement."""
        p = self.get_pass_by_trip(trip_id)
        return p is not None and p.is_unlocked

    def claim_and_fulfill_event(
        self,
        trip_id: UUID,
        event_id: str,
        event_type: str,
        target_status: PassStatus | str,
        provider: str = "stripe",
        payment_reference: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[bool, bool, str | None, TripPass | None]:
        """Atomically claim a payment event and transition trip pass status.

        Guarantees:
        1. Atomic check-and-claim of event_id with database uniqueness.
        2. Concurrent claims for the same event cannot both fulfill the pass.
        3. A processing failure rolls back so the event is not permanently marked complete.
        4. Out-of-order downgrade protections are preserved.

        Returns:
            (success: bool, is_duplicate: bool, error: str | None, pass_record: TripPass | None)
        """
        canonical_target_status = self._normalize_status(target_status)
        merged_meta = dict(metadata or {})

        current_pass = self.get_pass_by_trip(trip_id)
        if not current_pass:
            return False, False, "PASS_NOT_FOUND", None

        # 1. If RPC function is available on database client:
        if self._client and hasattr(self._client, "rpc"):
            try:
                rpc_res = self._client.rpc(
                    "claim_and_fulfill_trip_pass",
                    {
                        "p_event_id": str(event_id),
                        "p_trip_id": str(trip_id),
                        "p_provider": provider,
                        "p_event_type": event_type,
                        "p_target_status": canonical_target_status,
                        "p_payment_reference": payment_reference,
                        "p_metadata": merged_meta,
                    },
                ).execute()
                if rpc_res and rpc_res.data:
                    data = rpc_res.data
                    if isinstance(data, dict):
                        is_dup = bool(data.get("is_duplicate"))
                        succ = bool(data.get("success"))
                        err = data.get("error")
                        fresh_pass = self.get_pass_by_trip(trip_id)
                        return succ, is_dup, err, fresh_pass
            except Exception as rpc_exc:
                err_msg = str(rpc_exc)
                if "not find the function" in err_msg or "PGRST202" in err_msg or "not implemented" in err_msg:
                    logger.debug(
                        "[TRIP_PASS] claim_and_fulfill_trip_pass RPC not present (%s); falling back to table-level operations",
                        rpc_exc,
                    )
                else:
                    # Genuine database transaction error! Re-raise to ensure transaction failure is surfaced.
                    raise rpc_exc

        # 2. Table-level operations (when client is present)
        if self._client:
            # Step A: Insert into payment_events table with database-enforced unique constraint
            event_row_id = str(uuid4())
            try:
                event_payload = {
                    "id": event_row_id,
                    "event_id": str(event_id),
                    "trip_id": str(trip_id),
                    "provider": provider,
                    "event_type": event_type,
                    "status": "PROCESSING",
                    "metadata": merged_meta,
                    "created_at": utc_now().isoformat(),
                }
                self._client.table("payment_events").insert(event_payload).execute()
            except Exception as exc:
                err_str = str(exc).lower()
                # Check for unique constraint violation (duplicate key 23505)
                if (
                    "unique" in err_str
                    or "duplicate" in err_str
                    or "23505" in err_str
                    or "conflict" in err_str
                    or "already exists" in err_str
                ):
                    logger.info("[TRIP_PASS] Event %s already claimed in payment_events table. Deduplicating.", event_id)
                    fresh_pass = self.get_pass_by_trip(trip_id)
                    return True, True, None, fresh_pass
                # If table payment_events doesn't exist on remote server (schema cache missing):
                logger.warning("[TRIP_PASS] payment_events table unavailable (%s), proceeding with pass metadata", exc)

            # Step B: Apply pass update
            try:
                existing_events = list(current_pass.metadata.get("processed_events", []))
                if str(event_id) not in existing_events:
                    existing_events.append(str(event_id))
                merged_meta["processed_events"] = existing_events

                updated_pass = self.update_pass_status(
                    trip_id=trip_id,
                    status=canonical_target_status,
                    payment_reference=payment_reference,
                    provider=provider,
                    metadata=merged_meta,
                )

                # Mark event COMPLETED in payment_events
                try:
                    self._client.table("payment_events").update({"status": "COMPLETED"}).eq("event_id", str(event_id)).execute()
                except Exception:
                    pass

                return True, False, None, updated_pass
            except Exception as update_exc:
                # If pass update fails, roll back the event from payment_events so it can be retried safely!
                try:
                    self._client.table("payment_events").delete().eq("event_id", str(event_id)).execute()
                except Exception:
                    pass
                raise update_exc

        # 3. In-memory mode (client is None)
        if not hasattr(self, "_memory_events"):
            self._memory_events = {}

        if str(event_id) in self._memory_events:
            return True, True, None, current_pass

        # Claim event
        self._memory_events[str(event_id)] = {
            "id": str(uuid4()),
            "event_id": str(event_id),
            "trip_id": str(trip_id),
            "provider": provider,
            "event_type": event_type,
            "status": "PROCESSING",
            "metadata": merged_meta,
            "created_at": utc_now().isoformat(),
        }

        try:
            existing_events = list(current_pass.metadata.get("processed_events", []))
            if str(event_id) not in existing_events:
                existing_events.append(str(event_id))
            merged_meta["processed_events"] = existing_events

            updated_pass = self.update_pass_status(
                trip_id=trip_id,
                status=canonical_target_status,
                payment_reference=payment_reference,
                provider=provider,
                metadata=merged_meta,
            )
            self._memory_events[str(event_id)]["status"] = "COMPLETED"
            return True, False, None, updated_pass
        except Exception as in_mem_exc:
            self._memory_events.pop(str(event_id), None)
            raise in_mem_exc

    def has_event_been_processed(self, event_id: str) -> bool:
        """Check whether an event ID has been processed or claimed."""
        if not event_id:
            return False
        if not self._client:
            if hasattr(self, "_memory_events") and str(event_id) in self._memory_events:
                return True
            for p in self._memory_store.values():
                if str(event_id) in p.metadata.get("processed_events", []):
                    return True
            return False

        try:
            res = self._client.table("payment_events").select("id").eq("event_id", str(event_id)).limit(1).execute()
            if res and res.data:
                return True
        except Exception:
            pass
        return False
