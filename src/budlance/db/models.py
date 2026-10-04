"""Domain models and database entity representations for Budlance.

Represents all 14 frozen schema entities defined in docs/PROJECT_SPEC.md:
- User
- Trip
- TripIntent
- TripOption
- FlightOption
- HotelOption
- PlaceOption
- Itinerary
- BudgetAllocation
- LedgerEntry
- PlanAttempt
- RescueEvent
- SearchCache
- ApiUsage
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    """Return current timestamp in UTC."""
    return datetime.now(timezone.utc)


# ============================================================================
# 1. User
# ============================================================================
class User(BaseModel):
    """User entity linked to a Telegram user account."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    telegram_user_id: int
    username: str | None = None
    first_name: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


# ============================================================================
# 2. Trip
# ============================================================================
TripStatus = Literal["PLANNING", "ACTIVE", "COMPLETED"]


class Trip(BaseModel):
    """Core Trip entity anchoring user intents, budget, itineraries, and rescues."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    telegram_chat_id: int
    destination: str | None = None
    origin: str | None = None
    budget_total: Decimal
    currency: str = "INR"
    people_count: int = 1
    duration_days: int = 1
    status: TripStatus = "PLANNING"
    current_day: int = Field(default=1, ge=1)
    is_active: bool = True  # Used by Rescue Mode to locate the current active trip
    completion_reason: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, v: Any) -> str:
        """Normalize case-insensitive status values to authoritative canonical uppercase."""
        if isinstance(v, str):
            v_up = v.strip().upper()
            if v_up in ("PLANNING", "ACTIVE", "COMPLETED"):
                return v_up
            raise ValueError(f"Invalid trip status: '{v}'. Canonical statuses are: PLANNING, ACTIVE, COMPLETED")
        if v is None:
            return "PLANNING"
        raise ValueError(f"Invalid trip status type: {type(v)}. Expected string.")


# ============================================================================
# 3. Trip Intent
# ============================================================================
class TripIntent(BaseModel):
    """Extracted trip intent parsed from user text messages."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    trip_id: UUID
    budget: Decimal
    currency: str = "INR"
    people: int
    days: int
    origin: str | None = None
    destination: str | None = None
    interests: list[str] = Field(default_factory=list)
    travel_party: str | None = None
    traveler_type: str | None = None
    transport_mode: str | None = None
    transport_class: str | None = None
    raw_prompt: str | None = None
    extracted_at: datetime = Field(default_factory=utc_now)


# ============================================================================
# 4. Trip Option
# ============================================================================
class TripOption(BaseModel):
    """Parent container for candidate transport/stay/places combinations."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    trip_id: UUID
    option_tier: str = "standard"  # standard, budget, relaxed
    total_estimated_cost: Decimal
    is_selected: bool = False
    created_at: datetime = Field(default_factory=utc_now)


# ============================================================================
# 5. Flight Option
# ============================================================================
class FlightOption(BaseModel):
    """Live or fallback flight option."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    trip_option_id: UUID
    airline: str | None = None
    flight_number: str | None = None
    departure_airport: str | None = None
    arrival_airport: str | None = None
    departure_time: datetime | None = None
    arrival_time: datetime | None = None
    price: Decimal
    currency: str = "INR"
    deep_link: str | None = None
    is_fallback: bool = False
    created_at: datetime = Field(default_factory=utc_now)


# ============================================================================
# 6. Hotel Option
# ============================================================================
class HotelOption(BaseModel):
    """Live or fallback hotel accommodation option."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    trip_option_id: UUID
    name: str
    hotel_class: int | None = None  # 1 to 5 stars
    address: str | None = None
    price_per_night: Decimal
    total_price: Decimal
    currency: str = "INR"
    rating: float | None = None
    review_count: int | None = None
    deep_link: str | None = None
    is_fallback: bool = False
    created_at: datetime = Field(default_factory=utc_now)


# ============================================================================
# 7. Place Option
# ============================================================================
class PlaceOption(BaseModel):
    """Attraction, dining, beach, or local place candidate."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    trip_option_id: UUID
    name: str
    category: str | None = None  # beach, restaurant, attraction, nature
    address: str | None = None
    rating: float | None = None
    estimated_cost: Decimal = Decimal("0.00")
    is_fallback: bool = False
    created_at: datetime = Field(default_factory=utc_now)


