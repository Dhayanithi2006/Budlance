"""Tests for Item 2: travel_party headcount matching, neutral tone, and 1 traveler handling."""

from decimal import Decimal
from uuid import uuid4
import pytest

from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.ai.service import AIIntentService
from budlance.itinerary.enhancer import ItineraryEnhancer
from budlance.itinerary.models import GeneratedItinerary, ItineraryDay, ItineraryItem
from budlance.orchestrator.orchestrator import BudlanceOrchestrator


def test_solo_only_if_people_1():
    """solo is valid only when people == 1. When people > 1, solo resets to None."""
    # 1 person solo -> valid
    intent1 = ParsedTripIntent(people=1, travel_party="solo")
    assert intent1.people == 1
    assert intent1.travel_party == "solo"

    # 2 people solo -> invalid, resets to None
    intent2 = ParsedTripIntent(people=2, travel_party="solo")
    assert intent2.people == 2
    assert intent2.travel_party is None

    # 3 people solo -> invalid, resets to None
    intent3 = ParsedTripIntent(people=3, travel_party="solo")
    assert intent3.people == 3
    assert intent3.travel_party is None


def test_people_1_group_parties_reset_to_none():
    """When people == 1, group travel_party values (friends, couple, family, relatives) reset to None."""
    for grp in ("friends", "couple", "family", "relatives"):
        intent = ParsedTripIntent(people=1, travel_party=grp)
        assert intent.people == 1
        assert intent.travel_party is None, f"Expected travel_party to be None for people=1, got {intent.travel_party}"


def test_change_people_action_headcount_matching():
    """Changing people count via apply_change_action maintains valid headcount rules."""
    # From couple (people=2) to people=1 -> resets party to None
    base_couple = ParsedTripIntent(
        action=TripAction.NEW_TRIP,
        budget=Decimal("20000"),
        people=2,
        days=3,
        travel_party="couple",
    )
    change_to_1 = ParsedTripIntent(
        action=TripAction.CHANGE_PEOPLE,
        people=1,
    )
    updated = base_couple.apply_change_action(change_to_1)
    assert updated.people == 1
    assert updated.travel_party is None

    # From solo (people=1) to people=3 -> resets party to None
    base_solo = ParsedTripIntent(
        action=TripAction.NEW_TRIP,
        budget=Decimal("20000"),
        people=1,
        days=3,
        travel_party="solo",
    )
    change_to_3 = ParsedTripIntent(
        action=TripAction.CHANGE_PEOPLE,
        people=3,
    )
    updated3 = base_solo.apply_change_action(change_to_3)
    assert updated3.people == 3
    assert updated3.travel_party is None


def test_enhancer_null_travel_party_never_defaults_to_friends():
    """When travel_party is None, enhancer must produce neutral tone and NEVER mention friends or squad."""
    enhancer = ItineraryEnhancer(use_mock=True)
    itinerary = GeneratedItinerary(
        trip_id=uuid4(),
        destination="Goa",
        days_count=1,
        total_budget=Decimal("20000.00"),
        total_planned_cost=Decimal("1000.00"),
        is_feasible=True,
        days=[
            ItineraryDay(
                day_number=1,
                theme_or_summary="Day 1 in Goa",
                items=[
                    ItineraryItem(time_slot="morning", attraction_name="Fort Aguada", activity="Sightseeing", category="attraction"),
                    ItineraryItem(time_slot="afternoon", attraction_name="Sinquerim Beach", activity="Beach walk", category="attraction"),
                    ItineraryItem(time_slot="evening", attraction_name="Panaji Promenade", activity="Dinner", category="food"),
                ],
            )
        ],
    )
    batch = enhancer._mock_enhance_itinerary(itinerary, travel_party=None)
    day1 = batch.day_descriptions[0]

    full_text = f"{day1.day_theme} {day1.morning_description} {day1.afternoon_description} {day1.evening_description}".lower()
    assert "friend" not in full_text
    assert "squad" not in full_text
    assert "cultural exploration" in day1.day_theme.lower()


@pytest.mark.asyncio
async def test_1_traveler_full_orchestrator_flow():
    """Test 1 traveler prompt through orchestrator:
    - travel_party is None (null)
    - Tone is neutral
    - Output never defaults to friends or includes '(Friends)'
    """
    ai_service = AIIntentService(use_mock=True)
    parsed = await ai_service.parse_trip_intent("Plan a trip from Chennai to Goa for 1 traveler, 3 days, budget 20000")
    assert parsed.people == 1
    assert parsed.travel_party is None

    orch = BudlanceOrchestrator(ai_service=ai_service)
    res = await orch.handle_user_message(
        telegram_user_id=12345,
        chat_id=777888,
        message="Plan a trip from Chennai to Goa for 1 traveler, 3 days, budget 20000",
    )
    assert res.status == "FEASIBLE"
    assert "1 traveler" in res.message_text
    assert "(Friends)" not in res.message_text
    assert "(friends)" not in res.message_text.lower()
    # Itinerary must not have friends tone
    assert "squad" not in res.message_text.lower()
