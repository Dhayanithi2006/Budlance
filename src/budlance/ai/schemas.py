"""Pydantic schemas for structured travel intent and rescue message classification.

AI is strictly non-authoritative for budget math, feasibility, and fare arithmetic.
These schemas represent extracted user intent before passing to the Reverse-Budget Engine.
"""

from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field
from budlance.db.models import TripIntent, utc_now


class ParsedTripIntent(BaseModel):
    """Structured travel intent parsed from user natural-language input."""
    model_config = ConfigDict(from_attributes=True)

    budget: Decimal | None = Field(
        default=None,
        description="Total trip budget declared by user. None if unspecified.",
    )
    currency: str = Field(
        default="INR",
        description="Currency code (e.g. INR, USD, EUR). Default is INR.",
    )
    people: int | None = Field(
        default=None,
        description="Number of travelers. None if unspecified.",
    )
    days: int | None = Field(
        default=None,
        description="Duration of the trip in days. None if unspecified.",
    )
    origin: str | None = Field(
        default=None,
        description="Departure city/airport. None if unspecified.",
    )
    destination: str | None = Field(
        default=None,
        description="Target destination. None if user wants Budlance to discover places.",
    )
    interests: list[str] = Field(
        default_factory=list,
        description="User interests e.g. beaches, local_food, nature, culture.",
    )
    traveler_type: str | None = Field(
        default=None,
        description="Traveler persona e.g. solo, couple, family, friends.",
    )

    @property
    def needs_destination_discovery(self) -> bool:
        """True if destination is not provided and needs reverse-budget discovery."""
        return self.destination is None or not self.destination.strip()

    @property
    def missing_fields(self) -> list[str]:
        """Return list of fundamental required fields missing for reverse-budget planning."""
        missing = []
        if self.budget is None or self.budget <= 0:
            missing.append("budget")
        if self.people is None or self.people <= 0:
            missing.append("people")
        if self.days is None or self.days <= 0:
            missing.append("days")
        return missing

    @property
    def is_plannable(self) -> bool:
        """True if core numerical constraints (budget, people, days) are available."""
        return len(self.missing_fields) == 0

    def to_trip_intent_record(self, trip_id: UUID, raw_prompt: str | None = None) -> TripIntent:
        """Convert validated intent to a database TripIntent entity."""
        if not self.is_plannable:
            raise ValueError(f"Cannot persist incomplete intent to database. Missing: {self.missing_fields}")

        return TripIntent(
            id=uuid4(),
            trip_id=trip_id,
            budget=self.budget,  # type: ignore[arg-type]
            currency=self.currency,
            people=self.people,  # type: ignore[arg-type]
            days=self.days,  # type: ignore[arg-type]
            origin=self.origin,
            destination=self.destination,
            interests=self.interests,
            traveler_type=self.traveler_type,
            raw_prompt=raw_prompt,
            extracted_at=utc_now(),
        )


RescueTypeClassification = Literal["weather_closure", "price_dispute", "unknown"]


class ParsedRescueIntent(BaseModel):
    """Structured intent parsed from in-trip rescue messages."""
    model_config = ConfigDict(from_attributes=True)

    rescue_type: RescueTypeClassification = Field(
        description="Class of rescue event: weather_closure, price_dispute, or unknown.",
    )
    user_issue: str = Field(
        description="Extracted summary of the obstacle or dispute reported by user.",
    )
    location_or_context: str | None = Field(
        default=None,
        description="Place, city, or attraction context if mentioned.",
    )
    reported_price: Decimal | None = Field(
        default=None,
        description="Price or fare quoted in dispute (e.g. ₹500 for auto).",
    )
    service_type: str | None = Field(
        default=None,
        description="Category of service involved (e.g. auto, taxi, entry_fee, hotel).",
    )
    raw_message: str = Field(
        default="",
        description="Original user message.",
    )
