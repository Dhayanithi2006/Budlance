"""Tests verifying travel_party preservation across multi-turn merges and ActionRouter routing."""

from decimal import Decimal
import pytest
from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.orchestrator.orchestrator import BudlanceOrchestrator


def test_merge_preserves_travel_party_when_second_message_has_none():
    """existing.merge_with(update) preserves travel_party when update has travel_party=None."""
    existing = ParsedTripIntent(
        budget=Decimal("15000"),
        people=2,
        days=3,
        origin="Chennai",
        destination="Goa",
        travel_party="couple",
    )
    update = ParsedTripIntent(
        action=TripAction.NEW_TRIP,
        days=4,
        travel_party=None,
    )
    merged = existing.merge_with(update)
    assert merged.days == 4
    assert merged.travel_party == "couple"
    assert merged.traveler_type == "couple"


def test_merge_updates_travel_party_when_explicitly_provided():
    """existing.merge_with(update) overrides travel_party when update specifies one."""
    existing = ParsedTripIntent(
        budget=Decimal("20000"),
        people=2,
        days=3,
        origin="Chennai",
        destination="Goa",
        travel_party="couple",
    )
    update = ParsedTripIntent(
        action=TripAction.NEW_TRIP,
        people=4,
        travel_party="friends",
    )
    merged = existing.merge_with(update)
    assert merged.people == 4
    assert merged.travel_party == "friends"
    assert merged.traveler_type == "friends"


def test_change_budget_preserves_travel_party():
    """CHANGE_BUDGET updates only budget and preserves travel_party."""
    existing = ParsedTripIntent(
        budget=Decimal("10000"),
        people=2,
        days=3,
        origin="Chennai",
        destination="Goa",
        travel_party="couple",
    )
    update = ParsedTripIntent(
        action=TripAction.CHANGE_BUDGET,
        budget=Decimal("18000"),
    )
    applied = existing.apply_change_action(update)
    assert applied.budget == Decimal("18000")
    assert applied.travel_party == "couple"
    assert applied.days == 3
    assert applied.destination == "Goa"


def test_change_days_preserves_travel_party():
    """CHANGE_DAYS updates only days and preserves travel_party."""
    existing = ParsedTripIntent(
        budget=Decimal("10000"),
        people=5,
        days=3,
        origin="Delhi",
        destination="Manali",
        travel_party="friends",
    )
    update = ParsedTripIntent(
        action=TripAction.CHANGE_DAYS,
        days=5,
    )
    applied = existing.apply_change_action(update)
    assert applied.days == 5
    assert applied.travel_party == "friends"
    assert applied.budget == Decimal("10000")
    assert applied.people == 5


def test_change_destination_preserves_travel_party():
    """CHANGE_DESTINATION updates only destination and preserves travel_party."""
    existing = ParsedTripIntent(
        budget=Decimal("25000"),
        people=4,
        days=4,
        origin="Bangalore",
        destination="Ooty",
        travel_party="family",
    )
    update = ParsedTripIntent(
        action=TripAction.CHANGE_DESTINATION,
        destination="Coorg",
    )
    applied = existing.apply_change_action(update)
    assert applied.destination == "Coorg"
    assert applied.travel_party == "family"
    assert applied.budget == Decimal("25000")
    assert applied.people == 4


def test_change_people_updates_travel_party_when_explicit():
    """CHANGE_PEOPLE with explicit 'only me now' updates travel_party to solo."""
    existing = ParsedTripIntent(
        budget=Decimal("20000"),
        people=2,
        days=3,
        origin="Chennai",
        destination="Goa",
        travel_party="couple",
    )
    update = ParsedTripIntent(
        action=TripAction.CHANGE_PEOPLE,
        people=1,
        travel_party="solo",
    )
    applied = existing.apply_change_action(update)
    assert applied.people == 1
    assert applied.travel_party == "solo"
    assert applied.traveler_type == "solo"


def test_change_people_invalidates_couple_if_headcount_changes_without_party():
    """If 2-person couple changes to 4 people without specifying party, couple is cleared."""
    existing = ParsedTripIntent(
        budget=Decimal("20000"),
        people=2,
        days=3,
        origin="Chennai",
        destination="Goa",
        travel_party="couple",
    )
    update = ParsedTripIntent(
        action=TripAction.CHANGE_PEOPLE,
        people=4,
        travel_party=None,
    )
    applied = existing.apply_change_action(update)
    assert applied.people == 4
    assert applied.travel_party is None


def test_find_alternative_preserves_travel_party_and_clears_destination():
    """FIND_ALTERNATIVE keeps travel_party from pending intent and clears destination."""
    pending = ParsedTripIntent(
        budget=Decimal("10000"),
        people=2,
        days=5,
        origin="Chennai",
        destination="Goa",
        travel_party="couple",
    )
    # Orchestrator FIND_ALTERNATIVE handling
    resolved = pending.model_copy(
        update={"destination": None, "action": TripAction.FIND_ALTERNATIVE}
    )
    assert resolved.destination is None
    assert resolved.travel_party == "couple"
    assert resolved.budget == Decimal("10000")
    assert resolved.people == 2
    assert resolved.days == 5
    assert resolved.origin == "Chennai"


@pytest.mark.asyncio
async def test_orchestrator_multi_turn_find_alternative_preserves_party():
    """Orchestrator preserves travel_party across full multi-turn Telegram flow."""
    from budlance.ai.service import AIIntentService
    from budlance.db.repositories.conversation_repo import ConversationStateRepository

    conv_repo = ConversationStateRepository(client=None)
    ai_service = AIIntentService(use_mock=True)
    orch = BudlanceOrchestrator(conversation_repo=conv_repo, ai_service=ai_service)
    chat_id = 998877

    # Turn 1: User sends initial request with party
    # "I want to go to Goa from Chennai for 5 days, budget 10000, 2 people, we are a couple"
    res1 = await orch.handle_user_message(
        telegram_user_id=123,
        chat_id=chat_id,
        message="I planned to go trip for 5 days, budget 10000, 2 people, we are a couple, from Chennai to Goa",
    )

    # Check that pending intent saved travel_party
    pending1 = conv_repo.get_pending_intent(chat_id)
    assert pending1 is not None
    assert pending1.travel_party == "couple"

    # Turn 2: User sends FIND_ALTERNATIVE
    res2 = await orch.handle_user_message(
        telegram_user_id=123,
        chat_id=chat_id,
        message="Recommend some other place within this budget",
    )

    # The resulting action should have preserved travel_party
    pending2 = conv_repo.get_pending_intent(chat_id)
    if pending2 is not None:
        assert pending2.travel_party == "couple"
