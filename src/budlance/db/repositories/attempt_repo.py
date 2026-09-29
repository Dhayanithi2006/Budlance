"""Attempt repository for logging 4-step optimization attempts."""

from uuid import UUID
from supabase import Client
from budlance.db.client import get_supabase_client
from budlance.db.models import PlanAttempt


class AttemptRepository:
    """Data access repository for optimization plan attempts."""

    def __init__(self, client: Client | None = None) -> None:
        self._client = client or get_supabase_client()
        self._memory_store: dict[UUID, list[PlanAttempt]] = {}

    def record_plan_attempt(self, attempt: PlanAttempt) -> PlanAttempt:
        """Store an optimization attempt record."""
        if self._client:
            res = (
                self._client.table("plan_attempts")
                .insert({
                    "id": str(attempt.id),
                    "trip_id": str(attempt.trip_id),
                    "attempt_number": attempt.attempt_number,
                    "downgrade_type": attempt.downgrade_type,
                    "was_feasible": attempt.was_feasible,
                    "cost_calculated": float(attempt.cost_calculated),
                    "notes": attempt.notes,
                    "created_at": attempt.created_at.isoformat(),
                })
                .execute()
            )
            return PlanAttempt.model_validate(res.data[0])

        if attempt.trip_id not in self._memory_store:
            self._memory_store[attempt.trip_id] = []
        self._memory_store[attempt.trip_id].append(attempt)
        return attempt

    def get_plan_attempts(self, trip_id: UUID) -> list[PlanAttempt]:
        """Fetch all optimization attempts for a trip."""
        if self._client:
            res = (
                self._client.table("plan_attempts")
                .select("*")
                .eq("trip_id", str(trip_id))
                .order("attempt_number", desc=False)
                .execute()
            )
            return [PlanAttempt.model_validate(item) for item in res.data]

        return self._memory_store.get(trip_id, [])
