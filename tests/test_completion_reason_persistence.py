"""Comprehensive tests for completion_reason schema and persistence (Phase 5).

Covers:
- Test A — Model accepts reason
- Test B — Model accepts NULL
- Test C — Repository persists reason
- Test D — Repository persists NULL
- Test E — End-to-end completion with reason
- Test F — End-to-end completion without reason
- Test G — Existing reconciliation remains unchanged
- Test H — Historical rows unaffected
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
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
from budlance.engine.optimizer import OptimizationEngine
from budlance.estimation.estimator import EstimationLayer
from budlance.itinerary.generator import ItineraryGenerator
from budlance.ledger.manager import VirtualLedgerManager
from budlance.lifecycle.completion_handler import TripCompletionHandler
from budlance.normalization.normalizer import DataNormalizer
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.rescue.service import RescueService
from budlance.serpapi.models import DataSource, TravelDataEnvelope


@pytest.fixture
def memory_repos():
    """In-memory repositories for deterministic testing."""
    return {
        "user_repo": UserRepository(client=None),
        "trip_repo": TripRepository(client=None),
        "intent_repo": IntentRepository(client=None),
        "itinerary_repo": ItineraryRepository(client=None),
        "ledger_repo": LedgerRepository(client=None),
        "rescue_repo": RescueRepository(client=None),
        "conversation_repo": ConversationStateRepository(client=None),
    }


@pytest.fixture
def active_trip(memory_repos):
    """Create a standard ACTIVE trip for completion tests."""
    user = memory_repos["user_repo"].get_or_create_user(
        telegram_user_id=881100,
        username="completion_tester",
    )
    trip = memory_repos["trip_repo"].create_trip(
        user_id=user.id,
        telegram_chat_id=881100,
        budget_total=Decimal("25000.00"),
        destination="Goa",
        origin="Mumbai",
        currency="INR",
        people_count=2,
        duration_days=3,
        status="ACTIVE",
        is_active=True,
    )
    alloc = BudgetAllocation(
        id=uuid4(),
        trip_id=trip.id,
        transport_allocated=Decimal("8000.00"),
        stay_allocated=Decimal("7000.00"),
        food_allocated=Decimal("5000.00"),
        activities_discretionary=Decimal("2500.00"),
        rescue_fund_allocated=Decimal("2500.00"),
        total_budget=Decimal("25000.00"),
    )
    memory_repos["ledger_repo"].save_budget_allocation(alloc)

    itin = Itinerary(
        id=uuid4(),
        trip_id=trip.id,
        days=[
            {"day_number": 1, "theme": "Arrival", "items": [], "estimated_cost": 2500, "status": "COMPLETED"},
            {"day_number": 2, "theme": "Sightseeing", "items": [], "estimated_cost": 2500, "status": "IN_PROGRESS"},
            {"day_number": 3, "theme": "Departure", "items": [], "estimated_cost": 2500, "status": "UPCOMING"},
        ],
    )
    memory_repos["itinerary_repo"].save_itinerary(itin)
    return trip


@pytest.fixture
def completion_handler(memory_repos):
    return TripCompletionHandler(
        trip_repo=memory_repos["trip_repo"],
        ledger_repo=memory_repos["ledger_repo"],
        conversation_repo=memory_repos["conversation_repo"],
    )


# ============================================================================
# Test A — Model accepts reason
# ============================================================================
def test_a_model_accepts_reason():
    """Test A — Trip model preserves valid completion_reason string."""
    trip = Trip(
        user_id=uuid4(),
        telegram_chat_id=12345,
        budget_total=Decimal("15000.00"),
        destination="Jaipur",
        status="COMPLETED",
        is_active=False,
        completion_reason="Trip completed normally",
    )
    assert trip.completion_reason == "Trip completed normally"
    assert trip.status == "COMPLETED"
    assert trip.is_active is False


# ============================================================================
# Test B — Model accepts NULL
# ============================================================================
def test_b_model_accepts_null():
    """Test B — Trip model accepts None (NULL) for optional completion_reason."""
    trip = Trip(
        user_id=uuid4(),
        telegram_chat_id=12345,
        budget_total=Decimal("15000.00"),
        destination="Jaipur",
        status="COMPLETED",
        is_active=False,
        completion_reason=None,
    )
    assert trip.completion_reason is None
    assert trip.status == "COMPLETED"
    assert trip.is_active is False


# ============================================================================
# Test C — Repository persists reason
# ============================================================================
def test_c_repository_persists_reason():
    """Test C — TripRepository persists completion_reason in update payload and memory store."""
    mock_client = MagicMock()
    mock_table = MagicMock()
    mock_client.table.return_value = mock_table
    mock_table.update.return_value.eq.return_value.execute.return_value.data = [{"id": "1", "status": "COMPLETED"}]

    repo = TripRepository(client=mock_client)
    trip_id = uuid4()
    success = repo.update_trip_status(
        trip_id=trip_id,
        status="COMPLETED",
        is_active=False,
        completion_reason="Trip completed normally",
    )
    assert success is True
    update_call = mock_table.update.call_args[0][0]
    assert update_call["status"] == "COMPLETED"
    assert update_call["is_active"] is False
    assert update_call["completion_reason"] == "Trip completed normally"

    # Also test memory store persistence
    mem_repo = TripRepository(client=None)
    mem_trip = mem_repo.create_trip(
        user_id=uuid4(),
        telegram_chat_id=1001,
        budget_total=Decimal("20000.00"),
        destination="Kochi",
        status="ACTIVE",
        is_active=True,
    )
    mem_repo.update_trip_status(
        trip_id=mem_trip.id,
        status="COMPLETED",
        is_active=False,
        completion_reason="Trip completed normally",
    )
    fetched = mem_repo.get_trip(mem_trip.id)
    assert fetched.completion_reason == "Trip completed normally"
    assert fetched.status == "COMPLETED"
    assert fetched.is_active is False


# ============================================================================
# Test D — Repository persists NULL
# ============================================================================
def test_d_repository_persists_null():
    """Test D — TripRepository persists NULL completion_reason in update payload."""
    mock_client = MagicMock()
    mock_table = MagicMock()
    mock_client.table.return_value = mock_table
    mock_table.update.return_value.eq.return_value.execute.return_value.data = [{"id": "1", "status": "COMPLETED"}]

    repo = TripRepository(client=mock_client)
    trip_id = uuid4()
    success = repo.update_trip_status(
        trip_id=trip_id,
        status="COMPLETED",
        is_active=False,
        completion_reason=None,
    )
    assert success is True
    update_call = mock_table.update.call_args[0][0]
    assert update_call["status"] == "COMPLETED"
    assert update_call["is_active"] is False
    assert update_call["completion_reason"] is None

    # Also test memory store persistence
    mem_repo = TripRepository(client=None)
    mem_trip = mem_repo.create_trip(
        user_id=uuid4(),
        telegram_chat_id=1002,
        budget_total=Decimal("20000.00"),
        destination="Udaipur",
        status="ACTIVE",
        is_active=True,
    )
    mem_repo.update_trip_status(
        trip_id=mem_trip.id,
        status="COMPLETED",
        is_active=False,
        completion_reason=None,
    )
    fetched = mem_repo.get_trip(mem_trip.id)
    assert fetched.completion_reason is None
    assert fetched.status == "COMPLETED"
    assert fetched.is_active is False


# ============================================================================
# Test E — End-to-end completion with reason
# ============================================================================
@pytest.mark.asyncio
async def test_e_end_to_end_completion_with_reason(completion_handler, active_trip, memory_repos):
    """Test E — End-to-end completion: ACTIVE -> TRIP_COMPLETE with reason -> COMPLETED."""
    chat_id = active_trip.telegram_chat_id

    # Turn 1: User indicates trip complete with reason
    reason = "Trip completed because we reached home safely."
    init_res = await completion_handler.handle_trip_complete(
        chat_id=chat_id,
        completion_reason=reason,
    )
    assert init_res.status == "PENDING_RECONCILIATION"

    # Turn 2: User reconciles or skips
    final_res = await completion_handler.handle_reconcile_amount(
        chat_id=chat_id,
        amount=Decimal("24000.00"),
    )
    assert final_res.status == "COMPLETED"
    assert final_res.completion_reason == reason

    reloaded = memory_repos["trip_repo"].get_trip(active_trip.id)
    assert reloaded.status == "COMPLETED"
    assert reloaded.is_active is False
    assert reloaded.completion_reason == reason


# ============================================================================
# Test F — End-to-end completion without reason
# ============================================================================
@pytest.mark.asyncio
async def test_f_end_to_end_completion_without_reason(completion_handler, active_trip, memory_repos):
    """Test F — End-to-end completion without reason completes successfully with NULL or default."""
    chat_id = active_trip.telegram_chat_id

    # Direct completion without reason
    direct_res = await completion_handler.complete_trip(
        chat_id=chat_id,
        trip_id=active_trip.id,
        completion_reason=None,
    )
    assert direct_res.status == "COMPLETED"
    assert direct_res.completion_reason is None

    reloaded = memory_repos["trip_repo"].get_trip(active_trip.id)
    assert reloaded.status == "COMPLETED"
    assert reloaded.is_active is False
    assert reloaded.completion_reason is None


# ============================================================================
# Test G — Existing reconciliation remains unchanged
# ============================================================================
@pytest.mark.asyncio
async def test_g_existing_reconciliation_remains_unchanged(completion_handler, active_trip, memory_repos):
    """Test G — completion_reason and financial reconciliation do not interfere with each other."""
    chat_id = active_trip.telegram_chat_id

    # Start reconciliation without custom reason
    await completion_handler.handle_trip_complete(chat_id=chat_id)

    # Reconcile with actual spending ₹22,000 against ₹25,000 budget
    rec_res = await completion_handler.handle_reconcile_amount(
        chat_id=chat_id,
        amount=Decimal("22000.00"),
    )
    assert rec_res.status == "COMPLETED"
    assert rec_res.recorded_actual_spend == Decimal("22000.00")
    assert rec_res.planned_budget == Decimal("25000.00")
    assert rec_res.final_variance == Decimal("3000.00")
    assert "Difference: ₹3,000 under planned budget" in rec_res.message_text

    # Verify standard default reason preserved when none specified
    reloaded = memory_repos["trip_repo"].get_trip(active_trip.id)
    assert reloaded.status == "COMPLETED"
    assert reloaded.is_active is False
    assert reloaded.completion_reason == "USER_CONFIRMED"


# ============================================================================
# Test H — Historical rows unaffected
# ============================================================================
def test_h_historical_rows_unaffected():
    """Test H — Historical trip records without completion_reason remain valid."""
    legacy_data = {
        "id": str(uuid4()),
        "user_id": str(uuid4()),
        "telegram_chat_id": 998877,
        "destination": "Goa",
        "origin": "Mumbai",
        "budget_total": "20000.00",
        "currency": "INR",
        "people_count": 2,
        "duration_days": 3,
        "status": "COMPLETED",
        "is_active": False,
        "current_day": 3,
        "created_at": utc_now().isoformat(),
        "updated_at": utc_now().isoformat(),
        # completion_reason omitted or None
    }
    trip = Trip.model_validate(legacy_data)
    assert trip.status == "COMPLETED"
    assert trip.is_active is False
    assert trip.completion_reason is None
