"""AI Intent Service orchestrating prompt generation, OpenRouter calls, and Pydantic validation."""

import logging
import re
from decimal import Decimal
from typing import Any
from pydantic import ValidationError

from budlance.ai.client import OpenRouterClient
from budlance.ai.exceptions import OpenRouterValidationError
from budlance.ai.prompts import RESCUE_INTENT_SYSTEM_PROMPT, TRIP_INTENT_SYSTEM_PROMPT
from budlance.ai.schemas import ParsedRescueIntent, ParsedTripIntent

logger = logging.getLogger(__name__)


class AIIntentService:
    """Service responsible for converting natural language into validated structured intent."""

    def __init__(
        self,
        client: OpenRouterClient | None = None,
        use_mock: bool = False,
    ) -> None:
        self.client = client or OpenRouterClient()
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

        raw_data = await self.client.chat_completion(messages)

        try:
            return ParsedTripIntent.model_validate(raw_data)
        except ValidationError as exc:
            logger.warning("Pydantic validation failed for travel intent output: %s", exc)
            raise OpenRouterValidationError(f"Invalid structured output format: {exc}") from exc

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

        raw_data = await self.client.chat_completion(messages)

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

        # 1. Budget extraction (e.g. ₹15,000, 15000 inr, 20000 rupees, 15000 ரூபாய்)
        budget: Decimal | None = None
        currency = "INR"
        budget_match = re.search(r"(?:₹|rs\.?|inr|rupees?|ரூபாய்)?\s*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{4,7})", clean)
        if budget_match:
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

        # 3. People extraction (e.g. 3 people, 2 adults, 4 log, 3 பேர், family of 4, solo, couple)
        people: int | None = None
        if "solo" in clean:
            people = 1
        elif "couple" in clean:
            people = 2
        else:
            people_match = re.search(r"(\d+)\s*(?:people|persons?|adults?|travelers?|log|பேர்)", clean)
            if people_match:
                people = int(people_match.group(1))

        # 4. Destination & Origin
        origin: str | None = None
        from_match = re.search(r"(?:from|starting from|leaving)\s+([a-zA-Z]+)", clean)
        if from_match:
            origin = from_match.group(1).title()

        destination: str | None = None
        known_places = [
            "kerala", "goa", "ooty", "manali", "munnar", "jaipur", "udaipur",
            "coorg", "pondicherry", "ladakh", "chennai", "bangalore", "mumbai", "delhi"
        ]
        for place in known_places:
            if re.search(rf"\b{place}\b", clean):
                if not origin or place.lower() != origin.lower():
                    destination = place.title()
                    break

        # 5. Interests
        interests = []
        for interest_kw in ["beach", "beaches", "food", "nature", "mountains", "temple", "culture", "relaxation", "calm"]:
            if interest_kw in clean:
                interests.append(interest_kw)

        # 6. Traveler type
        traveler_type: str | None = None
        if "family" in clean:
            traveler_type = "family"
        elif "friend" in clean or "buddies" in clean:
            traveler_type = "friends"
        elif "couple" in clean or "honeymoon" in clean:
            traveler_type = "couple"
        elif "solo" in clean:
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
        price_match = re.search(r"(?:₹|rs\.?|inr)?\s*(\d{2,5})", clean)
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
