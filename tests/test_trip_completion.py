"""Tests for Task 5 — Trip Completion + Final Reconciliation.

Verifies all 15 required scenarios:
1. TRIP_COMPLETE with active trip -> completion prompt created.
2. TRIP_COMPLETE without active trip -> safe response.
3. Valid final actual amount -> reconciliation recorded correctly, trip COMPLETED, active pointer cleared.
4. Skip reconciliation -> trip COMPLETED, no fabricated actual total, active pointer cleared.
5. Existing recorded expenses are preserved.
6. Final actual amount does not double-count previous actual expenses.
7. Planned amounts are never treated as actual spending.
8. NEW_TRIP while reconciliation pending -> old trip completed with skip semantics, new trip starts cleanly.
9. LOG_EXPENSE while reconciliation pending -> expense remains correctly associated with active trip.
10. RESCUE while reconciliation pending -> rescue still handled.
11. CHANGE_* while reconciliation pending -> existing active-trip behavior preserved.
12. Completed trip remains historical.
13. New trip does not inherit old trip state.
14. Existing NEW_TRIP behavior remains unchanged when no old trip is active.
15. Full lifecycle integration: plan -> confirm -> active -> expense -> day progression -> reoptimization -> rescue -> completion -> reconciliation -> completed -> new trip.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
import pytest

from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.ai.service import AIIntentService
from budlance.cache.manager import CacheFallbackManager
from budlance.db.models import BudgetAllocation, Itinerary, LedgerEntry, Trip, utc_now
from budlance.db.repositories.conversation_repo import ConversationStateRepository
from budlance.db.repositories.intent_repo import IntentRepository
from budlance.db.repositories.itinerary_repo import ItineraryRepository
from budlance.db.repositories.ledger_repo import LedgerRepository
from budlance.db.repositories.rescue_repo import RescueRepository
from budlance.db.repositories.trip_repo import TripRepository
from budlance.db.repositories.user_repo import UserRepository
from budlance.engine.budget import ReverseBudgetEngine
from budlance.engine.optimizer import OptimizationEngine
from budlance.estimation.estimator import EstimationLayer
from budlance.itinerary.generator import ItineraryGenerator
from budlance.itinerary.models import ItineraryDay, ItineraryItem
from budlance.ledger.manager import VirtualLedgerManager
from budlance.lifecycle.completion_handler import (
    CompletionResult,
    TripCompletionHandler,
    extract_reconciliation_amount,
    handle_trip_complete,
    is_new_trip_message,
    is_skip_response,
)
from budlance.lifecycle.expense_handler import ExpenseLifecycleHandler
from budlance.lifecycle.reoptimizer import reoptimize_remaining_trip
from budlance.normalization.normalizer import DataNormalizer
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.rescue.service import RescueService
from budlance.serpapi.models import DataSource, TravelDataEnvelope


@pytest.fixture
def memory_repos():
    """In-memory repositories for deterministic lifecycle testing."""
    return {
        "trip_repo": TripRepository(client=None),
        "ledger_repo": LedgerRepository(client=None),
        "itinerary_repo": ItineraryRepository(client=None),
        "conversation_repo": ConversationStateRepository(client=None),
    }


@pytest.fixture
def active_trip(memory_repos):
    """Set up an active 3-day trip with baseline allocation and line items."""
    user_id = uuid4()
    chat_id = 998877

    trip = memory_repos["trip_repo"].create_trip(
        user_id=user_id,
        telegram_chat_id=chat_id,
        budget_total=Decimal("20000.00"),
        destination="Goa",
        origin="Mumbai",
        currency="INR",
        people_count=2,
        duration_days=3,
        is_active=True,
    )
    memory_repos["trip_repo"].update_trip_status(trip.id, "ACTIVE")
    trip = memory_repos["trip_repo"].get_trip(trip.id)

    # Master allocation
    alloc = BudgetAllocation(
        id=uuid4(),
        trip_id=trip.id,
        transport_allocated=Decimal("6000.00"),
        stay_allocated=Decimal("6000.00"),
        food_allocated=Decimal("4000.00"),
        activities_discretionary=Decimal("2000.00"),
        rescue_fund_allocated=Decimal("2000.00"),
        total_budget=Decimal("20000.00"),
    )
    memory_repos["ledger_repo"].save_budget_allocation(alloc)

    # Baseline line items with planned amounts and actual_amount=None
    baseline = [
        LedgerEntry(
            id=uuid4(),
            trip_id=trip.id,
            category="fixed_booking",
            description="Flights Mumbai-Goa",
            allocated_amount=Decimal("6000.00"),
            planned_amount=Decimal("6000.00"),
            spent_amount=Decimal("0.00"),
            remaining_amount=Decimal("6000.00"),
            actual_amount=None,
            source="estimated",
            created_at=utc_now(),
        ),
        LedgerEntry(
            id=uuid4(),
            trip_id=trip.id,
            category="fixed_booking",
            description="Hotel Resort Goa",
            allocated_amount=Decimal("6000.00"),
            planned_amount=Decimal("6000.00"),
            spent_amount=Decimal("0.00"),
            remaining_amount=Decimal("6000.00"),
            actual_amount=None,
            source="estimated",
            created_at=utc_now(),
        ),
        LedgerEntry(
            id=uuid4(),
            trip_id=trip.id,
            category="daily_survival",
            description="Food Allowance",
            allocated_amount=Decimal("4000.00"),
            planned_amount=Decimal("4000.00"),
            spent_amount=Decimal("0.00"),
            remaining_amount=Decimal("4000.00"),
            actual_amount=None,
            source="estimated",
            created_at=utc_now(),
        ),
    ]
    for b in baseline:
        memory_repos["ledger_repo"].add_ledger_entry(b)

    # Itinerary days
    itin = Itinerary(
        id=uuid4(),
        trip_id=trip.id,
        days=[
            {"day_number": 1, "theme": "Arrival", "items": [], "estimated_cost": 2000, "status": "IN_PROGRESS"},
            {"day_number": 2, "theme": "Beach", "items": [], "estimated_cost": 2000, "status": "UPCOMING"},
            {"day_number": 3, "theme": "Departure", "items": [], "estimated_cost": 2000, "status": "UPCOMING"},
        ],
    )
    memory_repos["itinerary_repo"].save_itinerary(itin)

    return trip


@pytest.fixture
def completion_handler(memory_repos):
    """Instantiate TripCompletionHandler with memory repos."""
    return TripCompletionHandler(
        trip_repo=memory_repos["trip_repo"],
        ledger_repo=memory_repos["ledger_repo"],
        conversation_repo=memory_repos["conversation_repo"],
    )


# ============================================================================
# Test Cases
# ============================================================================

@pytest.mark.asyncio
async def test_1_trip_complete_with_active_trip_creates_prompt(completion_handler, active_trip, memory_repos):
    """1. TRIP_COMPLETE with active trip -> completion prompt created with pending state."""
    res = await completion_handler.handle_trip_complete(chat_id=active_trip.telegram_chat_id)

    assert res.status == "PENDING_RECONCILIATION"
    assert res.trip_id == active_trip.id
    assert "Your planned trip budget was ₹20,000" in res.message_text
    assert "Would you like to record your final actual spend? You can also say skip." in res.message_text

    # Conversation state must be set to LOG_ACTUAL_SPEND
    assert memory_repos["conversation_repo"].is_reconciling(active_trip.telegram_chat_id) is True
    assert memory_repos["conversation_repo"].get_reconciling_trip_id(active_trip.telegram_chat_id) == active_trip.id


@pytest.mark.asyncio
async def test_2_trip_complete_without_active_trip_safe_response(completion_handler):
    """2. TRIP_COMPLETE without active trip -> safe response."""
    res = await completion_handler.handle_trip_complete(chat_id=999999)

    assert res.status == "NO_ACTIVE_TRIP"
    assert res.trip_id is None
    assert "No active trip found" in res.message_text


@pytest.mark.asyncio
async def test_3_valid_final_actual_amount_reconciliation(completion_handler, active_trip, memory_repos):
    """3. Valid final actual amount -> reconciliation recorded correctly -> trip COMPLETED -> active pointer cleared."""
    chat_id = active_trip.telegram_chat_id
    # Initialize reconciliation prompt
    await completion_handler.handle_trip_complete(chat_id=chat_id)

    # Provide valid amount: ₹18,450
    rec_res = await completion_handler.handle_reconcile_amount(chat_id=chat_id, amount=Decimal("18450.00"))

    assert rec_res.status == "COMPLETED"
    assert rec_res.recorded_actual_spend == Decimal("18450.00")
    assert rec_res.planned_budget == Decimal("20000.00")
    assert rec_res.final_variance == Decimal("1550.00")
    assert "Difference: ₹1,550 under planned budget" in rec_res.message_text

    # Verify trip status and active pointer cleared
    trip = memory_repos["trip_repo"].get_trip(active_trip.id)
    assert trip.status == "COMPLETED"
    assert trip.is_active is False
    assert trip.completion_reason == "USER_CONFIRMED"
    assert memory_repos["trip_repo"].get_active_trip(chat_id) is None

    # Verify conversation state cleared
    assert memory_repos["conversation_repo"].is_reconciling(chat_id) is False


@pytest.mark.asyncio
async def test_4_skip_reconciliation(completion_handler, active_trip, memory_repos):
    """4. Skip reconciliation -> trip COMPLETED -> no fabricated actual total -> active pointer cleared."""
    chat_id = active_trip.telegram_chat_id
    await completion_handler.handle_trip_complete(chat_id=chat_id)

    # User says skip
    skip_res = await completion_handler.handle_skip_reconciliation(chat_id=chat_id)

    assert skip_res.status == "COMPLETED"
    assert skip_res.reconciliation_skipped is True
    assert skip_res.recorded_actual_spend == Decimal("0.00")  # No fake amount fabricated!
    assert "Final reconciliation: skipped" in skip_res.message_text

    # Verify trip status and active pointer cleared
    trip = memory_repos["trip_repo"].get_trip(active_trip.id)
    assert trip.status == "COMPLETED"
    assert trip.is_active is False
    assert trip.completion_reason == "USER_SKIPPED_RECONCILIATION"
    assert memory_repos["trip_repo"].get_active_trip(chat_id) is None


@pytest.mark.asyncio
async def test_5_existing_recorded_expenses_are_preserved(completion_handler, active_trip, memory_repos):
    """5. Existing recorded expenses are preserved during reconciliation."""
    chat_id = active_trip.telegram_chat_id

    # Record Day 1 and Day 2 expenses during the trip
    d1_entry = LedgerEntry(
        id=uuid4(),
        trip_id=active_trip.id,
        category="daily_survival",
        description="Day 1 Lunch",
        allocated_amount=Decimal("0.00"),
        planned_amount=Decimal("0.00"),
        spent_amount=Decimal("1200.00"),
        remaining_amount=Decimal("0.00"),
        actual_amount=Decimal("1200.00"),
        day_number=1,
        source="user_reported",
        created_at=utc_now(),
    )
    d2_entry = LedgerEntry(
        id=uuid4(),
        trip_id=active_trip.id,
        category="activities",
        description="Day 2 Museum",
        allocated_amount=Decimal("0.00"),
        planned_amount=Decimal("0.00"),
        spent_amount=Decimal("800.00"),
        remaining_amount=Decimal("0.00"),
        actual_amount=Decimal("800.00"),
        day_number=2,
        source="user_reported",
        created_at=utc_now(),
    )
    memory_repos["ledger_repo"].add_ledger_entry(d1_entry)
    memory_repos["ledger_repo"].add_ledger_entry(d2_entry)

    # Reconcile final actual spend of ₹18,450
    await completion_handler.handle_trip_complete(chat_id=chat_id)
    await completion_handler.handle_reconcile_amount(chat_id=chat_id, amount=Decimal("18450.00"))

    # Verify Day 1 and Day 2 rows still exist in the ledger untouched
    entries = memory_repos["ledger_repo"].get_ledger_entries(active_trip.id)
    descriptions = [e.description for e in entries]
    assert "Day 1 Lunch" in descriptions
    assert "Day 2 Museum" in descriptions


@pytest.mark.asyncio
async def test_6_final_actual_amount_does_not_double_count(completion_handler, active_trip, memory_repos):
    """6. Final actual amount does not double-count previous actual expenses."""
    chat_id = active_trip.telegram_chat_id

    # Add 2,000 and 3,000 previously recorded expenses (sum = 5,000)
    for desc, amt in [("Auto fare", Decimal("2000.00")), ("Scuba diving", Decimal("3000.00"))]:
        memory_repos["ledger_repo"].add_ledger_entry(
            LedgerEntry(
                id=uuid4(),
                trip_id=active_trip.id,
                category="activities",
                description=desc,
                allocated_amount=Decimal("0.00"),
                planned_amount=Decimal("0.00"),
                spent_amount=amt,
                remaining_amount=Decimal("0.00"),
                actual_amount=amt,
                source="user_reported",
                created_at=utc_now(),
            )
        )

    await completion_handler.handle_trip_complete(chat_id=chat_id)

    # User declares total final actual spend was ₹18,450
    rec_res = await completion_handler.handle_reconcile_amount(chat_id=chat_id, amount=Decimal("18450.00"))

    # Authoritative ledger total MUST be exactly 18,450 (NOT 5,000 + 18,450 = 23,450)
    all_entries = memory_repos["ledger_repo"].get_ledger_entries(active_trip.id)
    actual_spent_sum = sum(e.actual_amount for e in all_entries if e.actual_amount is not None)
    assert actual_spent_sum == Decimal("18450.00")
    assert rec_res.recorded_actual_spend == Decimal("18450.00")


@pytest.mark.asyncio
async def test_7_planned_amounts_never_treated_as_actual_spending(completion_handler, active_trip, memory_repos):
    """7. Planned amounts are never treated as actual spending."""
    chat_id = active_trip.telegram_chat_id

    # Trip has ₹16,000 total planned budget across baseline entries, but actual_amount=None for all
    res = await completion_handler.handle_trip_complete(chat_id=chat_id)
    assert res.recorded_actual_spend == Decimal("0.00")

    skip_res = await completion_handler.handle_skip_reconciliation(chat_id=chat_id)
    assert skip_res.recorded_actual_spend == Decimal("0.00")
    assert "Recorded actual spend: ₹0" in skip_res.message_text
    assert "₹20,000" not in skip_res.message_text.split("Recorded actual spend:")[1]


@pytest.mark.asyncio
async def test_8_new_trip_while_reconciliation_pending(memory_repos, active_trip):
    """8. NEW_TRIP while reconciliation pending -> old trip completed with skip semantics, new trip starts cleanly."""
    chat_id = active_trip.telegram_chat_id

    # Build orchestrator with mocked live search/LLM
    orch = _build_mock_orchestrator(memory_repos)

    # Step 1: User says trip complete -> reconciliation pending
    t1 = await orch.handle_user_message(telegram_user_id=1, chat_id=chat_id, message="Trip complete")
    assert t1.status == "PENDING_RECONCILIATION"
    assert memory_repos["conversation_repo"].is_reconciling(chat_id) is True

    # Step 2: User interrupts with NEW_TRIP
    t2 = await orch.handle_user_message(
        telegram_user_id=1,
        chat_id=chat_id,
        message="Plan a trip from Mumbai to Goa for 2 people, 3 days, with budget ₹25,000",
    )
    assert t2.status == "FEASIBLE"
    assert t2.trip_id is not None
    assert t2.trip_id != active_trip.id

    # Old trip must be COMPLETED with NEW_TRIP_STARTED
    old_trip = memory_repos["trip_repo"].get_trip(active_trip.id)
    assert old_trip.status == "COMPLETED"
    assert old_trip.is_active is False
    assert old_trip.completion_reason == "NEW_TRIP_STARTED"

    # New trip must be the active trip
    current_active = memory_repos["trip_repo"].get_active_trip(chat_id)
    assert current_active is not None
    assert current_active.id == t2.trip_id


@pytest.mark.asyncio
async def test_9_log_expense_while_reconciliation_pending(memory_repos, active_trip):
    """9. LOG_EXPENSE while reconciliation pending -> expense remains correctly associated with active trip."""
    chat_id = active_trip.telegram_chat_id
    orch = _build_mock_orchestrator(memory_repos)

    # Trigger reconciliation prompt
    await orch.handle_user_message(telegram_user_id=1, chat_id=chat_id, message="Trip complete")

    # User remembers an expense: "spent 500 on dinner"
    exp_res = await orch.handle_user_message(telegram_user_id=1, chat_id=chat_id, message="spent 500 on dinner")

    assert exp_res.status == "EXPENSE_LOGGED"
    assert exp_res.trip_id == active_trip.id

    # Expense is recorded in active trip's ledger
    entries = memory_repos["ledger_repo"].get_ledger_entries(active_trip.id)
    actuals = [e.actual_amount for e in entries if e.actual_amount is not None]
    assert Decimal("500.00") in actuals


@pytest.mark.asyncio
async def test_10_rescue_while_reconciliation_pending(memory_repos, active_trip):
    """10. RESCUE while reconciliation pending -> rescue still handled immediately."""
    chat_id = active_trip.telegram_chat_id
    orch = _build_mock_orchestrator(memory_repos)

    await orch.handle_user_message(telegram_user_id=1, chat_id=chat_id, message="Trip complete")

    # In-trip rescue emergency
    rescue_res = await orch.handle_user_message(
        telegram_user_id=1,
        chat_id=chat_id,
        message="auto driver is asking 600 rupees for 2 km ride",
    )
    assert rescue_res.status == "RESCUE"
    assert "Advisory Transit Fare Guidance" in rescue_res.message_text


@pytest.mark.asyncio
async def test_11_change_action_while_reconciliation_pending(memory_repos, active_trip):
    """11. CHANGE_* while reconciliation pending -> does not interpret change as reconciliation amount."""
    chat_id = active_trip.telegram_chat_id
    orch = _build_mock_orchestrator(memory_repos)

    await orch.handle_user_message(telegram_user_id=1, chat_id=chat_id, message="Trip complete")

    # User says "make it 4 days"
    change_res = await orch.handle_user_message(telegram_user_id=1, chat_id=chat_id, message="make it 4 days")
    # Must NOT complete trip or treat '4' as reconciliation amount
    assert change_res.status != "COMPLETED"
    trip = memory_repos["trip_repo"].get_trip(active_trip.id)
    assert trip.status == "ACTIVE"


@pytest.mark.asyncio
async def test_12_completed_trip_remains_historical(completion_handler, active_trip, memory_repos):
    """12. Completed trip remains historical data and is never deleted."""
    chat_id = active_trip.telegram_chat_id
    await completion_handler.handle_trip_complete(chat_id=chat_id)
    await completion_handler.handle_reconcile_amount(chat_id=chat_id, amount=Decimal("19000.00"))

    # Trip still exists in repository
    persisted_trip = memory_repos["trip_repo"].get_trip(active_trip.id)
    assert persisted_trip is not None
    assert persisted_trip.status == "COMPLETED"

    # Itinerary and ledger still exist
    itin = memory_repos["itinerary_repo"].get_itinerary(active_trip.id)
    assert itin is not None
    entries = memory_repos["ledger_repo"].get_ledger_entries(active_trip.id)
    assert len(entries) > 0


@pytest.mark.asyncio
async def test_13_new_trip_does_not_inherit_old_trip_state(memory_repos, active_trip):
    """13. New trip does not inherit old trip destination, budget, or people count."""
    chat_id = active_trip.telegram_chat_id
    orch = _build_mock_orchestrator(memory_repos)

    # Complete old trip
    await orch.handle_user_message(telegram_user_id=1, chat_id=chat_id, message="Trip complete")
    await orch.handle_user_message(telegram_user_id=1, chat_id=chat_id, message="skip")

    # Incomplete new request: "Plan a trip to Delhi" (missing budget, people, days, origin)
    res = await orch.handle_user_message(telegram_user_id=1, chat_id=chat_id, message="Plan a trip to Delhi")

    # Must ask for missing inputs, not silently inherit old trip's 2 people or ₹20,000 budget!
    assert res.status == "CLARIFICATION"
    pending = memory_repos["conversation_repo"].get_pending_intent(chat_id)
    assert pending is not None
    assert pending.destination == "Delhi"
    assert pending.budget is None  # Does NOT inherit ₹20,000!
    assert pending.people is None  # Does NOT inherit 2 people!


@pytest.mark.asyncio
async def test_14_existing_new_trip_unchanged_when_no_old_trip(memory_repos):
    """14. Existing NEW_TRIP behavior remains completely unchanged when no old trip is active."""
    orch = _build_mock_orchestrator(memory_repos)
    res = await orch.handle_user_message(
        telegram_user_id=2,
        chat_id=112233,
        message="Plan a trip from Mumbai to Goa for 2 people, 3 days, with budget ₹25,000",
    )
    assert res.status == "FEASIBLE"
    assert res.trip_id is not None
    assert res.selected_destination == "Goa"


@pytest.mark.asyncio
async def test_15_full_lifecycle_integration(memory_repos):
    """15. Full lifecycle integration:
    plan -> confirm -> active -> expense -> day progression -> reoptimization -> rescue -> completion -> reconciliation -> completed -> new trip.
    """
    chat_id = 778899
    user_id = 5566
    orch = _build_mock_orchestrator(memory_repos)

    # Step 1: Plan trip
    plan_res = await orch.handle_user_message(
        telegram_user_id=user_id,
        chat_id=chat_id,
        message="Plan a trip from Mumbai to Goa for 2 people, 3 days, with budget ₹25,000",
    )
    assert plan_res.status == "FEASIBLE"
    trip_id = plan_res.trip_id
    assert trip_id is not None

    # Step 2: Confirm booking / activate trip
    memory_repos["trip_repo"].update_trip_status(trip_id, "ACTIVE")
    active_trip = memory_repos["trip_repo"].get_trip(trip_id)
    assert active_trip.status == "ACTIVE"
    assert active_trip.current_day == 1

    # Step 3: Expense logging without day completion
    exp1 = await orch.handle_user_message(telegram_user_id=user_id, chat_id=chat_id, message="spent ₹500 on lunch")
    assert exp1.status == "EXPENSE_LOGGED"

    # Step 4: Expense logging WITH Day 1 completion -> advances to Day 2
    exp2 = await orch.handle_user_message(
        telegram_user_id=user_id,
        chat_id=chat_id,
        message="Day 1 is done, spent ₹2500 on dinner and cabs",
    )
    assert exp2.status == "EXPENSE_LOGGED"
    active_trip = memory_repos["trip_repo"].get_trip(trip_id)
    assert active_trip.current_day == 2

    # Step 5: Remaining-trip re-optimization check (Task 4 reoptimizer)
    opt_res = await reoptimize_remaining_trip(
        trip=active_trip,
        trip_repo=memory_repos["trip_repo"],
        ledger_repo=memory_repos["ledger_repo"],
        itinerary_repo=memory_repos["itinerary_repo"],
    )
    # Re-optimizer evaluates remaining days; Day 1 remains locked and immutable
    itin = memory_repos["itinerary_repo"].get_itinerary(trip_id)
    assert itin.days[0].get("status") == "COMPLETED"

    # Step 6: Rescue Mode handled during trip
    rescue_res = await orch.handle_user_message(
        telegram_user_id=user_id,
        chat_id=chat_id,
        message="auto driver is asking 600 rupees for 2 km ride",
    )
    assert rescue_res.status == "RESCUE"

    # Step 7: Explicit Trip Completion
    comp_res = await orch.handle_user_message(telegram_user_id=user_id, chat_id=chat_id, message="Trip complete")
    assert comp_res.status == "PENDING_RECONCILIATION"
    assert "Would you like to record your final actual spend? You can also say skip." in comp_res.message_text

    # Step 8: Final actual spend reconciliation
    final_res = await orch.handle_user_message(telegram_user_id=user_id, chat_id=chat_id, message="₹22,500")
    assert final_res.status == "COMPLETED"
    assert "Planned budget: ₹25,000" in final_res.message_text
    assert "Recorded actual spend: ₹22,500" in final_res.message_text
    assert "Difference: ₹2,500 under planned budget" in final_res.message_text

    # Completed trip is inactive and historical
    completed_trip = memory_repos["trip_repo"].get_trip(trip_id)
    assert completed_trip.status == "COMPLETED"
    assert completed_trip.is_active is False
    assert memory_repos["trip_repo"].get_active_trip(chat_id) is None

    # Step 9: Completely clean NEW_TRIP immediately after completion
    new_res = await orch.handle_user_message(
        telegram_user_id=user_id,
        chat_id=chat_id,
        message="Plan a trip from Mumbai to Goa for 2 people, 3 days, with budget ₹25,000",
    )
    assert new_res.status == "FEASIBLE"
    assert new_res.trip_id is not None
    assert new_res.trip_id != trip_id


# ============================================================================
# Helpers
# ============================================================================

def _build_mock_orchestrator(memory_repos):
    """Instantiate a fully wired BudlanceOrchestrator for offline integration tests."""
    mock_cache = MagicMock(spec=CacheFallbackManager)

    async def _mock_get_travel_data(engine, params, trip_id=None, **kwargs):
        if engine == "google_flights":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="flights_hash",
                data={"best_flights": [{"flights": [{"airline": "IndiGo"}], "price": 3000}]},
                is_fallback=False,
            )
        if engine == "google_hotels":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="hotels_hash",
                data={"properties": [{"name": "Goa Hotel", "rate_per_night": {"extracted_lowest": 1000}, "total_rate": {"extracted_lowest": 3000}}]},
                is_fallback=False,
            )
        if engine == "google_maps":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="maps_hash",
                data={"local_results": [{"title": "Calangute Beach", "type": "Beach", "rating": 4.5}]},
                is_fallback=False,
            )
        if engine == "google_maps_directions":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="directions_hash",
                data={"routes": [{"summary": "NH 66", "legs": [{"distance": {"text": "500 km", "value": 500000}, "duration": {"text": "8 hours", "value": 28800}}]}]},
                is_fallback=False,
            )
        return TravelDataEnvelope(
            source=DataSource.FALLBACK,
            engine=engine,
            query_hash="empty",
            data={},
            is_fallback=True,
        )

    mock_cache.get_travel_data = AsyncMock(side_effect=_mock_get_travel_data)

    user_repo = UserRepository(client=None)
    intent_repo = IntentRepository(client=None)
    rescue_repo = RescueRepository(client=None)

    ai_service = AIIntentService(use_mock=True)
    normalizer = DataNormalizer()
    estimation = EstimationLayer()
    budget_engine = ReverseBudgetEngine()
    optimizer = OptimizationEngine(budget_engine=budget_engine, estimation_layer=estimation)
    itin_gen = ItineraryGenerator(itinerary_repo=memory_repos["itinerary_repo"])
    ledger_mgr = VirtualLedgerManager(ledger_repo=memory_repos["ledger_repo"])

    rescue_service = RescueService(
        trip_repo=memory_repos["trip_repo"],
        itinerary_repo=memory_repos["itinerary_repo"],
        ledger_repo=memory_repos["ledger_repo"],
        rescue_repo=rescue_repo,
        ai_service=ai_service,
        cache_manager=mock_cache,
        normalizer=normalizer,
        budget_engine=budget_engine,
        estimation_layer=estimation,
        ledger_manager=ledger_mgr,
    )
    expense_handler = ExpenseLifecycleHandler(
        trip_repo=memory_repos["trip_repo"],
        ledger_repo=memory_repos["ledger_repo"],
        itinerary_repo=memory_repos["itinerary_repo"],
        ledger_manager=ledger_mgr,
    )
    completion_handler = TripCompletionHandler(
        trip_repo=memory_repos["trip_repo"],
        ledger_repo=memory_repos["ledger_repo"],
        conversation_repo=memory_repos["conversation_repo"],
        ledger_manager=ledger_mgr,
    )

    return BudlanceOrchestrator(
        user_repo=user_repo,
        trip_repo=memory_repos["trip_repo"],
        intent_repo=intent_repo,
        itinerary_repo=memory_repos["itinerary_repo"],
        ledger_repo=memory_repos["ledger_repo"],
        rescue_repo=rescue_repo,
        ai_service=ai_service,
        cache_manager=mock_cache,
        normalizer=normalizer,
        estimation_layer=estimation,
        budget_engine=budget_engine,
        optimizer=optimizer,
        itinerary_generator=itin_gen,
        ledger_manager=ledger_mgr,
        rescue_service=rescue_service,
        conversation_repo=memory_repos["conversation_repo"],
        expense_handler=expense_handler,
        completion_handler=completion_handler,
    )
