"""AI Intent Service orchestrating prompt generation, OpenRouter calls, and Pydantic validation.

Architecture:
  ONE AI call per user message returns both `action` and extracted fields.
  Python (ActionRouter in orchestrator) performs all state loading, merging, and routing.
  The AI is NOT responsible for deciding which database state to use.
"""

import json
import logging
import re
from decimal import Decimal
from typing import Any
from pydantic import ValidationError

from budlance.ai.client import GeminiClient, OpenRouterClient
from budlance.ai.exceptions import OpenRouterValidationError
from budlance.config import get_settings
from budlance.ai.prompts import (
    RESCUE_INTENT_SYSTEM_PROMPT,
    TRIP_INTENT_CONTEXT_PROMPT,
    TRIP_INTENT_SYSTEM_PROMPT,
)
from budlance.ai.schemas import ParsedRescueIntent, ParsedTripIntent, TravelParty, TripAction
# =============================================================================
# FALLBACK_PARSER_KNOWLEDGE
# Used ONLY for entity recognition / extraction in the offline heuristic parser
# when the live LLM is unreachable or disabled (e.g. testing).
# This static city list:
# 1. Never supplies provider/travel pricing.
# 2. Never acts as destination recommendations or destination discovery.
# 3. Merely allows regex entity extraction to recognize user-typed city names.
# =============================================================================
FALLBACK_PARSER_CITIES: tuple[str, ...] = (
    "kerala", "goa", "ooty", "manali", "munnar", "jaipur", "udaipur",
    "coorg", "pondicherry", "ladakh", "chennai", "bangalore", "mumbai", "delhi",
    "kodaikanal", "shimla", "darjeeling", "hyderabad", "kolkata", "pune",
    "agra", "varanasi", "mysore", "mysuru", "rishikesh", "mcleod",
)

logger = logging.getLogger(__name__)


