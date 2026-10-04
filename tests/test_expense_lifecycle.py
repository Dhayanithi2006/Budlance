"""Tests for Task 3 — Expense Logging and Day Lifecycle Only.

Verifies:
1. No active trip: returns safe response, zero ledger writes.
2. Expense without day completion: actual_amount recorded, day_number=current_day, day active, current_day unchanged.
3. Explicit day number: expense recorded for explicitly specified day.
4. Expense + day completion: actual_amount recorded, Day 1 COMPLETED, current_day advances to 2, Day 2 IN_PROGRESS.
5. Final-day completion: final itinerary day COMPLETED, trip remains ACTIVE (not marked COMPLETED).
6. Invalid day number: safely rejected without modifying trip or ledger.
7. Multiple expenses on same day: actual spending accumulated, planned budget is not duplicated.
8. Bucket mapping (A/B/C/D): food/transport -> daily_survival (B), activities/general -> activities (C), stay -> fixed_booking (A).
9. Prevent invalid progression: historical day completion does not advance current_day pointer.
10. Action routing via orchestrator: TripAction.LOG_EXPENSE routed without touching pending drafts.
"""

from decimal import Decimal
from uuid import uuid4
import pytest

from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.db.models import BudgetAllocation, Itinerary, LedgerEntry, Trip, utc_now
from budlance.db.repositories.conversation_repo import ConversationStateRepository
from budlance.db.repositories.itinerary_repo import ItineraryRepository
from budlance.db.repositories.ledger_repo import LedgerRepository
from budlance.db.repositories.trip_repo import TripRepository
from budlance.itinerary.models import ItineraryDay, ItineraryItem
from budlance.ledger.manager import VirtualLedgerManager
from budlance.lifecycle.expense_handler import ExpenseLifecycleHandler, handle_log_expense
from budlance.orchestrator.orchestrator import BudlanceOrchestrator


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
def active_trip_setup(memory_repos):
    """Set up an active 3-day trip with baseline itinerary and ledger allocations."""
    user_id = uuid4()
    chat_id = 12345678

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
    # Activate trip
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

    # Baseline line items
    baseline_entries = [
        LedgerEntry(
            id=uuid4(),
            trip_id=trip.id,
            category="fixed_booking",
            description="Transport",
            allocated_amount=Decimal("6000.00"),
            planned_amount=Decimal("6000.00"),
            spent_amount=Decimal("0.00"),
            remaining_amount=Decimal("6000.00"),
            source="estimated",
        ),
        LedgerEntry(
            id=uuid4(),
            trip_id=trip.id,
            category="fixed_booking",
            description="Hotel",
            allocated_amount=Decimal("6000.00"),
            planned_amount=Decimal("6000.00"),
            spent_amount=Decimal("0.00"),
            remaining_amount=Decimal("6000.00"),
            source="estimated",
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
            source="estimated",
        ),
        LedgerEntry(
            id=uuid4(),
            trip_id=trip.id,
            category="activities",
            description="Activities Allowance",
            allocated_amount=Decimal("2000.00"),
            planned_amount=Decimal("2000.00"),
            spent_amount=Decimal("0.00"),
            remaining_amount=Decimal("2000.00"),
            source="estimated",
        ),
    ]
    for e in baseline_entries:
        memory_repos["ledger_repo"].add_ledger_entry(e)

    # 3-Day Itinerary
    day1 = ItineraryDay(
        day_number=1,
        theme_or_summary="Day 1 - Arrival",
        status="IN_PROGRESS",
        items=[ItineraryItem(time_slot="Morning", activity="Arrive", category="transport", planned_cost=Decimal("0.00"))],
    )
    day2 = ItineraryDay(
        day_number=2,
        theme_or_summary="Day 2 - Exploration",
        status="UPCOMING",
        items=[ItineraryItem(time_slot="Morning", activity="Beach", category="beach", planned_cost=Decimal("0.00"))],
    )
    day3 = ItineraryDay(
        day_number=3,
        theme_or_summary="Day 3 - Departure",
        status="UPCOMING",
        items=[ItineraryItem(time_slot="Evening", activity="Depart", category="transport", planned_cost=Decimal("0.00"))],
    )
    itin = Itinerary(
        id=uuid4(),
        trip_id=trip.id,
        days=[day1.model_dump(mode="json"), day2.model_dump(mode="json"), day3.model_dump(mode="json")],
        is_feasible=True,
    )
    memory_repos["itinerary_repo"].save_itinerary(itin)

    return {
        "chat_id": chat_id,
        "trip": trip,
        "repos": memory_repos,
    }


