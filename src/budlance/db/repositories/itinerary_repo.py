"""Itinerary repository for storing day-by-day travel plans."""

from uuid import UUID
from supabase import Client
from budlance.db.client import get_supabase_client
from budlance.db.models import Itinerary, utc_now


class ItineraryRepository:
    """Data access repository for itineraries."""

    def __init__(self, client: Client | None = None) -> None:
        self._client = client or get_supabase_client()
        self._memory_store: dict[UUID, Itinerary] = {}

    def save_itinerary(self, itinerary: Itinerary) -> Itinerary:
        """Persist or update an itinerary for a trip."""
        if self._client:
            res = (
                self._client.table("itineraries")
                .upsert({
                    "id": str(itinerary.id),
                    "trip_id": str(itinerary.trip_id),
                    "days": itinerary.days,
                    "is_feasible": itinerary.is_feasible,
                    "feasibility_note": itinerary.feasibility_note,
                    "created_at": itinerary.created_at.isoformat(),
                    "updated_at": utc_now().isoformat(),
                })
                .execute()
            )
            return Itinerary.model_validate(res.data[0])

        self._memory_store[itinerary.trip_id] = itinerary
        return itinerary

    def get_itinerary(self, trip_id: UUID) -> Itinerary | None:
        """Fetch the itinerary associated with a trip."""
        if self._client:
            res = (
                self._client.table("itineraries")
                .select("*")
                .eq("trip_id", str(trip_id))
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            if res.data:
                return Itinerary.model_validate(res.data[0])
            return None

        return self._memory_store.get(trip_id)
