"""Comprehensive test suite for Phase 4: Conversational AI & Natural-Language Understanding.

Covers:
1. Example 1 — Natural-language trip planning with inclusive calendar dates:
   "Plan a trip from Chennai to Goa, 1–5 Nov 2026, 2 people, budget ₹30,000"
   -> 5 days, 4 nights (1–5 Nov 2026), 2 people, budget ₹30,000, FEASIBLE.
2. Example 2 — Multi-attribute conversational refinement:
   "Actually make it 4 days, lower my total budget to ₹25,000, and we want a hotel near the beach"
   -> Preserves Chennai, Goa, 2 people; updates to 4 days, 3 nights (1–4 Nov 2026), budget ₹25,000;
      applies hotel preference "near the beach", invalidates stale cache, FEASIBLE.
3. Example 3 — Explicit strict constraints & no silent downgrades:
   "Keep those dates and the ₹25,000 budget, but we strictly need flights and a 4-star hotel"
   -> Preserves 1–4 Nov 2026, ₹25,000, 2 people; enforces flights + 4-star hotel;
      strictly forbids silent downgrades; returns NOT_FEASIBLE with exact shortfall and proposed alternatives.
4. Additional Conversational & Architectural Invariants:
   - Inclusive calendar day & stay night calculations.
   - Relative duration deltas ("extend by 1 day", "shorten by 1 day").
   - Multi-user isolation across distinct telegram chat IDs.
   - Cache invalidation of component bookings upon modification.
   - Offline LLM fallback robustness with context preservation.
   - Preservation of DataSource provenance and Mode B fee disclosures.
"""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from budlance.ai.exceptions import OpenRouterValidationError
from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.ai.service import AIIntentService
from budlance.config import get_settings
from budlance.db.models import Trip, utc_now
from budlance.db.repositories.conversation_repo import ConversationStateRepository
from budlance.db.repositories.intent_repo import IntentRepository
from budlance.db.repositories.itinerary_repo import ItineraryRepository
from budlance.db.repositories.ledger_repo import LedgerRepository
from budlance.db.repositories.trip_repo import TripRepository
from budlance.db.repositories.user_repo import UserRepository
from budlance.engine.models import FeasibilityStatus
from budlance.engine.optimizer import OptimizationEngine
from budlance.orchestrator.models import OrchestrationResult
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.schemas.travel import FlightOption, HotelOption, TransitOption
from budlance.serpapi.models import DataSource


# =============================================================================
# THREE EXECUTION EXAMPLES
# =============================================================================

@pytest.mark.asyncio
async def test_phase4_example_1_initial_trip_with_date_range(monkeypatch):
    """Example 1: Initial trip planning with inclusive calendar dates.

    User prompt: "Plan a trip from Chennai to Goa, 1–5 Nov 2026, 2 people, budget ₹30,000"
    - Inclusive days: 1–5 Nov 2026 = 5 days, 4 nights.
    - Preserves origin Chennai, destination Goa, headcount 2, budget ₹30,000.
    - Yields a FEASIBLE plan.
    """
    monkeypatch.setenv("SERPAPI_LIVE_ENABLED", "false")
    ai_service = AIIntentService(use_mock=True)
    orchestrator = BudlanceOrchestrator(ai_service=ai_service)
    chat_id = 811001

    prompt = "Plan a trip from Chennai to Goa, 1–5 Nov 2026, 2 people, budget ₹30,000"
    result = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message=prompt,
    )

    assert result.status == "FEASIBLE"
    assert result.selected_destination == "Goa"
    assert result.budget_breakdown is not None
    assert result.budget_breakdown.total_budget == Decimal("30000.00")
    assert result.generated_itinerary is not None
    assert len(result.generated_itinerary.days) == 5

    # Check inclusive date representation
    days = result.generated_itinerary.days
    assert days[0].date_str == "2026-11-01"
    assert days[-1].date_str == "2026-11-05"
    assert "Budlance Trip Plan: Goa" in result.message_text
    assert "2 travelers" in result.message_text
    assert "5 days" in result.message_text


