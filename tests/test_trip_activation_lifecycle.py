"""Budlance — Phase 4 Focused Test Suite.

Lifecycle Transition Verification: PLANNING → ACTIVE
Covers:
- Test A: New trip starts PLANNING (no accidental immediate activation)
- Test B: Booking confirmation activates same trip
- Test C: Persistence verified at repository boundary
- Test D: Active-trip lookup returns the same trip
- Test E: LOG_EXPENSE after activation works (no NO_ACTIVE_TRIP, saved to same trip)
- Test F: RESCUE after activation loads active trip and ledger
- Test G: Repeated confirmation is safe and idempotent (no duplicates, still ACTIVE)
- Test H: Booking confirmation with no valid planning trip handled safely
- Test I: Lifecycle compatibility (PLANNING -> ACTIVE -> TRIP_COMPLETE -> COMPLETED)
- Step 15: Realistic Conversation Simulation (Chennai -> Delhi, Flight, Booked, Expense, Rescue)
"""

from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest

from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.ai.service import AIIntentService
from budlance.attractions.selector import AttractionSelector
from budlance.cache.manager import CacheFallbackManager
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
from budlance.itinerary.enhancer import ItineraryEnhancer
from budlance.itinerary.generator import ItineraryGenerator
from budlance.ledger.manager import VirtualLedgerManager
from budlance.lifecycle.completion_handler import TripCompletionHandler
from budlance.lifecycle.expense_handler import ExpenseLifecycleHandler
from budlance.normalization.normalizer import DataNormalizer
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.rescue.service import RescueService


@pytest.fixture
def repos():
    """Create in-memory repositories."""
    user_repo = UserRepository(client=None)
    trip_repo = TripRepository(client=None)
    intent_repo = IntentRepository(client=None)
    itinerary_repo = ItineraryRepository(client=None)
    ledger_repo = LedgerRepository(client=None)
    rescue_repo = RescueRepository(client=None)
    conversation_repo = ConversationStateRepository(client=None)
    return {
        "user_repo": user_repo,
        "trip_repo": trip_repo,
        "intent_repo": intent_repo,
        "itinerary_repo": itinerary_repo,
        "ledger_repo": ledger_repo,
        "rescue_repo": rescue_repo,
        "conversation_repo": conversation_repo,
    }


@pytest.fixture
def orchestrator(repos):
    """Build orchestrator with in-memory repos and offline components."""
    ai_service = AIIntentService(use_mock=True)
    cache_manager = CacheFallbackManager()
    normalizer = DataNormalizer()
    estimation_layer = EstimationLayer()
    budget_engine = ReverseBudgetEngine()
    optimizer = OptimizationEngine(budget_engine=budget_engine, estimation_layer=estimation_layer)
    attraction_selector = AttractionSelector()
    itinerary_generator = ItineraryGenerator(
        itinerary_repo=repos["itinerary_repo"],
        attraction_selector=attraction_selector,
    )
    itinerary_enhancer = ItineraryEnhancer(use_mock=True)
    ledger_manager = VirtualLedgerManager(ledger_repo=repos["ledger_repo"])
    rescue_service = RescueService(
        trip_repo=repos["trip_repo"],
        itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"],
        rescue_repo=repos["rescue_repo"],
        ai_service=ai_service,
        cache_manager=cache_manager,
        normalizer=normalizer,
        budget_engine=budget_engine,
        estimation_layer=estimation_layer,
        ledger_manager=ledger_manager,
    )
    expense_handler = ExpenseLifecycleHandler(
        trip_repo=repos["trip_repo"],
        ledger_repo=repos["ledger_repo"],
        ledger_manager=ledger_manager,
    )
    completion_handler = TripCompletionHandler(
        trip_repo=repos["trip_repo"],
        ledger_repo=repos["ledger_repo"],
        conversation_repo=repos["conversation_repo"],
        ledger_manager=ledger_manager,
    )

    return BudlanceOrchestrator(
        user_repo=repos["user_repo"],
        trip_repo=repos["trip_repo"],
        intent_repo=repos["intent_repo"],
        itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"],
        rescue_repo=repos["rescue_repo"],
        conversation_repo=repos["conversation_repo"],
        ai_service=ai_service,
        cache_manager=cache_manager,
        normalizer=normalizer,
        estimation_layer=estimation_layer,
        budget_engine=budget_engine,
        optimizer=optimizer,
        itinerary_generator=itinerary_generator,
        itinerary_enhancer=itinerary_enhancer,
        ledger_manager=ledger_manager,
        rescue_service=rescue_service,
        expense_handler=expense_handler,
        completion_handler=completion_handler,
    )


