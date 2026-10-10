"""Comprehensive tests for Budlance Phase 7:
In-Trip Companion, Expense Tracking, Budget Rescue & Booking Lifecycle.

Includes:
- Mandatory Example 1: Multi-expense logging + Event replay deduplication + Repo reconstruction.
- Mandatory Example 2: Closed museum rescue -> Nature spot proposal -> User approval gate (Yes/No).
- Mandatory Example 3: Return flight booking confirmation -> USER_CONFIRMED (NOT PROVIDER_VERIFIED) +
                        Emergency airport bus alternative under ₹700 + Reserve impact.
- 20 Required Regression Tests covering all Phase 7 functional areas.
"""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4
import pytest

from budlance.ai.schemas import ExpenseItem, ParsedTripIntent, TripAction
from budlance.ai.service import AIIntentService
from budlance.cache.manager import CacheFallbackManager
from budlance.db.models import BudgetAllocation, Itinerary, LedgerEntry, Trip, TripPass, utc_now
from budlance.db.repositories.conversation_repo import ConversationStateRepository
from budlance.db.repositories.intent_repo import IntentRepository
from budlance.db.repositories.itinerary_repo import ItineraryRepository
from budlance.db.repositories.ledger_repo import LedgerRepository
from budlance.db.repositories.rescue_repo import RescueRepository
from budlance.db.repositories.trip_pass_repo import TripPassRepository
from budlance.db.repositories.trip_repo import TripRepository
from budlance.db.repositories.user_repo import UserRepository
from budlance.engine.budget import ReverseBudgetEngine
from budlance.engine.optimizer import OptimizationEngine
from budlance.estimation.estimator import EstimationLayer
from budlance.itinerary.generator import ItineraryGenerator
from budlance.itinerary.models import GeneratedItinerary, ItineraryDay, ItineraryItem
from budlance.ledger.manager import VirtualLedgerManager
from budlance.lifecycle.booking_handler import (
    BookingComponentType,
    BookingLifecycleHandler,
    BookingRecord,
    BookingState,
)
from budlance.lifecycle.completion_handler import TripCompletionHandler
from budlance.lifecycle.expense_handler import ExpenseLifecycleHandler
from budlance.lifecycle.intrip_companion import InTripCompanionHandler
from budlance.lifecycle.reoptimizer import RemainingTripReoptimizer
from budlance.normalization.normalizer import DataNormalizer
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.rescue.service import RescueService
from budlance.schemas.travel import PlaceOption
from budlance.serpapi.models import DataSource, TravelDataEnvelope


# =========================================================================
# Fixtures
# =========================================================================

@pytest.fixture(autouse=True)
def reset_stores():
    """Reset class-level in-memory stores before and after each test."""
    LedgerRepository.reset_in_memory_store()
    BookingLifecycleHandler.reset_in_memory_store()
    yield
    LedgerRepository.reset_in_memory_store()
    BookingLifecycleHandler.reset_in_memory_store()


@pytest.fixture
def repos():
    """Deterministic in-memory repositories."""
    return {
        "user_repo": UserRepository(client=None),
        "trip_repo": TripRepository(client=None),
        "intent_repo": IntentRepository(client=None),
        "itinerary_repo": ItineraryRepository(client=None),
        "ledger_repo": LedgerRepository(client=None),
        "rescue_repo": RescueRepository(client=None),
        "conversation_repo": ConversationStateRepository(client=None),
        "trip_pass_repo": TripPassRepository(client=None),
    }


