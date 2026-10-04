"""Ledger repository for tracking allocated, planned, and spent funds."""

from uuid import UUID
from supabase import Client
from budlance.db.client import get_supabase_client
from budlance.db.models import BudgetAllocation, LedgerEntry, utc_now


class LedgerRepository:
    """Data access repository for budget allocations and virtual ledger entries."""

    def __init__(self, client: Client | None = None) -> None:
        self._client = client or get_supabase_client()
        self._allocations_store: dict[UUID, BudgetAllocation] = {}
        self._entries_store: dict[UUID, list[LedgerEntry]] = {}

    def save_budget_allocation(self, allocation: BudgetAllocation) -> BudgetAllocation:
        """Store or update waterfall budget allocations."""
        if self._client:
            res = (
                self._client.table("budget_allocations")
                .upsert({
                    "id": str(allocation.id),
                    "trip_id": str(allocation.trip_id),
                    "transport_allocated": float(allocation.transport_allocated),
                    "stay_allocated": float(allocation.stay_allocated),
                    "food_allocated": float(allocation.food_allocated),
                    "activities_discretionary": float(allocation.activities_discretionary),
                    "rescue_fund_allocated": float(allocation.rescue_fund_allocated),
                    "total_budget": float(allocation.total_budget),
                    "created_at": allocation.created_at.isoformat(),
                    "updated_at": utc_now().isoformat(),
                })
                .execute()
            )
            return BudgetAllocation.model_validate(res.data[0])

        self._allocations_store[allocation.trip_id] = allocation
        return allocation

    def get_budget_allocation(self, trip_id: UUID) -> BudgetAllocation | None:
        """Retrieve budget allocations for a trip."""
        if self._client:
            res = (
                self._client.table("budget_allocations")
                .select("*")
                .eq("trip_id", str(trip_id))
                .limit(1)
                .execute()
            )
            if res.data:
                return BudgetAllocation.model_validate(res.data[0])
            return None

        return self._allocations_store.get(trip_id)

    def add_ledger_entry(self, entry: LedgerEntry) -> LedgerEntry:
        """Append a new line-item entry to the trip ledger."""
        if self._client:
            res = (
                self._client.table("ledger_entries")
                .insert({
                    "id": str(entry.id),
                    "trip_id": str(entry.trip_id),
                    "category": entry.category,
                    "description": entry.description,
                    "allocated_amount": float(entry.allocated_amount),
                    "planned_amount": float(entry.planned_amount),
                    "spent_amount": float(entry.spent_amount),
                    "remaining_amount": float(entry.remaining_amount),
                    "actual_amount": float(entry.actual_amount) if entry.actual_amount is not None else None,
                    "day_number": entry.day_number,
                    "source": entry.source,
                    "created_at": entry.created_at.isoformat(),
                })
                .execute()
            )
            return LedgerEntry.model_validate(res.data[0])

        if entry.trip_id not in self._entries_store:
            self._entries_store[entry.trip_id] = []
        self._entries_store[entry.trip_id].append(entry)
        return entry

    def get_ledger_entries(self, trip_id: UUID) -> list[LedgerEntry]:
        """Fetch all ledger entries for a trip ordered chronologically."""
        if self._client:
            res = (
                self._client.table("ledger_entries")
                .select("*")
                .eq("trip_id", str(trip_id))
                .order("created_at", desc=False)
                .execute()
            )
            return [LedgerEntry.model_validate(item) for item in res.data]

        return self._entries_store.get(trip_id, [])
