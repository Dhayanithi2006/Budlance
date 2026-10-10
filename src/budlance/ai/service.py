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
from budlance.ai.telemetry import AITelemetry
from budlance.config import get_settings
from budlance.ai.prompts import (
    RESCUE_INTENT_SYSTEM_PROMPT,
    TRIP_INTENT_CONTEXT_PROMPT,
    TRIP_INTENT_SYSTEM_PROMPT,
)
from budlance.ai.schemas import ExpenseItem, ParsedRescueIntent, ParsedTripIntent, TravelParty, TripAction
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
    "port blair", "andaman", "leh", "hosur",
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
        elif settings.has_gemini_credentials:
            self.client = GeminiClient()
        elif settings.has_openrouter_credentials:
            self.client = OpenRouterClient()
        else:
            self.client = GeminiClient()

        self.use_mock = use_mock or not self.client.has_credentials
        self.telemetry = getattr(self.client, "telemetry", None) or AITelemetry()

    async def _safe_chat_completion(
        self,
        messages: list[dict[str, str]],
        response_validator: Any | None = None,
    ) -> dict[str, Any]:
        """Safely call chat_completion passing response_validator if supported by client."""
        if hasattr(self.client, "chat_completion"):
            import inspect
            sig = inspect.signature(self.client.chat_completion)
            if "response_validator" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ):
                return await self.client.chat_completion(
                    messages,
                    response_validator=response_validator,
                )
        return await self.client.chat_completion(messages)

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
            raw_data = await self._safe_chat_completion(
                messages,
                response_validator=ParsedTripIntent.model_validate,
            )
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
            raise OpenRouterValidationError(f"Pydantic validation failed for travel intent output: {exc}") from exc

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
                "start_date": existing_intent.start_date,
                "end_date": existing_intent.end_date,
                "origin": existing_intent.origin,
                "destination": existing_intent.destination,
                "interests": existing_intent.interests,
                "hotel_tier": existing_intent.hotel_tier,
                "hotel_preference": existing_intent.hotel_preference,
                "strict_constraints": existing_intent.strict_constraints,
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
            raw_data = await self._safe_chat_completion(
                messages,
                response_validator=ParsedTripIntent.model_validate,
            )
            return ParsedTripIntent.model_validate(raw_data)
        except Exception as exc:
            logger.warning(
                "Live AI context-aware intent parsing encountered an error (%s: %s). "
                "Falling back to heuristic merge.",
                type(exc).__name__,
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

        def _rescue_validator(raw: dict[str, Any]) -> None:
            data = dict(raw)
            data["raw_message"] = user_message
            ParsedRescueIntent.model_validate(data)

        try:
            raw_data = await self._safe_chat_completion(
                messages,
                response_validator=_rescue_validator,
            )
            raw_data["raw_message"] = user_message
            return ParsedRescueIntent.model_validate(raw_data)
        except Exception as exc:
            logger.warning(
                "Live AI rescue intent parsing encountered an error (%s: %s). Falling back to heuristic parser.",
                type(exc).__name__,
                exc,
            )
            return self._mock_parse_rescue_intent(user_message)

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
                amount=self._extract_expense_amount(clean),
                expense_category=self._extract_expense_category(clean),
                day_number=self._extract_day_number(clean),
                day_completed=self._extract_day_completed(clean),
            )
        if action == TripAction.LOG_EXPENSE:
            multi_expenses = self._extract_multi_expenses(clean)
            primary_amt = multi_expenses[0].amount if multi_expenses else self._extract_expense_amount(clean)
            primary_cat = multi_expenses[0].category if multi_expenses else self._extract_expense_category(clean)
            return ParsedTripIntent(
                action=action,
                amount=primary_amt,
                expense_category=primary_cat,
                expenses=multi_expenses,
                day_number=self._extract_day_number(clean),
                day_completed=self._extract_day_completed(clean),
            )
        if action == TripAction.IN_TRIP_QUERY:
            return ParsedTripIntent(
                action=action,
                is_in_trip_query=True,
                in_trip_query_type=self._extract_in_trip_query_type(clean),
            )
        if action in (TripAction.CONFIRM_BOOKING, TripAction.MANAGE_BOOKING):
            b_target, b_action = self._extract_booking_details(clean)
            return ParsedTripIntent(
                action=action,
                booking_confirmed=(b_action == "confirmed"),
                booking_target=b_target,
                booking_action=b_action,
            )

        # For CHANGE_* — only extract the relevant field
        if action == TripAction.CHANGE_BUDGET:
            is_delta, delta_amount, abs_budget = self._extract_budget_delta(clean)
            if is_delta:
                return ParsedTripIntent(action=action, is_delta=True, budget_delta=delta_amount)
            return ParsedTripIntent(action=action, budget=abs_budget)
        if action == TripAction.CHANGE_DAYS:
            days = self._extract_days(clean)
            start_date, end_date, date_is_explicit, date_is_ambiguous, date_ambiguous_phrase = self._extract_date_details(clean)
            if start_date and days is not None and not end_date:
                from datetime import datetime as dt, timedelta
                d_s = dt.strptime(start_date, "%Y-%m-%d").date()
                end_date = (d_s + timedelta(days=max(0, days - 1))).strftime("%Y-%m-%d")
            return ParsedTripIntent(
                action=action,
                days=days,
                start_date=start_date,
                end_date=end_date,
                date_is_explicit=date_is_explicit,
                date_is_ambiguous=date_is_ambiguous,
                date_ambiguous_phrase=date_ambiguous_phrase,
                date_confirmed=date_is_explicit,
            )
        if action == TripAction.CHANGE_PEOPLE:
            people = self._extract_people(clean)
            travel_party = self._extract_travel_party(clean)
            return ParsedTripIntent(action=action, people=people, travel_party=travel_party)
        if action == TripAction.CHANGE_DESTINATION:
            destination = self._extract_single_destination(clean)
            return ParsedTripIntent(action=action, destination=destination, requested_destination=destination)
        if action == TripAction.CHANGE_TRANSPORT:
            mode, cls = self._extract_transport(clean)
            return ParsedTripIntent(action=action, transport_mode=mode, transport_class=cls)

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
                amount=self._extract_expense_amount(clean),
                expense_category=self._extract_expense_category(clean),
                day_number=self._extract_day_number(clean),
                day_completed=self._extract_day_completed(clean),
            )
        if action == TripAction.LOG_EXPENSE:
            multi_expenses = self._extract_multi_expenses(clean)
            primary_amt = multi_expenses[0].amount if multi_expenses else self._extract_expense_amount(clean)
            primary_cat = multi_expenses[0].category if multi_expenses else self._extract_expense_category(clean)
            return ParsedTripIntent(
                action=action,
                amount=primary_amt,
                expense_category=primary_cat,
                expenses=multi_expenses,
                day_number=self._extract_day_number(clean),
                day_completed=self._extract_day_completed(clean),
            )
        if action == TripAction.IN_TRIP_QUERY:
            return ParsedTripIntent(
                action=action,
                is_in_trip_query=True,
                in_trip_query_type=self._extract_in_trip_query_type(clean),
            )
        if action == TripAction.FIND_ALTERNATIVE:
            return ParsedTripIntent(action=action)
        if action == TripAction.CHANGE_BUDGET:
            is_delta, delta_amount, abs_budget = self._extract_budget_delta(clean)
            if is_delta:
                return ParsedTripIntent(action=action, is_delta=True, budget_delta=delta_amount)
            return ParsedTripIntent(action=action, budget=abs_budget)
        if action == TripAction.CHANGE_DAYS:
            days = self._extract_days(clean)
            start_date, end_date, date_is_explicit, date_is_ambiguous, date_ambiguous_phrase = self._extract_date_details(clean)
            if not start_date and existing_intent.start_date:
                start_date = existing_intent.start_date
                date_is_explicit = existing_intent.date_is_explicit
                date_confirmed = existing_intent.date_confirmed
            else:
                date_confirmed = date_is_explicit
            eff_days = days if days is not None else existing_intent.days
            if start_date and eff_days is not None and not end_date:
                from datetime import datetime as dt, timedelta
                d_s = dt.strptime(start_date, "%Y-%m-%d").date()
                end_date = (d_s + timedelta(days=max(0, eff_days - 1))).strftime("%Y-%m-%d")
            return ParsedTripIntent(
                action=action,
                days=days,
                start_date=start_date,
                end_date=end_date,
                date_is_explicit=date_is_explicit,
                date_is_ambiguous=date_is_ambiguous,
                date_ambiguous_phrase=date_ambiguous_phrase,
                date_confirmed=date_confirmed,
            )
        if action == TripAction.CHANGE_PEOPLE:
            return ParsedTripIntent(
                action=action,
                people=self._extract_people(clean),
                travel_party=self._extract_travel_party(clean),
            )
        if action == TripAction.CHANGE_DESTINATION:
            dest = self._extract_single_destination(clean)
            return ParsedTripIntent(action=action, destination=dest, requested_destination=dest)
        if action == TripAction.CHANGE_TRANSPORT:
            mode, cls = self._extract_transport(clean)
            return ParsedTripIntent(action=action, transport_mode=mode, transport_class=cls)
        if action in (TripAction.CONFIRM_BOOKING, TripAction.MANAGE_BOOKING):
            b_target, b_action = self._extract_booking_details(clean)
            return ParsedTripIntent(
                action=action,
                booking_confirmed=(b_action == "confirmed"),
                booking_target=b_target,
                booking_action=b_action,
            )

        if action == TripAction.MODIFY_TRIP:
            extracted = self._mock_full_extract(text, action=action)
            is_delta, delta = self._extract_days_delta(clean)
            if is_delta and delta is not None:
                extracted.days = max(1, (existing_intent.days or 1) + delta)
                extracted.is_days_delta = True
                extracted.days_delta = delta
            return existing_intent.merge_with(extracted)

        # NEW_TRIP: discard stale context completely only if it is an explicit reset / new trip command
        explicit_reset_signals = [
            "start over", "start fresh", "reset", "cancel that", "forget that", "ignore that",
            "new trip", "plan a trip", "plan another trip", "another trip",
            "let me plan a new trip", "let me plan new trip", "plan a new trip",
        ]
        is_explicit_reset = any(sig in clean for sig in explicit_reset_signals)
        if action == TripAction.NEW_TRIP and is_explicit_reset:
            return self._mock_full_extract(text, action=action)

        # Simple follow-up: extract fully and merge with context
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
            r"\bfinishing\s+(?:the\s+)?trip\b",
            r"\bmark\s+(?:the\s+)?trip\s+as\s+completed\b",
        ]
        if any(re.search(p, clean) for p in trip_complete_patterns):
            return TripAction.TRIP_COMPLETE

        # Disambiguation guards for LOG_EXPENSE:
        # 1. "spend time" / "spend 7 days" / "spend more time in nature" is spending time, NEVER LOG_EXPENSE
        is_spending_time = bool(
            re.search(r"\bspend\b.*?\b(?:time|days?|nights?|hours?|weeks?|months?|nature|beach|mountains?|sightseeing|outdoors?)\b", clean)
            or re.search(r"\bspend\s+(?:more|less|some|any|all)?\s*(?:time|in\s+nature|time\s+in\s+nature)\b", clean)
            or bool(re.search(r"\b(?:would rather|rather|prefer to|want to|like to|hope to)\s+spend\b", clean))
            or ("spend" in clean and any(w in clean for w in ("nature", "time", "beach", "sightseeing", "relax", "outdoors")))
        )
        # 2. "can spend" / "don't want to spend" is budget constraint, NEVER LOG_EXPENSE
        is_budget_constraint = bool(
            re.search(r"\b(?:can|could|will|may|might|plan to|want to|like to|hope to|would rather|rather)\s+spend\b", clean)
            or re.search(r"\b(?:don'?t|do not|cannot|can't|won't)\s+(?:want to\s+)?spend\b", clean)
        )

        # LOG_EXPENSE — user reporting actual money spent during active trip
        expense_verbs = [
            r"\bspent\b",
            r"\bcost(?:\s+me)?\b",
            r"\bused\b",
            r"\bpaid\b",
            r"\bselavu\b",
            r"\bexpenses?\b",
            r"\blog\s+(?:that\s+)?expense\b",
            r"\brecord\s+(?:that\s+)?expense\b",
            r"\b(?:log|record)\s+(?:₹|rs\.?|inr)?\s*[\d,]+",
            r"\bconfirm\s+(?:payment|expense)\b",
            r"\bconfirmed\s+payment\b",
        ]
        has_expense_verb = any(re.search(v, clean) for v in expense_verbs)
        has_day_completion_with_expense = bool(
            re.search(r"\bday\s*\d+\s*(?:is\s*)?(?:done|over|finished|completed|mudinjadhu)\b", clean)
            and re.search(r"(?:₹|rs\.?|inr)?\s*\d+", clean)
        )
        is_explicit_expense = (has_expense_verb or has_day_completion_with_expense) and not is_spending_time and not is_budget_constraint

        # Active dispute/distress phrases that override simple expense logging
        active_distress_phrases = [
            "overcharging", "demanding", "price dispute", "fare dispute",
            "driver is asking", "driver charging", "asking too much",
            "can't afford tomorrow", "cant afford tomorrow", "cannot afford tomorrow",
            "cheaper replacement", "auto fare quote", "fare quote",
            "is it fair", "is fair", "quote of", "asking whether",
        ]

        if is_explicit_expense and not any(re.search(rf"\b{re.escape(dp)}\b", clean) for dp in active_distress_phrases):
            is_trip_planning = (
                "plan a trip" in clean
                or "trip to" in clean
                or "want to visit" in clean
                or "starting from" in clean
                or "budget for" in clean
                or "solo trip" in clean
                or "trip plan" in clean
                or "destination fixed" in clean
                or "best overall option" in clean
                or "optimize" in clean
            )
            if not is_trip_planning:
                return TripAction.LOG_EXPENSE

        # RESCUE — in-trip distress signals
        rescue_keywords = [
            "raining", "rain", "storm", "flood", "closed", "shut", "landslide",
            "bad weather", "auto driver", "cab driver", "taxi driver", "asking", "charging",
            "fare dispute", "price dispute", "demanding", "overcharging", "injured",
            "sick", "hospital", "accident",
            "can't afford tomorrow", "cant afford tomorrow", "cannot afford tomorrow",
            "cheaper replacement", "auto fare quote", "fare quote", "quote",
            "cancelled", "canceled", "bus cancelled", "bus is cancelled", "bus is canceled",
            "airport bus is cancelled", "airport bus is canceled", "museum is closed",
            "crowded", "overcrowded", "too crowded", "what else nearby",
        ]
        strong_rescue_phrases = [
            "auto driver", "cab driver", "taxi driver", "driver is asking", "driver charging",
            "price dispute", "fare dispute", "overcharging", "asking too much", "closed today", "shut today",
            "can't afford tomorrow", "cant afford tomorrow", "cannot afford tomorrow",
            "cheaper replacement", "auto fare quote", "fare quote", "quote of", "is it fair", "is fair",
            "asking whether",
            "museum is closed", "museum closed", "bus is cancelled", "bus cancelled", "bus is canceled",
            "airport bus is cancelled", "airport bus is canceled", "bus got cancelled",
            "is crowded", "too crowded", "overcrowded", "what else nearby", "what else is nearby",
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

        # MANAGE_BOOKING — user inquiries or updates about specific bookings
        booking_manage_patterns = [
            r"\bshow\s+(?:me\s+)?(?:the\s+)?flight\s+i\s+planned\b",
            r"\bopen\s+(?:the\s+)?hotel\s+booking\s+link\b",
            r"\bi\s+(?:have\s+)?cancelled\s+(?:the\s+)?hotel\b",
            r"\bi\s+cancelled\s+(?:the\s+)?hotel\b",
            r"\bairline\s+(?:has\s+)?confirmed\s+my\s+cancellation\b",
            r"\bprovider\s+confirmed\s+my\s+reservation\b",
        ]
        if any(re.search(p, clean) for p in booking_manage_patterns):
            return TripAction.MANAGE_BOOKING

        # IN_TRIP_QUERY — questions asked during active travel
        in_trip_query_patterns = [
            r"\bwhat\s+(?:is|'s)?\s*(?:planned\s+for\s+today|planned\s+today|today's\s+plan|on\s+for\s+today)\b",
            r"\btoday's\s+(?:schedule|itinerary|plan)\b",
            r"\bwhat\s+(?:is|'s)\s+the\s+next\s+activity\b",
            r"\bwhat\s+(?:is|'s)\s+next\b",
            r"\bnext\s+(?:activity|place|stop)\b",
            r"\bfind\s+(?:somewhere|a\s+place)?\s*(?:good\s+to\s+eat|food|restaurant)\b",
            r"\bwhere\s+to\s+eat\b",
            r"\bgood\s+(?:place\s+to\s+eat|food|restaurant)\s+near\b",
            r"\bhow\s+much\s+(?:of\s+my\s+)?budget\s+is\s+left\b",
            r"\bhow\s+much\s+budget\s+left\b",
            r"\bcan\s+(?:i|we)\s+afford\s+(?:another|an?)\s+activity\b",
            r"\bcan\s+(?:i|we)\s+afford\b",
            r"\bshow\s+(?:me\s+)?(?:the\s+)?route\s+to\s+(?:the\s+)?next\s+place\b",
            r"\broute\s+to\s+(?:the\s+)?next\s+place\b",
            r"\bhelp\s+me\s+plan\s+(?:transport\s+to|travel\s+to\s+the\s+airport)\b",
            r"\btravell?ing\s+to\s+the\s+airport\b",
            r"\bremove\s+today's\s+final\s+activity\b",
            r"\bremove\s+final\s+activity\b",
            r"\bmovie(?:\s+night)?\b",
            r"\bshowtimes?\b",
            r"\bcinema\b",
            r"\btheatres?\b",
            r"\bmovies?\s+(?:near|playing|tonight|in\b)",
        ]
        if any(re.search(p, clean) for p in in_trip_query_patterns):
            return TripAction.IN_TRIP_QUERY

        # MODIFY_TRIP — explicit refinement, constraint updates, or multi-attribute changes
        refinement_signals = [
            "keep everything else the same", "keep everything else", "keep the rest",
            "keep those dates", "keep that budget", "keep the same",
            "don't change my choices", "do not change my choices",
            "without asking", "keep those", "keep my", "keep the return",
            "land at", "prefer vegetarian", "don't schedule", "dont schedule",
            "sunset at a beach", "beach sunset", "replace", "replace it with",
            "is closed", "don't change anything else", "dont change anything else",
            "keep my hotel and flights unchanged",
        ]
        has_refinement_phrase = any(sig in clean for sig in refinement_signals)

        field_changes = 0
        if self._extract_days(clean) is not None or self._extract_days_delta(clean)[0]:
            field_changes += 1
        if self._extract_budget(clean) is not None:
            field_changes += 1
        t_mode, t_cls = self._extract_transport(clean)
        if t_mode is not None or t_cls is not None:
            field_changes += 1
        h_tier, h_pref = self._extract_hotel_preferences(clean)
        if h_tier is not None or h_pref is not None:
            field_changes += 1

        is_explicit_new_trip_text = any(
            phrase in clean for phrase in [
                "start over", "reset", "start fresh", "cancel that", "forget that",
                "plan a trip", "plan new trip", "plan a new trip", "another trip"
            ]
        )

        if not is_explicit_new_trip_text and (
            has_refinement_phrase
            or (field_changes > 1 and any(w in clean for w in ["actually", "make it", "lower", "reduce", "cut", "change", "keep"]))
        ):
            return TripAction.MODIFY_TRIP

        # FIND_ALTERNATIVE — wants a different place but doesn't name one
        find_alt_phrases = [
            "show alternatives", "alternatives", "suggest alternatives", "show me alternatives",
            "find alternatives", "other destinations", "give me alternatives", "alternative destinations",
            "recommend another", "recommend other", "other place", "somewhere else",
            "another place", "another destination", "different place", "different destination",
            "find another", "suggest another", "suggest other", "any other place",
            "any other destination", "what else", "anything else", "cheaper option",
            "cheaper place", "affordable option", "too expensive",
            "within my budget", "within this budget", "what other",
        ]
        if (
            any(phrase in clean for phrase in find_alt_phrases)
            or clean.strip() in ("yes", "yes please", "sure", "ok", "yes find in india", "yes, find in india", "find trips in india")
        ):
            return TripAction.FIND_ALTERNATIVE

        # CHANGE_DESTINATION — explicitly changing the destination to a named city
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

        # CHANGE_BUDGET — changing the budget or budget limits
        is_delta, delta_amount, _ = self._extract_budget_delta(clean)
        if is_delta:
            return TripAction.CHANGE_BUDGET

        budget_change_phrases = [
            "budget is now", "budget is", "increase budget", "change budget",
            "make budget", "budget changed", "now budget", "work within",
            "keep the budget at", "keep budget at", "make the same trip work within",
            "don't want to spend anywhere close", "limit to", "cut budget",
            "reduce budget", "under budget", "to my budget", "to the budget", "to budget",
            "add to budget", "add to my budget", "lower my budget", "lower total budget", "lower my total budget",
        ]
        budget_change_patterns = [
            r"\b(?:reduce|lower|cut|decrease|increase|raise|change)\s+(?:the\s+|my\s+|total\s+)?budget\b",
            r"\bkeep\s+(?:the\s+)?(?:whole\s+trip\s+)?under\b",
            r"^(?:keep\s+)?(?:the\s+)?(?:whole\s+)?(?:trip\s+)?under\s+(?:₹|rs\.?|inr)?\s*\d+k?[.!]*$",
            r"\b(?:my\s+)?budget\s+(?:is|to|at)\b",
        ]
        if not is_explicit_new_trip_text and (
            any(phrase in clean for phrase in budget_change_phrases)
            or any(re.search(pat, clean) for pat in budget_change_patterns)
        ):
            return TripAction.CHANGE_BUDGET

        # CHANGE_DAYS — changing days or shifting dates
        date_shift_signals = [
            "following week", "next week", "move the trip", "shift the trip", "change dates",
        ]
        if any(phrase in clean for phrase in date_shift_signals):
            return TripAction.CHANGE_DAYS

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

        # CONFIRM_BOOKING — user explicitly reports completing transport booking externally or confirming plan
        booking_confirm_phrases = [
            "booked", "i booked it", "i have booked", "booking done",
            "ticket booked", "tickets booked", "confirmed booking",
            "train booked", "flight booked", "we booked", "already booked", "done booking",
            "confirm this trip", "confirm trip", "confirm the trip", "confirm my trip",
            "confirm the plan", "confirm this plan", "confirm plan",
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
        has_new_trip_intent = any(
            phrase in clean for phrase in ["plan a trip", "plan new trip", "plan a 3-day", "plan a 2-day", "plan a 5-day", "plan a"]
        ) or (
            not any(w in clean for w in ["actually", "keep", "lower", "reduce", "extend", "instead", "don't", "dont"])
            and self._extract_budget(clean) is not None
            and (self._extract_days(clean) is not None or self._extract_people(clean) is not None)
        )
        if not has_new_trip_intent and (is_pure_transport or any(phrase in clean for phrase in transport_change_signals)):
            mode, cls = self._extract_transport(clean)
            if mode is not None or cls is not None:
                return TripAction.CHANGE_TRANSPORT

        # NEW_TRIP — start over / explicit reset
        new_trip_phrases = [
            "start over", "new trip", "forget that", "reset", "cancel that",
            "ignore that", "start fresh", "let me plan", "plan a trip", "plan new trip",
            "plan a new trip",
        ]
        if any(phrase in clean for phrase in new_trip_phrases) or bool(re.search(r"\blet\s+me\s+plan(?:\s+a)?\s+new\s+trip\b", clean)):
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
        # 2. After spent / used / cost / paid / selavu / confirm payment:
        m = re.search(
            r"(?:spent|spend|cost(?:\s+me)?|used|paid|selavu|expense|confirm\s+payment\s+of|payment\s+of)\s+(?:about\s+|around\s+)?(?:₹|rs\.?|inr|rupees?)?\s*([0-9]{1,3}(?:,[0-9]{2,3})+|[0-9]+(?:\.[0-9]+)?)",
            clean,
        )
        if m:
            return Decimal(m.group(1).replace(",", ""))
        # 3. Currency symbols: ₹2200, rs 3000
        m2 = re.search(
            r"(?:₹|rs\.?|inr|rupees?)\s*([0-9]{1,3}(?:,[0-9]{2,3})+|[0-9]+(?:\.[0-9]+)?)",
            clean,
        )
        if m2:
            return Decimal(m2.group(1).replace(",", ""))
        # 4. Trailing currency: 2200 rs, 2200 inr
        m3 = re.search(
            r"([0-9]{1,3}(?:,[0-9]{2,3})+|[0-9]+(?:\.[0-9]+)?)\s*(?:rs|inr|rupees|rupayee)",
            clean,
        )
        if m3:
            return Decimal(m3.group(1).replace(",", ""))
        # 5. Fallback: first number not preceded by "day"
        numbers = re.findall(r"(?<!day\s)(?<!day)\b([0-9]{1,3}(?:,[0-9]{2,3})+|[0-9]{2,7}(?:\.[0-9]+)?)\b", clean)
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

    def _extract_multi_expenses(self, clean: str) -> list[ExpenseItem]:
        """Extract multiple individual expenses from natural compound messages."""
        # Split on 'and', ';', 'also', or commas not between digits
        parts = re.split(r"\band\b|[;]|\balso\b|(?<!\d),(?!\d)", clean)
        items: list[ExpenseItem] = []

        cat_keywords = {
            "food": ("food", "lunch", "dinner", "breakfast", "meal", "meals", "cafe", "restaurant", "snack", "snacks", "water", "tea", "coffee"),
            "transport": ("metro", "cab", "cabs", "taxi", "taxis", "auto", "autos", "bus", "train", "flight", "transit", "transport", "rickshaw", "fuel", "tip"),
            "activities": ("ticket", "tickets", "entry", "museum", "activity", "activities", "sightseeing", "tour", "safari", "scuba"),
            "stay": ("hotel", "stay", "room", "hostel", "resort", "lodging"),
        }

        for part in parts:
            p = part.strip()
            if not p:
                continue
            amt_match = re.search(
                r"(?:₹|rs\.?|inr|rupees?)?\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)",
                p,
            )
            if not amt_match:
                continue
            try:
                amt = Decimal(amt_match.group(1).replace(",", ""))
                if amt <= Decimal("0"):
                    continue
            except Exception:
                continue

            # Skip if amount represents a day number ("day 2") or people ("for 2")
            if re.search(rf"\bday\s*{re.escape(amt_match.group(1))}\b", p) or re.search(rf"\bfor\s*{re.escape(amt_match.group(1))}\s*(?:people|persons?|two)\b", p):
                continue

            detected_cat = "general"
            for cat, kws in cat_keywords.items():
                if any(kw in p for kw in kws):
                    detected_cat = cat
                    break

            desc = None
            for cat, kws in cat_keywords.items():
                for kw in kws:
                    if kw in p:
                        desc = kw.capitalize()
                        break
                if desc:
                    break
            if not desc:
                desc = detected_cat.capitalize() + " expense"

            items.append(ExpenseItem(amount=amt, category=detected_cat, description=desc))

        return items

    def _extract_in_trip_query_type(self, clean: str) -> str:
        """Classify specific type of in-trip companion query."""
        if any(w in clean for w in ("today", "schedule", "planned for today", "planned today")):
            return "today"
        if any(w in clean for w in ("next activity", "next place", "what's next", "what is next", "next stop", "where to go next")):
            return "next"
        if any(w in clean for w in ("budget", "afford", "left", "remaining")):
            return "budget"
        if any(w in clean for w in ("eat", "food", "restaurant", "lunch place", "dinner place", "cafe", "somewhere good to eat")):
            return "food"
        if any(w in clean for w in ("route", "how to reach", "directions")):
            return "route"
        if any(w in clean for w in ("airport", "flight")):
            return "transport_transit"
        if any(w in clean for w in ("movie", "showtime", "cinema", "theatre", "theater", "film")):
            return "movie"
        return "general"

    def _extract_booking_details(self, clean: str) -> tuple[str | None, str | None]:
        """Extract targeted component and action for booking management."""
        target = None
        if any(w in clean for w in ("flight", "airline", "plane", "return flight")):
            target = "flight"
        elif any(w in clean for w in ("hotel", "stay", "room", "resort")):
            target = "hotel"
        elif any(w in clean for w in ("train", "bus", "cab", "transport")):
            target = "transport"

        action = "confirmed"
        if "cancel" in clean:
            action = "cancelled"
        elif any(w in clean for w in ("show", "open", "link", "view")):
            action = "show"
        return target, action

    def _extract_proposal_confirmation(self, clean: str) -> bool | None:
        """Detect user decision for a pending proposal (Yes/Confirm vs No/Cancel)."""
        c = clean.strip().lower().rstrip(".!?,")
        if c in ("yes", "confirm", "go ahead", "accept", "apply", "sure", "ok", "okay", "proceed", "sounds good", "do it", "i will go", "i'll go", "ill go", "let's go", "lets go"):
            return True
        if c in ("no", "cancel", "don't", "dont", "keep original", "don't change", "reject", "leave it", "never mind"):
            return False
        if any(w in c for w in ("confirm the change", "apply the change", "yes replace", "yes do it", "yes, apply", "yes apply", "i will go", "i'll go", "ill go")) or c.startswith("yes"):
            return True
        if any(w in c for w in ("keep original", "keep my original", "don't change", "dont change", "cancel proposal", "no keep", "no, keep")) or c.startswith("no"):
            return False
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

    def _extract_budget_delta(
        self, clean: str
    ) -> tuple[bool, Decimal | None, Decimal | None]:
        """Detect incremental (delta) vs absolute budget phrasing.

        Returns:
            (is_delta, delta_amount, absolute_budget)
            - is_delta=True when user said 'add/increase/top-up by X'
            - delta_amount is the additive increment when is_delta=True
            - absolute_budget is the new total when is_delta=False
        """
        # Delta patterns: "add 5000", "increase by 5000", "top up by 5k", "add more 3000"
        delta_patterns = [
            r"(?:add|adding|increase\s+by|raise\s+by|top\s+up\s+by|bump\s+up\s+by|raise\s+the\s+budget\s+by|add\s+more)\s+"
            r"(?:₹|rs\.?|inr|rupees?)?\s*"
            r"([0-9]{1,3}(?:,[0-9]{2,3})+|\d+(?:\.\d+)?)\s*(lakhs?|lac|lacs?|l\b|crores?|cr\b|k\b)?",
        ]
        for pat in delta_patterns:
            m = re.search(pat, clean, re.IGNORECASE)
            if m:
                val = float(m.group(1).replace(",", ""))
                unit = (m.group(2) or "").lower()
                if unit in ("lakh", "lakhs", "lac", "lacs", "l"):
                    delta = Decimal(str(int(val * 100000)))
                elif unit in ("crore", "crores", "cr"):
                    delta = Decimal(str(int(val * 10000000)))
                elif unit == "k":
                    delta = Decimal(str(int(val * 1000)))
                else:
                    delta = Decimal(str(int(val)))
                return True, delta, None
        # Absolute budget
        return False, None, self._extract_budget(clean)

    def _extract_budget(self, clean: str) -> Decimal | None:
        # Target budget priority: e.g. "within ₹1.5L", "work within 1.5L", "keep budget at 1.5L", "lower my total budget to ₹25,000", "under 25k"
        target_m = re.search(
            r"(?:within|under|limit\s+(?:it\s+)?to|budget\s+(?:is|to|at)?|keep\s+(?:the\s+)?budget\s+at|make\s+(?:it\s+)?(?:work\s+)?within|lower\s+(?:my\s+)?(?:total\s+)?budget\s+to|reduce\s+(?:my\s+)?(?:total\s+)?budget\s+to|cut\s+(?:my\s+)?(?:total\s+)?budget\s+to)\s*(?:₹|rs\.?|inr)?\s*([0-9]{1,3}(?:,[0-9]{2,3})+|\d+(?:\.\d+)?)\s*(lakhs?|lac|lacs?|l\b|crores?|cr\b|k\b)?",
            clean,
            re.IGNORECASE,
        )
        if target_m:
            raw_num = target_m.group(1).replace(",", "")
            val = float(raw_num)
            unit = (target_m.group(2) or "").lower()
            if unit in ("lakh", "lakhs", "lac", "lacs", "l"):
                return Decimal(str(int(val * 100000)))
            elif unit in ("crore", "crores", "cr"):
                return Decimal(str(int(val * 10000000)))
            elif unit == "k":
                return Decimal(str(int(val * 1000)))
            elif val >= 500:
                return Decimal(str(int(val)))

        # 1. Lakhs notation e.g. 5 lakh, 5.5 lakhs, 5 lac, 5L, 1.5L
        lakh_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:lakhs?|lac|lacs?|l\b)", clean, re.IGNORECASE)
        if lakh_match:
            return Decimal(str(int(float(lakh_match.group(1)) * 100000)))
        # 2. Crores notation e.g. 1 crore, 1 cr
        cr_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:crores?|cr\b)", clean, re.IGNORECASE)
        if cr_match:
            return Decimal(str(int(float(cr_match.group(1)) * 10000000)))
        # 3. k notation e.g. 50k
        k_match = re.search(r"(\d+(?:\.\d+)?)\s*k\b", clean, re.IGNORECASE)
        if k_match:
            return Decimal(str(int(float(k_match.group(1)) * 1000)))
        # 4. Currency or budget keyword before number (including Indian or Western comma format)
        curr_match = re.search(
            r"(?:(?:₹|rs\.?|inr|rupees?|ரூபாய்|budget(?:\s*(?:is|of|:))?)\s*)([0-9]{1,3}(?:,[0-9]{2,3})+|[0-9]{4,9})",
            clean,
            re.IGNORECASE,
        )
        if curr_match:
            return Decimal(curr_match.group(1).replace(",", ""))
        # 5. Trailing currency e.g. 50000 rs, 5,00,000 inr
        trail_match = re.search(
            r"([0-9]{1,3}(?:,[0-9]{2,3})+|[0-9]{4,9})\s*(?:rs|inr|rupees|rupayee)",
            clean,
            re.IGNORECASE,
        )
        if trail_match:
            return Decimal(trail_match.group(1).replace(",", ""))
        # 6. Fallback standalone comma-separated number or 4-9 digit number
        budget_match = re.search(
            r"\b([0-9]{1,3}(?:,[0-9]{2,3})+|[0-9]{4,9})\b",
            clean,
        )
        if budget_match:
            return Decimal(budget_match.group(1).replace(",", ""))
        return None

    def _extract_hotel_preferences(self, clean: str) -> tuple[str | None, str | None]:
        """Extract hotel tier and location/amenity preference (e.g. beachside, near beach)."""
        tier: str | None = None
        pref: str | None = None

        # Hotel tier
        if re.search(r"\b(?:5[- ]?star|5\s*star|five[- ]?star)\b", clean):
            tier = "5-star"
        elif re.search(r"\b(?:4[- ]?star|4\s*star|four[- ]?star)\b", clean):
            tier = "4-star"
        elif re.search(r"\b(?:3[- ]?star|3\s*star|three[- ]?star)\b", clean):
            tier = "3-star"
        elif re.search(r"\b(?:luxury|resort)\b", clean):
            tier = "luxury"
        elif re.search(r"\b(?:budget\s+(?:hotel|stay|accommodation|lodging|room)|hostel|dorm)\b", clean):
            tier = "budget"

        # Hotel location / proximity / amenity preference
        if re.search(r"\b(?:near\s+(?:the\s+)?beach|beachside|beachfront|beach\s+proximity|by\s+the\s+beach|near\s+beach)\b", clean):
            pref = "near the beach"
        elif re.search(r"\b(?:city\s+center|downtown|central)\b", clean):
            pref = "city center"
        elif re.search(r"\b(?:near\s+(?:the\s+)?airport|airport)\b", clean):
            pref = "near airport"
        elif re.search(r"\b(?:near\s+(?:the\s+)?station|railway\s+station)\b", clean):
            pref = "near station"

        return tier, pref

    def _extract_strict_constraints(self, clean: str) -> list[str]:
        """Extract explicit user constraints that must not be silently compromised."""
        strict: list[str] = []
        is_strict_tone = bool(
            re.search(r"\b(?:absolutely\s+want|must\s+have|strictly|only|non[- ]negotiable|don['’]t\s+change\s+my\s+choices|do\s+not\s+change\s+my\s+choices|without\s+asking)\b", clean)
        )
        if is_strict_tone or "flight" in clean or "4-star" in clean or "4 star" in clean:
            if re.search(r"\b(?:flight|flights|by\s+air)\b", clean) and is_strict_tone:
                strict.append("flight")
            if re.search(r"\b(?:train|trains)\b", clean) and is_strict_tone:
                strict.append("train")
            if re.search(r"\b(?:4[- ]?star|4\s*star|5[- ]?star|5\s*star|luxury)\b", clean) and is_strict_tone:
                strict.append("luxury_hotel")
        return strict

    def _extract_days_delta(self, clean: str) -> tuple[bool, int | None]:
        """Detect relative duration changes like 'extend by one day', 'add 2 days'."""
        word_map = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
        word_re = "|".join(word_map.keys())

        # Extend / add
        m_ext = re.search(rf"\b(?:extend\s+by|add)\s+(\d+|{word_re})\s*(?:more\s+)?(?:days?|nights?|din)\b", clean)
        if m_ext:
            v_str = m_ext.group(1)
            delta = int(v_str) if v_str.isdigit() else word_map.get(v_str, 1)
            return True, delta

        # Reduce / cut
        m_red = re.search(rf"\b(?:reduce\s+by|cut\s+by|shorten\s+by)\s+(\d+|{word_re})\s*(?:days?|nights?|din)\b", clean)
        if m_red:
            v_str = m_red.group(1)
            delta = int(v_str) if v_str.isdigit() else word_map.get(v_str, 1)
            return True, -delta

        return False, None

    def _extract_days(self, clean: str) -> int | None:
        # Check range pattern: e.g. "7 to 10 days", "7-10 days", "7 or 8 days"
        range_m = re.search(r"(\d+)\s*(?:to|-|or)\s*(\d+)\s*(?:days?|nights?|din|naatkal|நாட்கள்)", clean)
        if range_m:
            # Conservative duration selection rule: choose lower bound (min) to guarantee budget feasibility
            return min(int(range_m.group(1)), int(range_m.group(2)))
        m = re.search(r"(\d+)\s*[-]?\s*(?:days?|nights?|din|naatkal|நாட்கள்)", clean)
        if m:
            return int(m.group(1))
        # Word numbers: one, two, three, four, five, six, seven, eight, nine, ten
        word_map = {
            "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        }
        word_re = "|".join(word_map.keys())
        m_w = re.search(rf"\b({word_re})\s*[-]?\s*(?:days?|nights?|din|naatkal|நாட்கள்)\b", clean)
        if m_w:
            return word_map[m_w.group(1)]
        # bare number when context strongly implies days
        m2 = re.search(r"(?:make it|change to|actually)\s+(\d+)(?:\s*(?:days?|nights?|din))?(?!\s*(?:people|person|peru|adults|travelers|members|kids|children))\b", clean)
        if m2 and m2.group(1):
            cand = int(m2.group(1))
            if cand <= 60 and not ("budget" in clean and cand > 30):
                return cand
        m2_for = re.search(r"\bfor\s+(\d+)\s*(?:days?|nights?|din)\b", clean)
        if m2_for:
            cand = int(m2_for.group(1))
            if cand <= 60:
                return cand
        m2_w = re.search(rf"(?:make it|change to|actually)\s+({word_re})(?:\s*(?:days?|nights?|din))?(?!\s*(?:people|person|peru|adults|travelers|members|kids|children))\b", clean)
        if m2_w and m2_w.group(1):
            return word_map[m2_w.group(1)]
        m2_w_for = re.search(rf"\bfor\s+({word_re})\s*(?:days?|nights?|din)\b", clean)
        if m2_w_for:
            return word_map[m2_w_for.group(1)]
        return None

    def _extract_date_details(self, clean: str) -> tuple[str | None, str | None, bool, bool, str | None]:
        """Extract date details including explicit start/end dates, relative dates, and ambiguous date phrases.

        Returns: (start_date, end_date, date_is_explicit, date_is_ambiguous, date_ambiguous_phrase)
        """
        import datetime

        ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        today_ist = datetime.datetime.now(ist).date()

        # 1. Check for ambiguous relative date phrases
        ambiguous_patterns = [
            (r"\bnext\s+weekend\b", "next weekend"),
            (r"\bthis\s+weekend\b", "this weekend"),
            (r"\bcoming\s+weekend\b", "coming weekend"),
            (r"\bsometime\s+next\s+month\b", "sometime next month"),
            (r"\bnext\s+month\b", "next month"),
            (r"\bsometime\s+in\s+([a-z]+)\b", "sometime in {0}"),
            (r"\baround\s+diwali\b", "around Diwali"),
            (r"\baround\s+pongal\b", "around Pongal"),
            (r"\baround\s+christmas\b", "around Christmas"),
            (r"\baround\s+new\s*year\b", "around New Year"),
            (r"\bsometime\s+soon\b", "sometime soon"),
            (r"\bin\s+summer\b", "in summer"),
            (r"\bin\s+winter\b", "in winter"),
            (r"\bin\s+monsoon\b", "in monsoon"),
        ]
        for pattern, label in ambiguous_patterns:
            m_amb = re.search(pattern, clean)
            if m_amb:
                phrase = m_amb.group(0)
                return None, None, False, True, phrase

        month_map = {
            "jan": 1, "january": 1,
            "feb": 2, "february": 2,
            "mar": 3, "march": 3,
            "apr": 4, "april": 4,
            "may": 5, "jun": 6, "june": 6,
            "jul": 7, "july": 7,
            "aug": 8, "august": 8,
            "sep": 9, "sept": 9, "september": 9,
            "oct": 10, "october": 10,
            "nov": 11, "november": 11,
            "dec": 12, "december": 12,
        }
        month_pattern = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"

        def _to_iso(day: int, month_str: str, year: int) -> str | None:
            m_num = month_map.get(month_str.lower()[:3])
            if not m_num:
                return None
            try:
                return datetime.date(year, m_num, day).strftime("%Y-%m-%d")
            except ValueError:
                return None

        start_date: str | None = None
        end_date: str | None = None

        # 2. ISO format: YYYY-MM-DD
        iso_matches = re.findall(r"\b(\d{4})-(\d{2})-(\d{2})\b", clean)
        if len(iso_matches) >= 2:
            start_date = f"{iso_matches[0][0]}-{iso_matches[0][1]}-{iso_matches[0][2]}"
            end_date = f"{iso_matches[1][0]}-{iso_matches[1][1]}-{iso_matches[1][2]}"
            return start_date, end_date, True, False, None
        elif len(iso_matches) == 1:
            start_date = f"{iso_matches[0][0]}-{iso_matches[0][1]}-{iso_matches[0][2]}"

        # 3a. Shared-month date interval: "1–5 November 2026", "1-5 Nov 2026", "1 to 5 November 2026"
        shared_interval_re = rf"(\d{{1,2}})\s*(?:–|-|to)\s*(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_pattern})\s+(\d{{4}})"
        m_shared = re.search(shared_interval_re, clean)
        if m_shared:
            d1, d2, m_str, y = m_shared.groups()
            s_iso = _to_iso(int(d1), m_str, int(y))
            e_iso = _to_iso(int(d2), m_str, int(y))
            if s_iso and e_iso:
                return s_iso, e_iso, True, False, None

        # 3b. Separate month date intervals: "1 october 2026 to 5 october 2026"
        interval_re = rf"(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_pattern})\s+(\d{{4}})\s*(?:to|until|through|-)\s*(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_pattern})\s+(\d{{4}})"
        m_interval = re.search(interval_re, clean)
        if m_interval:
            d1, m1, y1, d2, m2, y2 = m_interval.groups()
            s_iso = _to_iso(int(d1), m1, int(y1))
            e_iso = _to_iso(int(d2), m2, int(y2))
            if s_iso and e_iso:
                return s_iso, e_iso, True, False, None

        # 4. Explicit departure / start date: "start on 1 october 2026", "starting 1 october 2026", "start 1 oct 2026"
        start_re = rf"(?:start(?:ing)?(?:\s+on)?|from|depart(?:ing)?(?:\s+on)?)\s+(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_pattern})\s+(\d{{4}})"
        m_start = re.search(start_re, clean)
        if m_start:
            d, m_str, y = m_start.groups()
            start_date = _to_iso(int(d), m_str, int(y))

        # 5. Explicit return / end date: "until 5 october 2026", "end on 5 october 2026", "return on 5 october 2026"
        end_re = rf"(?:until|end(?:ing)?(?:\s+on)?|return(?:ing)?(?:\s+on)?|through)\s+(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_pattern})\s+(\d{{4}})"
        m_end = re.search(end_re, clean)
        if m_end:
            d, m_str, y = m_end.groups()
            end_date = _to_iso(int(d), m_str, int(y))

        # 6. General standalone date with 4-digit year if start_date is not yet found:
        if not start_date:
            gen_re = rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_pattern})\s+(\d{{4}})\b"
            m_gen = re.search(gen_re, clean)
            if m_gen:
                d, m_str, y = m_gen.groups()
                start_date = _to_iso(int(d), m_str, int(y))

        # 6b. Standalone date without year (e.g. "5 Nov", "20 nov", "starting 5 nov", "5th november"):
        if not start_date:
            no_yr_re = rf"(?:(?:start(?:ing)?(?:\s+on)?|from|depart(?:ing)?(?:\s+on)?)\s+)?\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_pattern})\b"
            m_noyr = re.search(no_yr_re, clean)
            if m_noyr:
                d, m_str = m_noyr.groups()
                m_num = month_map.get(m_str.lower()[:3])
                if m_num:
                    cand_year = today_ist.year
                    try:
                        cand_date = datetime.date(cand_year, m_num, int(d))
                        if cand_date < today_ist.date():
                            cand_year += 1
                        start_date = datetime.date(cand_year, m_num, int(d)).strftime("%Y-%m-%d")
                    except Exception:
                        pass

        if start_date:
            return start_date, end_date, True, False, None

        # 7. Unambiguous relative dates resolved using IST (UTC+05:30)
        if re.search(r"\btoday\b", clean):
            return today_ist.strftime("%Y-%m-%d"), None, True, False, None
        if re.search(r"\bday\s+after\s+tomorrow\b", clean):
            d_after = today_ist + datetime.timedelta(days=2)
            return d_after.strftime("%Y-%m-%d"), None, True, False, None
        if re.search(r"\btomorrow\b", clean):
            d_tom = today_ist + datetime.timedelta(days=1)
            return d_tom.strftime("%Y-%m-%d"), None, True, False, None

        m_in_days = re.search(r"\b(?:in|after)\s+(\d+)\s+days?\b", clean)
        if m_in_days:
            n = int(m_in_days.group(1))
            d_n = today_ist + datetime.timedelta(days=n)
            return d_n.strftime("%Y-%m-%d"), None, True, False, None

        # Weekdays: e.g. "this friday", "next monday"
        weekday_map = {
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6,
        }
        for day_name, day_idx in weekday_map.items():
            if re.search(rf"\bthis\s+{day_name}\b", clean):
                ahead = (day_idx - today_ist.weekday()) % 7
                if ahead == 0:
                    ahead = 7
                res_d = today_ist + datetime.timedelta(days=ahead)
                return res_d.strftime("%Y-%m-%d"), None, True, False, None
            if re.search(rf"\bnext\s+{day_name}\b", clean):
                ahead = ((day_idx - today_ist.weekday()) % 7) + 7
                res_d = today_ist + datetime.timedelta(days=ahead)
                return res_d.strftime("%Y-%m-%d"), None, True, False, None

        # Missing dates: neither explicit nor relative dates provided
        return None, None, False, False, None

    def _extract_dates(self, clean: str) -> tuple[str | None, str | None]:
        """Extract explicit calendar start_date and/or end_date in YYYY-MM-DD format."""
        s_date, e_date, _, _, _ = self._extract_date_details(clean)
        return s_date, e_date

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
        # Word numbers: one, two, three, four, five, six, seven, eight, nine, ten
        word_map = {
            "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        }
        word_re = "|".join(word_map.keys())
        m_w = re.search(rf"\b({word_re})\s*(?:people|persons?|adults?|travelers?|friends?|log|பேர்)\b", clean)
        if m_w:
            return word_map[m_w.group(1)]
        # "just X", "only X"
        m = re.search(r"(?:just|only)\s+(\d+)", clean)
        if m:
            return int(m.group(1))
        m_w_only = re.search(rf"(?:just|only)\s+({word_re})\b", clean)
        if m_w_only:
            return word_map[m_w_only.group(1)]
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
            r"make\s+it\s+to|switch\s+to|go\s+to\s+instead|instead\s+go\s+to|explore|visit|trip to)\s+([a-z][a-z]+(?:\s+[a-z][a-z]+)?)",
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

        # Guard against phrases like "flights unchanged" or "keep flights unchanged"
        if re.search(r"\b(?:flights?|planes?|trains?|transport)\s+unchanged\b", clean) or "keep my hotel and flights unchanged" in clean:
            return None, None

        # Mode detection (English and Tanglish)
        if re.search(r"\b(?:trains?|rail|railway)\b", clean) or "train la" in clean or "trainle" in clean:
            mode = "train"
        elif re.search(r"\b(?:flights?|planes?|by\s+air|air\s+travel|air\s+tickets?|aeroplanes?|airlines?)\b", clean) or "flight la" in clean or "flightle" in clean:
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
            if "flight" in clean or "airline" in clean or "by air" in clean or "plane" in clean:
                mode = "flight"
        # Business
        elif re.search(r"\b(?:business(?:\s*class)?)\b", clean):
            cls = "business"
            if "flight" in clean or "airline" in clean or "by air" in clean or "plane" in clean:
                mode = "flight"
        # Economy
        elif re.search(r"\b(?:economy(?:\s*class)?|coach)\b", clean):
            cls = "economy"
            if "flight" in clean or "airline" in clean or "by air" in clean or "plane" in clean:
                mode = "flight"
        # First class (flight vs train)
        elif re.search(r"\b(?:first\s*class)\b", clean):
            if mode == "train" or "train" in clean:
                cls = "1ac"
                mode = "train"
            else:
                cls = "first"
                if "flight" in clean or "airline" in clean or "by air" in clean or "plane" in clean:
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

        # 2. Duration and Dates
        days = self._extract_days(clean)
        start_date, end_date, date_is_explicit, date_is_ambiguous, date_ambiguous_phrase = self._extract_date_details(clean)
        if start_date and end_date:
            from datetime import datetime as dt
            d_s = dt.strptime(start_date, "%Y-%m-%d").date()
            d_e = dt.strptime(end_date, "%Y-%m-%d").date()
            days = max(1, (d_e - d_s).days + 1)
        elif start_date and days is not None and not end_date:
            from datetime import datetime as dt, timedelta
            d_s = dt.strptime(start_date, "%Y-%m-%d").date()
            end_date = (d_s + timedelta(days=max(0, days - 1))).strftime("%Y-%m-%d")

        date_confirmed = date_is_explicit

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
        for to_match in re.finditer(r"\b([a-z][a-z]+)\s+to\s+([a-z][a-z]+(?:\s+[a-z][a-z]+)?)\b", clean):
            src = to_match.group(1).strip().title()
            dst_candidate = to_match.group(2).strip().lower()
            matched_known = None
            for city in sorted(FALLBACK_PARSER_CITIES, key=len, reverse=True):
                if dst_candidate == city or dst_candidate.startswith(city + " "):
                    matched_known = city.title()
                    break
            dst = matched_known or dst_candidate.split()[0].title()

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

        # 5b. "explore X" / "going to X" / "want to go X" / "poganum"
        if not destination:
            goto_match = re.search(
                r"(?:explore|going to|want to go|want to visit|interested to go|"
                r"to visit|to go to|visit|poganum|trip to|tour(?: to)?|travel to|holiday in|vacation in)\s+(?!from\b)([a-z][a-z]+(?:\s+[a-z][a-z]+)?)",
                clean,
            )
            if goto_match:
                dst_candidate = goto_match.group(1).strip().lower()
                matched_known = None
                for city in sorted(FALLBACK_PARSER_CITIES, key=len, reverse=True):
                    if dst_candidate == city or dst_candidate.startswith(city + " "):
                        matched_known = city.title()
                        break
                stopwords2 = {
                    "A", "The", "My", "Our", "This", "That",
                    "Hill", "Beach", "There", "Here",
                    "From", "To", "None", "Flight", "Flights", "Train", "Trains", "Bus", "Buses",
                    "Is", "Are", "Was", "Were", "In", "At", "On", "By", "With", "For", "About",
                    "Some", "Any", "One", "More", "It", "Be", "So", "As", "Not", "Just",
                    "Local Food", "Food", "Theme Park", "Nature", "Mountains", "Culture", "Heritage", "Temples", "Temple", "Places", "Place", "Famous Places", "Local",
                }
                interest_filter = {
                    "local food", "food", "theme park", "nature", "mountains", "culture",
                    "heritage", "temples", "temple", "places", "place", "beach", "beaches", "local",
                }
                words = dst_candidate.split()
                if words and words[0].title() not in stopwords2 and dst_candidate.lower() not in interest_filter:
                    if len(words) > 1 and words[1].title() in stopwords2:
                        dst = matched_known or words[0].title()
                    else:
                        dst = matched_known or dst_candidate.strip().title()
                    if dst not in stopwords2 and dst.lower() not in interest_filter:
                        destination = dst

        # 5c. Known-place fallback scan using FALLBACK_PARSER_CITIES
        if not destination:
            interest_words = {"beach", "hill", "station", "mountain", "temple", "food"}
            for place in sorted(FALLBACK_PARSER_CITIES, key=len, reverse=True):
                if re.search(rf"\b{re.escape(place)}\b", clean):
                    if origin and place.lower() == origin.lower():
                        continue
                    if place in interest_words:
                        continue
                    destination = place.title()
                    break

        requested_destination = destination

        # 6. Interests
        interests = []
        interest_keywords = [
            "scuba diving", "scuba", "local food", "beach", "beaches", "food", "nature", "mountains", "temple", "culture",
            "relaxation", "calm", "theme park", "theme_park", "hill station",
            "famous places", "famous place", "landmarks", "sightseeing", "adventure",
            "scenic", "scenery", "snow", "heritage", "wildlife", "safari", "backwaters", "waterfall", "waterfalls", "trekking",
        ]
        for interest_kw in interest_keywords:
            if interest_kw in clean:
                normalized = interest_kw.replace("_", " ")
                if normalized == "famous place":
                    normalized = "famous places"
                elif normalized == "scenery":
                    normalized = "scenic"
                if normalized not in interests:
                    interests.append(normalized)

        # 7. Travel party
        travel_party = self._extract_travel_party(clean)

        # 8. Transport preference
        transport_mode, transport_class = self._extract_transport(clean)
        booking_confirmed = action == TripAction.CONFIRM_BOOKING

        # 9. Hotel preference & tier
        hotel_tier, hotel_preference = self._extract_hotel_preferences(clean)

        # 10. Strict constraints
        strict_constraints = self._extract_strict_constraints(clean)

        # 11. Phase 5 schedule, pacing, dietary & replacement attributes
        dietary_pref = self._extract_dietary_preference(clean)
        sched_pace = self._extract_schedule_pace(clean)
        arr_time = self._extract_arrival_time(clean)
        earliest_time = self._extract_earliest_activity_time(clean)
        spec_activity = self._extract_special_activity_request(clean)
        rep_target, rep_cat, target_day = self._extract_activity_replacement(clean)

        return ParsedTripIntent(
            action=action,
            budget=budget,
            currency=currency,
            people=people,
            days=days,
            start_date=start_date,
            end_date=end_date,
            date_is_explicit=date_is_explicit,
            date_is_ambiguous=date_is_ambiguous,
            date_ambiguous_phrase=date_ambiguous_phrase,
            date_confirmed=date_confirmed,
            origin=origin,
            destination=destination,
            requested_destination=requested_destination,
            interests=interests,
            hotel_tier=hotel_tier,
            hotel_preference=hotel_preference,
            strict_constraints=strict_constraints,
            travel_party=travel_party,
            traveler_type=travel_party,
            transport_mode=transport_mode,
            transport_class=transport_class,
            booking_confirmed=booking_confirmed,
            dietary_preference=dietary_pref,
            schedule_pace=sched_pace,
            earliest_activity_time=earliest_time,
            arrival_time=arr_time,
            special_activity_request=spec_activity,
            replace_activity_target=rep_target,
            replace_activity_category=rep_cat,
            target_day_number=target_day,
        )

    def _extract_dietary_preference(self, clean: str) -> str | None:
        if any(w in clean for w in ["vegetarian", "veg food", "pure veg", "veg preference"]):
            return "vegetarian"
        if "vegan" in clean:
            return "vegan"
        return None

    def _extract_schedule_pace(self, clean: str) -> str | None:
        if any(w in clean for w in ["relaxed", "leisurely", "relaxing", "slow pace", "relaxed pace"]):
            return "relaxed"
        if any(w in clean for w in ["packed", "fast pace", "fast-paced", "hectic"]):
            return "packed"
        return None

    def _extract_arrival_time(self, clean: str) -> str | None:
        m = re.search(r"\b(?:land|arrive|arrival|landing)\s+(?:at\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b", clean)
        if m:
            raw = m.group(1).strip().upper()
            if "AM" not in raw and "PM" not in raw:
                raw += " AM"
            return raw
        return None

    def _extract_earliest_activity_time(self, clean: str) -> str | None:
        m = re.search(
            r"(?:don't schedule|dont schedule|no activities|activities (?:starting )?after|start (?:after|from))\s+(?:any\s+activities\s+)?(?:before|from|after)?\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
            clean,
        )
        if m:
            raw = m.group(1).strip().upper()
            if "AM" not in raw and "PM" not in raw:
                raw += " AM"
            return raw
        return None

    def _extract_special_activity_request(self, clean: str) -> str | None:
        if any(w in clean for w in ["sunset at a beach", "beach sunset", "sunset at the beach", "watch the sunset"]):
            return "sunset at a beach"
        return None

    def _extract_activity_replacement(self, clean: str) -> tuple[str | None, str | None, int | None]:
        target_name = None
        target_day = None
        replacement_category = None
        m_day = re.search(r"\bday\s*(\d+)\b", clean)
        if m_day:
            target_day = int(m_day.group(1))

        if "museum" in clean and ("closed" in clean or "replace" in clean):
            target_name = "museum"
        elif "temple" in clean and ("closed" in clean or "replace" in clean):
            target_name = "temple"
        elif "fort" in clean and ("closed" in clean or "replace" in clean):
            target_name = "fort"
        elif "morning" in clean and ("closed" in clean or "replace" in clean):
            target_name = "morning"
        elif "afternoon" in clean and ("closed" in clean or "replace" in clean):
            target_name = "afternoon"
        elif "evening" in clean and ("closed" in clean or "replace" in clean):
            target_name = "evening"
        elif "activity" in clean and ("closed" in clean or "replace" in clean):
            target_name = "activity"

        if any(w in clean for w in ["nature spot", "nature place", "nature"]):
            replacement_category = "nature"
        elif any(w in clean for w in ["beach", "beaches"]):
            replacement_category = "beach"
        elif any(w in clean for w in ["heritage", "culture"]):
            replacement_category = "heritage"

        return target_name, replacement_category, target_day

    def _mock_parse_rescue_intent(self, text: str) -> ParsedRescueIntent:
        """Rule-based offline heuristic parser for rescue messages."""
        clean = text.lower()

        # Word numbers map for distance
        word_to_dist = {
            "one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0, "five": 5.0,
            "six": 6.0, "seven": 7.0, "eight": 8.0, "nine": 9.0, "ten": 10.0,
            "eleven": 11.0, "twelve": 12.0, "fifteen": 15.0, "twenty": 20.0,
        }
        # Price dispute patterns
        price_match = re.search(r"(?:₹|rs\.?|inr)?[\s]*([0-9]{1,3}(?:,[0-9]{2,3})+|[0-9]{2,7})", clean)
        if any(w in clean for w in ["auto", "cab", "taxi", "driver", "asking", "charging", "fare", "demanding", "dispute"]):
            reported_price = Decimal(price_match.group(1).replace(",", "")) if price_match else None
            service = "auto" if "auto" in clean else ("cab" if "cab" in clean or "taxi" in clean else "transport")
            dist_match = re.search(
                r"(?:(\d+(?:\.\d+)?)|(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty))\s*(?:km|kms|kilometer|kilometers|kilometre|kilometres)\b",
                clean,
            )
            if dist_match:
                if dist_match.group(1):
                    parsed_distance = float(dist_match.group(1))
                else:
                    parsed_distance = word_to_dist.get(dist_match.group(2).lower())
            else:
                parsed_distance = None
            return ParsedRescueIntent(
                rescue_type="price_dispute",
                user_issue="Fare dispute reported by user",
                location_or_context=None,
                reported_price=reported_price,
                service_type=service,
                distance_km=parsed_distance,
                raw_message=text,
            )

        # Weather / Closure / Transit Disruption / Crowd Swap patterns
        if any(w in clean for w in ["rain", "raining", "storm", "closed", "shut", "landslide", "bad weather", "cancelled", "canceled", "crowded", "crowd", "what else nearby"]):
            loc = None
            if "mattupetty" in clean or "lake" in clean:
                loc = "mattupetty lake"
            elif "museum" in clean:
                loc = "museum"
            elif "airport" in clean:
                loc = "airport"
            elif "beach" in clean:
                loc = "beach"
            elif "temple" in clean:
                loc = "temple"
            elif "fort" in clean:
                loc = "fort"
            elif "peak" in clean or "anamudi" in clean:
                loc = "anamudi peak"
            return ParsedRescueIntent(
                rescue_type="weather_closure",
                user_issue="Attraction crowded or closed" if any(w in clean for w in ("crowded", "crowd", "packed")) else "Weather disruption, venue closure, or cancelled transit reported by user",
                location_or_context=loc,
                reported_price=None,
                service_type="transit" if any(w in clean for w in ("bus", "flight", "train", "cab", "auto")) else None,
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