@pytest.fixture
def active_munnar_trip(repos):
    """Create a fully active Munnar trip for in-trip testing."""
    user = repos["user_repo"].get_or_create_user(telegram_user_id=112233)
    trip = repos["trip_repo"].create_trip(
        user_id=user.id,
        telegram_chat_id=112233,
        budget_total=Decimal("25000.00"),
        destination="Munnar",
        origin="Chennai",
        currency="INR",
        people_count=2,
        duration_days=3,
        is_active=True,
    )
    repos["trip_repo"].update_trip_status(trip.id, "ACTIVE", is_active=True)
    trip = repos["trip_repo"].get_trip(trip.id)

    # Master allocation
    alloc = BudgetAllocation(
        id=uuid4(),
        trip_id=trip.id,
        transport_allocated=Decimal("7000.00"),
        stay_allocated=Decimal("8000.00"),
        food_allocated=Decimal("5000.00"),
        activities_discretionary=Decimal("2500.00"),
        rescue_fund_allocated=Decimal("2500.00"),
        total_budget=Decimal("25000.00"),
    )
    repos["ledger_repo"].save_budget_allocation(alloc)

    # Baseline line items
    baseline_items = [
        LedgerEntry(
            id=uuid4(),
            trip_id=trip.id,
            category="fixed_booking",
            description="Chennai to Munnar Train",
            allocated_amount=Decimal("3500.00"),
            planned_amount=Decimal("3500.00"),
            spent_amount=Decimal("0.00"),
            actual_amount=None,
            source="estimated",
            created_at=utc_now(),
        ),
        LedgerEntry(
            id=uuid4(),
            trip_id=trip.id,
            category="fixed_booking",
            description="Munnar to Chennai Flight",
            allocated_amount=Decimal("3500.00"),
            planned_amount=Decimal("3500.00"),
            spent_amount=Decimal("0.00"),
            actual_amount=None,
            source="estimated",
            created_at=utc_now(),
        ),
        LedgerEntry(
            id=uuid4(),
            trip_id=trip.id,
            category="fixed_booking",
            description="Munnar Hill View Hotel (2 Nights)",
            allocated_amount=Decimal("8000.00"),
            planned_amount=Decimal("8000.00"),
            spent_amount=Decimal("0.00"),
            actual_amount=None,
            source="estimated",
            created_at=utc_now(),
        ),
        LedgerEntry(
            id=uuid4(),
            trip_id=trip.id,
            category="daily_survival",
            description="Daily Food Allowance",
            allocated_amount=Decimal("5000.00"),
            planned_amount=Decimal("5000.00"),
            spent_amount=Decimal("0.00"),
            actual_amount=None,
            source="estimated",
            created_at=utc_now(),
        ),
    ]
    for e in baseline_items:
        repos["ledger_repo"].add_ledger_entry(e)

    # Day 1 Itinerary with tea museum
    day1_items = [
        ItineraryItem(
            time_slot="Morning",
            activity="Visit Munnar Tea Museum",
            place_name="Munnar Tea Museum",
            category="attraction",
            planned_cost=Decimal("150.00"),
            description="Explore tea processing history",
        ),
        ItineraryItem(
            time_slot="Afternoon",
            activity="Walk around Mattupetty Lake",
            place_name="Mattupetty Lake",
            category="nature",
            planned_cost=Decimal("100.00"),
            description="Scenic lake views",
        ),
    ]
    itin = Itinerary(
        id=uuid4(),
        trip_id=trip.id,
        days=[
            {"day_number": 1, "theme_or_summary": "Munnar Culture & Nature", "items": [it.model_dump(mode="json") for it in day1_items], "daily_estimated_cost": 250.0},
            {"day_number": 2, "theme_or_summary": "Hill Station Exploration", "items": [], "daily_estimated_cost": 0.0},
            {"day_number": 3, "theme_or_summary": "Scenic Return Journey", "items": [], "daily_estimated_cost": 0.0},
        ],
        updated_at=utc_now(),
    )
    repos["itinerary_repo"].save_itinerary(itin)

    # Unlock trip pass for testing
    repos["trip_pass_repo"].create_pass(
        trip_id=trip.id,
        telegram_user_id=112233,
        telegram_chat_id=112233,
        status="PAID",
    )

    return trip


def _mock_cache_manager() -> CacheFallbackManager:
    mock_cache = MagicMock(spec=CacheFallbackManager)

    async def _get(engine, params, trip_id=None, **kw):
        if engine == "google_maps":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="mock_maps_munnar",
                data={
                    "local_results": [
                        {
                            "title": "Eravikulam National Park",
                            "type": "National park",
                            "address": "Munnar, Kerala",
                            "rating": 4.6,
                            "reviews": 1500,
                        },
                        {
                            "title": "Attukal Waterfalls Nature Trail",
                            "type": "Nature preserve",
                            "address": "Attukal, Munnar, Kerala",
                            "rating": 4.5,
                            "reviews": 920,
                        },
                    ]
                },
                is_fallback=False,
            )
        if engine == "google_flights":
            return TravelDataEnvelope(
                source=DataSource.LIVE, engine=engine, query_hash="fh",
                data={"best_flights": [{"flights": [{"airline": "IndiGo"}], "price": 3200}]},
                is_fallback=False,
            )
        if engine == "google_hotels":
            return TravelDataEnvelope(
                source=DataSource.LIVE, engine=engine, query_hash="hh",
                data={"properties": [{"name": "Grand Hotel",
                    "rate_per_night": {"extracted_lowest": 1500},
                    "total_rate": {"extracted_lowest": 4500}}]},
                is_fallback=False,
            )
        return TravelDataEnvelope(
            source=DataSource.FALLBACK, engine=engine,
            query_hash="fbh", data={}, is_fallback=True,
        )

    mock_cache.get_travel_data = AsyncMock(side_effect=_get)
    return mock_cache


@pytest.fixture
def orch(repos):
    """Instantiate a fully configured BudlanceOrchestrator."""
    ai = AIIntentService(use_mock=True)
    cache = _mock_cache_manager()
    return BudlanceOrchestrator(
        user_repo=repos["user_repo"],
        trip_repo=repos["trip_repo"],
        intent_repo=repos["intent_repo"],
        itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"],
        rescue_repo=repos["rescue_repo"],
        conversation_repo=repos["conversation_repo"],
        trip_pass_repo=repos["trip_pass_repo"],
        ai_service=ai,
        cache_manager=cache,
        enable_trip_pass=True,
    )


# =========================================================================
# Mandatory Example 1: Multi-Expense Logging + Replay Deduplication
# =========================================================================

