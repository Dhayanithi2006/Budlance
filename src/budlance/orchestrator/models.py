"""Orchestration result models and status representations."""

from decimal import Decimal
from typing import Any
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field

from budlance.ai.schemas import TripAction
from budlance.engine.models import BudgetBreakdown, FeasibilityStatus
from budlance.itinerary.models import GeneratedItinerary
from budlance.ledger.models import LedgerSummary
from budlance.schemas.travel import FlightOption, HotelOption, PlaceOption, RouteOption, TransitOption


class OrchestrationResult(BaseModel):
    """Unified result container returned by the Budlance Orchestrator for Telegram presentation."""
    model_config = ConfigDict(from_attributes=True)

    trip_id: UUID | None = None
    status: str = Field(
        description="High-level orchestration status: FEASIBLE, NOT_FEASIBLE, CLARIFICATION, RESCUE, ERROR.",
    )
    action: TripAction | str | None = None
    selected_destination: str | None = None
    feasibility_status: FeasibilityStatus | None = None
    selected_transport: FlightOption | TransitOption | None = None
    selected_hotel: HotelOption | None = None
    selected_places: list[PlaceOption] = Field(default_factory=list)
    selected_route: RouteOption | None = None
    budget_breakdown: BudgetBreakdown | None = None
    optimization_attempts: int = 0
    downgrades_applied: list[str] = Field(default_factory=list)
    generated_itinerary: GeneratedItinerary | None = None
    ledger_summary: LedgerSummary | None = None
    provenance: dict[str, str] = Field(default_factory=dict)
    is_pass_unlocked: bool = True
    pass_status: str | None = None
    checkout_url: str | None = None
    search_scope: str | None = Field(
        default=None,
        description="Scope of destination search: 'bounded', 'exhaustive', or None for direct queries.",
    )
    evaluated_candidates_count: int = Field(
        default=0,
        description="Total destination candidates evaluated during candidate screening.",
    )
    message_text: str = Field(
        default="",
        description="Formatted, user-readable response text ready for Telegram delivery.",
    )
    error: str | None = None
