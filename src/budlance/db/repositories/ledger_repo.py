import re
import threading
from decimal import Decimal
from uuid import UUID, uuid5
from supabase import Client
from budlance.db.client import get_supabase_client
from budlance.db.models import BudgetAllocation, LedgerEntry, utc_now


class DuplicateLedgerEntryError(ValueError):
    """Raised when an atomic storage constraint detects a duplicate ledger entry insert."""
    pass


def extract_event_tag(description: str | None) -> str | None:
    """Extract compound event tag from description like '[evt:tg_upd_1:0]' -> 'tg_upd_1:0'."""
    if not description or description.startswith("Reversal"):
        return None
    m = re.search(r"\[evt:([^\]]+)\]", description)
    return m.group(1) if m else None


def derive_expense_entry_id(trip_id: UUID, compound_event_tag: str) -> UUID:
    """Generate deterministic UUIDv5 for a ledger entry to enforce atomic storage deduplication."""
    namespace = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
    return uuid5(namespace, f"budlance:expense:{trip_id}:{compound_event_tag}")


def is_unique_violation(exc: Exception) -> bool:
    """Check if an exception specifically represents a PostgreSQL unique constraint violation (23505).

    Discriminates strictly between unique violations (23505) and other database errors
    such as foreign key violations (23503), check constraints (23514), syntax errors (42P01), etc.
    """
    code = getattr(exc, "code", None)
    if code is not None:
        return str(code).strip() == "23505"

    err_str = str(exc).lower()
    if "23505" in err_str or "violates unique constraint" in err_str:
        return True
    return False


