"""Remaining-trip re-optimizer for active travel lifecycle execution.

Fundamental Invariants:
1. Past reality and existing commitments are protected; only the remaining trip can be re-optimized.
2. Actual spending is derived solely from recorded `actual_amount` (never falling back to `planned_amount`).
3. Committed/booked costs (Bucket A) remain excluded from discretionary budget and are never double-counted.
4. Completed and locked days are strictly immutable.
5. Final day operations return a no-op (None) without completing the trip.
6. Reuses existing ReverseBudgetEngine and OptimizationEngine without creating a second optimizer.
"""

import logging
from decimal import Decimal
from typing import Any
from uuid import UUID

from budlance.db.models import BudgetAllocation, Itinerary, LedgerEntry, Trip
from budlance.db.repositories.itinerary_repo import ItineraryRepository
from budlance.db.repositories.ledger_repo import LedgerRepository
from budlance.db.repositories.trip_repo import TripRepository
from budlance.engine.budget import ReverseBudgetEngine
from budlance.engine.models import BudgetBreakdown, BudgetEvaluationResult, OptimizationResult
from budlance.engine.optimizer import OptimizationEngine
from budlance.estimation.estimator import EstimationLayer
from budlance.ledger.manager import VirtualLedgerManager

logger = logging.getLogger(__name__)


def calculate_trip_financial_state(
    trip_id: UUID,
    budget_total: Decimal,
    ledger_repo: LedgerRepository,
) -> dict[str, Any]:
    """Calculate actual spending, committed costs, and genuine remaining usable budget.

    Financial Rules:
    - `actual_amount` represents user-reported actual spend. If None, actual spend is unknown (never planned).
    - Bucket A (transport + hotel) commitments are preserved upfront.
    - If user logs an actual expense against Bucket A, it offsets the commitment rather than double-counting.
    - Remaining usable budget = Total Budget - (Committed Bucket A + Actual Variable Spend).
    """
    entries = ledger_repo.get_ledger_entries(trip_id)

    actual_entries = [e for e in entries if e.actual_amount is not None]
    baseline_entries = [e for e in entries if e.actual_amount is None]

    # 1. Actual spending by category
    categories = ["fixed_booking", "daily_survival", "activities", "rescue"]
    actual_spent_by_category: dict[str, Decimal] = {
        cat: sum((e.actual_amount for e in actual_entries if e.category == cat), Decimal("0.00"))
        for cat in categories
    }
    total_actual_spent = sum(actual_spent_by_category.values(), Decimal("0.00"))

    # 2. Committed costs (Bucket A - transport & hotel)
    planned_A = sum(
        (e.planned_amount for e in baseline_entries if e.category == "fixed_booking"),
        Decimal("0.00"),
    )
    # If no baseline entries exist, check budget allocation for stay + transport
    if planned_A == Decimal("0.00"):
        alloc = ledger_repo.get_budget_allocation(trip_id)
        if alloc:
            planned_A = alloc.transport_allocated + alloc.stay_allocated

    actual_A = actual_spent_by_category["fixed_booking"]
    unpaid_committed_A = max(Decimal("0.00"), planned_A - actual_A)
    committed_A_total = actual_A + unpaid_committed_A

    # 3. Variable actual spend (Buckets B, C, D)
    actual_variable_spent = (
        actual_spent_by_category["daily_survival"]
        + actual_spent_by_category["activities"]
        + actual_spent_by_category["rescue"]
    )

    # 4. Total financial obligations already incurred or committed
    total_committed_and_spent = committed_A_total + actual_variable_spent

    # 5. Genuine remaining budget for upcoming days
    remaining_budget = max(Decimal("0.00"), budget_total - total_committed_and_spent)

    return {
        "total_budget": budget_total,
        "total_actual_spent": total_actual_spent,
        "actual_spent_by_category": actual_spent_by_category,
        "planned_A": planned_A,
        "committed_A_total": committed_A_total,
        "unpaid_committed_A": unpaid_committed_A,
        "actual_variable_spent": actual_variable_spent,
        "total_committed_and_spent": total_committed_and_spent,
        "remaining_budget": remaining_budget,
    }


def get_locked_and_remaining_days(
    trip: Trip,
    itinerary: Itinerary | None,
    ledger_repo: LedgerRepository | None = None,
) -> tuple[set[int], list[int]]:
    """Identify immutable/locked days and genuinely eligible remaining planning days."""
    locked_days: set[int] = set()

    # 1. Any day marked COMPLETED in itinerary is immutable
    if itinerary and itinerary.days:
        for day in itinerary.days:
            d_num = day.get("day_number") if isinstance(day, dict) else getattr(day, "day_number", None)
            d_status = str(day.get("status") if isinstance(day, dict) else getattr(day, "status", "")).upper()
            if d_num is not None and d_status == "COMPLETED":
                locked_days.add(int(d_num))

    # 2. Any day strictly before current_day is in the past
    for d in range(1, trip.current_day):
        locked_days.add(d)

    # 3. If current_day already has actual spending recorded, lock it from replanning
    if ledger_repo:
        entries = ledger_repo.get_ledger_entries(trip.id)
        current_day_has_actual = any(
            e.actual_amount is not None and e.day_number == trip.current_day
            for e in entries
        )
        if current_day_has_actual:
            locked_days.add(trip.current_day)

    # 4. Remaining eligible days
    remaining_days = [d for d in range(1, trip.duration_days + 1) if d not in locked_days]
    return locked_days, remaining_days


