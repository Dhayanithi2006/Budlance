"""Normalized internal travel domain models.

These models decouple downstream planning and the Reverse-Budget Engine
from external SerpApi and static JSON response structures.
"""

from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field
from budlance.serpapi.models import DataSource


class FlightOption(BaseModel):
    """Normalized flight transport option."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    airline: str | None = None
    flight_number: str | None = None
    departure_airport: str | None = None
    arrival_airport: str | None = None
    departure_time: str | None = None
    arrival_time: str | None = None
    price: Decimal
    currency: str = "INR"
    duration_minutes: int | None = None
    stops: int = 0
    deep_link: str | None = None
    seller: str | None = None
    booking_token: str | None = None
    booking_request: dict[str, Any] | None = None
    outbound_date: str | None = None
    return_date: str | None = None
    is_exact_booking: bool = False
    return_flight_number: str | None = None
    return_departure_time: str | None = None
    return_arrival_time: str | None = None
    source: DataSource = DataSource.LIVE
    is_fallback: bool = False


class HotelOption(BaseModel):
    """Normalized accommodation option."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    name: str
    hotel_class: int | None = None  # 1 to 5 stars
    address: str | None = None
    price_per_night: Decimal | None = None
    total_price: Decimal
    currency: str = "INR"
    rating: float | None = None
    review_count: int | None = None
    deep_link: str | None = None
    source: DataSource = DataSource.LIVE
    is_fallback: bool = False


class PlaceOption(BaseModel):
    """Normalized attraction, beach, dining, or nature place candidate."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    name: str
    category: str | None = None  # beach, restaurant, attraction, nature, temple
    address: str | None = None
    rating: float | None = None
    review_count: int | None = None
    price_level: str | None = None  # coarse indicator like "₹₹" or "budget", never claimed as exact menu price
    estimated_cost: Decimal = Decimal("0.00")
    source: DataSource = DataSource.LIVE
    is_fallback: bool = False


class RouteOption(BaseModel):
    """Normalized route navigation option."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    origin: str
    destination: str
    distance_km: float
    duration_minutes: int
    summary: str | None = None
    source: DataSource = DataSource.LIVE
    is_fallback: bool = False


class TransitOption(BaseModel):
    """Normalized intercity train or bus transit option (primarily static fallback)."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    transit_type: Literal["train", "bus"]
    origin: str
    destination: str
    name_or_operator: str
    distance_km: float | None = None
    duration_hours: float | None = None
    price: Decimal
    currency: str = "INR"
    class_or_type: str | None = None  # e.g., "3A", "SL", "AC Sleeper"
    source: DataSource = DataSource.FALLBACK
    is_fallback: bool = True


class FoodEstimate(BaseModel):
    """Structured food cost estimate derived from configurable rate tables."""
    model_config = ConfigDict(from_attributes=True)

    tier: str  # budget, standard, comfort
    daily_cost_per_person: Decimal
    total_cost: Decimal
    people: int
    days: int
    currency: str = "INR"
    source: DataSource = DataSource.ESTIMATED


class LocalTransitEstimate(BaseModel):
    """Advisory local transit cost estimate based on route distance or day rate."""
    model_config = ConfigDict(from_attributes=True)

    mode: str  # auto, cab, metro_bus
    rate_per_km: Decimal | None = None
    distance_km: float | None = None
    days: int | None = None
    total_cost: Decimal
    currency: str = "INR"
    source: DataSource = DataSource.ESTIMATED
