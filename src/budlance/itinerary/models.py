"""Models representing structured day-by-day itineraries."""

from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field, field_validator
from budlance.serpapi.models import DataSource


class ItineraryItem(BaseModel):
    """Individual scheduled event, transit leg, or activity within a day."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    time_slot: str  # "Morning", "Afternoon", "Evening"
    activity: str
    place_name: str | None = None
    category: str  # "transport", "accommodation", "food", "attraction", "nature", "beach", "local_transit"
    planned_cost: Decimal = Decimal("0.00")
    source: DataSource = DataSource.LIVE
    notes: str | None = None
    # Real attraction and structured free-time metadata
    is_curated: bool = False
    attraction_name: str | None = None
    opening_hours: str | None = None
    entry_fee_inr: int = 0
    description: str = ""
    slot_type: str | None = None
    suggestion: str | None = None


DayStatus = Literal["UPCOMING", "IN_PROGRESS", "COMPLETED", "MODIFIED"]


class ItineraryDay(BaseModel):
    """Full schedule for a single day of travel."""
    model_config = ConfigDict(from_attributes=True)

    day_number: int
    theme_or_summary: str
    items: list[ItineraryItem] = Field(default_factory=list)
    daily_estimated_cost: Decimal = Decimal("0.00")
    status: DayStatus = "UPCOMING"

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, v: Any) -> Any:
        if isinstance(v, str):
            v_up = v.upper()
            if v_up in ("UPCOMING", "IN_PROGRESS", "COMPLETED", "MODIFIED"):
                return v_up
        return v


DayPlan = ItineraryDay


class GeneratedItinerary(BaseModel):
    """Complete day-by-day itinerary derived from an authoritative FEASIBLE budget."""
    model_config = ConfigDict(from_attributes=True)

    trip_id: UUID
    destination: str
    days_count: int
    days: list[ItineraryDay] = Field(default_factory=list)
    is_feasible: bool = True
    total_budget: Decimal
    total_planned_cost: Decimal = Decimal("0.00")
    feasibility_note: str | None = None
