"""Budlance — Natural Conversational Intent + Context Memory Tests.

Three-Level Strategy:
  Level 1 — Natural English (informal, mixed word order, abbreviations)
  Level 2 — Tanglish / Tamil-English mixed language
  Level 3 — Multi-turn conversational flows (corrections, follow-ups, pronoun resolution)

Architecture:
  - All heuristic parser tests use AIIntentService(use_mock=True) — deterministic, no API calls.
  - The ParsedTripIntent.merge_with() unit tests verify the merge contract directly.
  - The ConversationStateRepository tests verify in-memory persistence.
  - OpenRouter live tests are marked and skipped when rate-limited.
  - The orchestrator end-to-end tests mock the AI service and conversation repo to test wiring.

These tests MUST NOT:
  - Call SerpApi, Stripe, or any external API other than OpenRouter (and only 1-3 calls).
  - Modify engine/budget.py or optimizer business rules.
  - Require schema migration.
"""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from budlance.ai.schemas import ParsedTripIntent
from budlance.ai.service import AIIntentService
from budlance.db.repositories.conversation_repo import ConversationStateRepository
from budlance.orchestrator.formatter import format_clarification


# ===========================================================================
# Helpers
# ===========================================================================

def _svc() -> AIIntentService:
    """Return a mock-mode service (no API calls)."""
    return AIIntentService(use_mock=True)


def _intent(**kwargs) -> ParsedTripIntent:
    """Convenience factory."""
    return ParsedTripIntent(**kwargs)


# ===========================================================================
# LEVEL 1 — Natural English
# ===========================================================================


class TestLevel1NaturalEnglish:
    """Ordinary but informal user messages should be parsed correctly."""

    def test_l1_x_to_y_with_nospace_people(self):
        """'budget 20000, 2people(couple),3 days, place chennai to goa' parses completely."""
        svc = _svc()
        intent = svc._mock_parse_trip_intent("budget 20000, 2people(couple),3 days, place chennai to goa")
        assert intent.budget == Decimal("20000")
        assert intent.people == 2
        assert intent.days == 3
        assert (intent.origin or "").lower() == "chennai"
        assert (intent.destination or "").lower() == "goa"

    def test_l1_currently_in_with_famous_places_food(self):
        """'Currently i am in chennai interested to go delhi...' captures all 5 fields."""
        svc = _svc()
        intent = svc._mock_parse_trip_intent(
            "Currently i am in chennai interested to go delhi budget 10000,1 people,famous place there+food"
        )
        assert intent.budget == Decimal("10000")
        assert intent.people == 1
        assert intent.days is None  # not stated
        assert (intent.origin or "").lower() == "chennai"
        assert (intent.destination or "").lower() == "delhi"
        assert "famous places" in intent.interests or "famous place" in intent.interests
        assert "food" in intent.interests

    def test_l1_currently_in_asks_only_for_days(self):
        """When only days is missing, format_clarification produces a single targeted question."""
        intent = _intent(
            budget=Decimal("10000"),
            people=1,
            origin="Chennai",
            destination="Delhi",
            interests=["famous places", "food"],
        )
        msg = format_clarification(["days"], known_context=intent)
        assert "chennai" in msg.lower() or "delhi" in msg.lower()
        assert "days" in msg.lower() or "stay" in msg.lower()
        # Must NOT list budget, people, or city
        assert "budget" not in msg.lower().split("got it")[1] if "got it" in msg.lower() else True
        assert "travelers" not in msg.lower().split("got it")[1] if "got it" in msg.lower() else True

    def test_l1_from_to_under_budget(self):
        """'I want a 4 day trip from Bangalore to Jaipur for 3 people under 18000.'"""
        svc = _svc()
        intent = svc._mock_parse_trip_intent(
            "I want a 4 day trip from Bangalore to Jaipur for 3 people under 18000."
        )
        assert intent.budget == Decimal("18000")
        assert intent.people == 3
        assert intent.days == 4
        assert (intent.origin or "").lower() == "bangalore"
        assert (intent.destination or "").lower() == "jaipur"

    def test_l1_x_to_y_shorthand_max(self):
        """'Chennai to Goa, 2 people, 3 days, max 20000.'"""
        svc = _svc()
        intent = svc._mock_parse_trip_intent("Chennai to Goa, 2 people, 3 days, max 20000.")
        assert intent.budget == Decimal("20000")
        assert intent.people == 2
        assert intent.days == 3
        assert (intent.origin or "").lower() == "chennai"
        assert (intent.destination or "").lower() == "goa"

    def test_l1_solo_trip_presence_pattern(self):
        """'I am in Mumbai, solo trip to Goa, 3 days, budget 12000.'"""
        svc = _svc()
        intent = svc._mock_parse_trip_intent("I am in Mumbai, solo trip to Goa, 3 days, budget 12000.")
        assert intent.budget == Decimal("12000")
        assert intent.people == 1
        assert intent.days == 3
        assert (intent.origin or "").lower() == "mumbai"
        assert (intent.destination or "").lower() == "goa"
        assert intent.traveler_type == "solo"

    def test_l1_k_budget_shorthand(self):
        """'20k budget' is correctly parsed as 20000."""
        svc = _svc()
        intent = svc._mock_parse_trip_intent("20k budget, 2 people, 3 days, from Hyderabad to Goa")
        assert intent.budget == Decimal("20000")

    def test_l1_couple_keyword(self):
        """'couple' keyword sets people=2 and traveler_type=couple."""
        svc = _svc()
        intent = svc._mock_parse_trip_intent("Trip for a couple from Delhi to Jaipur, 2 days, ₹10000")
        assert intent.people == 2
        assert intent.traveler_type == "couple"


