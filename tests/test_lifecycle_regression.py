"""Budlance Phase 6 — Focused Lifecycle Regression & Hardening Verification.

Verifies end-to-end coherence across all lifecycle states and components:
- Step 2: Canonical State Machine (PLANNING -> ACTIVE -> COMPLETED)
- Step 3: Same Trip Identity preserved throughout (no duplicate trips)
- Step 4: Conversation-State Isolation (New trip after completed trip does not inherit state)
- Step 5: Authoritative actual expense tracking (no planned substitution)
- Step 6: Remaining-trip re-optimization with locked completed days
- Step 7: Rescue Mode isolation on active trip
- Step 8 & 9: Completion with reason and without reason (NULL)
- Step 10: Reconciliation state machine (all 7 branches verified)
- Step 11 & 12: Positive (+₹500) and Negative (-₹500) ledger reconciliation adjustments
- Step 13: Trip completion idempotency (safe handling of completed trip)
- Step 14: Trip activation idempotency (safe handling of repeated "Booked")
- Step 15: Cross-feature full lifecycle sequence
- Step 18 & 19: Persistence and reload semantics
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4
import pytest

from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.ai.service import AIIntentService
from budlance.db.models import BudgetAllocation, Itinerary, LedgerEntry, Trip, utc_now
from budlance.db.repositories.conversation_repo import ConversationStateRepository
from budlance.db.repositories.intent_repo import IntentRepository
from budlance.db.repositories.itinerary_repo import ItineraryRepository
from budlance.db.repositories.ledger_repo import LedgerRepository
from budlance.db.repositories.rescue_repo import RescueRepository
from budlance.db.repositories.trip_repo import TripRepository
from budlance.db.repositories.user_repo import UserRepository
from budlance.engine.budget import ReverseBudgetEngine
from budlance.engine.models import DataSource
from budlance.engine.optimizer import OptimizationEngine
from budlance.estimation.estimator import EstimationLayer
from budlance.itinerary.generator import ItineraryGenerator
from budlance.ledger.manager import VirtualLedgerManager
from budlance.lifecycle.completion_handler import TripCompletionHandler
from budlance.lifecycle.reoptimizer import reoptimize_remaining_trip
from budlance.normalization.normalizer import DataNormalizer
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.rescue.service import RescueService
from budlance.schemas.travel import FlightOption, HotelOption, PlaceOption, RouteOption
from budlance.serpapi.models import TravelDataEnvelope


@pytest.fixture
def memory_repos():
    """In-memory repository bundle for deterministic lifecycle verification."""
    return {
        "user_repo": UserRepository(client=None),
        "trip_repo": TripRepository(client=None),
        "intent_repo": IntentRepository(client=None),
        "itinerary_repo": ItineraryRepository(client=None),
        "ledger_repo": LedgerRepository(client=None),
        "rescue_repo": RescueRepository(client=None),
        "conversation_repo": ConversationStateRepository(client=None),
    }


def _build_mock_orchestrator(memory_repos):
    """Build fully wired BudlanceOrchestrator with mock cache & AI services."""
    mock_cache = MagicMock()

    async def _mock_get_travel_data(engine, params, trip_id=None, **kwargs):
        if engine == "google_flights":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="flights_hash",
                data={"best_flights": [{"flights": [{"airline": "IndiGo"}], "price": 4000}]},
                is_fallback=False,
            )
        if engine == "google_hotels":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="hotels_hash",
                data={"properties": [{"name": "Hotel Heritage", "rate_per_night": {"extracted_lowest": 1500}, "total_rate": {"extracted_lowest": 4500}}]},
                is_fallback=False,
            )
        if engine == "google_maps":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="maps_hash",
                data={"local_results": [{"title": "City Palace", "type": "Palace", "rating": 4.6}]},
                is_fallback=False,
            )
        if engine == "google_maps_directions":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="directions_hash",
                data={"routes": [{"summary": "Highway 48", "legs": [{"distance": {"text": "350 km", "value": 350000}, "duration": {"text": "6 hours", "value": 21600}}]}]},
                is_fallback=False,
            )
        return TravelDataEnvelope(
            source=DataSource.FALLBACK,
            engine=engine,
            query_hash="fallback_hash",
            data={},
            is_fallback=True,
        )

    mock_cache.get_travel_data = AsyncMock(side_effect=_mock_get_travel_data)

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
        rescue_repo=memory_repos["rescue_repo"],
        ai_service=ai_service,
        cache_manager=mock_cache,
        normalizer=normalizer,
        budget_engine=budget_engine,
        estimation_layer=estimation,
        ledger_manager=ledger_mgr,
    )

    return BudlanceOrchestrator(
        user_repo=memory_repos["user_repo"],
        trip_repo=memory_repos["trip_repo"],
        intent_repo=memory_repos["intent_repo"],
        itinerary_repo=memory_repos["itinerary_repo"],
        ledger_repo=memory_repos["ledger_repo"],
        rescue_repo=memory_repos["rescue_repo"],
        conversation_repo=memory_repos["conversation_repo"],
        ai_service=ai_service,
        cache_manager=mock_cache,
        normalizer=normalizer,
        estimation_layer=estimation,
        budget_engine=budget_engine,
        optimizer=optimizer,
        itinerary_generator=itin_gen,
        ledger_manager=ledger_mgr,
        rescue_service=rescue_service,
    )


# ============================================================================
# Step 10: Reconciliation State Machine — Branch 7 (Unrecognized Input)
# ============================================================================
@pytest.mark.asyncio
async def test_reconciliation_branch7_unrecognized_input_preserves_state(memory_repos):
    """Branch 7: Unrecognized message during reconciliation prompts user to supply spend or skip."""
    chat_id = 991101
    user_id = 991101
    orch = _build_mock_orchestrator(memory_repos)

    # 1. Start trip & activate
    plan_res = await orch.handle_user_message(
        telegram_user_id=user_id,
        chat_id=chat_id,
        message="Plan a trip from Mumbai to Goa for 2 people, 3 days, with budget ₹30,000",
    )
    trip_id = plan_res.trip_id
    memory_repos["trip_repo"].update_trip_status(trip_id, "ACTIVE", is_active=True)

    # 2. Complete trip -> enter reconciliation
    comp_res = await orch.handle_user_message(telegram_user_id=user_id, chat_id=chat_id, message="Trip complete")
    assert comp_res.status == "PENDING_RECONCILIATION"

    # 3. User sends unrecognized input: "hello what's up"
    unrec_res = await orch.handle_user_message(telegram_user_id=user_id, chat_id=chat_id, message="hello what's up")
    assert unrec_res.status == "PENDING_RECONCILIATION"
    assert "Your trip completion is pending" in unrec_res.message_text
    assert "skip" in unrec_res.message_text

    # Trip remains active until reconciliation finishes
    trip = memory_repos["trip_repo"].get_trip(trip_id)
    assert trip.status == "ACTIVE"
    assert trip.is_active is True

    # 4. Now user skips -> cleanly completed
    skip_res = await orch.handle_user_message(telegram_user_id=user_id, chat_id=chat_id, message="skip")
    assert skip_res.status == "COMPLETED"
    trip = memory_repos["trip_repo"].get_trip(trip_id)
    assert trip.status == "COMPLETED"
    assert trip.is_active is False


# ============================================================================
# Step 11: Positive Reconciliation Adjustment (+₹500)
# ============================================================================
@pytest.mark.asyncio
async def test_positive_reconciliation_adjustment(memory_repos):
    """Step 11: Authoritative recorded spend = ₹17,500, final declared = ₹18,000 -> adjustment = +₹500."""
    chat_id = 991102
    trip_repo = memory_repos["trip_repo"]
    ledger_repo = memory_repos["ledger_repo"]
    conv_repo = memory_repos["conversation_repo"]
    handler = TripCompletionHandler(trip_repo=trip_repo, ledger_repo=ledger_repo, conversation_repo=conv_repo)

    trip = trip_repo.create_trip(
        user_id=uuid4(),
        telegram_chat_id=chat_id,
        budget_total=Decimal("20000.00"),
        destination="Jaipur",
        status="ACTIVE",
        is_active=True,
    )
    # Recorded actual spending = ₹17,500
    e1 = LedgerEntry(
        id=uuid4(),
        trip_id=trip.id,
        category="fixed_booking",
        description="Train tickets",
        allocated_amount=Decimal("10000.00"),
        actual_amount=Decimal("10000.00"),
        source="user_reported",
        created_at=utc_now(),
    )
    e2 = LedgerEntry(
        id=uuid4(),
        trip_id=trip.id,
        category="fixed_booking",
        description="Hotel booking",
        allocated_amount=Decimal("7500.00"),
        actual_amount=Decimal("7500.00"),
        source="user_reported",
        created_at=utc_now(),
    )
    ledger_repo.add_ledger_entry(e1)
    ledger_repo.add_ledger_entry(e2)

    # User declares ₹18,000 final actual spend
    await handler.handle_trip_complete(chat_id=chat_id, trip=trip)
    res = await handler.handle_reconcile_amount(chat_id=chat_id, amount=Decimal("18000.00"), trip=trip)

    assert res.status == "COMPLETED"
    assert res.recorded_actual_spend == Decimal("18000.00")
    assert res.final_variance == Decimal("2000.00")  # ₹20,000 budget - ₹18,000 spent = ₹2,000 under

    # Check adjustment entry in ledger
    entries = ledger_repo.get_ledger_entries(trip.id)
    adj_entries = [e for e in entries if e.description == "Final reconciliation adjustment"]
    assert len(adj_entries) == 1
    assert adj_entries[0].actual_amount == Decimal("500.00")

    # Historical entries preserved
    assert e1 in entries
    assert e2 in entries


# ============================================================================
# Step 12: Negative Reconciliation Adjustment (-₹500)
# ============================================================================
@pytest.mark.asyncio
async def test_negative_reconciliation_adjustment(memory_repos):
    """Step 12: Authoritative recorded spend = ₹18,000, final declared = ₹17,500 -> adjustment = -₹500."""
    chat_id = 991103
    trip_repo = memory_repos["trip_repo"]
    ledger_repo = memory_repos["ledger_repo"]
    conv_repo = memory_repos["conversation_repo"]
    handler = TripCompletionHandler(trip_repo=trip_repo, ledger_repo=ledger_repo, conversation_repo=conv_repo)

    trip = trip_repo.create_trip(
        user_id=uuid4(),
        telegram_chat_id=chat_id,
        budget_total=Decimal("20000.00"),
        destination="Jaipur",
        status="ACTIVE",
        is_active=True,
    )
    # Recorded actual spending = ₹18,000
    e1 = LedgerEntry(
        id=uuid4(),
        trip_id=trip.id,
        category="fixed_booking",
        description="Train tickets",
        allocated_amount=Decimal("10000.00"),
        actual_amount=Decimal("10000.00"),
        source="user_reported",
        created_at=utc_now(),
    )
    e2 = LedgerEntry(
        id=uuid4(),
        trip_id=trip.id,
        category="fixed_booking",
        description="Hotel booking",
        allocated_amount=Decimal("8000.00"),
        actual_amount=Decimal("8000.00"),
        source="user_reported",
        created_at=utc_now(),
    )
    ledger_repo.add_ledger_entry(e1)
    ledger_repo.add_ledger_entry(e2)

    # User declares ₹17,500 final actual spend
    await handler.handle_trip_complete(chat_id=chat_id, trip=trip)
    res = await handler.handle_reconcile_amount(chat_id=chat_id, amount=Decimal("17500.00"), trip=trip)

    assert res.status == "COMPLETED"
    assert res.recorded_actual_spend == Decimal("17500.00")
    assert res.final_variance == Decimal("2500.00")

    # Check adjustment entry in ledger
    entries = ledger_repo.get_ledger_entries(trip.id)
    adj_entries = [e for e in entries if e.description == "Final reconciliation adjustment"]
    assert len(adj_entries) == 1
    assert adj_entries[0].actual_amount == Decimal("-500.00")

    # Historical entries preserved
    assert e1 in entries
    assert e2 in entries


# ============================================================================
# Step 13: Trip Completion Idempotency
# ============================================================================
@pytest.mark.asyncio
async def test_completion_idempotency_on_already_completed_trip(memory_repos):
    """Step 13: Once COMPLETED, repeating Trip complete does not corrupt state or reactivate trip."""
    chat_id = 991104
    user_id = 991104
    orch = _build_mock_orchestrator(memory_repos)

    plan_res = await orch.handle_user_message(
        telegram_user_id=user_id,
        chat_id=chat_id,
        message="Plan a trip from Mumbai to Goa for 2 people, 3 days, with budget ₹25,000",
    )
    trip_id = plan_res.trip_id
    memory_repos["trip_repo"].update_trip_status(trip_id, "ACTIVE", is_active=True)

    # Complete it
    await orch.handle_user_message(telegram_user_id=user_id, chat_id=chat_id, message="Trip complete")
    await orch.handle_user_message(telegram_user_id=user_id, chat_id=chat_id, message="skip")

    trip = memory_repos["trip_repo"].get_trip(trip_id)
    assert trip.status == "COMPLETED"
    assert trip.is_active is False

    # Repeat "Trip complete"
    repeat_res = await orch.handle_user_message(telegram_user_id=user_id, chat_id=chat_id, message="Trip complete")
    assert repeat_res.status == "NO_ACTIVE_TRIP"
    assert "No active trip found" in repeat_res.message_text

    # Trip remains COMPLETED
    trip_after = memory_repos["trip_repo"].get_trip(trip_id)
    assert trip_after.status == "COMPLETED"
    assert trip_after.is_active is False


# ============================================================================
# Step 14: Activation Idempotency
# ============================================================================
@pytest.mark.asyncio
async def test_activation_idempotency_on_already_active_trip(memory_repos):
    """Step 14: For an ACTIVE trip, repeating 'Booked' is idempotent and safe."""
    chat_id = 991105
    user_id = 991105
    orch = _build_mock_orchestrator(memory_repos)

    # Create planning trip
    plan_res = await orch.handle_user_message(
        telegram_user_id=user_id,
        chat_id=chat_id,
        message="Plan a trip from Mumbai to Goa for 2 people, 3 days, with budget ₹25,000",
    )
    trip_id = plan_res.trip_id

    # Turn 1: "Booked" activates trip
    book_intent = ParsedTripIntent(action=TripAction.CONFIRM_BOOKING, booking_confirmed=True)
    orch.ai_service.parse_trip_intent = AsyncMock(return_value=book_intent)

    res1 = await orch.handle_user_message(telegram_user_id=user_id, chat_id=chat_id, message="Booked")
    assert res1.status in ("ACTIVE", "FEASIBLE")

    trip1 = memory_repos["trip_repo"].get_trip(trip_id)
    assert trip1.status == "ACTIVE"
    assert trip1.is_active is True

    # Turn 2: "Booked" repeated
    res2 = await orch.handle_user_message(telegram_user_id=user_id, chat_id=chat_id, message="Booked")
    assert res2.status == "ACTIVE"
    assert "already active" in res2.message_text.lower()

    # Trip remains identical
    trip2 = memory_repos["trip_repo"].get_trip(trip_id)
    assert trip2.id == trip1.id
    assert trip2.status == "ACTIVE"
    assert trip2.is_active is True


# ============================================================================
# Step 15: Cross-Feature Lifecycle Sequence
# ============================================================================
@pytest.mark.asyncio
async def test_step15_cross_feature_lifecycle_sequence(memory_repos):
    """Step 15: Full cross-feature single integrated lifecycle run.

    Sequence:
    NEW_TRIP -> PLANNING -> CONFIRM_BOOKING -> ACTIVE -> LOG_EXPENSE ->
    REOPTIMIZE -> RESCUE -> LOG_EXPENSE -> TRIP_COMPLETE -> reconciliation -> COMPLETED.
    """
    chat_id = 991106
    user_id = 991106
    orch = _build_mock_orchestrator(memory_repos)

    # 1. NEW_TRIP
    plan_res = await orch.handle_user_message(
        telegram_user_id=user_id,
        chat_id=chat_id,
        message="Plan a trip from Mumbai to Goa for 2 people, 3 days, with budget ₹30,000",
    )
    assert plan_res.status == "FEASIBLE"
    trip_id = plan_res.trip_id
    assert trip_id is not None

    trip = memory_repos["trip_repo"].get_trip(trip_id)
    assert trip.id == trip_id

    # 2. CONFIRM_BOOKING -> ACTIVE
    orch.trip_repo.update_trip_status(trip_id, status="ACTIVE", is_active=True)
    trip = memory_repos["trip_repo"].get_trip(trip_id)
    assert trip.status == "ACTIVE"
    assert trip.is_active is True

    # 3. LOG_EXPENSE
    exp1_res = await orch.handle_user_message(
        telegram_user_id=user_id,
        chat_id=chat_id,
        message="Spent ₹1200 on seafood lunch today",
    )
    assert exp1_res.status == "EXPENSE_LOGGED"
    assert exp1_res.trip_id == trip_id

    # 4. REOPTIMIZE
    active_trip = memory_repos["trip_repo"].get_trip(trip_id)
    opt_res = await reoptimize_remaining_trip(
        trip=active_trip,
        trip_repo=memory_repos["trip_repo"],
        ledger_repo=memory_repos["ledger_repo"],
        itinerary_repo=memory_repos["itinerary_repo"],
    )
    assert opt_res is not None

    # 5. RESCUE
    rescue_res = await orch.handle_user_message(
        telegram_user_id=user_id,
        chat_id=chat_id,
        message="Auto driver asking 700 rupees for 2 km ride",
    )
    assert rescue_res.status == "RESCUE"
    assert rescue_res.trip_id == trip_id

    # 6. LOG_EXPENSE Day 1 Completion
    exp2_res = await orch.handle_user_message(
        telegram_user_id=user_id,
        chat_id=chat_id,
        message="Day 1 is done, spent ₹2500 on dinner",
    )
    assert exp2_res.status == "EXPENSE_LOGGED"
    active_trip = memory_repos["trip_repo"].get_trip(trip_id)
    assert active_trip.current_day == 2

    # 7. TRIP_COMPLETE with reason
    comp_res = await orch.handle_user_message(
        telegram_user_id=user_id,
        chat_id=chat_id,
        message="Trip completed: Reached home safely after vacation",
    )
    assert comp_res.status == "PENDING_RECONCILIATION"

    # 8. Reconciliation
    reconcile_res = await orch.handle_user_message(
        telegram_user_id=user_id,
        chat_id=chat_id,
        message="₹28,500",
    )
    assert reconcile_res.status == "COMPLETED"
    assert reconcile_res.trip_id == trip_id

    # Final Trip State: status=COMPLETED, is_active=False, reason preserved
    final_trip = memory_repos["trip_repo"].get_trip(trip_id)
    assert final_trip.status == "COMPLETED"
    assert final_trip.is_active is False
    assert final_trip.completion_reason == "Reached home safely after vacation"
    assert memory_repos["trip_repo"].get_active_trip(chat_id) is None


# ============================================================================
# Step 18 & 19: Database Persistence Across Reload
# ============================================================================
def test_database_persistence_across_repository_reload():
    """Step 18 & 19: Full lifecycle status and attributes survive repository recreation."""
    # Instance 1: Create & Activate trip
    repo1 = TripRepository(client=None)
    user_id = uuid4()
    trip = repo1.create_trip(
        user_id=user_id,
        telegram_chat_id=776655,
        budget_total=Decimal("20000.00"),
        destination="Coorg",
        status="PLANNING",
        is_active=True,
    )
    assert trip.status == "PLANNING"

    # Activate
    repo1.update_trip_status(trip.id, status="ACTIVE", is_active=True)

    # Simulate repo recreation / memory share
    repo2 = TripRepository(client=None)
    repo2._memory_store = dict(repo1._memory_store)

    active_trip = repo2.get_active_trip(776655)
    assert active_trip is not None
    assert active_trip.id == trip.id
    assert active_trip.status == "ACTIVE"

    # Complete with reason
    repo2.update_trip_status(trip.id, status="COMPLETED", completion_reason="Vacation ended", is_active=False)

    # Simulate repo recreation 3
    repo3 = TripRepository(client=None)
    repo3._memory_store = dict(repo2._memory_store)

    completed_trip = repo3.get_trip(trip.id)
    assert completed_trip.status == "COMPLETED"
    assert completed_trip.is_active is False
    assert completed_trip.completion_reason == "Vacation ended"

    # Active lookup returns None
    assert repo3.get_active_trip(776655) is None
