"""Tests for Task 4 — Remaining-Trip Re-optimization Only.

Verifies:
Test 1 — Over-budget Day 1: Day 1 locked, ₹500 overrun reflected, Days 2–3 receive reduced future planning budget.
Test 2 — Completed day cannot be modified: Day 1 (COMPLETED) remains completely unchanged.
Test 3 — Actual amount does not fall back to planned amount: planned=5000, actual=None does not count as actual spend.
Test 4 — Actual spending is counted: actual_amount=2000 contributes to actual-spend accounting exactly once.
Test 5 — Committed booking is preserved: Bucket A commitments excluded from discretionary budget, no double-counting when actual paid.
Test 6 — Multiple expenses on one day: food=800, local transport=400, activity=600 correctly bucketed to B=1200, C=600 without duplicating baseline.
Test 7 — Final day: current_day=final day returns no-op (None); trip remains ACTIVE.
Test 8 — Existing optimizer regression: OptimizationEngine works unchanged when locked_days=None.
Test 9 — No external API bypass: Reoptimizer does not directly call SerpApi.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4
import pytest

from budlance.db.models import BudgetAllocation, Itinerary, LedgerEntry, Trip, utc_now
from budlance.db.repositories.itinerary_repo import ItineraryRepository
from budlance.db.repositories.ledger_repo import LedgerRepository
from budlance.db.repositories.trip_repo import TripRepository
from budlance.engine.budget import ReverseBudgetEngine
from budlance.engine.optimizer import OptimizationEngine
from budlance.estimation.estimator import EstimationLayer
from budlance.itinerary.models import ItineraryDay, ItineraryItem
from budlance.lifecycle.reoptimizer import (
    RemainingTripReoptimizer,
    calculate_trip_financial_state,
    get_locked_and_remaining_days,
    reoptimize_remaining_trip,
)
from budlance.schemas.travel import FlightOption, FoodEstimate, HotelOption, LocalTransitEstimate


@pytest.fixture
def memory_repos():
    """In-memory repositories for deterministic reoptimization testing."""
    return {
        "trip_repo": TripRepository(client=None),
        "ledger_repo": LedgerRepository(client=None),
        "itinerary_repo": ItineraryRepository(client=None),
    }


@pytest.fixture
def active_trip_setup(memory_repos):
    """Set up an active 3-day trip with baseline itinerary and ledger allocations."""
    user_id = uuid4()
    chat_id = 991122

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
    # Update trip status to ACTIVE
    memory_repos["trip_repo"].update_trip_status(trip.id, "ACTIVE")
    trip = memory_repos["trip_repo"].get_trip(trip.id)

    # Master allocation: Bucket A = 12000 (transport 6000 + stay 6000), B = 4000, C = 2000, D = 2000
    alloc = BudgetAllocation(
        id=uuid4(),
        trip_id=trip.id,
        transport_allocated=Decimal("6000.00"),
        stay_allocated=Decimal("6000.00"),
        food_allocated=Decimal("3000.00"),
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
            description="Transport (Onward & Return)",
            allocated_amount=Decimal("6000.00"),
            planned_amount=Decimal("6000.00"),
            spent_amount=Decimal("0.00"),
            remaining_amount=Decimal("6000.00"),
            actual_amount=None,
            source="estimated",
        ),
        LedgerEntry(
            id=uuid4(),
            trip_id=trip.id,
            category="fixed_booking",
            description="Accommodation (Hotel)",
            allocated_amount=Decimal("6000.00"),
            planned_amount=Decimal("6000.00"),
            spent_amount=Decimal("0.00"),
            remaining_amount=Decimal("6000.00"),
            actual_amount=None,
            source="estimated",
        ),
        LedgerEntry(
            id=uuid4(),
            trip_id=trip.id,
            category="daily_survival",
            description="Food & Meals Allowance",
            allocated_amount=Decimal("3000.00"),
            planned_amount=Decimal("3000.00"),
            spent_amount=Decimal("0.00"),
            remaining_amount=Decimal("3000.00"),
            actual_amount=None,
            source="estimated",
        ),
        LedgerEntry(
            id=uuid4(),
            trip_id=trip.id,
            category="daily_survival",
            description="Local Transit Allowance",
            allocated_amount=Decimal("1000.00"),
            planned_amount=Decimal("1000.00"),
            spent_amount=Decimal("0.00"),
            remaining_amount=Decimal("1000.00"),
            actual_amount=None,
            source="estimated",
        ),
        LedgerEntry(
            id=uuid4(),
            trip_id=trip.id,
            category="activities",
            description="Activities / Discretionary",
            allocated_amount=Decimal("2000.00"),
            planned_amount=Decimal("2000.00"),
            spent_amount=Decimal("0.00"),
            remaining_amount=Decimal("2000.00"),
            actual_amount=None,
            source="estimated",
        ),
    ]
    for e in baseline_entries:
        memory_repos["ledger_repo"].add_ledger_entry(e)

    # 3-Day Itinerary
    day1 = ItineraryDay(
        day_number=1,
        theme_or_summary="Day 1 - Arrival & Beach",
        status="IN_PROGRESS",
        items=[
            ItineraryItem(time_slot="Morning", activity="Arrive Goa", category="transport", planned_cost=Decimal("0.00")),
            ItineraryItem(time_slot="Afternoon", activity="Lunch", category="food", planned_cost=Decimal("1500.00")),
        ],
        daily_estimated_cost=Decimal("1500.00"),
    )
    day2 = ItineraryDay(
        day_number=2,
        theme_or_summary="Day 2 - Sightseeing",
        status="UPCOMING",
        items=[
            ItineraryItem(time_slot="Morning", activity="Fort Aguada", category="attraction", planned_cost=Decimal("200.00")),
            ItineraryItem(time_slot="Afternoon", activity="Seafood Lunch", category="food", planned_cost=Decimal("1000.00")),
        ],
        daily_estimated_cost=Decimal("1200.00"),
    )
    day3 = ItineraryDay(
        day_number=3,
        theme_or_summary="Day 3 - Markets & Return",
        status="UPCOMING",
        items=[
            ItineraryItem(time_slot="Morning", activity="Anjuna Market", category="attraction", planned_cost=Decimal("0.00")),
            ItineraryItem(time_slot="Evening", activity="Return Flight", category="transport", planned_cost=Decimal("0.00")),
        ],
        daily_estimated_cost=Decimal("800.00"),
    )
    itin = Itinerary(
        id=uuid4(),
        trip_id=trip.id,
        days=[day1.model_dump(mode="json"), day2.model_dump(mode="json"), day3.model_dump(mode="json")],
        is_feasible=True,
    )
    memory_repos["itinerary_repo"].save_itinerary(itin)

    return {
        "trip": trip,
        "repos": memory_repos,
    }


# ============================================================================
# Test 1 — Over-budget Day 1
# ============================================================================
@pytest.mark.asyncio
async def test_over_budget_day_1(active_trip_setup):
    """Day 1 planned B = ₹1,500, actual B = ₹2,000.

    Day 1 locked, ₹500 overrun reflected, Days 2–3 receive reduced future planning budget.
    """
    setup = active_trip_setup
    trip = setup["trip"]
    repos = setup["repos"]

    # Record actual spend on Day 1: ₹2,000 for food (an overrun of ₹500 compared to planned ₹1,500)
    overrun_entry = LedgerEntry(
        id=uuid4(),
        trip_id=trip.id,
        category="daily_survival",
        description="Day 1 Lunch actual expense",
        allocated_amount=Decimal("0.00"),
        planned_amount=Decimal("0.00"),
        spent_amount=Decimal("2000.00"),
        actual_amount=Decimal("2000.00"),
        day_number=1,
        source="user_reported",
    )
    repos["ledger_repo"].add_ledger_entry(overrun_entry)

    # Day 1 is completed and current_day moves to 2
    repos["trip_repo"].update_current_day(trip.id, 2)
    itin = repos["itinerary_repo"].get_itinerary(trip.id)
    itin.days[0]["status"] = "COMPLETED"
    itin.days[1]["status"] = "IN_PROGRESS"
    repos["itinerary_repo"].save_itinerary(itin)

    trip = repos["trip_repo"].get_trip(trip.id)

    # 1. Verify financial state calculation
    fin_state = calculate_trip_financial_state(trip.id, trip.budget_total, repos["ledger_repo"])
    assert fin_state["actual_spent_by_category"]["daily_survival"] == Decimal("2000.00")
    # Committed Bucket A = 12000 (transport + stay)
    assert fin_state["committed_A_total"] == Decimal("12000.00")
    # Remaining budget = 20000 - 12000 - 2000 = 6000 (reflects the ₹500 overrun from original 6500)
    assert fin_state["remaining_budget"] == Decimal("6000.00")

    # 2. Check locked and remaining days
    locked_days, remaining_days = get_locked_and_remaining_days(trip, itin, repos["ledger_repo"])
    assert 1 in locked_days
    assert remaining_days == [2, 3]

    # 3. Run reoptimization
    result = await reoptimize_remaining_trip(
        trip=trip,
        trip_repo=repos["trip_repo"],
        ledger_repo=repos["ledger_repo"],
        itinerary_repo=repos["itinerary_repo"],
    )

    assert result is not None
    # Verify Days 2–3 received the reduced future planning budget (₹6,000 instead of ₹6,500)
    assert result.final_evaluation.breakdown.total_budget == Decimal("6000.00")
    # Verify Day 1 remained locked and completed in itinerary
    itin_after = repos["itinerary_repo"].get_itinerary(trip.id)
    assert itin_after.days[0]["status"] == "COMPLETED"


# ============================================================================
# Test 2 — Completed day cannot be modified
# ============================================================================
@pytest.mark.asyncio
async def test_completed_day_cannot_be_modified(active_trip_setup):
    """Day 1 = COMPLETED, Day 2 = UPCOMING, Day 3 = UPCOMING. Day 1 remains completely unchanged."""
    setup = active_trip_setup
    trip = setup["trip"]
    repos = setup["repos"]

    # Mark Day 1 COMPLETED
    itin = repos["itinerary_repo"].get_itinerary(trip.id)
    itin.days[0]["status"] = "COMPLETED"
    repos["itinerary_repo"].save_itinerary(itin)

    # Save a deep snapshot of Day 1 before reoptimization
    day1_before = dict(itin.days[0])

    # Run reoptimization with small budget that forces downgrades on future days
    trip_low = trip.model_copy(update={"budget_total": Decimal("14000.00")})
    result = await reoptimize_remaining_trip(
        trip=trip_low,
        trip_repo=repos["trip_repo"],
        ledger_repo=repos["ledger_repo"],
        itinerary_repo=repos["itinerary_repo"],
        force=True,
    )

    # Verify Day 1 in persisted itinerary is 100% unchanged
    itin_after = repos["itinerary_repo"].get_itinerary(trip.id)
    day1_after = itin_after.days[0]

    assert day1_after["status"] == "COMPLETED"
    assert day1_after["theme_or_summary"] == day1_before["theme_or_summary"]
    assert day1_after["items"] == day1_before["items"]
    assert day1_after["daily_estimated_cost"] == day1_before["daily_estimated_cost"]


# ============================================================================
# Test 3 — Actual amount does not fall back to planned amount
# ============================================================================
def test_actual_amount_does_not_fall_back_to_planned(memory_repos):
    """planned=5000, actual=None does NOT count as actual spending."""
    trip_id = uuid4()
    entry = LedgerEntry(
        id=uuid4(),
        trip_id=trip_id,
        category="daily_survival",
        description="Planned Food Allowance",
        planned_amount=Decimal("5000.00"),
        actual_amount=None,  # Unknown
        source="estimated",
    )
    memory_repos["ledger_repo"].add_ledger_entry(entry)

    fin_state = calculate_trip_financial_state(
        trip_id=trip_id,
        budget_total=Decimal("20000.00"),
        ledger_repo=memory_repos["ledger_repo"],
    )

    # actual spent must be 0, NEVER 5000
    assert fin_state["total_actual_spent"] == Decimal("0.00")
    assert fin_state["actual_spent_by_category"]["daily_survival"] == Decimal("0.00")


# ============================================================================
# Test 4 — Actual spending is counted
# ============================================================================
def test_actual_spending_is_counted(memory_repos):
    """actual_amount=2000 contributes to actual-spend accounting exactly once."""
    trip_id = uuid4()
    entry = LedgerEntry(
        id=uuid4(),
        trip_id=trip_id,
        category="daily_survival",
        description="User logged dinner",
        planned_amount=Decimal("0.00"),
        actual_amount=Decimal("2000.00"),
        source="user_reported",
    )
    memory_repos["ledger_repo"].add_ledger_entry(entry)

    fin_state = calculate_trip_financial_state(
        trip_id=trip_id,
        budget_total=Decimal("20000.00"),
        ledger_repo=memory_repos["ledger_repo"],
    )

    assert fin_state["total_actual_spent"] == Decimal("2000.00")
    assert fin_state["actual_spent_by_category"]["daily_survival"] == Decimal("2000.00")


# ============================================================================
# Test 5 — Committed booking is preserved
# ============================================================================
def test_committed_booking_preserved_and_no_double_count(memory_repos):
    """Committed Bucket A costs are excluded from discretionary budget and not double-counted when paid."""
    trip_id = uuid4()
    total_budget = Decimal("20000.00")

    # Baseline planned commitments in Bucket A
    memory_repos["ledger_repo"].add_ledger_entry(
        LedgerEntry(
            id=uuid4(),
            trip_id=trip_id,
            category="fixed_booking",
            description="Committed Flight",
            planned_amount=Decimal("6000.00"),
            actual_amount=None,
            source="estimated",
        )
    )
    memory_repos["ledger_repo"].add_ledger_entry(
        LedgerEntry(
            id=uuid4(),
            trip_id=trip_id,
            category="fixed_booking",
            description="Committed Hotel",
            planned_amount=Decimal("6000.00"),
            actual_amount=None,
            source="estimated",
        )
    )

    # 1. Before actual payment: Bucket A commitment = 12000, remaining usable = 8000
    state1 = calculate_trip_financial_state(trip_id, total_budget, memory_repos["ledger_repo"])
    assert state1["committed_A_total"] == Decimal("12000.00")
    assert state1["remaining_budget"] == Decimal("8000.00")

    # 2. Later, user logs actual payment for hotel: ₹6000
    memory_repos["ledger_repo"].add_ledger_entry(
        LedgerEntry(
            id=uuid4(),
            trip_id=trip_id,
            category="fixed_booking",
            description="Hotel actual payment",
            planned_amount=Decimal("0.00"),
            actual_amount=Decimal("6000.00"),
            source="user_reported",
        )
    )

    state2 = calculate_trip_financial_state(trip_id, total_budget, memory_repos["ledger_repo"])
    # Bucket A commitment remains ₹12,000 (₹6,000 actual + ₹6,000 unpaid flight)
    assert state2["committed_A_total"] == Decimal("12000.00")
    # Remaining usable budget is STILL ₹8,000 (hotel was NOT double-counted)
    assert state2["remaining_budget"] == Decimal("8000.00")


# ============================================================================
# Test 6 — Multiple expenses on one day
# ============================================================================
def test_multiple_expenses_on_one_day(memory_repos):
    """food=800, transport=400, activity=600 -> B=1200, C=600 without duplicating baseline."""
    trip_id = uuid4()

    # Initial baseline
    baseline_food = LedgerEntry(
        id=uuid4(),
        trip_id=trip_id,
        category="daily_survival",
        description="Planned Food",
        planned_amount=Decimal("3000.00"),
        actual_amount=None,
    )
    memory_repos["ledger_repo"].add_ledger_entry(baseline_food)

    # 3 user expenses logged on Day 1
    expenses = [
        ("daily_survival", Decimal("800.00"), "Food"),
        ("daily_survival", Decimal("400.00"), "Local transport"),
        ("activities", Decimal("600.00"), "Scuba Diving"),
    ]
    for cat, amt, desc in expenses:
        memory_repos["ledger_repo"].add_ledger_entry(
            LedgerEntry(
                id=uuid4(),
                trip_id=trip_id,
                category=cat,
                description=desc,
                planned_amount=Decimal("0.00"),
                actual_amount=amt,
                day_number=1,
                source="user_reported",
            )
        )

    state = calculate_trip_financial_state(trip_id, Decimal("10000.00"), memory_repos["ledger_repo"])
    assert state["actual_spent_by_category"]["daily_survival"] == Decimal("1200.00")
    assert state["actual_spent_by_category"]["activities"] == Decimal("600.00")
    assert state["total_actual_spent"] == Decimal("1800.00")

    # Baseline planned amounts not duplicated
    all_entries = memory_repos["ledger_repo"].get_ledger_entries(trip_id)
    total_planned = sum(e.planned_amount for e in all_entries)
    assert total_planned == Decimal("3000.00")


# ============================================================================
# Test 7 — Final day returns None (no-op)
# ============================================================================
@pytest.mark.asyncio
async def test_final_day_returns_none(active_trip_setup):
    """When current_day = final day, reoptimizer returns None without completing trip."""
    setup = active_trip_setup
    trip = setup["trip"]
    repos = setup["repos"]

    # Advance trip to final day (day 3 of 3)
    repos["trip_repo"].update_current_day(trip.id, 3)
    trip = repos["trip_repo"].get_trip(trip.id)
    assert trip.current_day == 3
    assert trip.duration_days == 3

    result = await reoptimize_remaining_trip(
        trip=trip,
        trip_repo=repos["trip_repo"],
        ledger_repo=repos["ledger_repo"],
        itinerary_repo=repos["itinerary_repo"],
    )

    assert result is None
    # Trip remains ACTIVE (Task 4 does not complete trip)
    updated_trip = repos["trip_repo"].get_trip(trip.id)
    assert updated_trip.status == "ACTIVE"


# ============================================================================
# Test 8 — Existing optimizer regression with locked_days=None
# ============================================================================
def test_optimizer_regression_with_locked_days_none():
    """Verify OptimizationEngine.optimize behaves normally when locked_days=None."""
    optimizer = OptimizationEngine()
    estimator = EstimationLayer()

    food_est = estimator.estimate_food(people=2, days=4, tier="standard")
    transit_est = estimator.estimate_local_transit_daily(days=4, people=2)

    # Infeasible configuration that requires downgrade
    res = optimizer.optimize(
        trip_id=None,
        total_budget=Decimal("5000.00"),
        people=2,
        days=4,
        initial_transport=FlightOption(price=Decimal("4000.00")),
        initial_hotel=HotelOption(name="Palace", price_per_night=Decimal("2000.00"), total_price=Decimal("8000.00")),
        initial_food=food_est,
        initial_transit=transit_est,
        locked_days=None,
    )

    # 4 attempts executed
    assert res.total_attempts > 0
    assert len(res.downgrades_applied) > 0


# ============================================================================
# Test 9 — No external API bypass
# ============================================================================
@pytest.mark.asyncio
async def test_no_external_api_bypass(active_trip_setup):
    """Reoptimization does not make direct external calls or bypass offline caches."""
    setup = active_trip_setup
    trip = setup["trip"]
    repos = setup["repos"]

    with patch("budlance.serpapi.gateway.SerpApiGateway.execute_search") as mock_serp:
        result = await reoptimize_remaining_trip(
            trip=trip,
            trip_repo=repos["trip_repo"],
            ledger_repo=repos["ledger_repo"],
            itinerary_repo=repos["itinerary_repo"],
        )
        assert mock_serp.call_count == 0


# ============================================================================
# Test 10 — locked_days={2} cannot reduce duration below 2
# ============================================================================
def test_locked_days_2_cannot_reduce_below_2():
    """locked_days={2} ensures optimizer Attempt 3 never reduces trip duration below 2."""
    optimizer = OptimizationEngine()
    estimator = EstimationLayer()

    food_est = estimator.estimate_food(people=2, days=3, tier="standard")
    transit_est = estimator.estimate_local_transit_daily(days=3, people=2)

    # Infeasible configuration: budget 1000 with 3 days
    res = optimizer.optimize(
        trip_id=None,
        total_budget=Decimal("1000.00"),
        people=2,
        days=3,
        initial_transport=None,
        initial_hotel=None,
        initial_food=food_est,
        initial_transit=transit_est,
        locked_days={2},
    )

    # Duration must be at least 2, never reduced below 2
    assert res.days >= 2


# ============================================================================
# Test 11 — locked_days={1, 3} cannot reduce duration below 3 (highest locked day)
# ============================================================================
def test_locked_days_1_3_cannot_reduce_below_3():
    """locked_days={1, 3} ensures optimizer never reduces trip duration below highest locked day (3)."""
    optimizer = OptimizationEngine()
    estimator = EstimationLayer()

    food_est = estimator.estimate_food(people=2, days=4, tier="standard")
    transit_est = estimator.estimate_local_transit_daily(days=4, people=2)

    # Infeasible configuration: budget 1000 with 4 days
    res = optimizer.optimize(
        trip_id=None,
        total_budget=Decimal("1000.00"),
        people=2,
        days=4,
        initial_transport=None,
        initial_hotel=None,
        initial_food=food_est,
        initial_transit=transit_est,
        locked_days={1, 3},
    )

    # Duration must be at least 3, never reduced below 3
    assert res.days >= 3


# ============================================================================
# Test 12 — Multi-Day Immutability: Day 1 & Day 2 COMPLETED, Days 3 & 4 UPCOMING
# ============================================================================
@pytest.mark.asyncio
async def test_multi_day_completed_immutability(memory_repos):
    """Day 1 COMPLETED, Day 2 COMPLETED, Day 3 UPCOMING, Day 4 UPCOMING.

    After optimization: Day 1 unchanged, Day 2 unchanged, only eligible future days can change.
    """
    trip_id = uuid4()
    user_id = uuid4()
    chat_id = 771122

    trip = memory_repos["trip_repo"].create_trip(
        user_id=user_id,
        telegram_chat_id=chat_id,
        budget_total=Decimal("25000.00"),
        destination="Kerala",
        origin="Bangalore",
        duration_days=4,
        people_count=2,
        is_active=True,
    )
    memory_repos["trip_repo"].update_trip_status(trip.id, "ACTIVE")
    memory_repos["trip_repo"].update_current_day(trip.id, 3)
    trip = memory_repos["trip_repo"].get_trip(trip.id)

    # Allocation
    alloc = BudgetAllocation(
        id=uuid4(),
        trip_id=trip.id,
        transport_allocated=Decimal("6000.00"),
        stay_allocated=Decimal("8000.00"),
        food_allocated=Decimal("5000.00"),
        activities_discretionary=Decimal("3000.00"),
        rescue_fund_allocated=Decimal("3000.00"),
        total_budget=Decimal("25000.00"),
    )
    memory_repos["ledger_repo"].save_budget_allocation(alloc)

    # Fixed bookings baseline
    memory_repos["ledger_repo"].add_ledger_entry(
        LedgerEntry(
            id=uuid4(),
            trip_id=trip.id,
            category="fixed_booking",
            description="Inter-city train",
            planned_amount=Decimal("6000.00"),
            actual_amount=None,
        )
    )
    memory_repos["ledger_repo"].add_ledger_entry(
        LedgerEntry(
            id=uuid4(),
            trip_id=trip.id,
            category="fixed_booking",
            description="Resort stay",
            planned_amount=Decimal("8000.00"),
            actual_amount=None,
        )
    )

    # 4-Day Itinerary: Day 1 & Day 2 COMPLETED, Day 3 & Day 4 UPCOMING
    day1 = ItineraryDay(
        day_number=1,
        theme_or_summary="Day 1 - Arrival Kochi",
        status="COMPLETED",
        items=[ItineraryItem(time_slot="Morning", activity="Fort Kochi Walk", category="attraction", planned_cost=Decimal("0.00"))],
        daily_estimated_cost=Decimal("1200.00"),
    )
    day2 = ItineraryDay(
        day_number=2,
        theme_or_summary="Day 2 - Munnar Hills",
        status="COMPLETED",
        items=[ItineraryItem(time_slot="Afternoon", activity="Tea Plantation", category="attraction", planned_cost=Decimal("500.00"))],
        daily_estimated_cost=Decimal("1800.00"),
    )
    day3 = ItineraryDay(
        day_number=3,
        theme_or_summary="Day 3 - Backwaters",
        status="UPCOMING",
        items=[ItineraryItem(time_slot="Morning", activity="Houseboat Cruise", category="activities", planned_cost=Decimal("2500.00"))],
        daily_estimated_cost=Decimal("3500.00"),
    )
    day4 = ItineraryDay(
        day_number=4,
        theme_or_summary="Day 4 - Beach & Departure",
        status="UPCOMING",
        items=[ItineraryItem(time_slot="Evening", activity="Return Departure", category="transport", planned_cost=Decimal("0.00"))],
        daily_estimated_cost=Decimal("1000.00"),
    )

    itin = Itinerary(
        id=uuid4(),
        trip_id=trip.id,
        days=[day1.model_dump(mode="json"), day2.model_dump(mode="json"), day3.model_dump(mode="json"), day4.model_dump(mode="json")],
        is_feasible=True,
    )
    memory_repos["itinerary_repo"].save_itinerary(itin)

    day1_snapshot = dict(day1.model_dump(mode="json"))
    day2_snapshot = dict(day2.model_dump(mode="json"))

    # Run re-optimization (tight budget to force adjustments on remaining days)
    trip_tight = trip.model_copy(update={"budget_total": Decimal("18000.00")})
    result = await reoptimize_remaining_trip(
        trip=trip_tight,
        trip_repo=memory_repos["trip_repo"],
        ledger_repo=memory_repos["ledger_repo"],
        itinerary_repo=memory_repos["itinerary_repo"],
        force=True,
    )

    itin_after = memory_repos["itinerary_repo"].get_itinerary(trip.id)

    # 1. Day 1 is 100% identical and unchanged
    assert itin_after.days[0]["day_number"] == 1
    assert itin_after.days[0]["status"] == "COMPLETED"
    assert itin_after.days[0]["theme_or_summary"] == day1_snapshot["theme_or_summary"]
    assert itin_after.days[0]["items"] == day1_snapshot["items"]
    assert itin_after.days[0]["daily_estimated_cost"] == day1_snapshot["daily_estimated_cost"]

    # 2. Day 2 is 100% identical and unchanged
    assert itin_after.days[1]["day_number"] == 2
    assert itin_after.days[1]["status"] == "COMPLETED"
    assert itin_after.days[1]["theme_or_summary"] == day2_snapshot["theme_or_summary"]
    assert itin_after.days[1]["items"] == day2_snapshot["items"]
    assert itin_after.days[1]["daily_estimated_cost"] == day2_snapshot["daily_estimated_cost"]


# ============================================================================
# Test 13 — Meaningful Re-optimization Gate: Small Expense Skip
# ============================================================================
@pytest.mark.asyncio
async def test_meaningful_reoptimization_gate(active_trip_setup):
    """₹100 water purchase does NOT cause full itinerary re-optimization.

    Re-optimization only triggers on day completion or material budget overrun.
    """
    setup = active_trip_setup
    trip = setup["trip"]
    repos = setup["repos"]

    # 1. Small non-material expense: ₹100 water without day completion
    res_small = await reoptimize_remaining_trip(
        trip=trip,
        trip_repo=repos["trip_repo"],
        ledger_repo=repos["ledger_repo"],
        itinerary_repo=repos["itinerary_repo"],
        force=False,
        day_completed=False,
        recent_expense_amount=Decimal("100.00"),
    )
    # Must be skipped (returns None)
    assert res_small is None

    # 2. With day_completed=True, re-optimization runs
    res_completed = await reoptimize_remaining_trip(
        trip=trip,
        trip_repo=repos["trip_repo"],
        ledger_repo=repos["ledger_repo"],
        itinerary_repo=repos["itinerary_repo"],
        force=False,
        day_completed=True,
    )
    assert res_completed is not None