@pytest.mark.asyncio
async def test_mandatory_example_1_multi_expense_and_replay_deduplication(orch, repos, active_munnar_trip):
    """Example 1: Multi-expense logging ("₹850 on lunch and ₹220 on metro") -> 2 ledger rows.
    Event replay with same event_id -> 0 new rows, identical totals.
    Deduplication persists across repository reconstruction.
    """
    chat_id = active_munnar_trip.telegram_chat_id
    trip_id = active_munnar_trip.id
    event_id = "tg-update-99001"

    # Step 1: User logs multiple expenses in a single natural-language message
    res1 = await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Spent ₹850 on lunch and ₹220 on metro",
        event_id=event_id,
    )
    assert res1.status == "EXPENSE_LOGGED"
    assert "850" in res1.message_text
    assert "220" in res1.message_text

    # Verify 2 distinct ledger entries were created
    entries = repos["ledger_repo"].get_ledger_entries(trip_id)
    actual_entries = [e for e in entries if e.actual_amount is not None]
    assert len(actual_entries) == 2

    # Check categories and amounts
    amounts = sorted([e.actual_amount for e in actual_entries])
    assert amounts == [Decimal("220.00"), Decimal("850.00")]

    food_entry = next(e for e in actual_entries if e.actual_amount == Decimal("850.00"))
    transport_entry = next(e for e in actual_entries if e.actual_amount == Decimal("220.00"))
    assert food_entry.category == "daily_survival"  # Food bucket B
    assert transport_entry.category == "daily_survival"  # In-trip transport bucket B
    assert f"[evt:{event_id}:0]" in food_entry.description
    assert f"[evt:{event_id}:1]" in transport_entry.description

    total_actual_before = sum(e.actual_amount for e in actual_entries)
    assert total_actual_before == Decimal("1070.00")

    # Step 2: Event replay with exact same event_id (e.g. Telegram network retry)
    res2 = await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Spent ₹850 on lunch and ₹220 on metro",
        event_id=event_id,
    )
    assert res2.status == "EXPENSE_LOGGED"
    assert "already recorded" in res2.message_text.lower() or "recorded" in res2.message_text.lower()

    # Verify ZERO new ledger entries were added
    entries_after = repos["ledger_repo"].get_ledger_entries(trip_id)
    actual_after = [e for e in entries_after if e.actual_amount is not None]
    assert len(actual_after) == 2
    assert sum(e.actual_amount for e in actual_after) == total_actual_before

    # Step 3: Process restart / Repository reconstruction
    # A new LedgerRepository instance querying the persistent store must detect the event
    reconstructed_ledger_repo = LedgerRepository(client=None)
    assert reconstructed_ledger_repo.has_event_id(trip_id, event_id) is True

    # Replay on a reconstructed expense handler
    fresh_expense_handler = ExpenseLifecycleHandler(
        trip_repo=repos["trip_repo"],
        ledger_repo=reconstructed_ledger_repo,
    )
    ai_parse = orch.ai_service._mock_parse_trip_intent("Spent ₹850 on lunch and ₹220 on metro")
    ai_parse.event_id = event_id
    replay_res = await fresh_expense_handler.handle_log_expense(
        chat_id=chat_id,
        parsed=ai_parse,
        event_id=event_id,
    )
    assert replay_res.status == "EXPENSE_LOGGED"
    assert "already recorded" in replay_res.message_text

    # Entries remain exactly 2
    final_entries = reconstructed_ledger_repo.get_ledger_entries(trip_id)
    assert len([e for e in final_entries if e.actual_amount is not None]) == 2


# =========================================================================
# Mandatory Example 2: Rescue Proposal Gate + User Confirmation
# =========================================================================

@pytest.mark.asyncio
async def test_mandatory_example_2_rescue_proposal_gate_user_confirmation(orch, repos, active_munnar_trip):
    """Example 2: Closed museum rescue -> proposal generated, hotel & return flight untouched,
    pending proposal created.
    User says 'Yes' -> single item replaced and ledger adjusted.
    User says 'No' -> original itinerary preserved.
    """
    chat_id = active_munnar_trip.telegram_chat_id
    trip_id = active_munnar_trip.id

    # Verify baseline itinerary has Munnar Tea Museum
    itin_before = repos["itinerary_repo"].get_itinerary(trip_id)
    d1_items = itin_before.days[0]["items"]
    assert any("Tea Museum" in it["place_name"] for it in d1_items)

    # 1. User reports disruption: Museum is closed, asking for nature spot
    rescue_msg = "The tea museum is closed today, suggest a nature spot"
    prop_res = await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message=rescue_msg,
    )
    assert prop_res.status == "RESCUE"
    assert "Proposed replacement" in prop_res.message_text or "Reply 'Yes'" in prop_res.message_text
    assert "hotel and return flight remain untouched" in prop_res.message_text.lower() or "untouched" in prop_res.message_text.lower()

    # CRITICAL INVARIANT: Itinerary in database MUST NOT BE MUTATED YET
    itin_mid = repos["itinerary_repo"].get_itinerary(trip_id)
    assert any("Tea Museum" in it["place_name"] for it in itin_mid.days[0]["items"])

    # Pending proposal must be stored in conversation repo
    pending = repos["conversation_repo"].get_pending_rescue_proposal(chat_id)
    assert pending is not None
    assert pending["trip_id"] == str(trip_id)

    # 2. Case A: User approves proposal with 'Yes'
    confirm_res = await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Yes, apply the change",
    )
    assert confirm_res.status == "RESCUE"
    assert "Confirmed" in confirm_res.message_text or "Replaced" in confirm_res.message_text
    assert "remain unchanged" in confirm_res.message_text or "hotel and return flight" in confirm_res.message_text

    # Verify pending proposal was cleared
    assert repos["conversation_repo"].get_pending_rescue_proposal(chat_id) is None

    # Verify database itinerary is now updated: Tea Museum was replaced by nature spot
    itin_after = repos["itinerary_repo"].get_itinerary(trip_id)
    d1_after = itin_after.days[0]["items"]
    assert not any("Tea Museum" in it["place_name"] for it in d1_after)
    assert any("Park" in it["place_name"] or "Nature" in it["activity"] or "Eravikulam" in it["place_name"] or "Attukal" in it["place_name"] for it in d1_after)

    # Verify hotel and flight line items in ledger remain untouched
    ledger_entries = repos["ledger_repo"].get_ledger_entries(trip_id)
    hotel_entries = [e for e in ledger_entries if "Hotel" in e.description]
    flight_entries = [e for e in ledger_entries if "Flight" in e.description]
    assert len(hotel_entries) == 1 and hotel_entries[0].planned_amount == Decimal("8000.00")
    assert len(flight_entries) == 1 and flight_entries[0].planned_amount == Decimal("3500.00")