# ============================================================================
# Test A — New trip starts in PLANNING
# ============================================================================
def test_a_new_trip_starts_planning(repos):
    """Test A: Newly created trip has status=PLANNING, protecting against accidental immediate activation."""
    trip_repo = repos["trip_repo"]
    user_id = uuid4()
    chat_id = 10001

    trip = trip_repo.create_trip(
        user_id=user_id,
        telegram_chat_id=chat_id,
        budget_total=Decimal("25000.00"),
        destination="Goa",
        origin="Mumbai",
        duration_days=3,
        people_count=2,
    )

    assert trip.status == "PLANNING"
    persisted = trip_repo.get_trip(trip.id)
    assert persisted is not None
    assert persisted.status == "PLANNING"


# ============================================================================
# Test B — Booking confirmation activates the same trip
# ============================================================================
@pytest.mark.asyncio
async def test_b_booking_confirmation_activates_same_trip(orchestrator, repos):
    """Test B: CONFIRM_BOOKING transitions existing trip from PLANNING to ACTIVE on the SAME trip."""
    trip_repo = repos["trip_repo"]
    user_repo = repos["user_repo"]
    chat_id = 10002

    user = user_repo.get_or_create_user(telegram_user_id=chat_id)
    trip = trip_repo.create_trip(
        user_id=user.id,
        telegram_chat_id=chat_id,
        budget_total=Decimal("30000.00"),
        destination="Delhi",
        origin="Chennai",
        duration_days=3,
        people_count=2,
        status="PLANNING",
        is_active=True,
    )
    trip_id_x = trip.id
    assert trip.status == "PLANNING"

    # User sends "Booked"
    orchestrator.ai_service.parse_trip_intent_with_context = AsyncMock(
        return_value=ParsedTripIntent(action=TripAction.CONFIRM_BOOKING, booking_confirmed=True)
    )

    res = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Booked",
    )

    assert res.trip_id == trip_id_x
    updated = trip_repo.get_trip(trip_id_x)
    assert updated is not None
    assert updated.status == "ACTIVE"
    assert updated.is_active is True


# ============================================================================
# Test C — Persistence at repository/database boundary
# ============================================================================
@pytest.mark.asyncio
async def test_c_persistence_at_repository_boundary(orchestrator, repos):
    """Test C: ACTIVE state is persisted to the repository boundary."""
    trip_repo = repos["trip_repo"]
    user_repo = repos["user_repo"]
    chat_id = 10003

    user = user_repo.get_or_create_user(telegram_user_id=chat_id)
    trip = trip_repo.create_trip(
        user_id=user.id,
        telegram_chat_id=chat_id,
        budget_total=Decimal("50000.00"),
        destination="Jaipur",
        origin="Delhi",
        duration_days=2,
        people_count=2,
        status="PLANNING",
        is_active=True,
    )

    # Before confirmation
    assert trip_repo.get_trip(trip.id).status == "PLANNING"

    orchestrator.ai_service.parse_trip_intent_with_context = AsyncMock(
        return_value=ParsedTripIntent(action=TripAction.CONFIRM_BOOKING, booking_confirmed=True)
    )

    await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="I have booked it",
    )

    # After confirmation: reloaded directly from repository
    persisted = trip_repo.get_trip(trip.id)
    assert persisted is not None
    assert persisted.status == "ACTIVE"
    assert persisted.is_active is True


# ============================================================================
# Test D — Active-trip lookup returns same trip
# ============================================================================
@pytest.mark.asyncio
async def test_d_active_trip_lookup(orchestrator, repos):
    """Test D: get_active_trip(chat_id) returns the exact same trip after activation."""
    trip_repo = repos["trip_repo"]
    user_repo = repos["user_repo"]
    chat_id = 10004

    user = user_repo.get_or_create_user(telegram_user_id=chat_id)
    trip = trip_repo.create_trip(
        user_id=user.id,
        telegram_chat_id=chat_id,
        budget_total=Decimal("45000.00"),
        destination="Goa",
        origin="Bangalore",
        duration_days=4,
        people_count=2,
        status="PLANNING",
        is_active=True,
    )

    orchestrator.ai_service.parse_trip_intent_with_context = AsyncMock(
        return_value=ParsedTripIntent(action=TripAction.CONFIRM_BOOKING, booking_confirmed=True)
    )

    await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Booked.",
    )

    active_trip = trip_repo.get_active_trip(chat_id)
    assert active_trip is not None
    assert active_trip.id == trip.id
    assert active_trip.status == "ACTIVE"
    assert active_trip.is_active is True


