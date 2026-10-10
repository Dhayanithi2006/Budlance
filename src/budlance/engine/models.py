"""Budget engine models and result containers."""

from decimal import Decimal
from typing import Any, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field
from budlance.schemas.travel import FlightOption, HotelOption, TransitOption
from budlance.serpapi.models import DataSource

FeasibilityStatus = Literal[
    "FEASIBLE",
    "NOT_FEASIBLE",
    "INCOMPLETE_COST_DATA",
    "BOUNDED_SEARCH_NO_FEASIBLE_OPTION",
]


class BudgetBreakdown(BaseModel):
    """Detailed breakdown of waterfall allocations and sources."""
    model_config = ConfigDict(from_attributes=True)

    total_budget: Decimal
    currency: str = "INR"

    # The 4 Budlance conceptual buckets
    bucket_a_fixed: Decimal = Field(description="Fixed costs: Transport + Stay")
    bucket_b_survival: Decimal = Field(description="Survival/Daily costs: Food + Local Transit")
    bucket_c_activities: Decimal = Field(description="Discretionary/Fun budget")
    bucket_d_rescue: Decimal = Field(description="Emergency reserve fund")

    # Granular allocations
    transport_cost: Decimal
    hotel_cost: Decimal
    food_cost: Decimal
    local_transit_cost: Decimal
    attraction_cost: Decimal = Field(default=Decimal("0.00"), description="Curated attraction entry fees")

    # Balance
    total_allocated: Decimal
    remaining_surplus: Decimal
    projected_trip_cost: Decimal | None = Field(default=None, description="Forecast total for proposed trip including reserve")
    has_unknown_attraction_fees: bool = Field(default=False, description="True if any selected attraction has an unknown admission fee")
    unknown_attraction_names: list[str] = Field(default_factory=list, description="Names of attractions with unknown admission fees")

    # Cost provenance mapping
    provenance: dict[str, DataSource] = Field(default_factory=dict)

    def is_reconciled(self) -> bool:
        """Verify authoritative reconciliation invariant: total_allocated + remaining_surplus == total_budget."""
        return (self.total_allocated + self.remaining_surplus) == self.total_budget


class BudgetEvaluationResult(BaseModel):
    """Authoritative decision output produced by ReverseBudgetEngine."""
    model_config = ConfigDict(from_attributes=True)

    status: FeasibilityStatus
    is_feasible: bool
    breakdown: BudgetBreakdown
    deficit: Decimal = Decimal("0.00")
    explanation: str
    major_cost_contributors: list[str] = Field(default_factory=list)
    missing_cost_items: list[str] = Field(default_factory=list)
    search_bounded: bool = Field(default=False)


class OptimizationResult(BaseModel):
    """Output from OptimizationEngine summarizing up to 4 downgrade attempts."""
    model_config = ConfigDict(from_attributes=True)

    initial_status: FeasibilityStatus
    final_status: FeasibilityStatus
    is_feasible: bool
    successful_attempt: int | None = None
    total_attempts: int
    final_evaluation: BudgetEvaluationResult

    # Selected trip components after optimization
    selected_transport: FlightOption | TransitOption | None = None
    selected_hotel: HotelOption | None = None
    days: int

    downgrades_applied: list[str] = Field(default_factory=list)
    alternatives_available: list[str] = Field(default_factory=list, description="Explicit constraint alternatives requiring user approval")
    explanation: str
    deficit: Decimal = Decimal("0.00")
    recommendation: str | None = None
