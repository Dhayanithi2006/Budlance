"""Models representing structured day-by-day itineraries."""

from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field
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


class ItineraryDay(BaseModel):
    """Full schedule for a single day of travel."""
    model_config = ConfigDict(from_attributes=True)

    day_number: int
    theme_or_summary: str
    items: list[ItineraryItem] = Field(default_factory=list)
    daily_estimated_cost: Decimal = Decimal("0.00")


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