# ============================================================================
# Test E — LOG_EXPENSE after activation
# ============================================================================
@pytest.mark.asyncio
async def test_e_log_expense_after_activation(orchestrator, repos):
    """Test E: Once ACTIVE, user can log expenses without NO_ACTIVE_TRIP error."""
    trip_repo = repos["trip_repo"]
    user_repo = repos["user_repo"]
    chat_id = 10005

    user = user_repo.get_or_create_user(telegram_user_id=chat_id)
    trip = trip_repo.create_trip(
        user_id=user.id,
        telegram_chat_id=chat_id,
        budget_total=Decimal("40000.00"),
        destination="Delhi",
        origin="Chennai",
        duration_days=3,
        people_count=2,
        status="PLANNING",
        is_active=True,
    )

    # 1. Before activation: Expense logging is rejected with NO_ACTIVE_TRIP
    expense_before = ParsedTripIntent(
        action=TripAction.LOG_EXPENSE,
        amount=Decimal("1800.00"),
        expense_category="food",
    )
    orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=expense_before)

    res_before = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Spent ₹1800 on lunch",
    )
    assert res_before.status == "NO_ACTIVE_TRIP"

    # 2. Activate the trip
    booked_intent = ParsedTripIntent(action=TripAction.CONFIRM_BOOKING, booking_confirmed=True)
    orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=booked_intent)
    orchestrator.ai_service.parse_trip_intent_with_context = AsyncMock(return_value=booked_intent)
    res_book = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Booked",
    )
    assert res_book.trip_id == trip.id

    # 3. After activation: Expense logging succeeds on the active trip
    expense_after = ParsedTripIntent(
        action=TripAction.LOG_EXPENSE,
        amount=Decimal("1800.00"),
        expense_category="food",
    )
    orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=expense_after)
    orchestrator.ai_service.parse_trip_intent_with_context = AsyncMock(return_value=expense_after)

    res_after = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="I spent ₹1800 on food today.",
    )

    assert res_after.status == "EXPENSE_LOGGED"
    assert res_after.trip_id == trip.id
    assert "₹1,800" in res_after.message_text

    # Verify expense is persisted in the ledger
    ledger_entries = repos["ledger_repo"].get_ledger_entries(trip.id)
    food_actuals = [e for e in ledger_entries if e.actual_amount == Decimal("1800.00")]
    assert len(food_actuals) == 1


# ============================================================================
# Test F — RESCUE after activation
# ============================================================================
@pytest.mark.asyncio
async def test_f_rescue_after_activation(orchestrator, repos):
    """Test F: After activation, RESCUE finds the active trip and ledger."""
    from budlance.itinerary.models import ItineraryDay, ItineraryItem
    from budlance.db.models import Itinerary as ItineraryModel
    from budlance.engine.models import DataSource

    trip_repo = repos["trip_repo"]
    user_repo = repos["user_repo"]
    chat_id = 10006

    user = user_repo.get_or_create_user(telegram_user_id=chat_id)
    trip = trip_repo.create_trip(
        user_id=user.id,
        telegram_chat_id=chat_id,
        budget_total=Decimal("40000.00"),
        destination="Goa",
        origin="Mumbai",
        duration_days=3,
        people_count=2,
        status="PLANNING",
        is_active=True,
    )

    # Attach baseline itinerary so rescue has an active plan to replan
    day1_item = ItineraryItem(
        time_slot="Morning",
        activity="Visit Beach",
        place_name="Beach",
        category="beach",
        planned_cost=Decimal("0.00"),
        source=DataSource.LIVE,
    )
    itin_day = ItineraryDay(day_number=1, theme_or_summary="Beach day", items=[day1_item])
    repos["itinerary_repo"].save_itinerary(
        ItineraryModel(
            trip_id=trip.id,
            days=[itin_day.model_dump(mode="json")],
        )
    )

    # Activate trip
    booked_intent = ParsedTripIntent(
        action=TripAction.CONFIRM_BOOKING,
        booking_confirmed=True,
    )
    orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=booked_intent)
    orchestrator.ai_service.parse_trip_intent_with_context = AsyncMock(return_value=booked_intent)
    res_book = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Booked",
    )
    assert res_book.trip_id == trip.id

    # Request rescue
    rescue_intent = ParsedTripIntent(
        action=TripAction.RESCUE,
        rescue_detail="Museum closed due to rain",
    )
    orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=rescue_intent)
    orchestrator.ai_service.parse_trip_intent_with_context = AsyncMock(return_value=rescue_intent)

    res_rescue = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Rescue: Museum closed due to rain",
    )

    assert res_rescue.status == "RESCUE"
    assert res_rescue.trip_id == trip.id
    assert res_rescue.error != "NO_ACTIVE_TRIP"