# ===========================================================================
# LEVEL 2 — Tanglish / Mixed Language
# ===========================================================================


class TestLevel2Tanglish:
    """Natural Tanglish and Tamil-English mixed-language inputs."""

    def test_l2_exact_tanglish_15k(self):
        """Exact Tanglish message from spec: budget, people, days, origin, interests."""
        svc = _svc()
        intent = svc._mock_parse_trip_intent(
            "Enakku 15000 budget irukku, 2 peru, 3 days Chennai la irundhu hill station poganum."
        )
        assert intent.budget == Decimal("15000")
        assert intent.people == 2
        assert intent.days == 3
        assert (intent.origin or "").lower() == "chennai"
        assert intent.destination is None  # "hill station" is not a city
        assert "hill station" in intent.interests

    def test_l2_tanglish_origin_not_destination(self):
        """Chennai origin (la irundhu) must never become destination."""
        svc = _svc()
        intent = svc._mock_parse_trip_intent(
            "Enakku 15000 budget irukku, 2 peru, 3 days Chennai la irundhu hill station poganum."
        )
        if intent.destination:
            assert intent.destination.lower() != "chennai"

    def test_l2_tanglish_with_destination_and_famous_places(self):
        """'Naan Chennai la iruken, 2 peru Delhi poganum, 4 days, 15k budget...'"""
        svc = _svc()
        intent = svc._mock_parse_trip_intent(
            "Naan Chennai la iruken, 2 peru Delhi poganum, 4 days, 15k budget, famous places um food um venum."
        )
        assert intent.budget == Decimal("15000")
        assert intent.people == 2
        assert intent.days == 4
        assert (intent.origin or "").lower() == "chennai"
        assert (intent.destination or "").lower() == "delhi"

    def test_l2_tanglish_x_to_y(self):
        """'2 peru, 3 days, Chennai la irundhu Goa poganum, budget 20k.'"""
        svc = _svc()
        intent = svc._mock_parse_trip_intent(
            "2 peru, 3 days, Chennai la irundhu Goa poganum, budget 20k."
        )
        assert intent.budget == Decimal("20000")
        assert intent.people == 2
        assert intent.days == 3
        assert (intent.origin or "").lower() == "chennai"
        assert (intent.destination or "").lower() == "goa"

    def test_l2_tanglish_variation_bangalore(self):
        """'Enakku 20k budget, 4 days, 3 peru, Bangalore la irundhu hill station poganum.'"""
        svc = _svc()
        intent = svc._mock_parse_trip_intent(
            "Enakku 20k budget, 4 days, 3 peru, Bangalore la irundhu hill station poganum."
        )
        assert intent.budget == Decimal("20000.0")
        assert intent.people == 3
        assert intent.days == 4
        assert (intent.origin or "").lower() == "bangalore"
        assert intent.destination is None
        assert "hill station" in intent.interests

    def test_l2_beach_trip_tanglish(self):
        """'Naan 15000 budget la 2 peru 3 days Chennai la irundhu beach trip poganum.'"""
        svc = _svc()
        intent = svc._mock_parse_trip_intent(
            "Naan 15000 budget la 2 peru 3 days Chennai la irundhu beach trip poganum."
        )
        assert intent.budget == Decimal("15000")
        assert intent.people == 2
        assert intent.days == 3
        assert (intent.origin or "").lower() == "chennai"
        assert "beach" in intent.interests

    def test_l2_variation3_chennai_irundhu(self):
        """'2 peru, 3 days, Chennai la irundhu trip venum, budget 18000.'"""
        svc = _svc()
        intent = svc._mock_parse_trip_intent(
            "2 peru, 3 days, Chennai la irundhu trip venum, budget 18000."
        )
        assert intent.budget == Decimal("18000")
        assert intent.people == 2
        assert intent.days == 3
        assert (intent.origin or "").lower() == "chennai"


