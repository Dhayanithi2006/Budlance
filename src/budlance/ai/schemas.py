"""Pydantic schemas for structured travel intent and rescue message classification.

AI is strictly non-authoritative for budget math, feasibility, and fare arithmetic.
These schemas represent extracted user intent before passing to the Reverse-Budget Engine.
"""

from decimal import Decimal
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field, model_validator
from budlance.db.models import TripIntent, utc_now

TravelParty = Literal[
    "solo",
    "couple",
    "friends",
    "family",
    "relatives",
]

TransportMode = Literal["train", "flight"]
TransportClass = Literal[
    "sleeper",
    "3ac",
    "2ac",
    "1ac",
    "economy",
    "premium_economy",
    "business",
    "first",
]


TravelAction = Literal[
    "NEW_TRIP",
    "CHANGE_BUDGET",
    "CHANGE_DAYS",
    "CHANGE_PEOPLE",
    "CHANGE_DESTINATION",
    "FIND_ALTERNATIVE",
    "RESCUE",
    "LOG_EXPENSE",
    "TRIP_COMPLETE",
    "UNRECOGNIZED",
]


class TripAction(str, Enum):
    """Classified intent action from a single AI call.

    Python performs all state loading, merging, and routing based on this value.
    The LLM is NOT responsible for deciding which database state to use.
    """
    NEW_TRIP = "NEW_TRIP"
    CHANGE_BUDGET = "CHANGE_BUDGET"
    CHANGE_DAYS = "CHANGE_DAYS"
    CHANGE_PEOPLE = "CHANGE_PEOPLE"
    CHANGE_DESTINATION = "CHANGE_DESTINATION"
    CHANGE_TRANSPORT = "CHANGE_TRANSPORT"
    CONFIRM_BOOKING = "CONFIRM_BOOKING"
    FIND_ALTERNATIVE = "FIND_ALTERNATIVE"
    RESCUE = "RESCUE"
    LOG_EXPENSE = "LOG_EXPENSE"
    TRIP_COMPLETE = "TRIP_COMPLETE"
    UNRECOGNIZED = "UNRECOGNIZED"


def normalize_transport_mode(val: Any) -> TransportMode | None:
    """Normalize transport mode string to canonical TransportMode Literal."""
    if not val:
        return None
    s = str(val).strip().lower()
    if s in ("train", "rail", "railway"):
        return "train"
    if s in ("flight", "plane", "air"):
        return "flight"
    return None


def normalize_transport_class(val: Any) -> TransportClass | None:
    """Normalize transport class string to canonical TransportClass Literal."""
    if not val:
        return None
    s = str(val).strip().lower().replace("-", " ").replace("_", " ")
    if s in ("1ac", "1a", "first ac", "1st ac", "1st class ac", "first class ac"):
        return "1ac"
    if s in ("2ac", "2a", "second ac", "2nd ac", "2nd class ac"):
        return "2ac"
    if s in ("3ac", "3a", "third ac", "3rd ac", "3rd class ac"):
        return "3ac"
    if s in ("sleeper", "sl"):
        return "sleeper"
    if s in ("economy", "coach"):
        return "economy"
    if s in ("premium economy", "pe", "premium_economy"):
        return "premium_economy"
    if s in ("business", "business class"):
        return "business"
    if s in ("first", "first class"):
        return "first"
    if "1ac" in s or "first ac" in s or "1st ac" in s:
        return "1ac"
    if "2ac" in s or "second ac" in s or "2nd ac" in s:
        return "2ac"
    if "3ac" in s or "third ac" in s or "3rd ac" in s:
        return "3ac"
    if "sleeper" in s:
        return "sleeper"
    if "premium" in s:
        return "premium_economy"
    if "business" in s:
        return "business"
    if "economy" in s:
        return "economy"
    if "first" in s:
        return "first"
    return None


