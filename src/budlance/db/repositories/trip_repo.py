"""Trip repository managing trip lifecycles and active trip status for Rescue Mode."""

from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4
from supabase import Client
from budlance.db.client import get_supabase_client
from budlance.db.models import Trip, TripStatus, utc_now

_UNSET = object()


class TripRepository:
    """Data access repository for trips."""

    def __init__(self, client: Client | None = None) -> None:
        self._client = client or get_supabase_client()
        self._memory_store: dict[UUID, Trip] = {}

    CANONICAL_STATUSES = frozenset({"PLANNING", "ACTIVE", "COMPLETED"})

    @classmethod
    def _normalize_status(cls, status: TripStatus | str | None) -> TripStatus:
        """Normalize status string to canonical uppercase TripStatus.

        Raises ValueError for invalid/unsupported status values.
        """
        if status is None:
            return "PLANNING"
        if not isinstance(status, str):
            raise ValueError(f"Invalid trip status type: {type(status)}. Expected string.")
        s_up = status.strip().upper()
        if s_up in cls.CANONICAL_STATUSES:
            return s_up  # type: ignore[return-value]
        raise ValueError(
            f"Invalid trip status: '{status}'. Canonical statuses are: {', '.join(sorted(cls.CANONICAL_STATUSES))}"
        )

    def create_trip(
        self,
        user_id: UUID,
        telegram_chat_id: int,
        budget_total: Decimal,
        destination: str | None = None,
        origin: str | None = None,
        currency: str = "INR",
        people_count: int = 1,
        duration_days: int = 1,
        status: TripStatus | str = "PLANNING",
        is_active: bool = True,
    ) -> Trip:
        """Create a new trip record and optionally deactivate previous active trips for this chat."""
        # If new trip is active, deactivate existing active trips for this chat
        if is_active:
            self.deactivate_previous_trips(telegram_chat_id)

        canonical_status = self._normalize_status(status)

        new_trip = Trip(
            id=uuid4(),
            user_id=user_id,
            telegram_chat_id=telegram_chat_id,
            destination=destination,
            origin=origin,
            budget_total=budget_total,
            currency=currency,
            people_count=people_count,
            duration_days=duration_days,
            status=canonical_status,
            current_day=1,
            is_active=is_active,
            created_at=utc_now(),
            updated_at=utc_now(),
        )

        if self._client:
            res = (
                self._client.table("trips")
                .insert({
                    "id": str(new_trip.id),
                    "user_id": str(new_trip.user_id),
                    "telegram_chat_id": new_trip.telegram_chat_id,
                    "destination": new_trip.destination,
                    "origin": new_trip.origin,
                    "budget_total": float(new_trip.budget_total),
                    "currency": new_trip.currency,
                    "people_count": new_trip.people_count,
                    "duration_days": new_trip.duration_days,
                    "status": canonical_status,
                    "current_day": new_trip.current_day,
                    "is_active": new_trip.is_active,
                    "created_at": new_trip.created_at.isoformat(),
                    "updated_at": new_trip.updated_at.isoformat(),
                })
                .execute()
            )
            return Trip.model_validate(res.data[0])

        self._memory_store[new_trip.id] = new_trip
        return new_trip

    def get_trip(self, trip_id: UUID) -> Trip | None:
        """Retrieve a trip by its unique ID."""
        if self._client:
            res = self._client.table("trips").select("*").eq("id", str(trip_id)).execute()
            if res.data:
                return Trip.model_validate(res.data[0])
            return None

        return self._memory_store.get(trip_id)

    def get_active_trip(self, telegram_chat_id: int) -> Trip | None:
        """Retrieve the currently active trip for a chat (status=ACTIVE and is_active=True)."""
        if self._client:
            # Prioritize canonical status=ACTIVE and is_active=True
            res = (
                self._client.table("trips")
                .select("*")
                .eq("telegram_chat_id", telegram_chat_id)
                .eq("status", "ACTIVE")
                .eq("is_active", True)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            if res.data:
                return Trip.model_validate(res.data[0])

            # Fallback to is_active=True for legacy backward compatibility
            res = (
                self._client.table("trips")
                .select("*")
                .eq("telegram_chat_id", telegram_chat_id)
                .eq("is_active", True)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            if res.data:
                return Trip.model_validate(res.data[0])
            return None

        # Memory store lookup: prioritize status == "ACTIVE" and is_active == True
        active_trips = [
            t for t in self._memory_store.values()
            if t.telegram_chat_id == telegram_chat_id
            and t.is_active
            and str(t.status).upper() == "ACTIVE"
        ]
        if active_trips:
            active_trips.sort(key=lambda t: t.created_at, reverse=True)
            return active_trips[0]

        # Fallback to is_active for legacy backward compatibility
        fallback_trips = [
            t for t in self._memory_store.values()
            if t.telegram_chat_id == telegram_chat_id and t.is_active
        ]
        if fallback_trips:
            fallback_trips.sort(key=lambda t: t.created_at, reverse=True)
            return fallback_trips[0]
        return None

    def get_planning_trip(self, telegram_chat_id: int) -> Trip | None:
        """Retrieve the most recent trip in PLANNING status for a chat."""
        if self._client:
            res = (
                self._client.table("trips")
                .select("*")
                .eq("telegram_chat_id", telegram_chat_id)
                .eq("status", "PLANNING")
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            if res.data:
                return Trip.model_validate(res.data[0])
            return None

        # Memory store lookup
        planning_trips = [
            t for t in self._memory_store.values()
            if t.telegram_chat_id == telegram_chat_id
            and str(t.status).upper() == "PLANNING"
        ]
        if planning_trips:
            planning_trips.sort(key=lambda t: t.created_at, reverse=True)
            return planning_trips[0]
        return None

    def deactivate_previous_trips(self, telegram_chat_id: int) -> None:
        """Deactivate older active trips for the given chat."""
        if self._client:
            (
                self._client.table("trips")
                .update({"is_active": False, "updated_at": utc_now().isoformat()})
                .eq("telegram_chat_id", telegram_chat_id)
                .eq("is_active", True)
                .execute()
            )
        else:
            for t in self._memory_store.values():
                if t.telegram_chat_id == telegram_chat_id and t.is_active:
                    t.is_active = False
                    t.updated_at = utc_now()

    def update_trip_status(
        self,
        trip_id: UUID,
        status: TripStatus | str,
        completion_reason: Any = _UNSET,
        is_active: bool | None = None,
    ) -> bool:
        """Update the trip status (e.g. PLANNING -> ACTIVE -> COMPLETED)."""
        canonical_status = self._normalize_status(status)
        update_data: dict[str, Any] = {"status": canonical_status, "updated_at": utc_now().isoformat()}
        if is_active is not None:
            update_data["is_active"] = is_active
        if completion_reason is not _UNSET:
            reason_val = str(completion_reason)[:50] if completion_reason is not None else None
            update_data["completion_reason"] = reason_val
        elif canonical_status == "COMPLETED":
            update_data["completion_reason"] = None

        if self._client:
            res = (
                self._client.table("trips")
                .update(update_data)
                .eq("id", str(trip_id))
                .execute()
            )
            return bool(res.data)

        if trip_id in self._memory_store:
            self._memory_store[trip_id].status = canonical_status
            if "completion_reason" in update_data:
                self._memory_store[trip_id].completion_reason = update_data["completion_reason"]
            if is_active is not None:
                self._memory_store[trip_id].is_active = is_active
            self._memory_store[trip_id].updated_at = utc_now()
            return True
        return False

    def update_current_day(self, trip_id: UUID, current_day: int) -> bool:
        """Update the current day of the trip (1-indexed)."""
        if current_day < 1:
            raise ValueError("current_day must be at least 1")
        if self._client:
            res = (
                self._client.table("trips")
                .update({"current_day": current_day, "updated_at": utc_now().isoformat()})
                .eq("id", str(trip_id))
                .execute()
            )
            return bool(res.data)

        if trip_id in self._memory_store:
            self._memory_store[trip_id].current_day = current_day
            self._memory_store[trip_id].updated_at = utc_now()
            return True
        return False