# ============================================================================
# Test G — Repeated confirmation is idempotent
# ============================================================================
@pytest.mark.asyncio
async def test_g_repeated_confirmation(orchestrator, repos):
    """Test G: Second CONFIRM_BOOKING does not create duplicate trip or corrupt status."""
    trip_repo = repos["trip_repo"]
    user_repo = repos["user_repo"]
    chat_id = 10007

    user = user_repo.get_or_create_user(telegram_user_id=chat_id)
    trip = trip_repo.create_trip(
        user_id=user.id,
        telegram_chat_id=chat_id,
        budget_total=Decimal("20000.00"),
        destination="Ooty",
        origin="Coimbatore",
        duration_days=2,
        people_count=2,
        status="PLANNING",
        is_active=True,
    )

    orchestrator.ai_service.parse_trip_intent_with_context = AsyncMock(
        return_value=ParsedTripIntent(action=TripAction.CONFIRM_BOOKING, booking_confirmed=True)
    )

    # First "Booked"
    res1 = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Booked",
    )
    assert res1.trip_id == trip.id
    assert trip_repo.get_trip(trip.id).status == "ACTIVE"

    # Second "Booked"
    res2 = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Booked",
    )

    # Verify same trip returned, status remains ACTIVE, no crash
    assert res2.trip_id == trip.id
    assert res2.status == "ACTIVE"
    assert "already active" in res2.message_text.lower()
    assert trip_repo.get_trip(trip.id).status == "ACTIVE"


# ============================================================================
# Test H — CONFIRM_BOOKING with no valid planning trip
# ============================================================================
@pytest.mark.asyncio
async def test_h_no_planning_trip(orchestrator, repos):
    """Test H: CONFIRM_BOOKING with no valid planning trip returns safe response without activating anything."""
    chat_id = 10008
    orchestrator.ai_service.parse_trip_intent = AsyncMock(
        return_value=ParsedTripIntent(action=TripAction.CONFIRM_BOOKING, booking_confirmed=True)
    )

    res = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Booked",
    )

    assert res.status == "NO_PLANNING_TRIP"
    assert res.trip_id is None
    assert "couldn't find a planned trip" in res.message_text.lower()

    # Verify no active trip was erroneously created
    assert repos["trip_repo"].get_active_trip(chat_id) is None


# ============================================================================
# Test I — Lifecycle compatibility with TRIP_COMPLETE
# ============================================================================
@pytest.mark.asyncio
async def test_i_lifecycle_compatibility(orchestrator, repos):
    """Test I: PLANNING -> ACTIVE -> existing TRIP_COMPLETE -> COMPLETED without breaking completion logic."""
    trip_repo = repos["trip_repo"]
    user_repo = repos["user_repo"]
    chat_id = 10009

    user = user_repo.get_or_create_user(telegram_user_id=chat_id)
    trip = trip_repo.create_trip(
        user_id=user.id,
        telegram_chat_id=chat_id,
        budget_total=Decimal("30000.00"),
        destination="Delhi",
        origin="Chennai",
        duration_days=3,
        people_count=2,
        status="PLANNING",
        is_active=True,
    )

    # 1. PLANNING status
    assert trip_repo.get_trip(trip.id).status == "PLANNING"

    # 2. Transition to ACTIVE
    orchestrator.ai_service.parse_trip_intent_with_context = AsyncMock(
        return_value=ParsedTripIntent(action=TripAction.CONFIRM_BOOKING, booking_confirmed=True)
    )
    await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Booked",
    )
    assert trip_repo.get_trip(trip.id).status == "ACTIVE"

    # 3. Trigger TRIP_COMPLETE
    orchestrator.ai_service.parse_trip_intent = AsyncMock(
        return_value=ParsedTripIntent(action=TripAction.TRIP_COMPLETE)
    )
    res_comp = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Trip is completed",
    )
    assert res_comp.trip_id == trip.id
    assert res_comp.status == "PENDING_RECONCILIATION"

    # 4. Skip reconciliation to finalize
    orchestrator.ai_service.parse_trip_intent = AsyncMock(
        return_value=ParsedTripIntent(action=TripAction.UNRECOGNIZED)
    )
    res_final = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Skip",
    )
    assert res_final.status == "COMPLETED"

    # 5. Verify status is COMPLETED and is_active is False
    final_trip = trip_repo.get_trip(trip.id)
    assert final_trip.status == "COMPLETED"
    assert final_trip.is_active is False
    assert trip_repo.get_active_trip(chat_id) is None


