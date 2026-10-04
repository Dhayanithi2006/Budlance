"""Focused tests for Task 2 — TravelParty schema, validation, and conversation state persistence."""

from decimal import Decimal
from typing import get_args
import pytest

from budlance.ai.schemas import ParsedTripIntent, TravelParty, TripAction
from budlance.db.repositories.conversation_repo import ConversationStateRepository


class TestTravelPartySchema:
    """Validate TravelParty type definition and ParsedTripIntent schema rules."""

    def test_travel_party_literal_values(self):
        """TravelParty must be Literal['solo', 'couple', 'friends', 'family', 'relatives']."""
        expected_parties = {"solo", "couple", "friends", "family", "relatives"}
        actual_parties = set(get_args(TravelParty))
        assert actual_parties == expected_parties

    def test_travel_party_defaults_to_none(self):
        """travel_party must default to None when unstated."""
        intent = ParsedTripIntent()
        assert intent.travel_party is None

    def test_people_2_does_not_imply_couple(self):
        """people=2 alone must NOT set or imply travel_party='couple'."""
        intent = ParsedTripIntent(people=2)
        assert intent.people == 2
        assert intent.travel_party is None

    @pytest.mark.parametrize("party", ["solo", "couple", "friends", "family", "relatives"])
    def test_valid_travel_party_values_accepted(self, party: str):
        """Explicit travel_party values must validate cleanly."""
        intent = ParsedTripIntent(travel_party=party)
        assert intent.travel_party == party

    def test_invalid_travel_party_rejected(self):
        """Invalid travel_party string must raise validation error."""
        with pytest.raises(Exception):
            ParsedTripIntent(travel_party="luxury_business")


class TestTravelPartyStatePersistence:
    """Validate persistence and retrieval of travel_party through ConversationStateRepository."""

    def test_conversation_repo_persists_travel_party(self):
        """ConversationStateRepository must roundtrip travel_party."""
        repo = ConversationStateRepository(client=None)  # In-memory store
        chat_id = 42001

        original = ParsedTripIntent(
            action=TripAction.NEW_TRIP,
            budget=Decimal("35000"),
            people=2,
            days=4,
            origin="Chennai",
            destination="Gujarat",
            interests=["heritage", "food"],
            travel_party="couple",
        )

        repo.save_pending_intent(chat_id, original)
        loaded = repo.get_pending_intent(chat_id)

        assert loaded is not None
        assert loaded.travel_party == "couple"
        assert loaded.budget == Decimal("35000")
        assert loaded.people == 2
        assert loaded.days == 4
        assert loaded.origin == "Chennai"
        assert loaded.destination == "Gujarat"
        assert loaded.interests == ["heritage", "food"]

    def test_conversation_repo_persists_none_travel_party(self):
        """When travel_party is None, it persists and loads as None."""
        repo = ConversationStateRepository(client=None)
        chat_id = 42002

        original = ParsedTripIntent(
            action=TripAction.NEW_TRIP,
            budget=Decimal("15000"),
            people=2,  # 2 people, but no explicit relationship
            origin="Mumbai",
            travel_party=None,
        )

        repo.save_pending_intent(chat_id, original)
        loaded = repo.get_pending_intent(chat_id)

        assert loaded is not None
        assert loaded.people == 2
        assert loaded.travel_party is None


class TestTravelPartyMergeLogic:
    """Validate merge_with and apply_change_action preserve travel_party."""

    def test_merge_with_preserves_existing_travel_party_when_update_none(self):
        """When follow-up provides missing days, travel_party is preserved."""
        base = ParsedTripIntent(
            action=TripAction.NEW_TRIP,
            budget=Decimal("50000"),
            people=2,
            days=None,
            origin="Chennai",
            destination="Gujarat",
            travel_party="couple",
            interests=["heritage"],
        )
        update = ParsedTripIntent(
            action=TripAction.NEW_TRIP,
            days=3,
            travel_party=None,
        )
        merged = base.merge_with(update)

        assert merged.days == 3
        assert merged.budget == Decimal("50000")
        assert merged.people == 2
        assert merged.origin == "Chennai"
        assert merged.destination == "Gujarat"
        assert merged.travel_party == "couple"
        assert merged.interests == ["heritage"]

    def test_merge_with_updates_travel_party_when_explicitly_provided(self):
        """When update provides a new travel_party, it overwrites the existing one."""
        base = ParsedTripIntent(
            action=TripAction.NEW_TRIP,
            travel_party="couple",
            budget=Decimal("20000"),
        )
        update = ParsedTripIntent(
            action=TripAction.NEW_TRIP,
            travel_party="friends",
        )
        merged = base.merge_with(update)
        assert merged.travel_party == "friends"
        assert merged.budget == Decimal("20000")

    def test_apply_change_action_preserves_travel_party(self):
        """CHANGE_DAYS or CHANGE_BUDGET must not reset travel_party."""
        base = ParsedTripIntent(
            action=TripAction.NEW_TRIP,
            budget=Decimal("40000"),
            people=2,
            days=5,
            origin="Delhi",
            travel_party="family",
        )
        days_update = ParsedTripIntent(action=TripAction.CHANGE_DAYS, days=4)
        result = base.apply_change_action(days_update)

        assert result.days == 4
        assert result.travel_party == "family"
        assert result.budget == Decimal("40000")
        assert result.people == 2
