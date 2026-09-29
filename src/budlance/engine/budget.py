"""Reverse-Budget Engine implementing the authoritative financial waterfall."""

import logging
from decimal import Decimal
from typing import Any
from budlance.cache.fallback import FallbackDataProvider
from budlance.db.models import BudgetAllocation
from budlance.engine.models import BudgetBreakdown, BudgetEvaluationResult, FeasibilityStatus
from budlance.schemas.travel import FlightOption, FoodEstimate, HotelOption, LocalTransitEstimate, TransitOption
from budlance.serpapi.models import DataSource

logger = logging.getLogger(__name__)


class ReverseBudgetEngine:
    """Authoritative reverse-budget calculation and feasibility evaluation engine."""

    def __init__(self, fallback_provider: FallbackDataProvider | None = None) -> None:
        self.fallback = fallback_provider or FallbackDataProvider()
        self._load_reserve_percent()

    def _load_reserve_percent(self) -> None:
        data = self.fallback.get_rate_tables()
        raw_pct = data.get("safety_rescue_reserve_percent", 0.10)
        self.reserve_percent = Decimal(str(raw_pct))

    def evaluate(
        self,
        total_budget: Decimal,
        people: int,
        days: int,
        transport: FlightOption | TransitOption | None,
        hotel: HotelOption | None,
        food_estimate: FoodEstimate,
        local_transit_estimate: LocalTransitEstimate,
        activities_budget: Decimal = Decimal("0.00"),
        currency: str = "INR",
    ) -> BudgetEvaluationResult:
        """Evaluate complete trip feasibility against the total budget constraint.

        Waterfall sequence:
        1. Reserve Rescue Fund (Bucket D).
        2. Subtract Known Fixed Costs: Transport + Stay (Bucket A).
        3. Verify Minimum Daily Allowance: Food + Local Transit (Bucket B).
        4. Allocate Activities / Discretionary Spending (Bucket C).
        5. Assert Budget Invariant: Total Allocated <= Total Budget.
        """
        if total_budget <= Decimal("0.00"):
            return self._build_infeasible_result(
                total_budget=total_budget,
                currency=currency,
                transport=transport,
                hotel=hotel,
                food_estimate=food_estimate,
                local_transit_estimate=local_transit_estimate,
                activities_budget=activities_budget,
                reason="Total declared budget must be greater than zero.",
            )

        # 1. Bucket D: Rescue Reserve
        rescue_reserve = round(total_budget * self.reserve_percent, 2)

        # 2. Bucket A: Fixed Costs (Transport + Hotel)
        transport_cost = transport.price if transport else Decimal("0.00")
        hotel_cost = hotel.total_price if hotel else Decimal("0.00")
        bucket_a_fixed = transport_cost + hotel_cost

        # 3. Bucket B: Survival / Daily (Food + Local Transit)
        food_cost = food_estimate.total_cost
        local_transit_cost = local_transit_estimate.total_cost
        bucket_b_survival = food_cost + local_transit_cost

        # 4. Mandatory minimum non-discretionary commitments
        mandatory_costs = rescue_reserve + bucket_a_fixed + bucket_b_survival

        # Build provenance map
        provenance = {
            "transport": transport.source if transport else DataSource.ESTIMATED,
            "hotel": hotel.source if hotel else DataSource.ESTIMATED,
            "food": food_estimate.source,
            "local_transit": local_transit_estimate.source,
            "rescue_reserve": DataSource.ESTIMATED,
            "activities": DataSource.ESTIMATED,
        }

        # Check if mandatory commitments already exceed user budget
        if mandatory_costs > total_budget:
            deficit = (mandatory_costs + activities_budget) - total_budget
            contributors = []
            if transport_cost > (total_budget * Decimal("0.35")):
                contributors.append(f"Transport ({currency} {transport_cost})")
            if hotel_cost > (total_budget * Decimal("0.40")):
                contributors.append(f"Accommodation ({currency} {hotel_cost})")
            if bucket_b_survival > (total_budget * Decimal("0.30")):
                contributors.append(f"Daily survival ({currency} {bucket_b_survival})")

            breakdown = BudgetBreakdown(
                total_budget=total_budget,
                currency=currency,
                bucket_a_fixed=bucket_a_fixed,
                bucket_b_survival=bucket_b_survival,
                bucket_c_activities=Decimal("0.00"),
                bucket_d_rescue=rescue_reserve,
                transport_cost=transport_cost,
                hotel_cost=hotel_cost,
                food_cost=food_cost,
                local_transit_cost=local_transit_cost,
                total_allocated=mandatory_costs,
                remaining_surplus=Decimal("0.00"),
                provenance=provenance,
            )

            explanation = (
                f"Trip exceeds budget by {currency} {deficit}. "
                f"Mandatory costs ({currency} {mandatory_costs}) alone exceed user budget of {currency} {total_budget}."
            )

            return BudgetEvaluationResult(
                status="NOT_FEASIBLE",
                is_feasible=False,
                breakdown=breakdown,
                deficit=deficit,
                explanation=explanation,
                major_cost_contributors=contributors,
            )

        # 5. Bucket C: Activities / Discretionary Allocation
        remaining_after_mandatory = total_budget - mandatory_costs
        if activities_budget > remaining_after_mandatory:
            # Over budget because of activities
            deficit = activities_budget - remaining_after_mandatory
            breakdown = BudgetBreakdown(
                total_budget=total_budget,
                currency=currency,
                bucket_a_fixed=bucket_a_fixed,
                bucket_b_survival=bucket_b_survival,
                bucket_c_activities=activities_budget,
                bucket_d_rescue=rescue_reserve,
                transport_cost=transport_cost,
                hotel_cost=hotel_cost,
                food_cost=food_cost,
                local_transit_cost=local_transit_cost,
                total_allocated=mandatory_costs + activities_budget,
                remaining_surplus=Decimal("0.00"),
                provenance=provenance,
            )
            return BudgetEvaluationResult(
                status="NOT_FEASIBLE",
                is_feasible=False,
                breakdown=breakdown,
                deficit=deficit,
                explanation=f"Trip exceeds budget by {currency} {deficit} due to discretionary activities.",
                major_cost_contributors=["Activities / Discretionary spending"],
            )

        # Successfully FEASIBLE
        bucket_c_allocated = activities_budget
        remaining_surplus = remaining_after_mandatory - bucket_c_allocated
        total_allocated = mandatory_costs + bucket_c_allocated

        # Enforce budget invariant: Total Allocations <= User Budget
        assert total_allocated <= total_budget, (
            f"Budget invariant violated: total_allocated ({total_allocated}) > total_budget ({total_budget})"
        )

        breakdown = BudgetBreakdown(
            total_budget=total_budget,
            currency=currency,
            bucket_a_fixed=bucket_a_fixed,
            bucket_b_survival=bucket_b_survival,
            bucket_c_activities=bucket_c_allocated,
            bucket_d_rescue=rescue_reserve,
            transport_cost=transport_cost,
            hotel_cost=hotel_cost,
            food_cost=food_cost,
            local_transit_cost=local_transit_cost,
            total_allocated=total_allocated,
            remaining_surplus=remaining_surplus,
            provenance=provenance,
        )

        return BudgetEvaluationResult(
            status="FEASIBLE",
            is_feasible=True,
            breakdown=breakdown,
            deficit=Decimal("0.00"),
            explanation=f"Trip is feasible within {currency} {total_budget} with a surplus of {currency} {remaining_surplus}.",
            major_cost_contributors=[],
        )

    def _build_infeasible_result(
        self,
        total_budget: Decimal,
        currency: str,
        transport: Any,
        hotel: Any,
        food_estimate: Any,
        local_transit_estimate: Any,
        activities_budget: Decimal,
        reason: str,
    ) -> BudgetEvaluationResult:
        breakdown = BudgetBreakdown(
            total_budget=total_budget,
            currency=currency,
            bucket_a_fixed=Decimal("0.00"),
            bucket_b_survival=Decimal("0.00"),
            bucket_c_activities=Decimal("0.00"),
            bucket_d_rescue=Decimal("0.00"),
            transport_cost=Decimal("0.00"),
            hotel_cost=Decimal("0.00"),
            food_cost=Decimal("0.00"),
            local_transit_cost=Decimal("0.00"),
            total_allocated=Decimal("0.00"),
            remaining_surplus=Decimal("0.00"),
        )
        return BudgetEvaluationResult(
            status="NOT_FEASIBLE",
            is_feasible=False,
            breakdown=breakdown,
            deficit=Decimal("0.00"),
            explanation=reason,
        )

    def evaluate_rescue(
        self,
        total_budget: Decimal,
        current_allocations: BudgetAllocation,
        cost_delta: Decimal,
        category: str = "activities",
        currency: str = "INR",
    ) -> BudgetEvaluationResult:
        """Authoritative mini-feasibility evaluation for Rescue Mode replanning.

        Evaluates whether a planned component replacement or price change fits within the
        trip budget and rescue reserve without violating the invariant:
        Total Allocations <= Total Budget.
        """
        if cost_delta <= Decimal("0.00"):
            # Replacement is cost-neutral or saves money
            return BudgetEvaluationResult(
                status="FEASIBLE",
                is_feasible=True,
                breakdown=BudgetBreakdown(
                    total_budget=total_budget,
                    currency=currency,
                    bucket_a_fixed=current_allocations.transport_allocated + current_allocations.stay_allocated,
                    bucket_b_survival=current_allocations.food_allocated,
                    bucket_c_activities=current_allocations.activities_discretionary + cost_delta,
                    bucket_d_rescue=current_allocations.rescue_fund_allocated,
                    transport_cost=current_allocations.transport_allocated,
                    hotel_cost=current_allocations.stay_allocated,
                    food_cost=current_allocations.food_allocated,
                    local_transit_cost=Decimal("0.00"),
                    total_allocated=total_budget + cost_delta,
                    remaining_surplus=abs(cost_delta),
                    provenance={"rescue": DataSource.ESTIMATED},
                ),
                deficit=Decimal("0.00"),
                explanation="Replacement is feasible within existing allocations (cost is equal or lower).",
                major_cost_contributors=[],
            )

        # Replacement costs extra money (cost_delta > 0)
        # Check against available rescue fund allocated (Bucket D)
        available_rescue = current_allocations.rescue_fund_allocated
        if cost_delta > available_rescue:
            deficit = cost_delta - available_rescue
            return BudgetEvaluationResult(
                status="NOT_FEASIBLE",
                is_feasible=False,
                breakdown=BudgetBreakdown(
                    total_budget=total_budget,
                    currency=currency,
                    bucket_a_fixed=current_allocations.transport_allocated + current_allocations.stay_allocated,
                    bucket_b_survival=current_allocations.food_allocated,
                    bucket_c_activities=current_allocations.activities_discretionary + cost_delta,
                    bucket_d_rescue=current_allocations.rescue_fund_allocated,
                    transport_cost=current_allocations.transport_allocated,
                    hotel_cost=current_allocations.stay_allocated,
                    food_cost=current_allocations.food_allocated,
                    local_transit_cost=Decimal("0.00"),
                    total_allocated=total_budget + deficit,
                    remaining_surplus=Decimal("0.00"),
                    provenance={"rescue": DataSource.ESTIMATED},
                ),
                deficit=deficit,
                explanation=f"Replacement exceeds available rescue reserve ({currency} {available_rescue}) by {currency} {deficit}.",
                major_cost_contributors=[f"Cost delta ({currency} {cost_delta}) exceeds rescue reserve ({currency} {available_rescue})"],
            )

        # Feasible: cost fits within rescue reserve
        return BudgetEvaluationResult(
            status="FEASIBLE",
            is_feasible=True,
            breakdown=BudgetBreakdown(
                total_budget=total_budget,
                currency=currency,
                bucket_a_fixed=current_allocations.transport_allocated + current_allocations.stay_allocated,
                bucket_b_survival=current_allocations.food_allocated,
                bucket_c_activities=current_allocations.activities_discretionary + cost_delta,
                bucket_d_rescue=available_rescue - cost_delta,
                transport_cost=current_allocations.transport_allocated,
                hotel_cost=current_allocations.stay_allocated,
                food_cost=current_allocations.food_allocated,
                local_transit_cost=Decimal("0.00"),
                total_allocated=total_budget,
                remaining_surplus=available_rescue - cost_delta,
                provenance={"rescue": DataSource.ESTIMATED},
            ),
            deficit=Decimal("0.00"),
            explanation=f"Replacement is feasible utilizing {currency} {cost_delta} from the rescue reserve.",
            major_cost_contributors=[],
        )

