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
    "IN_TRIP_QUERY",
    "MANAGE_BOOKING",
    "UNRECOGNIZED",
]


class TripAction(str, Enum):
    """Classified intent action from a single AI call.

    Python performs all state loading, merging, and routing based on this value.
    The LLM is NOT responsible for deciding which database state to use.
    """
    NEW_TRIP = "NEW_TRIP"
    MODIFY_TRIP = "MODIFY_TRIP"
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
    IN_TRIP_QUERY = "IN_TRIP_QUERY"
    MANAGE_BOOKING = "MANAGE_BOOKING"
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


class ExpenseItem(BaseModel):
    """Individual line-item expense extracted from natural language."""
    model_config = ConfigDict(from_attributes=True)

    amount: Decimal
    category: str = "general"
    description: str | None = None


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
    requested_destination: str | None = Field(
        default=None,
        description="Explicit named destination requested by user; skips destination discovery.",
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
    complete_day: bool = Field(
        default=False,
        description="Alias for day_completed.",
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
    is_delta: bool = Field(
        default=False,
        description=(
            "True when the user said 'add X to budget' / 'increase by X' (relative delta). "
            "False when the user stated an absolute new budget value."
        ),
    )
    budget_delta: Decimal | None = Field(
        default=None,
        description="The incremental amount to add to the existing budget when is_delta=True. None for absolute changes.",
    )
    start_date: str | None = Field(
        default=None,
        description="Trip departure / Day 1 date in YYYY-MM-DD ISO format if explicitly stated.",
    )
    end_date: str | None = Field(
        default=None,
        description="Trip return / Day N date in YYYY-MM-DD ISO format if explicitly stated.",
    )
    date_is_explicit: bool = Field(
        default=False,
        description="True if start date was explicitly provided or unambiguously resolved.",
    )
    date_is_ambiguous: bool = Field(
        default=False,
        description="True if user mentioned an ambiguous relative date phrase.",
    )
    date_ambiguous_phrase: str | None = Field(
        default=None,
        description="The ambiguous relative date phrase used by the user, if any.",
    )
    date_confirmed: bool = Field(
        default=False,
        description="True if user explicitly confirmed or provided the travel dates.",
    )
    event_id: str | None = Field(
        default=None,
        description="Stable incoming update/event identifier for idempotency.",
    )
    hotel_tier: str | None = Field(
        default=None,
        description="Explicit hotel tier preference e.g. 4-star, 5-star, luxury, budget, standard.",
    )
    hotel_preference: str | None = Field(
        default=None,
        description="Accommodation location/amenity preference e.g. beachside, near the beach, resort.",
    )
    strict_constraints: list[str] = Field(
        default_factory=list,
        description="Explicit hard user constraints that must not be silently downgraded.",
    )
    is_days_delta: bool = Field(
        default=False,
        description="True when user specified a relative change to days (e.g. extend by one day).",
    )
    days_delta: int | None = Field(
        default=None,
        description="Delta in days when is_days_delta=True.",
    )
    # Phase 5: Day-by-Day Scheduling, Constraints & Replacements
    dietary_preference: str | None = Field(
        default=None,
        description="Dietary preference for meal suggestions, e.g. vegetarian, vegan.",
    )
    schedule_pace: str | None = Field(
        default=None,
        description="Pacing preference: relaxed or packed.",
    )
    earliest_activity_time: str | None = Field(
        default=None,
        description="Earliest time of day for scheduled activities, e.g. '11:00 AM'.",
    )
    arrival_time: str | None = Field(
        default=None,
        description="Reported arrival/landing time on Day 1, e.g. '10:00 AM'.",
    )
    departure_time: str | None = Field(
        default=None,
        description="Reported departure time on Day N.",
    )
    special_activity_request: str | None = Field(
        default=None,
        description="Special timing-sensitive activity request, e.g. 'sunset at a beach'.",
    )
    replace_activity_target: str | None = Field(
        default=None,
        description="Name or category of venue/attraction to replace in itinerary, e.g. 'museum'.",
    )
    replace_activity_category: str | None = Field(
        default=None,
        description="Desired category for the replacement attraction, e.g. 'nature'.",
    )
    target_day_number: int | None = Field(
        default=None,
        description="Specific day number (1-indexed) targeted for modification or replacement.",
    )
    # Phase 7: Multi-expense, In-Trip Companion & Booking Lifecycle
    expenses: list[ExpenseItem] = Field(
        default_factory=list,
        description="Individual line-item expenses extracted from a compound message.",
    )
    is_in_trip_query: bool = Field(
        default=False,
        description="True if message is an informational query during an active trip.",
    )
    in_trip_query_type: str | None = Field(
        default=None,
        description="Type of in-trip query: today, next, budget, food, route, general.",
    )
    booking_target: str | None = Field(
        default=None,
        description="Component targeted for booking update: flight, hotel, transport, activity.",
    )
    booking_action: str | None = Field(
        default=None,
        description="Booking action requested: confirmed, cancelled, show, link.",
    )
    proposal_confirmed: bool | None = Field(
        default=None,
        description="User confirmation status for pending proposals (True for Yes/Confirm, False for No/Cancel).",
    )
    reversal_target_amount: Decimal | None = Field(
        default=None,
        description="Amount of previously recorded expense targeted for reversal/correction.",
    )
    reversal_reason: str | None = Field(
        default=None,
        description="Reason or description for expense reversal.",
    )

    @model_validator(mode="before")
    @classmethod
    def _sync_travel_party(cls, data: Any) -> Any:
        if isinstance(data, dict):
            tp = data.get("travel_party")
            tt = data.get("traveler_type")
            people = data.get("people")

            # Headcount constraint: solo only if people == 1; group parties require people > 1
            if isinstance(people, int):
                if people == 1 and tp in ("couple", "friends", "family", "relatives"):
                    tp = None
                    data["travel_party"] = None
                    data["traveler_type"] = None
                elif people > 1 and tp == "solo":
                    tp = None
                    data["travel_party"] = None
                    data["traveler_type"] = None

            if tp and not tt:
                data["traveler_type"] = tp
            elif tt and not tp and tt in ("solo", "couple", "friends", "family", "relatives"):
                if isinstance(people, int):
                    if people == 1 and tt in ("couple", "friends", "family", "relatives"):
                        tt = None
                    elif people > 1 and tt == "solo":
                        tt = None
                data["travel_party"] = tt
                data["traveler_type"] = tt

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
            if "expenses" in data and isinstance(data["expenses"], list):
                parsed_expenses = []
                for exp in data["expenses"]:
                    if isinstance(exp, dict):
                        amt = exp.get("amount")
                        cat = normalize_expense_category(exp.get("category", "general"))
                        desc = exp.get("description")
                        if amt is not None:
                            try:
                                amt_dec = Decimal(str(amt).replace(",", ""))
                                parsed_expenses.append(ExpenseItem(amount=amt_dec, category=cat or "general", description=desc))
                            except Exception:
                                pass
                    elif isinstance(exp, ExpenseItem):
                        parsed_expenses.append(exp)
                data["expenses"] = parsed_expenses
                if parsed_expenses and ("amount" not in data or data["amount"] is None):
                    data["amount"] = sum(e.amount for e in parsed_expenses)
                if parsed_expenses and ("expense_category" not in data or data["expense_category"] is None):
                    data["expense_category"] = parsed_expenses[0].category
        return data

    @model_validator(mode="after")
    def _enforce_travel_party_headcount(self) -> "ParsedTripIntent":
        if isinstance(self.people, int):
            if self.people == 1 and self.travel_party in ("couple", "friends", "family", "relatives"):
                object.__setattr__(self, "travel_party", None)
                object.__setattr__(self, "traveler_type", None)
            elif self.people > 1 and self.travel_party == "solo":
                object.__setattr__(self, "travel_party", None)
                object.__setattr__(self, "traveler_type", None)
        return self

    @property
    def needs_destination_discovery(self) -> bool:
        """True if destination is not provided and needs reverse-budget discovery."""
        if self.requested_destination and self.requested_destination.strip():
            return False
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
        Interests and strict constraints are merged (union, preserving order).
        The `action` is taken from the update so routing always reflects the latest intent.
        """
        merged_interests = list(self.interests)
        for i in update.interests:
            if i not in merged_interests:
                merged_interests.append(i)

        merged_constraints = list(self.strict_constraints)
        for c in update.strict_constraints:
            if c not in merged_constraints:
                merged_constraints.append(c)

        party = update.travel_party if update.travel_party is not None else self.travel_party
        ttype = update.traveler_type if update.traveler_type is not None else self.traveler_type
        if party and not ttype:
            ttype = party
        elif ttype and not party and ttype in ("solo", "couple", "friends", "family", "relatives"):
            party = ttype

        req_dest = update.requested_destination if update.requested_destination is not None else self.requested_destination

        # Determine effective days, start_date, and end_date
        eff_start = update.start_date if update.start_date is not None else self.start_date
        eff_days = update.days if update.days is not None else self.days
        if update.is_days_delta and update.days_delta is not None:
            eff_days = max(1, (self.days or 1) + update.days_delta)
        eff_end = update.end_date if update.end_date is not None else self.end_date

        # If days changed and update did NOT provide an explicit end_date:
        if (update.days is not None or update.is_days_delta) and update.end_date is None and eff_start and eff_days:
            try:
                from datetime import datetime as dt_cls, timedelta as td_cls
                s_d = dt_cls.strptime(eff_start, "%Y-%m-%d").date()
                eff_end = (s_d + td_cls(days=max(0, eff_days - 1))).strftime("%Y-%m-%d")
            except Exception:
                pass
        # If end_date changed and update did NOT provide explicit days:
        elif update.end_date is not None and update.days is None and eff_start:
            try:
                from datetime import datetime as dt_cls
                s_d = dt_cls.strptime(eff_start, "%Y-%m-%d").date()
                e_d = dt_cls.strptime(update.end_date, "%Y-%m-%d").date()
                eff_days = max(1, (e_d - s_d).days + 1)
            except Exception:
                pass

        eff_budget = update.budget if update.budget is not None else self.budget
        if update.is_delta and update.budget_delta is not None and self.budget is not None:
            eff_budget = self.budget + update.budget_delta

        return ParsedTripIntent(
            action=update.action,
            budget=eff_budget,
            currency=update.currency if update.currency != "INR" or self.currency == "INR" else self.currency,
            people=update.people if update.people is not None else self.people,
            days=eff_days,
            origin=update.origin if update.origin is not None else self.origin,
            destination=update.destination if update.destination is not None else self.destination,
            requested_destination=req_dest,
            interests=merged_interests,
            travel_party=party,
            traveler_type=ttype,
            transport_mode=update.transport_mode if update.transport_mode is not None else self.transport_mode,
            transport_class=update.transport_class if update.transport_class is not None else self.transport_class,
            hotel_tier=update.hotel_tier if update.hotel_tier is not None else self.hotel_tier,
            hotel_preference=update.hotel_preference if update.hotel_preference is not None else self.hotel_preference,
            strict_constraints=merged_constraints,
            booking_confirmed=update.booking_confirmed or self.booking_confirmed,
            rescue_detail=update.rescue_detail,
            amount=update.amount if update.amount is not None else self.amount,
            expense_category=update.expense_category if update.expense_category is not None else self.expense_category,
            day_number=update.day_number if update.day_number is not None else self.day_number,
            day_completed=update.day_completed or self.day_completed,
            start_date=eff_start,
            end_date=eff_end,
            date_is_explicit=update.date_is_explicit or self.date_is_explicit,
            date_is_ambiguous=update.date_is_ambiguous if update.date_is_ambiguous else self.date_is_ambiguous,
            date_ambiguous_phrase=update.date_ambiguous_phrase if update.date_ambiguous_phrase else self.date_ambiguous_phrase,
            date_confirmed=update.date_confirmed or self.date_confirmed,
            event_id=update.event_id if update.event_id is not None else self.event_id,
            dietary_preference=update.dietary_preference or self.dietary_preference,
            schedule_pace=update.schedule_pace or self.schedule_pace,
            earliest_activity_time=update.earliest_activity_time or self.earliest_activity_time,
            arrival_time=update.arrival_time or self.arrival_time,
            departure_time=update.departure_time or self.departure_time,
            special_activity_request=update.special_activity_request or self.special_activity_request,
            replace_activity_target=update.replace_activity_target or self.replace_activity_target,
            replace_activity_category=update.replace_activity_category or self.replace_activity_category,
            target_day_number=update.target_day_number if update.target_day_number is not None else self.target_day_number,
            expenses=update.expenses if update.expenses else self.expenses,
            is_in_trip_query=update.is_in_trip_query or self.is_in_trip_query,
            in_trip_query_type=update.in_trip_query_type or self.in_trip_query_type,
            booking_target=update.booking_target or self.booking_target,
            booking_action=update.booking_action or self.booking_action,
            proposal_confirmed=update.proposal_confirmed if update.proposal_confirmed is not None else self.proposal_confirmed,
            reversal_target_amount=update.reversal_target_amount if update.reversal_target_amount is not None else self.reversal_target_amount,
            reversal_reason=update.reversal_reason or self.reversal_reason,
        )

    def apply_change_action(self, update: "ParsedTripIntent") -> "ParsedTripIntent":
        """Apply a CHANGE_* or MODIFY_TRIP action: merge updated fields into existing context.

        Recalculates dependent values (e.g. inclusive dates) and preserves all unmentioned fields.
        """
        action = update.action
        if action == TripAction.CHANGE_BUDGET:
            if update.is_delta and update.budget_delta is not None and self.budget is not None:
                new_budget = self.budget + update.budget_delta
            else:
                new_budget = update.budget if update.budget is not None else self.budget
            return self.model_copy(update={
                "budget": new_budget,
                "action": action,
                "is_delta": False,
                "budget_delta": None,
                "travel_party": update.travel_party if update.travel_party is not None else self.travel_party,
                "traveler_type": update.traveler_type if update.traveler_type is not None else self.traveler_type,
                "interests": update.interests if update.interests else self.interests,
            })
        if action == TripAction.CHANGE_DAYS:
            new_days = update.days if update.days is not None else self.days
            eff_end = self.end_date
            if update.end_date is not None:
                eff_end = update.end_date
            elif self.start_date and new_days:
                try:
                    from datetime import datetime as dt_cls, timedelta as td_cls
                    s_d = dt_cls.strptime(self.start_date, "%Y-%m-%d").date()
                    eff_end = (s_d + td_cls(days=max(0, new_days - 1))).strftime("%Y-%m-%d")
                except Exception:
                    pass
            return self.model_copy(update={
                "days": new_days,
                "end_date": eff_end,
                "action": action,
                "travel_party": update.travel_party if update.travel_party is not None else self.travel_party,
                "traveler_type": update.traveler_type if update.traveler_type is not None else self.traveler_type,
                "interests": update.interests if update.interests else self.interests,
            })
        if action == TripAction.CHANGE_PEOPLE:
            new_people = update.people if update.people is not None else self.people
            upd: dict[str, Any] = {
                "people": new_people,
                "action": action,
                "interests": update.interests if update.interests else self.interests,
            }
            if update.travel_party is not None:
                upd["travel_party"] = update.travel_party
                upd["traveler_type"] = update.traveler_type or update.travel_party
            elif self.travel_party == "couple" and new_people is not None and new_people != 2:
                upd["travel_party"] = None
                upd["traveler_type"] = None
            elif self.travel_party == "solo" and new_people is not None and new_people > 1:
                upd["travel_party"] = None
                upd["traveler_type"] = None
            else:
                upd["travel_party"] = self.travel_party
                upd["traveler_type"] = self.traveler_type
            return self.model_copy(update=upd)
        if action == TripAction.CHANGE_DESTINATION:
            return self.model_copy(update={
                "destination": update.destination,
                "requested_destination": update.destination,
                "action": action,
            })
        if action == TripAction.CHANGE_TRANSPORT:
            upd: dict[str, Any] = {"action": action}
            if update.transport_mode is not None:
                upd["transport_mode"] = update.transport_mode
            if update.transport_class is not None:
                upd["transport_class"] = update.transport_class
            return self.model_copy(update=upd)
        if action == TripAction.FIND_ALTERNATIVE:
            return self.model_copy(update={
                "destination": None,
                "requested_destination": None,
                "action": action,
            })
        # MODIFY_TRIP and general fallback: full context-preserving merge
        return self.merge_with(update)

    def to_trip_intent_record(self, trip_id: UUID, raw_prompt: str | None = None) -> TripIntent:
        """Convert validated intent to a database TripIntent entity."""
        if not self.is_plannable:
            raise ValueError(f"Cannot persist incomplete intent to database. Missing: {self.missing_fields}")

        all_interests = list(self.interests)
        if self.hotel_preference and self.hotel_preference not in all_interests:
            all_interests.append(self.hotel_preference)
        if self.hotel_tier and f"{self.hotel_tier} hotel" not in all_interests and self.hotel_tier not in all_interests:
            all_interests.append(f"{self.hotel_tier} hotel")
        for sc in self.strict_constraints:
            tag = f"strict_constraint:{sc}"
            if tag not in all_interests:
                all_interests.append(tag)

        return TripIntent(
            id=uuid4(),
            trip_id=trip_id,
            budget=self.budget,  # type: ignore[arg-type]
            currency=self.currency,
            people=self.people,  # type: ignore[arg-type]
            days=self.days,  # type: ignore[arg-type]
            origin=self.origin,
            destination=self.destination,
            interests=all_interests,
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
    distance_km: float | None = Field(
        default=None,
        description="Ride or transit distance in kilometers if mentioned by user (e.g. 12 km).",
    )
    raw_message: str = Field(
        default="",
        description="Original user message.",
    )


# Alias for compatibility with project specifications
ParsedIntent = ParsedTripIntent

