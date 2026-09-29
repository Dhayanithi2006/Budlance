"""Pydantic domain models for Rescue Mode replanning requests and results."""

from decimal import Decimal
from typing import Any
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field

from budlance.itinerary.models import GeneratedItinerary
from budlance.ledger.models import LedgerSummary
from budlance.schemas.travel import PlaceOption


class RescueRequest(BaseModel):
    """Input payload for a live trip rescue operation."""
    model_config = ConfigDict(from_attributes=True)

    telegram_chat_id: int
    user_message: str
    current_location: str | None = None


class FareGuidance(BaseModel):
    """Advisory transit fare evaluation comparing reported vs estimated fair rates."""
    model_config = ConfigDict(from_attributes=True)

    mode: str
    reported_price: Decimal
    estimated_fare: Decimal
    difference: Decimal
    rate_per_km: Decimal | None = None
    distance_km: float | None = None
    status: str  # "fair", "slightly_high", "significantly_high"
    advisory_notes: str


class RescueResult(BaseModel):
    """Structured result returned by the Rescue Service after replanning evaluation."""
    model_config = ConfigDict(from_attributes=True)

    trip_id: UUID | None = None
    success: bool
    rescue_type: str  # "weather_closure", "price_dispute", "unknown", "none"
    user_issue: str
    resolution_summary: str
    is_feasible: bool = True
    budget_impact: Decimal = Decimal("0.00")
    fare_guidance: FareGuidance | None = None
    selected_alternative: PlaceOption | None = None
    updated_itinerary: GeneratedItinerary | None = None
    ledger_summary: LedgerSummary | None = None
    error: str | None = None