def normalize_expense_category(val: Any) -> str | None:
    """Normalize expense category according to Budlance conventions."""
    if not val:
        return None
    s = str(val).strip().lower()
    if any(k in s for k in ("activity", "activities", "entry", "ticket", "tickets", "sightseeing", "tour", "museum", "safari", "scuba", "watersports")):
        return "activities"
    if any(k in s for k in ("food", "lunch", "dinner", "breakfast", "meal", "cafe", "restaurant", "snack", "water", "tea", "coffee")):
        return "food"
    if any(k in s for k in ("transport", "transit", "travel", "auto", "autos", "cab", "cabs", "taxi", "taxis", "bus", "train", "flight", "metro", "rickshaw", "fuel")):
        return "transport"
    if any(k in s for k in ("hotel", "stay", "room", "hostel", "resort", "accommodation", "lodging")):
        return "stay"
    return s


class ParsedTripIntent(BaseModel):
    """Structured travel intent parsed from user natural-language input.

    The `action` field classifies the conversational intent so the orchestrator can
    choose the correct state source and merge rule without a second AI call.
    """
    model_config = ConfigDict(from_attributes=True)

    action: TripAction = Field(
        default=TripAction.NEW_TRIP,
        description=(
            "Conversational action classification. "
            "NEW_TRIP = fresh request; CHANGE_* = field update; "
            "FIND_ALTERNATIVE = reuse context but reopen destination; "
            "RESCUE = in-trip problem; UNRECOGNIZED = unclear."
        ),
    )
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
    travel_party: TravelParty | None = Field(
        default=None,
        description="Explicit travel party relationship: solo, couple, friends, family, relatives.",
    )
    traveler_type: str | None = Field(
        default=None,
        description="Traveler persona e.g. solo, couple, family, friends.",
    )
    rescue_detail: str | None = Field(
        default=None,
        description="Summary of in-trip issue when action=RESCUE. Null for planning intents.",
    )
    transport_mode: TransportMode | None = Field(
        default=None,
        description="Transport mode preference: train, flight, or None.",
    )
    transport_class: TransportClass | None = Field(
        default=None,
        description="Transport class preference: sleeper, 3ac, 2ac, 1ac, economy, premium_economy, business, first, or None.",
    )
    booking_confirmed: bool = Field(
        default=False,
        description="Whether user has confirmed completing external transport booking.",
    )
    amount: Decimal | None = Field(
        default=None,
        description="User's stated actual expense amount when reporting spending.",
    )
    expense_category: str | None = Field(
        default=None,
        description="Category of the expense (e.g. food, transport, activities, stay, general).",
    )
    day_number: int | None = Field(
        default=None,
        description="Day number (1-indexed) if explicitly mentioned or unambiguously expressed.",
    )
    day_completed: bool = Field(
        default=False,
        description="True if user explicitly stated the day is completed/done.",
    )
    pending_action: str | None = Field(
        default=None,
        description="Pending conversational action awaiting user response (e.g. LOG_ACTUAL_SPEND).",
    )
    reconciling_trip_id: str | None = Field(
        default=None,
        description="Trip ID currently undergoing reconciliation, if any.",
    )
    completion_reason: str | None = Field(
        default=None,
        description="Optional reason stated by user for completing or closing the trip.",
    )

    @model_validator(mode="before")
    @classmethod
    def _sync_travel_party(cls, data: Any) -> Any:
        if isinstance(data, dict):
            tp = data.get("travel_party")
            tt = data.get("traveler_type")
            if tp and not tt:
                data["traveler_type"] = tp
            elif tt and not tp and tt in ("solo", "couple", "friends", "family", "relatives"):
                data["travel_party"] = tt
            if "transport_mode" in data and data["transport_mode"] is not None:
                data["transport_mode"] = normalize_transport_mode(data["transport_mode"])
            if "transport_class" in data and data["transport_class"] is not None:
                data["transport_class"] = normalize_transport_class(data["transport_class"])
            if "expense_category" in data and data["expense_category"] is not None:
                data["expense_category"] = normalize_expense_category(data["expense_category"])
            if "amount" in data and data["amount"] is not None:
                try:
                    data["amount"] = Decimal(str(data["amount"]).replace(",", ""))
                except Exception:
                    pass
        return data

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

    def merge_with(self, update: "ParsedTripIntent") -> "ParsedTripIntent":
        """Return a new ParsedTripIntent merging known values from this intent with an update.

        Fields provided (non-None) in `update` overwrite the corresponding field here.
        Fields absent (None) in `update` fall back to the existing value.
        Interests are merged (union, preserving order).
        The `action` is taken from the update so routing always reflects the latest intent.
        """
        merged_interests = list(self.interests)
        for i in update.interests:
            if i not in merged_interests:
                merged_interests.append(i)

        party = update.travel_party if update.travel_party is not None else self.travel_party
        ttype = update.traveler_type if update.traveler_type is not None else self.traveler_type
        if party and not ttype:
            ttype = party
        elif ttype and not party and ttype in ("solo", "couple", "friends", "family", "relatives"):
            party = ttype

        return ParsedTripIntent(
            action=update.action,
            budget=update.budget if update.budget is not None else self.budget,
            currency=update.currency if update.currency != "INR" or self.currency == "INR" else self.currency,
            people=update.people if update.people is not None else self.people,
            days=update.days if update.days is not None else self.days,
            origin=update.origin if update.origin is not None else self.origin,
            destination=update.destination if update.destination is not None else self.destination,
            interests=merged_interests,
            travel_party=party,
            traveler_type=ttype,
            transport_mode=update.transport_mode if update.transport_mode is not None else self.transport_mode,
            transport_class=update.transport_class if update.transport_class is not None else self.transport_class,
            booking_confirmed=update.booking_confirmed or self.booking_confirmed,
            rescue_detail=update.rescue_detail,
            amount=update.amount if update.amount is not None else self.amount,
            expense_category=update.expense_category if update.expense_category is not None else self.expense_category,
            day_number=update.day_number if update.day_number is not None else self.day_number,
            day_completed=update.day_completed or self.day_completed,
        )

    def apply_change_action(self, update: "ParsedTripIntent") -> "ParsedTripIntent":
        """Apply a CHANGE_* action: only the field relevant to the action is overwritten.

        All other fields are preserved from `self`. This is stricter than merge_with(),
        which merges any non-None field from the update.
        """
        action = update.action
        if action == TripAction.CHANGE_BUDGET:
            return self.model_copy(update={"budget": update.budget, "action": action})
        if action == TripAction.CHANGE_DAYS:
            return self.model_copy(update={"days": update.days, "action": action})
        if action == TripAction.CHANGE_PEOPLE:
            upd: dict[str, Any] = {"people": update.people, "action": action}
            if update.travel_party is not None:
                upd["travel_party"] = update.travel_party
                upd["traveler_type"] = update.traveler_type or update.travel_party
            elif self.travel_party == "couple" and update.people is not None and update.people != 2:
                upd["travel_party"] = None
                upd["traveler_type"] = None
            elif self.travel_party == "solo" and update.people is not None and update.people > 1:
                upd["travel_party"] = None
                upd["traveler_type"] = None
            return self.model_copy(update=upd)
        if action == TripAction.CHANGE_DESTINATION:
            return self.model_copy(update={"destination": update.destination, "action": action})
        if action == TripAction.CHANGE_TRANSPORT:
            upd: dict[str, Any] = {"action": action}
            if update.transport_mode is not None:
                upd["transport_mode"] = update.transport_mode
            if update.transport_class is not None:
                upd["transport_class"] = update.transport_class
            return self.model_copy(update=upd)
        if action == TripAction.FIND_ALTERNATIVE:
            return self.model_copy(update={"destination": None, "action": action})
        # Fallback: full merge
        return self.merge_with(update)

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
            travel_party=self.travel_party,
            traveler_type=self.traveler_type or self.travel_party,
            transport_mode=self.transport_mode,
            transport_class=self.transport_class,
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


# Alias for compatibility with project specifications
ParsedIntent = ParsedTripIntent

