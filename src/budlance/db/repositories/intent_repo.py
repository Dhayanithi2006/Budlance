"""Intent repository for persisting parsed trip requests."""

from uuid import UUID
from supabase import Client
from budlance.db.client import get_supabase_client
from budlance.db.models import TripIntent


class IntentRepository:
    """Data access repository for trip intents."""

    def __init__(self, client: Client | None = None) -> None:
        self._client = client or get_supabase_client()
        self._memory_store: dict[UUID, TripIntent] = {}

    def save_trip_intent(self, intent: TripIntent) -> TripIntent:
        """Persist an extracted trip intent."""
        if self._client:
            res = (
                self._client.table("trip_intents")
                .insert({
                    "id": str(intent.id),
                    "trip_id": str(intent.trip_id),
                    "budget": float(intent.budget),
                    "currency": intent.currency,
                    "people": intent.people,
                    "days": intent.days,
                    "origin": intent.origin,
                    "destination": intent.destination,
                    "interests": intent.interests,
                    "traveler_type": intent.traveler_type,
                    "raw_prompt": intent.raw_prompt,
                    "extracted_at": intent.extracted_at.isoformat(),
                })
                .execute()
            )
            return TripIntent.model_validate(res.data[0])

        self._memory_store[intent.trip_id] = intent
        return intent

    def get_trip_intent(self, trip_id: UUID) -> TripIntent | None:
        """Retrieve intent associated with a trip."""
        if self._client:
            res = (
                self._client.table("trip_intents")
                .select("*")
                .eq("trip_id", str(trip_id))
                .limit(1)
                .execute()
            )
            if res.data:
                return TripIntent.model_validate(res.data[0])
            return None

        return self._memory_store.get(trip_id)

    def get_intent_by_trip(self, trip_id: UUID) -> TripIntent | None:
        """Alias for get_trip_intent."""
        return self.get_trip_intent(trip_id)