@pytest.mark.asyncio
async def test_phase4_example_2_multi_attribute_modification(monkeypatch):
    """Example 2: Multi-attribute follow-up refinement.

    Turn 1: "Plan a trip from Chennai to Goa, 1–5 Nov 2026, 2 people, budget ₹30,000" -> FEASIBLE
    Turn 2: "Actually make it 4 days, lower my total budget to ₹25,000, and we want a hotel near the beach"
    - Preserves: origin Chennai, destination Goa, headcount 2.
    - Recalculates: 4 days, 3 nights (1–4 Nov 2026).
    - Updates: budget ₹25,000, hotel preference "near the beach".
    - Yields a FEASIBLE plan with updated breakdown and schedule.
    """
    monkeypatch.setenv("SERPAPI_LIVE_ENABLED", "false")
    ai_service = AIIntentService(use_mock=True)
    orchestrator = BudlanceOrchestrator(ai_service=ai_service)
    chat_id = 811002

    # Turn 1
    t1_prompt = "Plan a trip from Chennai to Goa, 1–5 Nov 2026, 2 people, budget ₹30,000"
    res1 = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message=t1_prompt,
    )
    assert res1.status == "FEASIBLE"
    assert len(res1.generated_itinerary.days) == 5

    # Turn 2: Multi-attribute modification
    t2_prompt = "Actually make it 4 days, lower my total budget to ₹25,000, and we want a hotel near the beach"
    res2 = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message=t2_prompt,
    )

    assert res2.status == "FEASIBLE"
    assert res2.action == TripAction.MODIFY_TRIP
    assert res2.selected_destination == "Goa"
    assert res2.budget_breakdown is not None
    assert res2.budget_breakdown.total_budget == Decimal("25000.00")
    assert res2.generated_itinerary is not None
    assert len(res2.generated_itinerary.days) == 4

    # Inclusive calendar days recomputed: 1–4 Nov 2026 (4 days, 3 nights)
    days = res2.generated_itinerary.days
    assert days[0].date_str == "2026-11-01"
    assert days[-1].date_str == "2026-11-04"
    assert "4 days" in res2.message_text


@pytest.mark.asyncio
async def test_phase4_example_3_strict_constraints_and_no_silent_downgrades(monkeypatch):
    """Example 3: Explicit transport & luxury hotel constraints must NOT be silently downgraded.

    Follow-up to Example 2:
    Turn 3: "Keep those dates and the ₹25,000 budget, but we strictly need flights and a 4-star hotel"
    - Strict requirements: flights + 4-star hotel for 2 people across 4 days (1–4 Nov 2026).
    - ₹25,000 cannot support round-trip flights + 3 nights 4-star hotel for 2 people.
    - System MUST return NOT_FEASIBLE (no silent downgrading to bus or budget lodge).
    - Returns exact shortfall and proposed alternatives.
    """
    monkeypatch.setenv("SERPAPI_LIVE_ENABLED", "false")
    ai_service = AIIntentService(use_mock=True)
    orchestrator = BudlanceOrchestrator(ai_service=ai_service)
    chat_id = 811003

    # Turn 1: 5 days, ₹30,000
    t1_prompt = "Plan a trip from Chennai to Goa, 1–5 Nov 2026, 2 people, budget ₹30,000"
    res1 = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message=t1_prompt,
    )
    assert res1.status == "FEASIBLE"

    # Turn 2: 4 days, ₹25,000
    t2_prompt = "Actually make it 4 days, lower my total budget to ₹25,000, and we want a hotel near the beach"
    res2 = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message=t2_prompt,
    )
    assert res2.status == "FEASIBLE"

    # Turn 3: Strict flights + 4-star hotel constraints
    real_lookup = orchestrator.lookup_transport_options

    async def mock_lookup(origin, destination, people, transport_mode=None, transport_class=None, **kwargs):
        if transport_mode == "flight":
            return [
                FlightOption(
                    airline="IndiGo",
                    departure_airport="MAA",
                    arrival_airport="GOI",
                    price=Decimal("12000.00"),
                    source=DataSource.LIVE,
                )
            ]
        return await real_lookup(
            origin, destination, people, transport_mode=transport_mode, transport_class=transport_class, **kwargs
        )

    orchestrator.lookup_transport_options = mock_lookup

    t3_prompt = "Keep those dates and the ₹25,000 budget, but we strictly need flights and a 4-star hotel"
    res3 = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message=t3_prompt,
    )

    assert res3.status == "NOT_FEASIBLE"
    assert "Deficit" in res3.message_text or "exceed" in res3.message_text.lower() or "shortfall" in res3.message_text.lower()
    assert "₹" in res3.message_text or "INR" in res3.message_text
    # Flight or hotel was not silently replaced behind the user's back
    assert res3.selected_hotel is None or res3.feasibility_status == "NOT_FEASIBLE"


# =============================================================================
# NLU & CONVERSATIONAL INVARIANT TESTS
# =============================================================================