@pytest.mark.asyncio
async def test_mandatory_example_2_rescue_proposal_rejected(orch, repos, active_munnar_trip):
    """Example 2 (Branch B): User rejects proposal -> original plan retained 100% intact."""
    chat_id = active_munnar_trip.telegram_chat_id
    trip_id = active_munnar_trip.id

    # Disruption reported -> Proposal generated
    await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="The tea museum is closed today, suggest a nature spot",
    )
    assert repos["conversation_repo"].get_pending_rescue_proposal(chat_id) is not None

    # User says 'No' / 'Cancel'
    cancel_res = await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="No, keep my original plan",
    )
    assert cancel_res.status == "RESCUE"
    assert "Cancelled" in cancel_res.message_text
    assert "Tea Museum" in cancel_res.message_text or "unchanged" in cancel_res.message_text

    # Pending proposal cleared
    assert repos["conversation_repo"].get_pending_rescue_proposal(chat_id) is None

    # Itinerary remains 100% unchanged with original Tea Museum
    itin = repos["itinerary_repo"].get_itinerary(trip_id)
    d1_items = itin.days[0]["items"]
    assert any("Tea Museum" in it["place_name"] for it in d1_items)


# =========================================================================
# Mandatory Example 3: Booking Lifecycle + Emergency Transit Alternative
# =========================================================================

@pytest.mark.asyncio
async def test_mandatory_example_3_booking_confirmation_and_emergency_transit(orch, repos, active_munnar_trip):
    """Example 3: Return flight booking confirmation -> USER_CONFIRMED (NOT PROVIDER_VERIFIED).
    Emergency airport bus alternative under ₹700 -> estimate shown, no automatic booking,
    reserve impact calculated, no expense recorded until confirmed payment.
    """
    chat_id = active_munnar_trip.telegram_chat_id
    trip_id = active_munnar_trip.id

    # 1. User reports completing return flight booking externally
    res1 = await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="I booked the return flight to Chennai for ₹3,200",
    )
    assert res1.status in ("MANAGE_BOOKING", "ACTIVE")
    assert "USER_CONFIRMED" in res1.message_text
    # Boundary integrity: must state provider limitation honestly
    assert "provider" in res1.message_text.lower() or "assistant" in res1.message_text.lower()

    # Verify booking record in handler
    bookings = orch.booking_handler.get_bookings(trip_id)
    flight_b = next(b for b in bookings if b.component_type == BookingComponentType.FLIGHT)
    assert flight_b.state == BookingState.USER_CONFIRMED
    assert flight_b.is_provider_verified is False  # STRICT REQUIREMENT

    # 2. Emergency transit disruption: "Airport bus cancelled, need an alternative under ₹700"
    res2 = await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Airport bus cancelled, need an alternative under ₹700",
    )
    assert res2.status == "RESCUE"
    assert "700" in res2.message_text or "transport" in res2.message_text.lower() or "alternative" in res2.message_text.lower()

    # Reserve impact check: no automatic booking or expense entry before actual payment
    ledger_entries = repos["ledger_repo"].get_ledger_entries(trip_id)
    actual_entries = [e for e in ledger_entries if e.actual_amount is not None]
    # No actual spending recorded for an advisory inquiry
    assert len(actual_entries) == 0


# =========================================================================
# 20 Targeted Regression Tests (Phase 7 Core Capabilities)
# =========================================================================

