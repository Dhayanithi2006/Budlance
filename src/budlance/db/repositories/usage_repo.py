"""API usage repository for auditing SerpApi quota utilization."""

from uuid import UUID, uuid4
from supabase import Client
from budlance.db.client import get_supabase_client
from budlance.db.models import ApiUsage, utc_now


class UsageRepository:
    """Data access repository for tracking API usage per trip."""

    def __init__(self, client: Client | None = None) -> None:
        self._client = client or get_supabase_client()
        self._memory_store: list[ApiUsage] = []

    def record_api_call(
        self,
        trip_id: UUID | None,
        engine: str,
        cached: bool = False,
    ) -> ApiUsage:
        """Log a live or cached SerpApi call."""
        record = ApiUsage(
            id=uuid4(),
            trip_id=trip_id,
            engine=engine,
            call_count=0 if cached else 1,
            cached_count=1 if cached else 0,
            created_at=utc_now(),
        )

        if self._client:
            res = (
                self._client.table("api_usage")
                .insert({
                    "id": str(record.id),
                    "trip_id": str(record.trip_id) if record.trip_id else None,
                    "engine": record.engine,
                    "call_count": record.call_count,
                    "cached_count": record.cached_count,
                    "created_at": record.created_at.isoformat(),
                })
                .execute()
            )
            return ApiUsage.model_validate(res.data[0])

        self._memory_store.append(record)
        return record

    def get_trip_usage(self, trip_id: UUID) -> list[ApiUsage]:
        """Fetch all usage entries associated with a trip."""
        if self._client:
            res = (
                self._client.table("api_usage")
                .select("*")
                .eq("trip_id", str(trip_id))
                .order("created_at", desc=False)
                .execute()
            )
            return [ApiUsage.model_validate(item) for item in res.data]

        return [r for r in self._memory_store if r.trip_id == trip_id]
