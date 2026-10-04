"""Trip Pass repository managing pass state and monetization access control."""

from datetime import datetime
from decimal import Decimal
import logging
from typing import Any
from uuid import UUID, uuid4
from supabase import Client
from budlance.db.client import get_supabase_client
from budlance.db.models import PassStatus, TripPass, utc_now

logger = logging.getLogger(__name__)


class TripPassRepository:
    """Data access repository for trip passes."""

    CANONICAL_STATUSES = frozenset({"FREE", "CHECKOUT_PENDING", "PAID", "PAYMENT_FAILED", "PAYMENT_ABANDONED"})

    def __init__(self, client: Client | None = None) -> None:
        self._client = client or get_supabase_client()
        self._memory_store: dict[UUID, TripPass] = {}

    @classmethod
    def _normalize_status(cls, status: PassStatus | str | None) -> PassStatus:
        if status is None:
            return "FREE"
        if not isinstance(status, str):
            raise ValueError(f"Invalid pass status type: {type(status)}. Expected string.")
        s_up = status.strip().upper()
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
        amount: Decimal = Decimal("49.00"),
        currency: str = "INR",
        provider: str = "razorpay",
        payment_reference: str | None = None,
        status: PassStatus | str = "FREE",
        metadata: dict[str, Any] | None = None,
    ) -> TripPass:
        """Create a new trip pass record."""
        canonical_status = self._normalize_status(status)

        # Check existing pass for this trip to enforce idempotency
        existing = self.get_pass_by_trip(trip_id)
        if existing:
            return existing

        pass_obj = TripPass(
            id=uuid4(),
            trip_id=trip_id,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            amount=amount,
            currency=currency,
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

        if not self._client:
            updated = current.model_copy(
                update={
                    "status": canonical_status,
                    "payment_reference": ref,
                    "provider": prov,
                    "metadata": merged_meta,
                    "updated_at": now_dt,
                }
            )
            self._memory_store[current.id] = updated
            return updated

        patch = {
            "status": canonical_status,
            "payment_reference": ref,
            "provider": prov,
            "metadata": merged_meta,
            "updated_at": now_dt.isoformat(),
        }
        res = self._client.table("trip_passes").update(patch).eq("trip_id", str(trip_id)).execute()
        if res.data:
            return TripPass.model_validate(res.data[0])
        return current