# ============================================================================
# 8. Itinerary
# ============================================================================
class Itinerary(BaseModel):
    """Persisted day-by-day travel schedule."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    trip_id: UUID
    days: list[dict[str, Any]] = Field(default_factory=list)
    is_feasible: bool = True
    feasibility_note: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


# ============================================================================
# 9. Budget Allocation
# ============================================================================
class BudgetAllocation(BaseModel):
    """Reverse-budget waterfall allocation buckets."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    trip_id: UUID
    transport_allocated: Decimal = Decimal("0.00")
    stay_allocated: Decimal = Decimal("0.00")
    food_allocated: Decimal = Decimal("0.00")
    activities_discretionary: Decimal = Decimal("0.00")
    rescue_fund_allocated: Decimal = Decimal("0.00")
    total_budget: Decimal
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


# ============================================================================
# 10. Ledger Entry
# ============================================================================
LedgerCategory = Literal["fixed_booking", "daily_survival", "activities", "rescue"]
ExpenseSource = Literal["live", "estimated", "fallback", "user_reported"]


class LedgerEntry(BaseModel):
    """Granular line-item budget ledger entry."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    trip_id: UUID
    category: LedgerCategory
    description: str
    allocated_amount: Decimal = Decimal("0.00")
    planned_amount: Decimal = Decimal("0.00")
    spent_amount: Decimal = Decimal("0.00")
    remaining_amount: Decimal = Decimal("0.00")
    actual_amount: Decimal | None = None
    day_number: int | None = None
    source: ExpenseSource = "estimated"
    created_at: datetime = Field(default_factory=utc_now)


# ============================================================================
# 11. Plan Attempt
# ============================================================================
DowngradeType = Literal[
    "hotel_tier_down",
    "transport_class_down",
    "reduce_trip_length",
    "trim_discretionary_b",
]


class PlanAttempt(BaseModel):
    """Record of an optimization loop attempt (maximum 4)."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    trip_id: UUID
    attempt_number: int = Field(ge=1, le=4)
    downgrade_type: DowngradeType
    was_feasible: bool
    cost_calculated: Decimal
    notes: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


# ============================================================================
# 12. Rescue Event
# ============================================================================
RescueType = Literal["weather_closure", "price_dispute", "unknown"]


class RescueEvent(BaseModel):
    """Rescue Mode execution record triggered during an active trip."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    trip_id: UUID
    rescue_type: RescueType
    user_message: str
    resolution_summary: str
    ledger_impact: Decimal = Decimal("0.00")
    created_at: datetime = Field(default_factory=utc_now)


# ============================================================================
# 13. Search Cache
# ============================================================================
class SearchCache(BaseModel):
    """SerpApi query cache record stored in Supabase PostgreSQL."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    query_hash: str
    engine: str
    params_json: dict[str, Any]
    response_data: dict[str, Any]
    expires_at: datetime
    created_at: datetime = Field(default_factory=utc_now)


# ============================================================================
# 14. API Usage
# ============================================================================
class ApiUsage(BaseModel):
    """Tracks live SerpApi vs cached calls for auditing and cost efficiency."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    trip_id: UUID | None = None
    engine: str
    call_count: int = 1
    cached_count: int = 0
    created_at: datetime = Field(default_factory=utc_now)


# ============================================================================
# 15. Trip Pass (Monetization & Access Control)
# ============================================================================
PassStatus = Literal["FREE", "CHECKOUT_PENDING", "PAID", "PAYMENT_FAILED", "PAYMENT_ABANDONED"]


class TripPass(BaseModel):
    """Trip Pass monetization entity unlocking premium planning and rescue for a specific trip."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    trip_id: UUID
    telegram_user_id: int
    telegram_chat_id: int
    amount: Decimal = Decimal("49.00")
    currency: str = "INR"
    provider: str = "razorpay"  # razorpay, stripe, demo
    payment_reference: str | None = None
    status: PassStatus = "FREE"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def checkout_url(self) -> str | None:
        """Convenience property accessing checkout URL from pass metadata."""
        return self.metadata.get("checkout_url")

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, v: Any) -> str:
        """Normalize case-insensitive status values to authoritative canonical uppercase."""
        if isinstance(v, str):
            v_up = v.strip().upper()
            if v_up in ("FREE", "CHECKOUT_PENDING", "PAID", "PAYMENT_FAILED", "PAYMENT_ABANDONED"):
                return v_up
            raise ValueError(
                f"Invalid pass status: '{v}'. Canonical statuses are: FREE, CHECKOUT_PENDING, PAID, PAYMENT_FAILED, PAYMENT_ABANDONED"
            )
        if v is None:
            return "FREE"
        raise ValueError(f"Invalid pass status type: {type(v)}. Expected string.")