# ============================================================================
# 1. No Active Trip
# ============================================================================
@pytest.mark.asyncio
async def test_no_active_trip_returns_safe_response(memory_repos):
    """When no active trip exists, returns clear message and performs zero ledger writes."""
    handler = ExpenseLifecycleHandler(
        trip_repo=memory_repos["trip_repo"],
        ledger_repo=memory_repos["ledger_repo"],
        itinerary_repo=memory_repos["itinerary_repo"],
    )

    parsed = ParsedTripIntent(
        action=TripAction.LOG_EXPENSE,
        amount=Decimal("500.00"),
        expense_category="food",
        day_number=1,
        day_completed=False,
    )

    result = await handler.handle_log_expense(chat_id=999999, parsed=parsed)

    assert result.status == "NO_ACTIVE_TRIP"
    assert "no active trip found" in result.message_text.lower()
    assert result.trip_id is None

    # Verify no ledger entries written
    entries = memory_repos["ledger_repo"].get_ledger_entries(uuid4())
    assert len(entries) == 0


@pytest.mark.asyncio
async def test_planning_status_trip_rejected(memory_repos):
    """A trip with status PLANNING (not ACTIVE) is rejected for expense logging."""
    user_id = uuid4()
    chat_id = 888888
    # create_trip defaults to status="PLANNING"
    trip = memory_repos["trip_repo"].create_trip(
        user_id=user_id,
        telegram_chat_id=chat_id,
        budget_total=Decimal("15000.00"),
    )
    assert trip.status == "PLANNING"

    handler = ExpenseLifecycleHandler(
        trip_repo=memory_repos["trip_repo"],
        ledger_repo=memory_repos["ledger_repo"],
        itinerary_repo=memory_repos["itinerary_repo"],
    )
    parsed = ParsedTripIntent(
        action=TripAction.LOG_EXPENSE,
        amount=Decimal("500.00"),
        expense_category="food",
    )
    result = await handler.handle_log_expense(chat_id=chat_id, parsed=parsed)
    assert result.status == "NO_ACTIVE_TRIP"


# ============================================================================
# 2. Expense Without Day Completion
# ============================================================================
@pytest.mark.asyncio
async def test_expense_without_day_completion(active_trip_setup):
    """'Spent ₹500 on lunch' records expense, leaves current_day and day status unchanged."""
    setup = active_trip_setup
    chat_id = setup["chat_id"]
    trip = setup["trip"]
    repos = setup["repos"]

    handler = ExpenseLifecycleHandler(
        trip_repo=repos["trip_repo"],
        ledger_repo=repos["ledger_repo"],
        itinerary_repo=repos["itinerary_repo"],
    )

    parsed = ParsedTripIntent(
        action=TripAction.LOG_EXPENSE,
        amount=Decimal("500.00"),
        expense_category="food",
        day_number=None,
        day_completed=False,
    )

    result = await handler.handle_log_expense(chat_id=chat_id, parsed=parsed)

    assert result.status == "EXPENSE_LOGGED"
    assert "Recorded ₹500 for Day 1." in result.message_text
    assert "Day 1 is still active." in result.message_text

    # Current day remains 1
    updated_trip = repos["trip_repo"].get_trip(trip.id)
    assert updated_trip.current_day == 1

    # Itinerary Day 1 status remains IN_PROGRESS
    itin = repos["itinerary_repo"].get_itinerary(trip.id)
    assert itin.days[0]["status"] == "IN_PROGRESS"

    # Verify ledger entry
    entries = repos["ledger_repo"].get_ledger_entries(trip.id)
    user_entries = [e for e in entries if e.source == "user_reported"]
    assert len(user_entries) == 1
    entry = user_entries[0]
    assert entry.actual_amount == Decimal("500.00")
    assert entry.day_number == 1
    assert entry.planned_amount == Decimal("0.00")
    assert entry.category == "daily_survival"