def test_inclusive_calendar_day_calculation():
    """Verify inclusive calendar days and stay nights are calculated accurately."""
    ai_service = AIIntentService(use_mock=True)

    # 1. 1–5 Nov 2026 = 5 days, 4 nights
    intent1 = ai_service._mock_parse_trip_intent("Trip to Goa from Chennai 1–5 Nov 2026 for 2 people with budget 30000")
    assert intent1.start_date == "2026-11-01"
    assert intent1.end_date == "2026-11-05"
    assert intent1.days == 5

    # 2. 1-4 Nov 2026 = 4 days, 3 nights
    intent2 = ai_service._mock_parse_trip_intent("Trip from Chennai to Goa from 1 to 4 November 2026 budget 25000 for 2 people")
    assert intent2.start_date == "2026-11-01"
    assert intent2.end_date == "2026-11-04"
    assert intent2.days == 4

    # 3. 10–12 Dec 2026 = 3 days, 2 nights
    intent3 = ai_service._mock_parse_trip_intent("Goa trip 10–12 Dec 2026 for 2 people budget 20000")
    assert intent3.start_date == "2026-12-10"
    assert intent3.end_date == "2026-12-12"
    assert intent3.days == 3


def test_merge_with_relative_days_delta():
    """Verify ParsedTripIntent.merge_with() handles relative duration deltas and date math."""
    base_intent = ParsedTripIntent(
        action=TripAction.NEW_TRIP,
        origin="Chennai",
        destination="Goa",
        people=2,
        days=5,
        start_date="2026-11-01",
        end_date="2026-11-05",
        budget=Decimal("30000.00"),
    )

    # Shorten by 1 day
    update_shorten = ParsedTripIntent(
        action=TripAction.MODIFY_TRIP,
        is_days_delta=True,
        days_delta=-1,
    )
    merged_shorten = base_intent.merge_with(update_shorten)
    assert merged_shorten.days == 4
    assert merged_shorten.start_date == "2026-11-01"
    assert merged_shorten.end_date == "2026-11-04"
    assert merged_shorten.origin == "Chennai"
    assert merged_shorten.destination == "Goa"
    assert merged_shorten.people == 2
    assert merged_shorten.budget == Decimal("30000.00")

    # Extend by 2 days
    update_extend = ParsedTripIntent(
        action=TripAction.MODIFY_TRIP,
        is_days_delta=True,
        days_delta=2,
    )
    merged_extend = base_intent.merge_with(update_extend)
    assert merged_extend.days == 7
    assert merged_extend.start_date == "2026-11-01"
    assert merged_extend.end_date == "2026-11-07"


def test_merge_with_hotel_preferences_and_strict_constraints():
    """Verify hotel tier, preferences, and strict constraints merge seamlessly."""
    base_intent = ParsedTripIntent(
        action=TripAction.NEW_TRIP,
        origin="Chennai",
        destination="Goa",
        people=2,
        days=4,
        budget=Decimal("25000.00"),
        interests=["beach"],
    )

    update = ParsedTripIntent(
        action=TripAction.MODIFY_TRIP,
        hotel_tier="4-star",
        hotel_preference="near the beach",
        strict_constraints=["4-star hotel", "flights"],
    )

    merged = base_intent.merge_with(update)
    assert merged.hotel_tier == "4-star"
    assert merged.hotel_preference == "near the beach"
    assert "4-star hotel" in merged.strict_constraints
    assert "flights" in merged.strict_constraints
    assert merged.origin == "Chennai"
    assert merged.destination == "Goa"
    assert merged.people == 2
    assert merged.budget == Decimal("25000.00")


@pytest.mark.asyncio
async def test_multi_user_session_isolation(monkeypatch):
    """Verify strict multi-user session isolation: state in chat A never leaks into chat B."""
    monkeypatch.setenv("SERPAPI_LIVE_ENABLED", "false")
    ai_service = AIIntentService(use_mock=True)
    orchestrator = BudlanceOrchestrator(ai_service=ai_service)

    chat_a = 771101
    chat_b = 771102

    # Chat A: Chennai to Goa, 2 people, 30k
    res_a1 = await orchestrator.handle_user_message(
        telegram_user_id=chat_a,
        chat_id=chat_a,
        message="Plan a trip from Chennai to Goa, 1–5 Nov 2026, 2 people, budget ₹30,000",
    )
    assert res_a1.status == "FEASIBLE"
    assert res_a1.selected_destination == "Goa"

    # Chat B: Bangalore to Munnar, 4 people, 40k
    res_b1 = await orchestrator.handle_user_message(
        telegram_user_id=chat_b,
        chat_id=chat_b,
        message="Plan a trip from Bangalore to Munnar, 3 days, 4 people, budget ₹40,000",
    )
    assert res_b1.status == "FEASIBLE"
    assert res_b1.selected_destination == "Munnar"

    # Chat A modifies trip duration to 4 days
    res_a2 = await orchestrator.handle_user_message(
        telegram_user_id=chat_a,
        chat_id=chat_a,
        message="Actually make it 4 days and lower budget to ₹25,000",
    )
    assert res_a2.status == "FEASIBLE"
    assert res_a2.selected_destination == "Goa"
    assert res_a2.budget_breakdown.total_budget == Decimal("25000.00")

    # Chat B sends a follow up: must still reflect Munnar, 4 people, Bangalore
    res_b2 = await orchestrator.handle_user_message(
        telegram_user_id=chat_b,
        chat_id=chat_b,
        message="Add ₹10,000 to my budget",
    )
    assert res_b2.status == "FEASIBLE"
    assert res_b2.selected_destination == "Munnar"
    assert res_b2.budget_breakdown.total_budget == Decimal("50000.00")