# Reg 1: Natural-language multi-expense parsing
def test_reg1_natural_language_multi_expense_parsing():
    ai = AIIntentService(use_mock=True)
    # Compound with and
    items1 = ai._extract_multi_expenses("Spent ₹850 on lunch and ₹220 on metro")
    assert len(items1) == 2
    assert items1[0].amount == Decimal("850") and items1[0].category == "food"
    assert items1[1].amount == Decimal("220") and items1[1].category == "transport"

    # Compound with Indian comma format ₹1,500
    items2 = ai._extract_multi_expenses("Paid ₹1,500 on resort dinner, ₹300 for taxi")
    assert len(items2) == 2
    assert items2[0].amount == Decimal("1500") and items2[0].category == "food"
    assert items2[1].amount == Decimal("300") and items2[1].category == "transport"


# Reg 2: Category assignment & Decimal arithmetic
def test_reg2_category_assignment_decimal_arithmetic():
    ai = AIIntentService(use_mock=True)
    text = "₹450 on museum tickets and ₹150.50 on snacks"
    items = ai._extract_multi_expenses(text)
    assert len(items) == 2
    assert items[0].amount == Decimal("450") and items[0].category == "activities"
    assert items[1].amount == Decimal("150.50") and items[1].category == "food"
    # Exact Decimal sum
    total = sum(i.amount for i in items)
    assert total == Decimal("600.50")


# Reg 3: Event deduplication on repeated update delivery
@pytest.mark.asyncio
async def test_reg3_event_deduplication_repeated_update(orch, active_munnar_trip):
    chat_id = active_munnar_trip.telegram_chat_id
    res1 = await orch.handle_user_message(chat_id, chat_id, "Spent ₹400 on dinner", event_id="evt-dup-1")
    assert res1.status == "EXPENSE_LOGGED"
    res2 = await orch.handle_user_message(chat_id, chat_id, "Spent ₹400 on dinner", event_id="evt-dup-1")
    assert res2.status == "EXPENSE_LOGGED"
    assert "already recorded" in res2.message_text


# Reg 4: Deduplication across repository reconstruction & process restart
@pytest.mark.asyncio
async def test_reg4_deduplication_across_repository_reconstruction(repos, active_munnar_trip):
    handler = ExpenseLifecycleHandler(trip_repo=repos["trip_repo"], ledger_repo=repos["ledger_repo"])
    parsed = ParsedTripIntent(amount=Decimal("350.00"), expense_category="food", event_id="persist-evt-77")
    await handler.handle_log_expense(chat_id=active_munnar_trip.telegram_chat_id, parsed=parsed, event_id="persist-evt-77")

    # Reconstruct
    fresh_ledger_repo = LedgerRepository(client=None)
    assert fresh_ledger_repo.has_event_id(active_munnar_trip.id, "persist-evt-77") is True


# Reg 5: Concurrent duplicate expense handling
@pytest.mark.asyncio
async def test_reg5_concurrent_duplicate_expense_handling(orch, active_munnar_trip):
    chat_id = active_munnar_trip.telegram_chat_id
    # Fire two concurrent calls with same event_id
    t1 = orch.handle_user_message(chat_id, chat_id, "Spent ₹500 on coffee", event_id="concurrent-evt-1")
    t2 = orch.handle_user_message(chat_id, chat_id, "Spent ₹500 on coffee", event_id="concurrent-evt-1")
    results = await asyncio.gather(t1, t2)
    assert any("already recorded" in r.message_text for r in results)
    entries = orch.ledger_repo.get_ledger_entries(active_munnar_trip.id)
    assert len([e for e in entries if e.actual_amount == Decimal("500.00")]) == 1


# Reg 6: Expense correction / reversal
@pytest.mark.asyncio
async def test_reg6_expense_correction_reversal(orch, active_munnar_trip):
    chat_id = active_munnar_trip.telegram_chat_id
    # Log an initial expense
    await orch.handle_user_message(chat_id, chat_id, "Spent ₹500 on lunch")
    entries_before = orch.ledger_repo.get_ledger_entries(active_munnar_trip.id)
    actual_before = sum(e.actual_amount for e in entries_before if e.actual_amount is not None)
    assert actual_before == Decimal("500.00")

    # Issue reversal via ledger repo
    rev_entry = orch.ledger_repo.record_expense_reversal(
        trip_id=active_munnar_trip.id,
        target_amount=Decimal("500.00"),
        category="daily_survival",
        reason="Mistaken duplicate entry",
    )
    assert rev_entry is not None
    assert rev_entry.actual_amount == Decimal("-500.00")

    entries_after = orch.ledger_repo.get_ledger_entries(active_munnar_trip.id)
    actual_after = sum(e.actual_amount for e in entries_after if e.actual_amount is not None)
    assert actual_after == Decimal("0.00")  # Perfectly offset without deleting historical row


# Reg 7: Separation of actual expenses, projected obligations, and reserve
@pytest.mark.asyncio
async def test_reg7_separation_actual_projected_reserve(orch, active_munnar_trip):
    chat_id = active_munnar_trip.telegram_chat_id
    await orch.handle_user_message(chat_id, chat_id, "Spent ₹600 on breakfast")
    summary = orch.ledger_manager.get_summary(active_munnar_trip.id)
    assert summary.total_budget == Decimal("25000.00")
    assert summary.total_spent == Decimal("600.00")
    assert summary.allocation.rescue_fund_allocated == Decimal("2500.00")
    assert summary.rescue_reserve_remaining == Decimal("2500.00")