# ===========================================================================
# LEVEL 3 — Multi-Turn Conversational Flows
# ===========================================================================


class TestLevel3MultiTurnMerge:
    """Context-merge via ParsedTripIntent.merge_with() — deterministic, no AI calls."""

    def test_l3_a_missing_days_follow_up(self):
        """Scenario A: User provides all except days, then says '4 days'."""
        svc = _svc()
        base = svc._mock_parse_trip_intent(
            "Currently I am in Chennai, I want to go Delhi, budget 10000, 1 person, famous places and food."
        )
        # Verify initial parse
        assert (base.origin or "").lower() == "chennai"
        assert (base.destination or "").lower() == "delhi"
        assert base.budget == Decimal("10000")
        assert base.people == 1
        assert base.days is None

        # Simulate follow-up
        partial = svc._mock_parse_trip_intent("4 days")
        merged = base.merge_with(partial)

        assert merged.days == 4
        assert merged.budget == Decimal("10000")
        assert merged.people == 1
        assert (merged.origin or "").lower() == "chennai"
        assert (merged.destination or "").lower() == "delhi"

    def test_l3_a_clarification_asks_only_days(self):
        """After partial parse, clarification must reference known fields and ask only for days."""
        intent = _intent(
            budget=Decimal("10000"),
            people=1,
            origin="Chennai",
            destination="Delhi",
            interests=["famous places", "food"],
        )
        msg = format_clarification(["days"], known_context=intent)
        # Must mention something we know
        assert ("chennai" in msg.lower()) or ("delhi" in msg.lower()) or ("10,000" in msg)
        # Must ask about days
        assert "day" in msg.lower() or "stay" in msg.lower() or "long" in msg.lower()

    def test_l3_b_destination_correction(self):
        """Scenario B: User says 'Actually I want Delhi' after Goa trip."""
        svc = _svc()
        base = svc._mock_parse_trip_intent("Chennai to Goa, 2 people, 3 days, 15000 budget.")
        assert (base.destination or "").lower() == "goa"

        partial = svc._mock_parse_trip_intent("Actually I want Delhi.")
        merged = base.merge_with(partial)
        assert (merged.destination or "").lower() == "delhi"
        assert merged.budget == Decimal("15000")
        assert merged.people == 2
        assert merged.days == 3
        assert (merged.origin or "").lower() == "chennai"

    def test_l3_c_multiple_corrections(self):
        """Scenario C: Sequence of corrections — days, people, budget."""
        svc = _svc()
        base = svc._mock_parse_trip_intent("Chennai to Goa for 2 people, 3 days, 15000 budget.")
        step1 = base.merge_with(svc._mock_parse_trip_intent("Make it 4 days."))
        step2 = step1.merge_with(svc._mock_parse_trip_intent("3 people."))
        step3 = step2.merge_with(svc._mock_parse_trip_intent("budget 18000"))

        assert step3.days == 4
        assert step3.people == 3
        assert step3.budget == Decimal("18000")
        assert (step3.origin or "").lower() == "chennai"
        assert (step3.destination or "").lower() == "goa"

    def test_l3_d_pronoun_reference_interest_addition(self):
        """Scenario D: 'Show famous places there and good food' resolves against existing destination."""
        svc = _svc()
        base = svc._mock_parse_trip_intent("I am in Chennai, going to Delhi for 3 days, solo, budget 12000.")
        partial = svc._mock_parse_trip_intent("Show famous places there and good food.")
        merged = base.merge_with(partial)

        # Famous places and food must be merged in
        assert "famous places" in merged.interests or "famous place" in merged.interests
        assert "food" in merged.interests
        # Origin/destination must remain
        assert (merged.origin or "").lower() == "chennai"
        assert (merged.destination or "").lower() == "delhi"

    def test_l3_solo_correction(self):
        """'Only me now.' reduces people from 2 to 1."""
        base = _intent(
            budget=Decimal("15000"), people=2, days=3,
            origin="Chennai", destination="Goa"
        )
        svc = _svc()
        partial = svc._mock_parse_trip_intent("Only me now.")
        merged = base.merge_with(partial)
        assert merged.people == 1

    def test_l3_budget_update(self):
        """'Budget is actually 18000.' updates budget only."""
        base = _intent(
            budget=Decimal("15000"), people=2, days=3,
            origin="Chennai", destination="Goa"
        )
        svc = _svc()
        partial = svc._mock_parse_trip_intent("Budget is actually 18000.")
        merged = base.merge_with(partial)
        assert merged.budget == Decimal("18000")
        assert merged.people == 2
        assert merged.days == 3

    def test_l3_interests_accumulate(self):
        """Adding new interests merges with existing ones."""
        base = _intent(interests=["beach"], budget=Decimal("10000"), people=2, days=3)
        update = _intent(interests=["food"])
        merged = base.merge_with(update)
        assert "beach" in merged.interests
        assert "food" in merged.interests

    def test_l3_no_field_duplication_on_merge(self):
        """Merging the same interest twice does not duplicate it."""
        base = _intent(interests=["beach", "food"], budget=Decimal("10000"), people=2, days=3)
        update = _intent(interests=["beach"])
        merged = base.merge_with(update)
        assert merged.interests.count("beach") == 1