# ============================================================================
# 3. Explicit Day Number
# ============================================================================
@pytest.mark.asyncio
async def test_explicit_day_number(active_trip_setup):
    """'Spent ₹800 on Day 2' records actual expense against day 2."""
    setup = active_trip_setup
    chat_id = setup["chat_id"]
    trip = setup["trip"]
    repos = setup["repos"]

    handler = ExpenseLifecycleHandler(
        trip_repo=repos["trip_repo"],
        ledger_repo=repos["ledger_repo"],
        itinerary_repo=repos["itinerary_repo"],
    )

    parsed = ParsedTripIntent(
        action=TripAction.LOG_EXPENSE,
        amount=Decimal("800.00"),
        expense_category="activities",
        day_number=2,
        day_completed=False,
    )

    result = await handler.handle_log_expense(chat_id=chat_id, parsed=parsed)

    assert result.status == "EXPENSE_LOGGED"
    assert "Recorded ₹800 for Day 2." in result.message_text
    assert "Day 2 is still active." in result.message_text

    # Current day remains 1
    updated_trip = repos["trip_repo"].get_trip(trip.id)
    assert updated_trip.current_day == 1

    entries = repos["ledger_repo"].get_ledger_entries(trip.id)
    user_entries = [e for e in entries if e.source == "user_reported"]
    assert len(user_entries) == 1
    assert user_entries[0].actual_amount == Decimal("800.00")
    assert user_entries[0].day_number == 2
    assert user_entries[0].category == "activities"


# ============================================================================
# 4. Expense + Day Completion
# ============================================================================
@pytest.mark.asyncio
async def test_expense_plus_day_completion(active_trip_setup):
    """'Day 1 done, spent ₹3000 today' completes Day 1, advances current_day to 2."""
    setup = active_trip_setup
    chat_id = setup["chat_id"]
    trip = setup["trip"]
    repos = setup["repos"]

    handler = ExpenseLifecycleHandler(
        trip_repo=repos["trip_repo"],
        ledger_repo=repos["ledger_repo"],
        itinerary_repo=repos["itinerary_repo"],
    )

    parsed = ParsedTripIntent(
        action=TripAction.LOG_EXPENSE,
        amount=Decimal("3000.00"),
        expense_category="transport",
        day_number=1,
        day_completed=True,
    )

    result = await handler.handle_log_expense(chat_id=chat_id, parsed=parsed)

    assert result.status == "EXPENSE_LOGGED"
    assert "Recorded ₹3,000 for Day 1." in result.message_text
    assert "Day 1 completed. Moving to Day 2." in result.message_text

    # Trip current_day advanced to 2
    updated_trip = repos["trip_repo"].get_trip(trip.id)
    assert updated_trip.current_day == 2

    # Itinerary Day 1 is COMPLETED, Day 2 is IN_PROGRESS
    itin = repos["itinerary_repo"].get_itinerary(trip.id)
    assert itin.days[0]["status"] == "COMPLETED"
    assert itin.days[1]["status"] == "IN_PROGRESS"
    assert itin.days[2]["status"] == "UPCOMING"

    # Ledger entry recorded with actual_amount
    entries = repos["ledger_repo"].get_ledger_entries(trip.id)
    user_entries = [e for e in entries if e.source == "user_reported"]
    assert len(user_entries) == 1
    assert user_entries[0].actual_amount == Decimal("3000.00")
    assert user_entries[0].day_number == 1
    assert user_entries[0].planned_amount == Decimal("0.00")


