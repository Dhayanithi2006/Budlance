"""Rescue repository for logging live trip rescue events (weather/disputes)."""

from uuid import UUID
from supabase import Client
from budlance.db.client import get_supabase_client
from budlance.db.models import RescueEvent


class RescueRepository:
    """Data access repository for Rescue Mode events."""

    def __init__(self, client: Client | None = None) -> None:
        self._client = client or get_supabase_client()
        self._memory_store: dict[UUID, list[RescueEvent]] = {}

    def record_rescue_event(self, event: RescueEvent) -> RescueEvent:
        """Store a rescue event."""
        if self._client:
            res = (
                self._client.table("rescue_events")
                .insert({
                    "id": str(event.id),
                    "trip_id": str(event.trip_id),
                    "rescue_type": event.rescue_type,
                    "user_message": event.user_message,
                    "resolution_summary": event.resolution_summary,
                    "ledger_impact": float(event.ledger_impact),
                    "created_at": event.created_at.isoformat(),
                })
                .execute()
            )
            return RescueEvent.model_validate(res.data[0])

        if event.trip_id not in self._memory_store:
            self._memory_store[event.trip_id] = []
        self._memory_store[event.trip_id].append(event)
        return event

    def get_rescue_events(self, trip_id: UUID) -> list[RescueEvent]:
        """Fetch all rescue events recorded for a trip."""
        if self._client:
            res = (
                self._client.table("rescue_events")
                .select("*")
                .eq("trip_id", str(trip_id))
                .order("created_at", desc=False)
                .execute()
            )
            return [RescueEvent.model_validate(item) for item in res.data]

        return self._memory_store.get(trip_id, [])