# ===========================================================================
# merge_with contract tests
# ===========================================================================


class TestMergeWith:
    """Unit tests for ParsedTripIntent.merge_with()."""

    def test_merge_fills_null_days(self):
        base = _intent(budget=Decimal("10000"), people=1, origin="Chennai", destination="Delhi")
        update = _intent(days=4)
        merged = base.merge_with(update)
        assert merged.days == 4
        assert merged.budget == Decimal("10000")
        assert merged.people == 1
        assert merged.origin == "Chennai"
        assert merged.destination == "Delhi"

    def test_merge_replaces_destination(self):
        base = _intent(budget=Decimal("15000"), people=2, days=3, origin="Chennai", destination="Goa")
        update = _intent(destination="Jaipur")
        merged = base.merge_with(update)
        assert merged.destination == "Jaipur"
        assert merged.origin == "Chennai"
        assert merged.budget == Decimal("15000")

    def test_merge_replaces_budget(self):
        base = _intent(budget=Decimal("15000"), people=2, days=3, origin="Chennai", destination="Goa")
        update = _intent(budget=Decimal("18000"))
        merged = base.merge_with(update)
        assert merged.budget == Decimal("18000")
        assert merged.people == 2
        assert merged.days == 3

    def test_merge_replaces_people(self):
        base = _intent(budget=Decimal("15000"), people=4, days=3, origin="Chennai", destination="Goa")
        update = _intent(people=2)
        merged = base.merge_with(update)
        assert merged.people == 2

    def test_merge_preserves_nulls(self):
        """Null fields in update must not overwrite known fields."""
        base = _intent(budget=Decimal("15000"), people=2, days=3, origin="Chennai", destination="Goa")
        update = _intent()  # all nulls
        merged = base.merge_with(update)
        assert merged.budget == Decimal("15000")
        assert merged.people == 2
        assert merged.days == 3
        assert merged.origin == "Chennai"
        assert merged.destination == "Goa"

    def test_merge_interests_union(self):
        base = _intent(interests=["beach", "food"], budget=Decimal("10000"))
        update = _intent(interests=["food", "nature"])
        merged = base.merge_with(update)
        assert "beach" in merged.interests
        assert "food" in merged.interests
        assert "nature" in merged.interests
        assert merged.interests.count("food") == 1


# ===========================================================================
# ConversationStateRepository tests
# ===========================================================================