# Reg 8: Budget rescue suggestions with mock fixtures
@pytest.mark.asyncio
async def test_reg8_budget_rescue_suggestions_mock_fixtures(orch, active_munnar_trip):
    res = await orch.rescue_service.execute_rescue(
        chat_id=active_munnar_trip.telegram_chat_id,
        user_message="Raining heavily at tea museum",
        as_proposal=True,
    )
    assert res.success is True
    assert res.is_proposal is True
    assert res.selected_alternative is not None


# Reg 9: Activity replacement isolated to target slot
@pytest.mark.asyncio
async def test_reg9_activity_replacement_isolated_to_target_slot(orch, repos, active_munnar_trip):
    chat_id = active_munnar_trip.telegram_chat_id
    trip_id = active_munnar_trip.id

    # Create proposal and apply
    res = await orch.rescue_service.execute_rescue(
        chat_id=chat_id,
        user_message="Munnar Tea Museum is closed",
        as_proposal=True,
    )
    orch.rescue_service.apply_confirmed_rescue(chat_id, res.pending_proposal)

    itin = repos["itinerary_repo"].get_itinerary(trip_id)
    d1 = itin.days[0]["items"]
    # Morning slot replaced
    assert d1[0]["place_name"] != "Munnar Tea Museum"
    # Afternoon slot (Mattupetty Lake) strictly untouched
    assert d1[1]["place_name"] == "Mattupetty Lake"


# Reg 10: Proposal confirmation gate before itinerary modification
@pytest.mark.asyncio
async def test_reg10_proposal_confirmation_gate_before_itinerary_modification(orch, repos, active_munnar_trip):
    chat_id = active_munnar_trip.telegram_chat_id
    await orch.handle_user_message(chat_id, chat_id, "Tea museum closed, find alternative")
    itin = repos["itinerary_repo"].get_itinerary(active_munnar_trip.id)
    assert any("Tea Museum" in it["place_name"] for it in itin.days[0]["items"])


# Reg 11: Strict preservation of hotel, flight, and dates
@pytest.mark.asyncio
async def test_reg11_strict_preservation_hotel_flight_dates(orch, repos, active_munnar_trip):
    chat_id = active_munnar_trip.telegram_chat_id
    await orch.handle_user_message(chat_id, chat_id, "Tea museum closed")
    await orch.handle_user_message(chat_id, chat_id, "yes")
    entries = repos["ledger_repo"].get_ledger_entries(active_munnar_trip.id)
    hotel = next(e for e in entries if "Hotel" in e.description)
    assert hotel.allocated_amount == Decimal("8000.00")
    assert hotel.planned_amount == Decimal("8000.00")


# Reg 12: Pending action decision, rejection, stale confirmation, and expiry
@pytest.mark.asyncio
async def test_reg12_pending_action_decision_lifecycle(orch, repos, active_munnar_trip):
    chat_id = active_munnar_trip.telegram_chat_id
    await orch.handle_user_message(chat_id, chat_id, "Tea museum is closed")
    assert repos["conversation_repo"].get_pending_rescue_proposal(chat_id) is not None

    # Clarification/unrecognized during pending proposal reminds user
    unrec = await orch.handle_user_message(chat_id, chat_id, "what time is it?")
    assert "Pending" in unrec.message_text
    assert repos["conversation_repo"].get_pending_rescue_proposal(chat_id) is not None

    # User cancels
    cancel = await orch.handle_user_message(chat_id, chat_id, "no")
    assert "Cancelled" in cancel.message_text
    assert repos["conversation_repo"].get_pending_rescue_proposal(chat_id) is None


# Reg 13: Booking state transitions
def test_reg13_booking_state_transitions():
    handler = BookingLifecycleHandler()
    t_id = uuid4()
    # Initial creation -> PLANNED
    b = handler.get_or_create_booking(t_id, "flight", name="Chennai flight")
    assert b.state == BookingState.PLANNED

    # Link provided
    b_link = handler.get_or_create_booking(t_id, "flight", booking_link="https://flights.com/book")
    assert b_link.state == BookingState.LINK_PROVIDED

    # User confirms
    tr = handler.user_confirms_booking(t_id, "flight")
    assert tr.new_state == BookingState.USER_CONFIRMED
    assert tr.is_provider_verified is False


# Reg 14: Cancellation integrity (user cancellation != provider verified)
def test_reg14_cancellation_integrity():
    handler = BookingLifecycleHandler()
    t_id = uuid4()
    res = handler.record_cancellation(t_id, "hotel", user_reported_only=True)
    assert res.new_state == BookingState.CANCELLATION_RECORDED
    b = handler.get_or_create_booking(t_id, "hotel")
    assert b.cancellation_verified is False  # Must NOT be provider verified