def is_meaningful_reoptimization_trigger(
    trip: Trip,
    ledger_repo: LedgerRepository,
    day_completed: bool = False,
    recent_expense_amount: Decimal | None = None,
    material_threshold: Decimal | None = None,
) -> bool:
    """Determine whether an expense or lifecycle event warrants re-optimization.

    A small expense (e.g. ₹100 water) without day completion or budget overrun
    must not trigger full itinerary re-optimization.
    """
    if day_completed:
        return True

    if material_threshold is None:
        from budlance.config import get_settings
        material_threshold = get_settings().reoptimization_material_threshold

    # If an explicit small expense is reported, verify whether it caused a material overrun
    if recent_expense_amount is not None and recent_expense_amount < material_threshold:
        fin_state = calculate_trip_financial_state(trip.id, trip.budget_total, ledger_repo)
        alloc = ledger_repo.get_budget_allocation(trip.id)
        if alloc:
            actual_B = fin_state["actual_spent_by_category"]["daily_survival"]
            actual_C = fin_state["actual_spent_by_category"]["activities"]
            # Within planned budget — no material overrun
            if actual_B <= alloc.food_allocated and actual_C <= alloc.activities_discretionary:
                return False

    return True


class RemainingTripReoptimizer:
    """Coordinates re-optimization of remaining active trip days."""

    def __init__(
        self,
        trip_repo: TripRepository | None = None,
        ledger_repo: LedgerRepository | None = None,
        itinerary_repo: ItineraryRepository | None = None,
        budget_engine: ReverseBudgetEngine | None = None,
        optimizer: OptimizationEngine | None = None,
        estimation_layer: EstimationLayer | None = None,
        ledger_manager: VirtualLedgerManager | None = None,
    ) -> None:
        self.trip_repo = trip_repo or TripRepository()
        self.ledger_repo = ledger_repo or LedgerRepository()
        self.itinerary_repo = itinerary_repo or ItineraryRepository()
        self.budget_engine = budget_engine or ReverseBudgetEngine()
        self.estimation = estimation_layer or EstimationLayer()
        self.optimizer = optimizer or OptimizationEngine(
            budget_engine=self.budget_engine,
            estimation_layer=self.estimation,
        )
        self.ledger_manager = ledger_manager or VirtualLedgerManager(self.ledger_repo)

    async def reoptimize_remaining_trip(
        self,
        trip: Trip,
        force: bool = False,
        day_completed: bool = False,
        recent_expense_amount: Decimal | None = None,
    ) -> OptimizationResult | None:
        """Evaluate remaining budget and re-optimize future itinerary days if required.

        Returns None if on final day, if no eligible future days exist, or if change is non-meaningful.
        """
        # Guard: Final day or beyond — no future eligible days to reoptimize
        if trip.current_day >= trip.duration_days:
            logger.info(
                "[REOPTIMIZER] Trip %s on final day (%s/%s) — no future days to re-optimize.",
                trip.id, trip.current_day, trip.duration_days,
            )
            return None

        # Guard: Meaningful-change gate (small expenses like ₹100 water don't trigger full optimization)
        if not force and not is_meaningful_reoptimization_trigger(
            trip=trip,
            ledger_repo=self.ledger_repo,
            day_completed=day_completed,
            recent_expense_amount=recent_expense_amount,
        ):
            logger.info(
                "[REOPTIMIZER] Skipping re-optimization for trip %s: non-material expense (₹%s) without day completion.",
                trip.id, recent_expense_amount,
            )
            return None

        # 1. Load itinerary and determine locked vs remaining days
        itinerary_record = self.itinerary_repo.get_itinerary(trip.id)
        locked_days, remaining_days = get_locked_and_remaining_days(
            trip=trip,
            itinerary=itinerary_record,
            ledger_repo=self.ledger_repo,
        )

        if not remaining_days:
            logger.info("[REOPTIMIZER] No eligible remaining days for trip %s.", trip.id)
            return None

        num_remaining_days = len(remaining_days)

        # 2. Calculate actual spending and genuine remaining usable budget
        fin_state = calculate_trip_financial_state(
            trip_id=trip.id,
            budget_total=trip.budget_total,
            ledger_repo=self.ledger_repo,
        )
        remaining_budget = fin_state["remaining_budget"]

        # 3. Estimate standard components for remaining days
        people = trip.people_count
        food_estimate = self.estimation.estimate_food(people=people, days=num_remaining_days, tier="standard")
        local_transit_estimate = self.estimation.estimate_local_transit_daily(days=num_remaining_days, people=people)

        # Discretionary activities allowance for remaining days
        alloc = self.ledger_repo.get_budget_allocation(trip.id)
        if alloc:
            actual_C = fin_state["actual_spent_by_category"]["activities"]
            remaining_activities = max(Decimal("0.00"), alloc.activities_discretionary - actual_C)
        else:
            remaining_activities = Decimal("0.00")

        # Fixed costs (transport + hotel) are already accounted for in Bucket A commitments
        # 4. Evaluate feasibility of remaining trip against remaining budget
        eval_result = self.budget_engine.evaluate(
            total_budget=remaining_budget,
            people=people,
            days=num_remaining_days,
            transport=None,
            hotel=None,
            food_estimate=food_estimate,
            local_transit_estimate=local_transit_estimate,
            activities_budget=remaining_activities,
            currency=trip.currency,
        )

        if eval_result.is_feasible:
            # Remaining days are fully feasible within available remaining budget
            logger.info(
                "[REOPTIMIZER] Remaining %s days for trip %s are FEASIBLE within remaining budget ₹%s.",
                num_remaining_days, trip.id, remaining_budget,
            )
            return OptimizationResult(
                initial_status="FEASIBLE",
                final_status="FEASIBLE",
                is_feasible=True,
                successful_attempt=None,
                total_attempts=0,
                final_evaluation=eval_result,
                selected_transport=None,
                selected_hotel=None,
                days=num_remaining_days,
                explanation="Remaining trip configuration is feasible within available budget.",
                deficit=Decimal("0.00"),
            )

        # 5. Over-budget — run OptimizationEngine for the remaining scope
        logger.info(
            "[REOPTIMIZER] Remaining %s days for trip %s are NOT_FEASIBLE (deficit: ₹%s). Running optimizer.",
            num_remaining_days, trip.id, eval_result.deficit,
        )

        opt_result = self.optimizer.optimize(
            trip_id=trip.id,
            total_budget=remaining_budget,
            people=people,
            days=num_remaining_days,
            initial_transport=None,
            initial_hotel=None,
            initial_food=food_estimate,
            initial_transit=local_transit_estimate,
            activities_budget=remaining_activities,
            available_hotels=None,          # Fixed commitments cannot be changed
            available_transports=None,      # Fixed commitments cannot be changed
            currency=trip.currency,
            locked_days=locked_days,
        )

        # 6. Apply modifications ONLY to eligible future days in itinerary (never locked days)
        if itinerary_record and itinerary_record.days:
            updated_days: list[Any] = []
            for day in itinerary_record.days:
                d_num = day.get("day_number") if isinstance(day, dict) else getattr(day, "day_number", None)
                if d_num in locked_days:
                    # STRICT INVARIANT: Locked / completed days remain completely immutable
                    updated_days.append(day)
                elif d_num in remaining_days:
                    # Update eligible future day
                    if isinstance(day, dict):
                        d_copy = dict(day)
                        d_copy["status"] = "MODIFIED"
                        if "Food trimmed to budget tier" in str(opt_result.downgrades_applied):
                            # Mark food items as budget
                            items = []
                            for item in d_copy.get("items", []):
                                if isinstance(item, dict) and item.get("category") == "food":
                                    it_copy = dict(item)
                                    it_copy["notes"] = "Budget tier meal"
                                    items.append(it_copy)
                                elif isinstance(item, dict) and item.get("category") == "activities" and "discretionary activities removed" in str(opt_result.downgrades_applied):
                                    continue
                                else:
                                    items.append(item)
                            d_copy["items"] = items
                        updated_days.append(d_copy)
                    elif hasattr(day, "day_number"):
                        setattr(day, "status", "MODIFIED")
                        updated_days.append(day)
                    else:
                        updated_days.append(day)
                else:
                    updated_days.append(day)

            itinerary_record.days = updated_days
            itinerary_record.is_feasible = opt_result.is_feasible
            itinerary_record.feasibility_note = opt_result.explanation
            self.itinerary_repo.save_itinerary(itinerary_record)

        return opt_result


async def reoptimize_remaining_trip(
    trip: Trip,
    trip_repo: TripRepository | None = None,
    ledger_repo: LedgerRepository | None = None,
    itinerary_repo: ItineraryRepository | None = None,
    budget_engine: ReverseBudgetEngine | None = None,
    optimizer: OptimizationEngine | None = None,
    estimation_layer: EstimationLayer | None = None,
    ledger_manager: VirtualLedgerManager | None = None,
    force: bool = False,
    day_completed: bool = False,
    recent_expense_amount: Decimal | None = None,
) -> OptimizationResult | None:
    """Module-level entry point equivalent to reoptimize_remaining_trip(trip)."""
    reoptimizer = RemainingTripReoptimizer(
        trip_repo=trip_repo,
        ledger_repo=ledger_repo,
        itinerary_repo=itinerary_repo,
        budget_engine=budget_engine,
        optimizer=optimizer,
        estimation_layer=estimation_layer,
        ledger_manager=ledger_manager,
    )
    return await reoptimizer.reoptimize_remaining_trip(
        trip=trip,
        force=force,
        day_completed=day_completed,
        recent_expense_amount=recent_expense_amount,
    )