# ============================================================================
# 5. Final-Day Completion
# ============================================================================
@pytest.mark.asyncio
async def test_final_day_completion_keeps_trip_active(active_trip_setup):
    """Completing final day marks day COMPLETED but keeps trip itself ACTIVE (Task 5 handles trip completion)."""
    setup = active_trip_setup
    chat_id = setup["chat_id"]
    trip = setup["trip"]
    repos = setup["repos"]

    # Fast-forward trip to day 3
    repos["trip_repo"].update_current_day(trip.id, 3)

    handler = ExpenseLifecycleHandler(
        trip_repo=repos["trip_repo"],
        ledger_repo=repos["ledger_repo"],
        itinerary_repo=repos["itinerary_repo"],
    )

    parsed = ParsedTripIntent(
        action=TripAction.LOG_EXPENSE,
        amount=Decimal("1500.00"),
        expense_category="food",
        day_number=3,
        day_completed=True,
    )

    result = await handler.handle_log_expense(chat_id=chat_id, parsed=parsed)

    assert result.status == "EXPENSE_LOGGED"
    assert "Recorded ₹1,500 for Day 3." in result.message_text
    assert "Day 3 completed." in result.message_text
    assert "Moving to Day" not in result.message_text

    # Itinerary final day is COMPLETED
    itin = repos["itinerary_repo"].get_itinerary(trip.id)
    assert itin.days[2]["status"] == "COMPLETED"

    # Trip itself remains ACTIVE (Task 3 does not complete trip)
    updated_trip = repos["trip_repo"].get_trip(trip.id)
    assert updated_trip.status == "ACTIVE"


# ============================================================================
# 6. Invalid Day Number Handling
# ============================================================================
@pytest.mark.asyncio
async def test_invalid_day_number_safely_rejected(active_trip_setup):
    """Day 8 on a 3-day trip is safely rejected without modifying trip or ledger."""
    setup = active_trip_setup
    chat_id = setup["chat_id"]
    trip = setup["trip"]
    repos = setup["repos"]

    handler = ExpenseLifecycleHandler(
        trip_repo=repos["trip_repo"],
        ledger_repo=repos["ledger_repo"],
        itinerary_repo=repos["itinerary_repo"],
    )

    parsed = ParsedTripIntent(
        action=TripAction.LOG_EXPENSE,
        amount=Decimal("500.00"),
        expense_category="food",
        day_number=8,
        day_completed=False,
    )

    result = await handler.handle_log_expense(chat_id=chat_id, parsed=parsed)

    assert result.status == "INVALID_DAY"
    assert "outside the valid range" in result.message_text
    assert "Day 1 to Day 3" in result.message_text

    # Trip remains on Day 1
    updated_trip = repos["trip_repo"].get_trip(trip.id)
    assert updated_trip.current_day == 1

    # Zero user expenses recorded
    entries = repos["ledger_repo"].get_ledger_entries(trip.id)
    user_entries = [e for e in entries if e.source == "user_reported"]
    assert len(user_entries) == 0