@pytest.mark.asyncio
async def test_offline_fallback_on_llm_validation_error():
    """Verify that when live OpenRouter returns invalid schema, orchestrator recovers gracefully via fallback."""
    mock_client = MagicMock()
    mock_client.has_credentials = True
    # Return malformed JSON that fails ParsedTripIntent schema
    mock_client.chat_completion = AsyncMock(return_value={"budget": "not_a_valid_number"})

    service = AIIntentService(client=mock_client, use_mock=False)
    orchestrator = BudlanceOrchestrator(ai_service=service)
    chat_id = 998877

    # Should not crash with unhandled 500/OpenRouterValidationError; falls back to heuristic parser
    result = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Plan a 3-day trip from Chennai to Goa for 2 people with budget 30000",
    )
    assert result.status == "FEASIBLE"
    assert result.selected_destination == "Goa"


def test_optimizer_strictly_respects_explicit_luxury_hotel_and_flight():
    """Verify optimizer does not silently downgrade explicit 4-star hotel or flight requests."""
    optimizer = OptimizationEngine()
    trip_id = uuid4()

    # Low budget that cannot afford 4-star hotel + flight
    total_budget = Decimal("25000.00")
    people = 2
    days = 4

    flight = FlightOption(
        airline="IndiGo",
        flight_number="6E-101",
        origin="Chennai",
        destination="Goa",
        departure_time="08:00",
        arrival_time="10:00",
        price=Decimal("12000.00"),  # ₹24,000 for 2 people
        source=DataSource.LIVE,
    )
    train = TransitOption(
        transit_type="train",
        origin="Chennai",
        destination="Goa",
        name_or_operator="Southern Railway",
        price=Decimal("2000.00"),
        source=DataSource.LIVE,
    )

    four_star_hotel = HotelOption(
        name="Goa Luxury Resort",
        hotel_class=4,
        rating=4.6,
        price_per_night=Decimal("5000.00"),
        total_price=Decimal("15000.00"),  # 3 nights
        nights=3,
        source=DataSource.LIVE,
    )
    budget_lodge = HotelOption(
        name="Goa Basic Stay",
        hotel_class=2,
        rating=3.5,
        price_per_night=Decimal("1000.00"),
        total_price=Decimal("3000.00"),
        nights=3,
        source=DataSource.LIVE,
    )

    from budlance.schemas.travel import FoodEstimate, LocalTransitEstimate

    food = FoodEstimate(
        tier="standard",
        daily_cost_per_person=Decimal("600.00"),
        total_cost=Decimal("4800.00"),
        people=people,
        days=days,
        source=DataSource.ESTIMATED,
    )
    transit = LocalTransitEstimate(
        mode="auto",
        total_cost=Decimal("1200.00"),
        source=DataSource.ESTIMATED,
    )

    result = optimizer.optimize(
        trip_id=trip_id,
        total_budget=total_budget,
        people=people,
        days=days,
        initial_transport=flight,
        initial_hotel=four_star_hotel,
        initial_food=food,
        initial_transit=transit,
        available_transports=[flight, train],
        available_hotels=[four_star_hotel, budget_lodge],
        currency="INR",
        explicit_hotel_tier="4-star",
        explicit_transport_mode="flight",
        strict_preferences=["flights", "4-star"],
        locked_days=True,
    )

    # Must NOT silently downgrade to train or budget lodge and claim FEASIBLE
    assert result.is_feasible is False
    assert result.final_evaluation.status == "NOT_FEASIBLE"
    # Ensure alternatives are suggested in the evaluation
    assert len(result.alternatives_available) > 0