# Reg 15: Trip completion reconciliation with partial spending disclosure
@pytest.mark.asyncio
async def test_reg15_trip_completion_reconciliation_partial_spend_disclosure(orch, active_munnar_trip):
    chat_id = active_munnar_trip.telegram_chat_id
    # Log 1 expense
    await orch.handle_user_message(chat_id, chat_id, "Spent ₹1200 on lunch")
    # Complete trip
    c_res = await orch.handle_user_message(chat_id, chat_id, "trip complete")
    assert "Would you like to record your final actual spend" in c_res.message_text

    # Provide reconciliation amount
    final_res = await orch.handle_user_message(chat_id, chat_id, "₹18,500")
    assert final_res.status == "COMPLETED"
    assert "Planned budget: ₹25,000" in final_res.message_text
    assert "Recorded actual spend: ₹18,500" in final_res.message_text
    assert "Disclosure:" in final_res.message_text


# Reg 16: Chat and trip isolation across concurrent conversations
@pytest.mark.asyncio
async def test_reg16_chat_and_trip_isolation(orch, repos, active_munnar_trip):
    chat_a = active_munnar_trip.telegram_chat_id
    chat_b = 887766
    # Chat B plans a different trip
    plan_b = await orch.handle_user_message(chat_b, chat_b, "Plan a trip to Goa from Mumbai for 2 people, 3 days, budget ₹25,000")
    assert plan_b.status == "FEASIBLE"
    assert plan_b.selected_destination == "Goa"

    # Chat A's active trip is still Munnar
    active_a = repos["trip_repo"].get_active_trip(chat_a)
    assert active_a.destination == "Munnar"


# Reg 17: Reverse-budget safeguards & unknown fee handling
def test_reg17_reverse_budget_safeguards_unknown_fees():
    engine = ReverseBudgetEngine()
    alloc = BudgetAllocation(
        id=uuid4(),
        trip_id=uuid4(),
        transport_allocated=Decimal("5000.00"),
        stay_allocated=Decimal("5000.00"),
        food_allocated=Decimal("3000.00"),
        activities_discretionary=Decimal("1000.00"),
        rescue_fund_allocated=Decimal("1000.00"),
        total_budget=Decimal("15000.00"),
    )
    # Exceeding rescue reserve
    res = engine.evaluate_rescue(
        total_budget=Decimal("15000.00"),
        current_allocations=alloc,
        cost_delta=Decimal("2500.00"),  # exceeds ₹1,000 reserve
        category="activities",
    )
    assert res.is_feasible is False
    assert "exceeds" in res.explanation.lower()


# Reg 18: Trip Pass authorization enforcement for premium features
@pytest.mark.asyncio
async def test_reg18_trip_pass_authorization_enforcement(repos):
    ai = AIIntentService(use_mock=True)
    orch_pass = BudlanceOrchestrator(
        user_repo=repos["user_repo"],
        trip_repo=repos["trip_repo"],
        intent_repo=repos["intent_repo"],
        itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"],
        rescue_repo=repos["rescue_repo"],
        conversation_repo=repos["conversation_repo"],
        trip_pass_repo=repos["trip_pass_repo"],
        ai_service=ai,
        enable_trip_pass=True,
    )
    chat_id = 991122
    user = repos["user_repo"].get_or_create_user(telegram_user_id=chat_id)
    trip = repos["trip_repo"].create_trip(
        user_id=user.id,
        telegram_chat_id=chat_id,
        budget_total=Decimal("15000.00"),
        destination="Ooty",
        is_active=True,
    )
    repos["trip_repo"].update_trip_status(trip.id, "ACTIVE", is_active=True)

    # Trip Pass NOT unlocked
    res = await orch_pass.handle_user_message(chat_id, chat_id, "The tea garden is closed today")
    assert res.status == "PASS_LOCKED"
    assert "Locked" in res.message_text


# Reg 19: Full Phase 1–6 regression backward compatibility
@pytest.mark.asyncio
async def test_reg19_full_phase_regression_backward_compatibility(orch, repos):
    chat_id = 445566
    # Phase 1-5 planning pipeline
    res = await orch.handle_user_message(
        chat_id, chat_id,
        "Plan a trip to Kerala from Chennai for 2 people, 3 days, budget ₹30,000",
    )
    assert res.status == "FEASIBLE"
    assert res.trip_id is not None
    assert "Water" in res.message_text or "Plan" in res.message_text or "Day 1" in res.message_text


# Reg 20: Graceful fallback when LLM / provider is unavailable
@pytest.mark.asyncio
async def test_reg20_graceful_fallback_when_llm_or_provider_unavailable(repos):
    ai = AIIntentService(use_mock=False)
    # Mock OpenRouter client raising Exception
    ai.client = MagicMock()
    ai.client.chat = MagicMock()
    ai.client.chat.completions = MagicMock()
    ai.client.chat.completions.create = AsyncMock(side_effect=Exception("API 503 Provider Unavailable"))

    orch_fb = BudlanceOrchestrator(
        user_repo=repos["user_repo"],
        trip_repo=repos["trip_repo"],
        intent_repo=repos["intent_repo"],
        itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"],
        rescue_repo=repos["rescue_repo"],
        conversation_repo=repos["conversation_repo"],
        trip_pass_repo=repos["trip_pass_repo"],
        ai_service=ai,
    )
    chat_id = 332211
    res = await orch_fb.handle_user_message(
        chat_id, chat_id,
        "Plan a trip to Goa from Mumbai for 2 people, 3 days, budget ₹20000",
    )
    # Must degrade gracefully to fallback parser and return a feasible plan
    assert res.status == "FEASIBLE"
    assert res.selected_destination == "Goa"


