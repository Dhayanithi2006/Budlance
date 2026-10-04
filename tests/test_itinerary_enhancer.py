"""Tests for ItineraryEnhancer: single batched LLM call, party personalization, and strict length validation."""

from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest

from budlance.engine.budget import ReverseBudgetEngine
from budlance.estimation.estimator import EstimationLayer
from budlance.itinerary.enhancer import (
    DayDescription,
    ItineraryDescriptionsBatch,
    ItineraryEnhancer,
    count_words,
    is_single_sentence,
)
from budlance.itinerary.generator import ItineraryGenerator
from budlance.schemas.travel import FlightOption, HotelOption


@pytest.fixture
def sample_itinerary():
    """Build a 2-day sample itinerary in Gujarat with real attractions."""
    engine = ReverseBudgetEngine()
    estimation = EstimationLayer()
    transport = FlightOption(airline="IndiGo", price=Decimal("4000.00"))
    hotel = HotelOption(name="Haveli", total_price=Decimal("4000.00"), price_per_night=Decimal("2000.00"))
    food = estimation.estimate_food(people=2, days=2)
    transit = estimation.estimate_local_transit_daily(days=2, people=2)

    eval_result = engine.evaluate(
        total_budget=Decimal("20000.00"),
        people=2,
        days=2,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
    )

    gen = ItineraryGenerator()
    return gen.generate(
        trip_id=uuid4(),
        destination="Gujarat",
        evaluation=eval_result,
        days=2,
        transport=transport,
        hotel=hotel,
    )


@pytest.mark.asyncio
async def test_mock_enhancer_produces_valid_descriptions_for_all_days(sample_itinerary):
    """Mock mode produces valid single-sentence descriptions under 20 words for all days."""
    enhancer = ItineraryEnhancer(use_mock=True)
    enhanced = await enhancer.enhance_itinerary(sample_itinerary, travel_party="family")

    assert len(enhanced.days) == 2
    for day in enhanced.days:
        for item in day.items:
            wc = count_words(item.description)
            assert 1 <= wc < 20, f"Slot {item.time_slot} Day {day.day_number} word count {wc} out of range [1, 20)"
            assert is_single_sentence(item.description), f"Slot {item.time_slot} Day {day.day_number} is not single sentence"


@pytest.mark.asyncio
async def test_descriptions_reflect_travel_party(sample_itinerary):
    """Personalized descriptions reflect specific travel_party keywords."""
    enhancer = ItineraryEnhancer(use_mock=True)

    # Family
    fam_itin = await enhancer.enhance_itinerary(sample_itinerary, travel_party="family")
    fam_text = " ".join(item.description for day in fam_itin.days for item in day.items).lower()
    assert "family" in fam_text or "child" in fam_text

    # Couple
    couple_itin = await enhancer.enhance_itinerary(sample_itinerary, travel_party="couple")
    couple_text = " ".join(item.description for day in couple_itin.days for item in day.items).lower()
    assert "romantic" in couple_text or "partner" in couple_text or "scenic" in couple_text

    # Friends
    friends_itin = await enhancer.enhance_itinerary(sample_itinerary, travel_party="friends")
    friends_text = " ".join(item.description for day in friends_itin.days for item in day.items).lower()
    assert "friends" in friends_text or "social" in friends_text or "group" in friends_text or "squad" in friends_text


@pytest.mark.asyncio
async def test_length_validation_rejects_zero_or_empty_words(sample_itinerary):
    """Validation rejects empty/0-word descriptions and falls back to curated description."""
    mock_client = AsyncMock()
    mock_client.has_credentials = True
    valid_desc = "Stroll peacefully through the historic corridors and enjoy the scenic morning views."

    invalid_batch = {
        "day_descriptions": [
            {
                "day_number": 1,
                "morning_description": "",
                "afternoon_description": valid_desc,
                "evening_description": valid_desc,
                "day_theme": "Day 1 Highlights",
            },
            {
                "day_number": 2,
                "morning_description": valid_desc,
                "afternoon_description": valid_desc,
                "evening_description": valid_desc,
                "day_theme": "Day 2 Highlights",
            },
        ]
    }
    mock_client.chat_completion.return_value = invalid_batch

    original_desc = sample_itinerary.days[0].items[0].description
    enhancer = ItineraryEnhancer(client=mock_client, use_mock=False)
    enhanced = await enhancer.enhance_itinerary(sample_itinerary, travel_party="family")

    # Should retain curated description because validation rejected the empty description
    assert enhanced.days[0].items[0].description == original_desc


