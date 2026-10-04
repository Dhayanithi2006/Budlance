"""Itinerary Enhancer performing a single batched LLM call to personalize descriptions."""

import json
import logging
from typing import Any
from pydantic import BaseModel, Field, ValidationError

from budlance.ai.client import GeminiClient, OpenRouterClient
from budlance.ai.prompts import ITINERARY_ENHANCEMENT_SYSTEM_PROMPT
from budlance.config import get_settings
from budlance.itinerary.models import GeneratedItinerary

logger = logging.getLogger(__name__)


def count_words(text: str) -> int:
    """Return whitespace-delimited word count for strict length validation."""
    if not text or not text.strip():
        return 0
    return len(text.strip().split())


def is_single_sentence(text: str) -> bool:
    """Return True if text contains exactly one sentence.

    A single sentence contains at most one terminal punctuation mark (. ! ?)
    at the end, with no internal sentence-ending punctuation (. , ! , ? followed by space).
    """
    clean = text.strip()
    if not clean:
        return False
    trimmed = clean.rstrip(".!?")
    if any(p in trimmed for p in (". ", "! ", "? ")):
        return False
    return True


class DayDescription(BaseModel):
    """Personalized slot descriptions for a single travel day."""

    day_number: int
    morning_description: str
    afternoon_description: str
    evening_description: str
    day_theme: str | None = None


class ItineraryDescriptionsBatch(BaseModel):
    """Batched descriptions for all days of an itinerary produced in a single call."""

    day_descriptions: list[DayDescription] = Field(default_factory=list)


