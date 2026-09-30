"""AI Intent Service orchestrating prompt generation, OpenRouter calls, and Pydantic validation."""

import json
import logging
import re
from decimal import Decimal
from typing import Any
from pydantic import ValidationError

from budlance.ai.client import GeminiClient, OpenRouterClient
from budlance.ai.exceptions import OpenRouterValidationError
from budlance.ai.prompts import (
    RESCUE_INTENT_SYSTEM_PROMPT,
    TRIP_INTENT_CONTEXT_PROMPT,
    TRIP_INTENT_SYSTEM_PROMPT,
)
from budlance.ai.schemas import ParsedRescueIntent, ParsedTripIntent
from budlance.config import get_settings

logger = logging.getLogger(__name__)


class AIIntentService:
    """Service responsible for converting natural language into validated structured intent."""

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

    async def parse_trip_intent(self, user_prompt: str) -> ParsedTripIntent:
        """Parse natural-language travel request into validated ParsedTripIntent."""
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
        """Parse a follow-up or correction message, merging it with the existing active intent.

        This is used for multi-turn conversations: a partial new message (e.g. "4 days")
        is merged onto an already-known partial intent so no information is lost.
        """
        if not user_prompt or not user_prompt.strip():
            return existing_intent

        if self.use_mock:
            partial = self._mock_parse_trip_intent(user_prompt)
            return existing_intent.merge_with(partial)

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
                "traveler_type": existing_intent.traveler_type,
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
            partial = self._mock_parse_trip_intent(user_prompt)
            return existing_intent.merge_with(partial)

        try:
            updated = ParsedTripIntent.model_validate(raw_data)
            return updated
        except ValidationError as exc:
            logger.warning(
                "Pydantic validation failed for context-aware intent output: %s. "
                "Falling back to heuristic merge.",
                exc,
            )
            partial = self._mock_parse_trip_intent(user_prompt)
            return existing_intent.merge_with(partial)

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
        """Rule-based offline heuristic parser for tests and fallback execution."""
        clean = text.lower()

        # 1. Budget extraction (e.g. ₹15,000 / 15000 inr / 20k / enakku 15000 budget irukku)
        budget: Decimal | None = None
        currency = "INR"
        # Handle "20k" / "20K" shorthand before standard number match
        k_match = re.search(r"(\d+(?:\.\d+)?)\s*k\b", clean)
        budget_match = re.search(r"(?:₹|rs\.?|inr|rupees?|ரூபாய்)?[\s]*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{4,7})", clean)
        if k_match:
            budget = Decimal(str(float(k_match.group(1)) * 1000))
        elif budget_match:
            raw_num = budget_match.group(1).replace(",", "")
            budget = Decimal(raw_num)

        # Currency detection
        if "$" in clean or "usd" in clean:
            currency = "USD"
        elif "€" in clean or "eur" in clean:
            currency = "EUR"

        # 2. Duration extraction (e.g. 5 days, 4 nights, 3 din, 5 நாட்கள்)
        days: int | None = None
        days_match = re.search(r"(\d+)\s*(?:days?|nights?|din|naatkal|நாட்கள்)", clean)
        if days_match:
            days = int(days_match.group(1))

        # 3. People extraction — order matters; Tanglish before English patterns
        people: int | None = None
        if re.search(r"\bsolo\b|\balone\b|\bjust me\b|\bonly me\b", clean):
            people = 1
        elif re.search(r"\bcouple\b", clean):
            people = 2
        else:
            # Tanglish "X peru" (must be checked before generic patterns)
            peru_match = re.search(r"(\d+)\s*peru\b", clean)
            if peru_match:
                people = int(peru_match.group(1))
            else:
                # "2people" / "2persons" (no space, with/without brackets)
                nospace_match = re.search(r"(\d+)\s*(?:people|persons?|adults?|travelers?|log|பேர்)", clean)
                if nospace_match:
                    people = int(nospace_match.group(1))
                else:
                    # "we are 3" / "4 of us"
                    group_match = re.search(r"(?:we are|of us)\s*(\d+)|(\d+)\s*(?:of us)", clean)
                    if group_match:
                        people = int(group_match.group(1) or group_match.group(2))

        # 4. Origin extraction — detect explicitly stated departure before generic city scan.
        # Priority order:
        # 1. Presence patterns: "currently in X", "I am in X", "I'm in X" (highest confidence)
        # 2. Tanglish "X la irundhu" / "X la iruken" / "X irundhu"
        # 3. English "from X" / "starting from X" / "leaving X"
        # 4. X-to-Y pair (lowest confidence, captured together with destination)
        origin: str | None = None

        # 4a. HIGH PRIORITY: Explicit presence patterns — "currently in X", "i am in X", "i'm in X"
        # These must be resolved FIRST before X-to-Y grabs the wrong city.
        _STOPWORDS = {
            "a", "the", "my", "our", "trip", "travel", "going", "want",
            "planning", "plan", "budget", "days", "day", "nights", "night",
            "interested", "excited", "looking",
        }
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

        # 4b. Tanglish origin patterns (iruken/irukken = present tense "am in")
        if not origin:
            tanglish_origin = re.search(
                r"([\w]+)\s+la\s+(?:irun(?:dhu|du|d|ku)|iruk(?:en|iru|kiren?))|"  # "X la irundhu" / "X la iruken"
                r"([\w]+)\s+irundhu\b",                                               # "X irundhu"
                clean,
            )
            if tanglish_origin:
                raw_origin = (tanglish_origin.group(1) or tanglish_origin.group(2) or "").strip()
                if raw_origin and raw_origin not in _STOPWORDS:
                    origin = raw_origin.title()

        # 4c. English explicit origin: "from X" / "starting from X" / "leaving X"
        if not origin:
            explicit_from = re.search(
                r"(?:from|starting from|leaving|departing from)\s+([a-z][a-z]+)",
                clean,
            )
            if explicit_from:
                raw_origin = explicit_from.group(1).strip()
                if raw_origin not in _STOPWORDS:
                    origin = raw_origin.title()

        # 5. Destination — X-to-Y extraction first, then single known-place scan
        destination: str | None = None

        # 5a. "X to Y" pattern — explicit pair (e.g. "chennai to goa")
        to_match = re.search(r"\b([a-z][a-z]+)\s+to\s+([a-z][a-z]+)\b", clean)
        if to_match:
            src = to_match.group(1).strip().title()
            dst = to_match.group(2).strip().title()
            # Ignore filler words and infinitive verbs (e.g. "want to visit", "plan to travel")
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
            }
            if (
                src not in infinitive_src
                and dst not in infinitive_dst
                and dst not in stopwords
            ):
                destination = dst
                if src not in stopwords and not origin:
                    origin = src

        # 5b. "going to X" / "want to go X" / "interested to go X" / "poganum X"
        if not destination:
            goto_match = re.search(
                r"(?:going to|want to go|want to visit|interested to go|"
                r"to visit|to go to|visit|poganum|poganum)\s+([a-z][a-z]+)",
                clean,
            )
            if goto_match:
                dst = goto_match.group(1).strip().title()
                stopwords2 = {
                    "A", "The", "My", "Our", "This", "That",
                    "Hill", "Beach", "There", "Here",
                }
                if dst not in stopwords2:
                    destination = dst

        # 5c. Fall back: scan known places — but skip anything matching origin or an interest keyword
        if not destination:
            known_places = [
                "kerala", "goa", "ooty", "manali", "munnar", "jaipur", "udaipur",
                "coorg", "pondicherry", "ladakh", "chennai", "bangalore", "mumbai", "delhi",
                "kodaikanal", "shimla", "darjeeling", "hyderabad", "kolkata", "pune",
                "agra", "varanasi", "mysore", "mysuru",
            ]
            interest_words = {"beach", "hill", "station", "mountain", "temple", "food"}
            for place in known_places:
                if re.search(rf"\b{place}\b", clean):
                    if origin and place.lower() == origin.lower():
                        continue
                    # Don't set destination if the word is clearly part of an interest phrase
                    if place in interest_words:
                        continue
                    destination = place.title()
                    break

        # 6. Interests
        interests = []
        interest_keywords = [
            "beach", "beaches", "food", "nature", "mountains", "temple", "culture",
            "relaxation", "calm", "theme park", "theme_park", "hill station",
            "famous places", "famous place", "landmarks", "sightseeing", "adventure",
        ]
        for interest_kw in interest_keywords:
            if interest_kw in clean:
                normalized = interest_kw.replace("_", " ")
                # Normalize "famous place" → "famous places"
                if normalized == "famous place":
                    normalized = "famous places"
                if normalized not in interests:
                    interests.append(normalized)

        # 7. Traveler type
        traveler_type: str | None = None
        if "family" in clean:
            traveler_type = "family"
        elif "friend" in clean or "buddies" in clean:
            traveler_type = "friends"
        elif "couple" in clean or "honeymoon" in clean:
            traveler_type = "couple"
        elif re.search(r"\bsolo\b|\balone\b|\bjust me\b|\bonly me\b", clean):
            traveler_type = "solo"

        return ParsedTripIntent(
            budget=budget,
            currency=currency,
            people=people,
            days=days,
            origin=origin,
            destination=destination,
            interests=interests,
            traveler_type=traveler_type,
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