@pytest.mark.asyncio
async def test_length_validation_rejects_20_or_more_words(sample_itinerary):
    """Validation rejects descriptions with >= 20 words and falls back to curated description."""
    mock_client = AsyncMock()
    mock_client.has_credentials = True
    # 22 words (>= 20)
    long_desc = "This is a sentence that has been intentionally written to have more than twenty words in it for length testing in our suite."
    valid_desc = "Stroll peacefully through the historic corridors and enjoy the scenic morning views."

    invalid_batch = {
        "day_descriptions": [
            {
                "day_number": 1,
                "morning_description": long_desc,
                "afternoon_description": valid_desc,
                "evening_description": valid_desc,
                "day_theme": "Day 1 Highlights",
            },
            {
                "day_number": 2,
                "morning_description": valid_desc,
                "afternoon_description": valid_desc,
                "evening_description": valid_desc,
                "day_theme": "Day 2 Highlights",
            },
        ]
    }
    mock_client.chat_completion.return_value = invalid_batch

    original_desc = sample_itinerary.days[0].items[0].description
    enhancer = ItineraryEnhancer(client=mock_client, use_mock=False)
    enhanced = await enhancer.enhance_itinerary(sample_itinerary, travel_party="couple")

    # Should retain curated description
    assert enhanced.days[0].items[0].description == original_desc


@pytest.mark.asyncio
async def test_validation_rejects_multiple_sentences(sample_itinerary):
    """Validation rejects descriptions with multiple sentences and falls back to curated description."""
    mock_client = AsyncMock()
    mock_client.has_credentials = True
    # 2 short sentences totaling 9 words
    multi_sentence = "Explore the museum grounds. Enjoy peaceful garden walkways."
    valid_desc = "Stroll peacefully through the historic corridors and enjoy the scenic morning views."

    invalid_batch = {
        "day_descriptions": [
            {
                "day_number": 1,
                "morning_description": multi_sentence,
                "afternoon_description": valid_desc,
                "evening_description": valid_desc,
                "day_theme": "Day 1 Highlights",
            },
            {
                "day_number": 2,
                "morning_description": valid_desc,
                "afternoon_description": valid_desc,
                "evening_description": valid_desc,
                "day_theme": "Day 2 Highlights",
            },
        ]
    }
    mock_client.chat_completion.return_value = invalid_batch

    original_desc = sample_itinerary.days[0].items[0].description
    enhancer = ItineraryEnhancer(client=mock_client, use_mock=False)
    enhanced = await enhancer.enhance_itinerary(sample_itinerary, travel_party="couple")

    # Should retain curated description
    assert enhanced.days[0].items[0].description == original_desc


@pytest.mark.asyncio
async def test_single_batched_call_called_exactly_once(sample_itinerary):
    """Verify that chat_completion is called EXACTLY ONCE for an entire multi-day itinerary."""
    mock_client = AsyncMock()
    mock_client.has_credentials = True
    valid_desc = "Stroll peacefully through the historic corridors and enjoy the scenic morning views."

    valid_batch = {
        "day_descriptions": [
            {
                "day_number": 1,
                "morning_description": valid_desc,
                "afternoon_description": valid_desc,
                "evening_description": valid_desc,
                "day_theme": "Day 1 Theme",
            },
            {
                "day_number": 2,
                "morning_description": valid_desc,
                "afternoon_description": valid_desc,
                "evening_description": valid_desc,
                "day_theme": "Day 2 Theme",
            },
        ]
    }
    mock_client.chat_completion.return_value = valid_batch

    enhancer = ItineraryEnhancer(client=mock_client, use_mock=False)
    enhanced = await enhancer.enhance_itinerary(sample_itinerary, travel_party="friends")

    # Assert exactly ONE call was made
    assert mock_client.chat_completion.call_count == 1

    # Assert descriptions were updated
    assert enhanced.days[0].items[0].description == valid_desc
    assert enhanced.days[1].items[1].description == valid_desc