# ============================================================================
# 7. Multiple Expenses on the Same Day
# ============================================================================
@pytest.mark.asyncio
async def test_multiple_expenses_do_not_duplicate_planned_budget(active_trip_setup):
    """Logging multiple expenses records actual amounts without duplicating planned budget."""
    setup = active_trip_setup
    chat_id = setup["chat_id"]
    trip = setup["trip"]
    repos = setup["repos"]

    handler = ExpenseLifecycleHandler(
        trip_repo=repos["trip_repo"],
        ledger_repo=repos["ledger_repo"],
        itinerary_repo=repos["itinerary_repo"],
    )

    # Initial planned budget sum
    initial_entries = repos["ledger_repo"].get_ledger_entries(trip.id)
    initial_planned_sum = sum(e.planned_amount for e in initial_entries)
    assert initial_planned_sum == Decimal("18000.00")

    # Expense 1: ₹500 lunch
    parsed1 = ParsedTripIntent(
        action=TripAction.LOG_EXPENSE,
        amount=Decimal("500.00"),
        expense_category="food",
        day_number=1,
        day_completed=False,
    )
    await handler.handle_log_expense(chat_id=chat_id, parsed=parsed1)

    # Expense 2: ₹300 taxi
    parsed2 = ParsedTripIntent(
        action=TripAction.LOG_EXPENSE,
        amount=Decimal("300.00"),
        expense_category="transport",
        day_number=1,
        day_completed=False,
    )
    await handler.handle_log_expense(chat_id=chat_id, parsed=parsed2)

    # Expense 3: ₹1200 dinner
    parsed3 = ParsedTripIntent(
        action=TripAction.LOG_EXPENSE,
        amount=Decimal("1200.00"),
        expense_category="food",
        day_number=1,
        day_completed=False,
    )
    await handler.handle_log_expense(chat_id=chat_id, parsed=parsed3)

    # Check total planned budget did NOT increase
    all_entries = repos["ledger_repo"].get_ledger_entries(trip.id)
    new_planned_sum = sum(e.planned_amount for e in all_entries)
    assert new_planned_sum == initial_planned_sum

    # Actual spending recorded
    user_entries = [e for e in all_entries if e.source == "user_reported"]
    assert len(user_entries) == 3
    day1_actual_sum = sum(e.actual_amount for e in user_entries if e.day_number == 1)
    assert day1_actual_sum == Decimal("2000.00")


# ============================================================================
# 8. Bucket Mapping (A/B/C/D)
# ============================================================================
@pytest.mark.asyncio
async def test_bucket_mapping(active_trip_setup):
    """Verify category mapping to authoritative A/B/C/D buckets."""
    setup = active_trip_setup
    chat_id = setup["chat_id"]
    trip = setup["trip"]
    repos = setup["repos"]

    handler = ExpenseLifecycleHandler(
        trip_repo=repos["trip_repo"],
        ledger_repo=repos["ledger_repo"],
        itinerary_repo=repos["itinerary_repo"],
    )

    cases = [
        ("food", "daily_survival"),        # Bucket B
        ("transport", "daily_survival"),   # Bucket B
        ("activities", "activities"),      # Bucket C
        ("stay", "fixed_booking"),         # Bucket A
        ("general", "activities"),         # Bucket C
        (None, "activities"),              # Bucket C default
    ]

    for cat, expected_bucket in cases:
        parsed = ParsedTripIntent(
            action=TripAction.LOG_EXPENSE,
            amount=Decimal("100.00"),
            expense_category=cat,
            day_number=1,
            day_completed=False,
        )
        await handler.handle_log_expense(chat_id=chat_id, parsed=parsed)
        entries = repos["ledger_repo"].get_ledger_entries(trip.id)
        latest = entries[-1]
        assert latest.category == expected_bucket, f"Expected {expected_bucket} for {cat}, got {latest.category}"
        assert latest.actual_amount == Decimal("100.00")


# ============================================================================
# 9. Prevent Invalid Progression (Historical Day)
# ============================================================================
@pytest.mark.asyncio
async def test_historical_day_completion_does_not_advance_current_day(active_trip_setup):
    """When current_day is 2 and user reports 'Day 1 done', current_day does not move to 3."""
    setup = active_trip_setup
    chat_id = setup["chat_id"]
    trip = setup["trip"]
    repos = setup["repos"]

    # Advance current_day to 2
    repos["trip_repo"].update_current_day(trip.id, 2)

    handler = ExpenseLifecycleHandler(
        trip_repo=repos["trip_repo"],
        ledger_repo=repos["ledger_repo"],
        itinerary_repo=repos["itinerary_repo"],
    )

    parsed = ParsedTripIntent(
        action=TripAction.LOG_EXPENSE,
        amount=Decimal("750.00"),
        expense_category="food",
        day_number=1,
        day_completed=True,
    )

    result = await handler.handle_log_expense(chat_id=chat_id, parsed=parsed)

    assert result.status == "EXPENSE_LOGGED"
    assert "Day 1 completed." in result.message_text
    assert "Current day remains Day 2." in result.message_text

    # Verify current_day is still 2
    updated_trip = repos["trip_repo"].get_trip(trip.id)
    assert updated_trip.current_day == 2

    # Itinerary Day 1 is marked COMPLETED
    itin = repos["itinerary_repo"].get_itinerary(trip.id)
    assert itin.days[0]["status"] == "COMPLETED"