class ItineraryEnhancer:
    """Personalizes itinerary descriptions in exactly ONE batched LLM call with strict length validation."""

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

        self.use_mock = use_mock or not getattr(self.client, "has_credentials", False)

    async def enhance_itinerary(
        self,
        itinerary: GeneratedItinerary | None,
        travel_party: str | None = None,
    ) -> GeneratedItinerary | None:
        """Personalize all day descriptions in exactly ONE batched LLM call.

        Strict validation rules:
        - Exactly ONE sentence per place.
        - Strictly fewer than 20 words (1 <= word_count < 20).
        - No factual invention; contextual tone and suitability only.
        - On validation failure or API error, gracefully falls back to curated/default descriptions.
        """
        if itinerary is None or not getattr(itinerary, "is_feasible", False) or not getattr(itinerary, "days", None):
            return itinerary

        if self.use_mock:
            batch = self._mock_enhance_itinerary(itinerary, travel_party)
        else:
            batch = await self._call_llm_batched(itinerary, travel_party)

        if batch is None:
            logger.warning("[ENHANCER] Batched enhancement failed or rejected; retaining curated descriptions.")
            return itinerary

        # Validate that every description in the batch strictly obeys 1 <= word_count < 20 and is 1 sentence
        valid = self._validate_batch(batch, expected_days=len(itinerary.days))
        if not valid:
            logger.warning("[ENHANCER] Batch validation failed word count constraints; falling back to curated descriptions.")
            return itinerary

        # Apply descriptions to itinerary items
        descriptions_by_day = {d.day_number: d for d in batch.day_descriptions}
        for day in itinerary.days:
            d_desc = descriptions_by_day.get(day.day_number)
            if not d_desc:
                continue

            for item in day.items:
                slot = item.time_slot.strip().lower()
                if slot == "morning" and d_desc.morning_description:
                    item.description = d_desc.morning_description.strip()
                elif slot == "afternoon" and d_desc.afternoon_description:
                    item.description = d_desc.afternoon_description.strip()
                elif slot == "evening" and d_desc.evening_description:
                    item.description = d_desc.evening_description.strip()

            if d_desc.day_theme and d_desc.day_theme.strip():
                day.theme_or_summary = d_desc.day_theme.strip()

        return itinerary

    def _validate_batch(
        self,
        batch: ItineraryDescriptionsBatch,
        expected_days: int,
    ) -> bool:
        """Validate that all days are present, each description is 1 sentence, and strictly 1 <= word_count < 20."""
        if len(batch.day_descriptions) != expected_days:
            logger.info(
                "[ENHANCER] Day count mismatch in LLM output: expected %s, got %s",
                expected_days,
                len(batch.day_descriptions),
            )
            return False

        for day_desc in batch.day_descriptions:
            for slot_name, text in (
                ("morning", day_desc.morning_description),
                ("afternoon", day_desc.afternoon_description),
                ("evening", day_desc.evening_description),
            ):
                wc = count_words(text)
                if not (1 <= wc < 20):
                    logger.info(
                        "[ENHANCER] Day %s %s description rejected: word count %s not in range [1, 20).",
                        day_desc.day_number,
                        slot_name,
                        wc,
                    )
                    return False
                if not is_single_sentence(text):
                    logger.info(
                        "[ENHANCER] Day %s %s description rejected: must be exactly one sentence.",
                        day_desc.day_number,
                        slot_name,
                    )
                    return False
        return True

    async def _call_llm_batched(
        self,
        itinerary: GeneratedItinerary,
        travel_party: str | None,
    ) -> ItineraryDescriptionsBatch | None:
        """Perform a single OpenRouter/Gemini call to get descriptions for all days."""
        days_payload = []
        for day in itinerary.days:
            morning_item = next((it for it in day.items if it.time_slot.lower() == "morning"), None)
            afternoon_item = next((it for it in day.items if it.time_slot.lower() == "afternoon"), None)
            evening_item = next((it for it in day.items if it.time_slot.lower() == "evening"), None)

            days_payload.append(
                {
                    "day_number": day.day_number,
                    "morning": morning_item.attraction_name or morning_item.activity if morning_item else "Morning leisure",
                    "afternoon": afternoon_item.attraction_name or afternoon_item.activity if afternoon_item else "Afternoon exploration",
                    "evening": evening_item.attraction_name or evening_item.activity if evening_item else "Local dining",
                }
            )

        user_content = json.dumps(
            {
                "destination": itinerary.destination,
                "travel_party": travel_party,
                "days": days_payload,
            },
            ensure_ascii=False,
        )

        messages = [
            {"role": "system", "content": ITINERARY_ENHANCEMENT_SYSTEM_PROMPT},
            {"role": "user", "content": f"Generate personalized descriptions for this itinerary:\n{user_content}"},
        ]

        try:
            raw_data = await self.client.chat_completion(messages)
            return ItineraryDescriptionsBatch.model_validate(raw_data)
        except Exception as exc:
            logger.warning("[ENHANCER] Live LLM call failed (%s: %s).", type(exc).__name__, exc)
            return None

    def _mock_enhance_itinerary(
        self,
        itinerary: GeneratedItinerary,
        travel_party: str | None,
    ) -> ItineraryDescriptionsBatch:
        """Deterministic offline mock generator producing valid single-sentence descriptions under 20 words."""
        party = (travel_party or "").strip().lower()
        descriptions: list[DayDescription] = []

        for day in itinerary.days:
            morning_item = next((it for it in day.items if it.time_slot.lower() == "morning"), None)
            afternoon_item = next((it for it in day.items if it.time_slot.lower() == "afternoon"), None)
            evening_item = next((it for it in day.items if it.time_slot.lower() == "evening"), None)

            m_name = morning_item.attraction_name or morning_item.activity if morning_item else "morning leisure"
            a_name = afternoon_item.attraction_name or afternoon_item.activity if afternoon_item else "afternoon exploration"
            e_name = evening_item.attraction_name or evening_item.activity if evening_item else "evening relaxation"

            if party == "family":
                m_desc = f"Explore {m_name} together with child-friendly walkways and relaxed pacing."
                a_desc = f"Enjoy an easy afternoon discovering {a_name} with engaging sights for all generations."
                e_desc = f"Conclude your family day with calm, child-friendly dining near {itinerary.destination}."
                theme = f"Day {day.day_number}: Family Discovery & Comfort in {itinerary.destination}"

            elif party == "couple":
                m_desc = f"Stroll through scenic {m_name} hand in hand away from morning crowds."
                a_desc = f"Admire romantic views and timeless architecture together at {a_name}."
                e_desc = f"Share an intimate dinner featuring authentic local delicacies in {itinerary.destination}."
                theme = f"Day {day.day_number}: Romantic Highlights in {itinerary.destination}"

            elif party == "friends":
                m_desc = f"Kick off the morning snapping vibrant group photos across {m_name}."
                a_desc = f"Experience lively vibes and fun viewpoints with your squad at {a_name}."
                e_desc = f"Dive into spirited local street food and evening night markets in {itinerary.destination}."
                theme = f"Day {day.day_number}: Fun Friends Adventure in {itinerary.destination}"

            elif party == "solo":
                m_desc = f"Enjoy a peaceful solo stroll exploring {m_name} at your own pace."
                a_desc = f"Discover inspiring heritage and quiet corners unhurriedly at {a_name}."
                e_desc = f"Savor a quiet dinner and thoughtful personal reflection in {itinerary.destination}."
                theme = f"Day {day.day_number}: Solo Journey & Discovery in {itinerary.destination}"

            elif party == "relatives":
                m_desc = f"Visit {m_name} with comfortable accessibility and easy seating for all relatives."
                a_desc = f"Tour {a_name} together at a gentle pace suitable for extended family."
                e_desc = f"Gather for a relaxed and spacious group banquet in {itinerary.destination}."
                theme = f"Day {day.day_number}: Comfortable Group Travel in {itinerary.destination}"

            else:
                m_desc = f"Begin your morning exploring historical highlights across scenic {m_name}."
                a_desc = f"Discover remarkable architecture and local cultural craftsmanship at {a_name}."
                e_desc = f"Unwind with delicious regional flavors at a cozy dinner in {itinerary.destination}."
                theme = f"Day {day.day_number}: Cultural Exploration in {itinerary.destination}"

            descriptions.append(
                DayDescription(
                    day_number=day.day_number,
                    morning_description=m_desc,
                    afternoon_description=a_desc,
                    evening_description=e_desc,
                    day_theme=theme,
                )
            )

        return ItineraryDescriptionsBatch(day_descriptions=descriptions)