# ============================================================================
# Step 15 — Realistic Conversation Simulation
# ============================================================================
@pytest.mark.asyncio
async def test_step15_realistic_conversation_simulation(orchestrator, repos):
    """Step 15: Realistic Conversation Simulation using actual Budlance flow.

    Conversation:
    1. User: "I want to go from Chennai to Delhi. Budget ₹100000, 2 people, 3 days."
       Bot: Clarifies transport (train or flight).
    2. User: "Flight"
       Bot: Evaluates transport feasibility, creates trip in PLANNING, returns booking handoff.
    3. User: "Booked"
       Bot: CONFIRM_BOOKING detected, transitions trip to ACTIVE, preserves same trip,
            returns plan, no fake booking verification claimed.
    4. User: "I spent ₹1800 on food today."
       Bot: Active trip found, expense saved to same trip.
    5. User: "Rescue"
       Bot: Active confirmed trip loaded, current ledger loaded.
    """
    chat_id = 10010
    trip_repo = repos["trip_repo"]

    # Turn 1: Initial trip request
    t1_intent = ParsedTripIntent(
        action=TripAction.NEW_TRIP,
        origin="Chennai",
        destination="Delhi",
        budget=Decimal("100000.00"),
        people=2,
        days=3,
        currency="INR",
    )
    orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=t1_intent)

    res1 = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="I want to go from Chennai to Delhi. Budget ₹100000, 2 people, 3 days, interested in sunrise, India Gate and nearby famous places.",
    )
    assert res1.status == "CLARIFICATION"
    assert "train or flight" in res1.message_text.lower()

    # Turn 2: Transport selection
    t2_intent = ParsedTripIntent(
        action=TripAction.CHANGE_TRANSPORT,
        transport_mode="flight",
        transport_class="economy",
    )
    orchestrator.ai_service.parse_trip_intent_with_context = AsyncMock(return_value=t2_intent)

    res2 = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Flight",
    )
    assert res2.status == "FEASIBLE_TRANSPORT"
    assert res2.trip_id is not None
    initial_trip_id = res2.trip_id

    # 1. Verify trip was initially PLANNING
    trip_initial = trip_repo.get_trip(initial_trip_id)
    assert trip_initial is not None
    assert trip_initial.status == "PLANNING"
    assert trip_initial.is_active is True

    # Turn 3: User completes external booking and confirms ("Booked")
    t3_intent = ParsedTripIntent(
        action=TripAction.CONFIRM_BOOKING,
        booking_confirmed=True,
    )
    orchestrator.ai_service.parse_trip_intent_with_context = AsyncMock(return_value=t3_intent)

    res3 = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Booked",
    )

    # 2. User's selected trip remains the SAME trip
    assert res3.trip_id == initial_trip_id
    assert res3.status == "FEASIBLE"

    # 3 & 4 & 5 & 6: Trip transitioned to ACTIVE, is_active=True
    trip_active = trip_repo.get_trip(initial_trip_id)
    assert trip_active.status == "ACTIVE"
    assert trip_active.is_active is True

    # 7. Active-trip lookup finds the same trip
    lookup_trip = trip_repo.get_active_trip(chat_id)
    assert lookup_trip is not None
    assert lookup_trip.id == initial_trip_id
    assert lookup_trip.status == "ACTIVE"

    # 10. Truthful response verification: no fabricated booking claims
    msg_lower = res3.message_text.lower()
    for forbidden in ["ticket has been verified", "successfully booked your ticket", "pnr confirmed"]:
        assert forbidden not in msg_lower

    # Turn 4: User logs an expense
    t4_intent = ParsedTripIntent(
        action=TripAction.LOG_EXPENSE,
        amount=Decimal("1800.00"),
        expense_category="food",
    )
    orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=t4_intent)

    res4 = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="I spent ₹1800 on food today.",
    )

    # 8. LOG_EXPENSE works afterward on the same trip
    assert res4.status == "EXPENSE_LOGGED"
    assert res4.trip_id == initial_trip_id

    # Turn 5: User requests rescue
    t5_intent = ParsedTripIntent(
        action=TripAction.RESCUE,
        rescue_detail="Need budget rescue",
    )
    orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=t5_intent)

    res5 = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Rescue",
    )

    # 9. RESCUE works afterward on the active trip
    assert res5.status == "RESCUE"
    assert res5.trip_id == initial_trip_id
    assert res5.error is None