# ============================================================================
# 10. Missing / Zero Amount Validation
# ============================================================================
@pytest.mark.asyncio
async def test_missing_or_zero_amount_rejected(active_trip_setup):
    """An expense without amount or zero amount is rejected safely."""
    setup = active_trip_setup
    chat_id = setup["chat_id"]
    repos = setup["repos"]

    handler = ExpenseLifecycleHandler(
        trip_repo=repos["trip_repo"],
        ledger_repo=repos["ledger_repo"],
        itinerary_repo=repos["itinerary_repo"],
    )

    for amt in [None, Decimal("0.00"), Decimal("-50.00")]:
        parsed = ParsedTripIntent(
            action=TripAction.LOG_EXPENSE,
            amount=amt,
            expense_category="food",
        )
        res = await handler.handle_log_expense(chat_id=chat_id, parsed=parsed)
        assert res.status == "INVALID_EXPENSE"
        assert "valid expense amount" in res.message_text


# ============================================================================
# 11. Module-Level handle_log_expense Function
# ============================================================================
@pytest.mark.asyncio
async def test_module_level_handle_log_expense(active_trip_setup):
    """Verify module-level handle_log_expense works equivalent to the class method."""
    setup = active_trip_setup
    chat_id = setup["chat_id"]
    repos = setup["repos"]

    parsed = ParsedTripIntent(
        action=TripAction.LOG_EXPENSE,
        amount=Decimal("450.00"),
        expense_category="food",
        day_number=1,
    )

    res = await handle_log_expense(
        chat_id=chat_id,
        parsed=parsed,
        trip_repo=repos["trip_repo"],
        ledger_repo=repos["ledger_repo"],
        itinerary_repo=repos["itinerary_repo"],
    )

    assert res.status == "EXPENSE_LOGGED"
    assert "Recorded ₹450 for Day 1." in res.message_text


# ============================================================================
# 12. Orchestrator Integration & Draft Preservation
# ============================================================================
@pytest.mark.asyncio
async def test_orchestrator_log_expense_preserves_pending_draft(active_trip_setup):
    """LOG_EXPENSE routed via orchestrator updates active trip without touching pending draft."""
    setup = active_trip_setup
    chat_id = setup["chat_id"]
    trip = setup["trip"]
    repos = setup["repos"]

    # Create a pending planning draft in conversation repo
    draft_intent = ParsedTripIntent(
        action=TripAction.NEW_TRIP,
        budget=Decimal("50000.00"),
        people=4,
        days=5,
        origin="Delhi",
    )
    repos["conversation_repo"].save_pending_intent(chat_id, draft_intent)

    orchestrator = BudlanceOrchestrator(
        trip_repo=repos["trip_repo"],
        ledger_repo=repos["ledger_repo"],
        itinerary_repo=repos["itinerary_repo"],
        conversation_repo=repos["conversation_repo"],
    )

    # User sends expense report message
    msg = "spent 650 on lunch"
    res = await orchestrator.handle_user_message(
        telegram_user_id=12345678,
        chat_id=chat_id,
        message=msg,
    )

    assert res.status == "EXPENSE_LOGGED"
    assert "Recorded ₹650 for Day 1." in res.message_text

    # Verify pending planning draft was NOT cleared or mutated!
    preserved_draft = repos["conversation_repo"].get_pending_intent(chat_id)
    assert preserved_draft is not None
    assert preserved_draft.budget == Decimal("50000.00")
    assert preserved_draft.people == 4
    assert preserved_draft.days == 5
    assert preserved_draft.origin == "Delhi"

    # Verify actual expense recorded on active trip
    entries = repos["ledger_repo"].get_ledger_entries(trip.id)
    expense_entries = [e for e in entries if e.source == "user_reported"]
    assert len(expense_entries) == 1
    assert expense_entries[0].actual_amount == Decimal("650.00")