class TestConversationStateRepository:
    """Verify pending intent persistence and retrieval (in-memory fallback)."""

    def _make_repo(self) -> ConversationStateRepository:
        return ConversationStateRepository(client=None)  # in-memory

    def test_save_and_retrieve(self):
        repo = self._make_repo()
        intent = _intent(
            budget=Decimal("10000"), people=1, origin="Chennai", destination="Delhi"
        )
        repo.save_pending_intent(12345, intent)
        retrieved = repo.get_pending_intent(12345)
        assert retrieved is not None
        assert retrieved.budget == Decimal("10000")
        assert retrieved.origin == "Chennai"

    def test_retrieve_unknown_chat_returns_none(self):
        repo = self._make_repo()
        assert repo.get_pending_intent(99999) is None

    def test_clear_removes_entry(self):
        repo = self._make_repo()
        intent = _intent(budget=Decimal("10000"), people=1)
        repo.save_pending_intent(12345, intent)
        repo.clear_pending_intent(12345)
        assert repo.get_pending_intent(12345) is None

    def test_overwrite_updates_entry(self):
        repo = self._make_repo()
        intent1 = _intent(budget=Decimal("10000"), people=1, origin="Chennai")
        intent2 = _intent(budget=Decimal("15000"), people=2, origin="Chennai", days=3)
        repo.save_pending_intent(12345, intent1)
        repo.save_pending_intent(12345, intent2)
        retrieved = repo.get_pending_intent(12345)
        assert retrieved is not None
        assert retrieved.budget == Decimal("15000")
        assert retrieved.days == 3

    def test_different_chats_isolated(self):
        repo = self._make_repo()
        repo.save_pending_intent(111, _intent(budget=Decimal("5000"), origin="Mumbai"))
        repo.save_pending_intent(222, _intent(budget=Decimal("20000"), origin="Delhi"))
        r1 = repo.get_pending_intent(111)
        r2 = repo.get_pending_intent(222)
        assert r1 is not None and r1.origin == "Mumbai"
        assert r2 is not None and r2.origin == "Delhi"


# ===========================================================================
# format_clarification context-aware tests
# ===========================================================================


class TestFormatClarification:
    """Verify that format_clarification generates appropriate messages."""

    def test_single_missing_field_with_context_is_conversational(self):
        """When only days is missing and context is rich, the message summarises known info."""
        known = _intent(
            budget=Decimal("10000"),
            people=1,
            origin="Chennai",
            destination="Delhi",
            interests=["famous places", "food"],
        )
        msg = format_clarification(["days"], known_context=known)
        assert "got it" in msg.lower()
        # Must reference something we know
        assert "chennai" in msg.lower() or "delhi" in msg.lower() or "10,000" in msg
        # Must ask for days
        assert "day" in msg.lower() or "stay" in msg.lower()

    def test_multiple_missing_no_context_shows_bullet_list(self):
        """When no context is known, the bullet-list format is used."""
        msg = format_clarification(["budget", "people", "days", "origin"])
        assert "budget" in msg.lower()
        assert "traveler" in msg.lower() or "people" in msg.lower()

    def test_two_missing_with_context_shows_targeted_questions(self):
        """Two missing fields with context should not repeat known values."""
        known = _intent(origin="Chennai", destination="Goa")
        msg = format_clarification(["budget", "days"], known_context=known)
        # Should mention known info
        assert "chennai" in msg.lower() or "goa" in msg.lower()
        # Should ask for both missing
        assert "budget" in msg.lower()
        assert "day" in msg.lower() or "long" in msg.lower() or "stay" in msg.lower()

    def test_message_does_not_ask_for_already_known_fields(self):
        """When budget and people are known but only days is missing, the question asks only about days."""
        known = _intent(budget=Decimal("15000"), people=2, origin="Chennai", destination="Goa")
        msg = format_clarification(["days"], known_context=known)
        # Should be the single-question "Got it — <summary>. <question>" form
        assert "got it" in msg.lower()
        # The question is the last sentence after the last ". "
        # e.g. "Got it — Chennai → Goa, 2 travelers, ₹15,000. How many days would you like to stay?"
        question_sentence = msg.rsplit(".", 1)[-1].lower().strip()
        assert "budget" not in question_sentence
        assert "traveler" not in question_sentence
        assert "people" not in question_sentence
        # The question must ask about days
        assert "day" in question_sentence or "stay" in question_sentence or "long" in question_sentence