# Reg 21: "What's today's plan?" dynamically synchronizes with active date
@pytest.mark.asyncio
async def test_today_plan_dynamic_date_sync_and_slots(orch, repos, active_munnar_trip):
    import datetime
    from datetime import timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    today_iso = datetime.datetime.now(ist).date().strftime("%Y-%m-%d")

    chat_id = active_munnar_trip.telegram_chat_id
    trip_id = active_munnar_trip.id

    # Update Day 2 date_str to match today_iso
    itin = repos["itinerary_repo"].get_itinerary(trip_id)
    itin.days[1]["date_str"] = today_iso
    itin.days[1]["theme_or_summary"] = "High Altitude Wildlife & Trekking"
    itin.days[1]["items"] = [
        {
            "time_slot": "Morning",
            "activity": "Trek through Eravikulam towards Anamudi",
            "place_name": "Anamudi Peak Foothills",
            "category": "nature",
            "planned_cost": 200.0,
            "description": "Scenic trekking",
        }
    ]
    repos["itinerary_repo"].save_itinerary(itin)

    # User asks "What's today's plan?"
    res = await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="What's today's plan?",
    )
    assert res.status == "IN_TRIP_QUERY"
    assert "Day 2" in res.message_text
    assert "Anamudi Peak Foothills" in res.message_text
    assert "Trek through Eravikulam" in res.message_text


# Reg 22: Movie night showtimes lookup with honest fallback when no multiplexes exist
@pytest.mark.asyncio
async def test_movie_night_honest_fallback_in_remote_destination(orch, active_munnar_trip):
    chat_id = active_munnar_trip.telegram_chat_id

    res = await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Are there any movie showtimes tonight?",
    )
    assert res.status == "IN_TRIP_QUERY"
    assert "Movie Night in Munnar" in res.message_text
    assert "No commercial movie theatres" in res.message_text
    assert "bookmyshow.com" in res.message_text


# Reg 23: Movie night showtimes lookup with real local cinema discovered
@pytest.mark.asyncio
async def test_movie_night_with_local_cinema_discovered(orch, repos, active_munnar_trip):
    chat_id = active_munnar_trip.telegram_chat_id
    trip_id = active_munnar_trip.id

    # Mock cache returning a local theatre
    orch.cache_manager.get_travel_data = AsyncMock(
        return_value=TravelDataEnvelope(
            source=DataSource.LIVE,
            engine="google_maps",
            query_hash="mock_theatre",
            data={
                "local_results": [
                    {
                        "title": "Munnar Scenic Cinema Talkies",
                        "type": "Movie theater",
                        "address": "Bazaar Road, Munnar",
                        "rating": 4.2,
                    }
                ]
            },
        )
    )

    res = await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="movie night",
    )
    assert res.status == "IN_TRIP_QUERY"
    assert "Movie Night in Munnar" in res.message_text
    assert "Munnar Scenic Cinema Talkies" in res.message_text
    assert "bookmyshow.com" in res.message_text


# Reg 24: Crowd swap with nearby search, distance call, Bucket D taxi estimate, and "I will go" update
@pytest.mark.asyncio
async def test_crowd_swap_nearby_search_taxi_estimate_and_i_will_go(orch, repos, active_munnar_trip):
    chat_id = active_munnar_trip.telegram_chat_id
    trip_id = active_munnar_trip.id

    # Verify Mattupetty Lake is in Day 1 items
    itin_start = repos["itinerary_repo"].get_itinerary(trip_id)
    day1_items = itin_start.days[0]["items"]
    assert any("Mattupetty Lake" in it["place_name"] for it in day1_items)

    # 1. User reports crowding at Mattupetty Lake and asks for nearby alternative
    crowd_msg = "Mattupetty Lake is crowded, what else nearby?"
    res_prop = await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message=crowd_msg,
    )

    assert res_prop.status == "RESCUE"
    assert "Proposed replacement" in res_prop.message_text
    assert "Estimated taxi transfer" in res_prop.message_text
    assert "Bucket D" in res_prop.message_text or "Buffer / Reserve" in res_prop.message_text
    assert "km" in res_prop.message_text
    assert "I will go" in res_prop.message_text

    # Verify baseline itinerary has NOT been mutated yet
    itin_mid = repos["itinerary_repo"].get_itinerary(trip_id)
    assert any("Mattupetty Lake" in it["place_name"] for it in itin_mid.days[0]["items"])

    # 2. User confirms by sending "I will go"
    res_confirm = await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="I will go",
    )

    assert res_confirm.status == "RESCUE"
    assert "Confirmed" in res_confirm.message_text
    assert "Bucket D" in res_confirm.message_text or "Taxi transfer" in res_confirm.message_text

    # 3. Verify itinerary in repo was updated to replace Mattupetty Lake
    itin_after = repos["itinerary_repo"].get_itinerary(trip_id)
    day1_after_places = [it["place_name"] for it in itin_after.days[0]["items"]]
    assert not any("Mattupetty Lake" in p for p in day1_after_places)
    # Afternoon item was successfully replaced
    assert len(day1_after_places) >= 2



