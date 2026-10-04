"""Optimization Engine executing the frozen 4-step downgrade sequence."""

import logging
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from budlance.db.models import PlanAttempt, utc_now
from budlance.db.repositories.attempt_repo import AttemptRepository
from budlance.engine.budget import ReverseBudgetEngine
from budlance.engine.models import BudgetEvaluationResult, OptimizationResult
from budlance.estimation.estimator import EstimationLayer
from budlance.schemas.travel import FlightOption, FoodEstimate, HotelOption, LocalTransitEstimate, TransitOption

logger = logging.getLogger(__name__)


class OptimizationEngine:
    """Manages the frozen 4-step downgrade loop when a trip is initially NOT_FEASIBLE.

    Frozen Downgrade Sequence (Max 4 attempts):
    Attempt 1: Hotel tier down ➔ Re-evaluate
    Attempt 2: Transport class down ➔ Re-evaluate
    Attempt 3: Reduce trip length by 1 day ➔ Re-evaluate
    Attempt 4: Trim discretionary Bucket B ➔ Re-evaluate
    """

    def __init__(
        self,
        budget_engine: ReverseBudgetEngine | None = None,
        estimation_layer: EstimationLayer | None = None,
        attempt_repo: AttemptRepository | None = None,
    ) -> None:
        self.budget_engine = budget_engine or ReverseBudgetEngine()
        self.estimation = estimation_layer or EstimationLayer()
        self.attempt_repo = attempt_repo or AttemptRepository()

    def optimize(
        self,
        trip_id: UUID | None,
        total_budget: Decimal,
        people: int,
        days: int,
        initial_transport: FlightOption | TransitOption | None,
        initial_hotel: HotelOption | None,
        initial_food: FoodEstimate,
        initial_transit: LocalTransitEstimate,
        activities_budget: Decimal = Decimal("0.00"),
        available_hotels: list[HotelOption] | None = None,
        available_transports: list[FlightOption | TransitOption] | None = None,
        currency: str = "INR",
        selected_attractions: list[Any] | None = None,
        locked_days: set[int] | None = None,
        requires_transport: bool = False,
        requires_lodging: bool = False,
    ) -> OptimizationResult:
        """Run the optimization loop up to a maximum of 4 attempts."""
        # Baseline evaluation
        current_days = days
        current_transport = initial_transport
        current_hotel = initial_hotel
        current_food = initial_food
        current_transit = initial_transit
        current_activities = activities_budget
        downgrades_applied: list[str] = []

        baseline_eval = self.budget_engine.evaluate(
            total_budget=total_budget,
            people=people,
            days=current_days,
            transport=current_transport,
            hotel=current_hotel,
            food_estimate=current_food,
            local_transit_estimate=current_transit,
            activities_budget=current_activities,
            currency=currency,
            selected_attractions=selected_attractions,
        )

        has_invalid_transport = requires_transport and (
            current_transport is None or current_transport.price <= Decimal("0.00")
        )
        has_invalid_lodging = requires_lodging and (
            (current_hotel is None or current_hotel.total_price <= Decimal("0.00"))
            and baseline_eval.breakdown.hotel_cost <= Decimal("0.00")
        )

        # Gate constraint: Optimizer cannot fabricate or bypass missing physical transport or lodging
        if has_invalid_transport or has_invalid_lodging:
            reason = "Missing valid physical transport" if has_invalid_transport else "Missing valid accommodation"
            return OptimizationResult(
                initial_status="NOT_FEASIBLE",
                final_status="NOT_FEASIBLE",
                is_feasible=False,
                successful_attempt=None,
                total_attempts=0,
                final_evaluation=baseline_eval,
                selected_transport=current_transport,
                selected_hotel=current_hotel,
                days=current_days,
                explanation=f"{reason} cannot be optimized into feasibility.",
                deficit=baseline_eval.deficit,
            )

        if baseline_eval.is_feasible:
            # Already feasible, no downgrades needed
            return OptimizationResult(
                initial_status="FEASIBLE",
                final_status="FEASIBLE",
                is_feasible=True,
                successful_attempt=None,
                total_attempts=0,
                final_evaluation=baseline_eval,
                selected_transport=current_transport,
                selected_hotel=current_hotel,
                days=current_days,
                explanation="Initial configuration is already feasible within budget.",
                deficit=Decimal("0.00"),
            )

        latest_eval = baseline_eval

        # =====================================================================
        # Attempt 1: Hotel tier down
        # =====================================================================
        current_hotel_tier = "standard"
        downgraded_hotel = self._downgrade_hotel(current_hotel, available_hotels)
        if downgraded_hotel and (current_hotel is None or downgraded_hotel.total_price < current_hotel.total_price):
            current_hotel = downgraded_hotel
            downgrades_applied.append(f"Hotel downgraded to {current_hotel.name} ({currency} {current_hotel.total_price})")
        elif current_hotel is None and current_days > 1:
            current_hotel_tier = "budget"
            downgrades_applied.append("Lodging tier adjusted to budget")

        latest_eval = self.budget_engine.evaluate(
            total_budget=total_budget,
            people=people,
            days=current_days,
            transport=current_transport,
            hotel=current_hotel,
            food_estimate=current_food,
            local_transit_estimate=current_transit,
            activities_budget=current_activities,
            currency=currency,
            selected_attractions=selected_attractions,
            hotel_tier=current_hotel_tier,
        )

        self._record_attempt(trip_id, 1, "hotel_tier_down", latest_eval)
        if latest_eval.is_feasible and not (requires_transport and (current_transport is None or current_transport.price <= Decimal("0.00"))) and not (requires_lodging and (current_hotel is None or current_hotel.total_price <= Decimal("0.00")) and latest_eval.breakdown.hotel_cost <= Decimal("0.00")):
            return self._build_success_result(
                attempt_num=1,
                evaluation=latest_eval,
                transport=current_transport,
                hotel=current_hotel,
                days=current_days,
                downgrades=downgrades_applied,
            )

        # =====================================================================
        # Attempt 2: Transport class down
        # =====================================================================
        downgraded_transport = self._downgrade_transport(current_transport, available_transports)
        if downgraded_transport and (current_transport is None or downgraded_transport.price < current_transport.price):
            current_transport = downgraded_transport
            name = getattr(current_transport, "airline", None) or getattr(current_transport, "name_or_operator", "Transport")
            downgrades_applied.append(f"Transport downgraded to {name} ({currency} {current_transport.price})")

        latest_eval = self.budget_engine.evaluate(
            total_budget=total_budget,
            people=people,
            days=current_days,
            transport=current_transport,
            hotel=current_hotel,
            food_estimate=current_food,
            local_transit_estimate=current_transit,
            activities_budget=current_activities,
            currency=currency,
            selected_attractions=selected_attractions,
            hotel_tier=current_hotel_tier,
        )

        self._record_attempt(trip_id, 2, "transport_class_down", latest_eval)
        if latest_eval.is_feasible and not (requires_transport and (current_transport is None or current_transport.price <= Decimal("0.00"))) and not (requires_lodging and (current_hotel is None or current_hotel.total_price <= Decimal("0.00")) and latest_eval.breakdown.hotel_cost <= Decimal("0.00")):
            return self._build_success_result(
                attempt_num=2,
                evaluation=latest_eval,
                transport=current_transport,
                hotel=current_hotel,
                days=current_days,
                downgrades=downgrades_applied,
            )

        # =====================================================================
        # Attempt 3: Reduce trip length by 1 day
        # =====================================================================
        min_allowed_days = max(locked_days) if locked_days else 1
        if current_days > min_allowed_days:
            current_days -= 1
            # Re-scale hotel stay for (current_days) nights
            if current_hotel and current_hotel.price_per_night:
                current_hotel = current_hotel.model_copy(
                    update={"total_price": current_hotel.price_per_night * Decimal(current_days)}
                )
            # Re-estimate daily food and local transit
            current_food = self.estimation.estimate_food(people, current_days, tier=current_food.tier)
            current_transit = self.estimation.estimate_local_transit_daily(current_days, people)
            downgrades_applied.append(f"Trip duration reduced to {current_days} days")

        latest_eval = self.budget_engine.evaluate(
            total_budget=total_budget,
            people=people,
            days=current_days,
            transport=current_transport,
            hotel=current_hotel,
            food_estimate=current_food,
            local_transit_estimate=current_transit,
            activities_budget=current_activities,
            currency=currency,
            selected_attractions=selected_attractions,
            hotel_tier=current_hotel_tier,
        )

        self._record_attempt(trip_id, 3, "reduce_trip_length", latest_eval)
        if latest_eval.is_feasible and not (requires_transport and (current_transport is None or current_transport.price <= Decimal("0.00"))) and not (requires_lodging and (current_hotel is None or current_hotel.total_price <= Decimal("0.00")) and latest_eval.breakdown.hotel_cost <= Decimal("0.00")):
            return self._build_success_result(
                attempt_num=3,
                evaluation=latest_eval,
                transport=current_transport,
                hotel=current_hotel,
                days=current_days,
                downgrades=downgrades_applied,
            )

        # =====================================================================
        # Attempt 4: Trim discretionary Bucket B
        # =====================================================================
        # Switch food to budget tier and trim optional activities
        current_food = self.estimation.estimate_food(people, current_days, tier="budget")
        current_activities = Decimal("0.00")
        downgrades_applied.append("Food trimmed to budget tier and discretionary activities removed")

        latest_eval = self.budget_engine.evaluate(
            total_budget=total_budget,
            people=people,
            days=current_days,
            transport=current_transport,
            hotel=current_hotel,
            food_estimate=current_food,
            local_transit_estimate=current_transit,
            activities_budget=current_activities,
            currency=currency,
            selected_attractions=selected_attractions,
            hotel_tier=current_hotel_tier,
        )

        self._record_attempt(trip_id, 4, "trim_discretionary_b", latest_eval)
        if latest_eval.is_feasible and not (requires_transport and (current_transport is None or current_transport.price <= Decimal("0.00"))) and not (requires_lodging and (current_hotel is None or current_hotel.total_price <= Decimal("0.00")) and latest_eval.breakdown.hotel_cost <= Decimal("0.00")):
            return self._build_success_result(
                attempt_num=4,
                evaluation=latest_eval,
                transport=current_transport,
                hotel=current_hotel,
                days=current_days,
                downgrades=downgrades_applied,
            )

        # Still impossible after 4 attempts (no attempt 5)
        rec = (
            f"Trip remains over budget by {currency} {latest_eval.deficit} after all 4 optimization steps. "
            f"Consider increasing budget to {currency} {latest_eval.breakdown.total_allocated} "
            f"or further reducing the trip duration."
        )

        return OptimizationResult(
            initial_status="NOT_FEASIBLE",
            final_status="NOT_FEASIBLE",
            is_feasible=False,
            successful_attempt=None,
            total_attempts=4,
            final_evaluation=latest_eval,
            selected_transport=current_transport,
            selected_hotel=current_hotel,
            days=current_days,
            downgrades_applied=downgrades_applied,
            explanation=f"Trip is still impossible within {currency} {total_budget} after 4 optimization attempts.",
            deficit=latest_eval.deficit,
            recommendation=rec,
        )

    # =========================================================================
    # Downgrade Selection Helpers
    # =========================================================================
    def _downgrade_hotel(
        self,
        current_hotel: HotelOption | None,
        available_hotels: list[HotelOption] | None,
    ) -> HotelOption | None:
        if not available_hotels or not current_hotel:
            return current_hotel

        # Filter hotels strictly cheaper than current
        cheaper = [h for h in available_hotels if h.total_price < current_hotel.total_price]
        if not cheaper:
            return current_hotel

        # Sort by total_price descending to take the next closest tier down
        cheaper.sort(key=lambda h: h.total_price, reverse=True)
        return cheaper[0]

    def _downgrade_transport(
        self,
        current_transport: FlightOption | TransitOption | None,
        available_transports: list[FlightOption | TransitOption] | None,
    ) -> FlightOption | TransitOption | None:
        if not available_transports or not current_transport:
            return current_transport

        cheaper = [t for t in available_transports if t.price < current_transport.price]
        if not cheaper:
            return current_transport

        cheaper.sort(key=lambda t: t.price, reverse=True)
        return cheaper[0]

    def _record_attempt(
        self,
        trip_id: UUID | None,
        attempt_number: int,
        downgrade_type: Any,
        evaluation: BudgetEvaluationResult,
    ) -> None:
        # Skip persistence when trip_id is None (pre-persistence evaluation phase).
        # At that stage no trips row exists yet, so inserting plan_attempts would
        # violate the FK constraint. The orchestrator records attempts with a real
        # trip_id only after the trip is persisted.
        if trip_id is None:
            return
        attempt_record = PlanAttempt(
            id=uuid4(),
            trip_id=trip_id,
            attempt_number=attempt_number,
            downgrade_type=downgrade_type,
            was_feasible=evaluation.is_feasible,
            cost_calculated=evaluation.breakdown.total_allocated,
            notes=evaluation.explanation,
            created_at=utc_now(),
        )
        self.attempt_repo.record_plan_attempt(attempt_record)

    def _build_success_result(
        self,
        attempt_num: int,
        evaluation: BudgetEvaluationResult,
        transport: Any,
        hotel: Any,
        days: int,
        downgrades: list[str],
    ) -> OptimizationResult:
        return OptimizationResult(
            initial_status="NOT_FEASIBLE",
            final_status="FEASIBLE",
            is_feasible=True,
            successful_attempt=attempt_num,
            total_attempts=attempt_num,
            final_evaluation=evaluation,
            selected_transport=transport,
            selected_hotel=hotel,
            days=days,
            downgrades_applied=downgrades,
            explanation=f"Trip became feasible on attempt {attempt_num}: {downgrades[-1]}.",
            deficit=Decimal("0.00"),
        )
