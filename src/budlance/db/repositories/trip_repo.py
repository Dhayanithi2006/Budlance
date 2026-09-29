"""Trip repository managing trip lifecycles and active trip status for Rescue Mode."""

from decimal import Decimal
from uuid import UUID, uuid4
from supabase import Client
from budlance.db.client import get_supabase_client
from budlance.db.models import Trip, TripStatus, utc_now


class TripRepository:
    """Data access repository for trips."""

    def __init__(self, client: Client | None = None) -> None:
        self._client = client or get_supabase_client()
        self._memory_store: dict[UUID, Trip] = {}

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
        is_active: bool = True,
    ) -> Trip:
        """Create a new trip record and optionally deactivate previous active trips for this chat."""
        # If new trip is active, deactivate existing active trips for this chat
        if is_active:
            self.deactivate_previous_trips(telegram_chat_id)

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
            status="planning",
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
                    "status": new_trip.status,
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
        """Retrieve the currently active trip for a chat (used by Rescue Mode)."""
        if self._client:
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

        # Memory store lookup
        active_trips = [
            t for t in self._memory_store.values()
            if t.telegram_chat_id == telegram_chat_id and t.is_active
        ]
        if active_trips:
            active_trips.sort(key=lambda t: t.created_at, reverse=True)
            return active_trips[0]
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

    def update_trip_status(self, trip_id: UUID, status: TripStatus) -> bool:
        """Update the trip status (e.g. planning -> active -> completed)."""
        if self._client:
            res = (
                self._client.table("trips")
                .update({"status": status, "updated_at": utc_now().isoformat()})
                .eq("id", str(trip_id))
                .execute()
            )
            return bool(res.data)

        if trip_id in self._memory_store:
            self._memory_store[trip_id].status = status
            self._memory_store[trip_id].updated_at = utc_now()
            return True
        return False