class LedgerRepository:
    """Data access repository for budget allocations and virtual ledger entries."""

    _class_allocations_store: dict[UUID, BudgetAllocation] = {}
    _class_entries_store: dict[UUID, list[LedgerEntry]] = {}
    _class_lock = threading.Lock()

    def __init__(self, client: Client | None = None) -> None:
        self._client = client or get_supabase_client()
        self._allocations_store = self._class_allocations_store
        self._entries_store = self._class_entries_store

    @classmethod
    def reset_in_memory_store(cls) -> None:
        """Clear class-level in-memory stores for isolated test runs."""
        cls._class_allocations_store.clear()
        cls._class_entries_store.clear()

    def has_event_id(self, trip_id: UUID, event_id: str) -> bool:
        """Check if an expense with the given event_id has already been recorded."""
        entries = self.get_ledger_entries(trip_id)
        tag = f"[evt:{event_id}]"
        tag_prefix = f"[evt:{event_id}:"
        return any(tag in (e.description or "") or tag_prefix in (e.description or "") for e in entries)

    def record_expense_reversal(
        self,
        trip_id: UUID,
        target_amount: Decimal,
        category: str | None = None,
        reason: str | None = None,
    ) -> LedgerEntry | None:
        """Append an auditable reversal entry for a previously recorded expense."""
        entries = self.get_ledger_entries(trip_id)
        # Find matching actual expenditure
        matching = [
            e for e in entries
            if e.actual_amount is not None
            and e.actual_amount == target_amount
            and (category is None or e.category == category)
        ]
        if not matching:
            return None
        target = matching[-1]
        clean_target_desc = re.sub(r"\s*\[evt:[^\]]+\]", "", target.description or "").strip()
        reversal_entry = LedgerEntry(
            trip_id=trip_id,
            category=target.category,
            description=f"Reversal of {clean_target_desc}: {reason or 'User requested cancellation'}",
            allocated_amount=Decimal("0.00"),
            planned_amount=Decimal("0.00"),
            spent_amount=-target.actual_amount,
            remaining_amount=target.actual_amount,
            actual_amount=-target.actual_amount,
            day_number=target.day_number,
            source="user_reported",
            created_at=utc_now(),
        )
        return self.add_ledger_entry(reversal_entry)

    def save_budget_allocation(self, allocation: BudgetAllocation) -> BudgetAllocation:
        """Store or update waterfall budget allocations."""
        if self._client:
            existing = self.get_budget_allocation(allocation.trip_id)
            alloc_id = str(existing.id) if existing else str(allocation.id)
            res = (
                self._client.table("budget_allocations")
                .upsert({
                    "id": alloc_id,
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
                .order("updated_at", desc=True)
                .limit(1)
                .execute()
            )
            if res.data:
                return BudgetAllocation.model_validate(res.data[0])
            return None

        return self._allocations_store.get(trip_id)

    def add_ledger_entries(self, entries: list[LedgerEntry]) -> list[LedgerEntry]:
        """Append multiple line-item entries to the trip ledger in a single atomic transaction.

        Guarantees all-or-nothing atomicity: if any entry violates uniqueness or fails,
        zero rows are committed to persistent storage.
        """
        if not entries:
            return []

        if self._client:
            payloads = [
                {
                    "id": str(e.id),
                    "trip_id": str(e.trip_id),
                    "category": e.category,
                    "description": e.description,
                    "allocated_amount": float(e.allocated_amount),
                    "planned_amount": float(e.planned_amount),
                    "spent_amount": float(e.spent_amount),
                    "remaining_amount": float(e.remaining_amount),
                    "actual_amount": float(e.actual_amount) if e.actual_amount is not None else None,
                    "day_number": e.day_number,
                    "source": e.source,
                    "created_at": e.created_at.isoformat(),
                }
                for e in entries
            ]
            try:
                res = self._client.table("ledger_entries").insert(payloads).execute()
                return [LedgerEntry.model_validate(item) for item in res.data]
            except Exception as exc:
                if is_unique_violation(exc):
                    entry_ids = [str(e.id) for e in entries]
                    raise DuplicateLedgerEntryError(f"Duplicate ledger entry in batch: {entry_ids}") from exc
                raise exc

        with self._class_lock:
            # Atomic pre-validation phase across entire batch before modifying storage
            seen_ids = set()
            seen_tags = set()
            for entry in entries:
                trip_id = entry.trip_id
                existing = self._entries_store.get(trip_id, [])

                # Check ID uniqueness against existing store and intra-batch
                if any(e.id == entry.id for e in existing) or entry.id in seen_ids:
                    raise DuplicateLedgerEntryError(f"Duplicate ledger entry for id={entry.id}")
                seen_ids.add(entry.id)

                # Check compound event tag uniqueness against existing store and intra-batch
                new_tag = extract_event_tag(entry.description)
                if new_tag:
                    if any(extract_event_tag(e.description) == new_tag for e in existing) or new_tag in seen_tags:
                        raise DuplicateLedgerEntryError(f"Duplicate ledger entry for event tag={new_tag}")
                    seen_tags.add(new_tag)

            # Commit phase: all entries passed validation, commit atomically
            for entry in entries:
                if entry.trip_id not in self._entries_store:
                    self._entries_store[entry.trip_id] = []
                self._entries_store[entry.trip_id].append(entry)
            return entries

    def add_ledger_entry(self, entry: LedgerEntry) -> LedgerEntry:
        """Append a single line-item entry to the trip ledger. Delegates to atomic add_ledger_entries."""
        results = self.add_ledger_entries([entry])
        return results[0]

    def delete_ledger_entry(self, entry_id: UUID, trip_id: UUID) -> bool:
        """Delete a ledger entry by id (used for atomic rollback of failed multi-expense batches)."""
        if self._client:
            try:
                self._client.table("ledger_entries").delete().eq("id", str(entry_id)).eq("trip_id", str(trip_id)).execute()
                return True
            except Exception:
                return False

        with self._class_lock:
            if trip_id in self._entries_store:
                before_len = len(self._entries_store[trip_id])
                self._entries_store[trip_id] = [e for e in self._entries_store[trip_id] if e.id != entry_id]
                return len(self._entries_store[trip_id]) < before_len
            return False

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

    def clear_planning_entries(self, trip_id: UUID) -> None:
        """Clear uncommitted baseline ledger entries (where actual_amount is None) for recalculations."""
        if self._client:
            try:
                (
                    self._client.table("ledger_entries")
                    .delete()
                    .eq("trip_id", str(trip_id))
                    .is_("actual_amount", "null")
                    .execute()
                )
            except Exception:
                pass
        if trip_id in self._entries_store:
            self._entries_store[trip_id] = [
                e for e in self._entries_store[trip_id] if e.actual_amount is not None
            ]