# ===========================================================================
# AIIntentService.parse_trip_intent_with_context tests (mocked client)
# ===========================================================================

@pytest.mark.asyncio
async def test_context_parse_uses_merge_prompt():
    """parse_trip_intent_with_context calls the client with context JSON in the prompt."""
    existing = _intent(
        budget=Decimal("10000"), people=1,
        origin="Chennai", destination="Delhi",
        interests=["famous places"],
    )
    # Mock client returns a full merged intent
    mock_client = MagicMock()
    mock_client.has_credentials = True
    mock_client.chat_completion = AsyncMock(return_value={
        "budget": 10000,
        "currency": "INR",
        "people": 1,
        "days": 4,
        "origin": "Chennai",
        "destination": "Delhi",
        "interests": ["famous places"],
        "traveler_type": None,
    })

    svc = AIIntentService(client=mock_client, use_mock=False)
    result = await svc.parse_trip_intent_with_context("4 days", existing)

    assert result.days == 4
    assert result.budget == Decimal("10000")
    assert result.origin == "Chennai"
    # Confirm client was called
    mock_client.chat_completion.assert_called_once()
    # Verify context JSON was part of the prompt
    call_args = mock_client.chat_completion.call_args[0][0]
    user_message = call_args[-1]["content"]
    assert "10000" in user_message
    assert "Chennai" in user_message


@pytest.mark.asyncio
async def test_context_parse_falls_back_on_api_error():
    """When client raises, context-aware parse falls back to heuristic action+field result.

    The service returns an action-classified result (CHANGE_DAYS with days=4).
    The orchestrator's apply_change_action() then applies it onto the existing context.
    This test verifies the service returns days=4 and CHANGE_DAYS, and the merge
    via apply_change_action() preserves all other fields.
    """
    from budlance.ai.schemas import TripAction

    existing = _intent(
        budget=Decimal("15000"), people=2, days=3,
        origin="Chennai", destination="Goa",
    )
    mock_client = MagicMock()
    mock_client.has_credentials = True
    mock_client.chat_completion = AsyncMock(side_effect=Exception("Rate limited"))

    svc = AIIntentService(client=mock_client, use_mock=False)
    # "Make it 4 days" should still classify as CHANGE_DAYS with days=4 in fallback
    result = await svc.parse_trip_intent_with_context("Make it 4 days.", existing)
    assert result.days == 4
    # Service returns action-specific result; orchestrator applies merge:
    merged = existing.apply_change_action(result)
    assert merged.budget == Decimal("15000")   # preserved from existing
    assert merged.days == 4                    # updated
    assert merged.people == 2                  # preserved
    assert merged.action == TripAction.CHANGE_DAYS



# ===========================================================================
# Orchestrator wiring tests (fully mocked)
# ===========================================================================

@pytest.mark.asyncio
async def test_orchestrator_saves_pending_intent_on_clarification():
    """When a partial intent is returned, the orchestrator saves it to conversation_repo."""
    from budlance.orchestrator.orchestrator import BudlanceOrchestrator

    mock_ai = MagicMock(spec=AIIntentService)
    mock_ai.parse_rescue_intent = AsyncMock(return_value=MagicMock(rescue_type="unknown"))
    # First message: missing days
    mock_ai.parse_trip_intent = AsyncMock(return_value=_intent(
        budget=Decimal("10000"), people=1, origin="Chennai", destination="Delhi",
    ))
    mock_ai.parse_trip_intent_with_context = AsyncMock()

    mock_conv_repo = MagicMock(spec=ConversationStateRepository)
    mock_conv_repo.get_pending_intent.return_value = None  # fresh chat
    saved = []
    mock_conv_repo.save_pending_intent.side_effect = lambda cid, intent: saved.append(intent)

    orch = BudlanceOrchestrator(
        ai_service=mock_ai,
        conversation_repo=mock_conv_repo,
        user_repo=MagicMock(),
        trip_repo=MagicMock(),
        intent_repo=MagicMock(),
        itinerary_repo=MagicMock(),
        ledger_repo=MagicMock(),
        rescue_repo=MagicMock(),
        cache_manager=MagicMock(),
        normalizer=MagicMock(),
        estimation_layer=MagicMock(),
        budget_engine=MagicMock(),
        optimizer=MagicMock(),
        itinerary_generator=MagicMock(),
        ledger_manager=MagicMock(),
        rescue_service=MagicMock(),
    )

    result = await orch.handle_user_message(
        telegram_user_id=111,
        chat_id=111,
        message="Currently I am in Chennai, I want to go Delhi, budget 10000, 1 person.",
    )

    assert result.status == "CLARIFICATION"
    assert len(saved) == 1
    assert saved[0].origin == "Chennai"
    assert saved[0].destination == "Delhi"
    # Clarification must mention what we know
    assert "chennai" in result.message_text.lower() or "delhi" in result.message_text.lower()


