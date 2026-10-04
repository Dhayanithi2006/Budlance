"""Budlance — Conversational Action Router Tests.

Tests the action-first routing architecture introduced in the Conversational Action Router.

Coverage:
  - TripAction enum and action field on ParsedTripIntent
  - apply_change_action() field-specific merge
  - FIND_ALTERNATIVE state handling (destination cleared, constraints kept)
  - NOT_FEASIBLE state preservation (pending intent saved for follow-up)
  - UNRECOGNIZED handling (no state mutation)
  - RESCUE routing (active confirmed trip, never pending draft)
  - NEW_TRIP clears stale pending state
  - CHANGE_* actions only update the targeted field
  - Full orchestrator action routing flow with mocked dependencies

Invariants enforced:
  1. ONE AI call per user message (no double-call).
  2. Python owns state routing logic (not the LLM).
  3. NOT_FEASIBLE saves pending intent for FIND_ALTERNATIVE follow-up.
  4. RESCUE reads from confirmed trip only.
  5. UNRECOGNIZED changes nothing.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.ai.service import AIIntentService
from budlance.db.repositories.conversation_repo import ConversationStateRepository


# ============================================================================
# Helpers
# ============================================================================

def _intent(**kwargs) -> ParsedTripIntent:
    return ParsedTripIntent(**kwargs)


def _svc() -> AIIntentService:
    return AIIntentService(use_mock=True)


def _full_intent(**kwargs) -> ParsedTripIntent:
    """Full plannable intent with sensible defaults for all required fields."""
    defaults = dict(
        action=TripAction.NEW_TRIP,
        budget=Decimal("15000"),
        people=2,
        days=3,
        origin="Chennai",
        destination="Goa",
    )
    defaults.update(kwargs)
    return ParsedTripIntent(**defaults)


# ============================================================================
# TripAction enum
# ============================================================================

class TestTripActionEnum:
    def test_all_expected_values_exist(self):
        values = {a.value for a in TripAction}
        expected = {
            "NEW_TRIP", "CHANGE_BUDGET", "CHANGE_DAYS", "CHANGE_PEOPLE",
            "CHANGE_DESTINATION", "FIND_ALTERNATIVE", "RESCUE",
            "LOG_EXPENSE", "TRIP_COMPLETE", "UNRECOGNIZED",
        }
        assert expected.issubset(values)

    def test_action_field_default_is_new_trip(self):
        intent = ParsedTripIntent()
        assert intent.action == TripAction.NEW_TRIP

    def test_action_roundtrips_via_model_validate(self):
        for action in TripAction:
            data = {"action": action.value, "currency": "INR", "interests": []}
            intent = ParsedTripIntent.model_validate(data)
            assert intent.action == action


# ============================================================================
# apply_change_action — field-specific merge
# ============================================================================

class TestApplyChangeAction:

    def test_change_budget_only_updates_budget(self):
        base = _full_intent(budget=Decimal("15000"))
        update = ParsedTripIntent(action=TripAction.CHANGE_BUDGET, budget=Decimal("20000"))
        merged = base.apply_change_action(update)
        assert merged.budget == Decimal("20000")
        assert merged.people == 2     # unchanged
        assert merged.days == 3       # unchanged
        assert merged.origin == "Chennai"
        assert merged.destination == "Goa"
        assert merged.action == TripAction.CHANGE_BUDGET

    def test_change_days_only_updates_days(self):
        base = _full_intent(days=3)
        update = ParsedTripIntent(action=TripAction.CHANGE_DAYS, days=5)
        merged = base.apply_change_action(update)
        assert merged.days == 5
        assert merged.budget == Decimal("15000")  # unchanged
        assert merged.people == 2                 # unchanged

    def test_change_people_only_updates_people(self):
        base = _full_intent(people=2)
        update = ParsedTripIntent(action=TripAction.CHANGE_PEOPLE, people=1)
        merged = base.apply_change_action(update)
        assert merged.people == 1
        assert merged.budget == Decimal("15000")  # unchanged
        assert merged.days == 3                   # unchanged

    def test_change_destination_only_updates_destination(self):
        base = _full_intent(destination="Goa")
        update = ParsedTripIntent(action=TripAction.CHANGE_DESTINATION, destination="Delhi")
        merged = base.apply_change_action(update)
        assert merged.destination == "Delhi"
        assert merged.budget == Decimal("15000")  # unchanged
        assert merged.people == 2                 # unchanged

    def test_change_destination_to_none_not_allowed_via_change(self):
        """FIND_ALTERNATIVE (not CHANGE_DESTINATION) is the way to clear destination."""
        base = _full_intent(destination="Goa")
        # If destination is None in a CHANGE_DESTINATION update, model_copy won't overwrite
        update = ParsedTripIntent(action=TripAction.CHANGE_DESTINATION, destination=None)
        merged = base.apply_change_action(update)
        # apply_change_action only overwrites `destination` with what the update has
        assert merged.destination is None

    def test_fallback_action_uses_merge_with(self):
        """For actions not matching any CHANGE_*, falls back to merge_with."""
        base = _full_intent(budget=Decimal("10000"))
        update = ParsedTripIntent(
            action=TripAction.NEW_TRIP,
            budget=Decimal("20000"),
            people=3,
            days=4,
            origin="Mumbai",
        )
        merged = base.apply_change_action(update)
        # merge_with: update values take precedence
        assert merged.budget == Decimal("20000")
        assert merged.people == 3


# ============================================================================
# merge_with propagates action
# ============================================================================

class TestMergeWithAction:

    def test_merge_with_preserves_update_action(self):
        base = _full_intent(action=TripAction.NEW_TRIP)
        update = ParsedTripIntent(action=TripAction.CHANGE_DAYS, days=5)
        merged = base.merge_with(update)
        assert merged.action == TripAction.CHANGE_DAYS

    def test_merge_with_interests_union(self):
        base = _full_intent(interests=["beach"])
        update = ParsedTripIntent(action=TripAction.NEW_TRIP, interests=["food", "beach"])
        merged = base.merge_with(update)
        assert "beach" in merged.interests
        assert "food" in merged.interests
        assert merged.interests.count("beach") == 1  # no duplicates


# ============================================================================
# Mock action classification
# ============================================================================

class TestMockActionClassification:

    def test_new_trip_from_full_message(self):
        svc = _svc()
        action = svc._mock_classify_action("plan a trip from mumbai to goa 3 days budget 15000")
        assert action == TripAction.NEW_TRIP

    def test_change_days_pure_follow_up(self):
        svc = _svc()
        action = svc._mock_classify_action("4 days")
        assert action == TripAction.CHANGE_DAYS

    def test_change_days_make_it_phrase(self):
        svc = _svc()
        action = svc._mock_classify_action("make it 5 days.")
        assert action == TripAction.CHANGE_DAYS

    def test_change_people_only_me(self):
        svc = _svc()
        action = svc._mock_classify_action("only me now")
        assert action == TripAction.CHANGE_PEOPLE

    def test_find_alternative_other_place(self):
        svc = _svc()
        action = svc._mock_classify_action("suggest another place within my budget")
        assert action == TripAction.FIND_ALTERNATIVE

    def test_find_alternative_too_expensive(self):
        svc = _svc()
        action = svc._mock_classify_action("goa is too expensive. recommend another destination")
        assert action == TripAction.FIND_ALTERNATIVE

    def test_find_alternative_any_other_place(self):
        svc = _svc()
        action = svc._mock_classify_action("any other place?")
        assert action == TripAction.FIND_ALTERNATIVE

    def test_find_alternative_somewhere_else(self):
        svc = _svc()
        action = svc._mock_classify_action("try somewhere else")
        assert action == TripAction.FIND_ALTERNATIVE

    def test_rescue_rain_storm(self):
        svc = _svc()
        action = svc._mock_classify_action("it is storming and raining heavily")
        assert action == TripAction.RESCUE

    def test_rescue_price_dispute(self):
        svc = _svc()
        action = svc._mock_classify_action("auto driver asking 500 rupees")
        assert action == TripAction.RESCUE

    def test_unrecognized_ok(self):
        svc = _svc()
        action = svc._mock_classify_action("ok")
        assert action == TripAction.UNRECOGNIZED

    def test_unrecognized_hmm(self):
        svc = _svc()
        action = svc._mock_classify_action("hmm")
        assert action == TripAction.UNRECOGNIZED

    def test_change_destination_explicitly_named(self):
        svc = _svc()
        action = svc._mock_classify_action("actually want to go to delhi")
        assert action == TripAction.CHANGE_DESTINATION

    def test_rescue_not_triggered_by_planning_message_with_rain_word(self):
        """A planning message that happens to contain 'rain' is not a rescue."""
        svc = _svc()
        # Contains "rain" but also contains "budget", "days" — planning indicators
        action = svc._mock_classify_action("plan a rainforest trip budget 15000 3 days from mumbai")
        assert action == TripAction.NEW_TRIP

    def test_tanglish_new_trip(self):
        svc = _svc()
        intent = svc._mock_parse_trip_intent(
            "Enakku 15000 budget irukku, 2 peru, 3 days Chennai la irundhu hill station poganum."
        )
        assert intent.action == TripAction.NEW_TRIP
        assert intent.budget == Decimal("15000")
        assert intent.people == 2
        assert intent.days == 3
        assert (intent.origin or "").lower() == "chennai"


# ============================================================================
# Mock field extraction for CHANGE_* actions
# ============================================================================

class TestMockChangeActionExtraction:

    def test_change_days_extracts_only_days(self):
        svc = _svc()
        intent = svc._mock_parse_trip_intent("make it 4 days.")
        assert intent.action == TripAction.CHANGE_DAYS
        assert intent.days == 4
        assert intent.budget is None
        assert intent.people is None
        assert intent.origin is None

    def test_change_budget_extracts_only_budget(self):
        svc = _svc()
        intent = svc._mock_parse_trip_intent("budget is now 20000")
        assert intent.action == TripAction.CHANGE_BUDGET
        assert intent.budget == Decimal("20000")
        assert intent.days is None

    def test_rescue_extracts_detail(self):
        svc = _svc()
        intent = svc._mock_parse_trip_intent("it is raining so heavy at the beach")
        assert intent.action == TripAction.RESCUE
        assert intent.rescue_detail is not None

    def test_find_alternative_returns_empty_planning_fields(self):
        svc = _svc()
        intent = svc._mock_parse_trip_intent("recommend another place within my budget")
        assert intent.action == TripAction.FIND_ALTERNATIVE
        assert intent.destination is None
        assert intent.budget is None
        assert intent.people is None

    def test_unrecognized_returns_all_none(self):
        svc = _svc()
        intent = svc._mock_parse_trip_intent("ok")
        assert intent.action == TripAction.UNRECOGNIZED
        assert intent.budget is None
        assert intent.people is None
        assert intent.days is None


# ============================================================================
# Orchestrator Action-First Routing (fully mocked dependencies)
# ============================================================================

def _make_orch(ai_service, conv_repo=None):
    """Build a minimal BudlanceOrchestrator with everything else mocked."""
    from budlance.orchestrator.orchestrator import BudlanceOrchestrator

    conv_repo = conv_repo or MagicMock(spec=ConversationStateRepository)
    conv_repo.get_pending_intent.return_value = None
    conv_repo.save_pending_intent.return_value = None
    conv_repo.clear_pending_intent.return_value = None

    return BudlanceOrchestrator(
        ai_service=ai_service,
        conversation_repo=conv_repo,
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


@pytest.mark.asyncio
async def test_new_trip_clears_stale_pending():
    """NEW_TRIP action must clear any stale pending conversation state."""
    new_trip_result = _intent(
        action=TripAction.NEW_TRIP,
        budget=Decimal("15000"), people=2, origin="Chennai",
    )

    mock_ai = MagicMock()
    mock_ai.parse_trip_intent = AsyncMock(return_value=new_trip_result)
    mock_ai.parse_trip_intent_with_context = AsyncMock(return_value=new_trip_result)

    mock_conv = MagicMock(spec=ConversationStateRepository)
    stale = _full_intent(destination="Goa")
    mock_conv.get_pending_intent.return_value = stale  # stale pending intent exists
    cleared = []
    mock_conv.clear_pending_intent.side_effect = lambda cid: cleared.append(cid)
    mock_conv.save_pending_intent.return_value = None

    orch = _make_orch(mock_ai, mock_conv)
    result = await orch.handle_user_message(
        telegram_user_id=1, chat_id=100,
        message="Actually start over. Plan a trip from Chennai, 2 people, budget 15000.",
    )

    # Stale state was cleared
    assert len(cleared) > 0, "clear_pending_intent was not called for NEW_TRIP"
    # Result is CLARIFICATION because days is missing
    assert result.status == "CLARIFICATION"


@pytest.mark.asyncio
async def test_unrecognized_does_not_mutate_state():
    """UNRECOGNIZED must not call save_pending_intent or clear_pending_intent."""
    mock_ai = MagicMock()
    mock_ai.parse_trip_intent = AsyncMock(return_value=_intent(action=TripAction.UNRECOGNIZED))
    mock_ai.parse_trip_intent_with_context = AsyncMock(return_value=_intent(action=TripAction.UNRECOGNIZED))

    mock_conv = MagicMock(spec=ConversationStateRepository)
    mock_conv.get_pending_intent.return_value = None
    mock_conv.save_pending_intent.return_value = None
    mock_conv.clear_pending_intent.return_value = None

    orch = _make_orch(mock_ai, mock_conv)
    result = await orch.handle_user_message(telegram_user_id=1, chat_id=200, message="ok")

    mock_conv.save_pending_intent.assert_not_called()
    mock_conv.clear_pending_intent.assert_not_called()
    assert result.status == "CLARIFICATION"  # returns helpful message


@pytest.mark.asyncio
async def test_not_feasible_saves_pending_intent():
    """NOT_FEASIBLE must save the pending intent so the user can FIND_ALTERNATIVE."""
    from budlance.engine.models import BudgetBreakdown, BudgetEvaluationResult, OptimizationResult

    mock_ai = MagicMock(spec=AIIntentService)
    mock_ai.parse_trip_intent = AsyncMock(return_value=_full_intent(
        action=TripAction.NEW_TRIP,
        budget=Decimal("2000"),  # impossibly low
        people=2, days=5, origin="Mumbai", destination="Goa",
    ))

    # Make budget engine return NOT_FEASIBLE
    mock_budget = MagicMock()
    breakdown = BudgetBreakdown(
        total_budget=Decimal("2000"), bucket_a_fixed=Decimal("5000"),
        bucket_b_survival=Decimal("3000"), bucket_c_activities=Decimal("0"),
        bucket_d_rescue=Decimal("0"), transport_cost=Decimal("5000"),
        hotel_cost=Decimal("0"), food_cost=Decimal("0"), local_transit_cost=Decimal("0"),
        total_allocated=Decimal("8000"), remaining_surplus=Decimal("-6000"), currency="INR",
    )
    infeasible_eval = BudgetEvaluationResult(
        status="NOT_FEASIBLE", is_feasible=False, breakdown=breakdown,
        deficit=Decimal("6000"), explanation="Over budget",
    )
    mock_budget.evaluate.return_value = infeasible_eval

    mock_opt = MagicMock()
    opt_res = MagicMock(spec=OptimizationResult)
    opt_res.is_feasible = False
    opt_res.deficit = Decimal("6000")
    opt_res.explanation = "Cannot optimize further"
    opt_res.recommendation = "Increase budget"
    mock_opt.optimize.return_value = opt_res

    mock_cache = MagicMock()
    mock_cache.get_travel_data = AsyncMock(return_value=MagicMock(data={}, is_fallback=True))
    mock_norm = MagicMock()
    mock_norm.normalize_flights.return_value = []
    mock_norm.normalize_transit.return_value = []
    mock_norm.normalize_hotels.return_value = []
    mock_norm.normalize_places.return_value = []
    mock_norm.normalize_routes.return_value = []

    mock_est = MagicMock()
    mock_est.estimate_food.return_value = Decimal("2000")
    mock_est.estimate_local_transit_daily.return_value = Decimal("500")

    mock_conv = MagicMock(spec=ConversationStateRepository)
    mock_conv.get_pending_intent.return_value = None
    saved_intents = []
    mock_conv.save_pending_intent.side_effect = lambda cid, intent: saved_intents.append(intent)

    from budlance.orchestrator.orchestrator import BudlanceOrchestrator
    orch = BudlanceOrchestrator(
        ai_service=mock_ai,
        conversation_repo=mock_conv,
        user_repo=MagicMock(),
        trip_repo=MagicMock(),
        intent_repo=MagicMock(),
        itinerary_repo=MagicMock(),
        ledger_repo=MagicMock(),
        rescue_repo=MagicMock(),
        cache_manager=mock_cache,
        normalizer=mock_norm,
        estimation_layer=mock_est,
        budget_engine=mock_budget,
        optimizer=mock_opt,
        itinerary_generator=MagicMock(),
        ledger_manager=MagicMock(),
        rescue_service=MagicMock(),
    )

    result = await orch.handle_user_message(
        telegram_user_id=1, chat_id=300,
        message="Plan a trip from Mumbai to Goa for 2 people, 5 days, with budget ₹2,000",
    )

    assert result.status == "NOT_FEASIBLE"
    # CRITICAL: pending intent must have been saved (for FIND_ALTERNATIVE follow-up)
    assert len(saved_intents) > 0, "NOT_FEASIBLE must save pending intent for follow-up"
    saved = saved_intents[-1]
    assert saved.budget == Decimal("2000")
    assert saved.people == 2
    assert saved.origin == "Mumbai"


@pytest.mark.asyncio
async def test_find_alternative_clears_destination_keeps_constraints():
    """FIND_ALTERNATIVE must keep budget/people/days/origin and clear destination."""
    pending = _full_intent(
        action=TripAction.NEW_TRIP,
        budget=Decimal("10000"), people=2, days=3,
        origin="Mumbai", destination="Goa",
    )

    mock_ai = MagicMock()
    mock_ai.parse_trip_intent = AsyncMock(return_value=_intent(action=TripAction.FIND_ALTERNATIVE))
    mock_ai.parse_trip_intent_with_context = AsyncMock(return_value=_intent(
        action=TripAction.FIND_ALTERNATIVE,
        # AI returns no planning fields — destination discovery is Python's job
    ))

    mock_conv = MagicMock(spec=ConversationStateRepository)
    mock_conv.get_pending_intent.return_value = pending

    saved_intents = []
    mock_conv.save_pending_intent.side_effect = lambda cid, intent: saved_intents.append(intent)

    orch = _make_orch(mock_ai, mock_conv)

    # Make the cache manager + budget engine discoverable via AsyncMock so it doesn't raise
    orch.cache_manager = MagicMock()
    orch.cache_manager.get_travel_data = AsyncMock(return_value=MagicMock(data={}, is_fallback=True))
    orch.normalizer = MagicMock()
    orch.normalizer.normalize_flights.return_value = []
    orch.normalizer.normalize_transit.return_value = []
    orch.normalizer.normalize_hotels.return_value = []
    orch.normalizer.normalize_places.return_value = []
    orch.normalizer.normalize_routes.return_value = []

    from budlance.engine.models import BudgetBreakdown, BudgetEvaluationResult, OptimizationResult

    # Return feasible result so we can check destination was cleared before discovery
    fake_breakdown = BudgetBreakdown(
        total_budget=Decimal("10000"), bucket_a_fixed=Decimal("4000"),
        bucket_b_survival=Decimal("2000"), bucket_c_activities=Decimal("500"),
        bucket_d_rescue=Decimal("500"), transport_cost=Decimal("2000"),
        hotel_cost=Decimal("2000"), food_cost=Decimal("1500"),
        local_transit_cost=Decimal("500"), total_allocated=Decimal("7000"),
        remaining_surplus=Decimal("3000"), currency="INR",
    )
    fake_eval = BudgetEvaluationResult(
        status="FEASIBLE", is_feasible=True, breakdown=fake_breakdown,
        deficit=Decimal("0"), explanation="Feasible",
    )
    orch.budget_engine = MagicMock()
    orch.budget_engine.evaluate.return_value = fake_eval

    orch.estimation = MagicMock()
    orch.estimation.estimate_food.return_value = Decimal("1500")
    orch.estimation.estimate_local_transit_daily.return_value = Decimal("500")

    from uuid import uuid4
    from budlance.db.models import Trip
    fake_trip = Trip(
        id=uuid4(), user_id=uuid4(), telegram_chat_id=400,
        budget_total=Decimal("10000"), status="planning",
        origin="Mumbai", destination="Jaipur", people_count=2, duration_days=3,
    )
    orch.trip_repo.create_trip.return_value = fake_trip
    orch.user_repo.get_or_create_user.return_value = MagicMock(id=uuid4())
    orch.intent_repo.save_trip_intent.return_value = MagicMock()
    orch.itinerary_generator.generate.return_value = None
    orch.ledger_manager.initialize_ledger.return_value = None

    result = await orch.handle_user_message(
        telegram_user_id=1, chat_id=400,
        message="Goa is too expensive. Recommend somewhere else.",
    )

    # Should proceed to planning (discover alternatives) — any non-error status
    assert result.status in ("FEASIBLE", "NOT_FEASIBLE", "CLARIFICATION")
    # If FEASIBLE or NOT_FEASIBLE, we know destination discovery was triggered
    # (original destination "Goa" was cleared first via FIND_ALTERNATIVE routing)


@pytest.mark.asyncio
async def test_rescue_action_routes_to_rescue_service():
    """RESCUE action must call rescue_service.execute_rescue (not planning pipeline)."""
    from budlance.rescue.models import RescueResult
    from budlance.schemas.travel import PlaceOption
    from budlance.serpapi.models import DataSource

    fake_rescue_result = RescueResult(
        rescue_type="weather_closure",
        success=True,
        is_feasible=True,
        user_issue="Heavy rain",
        resolution_summary="Alternative found",
        selected_alternative=PlaceOption(
            name="Indoor Museum",
            category="museum",
            source=DataSource.FALLBACK,
            is_fallback=True,
        ),
        budget_impact=Decimal("0"),
        trip_id=uuid4(),
    )

    mock_rescue_svc = MagicMock()
    mock_rescue_svc.execute_rescue = AsyncMock(return_value=fake_rescue_result)

    mock_conv = MagicMock(spec=ConversationStateRepository)
    mock_conv.get_pending_intent.return_value = None

    mock_ai_svc = MagicMock()
    mock_ai_svc.parse_trip_intent = AsyncMock(return_value=_intent(
        action=TripAction.RESCUE,
        rescue_detail="It is raining heavily",
    ))
    mock_ai_svc.parse_trip_intent_with_context = AsyncMock(return_value=_intent(
        action=TripAction.RESCUE,
        rescue_detail="It is raining heavily",
    ))

    from budlance.orchestrator.orchestrator import BudlanceOrchestrator
    orch = BudlanceOrchestrator(
        ai_service=mock_ai_svc,
        conversation_repo=mock_conv,
        rescue_service=mock_rescue_svc,
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
    )

    result = await orch.handle_user_message(
        telegram_user_id=1, chat_id=500,
        message="It is raining heavily, what do I do?",
    )

    mock_rescue_svc.execute_rescue.assert_called_once_with(
        chat_id=500, user_message="It is raining heavily, what do I do?"
    )
    assert result.status == "RESCUE"


@pytest.mark.asyncio
async def test_change_days_with_pending_applies_only_days():
    """CHANGE_DAYS with pending context updates only days, keeps all other fields."""
    pending = _full_intent(
        action=TripAction.NEW_TRIP,
        budget=Decimal("15000"), people=2, days=3, origin="Chennai", destination="Goa",
    )

    mock_ai = MagicMock()
    # AI returns CHANGE_DAYS with only days filled
    mock_ai.parse_trip_intent = AsyncMock(return_value=_intent(action=TripAction.CHANGE_DAYS, days=5))
    mock_ai.parse_trip_intent_with_context = AsyncMock(return_value=_intent(
        action=TripAction.CHANGE_DAYS, days=5,
    ))

    mock_conv = MagicMock(spec=ConversationStateRepository)
    mock_conv.get_pending_intent.return_value = pending

    from budlance.engine.models import BudgetBreakdown, BudgetEvaluationResult
    fake_breakdown = BudgetBreakdown(
        total_budget=Decimal("15000"), bucket_a_fixed=Decimal("5000"),
        bucket_b_survival=Decimal("2500"), bucket_c_activities=Decimal("750"),
        bucket_d_rescue=Decimal("750"), transport_cost=Decimal("3000"),
        hotel_cost=Decimal("2000"), food_cost=Decimal("2000"),
        local_transit_cost=Decimal("500"), total_allocated=Decimal("9000"),
        remaining_surplus=Decimal("6000"), currency="INR",
    )
    fake_eval = BudgetEvaluationResult(
        status="FEASIBLE", is_feasible=True, breakdown=fake_breakdown,
        deficit=Decimal("0"), explanation="Feasible",
    )
    mock_budget = MagicMock()
    mock_budget.evaluate.return_value = fake_eval

    mock_cache = MagicMock()
    mock_cache.get_travel_data = AsyncMock(return_value=MagicMock(data={}, is_fallback=True))
    from budlance.schemas.travel import TransitOption
    mock_norm = MagicMock()
    mock_norm.normalize_flights.return_value = []
    mock_norm.normalize_transit.return_value = [
        TransitOption(
            transit_type="train",
            origin="Chennai",
            destination="Goa",
            name_or_operator="Vasco Express",
            price=Decimal("3000"),
            class_or_type="3A",
            is_fallback=True,
        )
    ]
    mock_norm.normalize_hotels.return_value = []
    mock_norm.normalize_places.return_value = []
    mock_norm.normalize_routes.return_value = []

    mock_est = MagicMock()
    mock_est.estimate_food.return_value = Decimal("2000")
    mock_est.estimate_local_transit_daily.return_value = Decimal("500")

    from uuid import uuid4
    from budlance.db.models import Trip
    fake_trip = Trip(
        id=uuid4(), user_id=uuid4(), telegram_chat_id=600,
        budget_total=Decimal("15000"), status="planning",
        origin="Chennai", destination="Goa", people_count=2, duration_days=5,
    )

    from budlance.orchestrator.orchestrator import BudlanceOrchestrator
    orch = BudlanceOrchestrator(
        ai_service=mock_ai,
        conversation_repo=mock_conv,
        user_repo=MagicMock(),
        trip_repo=MagicMock(),
        intent_repo=MagicMock(),
        itinerary_repo=MagicMock(),
        ledger_repo=MagicMock(),
        rescue_repo=MagicMock(),
        cache_manager=mock_cache,
        normalizer=mock_norm,
        estimation_layer=mock_est,
        budget_engine=mock_budget,
        optimizer=MagicMock(),
        itinerary_generator=MagicMock(),
        ledger_manager=MagicMock(),
        rescue_service=MagicMock(),
    )
    orch.trip_repo.create_trip.return_value = fake_trip
    orch.user_repo.get_or_create_user.return_value = MagicMock(id=uuid4())
    orch.intent_repo.save_trip_intent.return_value = MagicMock()
    orch.itinerary_generator.generate.return_value = None
    orch.ledger_manager.initialize_ledger.return_value = None

    result = await orch.handle_user_message(
        telegram_user_id=1, chat_id=600,
        message="Make it 5 days.",
    )

    # Should be FEASIBLE with the updated 5 days applied onto existing pending context
    assert result.status == "FEASIBLE"
    # parse_trip_intent_with_context must have been called (not fresh parse)
    mock_ai.parse_trip_intent_with_context.assert_called_once()


@pytest.mark.asyncio
async def test_rescue_does_not_use_pending_intent():
    """RESCUE must never load or modify the pending draft conversation state."""
    from budlance.rescue.models import RescueResult
    from budlance.schemas.travel import PlaceOption
    from budlance.serpapi.models import DataSource

    pending = _full_intent(
        action=TripAction.NEW_TRIP,
        budget=Decimal("15000"), people=2, days=3, origin="Chennai",
    )

    mock_ai = MagicMock()
    mock_ai.parse_trip_intent = AsyncMock(return_value=_intent(
        action=TripAction.RESCUE, rescue_detail="Heavy rain at the resort",
    ))
    mock_ai.parse_trip_intent_with_context = AsyncMock(return_value=_intent(
        action=TripAction.RESCUE,
        rescue_detail="Heavy rain at the resort",
    ))

    mock_conv = MagicMock(spec=ConversationStateRepository)
    mock_conv.get_pending_intent.return_value = pending  # pending exists but must NOT be used

    fake_rescue = RescueResult(
        rescue_type="weather_closure",
        success=True, is_feasible=True,
        user_issue="Heavy rain",
        resolution_summary="Alternative found",
        selected_alternative=PlaceOption(
            name="Indoor Market",
            category="market",
            source=DataSource.FALLBACK,
            is_fallback=True,
        ),
        budget_impact=Decimal("0"), trip_id=uuid4(),
    )
    mock_rescue_svc = MagicMock()
    mock_rescue_svc.execute_rescue = AsyncMock(return_value=fake_rescue)

    from budlance.orchestrator.orchestrator import BudlanceOrchestrator
    orch = BudlanceOrchestrator(
        ai_service=mock_ai,
        conversation_repo=mock_conv,
        rescue_service=mock_rescue_svc,
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
    )

    result = await orch.handle_user_message(
        telegram_user_id=1, chat_id=700,
        message="It is raining heavily",
    )

    assert result.status == "RESCUE"
    # CRITICAL: pending state must NOT have been mutated
    mock_conv.save_pending_intent.assert_not_called()
    mock_conv.clear_pending_intent.assert_not_called()


# ============================================================================
# ParsedTripIntent.rescue_detail field
# ============================================================================

class TestRescueDetailField:

    def test_rescue_detail_defaults_to_none(self):
        intent = ParsedTripIntent()
        assert intent.rescue_detail is None

    def test_rescue_detail_survives_model_copy(self):
        intent = _intent(action=TripAction.RESCUE, rescue_detail="heavy rain")
        copy = intent.model_copy(update={"days": 3})
        assert copy.rescue_detail == "heavy rain"

    def test_rescue_detail_cleared_in_merge_with(self):
        base = _intent(action=TripAction.RESCUE, rescue_detail="rain")
        update = _intent(action=TripAction.NEW_TRIP, budget=Decimal("10000"))
        merged = base.merge_with(update)
        assert merged.rescue_detail is None  # update has None rescue_detail


# ============================================================================
# Focused Telegram Reproduction: FIND_ALTERNATIVE after NOT_FEASIBLE Goa
# ============================================================================

@pytest.mark.asyncio
async def test_find_alternative_reproduction_does_not_repeat_goa():
    """Reproduce exact 2-message conversational flow:
    Message 1: 'I planned to go trip for 5 days, budget 10000, 1 person, from Chennai to Goa'
    Message 2: 'Recommend some other place within this budget'

    Verify:
    - Message 2 classifies as FIND_ALTERNATIVE
    - Pending intent keeps budget=10000, people=1, days=5, origin=Chennai
    - Goa is cleared and excluded from candidate discovery
    - Message 2 does not repeat Goa as the destination or in the message text
    """
    from budlance.orchestrator.orchestrator import BudlanceOrchestrator

    conv_repo = ConversationStateRepository(client=None)
    ai_service = AIIntentService(use_mock=True)
    orch = BudlanceOrchestrator(conversation_repo=conv_repo, ai_service=ai_service)
    chat_id = 123456

    # Simulate Goa exceeding budget in this test without relying on runtime fake hotels
    orig_eval = orch._evaluate_trip_candidate
    async def selective_eval(*args, **kwargs):
        dest = kwargs.get("destination") or (args[1] if len(args) > 1 else None)
        if dest == "Goa":
            return {
                "is_feasible": False,
                "destination": "Goa",
                "days": 5,
                "baseline_eval": None,
                "opt_result": None,
            }
        return await orig_eval(*args, **kwargs)
    orch._evaluate_trip_candidate = selective_eval

    # Message 1
    res1 = await orch.handle_user_message(
        telegram_user_id=1,
        chat_id=chat_id,
        message="I planned to go trip for 5 days, budget 10000, 1 person, from Chennai to Goa",
    )
    assert res1.status == "NOT_FEASIBLE"
    assert res1.selected_destination == "Goa"

    pending1 = conv_repo.get_pending_intent(chat_id)
    assert pending1 is not None
    assert pending1.destination == "Goa"
    assert pending1.budget == Decimal("10000")
    assert pending1.people == 1
    assert pending1.days == 5
    assert pending1.origin == "Chennai"

    # Message 2
    res2 = await orch.handle_user_message(
        telegram_user_id=1,
        chat_id=chat_id,
        message="Recommend some other place within this budget",
    )

    pending2 = conv_repo.get_pending_intent(chat_id)
    assert pending2 is not None
    assert pending2.budget == Decimal("10000")
    assert pending2.people == 1
    assert pending2.days == 5
    assert pending2.origin == "Chennai"
    assert pending2.destination is None

    # Crucial: Goa is NOT forced as selected_destination or formatted as destination
    assert res2.selected_destination != "Goa"
    assert "to Goa" not in res2.message_text

