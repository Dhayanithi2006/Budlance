"""Virtual Ledger Manager tracking allocated, planned, spent, and remaining funds."""

import logging
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from budlance.db.models import BudgetAllocation, ExpenseSource, LedgerCategory, LedgerEntry, utc_now
from budlance.db.repositories.ledger_repo import LedgerRepository
from budlance.engine.models import BudgetEvaluationResult
from budlance.ledger.models import LedgerSummary

logger = logging.getLogger(__name__)


class VirtualLedgerManager:
    """Manages the trip budget ledger across planning and in-trip execution.

    The Virtual Ledger is an advisory budget accounting system, NOT a bank account.
    It tracks:
    - Allocated (authoritative budget assigned by Reverse-Budget Engine)
    - Planned (expected cost from itinerary / bookings)
    - Spent / Reported (user-entered receipts / fare reports)
    - Remaining = Allocated - Spent/Reported
    """

    def __init__(self, ledger_repo: LedgerRepository | None = None) -> None:
        self.repo = ledger_repo or LedgerRepository()

    def initialize_ledger(
        self,
        trip_id: UUID,
        evaluation: BudgetEvaluationResult,
    ) -> LedgerSummary:
        """Create initial budget allocations and baseline line-item entries from a FEASIBLE result."""
        if not evaluation.is_feasible:
            raise ValueError("Cannot initialize Virtual Ledger from an infeasible budget evaluation.")

        breakdown = evaluation.breakdown

        # 1. Persist master BudgetAllocation
        alloc_record = BudgetAllocation(
            id=uuid4(),
            trip_id=trip_id,
            transport_allocated=breakdown.transport_cost,
            stay_allocated=breakdown.hotel_cost,
            food_allocated=breakdown.food_cost,
            activities_discretionary=breakdown.bucket_c_activities,
            rescue_fund_allocated=breakdown.bucket_d_rescue,
            total_budget=breakdown.total_budget,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        saved_alloc = self.repo.save_budget_allocation(alloc_record)

        # 2. Build initial line-item ledger entries
        provenance = breakdown.provenance

        def _map_source(src_key: str) -> ExpenseSource:
            val = provenance.get(src_key)
            if val is not None:
                s = str(val).lower()
                if "live" in s:
                    return "live"
                if "fallback" in s:
                    return "fallback"
                if "user" in s:
                    return "user_reported"
            return "estimated"

        initial_entries = [
            LedgerEntry(
                id=uuid4(),
                trip_id=trip_id,
                category="fixed_booking",
                description="Transport (Onward & Return)",
                allocated_amount=breakdown.transport_cost,
                planned_amount=breakdown.transport_cost,
                spent_amount=Decimal("0.00"),
                remaining_amount=breakdown.transport_cost,
                source=_map_source("transport"),
                created_at=utc_now(),
            ),
            LedgerEntry(
                id=uuid4(),
                trip_id=trip_id,
                category="fixed_booking",
                description="Accommodation",
                allocated_amount=breakdown.hotel_cost,
                planned_amount=breakdown.hotel_cost,
                spent_amount=Decimal("0.00"),
                remaining_amount=breakdown.hotel_cost,
                source=_map_source("hotel"),
                created_at=utc_now(),
            ),
            LedgerEntry(
                id=uuid4(),
                trip_id=trip_id,
                category="daily_survival",
                description="Food & Meals Allowance",
                allocated_amount=breakdown.food_cost,
                planned_amount=breakdown.food_cost,
                spent_amount=Decimal("0.00"),
                remaining_amount=breakdown.food_cost,
                source="estimated",
                created_at=utc_now(),
            ),
            LedgerEntry(
                id=uuid4(),
                trip_id=trip_id,
                category="daily_survival",
                description="Local Transit Allowance",
                allocated_amount=breakdown.local_transit_cost,
                planned_amount=breakdown.local_transit_cost,
                spent_amount=Decimal("0.00"),
                remaining_amount=breakdown.local_transit_cost,
                source="estimated",
                created_at=utc_now(),
            ),
            LedgerEntry(
                id=uuid4(),
                trip_id=trip_id,
                category="activities",
                description="Activities / Discretionary Fund",
                allocated_amount=breakdown.bucket_c_activities,
                planned_amount=breakdown.bucket_c_activities,
                spent_amount=Decimal("0.00"),
                remaining_amount=breakdown.bucket_c_activities,
                source="estimated",
                created_at=utc_now(),
            ),
            LedgerEntry(
                id=uuid4(),
                trip_id=trip_id,
                category="rescue",
                description="Emergency Rescue Reserve Fund",
                allocated_amount=breakdown.bucket_d_rescue,
                planned_amount=Decimal("0.00"),
                spent_amount=Decimal("0.00"),
                remaining_amount=breakdown.bucket_d_rescue,
                source="estimated",
                created_at=utc_now(),
            ),
        ]

        saved_entries = []
        for entry in initial_entries:
            saved_entries.append(self.repo.add_ledger_entry(entry))

        return self.get_summary(trip_id)

    def record_spending(
        self,
        trip_id: UUID,
        category: LedgerCategory,
        amount: Decimal,
        description: str,
        source: ExpenseSource = "user_reported",
        actual_amount: Decimal | None = None,
        day_number: int | None = None,
    ) -> LedgerEntry:
        """Record a user-reported spending transaction against a budget category.

        Deterministic rule:
        Remaining = Allocated - Spent/Reported
        Both planned and spent amounts exist distinctly and are never conflated.
        """
        existing_entries = self.repo.get_ledger_entries(trip_id)
        matching = [e for e in existing_entries if e.category == category]

        # Calculate current allocated and spent for this category
        cat_allocated = sum(e.allocated_amount for e in matching)
        current_cat_spent = sum(e.spent_amount for e in matching)
        new_total_spent = current_cat_spent + amount
        new_remaining = cat_allocated - new_total_spent

        new_entry = LedgerEntry(
            id=uuid4(),
            trip_id=trip_id,
            category=category,
            description=description,
            allocated_amount=Decimal("0.00"),  # Additional line item
            planned_amount=Decimal("0.00"),
            spent_amount=amount,
            remaining_amount=new_remaining,
            actual_amount=actual_amount if actual_amount is not None else amount,
            day_number=day_number,
            source=source,
            created_at=utc_now(),
        )

        return self.repo.add_ledger_entry(new_entry)

    def get_summary(self, trip_id: UUID) -> LedgerSummary:
        """Retrieve aggregated virtual ledger state for an active trip."""
        allocation = self.repo.get_budget_allocation(trip_id)
        if not allocation:
            raise KeyError(f"No budget allocations found for trip_id={trip_id}")

        entries = self.repo.get_ledger_entries(trip_id)

        total_allocated = sum(e.allocated_amount for e in entries)
        total_planned = sum(e.planned_amount for e in entries)
        total_spent = sum(e.spent_amount for e in entries)
        total_remaining = total_allocated - total_spent

        return LedgerSummary(
            trip_id=trip_id,
            total_budget=allocation.total_budget,
            total_allocated=total_allocated,
            total_planned=total_planned,
            total_spent=total_spent,
            total_remaining=total_remaining,
            allocation=allocation,
            entries=entries,
        )

    def record_rescue_adjustment(
        self,
        trip_id: UUID,
        category: LedgerCategory,
        cost_delta: Decimal,
        description: str,
        source: ExpenseSource = "estimated",
    ) -> LedgerEntry:
        """Record a planned budget adjustment resulting from a rescue replanning event.

        Tracks the delta in planned amounts while maintaining all existing reported spending
        and remaining calculations.
        """
        existing_entries = self.repo.get_ledger_entries(trip_id)
        matching = [e for e in existing_entries if e.category == category]
        cat_allocated = sum(e.allocated_amount for e in matching)
        current_cat_spent = sum(e.spent_amount for e in matching)
        new_remaining = cat_allocated - current_cat_spent

        entry = LedgerEntry(
            id=uuid4(),
            trip_id=trip_id,
            category=category,
            description=description,
            allocated_amount=Decimal("0.00"),
            planned_amount=cost_delta,
            spent_amount=Decimal("0.00"),
            remaining_amount=new_remaining,
            source=source,
            created_at=utc_now(),
        )
        return self.repo.add_ledger_entry(entry)