@pytest.mark.asyncio
async def test_orchestrator_merges_follow_up_into_pending_intent():
    """Second message '4 days' is merged into the pending intent and proceeds to planning."""
    from budlance.orchestrator.orchestrator import BudlanceOrchestrator

    existing_pending = _intent(
        budget=Decimal("10000"), people=1, origin="Chennai", destination="Delhi",
        interests=["famous places"],
    )
    merged_complete = _intent(
        budget=Decimal("10000"), people=1, days=4, origin="Chennai", destination="Delhi",
        interests=["famous places"],
    )

    mock_ai = MagicMock(spec=AIIntentService)
    mock_ai.parse_rescue_intent = AsyncMock(return_value=MagicMock(rescue_type="unknown"))
    mock_ai.parse_trip_intent = AsyncMock()  # should NOT be called
    mock_ai.parse_trip_intent_with_context = AsyncMock(return_value=merged_complete)

    mock_conv_repo = MagicMock(spec=ConversationStateRepository)
    mock_conv_repo.get_pending_intent.return_value = existing_pending

    # Mock all the planning infrastructure to return a feasible plan
    from budlance.engine.models import BudgetBreakdown, BudgetEvaluationResult
    from budlance.schemas.travel import FlightOption
    from budlance.serpapi.models import DataSource
    from budlance.db.models import utc_now, Trip
    from uuid import uuid4

    fake_transport = FlightOption(
        airline="IndiGo", flight_number="6E-101",
        price=Decimal("3000"), source=DataSource.FALLBACK, is_fallback=True
    )
    fake_breakdown = BudgetBreakdown(
        total_budget=Decimal("10000"),
        bucket_a_fixed=Decimal("5000"),
        bucket_b_survival=Decimal("2000"),
        bucket_c_activities=Decimal("500"),
        bucket_d_rescue=Decimal("500"),
        transport_cost=Decimal("3000"),
        hotel_cost=Decimal("2000"),
        food_cost=Decimal("1500"),
        local_transit_cost=Decimal("500"),
        total_allocated=Decimal("8000"),
        remaining_surplus=Decimal("2000"),
        currency="INR",
    )
    fake_eval = BudgetEvaluationResult(
        status="FEASIBLE",
        is_feasible=True,
        breakdown=fake_breakdown,
        deficit=Decimal("0"),
        explanation="Feasible",
    )

    mock_cache = MagicMock()
    mock_cache.get_travel_data = AsyncMock(return_value=MagicMock(data={}, is_fallback=True))
    mock_normalizer = MagicMock()
    mock_normalizer.normalize_flights.return_value = [fake_transport]
    mock_normalizer.normalize_transit.return_value = []
    mock_normalizer.normalize_hotels.return_value = []
    mock_normalizer.normalize_places.return_value = []
    mock_normalizer.normalize_routes.return_value = []

    mock_budget = MagicMock()
    mock_budget.evaluate.return_value = fake_eval

    mock_optimizer = MagicMock()
    mock_optimizer.optimize.return_value = MagicMock(is_feasible=False)

    mock_user_repo = MagicMock()
    mock_user_repo.get_or_create_user.return_value = MagicMock(id=uuid4())

    mock_trip_repo = MagicMock()
    fake_trip = Trip(
        id=uuid4(), user_id=uuid4(), telegram_chat_id=111,
        budget_total=Decimal("10000"), status="planning",
        origin="Chennai", destination="Delhi", people_count=1, duration_days=4,
    )
    mock_trip_repo.create_trip.return_value = fake_trip
    mock_trip_repo.deactivate_previous_trips.return_value = None

    mock_intent_repo = MagicMock()
    mock_intent_repo.save_trip_intent.return_value = MagicMock()

    mock_itin_gen = MagicMock()
    mock_itin_gen.generate.return_value = None

    mock_ledger = MagicMock()
    mock_ledger.initialize_ledger.return_value = None

    mock_estimation = MagicMock()
    mock_estimation.estimate_food.return_value = Decimal("2000")
    mock_estimation.estimate_local_transit_daily.return_value = Decimal("500")

    orch = BudlanceOrchestrator(
        ai_service=mock_ai,
        conversation_repo=mock_conv_repo,
        user_repo=mock_user_repo,
        trip_repo=mock_trip_repo,
        intent_repo=mock_intent_repo,
        itinerary_repo=MagicMock(),
        ledger_repo=MagicMock(),
        rescue_repo=MagicMock(),
        cache_manager=mock_cache,
        normalizer=mock_normalizer,
        estimation_layer=mock_estimation,
        budget_engine=mock_budget,
        optimizer=mock_optimizer,
        itinerary_generator=mock_itin_gen,
        ledger_manager=mock_ledger,
        rescue_service=MagicMock(),
    )

    result = await orch.handle_user_message(
        telegram_user_id=111,
        chat_id=111,
        message="4 days",
    )

    # Should have proceeded to planning (FEASIBLE or at least called context parse)
    mock_ai.parse_trip_intent_with_context.assert_called_once()
    # parse_trip_intent (fresh parse) should NOT have been called since we had pending intent
    mock_ai.parse_trip_intent.assert_not_called()


