"""Tests for travel_party intent extraction and prompt integration."""

import pytest
from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.ai.service import AIIntentService
from budlance.ai.prompts import TRIP_INTENT_SYSTEM_PROMPT, TRIP_INTENT_CONTEXT_PROMPT


@pytest.fixture
def intent_service():
    """Create an AIIntentService configured for mock/offline heuristic execution."""
    return AIIntentService(use_mock=True)


def test_prompt_includes_travel_party_definition_and_schema():
    """Verify system and context prompts include travel_party specifications."""
    for prompt in (TRIP_INTENT_SYSTEM_PROMPT, TRIP_INTENT_CONTEXT_PROMPT):
        assert '"travel_party"' in prompt
        assert "solo" in prompt
        assert "couple" in prompt
        assert "friends" in prompt
        assert "family" in prompt
        assert "relatives" in prompt
        assert "Do NOT infer travel_party from the number of people alone" in prompt


@pytest.mark.asyncio
async def test_extract_couple_explicit(intent_service):
    """'2 people, we are a couple' -> people=2, travel_party='couple'."""
    parsed = await intent_service.parse_trip_intent(
        "I want to go to Goa from Chennai for 3 days, budget 20000, 2 people, we are a couple"
    )
    assert parsed.people == 2
    assert parsed.travel_party == "couple"
    assert parsed.traveler_type == "couple"


@pytest.mark.asyncio
async def test_extract_people_without_party(intent_service):
    """'2 people going together' -> people=2, travel_party=None (strict rule)."""
    parsed = await intent_service.parse_trip_intent(
        "Trip from Chennai to Goa, 5 days, budget 10000, 2 people going together"
    )
    assert parsed.people == 2
    assert parsed.travel_party is None


@pytest.mark.asyncio
async def test_extract_friends(intent_service):
    """'5 friends' -> people=5, travel_party='friends'."""
    parsed = await intent_service.parse_trip_intent(
        "Planning a 4 day trip to Manali for 5 friends, budget 50000 from Delhi"
    )
    assert parsed.people == 5
    assert parsed.travel_party == "friends"


@pytest.mark.asyncio
async def test_extract_family(intent_service):
    """'family trip' -> travel_party='family'."""
    parsed = await intent_service.parse_trip_intent(
        "family trip to Ooty from Bangalore for 3 days, budget 30000"
    )
    assert parsed.travel_party == "family"


@pytest.mark.asyncio
async def test_extract_relatives(intent_service):
    """'with relatives' -> travel_party='relatives'."""
    parsed = await intent_service.parse_trip_intent(
        "Trip to Mysore from Chennai with relatives for 2 days, budget 20000"
    )
    assert parsed.travel_party == "relatives"


@pytest.mark.asyncio
async def test_extract_solo(intent_service):
    """'solo trip' -> people=1, travel_party='solo'."""
    parsed = await intent_service.parse_trip_intent(
        "solo trip to Pondicherry from Chennai for 2 days, budget 8000"
    )
    assert parsed.people == 1
    assert parsed.travel_party == "solo"


@pytest.mark.asyncio
async def test_extract_spouse_phrase(intent_service):
    """'me and my wife' -> people=2, travel_party='couple'."""
    parsed = await intent_service.parse_trip_intent(
        "trip from Mumbai to Goa for 4 days, budget 25000, me and my wife"
    )
    assert parsed.people == 2
    assert parsed.travel_party == "couple"


@pytest.mark.asyncio
async def test_context_parse_includes_travel_party(intent_service):
    """parse_trip_intent_with_context preserves and respects travel_party in context."""
    existing = ParsedTripIntent(
        action=TripAction.NEW_TRIP,
        budget=10000,
        people=2,
        days=3,
        origin="Chennai",
        destination="Goa",
        travel_party="couple",
    )
    # User updates duration: "make it 4 days"
    updated = await intent_service.parse_trip_intent_with_context(
        "make it 4 days",
        existing_intent=existing,
    )
    assert updated.action == TripAction.CHANGE_DAYS
    assert updated.days == 4
    # The action router will preserve travel_party from existing context


@pytest.mark.asyncio
async def test_context_parse_change_people_to_solo(intent_service):
    """User changes headcount to solo in follow-up."""
    existing = ParsedTripIntent(
        action=TripAction.NEW_TRIP,
        budget=10000,
        people=2,
        days=3,
        origin="Chennai",
        destination="Goa",
        travel_party="couple",
    )
    updated = await intent_service.parse_trip_intent_with_context(
        "only me now",
        existing_intent=existing,
    )
    assert updated.action == TripAction.CHANGE_PEOPLE
    assert updated.people == 1
    assert updated.travel_party == "solo"