class AIIntentService:
    """Service responsible for converting natural language into validated structured intent.

    Each user message results in EXACTLY ONE AI call that returns both the action
    classification and any extracted fields.  Python then does all routing.
    """

    def __init__(
        self,
        client: Any | None = None,
        use_mock: bool = False,
    ) -> None:
        settings = get_settings()
        if client is not None:
            self.client = client
        elif settings.has_openrouter_credentials:
            self.client = OpenRouterClient()
        elif settings.has_gemini_credentials:
            self.client = GeminiClient()
        else:
            self.client = OpenRouterClient()

        self.use_mock = use_mock or not self.client.has_credentials

    # =========================================================================
    # Primary public interface — ONE call per message
    # =========================================================================

    async def parse_trip_intent(self, user_prompt: str) -> ParsedTripIntent:
        """Parse a fresh natural-language travel request into a validated ParsedTripIntent.

        Returns the intent including the `action` field so the orchestrator can route
        without a second AI call.
        """
        if not user_prompt or not user_prompt.strip():
            return ParsedTripIntent()

        if self.use_mock:
            return self._mock_parse_trip_intent(user_prompt)

        messages = [
            {"role": "system", "content": TRIP_INTENT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt.strip()},
        ]

        try:
            raw_data = await self.client.chat_completion(messages)
        except Exception as exc:
            logger.warning(
                "Live AI intent parsing encountered an error (%s: %s). Falling back to heuristic parser.",
                type(exc).__name__,
                exc,
            )
            return self._mock_parse_trip_intent(user_prompt)

        try:
            return ParsedTripIntent.model_validate(raw_data)
        except ValidationError as exc:
            logger.warning("Pydantic validation failed for travel intent output: %s", exc)
            raise OpenRouterValidationError(f"Invalid structured output format: {exc}") from exc

    async def parse_trip_intent_with_context(
        self,
        user_prompt: str,
        existing_intent: ParsedTripIntent,
    ) -> ParsedTripIntent:
        """Parse a follow-up or correction message given an existing active intent.

        Returns a ParsedTripIntent where `action` indicates what the user wants to do.
        The orchestrator (ActionRouter) applies the action-specific merge/routing rule.

        This is ONE AI call — not two.
        """
        if not user_prompt or not user_prompt.strip():
            return existing_intent

        if self.use_mock:
            return self._mock_parse_with_context(user_prompt, existing_intent)

        # Build context JSON summary for the model
        context_json = json.dumps(
            {
                "budget": float(existing_intent.budget) if existing_intent.budget is not None else None,
                "currency": existing_intent.currency,
                "people": existing_intent.people,
                "days": existing_intent.days,
                "origin": existing_intent.origin,
                "destination": existing_intent.destination,
                "interests": existing_intent.interests,
                "travel_party": existing_intent.travel_party,
                "traveler_type": existing_intent.traveler_type,
                "transport_mode": existing_intent.transport_mode,
                "transport_class": existing_intent.transport_class,
                "booking_confirmed": existing_intent.booking_confirmed,
            },
            ensure_ascii=False,
        )

        messages = [
            {"role": "system", "content": TRIP_INTENT_CONTEXT_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Existing context: {context_json}\n\n"
                    f"New message: {user_prompt.strip()}"
                ),
            },
        ]

        try:
            raw_data = await self.client.chat_completion(messages)
        except Exception as exc:
            logger.warning(
                "Live AI context-aware intent parsing encountered an error (%s: %s). "
                "Falling back to heuristic merge.",
                type(exc).__name__,
                exc,
            )
            return self._mock_parse_with_context(user_prompt, existing_intent)

        try:
            updated = ParsedTripIntent.model_validate(raw_data)
            return updated
        except ValidationError as exc:
            logger.warning(
                "Pydantic validation failed for context-aware intent output: %s. "
                "Falling back to heuristic merge.",
                exc,
            )
            return self._mock_parse_with_context(user_prompt, existing_intent)

    async def parse_rescue_intent(self, user_message: str) -> ParsedRescueIntent:
        """Classify and extract in-trip rescue messages."""
        if not user_message or not user_message.strip():
            return ParsedRescueIntent(
                rescue_type="unknown",
                user_issue="Empty message",
                raw_message=user_message,
            )

        if self.use_mock:
            return self._mock_parse_rescue_intent(user_message)

        messages = [
            {"role": "system", "content": RESCUE_INTENT_SYSTEM_PROMPT},
            {"role": "user", "content": user_message.strip()},
        ]

        try:
            raw_data = await self.client.chat_completion(messages)
        except Exception as exc:
            logger.warning(
                "Live AI rescue intent parsing encountered an error (%s: %s). Falling back to heuristic parser.",
                type(exc).__name__,
                exc,
            )
            return self._mock_parse_rescue_intent(user_message)

        try:
            raw_data["raw_message"] = user_message
            return ParsedRescueIntent.model_validate(raw_data)
        except ValidationError as exc:
            logger.warning("Pydantic validation failed for rescue output: %s", exc)
            raise OpenRouterValidationError(f"Invalid rescue output format: {exc}") from exc

    # =========================================================================
    # Offline Mock Heuristics for Tests & Local Execution
    # =========================================================================

    def _mock_parse_trip_intent(self, text: str) -> ParsedTripIntent:
        """Rule-based offline heuristic parser for tests and fallback execution.

        Also classifies the action so the full mock path is consistent with the
        live path (both return ParsedTripIntent with an action field).
        """
        clean = text.lower()

        # ---- Action classification ----
        action = self._mock_classify_action(clean)

        # For UNRECOGNIZED or RESCUE — return minimal intent immediately
        if action == TripAction.UNRECOGNIZED:
            return ParsedTripIntent(action=action)
        if action == TripAction.RESCUE:
            return ParsedTripIntent(action=action, rescue_detail=text.strip())
        if action == TripAction.TRIP_COMPLETE:
            return ParsedTripIntent(
                action=action,
                completion_reason=self._extract_completion_reason(text),
            )
        if action == TripAction.LOG_EXPENSE:
            return ParsedTripIntent(
                action=action,
                amount=self._extract_expense_amount(clean),
                expense_category=self._extract_expense_category(clean),
                day_number=self._extract_day_number(clean),
                day_completed=self._extract_day_completed(clean),
            )

        # For CHANGE_* — only extract the relevant field
        if action == TripAction.CHANGE_BUDGET:
            budget = self._extract_budget(clean)
            return ParsedTripIntent(action=action, budget=budget)
        if action == TripAction.CHANGE_DAYS:
            days = self._extract_days(clean)
            return ParsedTripIntent(action=action, days=days)
        if action == TripAction.CHANGE_PEOPLE:
            people = self._extract_people(clean)
            travel_party = self._extract_travel_party(clean)
            return ParsedTripIntent(action=action, people=people, travel_party=travel_party)
        if action == TripAction.CHANGE_DESTINATION:
            destination = self._extract_single_destination(clean)
            return ParsedTripIntent(action=action, destination=destination)
        if action == TripAction.CHANGE_TRANSPORT:
            mode, cls = self._extract_transport(clean)
            return ParsedTripIntent(action=action, transport_mode=mode, transport_class=cls)
        if action == TripAction.CONFIRM_BOOKING:
            return ParsedTripIntent(action=action, booking_confirmed=True)

        # For FIND_ALTERNATIVE — no planning fields extracted
        if action == TripAction.FIND_ALTERNATIVE:
            return ParsedTripIntent(action=action)

        # NEW_TRIP — full extraction
        return self._mock_full_extract(text, action=action)

    def _mock_parse_with_context(
        self,
        text: str,
        existing_intent: ParsedTripIntent,
    ) -> ParsedTripIntent:
        """Heuristic context-aware parse returning a ParsedTripIntent with action.

        The orchestrator's ActionRouter applies the action-specific merge/routing.
        """
        clean = text.lower()
        action = self._mock_classify_action(clean)

        if action == TripAction.UNRECOGNIZED:
            return ParsedTripIntent(action=action)
        if action == TripAction.RESCUE:
            return ParsedTripIntent(action=action, rescue_detail=text.strip())
        if action == TripAction.TRIP_COMPLETE:
            return ParsedTripIntent(
                action=action,
                completion_reason=self._extract_completion_reason(text),
            )
        if action == TripAction.LOG_EXPENSE:
            return ParsedTripIntent(
                action=action,
                amount=self._extract_expense_amount(clean),
                expense_category=self._extract_expense_category(clean),
                day_number=self._extract_day_number(clean),
                day_completed=self._extract_day_completed(clean),
            )
        if action == TripAction.FIND_ALTERNATIVE:
            return ParsedTripIntent(action=action)
        if action == TripAction.CHANGE_BUDGET:
            return ParsedTripIntent(action=action, budget=self._extract_budget(clean))
        if action == TripAction.CHANGE_DAYS:
            return ParsedTripIntent(action=action, days=self._extract_days(clean))
        if action == TripAction.CHANGE_PEOPLE:
            return ParsedTripIntent(
                action=action,
                people=self._extract_people(clean),
                travel_party=self._extract_travel_party(clean),
            )
        if action == TripAction.CHANGE_DESTINATION:
            return ParsedTripIntent(action=action, destination=self._extract_single_destination(clean))
        if action == TripAction.CHANGE_TRANSPORT:
            mode, cls = self._extract_transport(clean)
            return ParsedTripIntent(action=action, transport_mode=mode, transport_class=cls)
        if action == TripAction.CONFIRM_BOOKING:
            return ParsedTripIntent(action=action, booking_confirmed=True)

        # NEW_TRIP or a simple follow-up: extract fully and merge with context
        extracted = self._mock_full_extract(text, action=action)
        return existing_intent.merge_with(extracted)

    # =========================================================================
    # Action Classification Heuristic
    # =========================================================================

    def _mock_classify_action(self, clean: str) -> TripAction:
        """Classify the action for a given lowercased input string.

        Checks FIND_ALTERNATIVE / RESCUE / CHANGE_* / UNRECOGNIZED before NEW_TRIP.
        """
        # UNRECOGNIZED — catch very short, vague messages first
        vague_patterns = [
            r"^\s*ok\s*$", r"^\s*hmm+\s*$", r"^\s*not sure\s*$",
            r"^\s*what\??\s*$", r"^\s*haha\s*$", r"^\s*lol\s*$",
            r"^\s*yes\s*$", r"^\s*no\s*$", r"^\s*maybe\s*$", r"^\s*sure\s*$",
            r"^\s*thanks\s*$", r"^\s*thank you\s*$", r"^\s*that's it\s*$",
            r"^\s*thats it\s*$", r"^\s*okay\s*$",
        ]
        for p in vague_patterns:
            if re.fullmatch(p, clean.strip()):
                return TripAction.UNRECOGNIZED

        # TRIP_COMPLETE — user explicitly indicates that the trip has finished
        trip_complete_patterns = [
            r"\b(?:the\s+)?trip\s+(?:is\s+)?(?:over|done|completed|finished)\b",
            r"\btrip['’]s\s+(?:done|over|finished)\b",
            r"\bclose\s+(?:the\s+)?trip\b",
            r"\bend\s+(?:the\s+)?trip\b",
            r"\b(?:we\s+are|we['’]re|we\s+re|am)\s+back\s+home\b",
            r"\bback\s+home\b",
            r"\btrip\s+complete\b",
            r"\btrip\s+mudinjadhu\b",
        ]
        if any(re.search(p, clean) for p in trip_complete_patterns):
            return TripAction.TRIP_COMPLETE

        # RESCUE — in-trip distress signals
        rescue_keywords = [
            "raining", "rain", "storm", "flood", "closed", "shut", "landslide",
            "bad weather", "auto driver", "cab driver", "taxi", "asking", "charging",
            "fare", "demanding", "price dispute", "overcharging", "wants", "injured",
            "sick", "hospital", "accident",
        ]
        strong_rescue_phrases = [
            "auto driver", "cab driver", "taxi driver", "driver is asking", "driver charging",
            "price dispute", "overcharging", "asking too much", "closed today", "shut today",
        ]
        if any(re.search(rf"\b{re.escape(kw)}\b", clean) for kw in rescue_keywords):
            # Strong rescue signals always indicate RESCUE
            if any(re.search(rf"\b{re.escape(srp)}\b", clean) for srp in strong_rescue_phrases):
                return TripAction.RESCUE
            # Only classify as RESCUE if there's no clear trip planning or transport intent
            planning_indicators = [
                "budget", "days", "people", "person", "peru",
                "irundhu", "poganum", "origin", "destination",
                "train", "flight", "sleeper", "1ac", "2ac", "3ac",
            ]
            if not any(re.search(rf"\b{re.escape(pi)}\b", clean) for pi in planning_indicators):
                return TripAction.RESCUE

        # LOG_EXPENSE — user reporting actual money spent during active trip
        expense_verbs = [
            r"\bspent\b", r"\bspend\b", r"\bcost(?:\s+me)?\b", r"\bused\b",
            r"\bpaid\b", r"\bselavu\b", r"\bexpense\b", r"\bexpenses\b",
        ]
        has_expense_verb = any(re.search(v, clean) for v in expense_verbs)
        has_day_completion_with_expense = bool(
            re.search(r"\bday\s*\d+\s*(?:is\s*)?(?:done|over|finished|completed|mudinjadhu)\b", clean)
            and re.search(r"(?:₹|rs\.?|inr)?\s*\d+", clean)
        )
        if has_expense_verb or has_day_completion_with_expense:
            if not ("plan a trip" in clean or "trip to" in clean or "want to visit" in clean):
                return TripAction.LOG_EXPENSE

        # FIND_ALTERNATIVE — wants a different place but doesn't name one
        find_alt_phrases = [
            "recommend another", "recommend other", "other place", "somewhere else",
            "another place", "another destination", "different place", "different destination",
            "find another", "suggest another", "suggest other", "any other place",
            "any other destination", "what else", "anything else", "cheaper option",
            "cheaper place", "affordable option", "too expensive",
            "within my budget", "within this budget", "what other",
        ]
        if any(phrase in clean for phrase in find_alt_phrases):
            return TripAction.FIND_ALTERNATIVE

        # CHANGE_DESTINATION — explicitly names a replacement destination
        dest_change_phrases = [
            "change destination to", "change to", "change it to",
            "actually go to", "actually want", "actually make it",
            "make it to ", "switch to", "go to instead",
            "instead go to", "different destination",
        ]
        if any(phrase in clean for phrase in dest_change_phrases):
            # Make sure there's an actual named place after the phrase
            if self._extract_single_destination(clean):
                return TripAction.CHANGE_DESTINATION

        # CHANGE_BUDGET — only changing the budget
        budget_change_phrases = [
            "budget is now", "budget is", "increase budget", "change budget",
            "make budget", "budget changed", "now budget",
        ]
        # Pure budget-only messages like "20000" or "budget 20000" without other trip fields
        if any(phrase in clean for phrase in budget_change_phrases):
            return TripAction.CHANGE_BUDGET

        # CHANGE_DAYS — only changing days
        days_change_phrases = [
            "make it ", "change to ", "change it to ", "actually ", "instead ",
        ]
        days_only_signals = re.search(
            r"^(?:make it|change to|actually|change days? to|change it to)\s+\d+\s*(?:days?|nights?|din)?\s*[.!]*$",
            clean.strip(),
        )
        if days_only_signals:
            return TripAction.CHANGE_DAYS

        # Check for pure days-only follow-up: "4 days", "3 din", etc.
        pure_days = re.fullmatch(r"\d+\s*(?:days?|nights?|din)\s*[.!]*", clean.strip())
        if pure_days:
            return TripAction.CHANGE_DAYS

        # CHANGE_PEOPLE — only changing headcount
        # Patterns: "make it 3 people", "only me now", "just me", "now 3 of us"
        people_solo_phrases = [
            "only me now", "only me", "just me now", "just me",
            "solo now", "traveling alone now", "alone now",
        ]
        if any(phrase in clean for phrase in people_solo_phrases):
            return TripAction.CHANGE_PEOPLE
        people_only_signals = re.search(
            r"^(?:make it|change to|now|change people to)\s+(?:\d+|solo|alone)\s*(?:people?|person|travelers?|of us|peru)?\s*(?:now)?\s*[.!]*$",
            clean.strip(),
        )
        if people_only_signals:
            return TripAction.CHANGE_PEOPLE
        # "3 of us now" / "just 2 of us"
        pure_people = re.search(
            r"^(?:just\s+|only\s+)?(\d+)\s*(?:of us|people|persons?|travelers?|peru)\s*(?:now)?\s*[.!]*$",
            clean.strip(),
        )
        if pure_people:
            return TripAction.CHANGE_PEOPLE

        # CONFIRM_BOOKING — user explicitly reports completing transport booking externally
        booking_confirm_phrases = [
            "booked", "i booked it", "i have booked", "booking done",
            "ticket booked", "tickets booked", "confirmed booking",
            "train booked", "flight booked", "we booked", "already booked", "done booking",
        ]
        if any(re.search(rf"\b{re.escape(phrase)}\b", clean) for phrase in booking_confirm_phrases):
            return TripAction.CONFIRM_BOOKING

        # CHANGE_TRANSPORT — changing mode or class
        transport_change_signals = [
            "try ", "prefer ", "change to ", "switch to ", "go by ", "instead",
            "let's go by", "prefer train", "prefer flight",
        ]
        is_pure_transport = bool(
            re.fullmatch(
                r"(?:(?:try|prefer|let's\s+go\s+by|we\s+prefer|i\s+prefer)\s+)?(?:\d\s*ac|1st\s*ac|2nd\s*ac|3rd\s*ac|first\s*ac|second\s*ac|third\s*ac|sleeper|sl|train|flight|flight\s+economy|economy|business(?:\s+class)?)(?:[,\s]+(?:\d\s*ac|1st\s*ac|2nd\s*ac|3rd\s*ac|first\s*ac|second\s*ac|third\s*ac|sleeper|sl|train|flight|flight\s+economy|economy|business(?:\s+class)?))?(?:\s+instead)?\s*[.!]*",
                clean.strip(),
            )
        )
        if is_pure_transport or any(phrase in clean for phrase in transport_change_signals):
            mode, cls = self._extract_transport(clean)
            if mode is not None or cls is not None:
                return TripAction.CHANGE_TRANSPORT

        # NEW_TRIP — start over / explicit reset
        new_trip_phrases = [
            "start over", "new trip", "forget that", "reset", "cancel that",
            "ignore that", "start fresh",
        ]
        if any(phrase in clean for phrase in new_trip_phrases):
            return TripAction.NEW_TRIP

        # Default to NEW_TRIP for substantive messages
        return TripAction.NEW_TRIP

    # =========================================================================
    # Atomic field extractors (reused across action paths)
    # =========================================================================

    def _extract_expense_amount(self, clean: str) -> Decimal | None:
        """Extract user stated expense amount as Decimal."""
        # 1. k notation e.g. 2.5k, 2k
        k_match = re.search(r"(\d+(?:\.\d+)?)\s*k\b", clean)
        if k_match:
            return Decimal(str(float(k_match.group(1)) * 1000))
        # 2. After spent / used / cost / paid / selavu:
        m = re.search(
            r"(?:spent|spend|cost(?:\s+me)?|used|paid|selavu|expense)\s+(?:about\s+|around\s+)?(?:₹|rs\.?|inr|rupees?)?\s*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+(?:\.[0-9]+)?)",
            clean,
        )
        if m:
            return Decimal(m.group(1).replace(",", ""))
        # 3. Currency symbols: ₹2200, rs 3000
        m2 = re.search(
            r"(?:₹|rs\.?|inr|rupees?)\s*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+(?:\.[0-9]+)?)",
            clean,
        )
        if m2:
            return Decimal(m2.group(1).replace(",", ""))
        # 4. Trailing currency: 2200 rs, 2200 inr
        m3 = re.search(
            r"([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+(?:\.[0-9]+)?)\s*(?:rs|inr|rupees|rupayee)",
            clean,
        )
        if m3:
            return Decimal(m3.group(1).replace(",", ""))
        # 5. Fallback: first number not preceded by "day"
        numbers = re.findall(r"(?<!day\s)(?<!day)\b([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{2,7}(?:\.[0-9]+)?)\b", clean)
        if numbers:
            return Decimal(numbers[0].replace(",", ""))
        return None

    def _extract_day_number(self, clean: str) -> int | None:
        """Extract explicit 1-indexed day number if present."""
        m = re.search(r"\bday\s*(\d+)\b", clean)
        if m:
            return int(m.group(1))
        return None

    def _extract_day_completed(self, clean: str) -> bool:
        """Detect if the user explicitly indicated completion of a day."""
        completed_patterns = [
            r"\bday\s*\d+\s*(?:is\s*)?(?:done|over|completed|finished|mudinjadhu)\b",
            r"\b(?:completed|finished|done with)\s+day\s*\d+\b",
            r"\bday\s*\d+\s+done\b",
            r"\btoday\s*(?:is\s*)?(?:done|over|completed|finished)\b",
        ]
        return any(re.search(p, clean) for p in completed_patterns)

    def _extract_expense_category(self, clean: str) -> str | None:
        """Extract and normalize category from expense message."""
        if any(w in clean for w in ("activity", "activities", "entry", "ticket", "tickets", "sightseeing", "tour", "museum", "safari", "scuba", "watersports")):
            return "activities"
        if any(w in clean for w in ("food", "lunch", "dinner", "breakfast", "meal", "meals", "cafe", "restaurant", "snack", "snacks", "water", "tea", "coffee")):
            return "food"
        if any(w in clean for w in ("auto", "autos", "cab", "cabs", "taxi", "taxis", "bus", "train", "flight", "metro", "transport", "transit", "rickshaw", "fuel")):
            return "transport"
        if any(w in clean for w in ("hotel", "stay", "room", "hostel", "resort", "accommodation")):
            return "stay"
        return None

    def _extract_completion_reason(self, text: str) -> str | None:
        """Extract optional user-provided completion reason from TRIP_COMPLETE text."""
        clean_lower = text.strip().lower()
        if "trip completed normally" in clean_lower or "completed normally" in clean_lower:
            return "Trip completed normally"

        m = re.search(
            r"(?:trip\s+(?:is\s+)?(?:over|done|completed|finished)|trip['’]s\s+(?:done|over|finished)|close\s+(?:the\s+)?trip|end\s+(?:the\s+)?trip|trip\s+complete)\s*(?:[-:]|\bbecause\b|\bdue to\b|\bas\b|\breason\s*[:=]?)\s*(.+)",
            text,
            re.IGNORECASE,
        )
        if m:
            val = m.group(1).strip()
            if val and val.lower() not in ("now", "today", "please", "."):
                return val[:50]

        m2 = re.search(r"(?:trip\s+completed|trip\s+complete)\s+([a-zA-Z0-9\s]{3,})", text, re.IGNORECASE)
        if m2:
            val = m2.group(1).strip()
            if val and val.lower() not in ("now", "today", "please", "."):
                return val[:50]
        return None

    def _extract_budget(self, clean: str) -> Decimal | None:
        k_match = re.search(r"(\d+(?:\.\d+)?)\s*k\b", clean)
        budget_match = re.search(
            r"(?:₹|rs\.?|inr|rupees?|ரூபாய்)?[\s]*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{4,7})",
            clean,
        )
        if k_match:
            return Decimal(str(float(k_match.group(1)) * 1000))
        if budget_match:
            return Decimal(budget_match.group(1).replace(",", ""))
        return None

    def _extract_days(self, clean: str) -> int | None:
        m = re.search(r"(\d+)\s*(?:days?|nights?|din|naatkal|நாட்கள்)", clean)
        if m:
            return int(m.group(1))
        # bare number when context strongly implies days
        m2 = re.search(r"(?:make it|change to|actually|for)\s+(\d+)\s*(?:days?|nights?|din)?", clean)
        if m2:
            return int(m2.group(1))
        return None

    def _extract_people(self, clean: str) -> int | None:
        if re.search(r"\bsolo\b|\balone\b|\bjust me\b|\bonly me\b", clean):
            return 1
        if re.search(r"\bcouple\b", clean):
            return 2
        if re.search(
            r"\b(?:me and my (?:wife|husband|partner|girlfriend|boyfriend)|with my (?:wife|husband|partner|girlfriend|boyfriend))\b",
            clean,
        ):
            return 2
        # Tanglish "X peru"
        m = re.search(r"(\d+)\s*peru\b", clean)
        if m:
            return int(m.group(1))
        m = re.search(r"(\d+)\s*(?:people|persons?|adults?|travelers?|friends?|log|பேர்)", clean)
        if m:
            return int(m.group(1))
        # "just X", "only X"
        m = re.search(r"(?:just|only)\s+(\d+)", clean)
        if m:
            return int(m.group(1))
        return None

    def _extract_travel_party(self, clean: str) -> TravelParty | None:
        """Extract travel party from explicit user phrasing.

        Strict rule: people count alone (e.g. 2 people) NEVER implies travel_party.
        """
        # relatives
        relatives_patterns = [
            r"\brelatives?\b",
            r"\bwith relatives?\b",
            r"\bfamily\s+gathering\b",
        ]
        if any(re.search(p, clean) for p in relatives_patterns):
            return "relatives"

        # family
        family_patterns = [
            r"\bfamily\b",
            r"\bwith family\b",
            r"\bwith kids\b",
            r"\bkids?\b",
            r"\bparents?\b",
            r"\bwith parents\b",
            r"\bchildren\b",
            r"\bfamily\s+trip\b",
        ]
        if any(re.search(p, clean) for p in family_patterns):
            return "family"

        # couple
        couple_patterns = [
            r"\bcouple\b",
            r"\bhoneymoon\b",
            r"\bwife\b",
            r"\bhusband\b",
            r"\bpartner\b",
            r"\bgirlfriend\b",
            r"\bboyfriend\b",
        ]
        if any(re.search(p, clean) for p in couple_patterns):
            return "couple"

        # friends
        friends_patterns = [
            r"\bfriends?\b",
            r"\bwith friends?\b",
            r"\bgang\b",
            r"\bbuddies\b",
            r"\bboys\s+trip\b",
            r"\bgirls\s+trip\b",
        ]
        if any(re.search(p, clean) for p in friends_patterns):
            return "friends"

        # solo
        solo_patterns = [
            r"\bsolo\b",
            r"\balone\b",
            r"\bjust\s+me\b",
            r"\bonly\s+me\b",
        ]
        if any(re.search(p, clean) for p in solo_patterns):
            return "solo"

        return None

    def _extract_single_destination(self, clean: str) -> str | None:
        """Extract a destination named after a change phrase."""
        # Prefer explicit "change to X" patterns first
        m = re.search(
            r"(?:change(?:\s+destination)?\s+to|actually\s+(?:go\s+to|want(?:\s+to\s+go)?\s+to)|"
            r"make\s+it\s+to|switch\s+to|go\s+to\s+instead|instead\s+go\s+to)\s+([a-z][a-z]+)",
            clean,
        )
        if m:
            candidate = m.group(1).strip().title()
            if candidate.lower() not in {"a", "the", "my", "there", "here", "from", "to", "none", "flight", "train", "bus"}:
                return candidate

        # Scan known places via FALLBACK_PARSER_CITIES
        for place in FALLBACK_PARSER_CITIES:
            if re.search(rf"\b{place}\b", clean):
                return place.title()
        return None

    def _extract_transport(self, clean: str) -> tuple[str | None, str | None]:
        """Extract transport mode and class from user message."""
        mode: str | None = None
        cls: str | None = None

        # Mode detection (English and Tanglish)
        if re.search(r"\b(?:train|rail|railway)\b", clean) or "train la" in clean or "trainle" in clean:
            mode = "train"
        elif re.search(r"\b(?:flight|plane|air|aeroplane|airline)\b", clean) or "flight la" in clean or "flightle" in clean:
            mode = "flight"

        # Class detection
        # 1AC / 1st AC / first AC / 1A
        if re.search(r"\b(?:1\s*ac|1st\s*ac|first\s*ac|1\s*a|first\s*class\s*ac)\b", clean):
            cls = "1ac"
            if mode is None:
                mode = "train"
        # 2AC / 2nd AC / second AC / 2A
        elif re.search(r"\b(?:2\s*ac|2nd\s*ac|second\s*ac|2\s*a|second\s*class\s*ac)\b", clean):
            cls = "2ac"
            if "train" in clean:
                mode = "train"
        # 3AC / 3rd AC / third AC / 3A
        elif re.search(r"\b(?:3\s*ac|3rd\s*ac|third\s*ac|3\s*a|third\s*class\s*ac)\b", clean):
            cls = "3ac"
            if "train" in clean:
                mode = "train"
        # Sleeper / SL
        elif re.search(r"\b(?:sleeper|sl)\b", clean):
            cls = "sleeper"
            if "train" in clean:
                mode = "train"
        # Premium economy
        elif re.search(r"\b(?:premium\s*economy|pe)\b", clean):
            cls = "premium_economy"
            if "flight" in clean or "air" in clean:
                mode = "flight"
        # Business
        elif re.search(r"\b(?:business(?:\s*class)?)\b", clean):
            cls = "business"
            if "flight" in clean or "air" in clean:
                mode = "flight"
        # Economy
        elif re.search(r"\b(?:economy(?:\s*class)?|coach)\b", clean):
            cls = "economy"
            if "flight" in clean or "air" in clean:
                mode = "flight"
        # First class (flight vs train)
        elif re.search(r"\b(?:first\s*class)\b", clean):
            if mode == "train" or "train" in clean:
                cls = "1ac"
                mode = "train"
            else:
                cls = "first"
                if "flight" in clean or "air" in clean:
                    mode = "flight"

        return mode, cls

    # =========================================================================
    # Full extraction (used for NEW_TRIP action)
    # =========================================================================

    def _mock_full_extract(self, text: str, action: TripAction = TripAction.NEW_TRIP) -> ParsedTripIntent:
        """Full field extraction matching the original heuristic parser behaviour."""
        clean = text.lower()

        # 1. Budget
        budget = self._extract_budget(clean)
        currency = "INR"
        if "$" in clean or "usd" in clean:
            currency = "USD"
        elif "€" in clean or "eur" in clean:
            currency = "EUR"

        # 2. Duration
        days = self._extract_days(clean)

        # 3. People
        people = self._extract_people(clean)

        # 4. Origin — detect before X-to-Y so explicit presence takes precedence
        origin: str | None = None
        _STOPWORDS = {
            "a", "the", "my", "our", "trip", "travel", "going", "want",
            "planning", "plan", "budget", "days", "day", "nights", "night",
            "interested", "excited", "looking",
        }

        # 4a. Explicit presence patterns
        presence_match = re.search(
            r"(?:"
            r"currently\s+i\s+am\s+in|currently\s+i'm\s+in|currently\s+in|currently\s+at|"
            r"i\s+am\s+in|i'm\s+in|iam\s+in|i\s+am\s+at|i'm\s+at"
            r")\s+([a-z][a-z]+)",
            clean,
        )
        if presence_match:
            raw_origin = presence_match.group(1).strip()
            if raw_origin not in _STOPWORDS:
                origin = raw_origin.title()

        # 4b. Tanglish origin
        if not origin:
            tanglish_origin = re.search(
                r"([\w]+)\s+la\s+(?:irun(?:dhu|du|d|ku)|iruk(?:en|iru|kiren?))|"
                r"([\w]+)\s+irundhu\b",
                clean,
            )
            if tanglish_origin:
                raw_origin = (tanglish_origin.group(1) or tanglish_origin.group(2) or "").strip()
                if raw_origin and raw_origin not in _STOPWORDS:
                    origin = raw_origin.title()

        # 4c. English explicit origin
        if not origin:
            explicit_from = re.search(
                r"(?:from|starting from|leaving|departing from)\s+([a-z][a-z]+)",
                clean,
            )
            if explicit_from:
                raw_origin = explicit_from.group(1).strip()
                if raw_origin not in _STOPWORDS:
                    origin = raw_origin.title()

        # 5. Destination — X-to-Y first, then goto pattern, then known-place scan
        destination: str | None = None

        # 5a. "X to Y" pattern (use finditer to scan beyond infinitive constructs like 'want to go')
        for to_match in re.finditer(r"\b([a-z][a-z]+)\s+to\s+([a-z][a-z]+)\b", clean):
            src = to_match.group(1).strip().title()
            dst = to_match.group(2).strip().title()
            infinitive_src = {
                "Want", "Need", "Plan", "Planning", "Like", "Love", "Hope", "Wish",
                "Going", "Trying", "Ready", "Intend",
            }
            infinitive_dst = {
                "Visit", "Go", "Travel", "See", "Explore", "Stay", "Head", "Drive",
                "Fly", "Vacation", "Trip", "Do", "Spend", "Reach", "Be",
            }
            stopwords = {
                "A", "The", "My", "Our", "This", "That", "Budget", "Days", "Day",
                "Night", "Nights", "Place", "Places", "People", "Friends", "Family",
                "From", "To", "None", "Flight", "Flights", "Train", "Trains", "Bus", "Buses",
            }
            if (
                src not in infinitive_src
                and dst not in infinitive_dst
                and dst not in stopwords
            ):
                destination = dst
                if src not in stopwords and not origin:
                    origin = src
                break

        # 5b. "going to X" / "want to go X" / "poganum"
        if not destination:
            goto_match = re.search(
                r"(?:going to|want to go|want to visit|interested to go|"
                r"to visit|to go to|visit|poganum)\s+(?!from\b)([a-z][a-z]+)",
                clean,
            )
            if goto_match:
                dst = goto_match.group(1).strip().title()
                stopwords2 = {
                    "A", "The", "My", "Our", "This", "That",
                    "Hill", "Beach", "There", "Here",
                    "From", "To", "None", "Flight", "Flights", "Train", "Trains", "Bus", "Buses",
                }
                if dst not in stopwords2:
                    destination = dst

        # 5c. Known-place fallback scan using FALLBACK_PARSER_CITIES
        if not destination:
            interest_words = {"beach", "hill", "station", "mountain", "temple", "food"}
            for place in FALLBACK_PARSER_CITIES:
                if re.search(rf"\b{place}\b", clean):
                    if origin and place.lower() == origin.lower():
                        continue
                    if place in interest_words:
                        continue
                    destination = place.title()
                    break

        # 6. Interests
        interests = []
        interest_keywords = [
            "local food", "beach", "beaches", "food", "nature", "mountains", "temple", "culture",
            "relaxation", "calm", "theme park", "theme_park", "hill station",
            "famous places", "famous place", "landmarks", "sightseeing", "adventure",
        ]
        for interest_kw in interest_keywords:
            if interest_kw in clean:
                normalized = interest_kw.replace("_", " ")
                if normalized == "famous place":
                    normalized = "famous places"
                if normalized not in interests:
                    interests.append(normalized)

        # 7. Travel party
        travel_party = self._extract_travel_party(clean)

        # 8. Transport preference
        transport_mode, transport_class = self._extract_transport(clean)
        booking_confirmed = action == TripAction.CONFIRM_BOOKING

        return ParsedTripIntent(
            action=action,
            budget=budget,
            currency=currency,
            people=people,
            days=days,
            origin=origin,
            destination=destination,
            interests=interests,
            travel_party=travel_party,
            traveler_type=travel_party,
            transport_mode=transport_mode,
            transport_class=transport_class,
            booking_confirmed=booking_confirmed,
        )

    def _mock_parse_rescue_intent(self, text: str) -> ParsedRescueIntent:
        """Rule-based offline heuristic parser for rescue messages."""
        clean = text.lower()

        # Price dispute patterns
        price_match = re.search(r"(?:₹|rs\.?|inr)?[\s]*([\d]{2,5})", clean)
        if any(w in clean for w in ["auto", "cab", "taxi", "driver", "asking", "charging", "fare", "demanding", "dispute"]):
            reported_price = Decimal(price_match.group(1)) if price_match else None
            service = "auto" if "auto" in clean else ("cab" if "cab" in clean or "taxi" in clean else "transport")
            return ParsedRescueIntent(
                rescue_type="price_dispute",
                user_issue="Fare dispute reported by user",
                location_or_context=None,
                reported_price=reported_price,
                service_type=service,
                raw_message=text,
            )

        # Weather / Closure patterns
        if any(w in clean for w in ["rain", "raining", "storm", "closed", "shut", "landslide", "bad weather"]):
            return ParsedRescueIntent(
                rescue_type="weather_closure",
                user_issue="Weather disruption or closed attraction",
                location_or_context=None,
                reported_price=None,
                service_type=None,
                raw_message=text,
            )

        # Unknown / general message
        return ParsedRescueIntent(
            rescue_type="unknown",
            user_issue="Unclassified rescue statement",
            location_or_context=None,
            reported_price=None,
            service_type=None,
            raw_message=text,
        )