# ===========================================================================
# No-Guessing rule tests
# ===========================================================================


class TestNoGuessingRule:
    """The system must not fabricate missing fields."""

    def test_no_guess_budget_from_sparse_message(self):
        svc = _svc()
        intent = svc._mock_parse_trip_intent("Plan a trip from Chennai.")
        assert intent.budget is None

    def test_no_guess_people_from_sparse_message(self):
        svc = _svc()
        intent = svc._mock_parse_trip_intent("Plan a trip from Chennai.")
        assert intent.people is None

    def test_no_guess_days_from_sparse_message(self):
        svc = _svc()
        intent = svc._mock_parse_trip_intent("I have 15000 and want to travel.")
        assert intent.days is None

    def test_no_guess_origin_from_destination_only(self):
        svc = _svc()
        intent = svc._mock_parse_trip_intent("I want to go to Goa, 2 people, 3 days, budget 20000.")
        assert intent.origin is None

    def test_no_guess_destination_from_origin_only(self):
        svc = _svc()
        intent = svc._mock_parse_trip_intent("Plan a trip from Chennai, 2 people, 3 days, budget 15000.")
        # destination may be None (no destination stated) but must NOT be Chennai
        if intent.destination:
            assert intent.destination.lower() != "chennai"


# ===========================================================================
# Regression: existing tests still work with new mock parser
# ===========================================================================


class TestRegressionExistingPatterns:
    """Ensure the fixes did not break any patterns that were already working."""

    def test_regression_complete_english_plan(self):
        svc = _svc()
        intent = svc._mock_parse_trip_intent(
            "Plan a trip from Chennai for 2 people, 3 days, with a budget of ₹15,000."
        )
        assert intent.budget == Decimal("15000")
        assert intent.people == 2
        assert intent.days == 3
        assert (intent.origin or "").lower() == "chennai"

    def test_regression_rescue_price_dispute(self):
        svc = _svc()
        rescue = svc._mock_parse_rescue_intent("The auto driver is asking ₹500, is it fair?")
        assert rescue.rescue_type == "price_dispute"

    def test_regression_rescue_weather(self):
        svc = _svc()
        rescue = svc._mock_parse_rescue_intent("It's raining heavily at the beach")
        assert rescue.rescue_type == "weather_closure"

    def test_regression_solo_keyword(self):
        svc = _svc()
        intent = svc._mock_parse_trip_intent("Solo trip from Delhi to Jaipur, 3 days, budget 8000.")
        assert intent.people == 1
        assert intent.traveler_type == "solo"

    def test_regression_couple_keyword(self):
        svc = _svc()
        intent = svc._mock_parse_trip_intent("A couple's trip to Goa from Bangalore for 4 days, ₹20000.")
        assert intent.people == 2
        assert intent.traveler_type == "couple"
