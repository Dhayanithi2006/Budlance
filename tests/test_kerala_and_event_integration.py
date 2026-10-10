"""Tests for Kerala trip planning flow, missing slot clarification, and event integration."""

import pytest
from decimal import Decimal
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.db.repositories.conversation_repo import ConversationStateRepository
from budlance.db.repositories.trip_repo import TripRepository
from budlance.db.repositories.user_repo import UserRepository
from budlance.db.repositories.itinerary_repo import ItineraryRepository
from budlance.db.repositories.ledger_repo import LedgerRepository
from budlance.db.repositories.rescue_repo import RescueRepository
from budlance.db.repositories.intent_repo import IntentRepository
from budlance.db.repositories.trip_pass_repo import TripPassRepository
from budlance.ai.service import AIIntentService
from budlance.ai.schemas import ParsedTripIntent


@pytest.fixture
def orchestrator():
    """Build isolated orchestrator using in-memory repositories."""
    return BudlanceOrchestrator(
        user_repo=UserRepository(client=None),
        trip_repo=TripRepository(client=None),
        intent_repo=IntentRepository(client=None),
        itinerary_repo=ItineraryRepository(client=None),
        ledger_repo=LedgerRepository(client=None),
        rescue_repo=RescueRepository(client=None),
        conversation_repo=ConversationStateRepository(client=None),
        trip_pass_repo=TripPassRepository(client=None),
        ai_service=AIIntentService(use_mock=True),
    )


@pytest.mark.asyncio
async def test_new_trip_greeting_clears_stale_context(orchestrator):
    """'Let me plan new trip' should prompt welcome and not evaluate any stale trip."""
    chat_id = 12345
    # Seed an old pending intent that would be infeasible
    orchestrator.conversation_repo.save_pending_intent(
        chat_id,
        ParsedTripIntent(destination="Hosur", budget=Decimal("5000"), people=1, days=2, origin="Chennai")
    )

    res = await orchestrator.handle_user_message(
        telegram_user_id=12345,
        chat_id=chat_id,
        message="Let me plan new trip"
    )
    assert res.status == "CLARIFICATION"
    assert "Welcome to Budlance" in res.message_text
    assert "Hosur" not in res.message_text
    assert "Not Feasible" not in res.message_text


@pytest.mark.asyncio
async def test_kerala_missing_days_asks_for_clarification(orchestrator):
    """When budget and people are given but days is omitted, orchestrator must ask for days."""
    chat_id = 12346
    res = await orchestrator.handle_user_message(
        telegram_user_id=12346,
        chat_id=chat_id,
        message="I would like to explore kerala,budget 50000, 3 person, friends from Chennai"
    )
    assert res.status == "CLARIFICATION"
    assert "How many days" in res.message_text or "duration" in res.message_text.lower()
    assert "₹50,000" in res.message_text
    assert "3 travelers" in res.message_text


@pytest.mark.asyncio
async def test_kerala_full_feasible_flow_with_events_and_addition(orchestrator):
    """Complete 3-step conversation: partial details -> days -> add event."""
    chat_id = 12347

    # Step 1: Provide details without days
    r1 = await orchestrator.handle_user_message(
        telegram_user_id=12347,
        chat_id=chat_id,
        message="I would like to explore kerala,budget 50000, 3 person, friends from Chennai"
    )
    assert r1.status == "CLARIFICATION"

    # Step 2: Provide duration
    r2 = await orchestrator.handle_user_message(
        telegram_user_id=12347,
        chat_id=chat_id,
        message="3 days"
    )
    assert r2.status == "FEASIBLE"
    assert "Budlance Trip Plan: Kerala" in r2.message_text
    assert "Fort Kochi" in r2.message_text or "Alleppey" in r2.message_text
    assert "Special Event Alert" in r2.message_text
    assert "Kochi-Muziris Biennale" in r2.message_text

    # Step 3: Add event
    r3 = await orchestrator.handle_user_message(
        telegram_user_id=12347,
        chat_id=chat_id,
        message="Yes, add the event"
    )
    assert r3.status == "FEASIBLE"
    assert "Event Added to Your Itinerary" in r3.message_text
    assert "Kochi-Muziris Biennale" in r3.message_text
    assert "Day 2" in r3.message_text

    # Verify Day 2 schedule contains the added event
    itin_rec = orchestrator.itinerary_repo.get_itinerary(r3.trip_id)
    day_2_items = itin_rec.days[1].get("items", [])
    assert any("Biennale" in it.get("activity", "") for it in day_2_items)
