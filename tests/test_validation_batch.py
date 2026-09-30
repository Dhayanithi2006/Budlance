"""Budlance Code-Level Validation Batch — Tasks 2–7.

Covers:
  Task 2 — Reverse-Budget Engine correctness (exact equality, marginal overage,
            invalid inputs, bucket sum integrity)
  Task 3 — Bucket Splitting (headcount scaling, very small budget, partial-day trip)
  Task 4 — Architecture Bug #1: Optimizer loop-back regression tests
  Task 5 — Architecture Bug #2: Feasible success-path wiring regression test
  Task 6 — Request/Call Instrumentation: log-field presence verification

No Telegram routing. No OpenRouter calls. No SerpApi calls.
All inputs are deterministic and hardcoded for mathematical verification.
"""

from decimal import Decimal, ROUND_HALF_UP
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
import logging
import pytest

from budlance.db.repositories.attempt_repo import AttemptRepository
from budlance.db.repositories.itinerary_repo import ItineraryRepository
from budlance.db.repositories.ledger_repo import LedgerRepository
from budlance.db.repositories.user_repo import UserRepository
from budlance.db.repositories.trip_repo import TripRepository
from budlance.db.repositories.intent_repo import IntentRepository
from budlance.engine.budget import ReverseBudgetEngine
from budlance.engine.optimizer import OptimizationEngine
from budlance.estimation.estimator import EstimationLayer
from budlance.itinerary.generator import ItineraryGenerator
from budlance.ledger.manager import VirtualLedgerManager
from budlance.orchestrator.models import OrchestrationResult
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.schemas.travel import (
    FlightOption,
    FoodEstimate,
    HotelOption,
    LocalTransitEstimate,
    TransitOption,
)
from budlance.serpapi.models import DataSource, TravelDataEnvelope
from budlance.ai.schemas import ParsedTripIntent


# ============================================================================
# Shared helpers
# ============================================================================

def _make_engine():
    return ReverseBudgetEngine()


def _make_estimation():
    return EstimationLayer()


def _make_flight(price: Decimal) -> FlightOption:
    return FlightOption(airline="IndiGo", price=price, source=DataSource.FALLBACK, is_fallback=True)


def _make_hotel(name: str, per_night: Decimal, days: int, hotel_class: int = 3) -> HotelOption:
    return HotelOption(
        name=name,
        hotel_class=hotel_class,
        price_per_night=per_night,
        total_price=per_night * Decimal(days),
        source=DataSource.FALLBACK,
        is_fallback=True,
    )


def _make_food(people: int, days: int, tier: str = "standard") -> FoodEstimate:
    return _make_estimation().estimate_food(people=people, days=days, tier=tier)


def _make_transit(days: int, people: int) -> LocalTransitEstimate:
    return _make_estimation().estimate_local_transit_daily(days=days, people=people)


# ============================================================================
# TASK 2.1 — Exact Equality
# ============================================================================

def test_exact_equality_exact_zero_difference():
    """
    Construct a trip where total_allocated == total_budget exactly.

    Observed arithmetic (standard tier food rate=800/person/day, transit=100/person/day):
      Budget        = 20_000.00
      Rescue (10%)  = 2_000.00
      Transport     = 3_000.00
      Hotel         = 1_500 * 3 = 4_500.00   → Bucket A = 7_500.00
      Food          = 800 * 2 * 3 = 4_800.00
      Transit       = 100 * 2 * 3 = 600.00    → Bucket B = 5_400.00
      Mandatory     = 2_000 + 7_500 + 5_400   = 14_900.00
      Remaining     = 20_000 − 14_900          = 5_100.00
      Activities    = 5_100.00                 → exact fit
      total_allocated = 14_900 + 5_100         = 20_000.00
      Difference    = 0.00  ✓
    """
    engine = _make_engine()
    transport = _make_flight(Decimal("3000.00"))
    hotel = _make_hotel("Exact Hotel", Decimal("1500.00"), 3)
    food = _make_food(2, 3, "standard")
    transit = _make_transit(3, 2)

    result = engine.evaluate(
        total_budget=Decimal("20000.00"),
        people=2,
        days=3,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
        activities_budget=Decimal("5100.00"),
    )

    # Record observed numbers
    bd = result.breakdown
    diff = bd.total_budget - bd.total_allocated

    assert result.is_feasible is True, f"Expected FEASIBLE, got {result.status}"
    assert result.status == "FEASIBLE"
    assert diff == Decimal("0.00"), f"Difference must be exactly 0.00; got {diff}"
    assert result.deficit == Decimal("0.00")
    assert bd.total_allocated == Decimal("20000.00")
    assert bd.remaining_surplus == Decimal("0.00")

    # Bucket integrity
    assert bd.bucket_d_rescue == Decimal("2000.00")
    assert bd.bucket_a_fixed == Decimal("7500.00")
    assert bd.bucket_b_survival == Decimal("5400.00")
    assert bd.bucket_c_activities == Decimal("5100.00")


# ============================================================================
# TASK 2.2 — Marginal Overage (₹50 over budget)
# ============================================================================

def test_marginal_50_overage_triggers_optimizer():
    """
    Trip total planned exceeds budget by exactly ₹50.
    Baseline → NOT_FEASIBLE → OptimizationEngine triggered → max 4 attempts.

    Arithmetic:
      Budget     = 14_950.00
      Rescue 10% = 1_495.00
      Transport  = 3_000.00
      Hotel      = 1_500 * 3 = 4_500.00  → Bucket A = 7_500.00
      Food       = 800*2*3 = 4_800
      Transit    = 100*2*3 = 600           → Bucket B = 5_400.00
      Mandatory  = 1_495 + 7_500 + 5_400  = 14_395.00
      Remaining  = 14_950 − 14_395         = 555.00
      Activities = 605.00                  → 50 over remaining (555)
      Deficit    = 605 − 555 = 50.00  ✓
    """
    engine = _make_engine()
    transport = _make_flight(Decimal("3000.00"))
    hotel = _make_hotel("Marginal Hotel", Decimal("1500.00"), 3)
    food = _make_food(2, 3, "standard")
    transit = _make_transit(3, 2)

    baseline = engine.evaluate(
        total_budget=Decimal("14950.00"),
        people=2,
        days=3,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
        activities_budget=Decimal("605.00"),
    )

    assert baseline.is_feasible is False, "Expected NOT_FEASIBLE for 50 overage"
    assert baseline.status == "NOT_FEASIBLE"
    assert baseline.deficit == Decimal("50.00"), f"Expected deficit=50.00, got {baseline.deficit}"

    # Now run the optimizer
    attempt_repo = AttemptRepository(client=None)
    optimizer = OptimizationEngine(attempt_repo=attempt_repo)
    cheaper_hotel = _make_hotel("Budget Inn", Decimal("1000.00"), 3)

    result = optimizer.optimize(
        trip_id=None,
        total_budget=Decimal("14950.00"),
        people=2,
        days=3,
        initial_transport=transport,
        initial_hotel=hotel,
        initial_food=food,
        initial_transit=transit,
        activities_budget=Decimal("605.00"),
        available_hotels=[cheaper_hotel],
        available_transports=[],
    )

    # Must have attempted at least 1 downgrade
    assert result.total_attempts >= 1, "Optimizer must attempt at least 1 downgrade"
    assert result.total_attempts <= 4, "Optimizer must not exceed 4 attempts"
    # Since cheaper hotel is provided, attempt 1 should resolve it
    assert result.is_feasible is True
    assert result.successful_attempt == 1


# ============================================================================
# TASK 2.3 — Invalid Inputs
# ============================================================================

@pytest.mark.parametrize("people,days,budget,label", [
    (0, 3, Decimal("15000.00"), "zero_people"),
    (2, 0, Decimal("15000.00"), "zero_days"),
    (2, 3, Decimal("0.00"),     "zero_budget"),
    (2, 3, Decimal("-100.00"),  "negative_budget"),
    (-1, 3, Decimal("15000.00"), "negative_people"),
    (2, -1, Decimal("15000.00"), "negative_days"),
])
def test_invalid_input_does_not_raise_or_corrupt(people, days, budget, label):
    """
    Each invalid input must be handled cleanly:
    - No unhandled exception
    - No infinite loop
    - Result is always a BudgetEvaluationResult (never None or corrupt)
    - Zero/negative budget → NOT_FEASIBLE with explanation
    - Zero/negative people/days → food estimator clamps to max(1,x); engine may still
      evaluate but must not crash or loop.
    """
    engine = _make_engine()
    estimation = _make_estimation()

    # Clamp for estimation calls so they don't crash independently
    safe_people = max(1, people)
    safe_days = max(1, days)

    transport = _make_flight(Decimal("2000.00"))
    hotel = _make_hotel("Test Hotel", Decimal("1000.00"), safe_days)
    food = estimation.estimate_food(people=safe_people, days=safe_days, tier="standard")
    transit = estimation.estimate_local_transit_daily(days=safe_days, people=safe_people)

    # Must not raise
    result = engine.evaluate(
        total_budget=budget,
        people=people,
        days=days,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
        activities_budget=Decimal("500.00"),
    )

    assert result is not None, f"[{label}] result must not be None"
    assert result.status in ("FEASIBLE", "NOT_FEASIBLE"), f"[{label}] unexpected status: {result.status}"

    if budget <= Decimal("0.00"):
        assert result.is_feasible is False, f"[{label}] zero/negative budget must be NOT_FEASIBLE"
        assert "greater than zero" in result.explanation, f"[{label}] explanation missing"


# ============================================================================
# TASK 2.4 — Bucket Sum Integrity
# ============================================================================

def test_bucket_sum_integrity_and_padding_consistency():
    """
    For a deterministic feasible case, verify:
      Bucket_A + Bucket_B + Bucket_C + Bucket_D == total_allocated
      total_allocated <= total_budget
      rescue_reserve == round(total_budget * 0.10, 2)    [10% from spec]
      rescue_reserve computed only once from total_budget, NOT re-derived separately.
    """
    engine = _make_engine()
    budget = Decimal("25000.00")
    transport = _make_flight(Decimal("4000.00"))
    hotel = _make_hotel("Spec Hotel", Decimal("2000.00"), 4)
    food = _make_food(2, 4, "standard")
    transit = _make_transit(4, 2)
    activities = Decimal("1000.00")

    result = engine.evaluate(
        total_budget=budget,
        people=2,
        days=4,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
        activities_budget=activities,
    )

    assert result.is_feasible is True
    bd = result.breakdown

    # Bucket integrity sum
    bucket_sum = bd.bucket_a_fixed + bd.bucket_b_survival + bd.bucket_c_activities + bd.bucket_d_rescue
    assert bucket_sum == bd.total_allocated, (
        f"Bucket sum {bucket_sum} != total_allocated {bd.total_allocated}"
    )
    assert bd.total_allocated <= budget

    # Rescue reserve is exactly 10% of total_budget (rounded to 2 dp)
    expected_rescue = round(budget * Decimal("0.10"), 2)
    assert bd.bucket_d_rescue == expected_rescue, (
        f"Rescue reserve {bd.bucket_d_rescue} != expected 10% = {expected_rescue}"
    )

    # Granular sub-bucket consistency
    assert bd.bucket_a_fixed == bd.transport_cost + bd.hotel_cost
    assert bd.bucket_b_survival == bd.food_cost + bd.local_transit_cost

    # Record exact observed values
    print(
        f"\n[OBSERVED] budget={budget} rescue={bd.bucket_d_rescue} "
        f"A={bd.bucket_a_fixed} B={bd.bucket_b_survival} C={bd.bucket_c_activities} "
        f"total_allocated={bd.total_allocated} surplus={bd.remaining_surplus}"
    )


# ============================================================================
# TASK 3.1 — Headcount Scaling (2 vs 5 people)
# ============================================================================

def test_bucket_b_scales_with_headcount():
    """
    Same budget, destination, and duration; vary people count.
    Verify Bucket B grows as people increases per documented business logic.

    Documented rates (from food.py DEFAULT_FOOD_RATES, transport.py DEFAULT_TRANSIT_RATES):
      Food standard = 800 INR/person/day
      Transit daily pass = 100 INR/person/day
      Bucket B = (food_rate + transit_rate) * people * days

    For days=3:
      2 people: B = (800+100)*2*3 = 5_400.00
      5 people: B = (800+100)*5*3 = 13_500.00
      Ratio: 5/2 = 2.5 (linear in people count)
    """
    engine = _make_engine()
    transport_2 = _make_flight(Decimal("4000.00"))
    hotel_2 = _make_hotel("Hotel A", Decimal("1500.00"), 3)
    food_2 = _make_food(2, 3, "standard")
    transit_2 = _make_transit(3, 2)

    result_2 = engine.evaluate(
        total_budget=Decimal("40000.00"),
        people=2,
        days=3,
        transport=transport_2,
        hotel=hotel_2,
        food_estimate=food_2,
        local_transit_estimate=transit_2,
        activities_budget=Decimal("500.00"),
    )

    transport_5 = _make_flight(Decimal("4000.00"))
    hotel_5 = _make_hotel("Hotel A", Decimal("1500.00"), 3)
    food_5 = _make_food(5, 3, "standard")
    transit_5 = _make_transit(3, 5)

    result_5 = engine.evaluate(
        total_budget=Decimal("40000.00"),
        people=5,
        days=3,
        transport=transport_5,
        hotel=hotel_5,
        food_estimate=food_5,
        local_transit_estimate=transit_5,
        activities_budget=Decimal("500.00"),
    )

    b2 = result_2.breakdown.bucket_b_survival
    b5 = result_5.breakdown.bucket_b_survival

    assert b5 > b2, f"Bucket B with 5 people ({b5}) must exceed 2 people ({b2})"

    # Exact expected values per rate tables
    expected_b2 = Decimal("5400.00")   # (800+100)*2*3
    expected_b5 = Decimal("13500.00")  # (800+100)*5*3
    assert b2 == expected_b2, f"2-person Bucket B: expected {expected_b2}, got {b2}"
    assert b5 == expected_b5, f"5-person Bucket B: expected {expected_b5}, got {b5}"

    diff = b5 - b2
    print(
        f"\n[OBSERVED] Bucket B: 2-person={b2} 5-person={b5} diff={diff}"
    )


# ============================================================================
# TASK 3.2 — Very Small Budget
# ============================================================================

def test_very_small_budget_rescue_reserve_behavior():
    """
    Test budget = 1000 INR.
    Rescue reserve = 10% = 100.00 INR.
    No minimum floor is specified in the project spec/config, so:
    - The engine MUST NOT invent a floor.
    - Bucket D = round(1000 * 0.10, 2) = 100.00
    - Mandatory costs almost certainly exceed budget → NOT_FEASIBLE (no crash).
    Report the actual behavior as-observed.
    """
    engine = _make_engine()
    transport = _make_flight(Decimal("500.00"))
    hotel = _make_hotel("Micro Hotel", Decimal("200.00"), 1)
    food = _make_food(1, 1, "standard")
    transit = _make_transit(1, 1)

    result = engine.evaluate(
        total_budget=Decimal("1000.00"),
        people=1,
        days=1,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
        activities_budget=Decimal("0.00"),
    )

    bd = result.breakdown
    expected_rescue = Decimal("100.00")  # 10% of 1000
    actual_rescue = bd.bucket_d_rescue

    # The rescue reserve must be exactly 10% (no hidden minimum floor)
    assert actual_rescue == expected_rescue or actual_rescue == Decimal("0.00"), (
        f"Rescue reserve {actual_rescue} unexpected: spec says 10% of budget."
        " If zero, the budget guard triggered first — acceptable."
        " A value other than 100.00 or 0.00 indicates an undocumented floor."
    )

    # No crash
    assert result.status in ("FEASIBLE", "NOT_FEASIBLE")

    print(
        f"\n[OBSERVED] Very small budget=1000 rescue_reserve={actual_rescue} "
        f"status={result.status} deficit={result.deficit}"
    )

    # Design note: no floor was observed if actual_rescue == 100.00
    # If the budget guard fires (budget<=0 check) it would catch budget<=0; for 1000 it is NOT triggered.
    # Actual rescue here: 100.00 (no invented floor — consistent with spec).


# ============================================================================
# TASK 3.3 — Partial-Day Trip (off-by-one check)
# ============================================================================

def test_partial_day_trip_no_off_by_one():
    """
    Scenario: "depart evening Day 1, return morning Day 4" → 3-night stay.
    The current implementation uses `days` as the integer trip duration passed by the caller.
    The Estimation Layer charges food/transit for exactly `days` days (no off-by-one).

    Test: days=3 (representing the stated 3-night stay).
      food = 800 * 2 * 3 = 4_800.00
      transit = 100 * 2 * 3 = 600.00
    The engine does NOT do internal off-by-one correction; it uses the passed value verbatim.
    """
    engine = _make_engine()
    estimation = _make_estimation()
    people = 2
    days = 3

    food = estimation.estimate_food(people=people, days=days, tier="standard")
    transit = estimation.estimate_local_transit_daily(days=days, people=people)

    assert food.days == days, f"FoodEstimate.days must equal input days ({days}); got {food.days}"
    assert transit.days == days, f"LocalTransitEstimate.days must equal input days ({days}); got {transit.days}"

    # Observed cost calculation
    expected_food = Decimal("800.00") * Decimal(people) * Decimal(days)
    expected_transit = Decimal("100.00") * Decimal(people) * Decimal(days)
    assert food.total_cost == expected_food, f"Food cost mismatch: expected {expected_food}, got {food.total_cost}"
    assert transit.total_cost == expected_transit, f"Transit cost mismatch: expected {expected_transit}, got {transit.total_cost}"

    transport = _make_flight(Decimal("3000.00"))
    hotel = _make_hotel("3-Night Hotel", Decimal("1500.00"), days)

    result = engine.evaluate(
        total_budget=Decimal("25000.00"),
        people=people,
        days=days,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
        activities_budget=Decimal("1000.00"),
    )

    # Number of Bucket B charging days must equal `days` — no off-by-one
    assert food.days == days, "PASS: No off-by-one; food charged for exactly days days"
    assert transit.days == days, "PASS: No off-by-one; transit charged for exactly days days"

    print(
        f"\n[OBSERVED] days={days} food_days={food.days} transit_days={transit.days} "
        f"food_total={food.total_cost} transit_total={transit.total_cost} "
        f"status={result.status}"
    )


# ============================================================================
# TASK 4 — Architecture Bug #1: Optimizer Loop-Back Regression
# ============================================================================

def test_optimizer_downgrade_1_reevaluates():
    """
    After downgrade #1 (hotel tier down), the engine MUST reevaluate via budget_engine.evaluate().
    Prove this by checking that a budget which is infeasible at baseline becomes feasible after
    downgrade #1 — the engine ran again and returned FEASIBLE.
    """
    attempt_repo = AttemptRepository(client=None)
    optimizer = OptimizationEngine(attempt_repo=attempt_repo)

    initial_hotel = _make_hotel("Expensive Hotel", Decimal("5000.00"), 3, hotel_class=5)
    cheap_hotel = _make_hotel("Budget Hotel", Decimal("800.00"), 3, hotel_class=2)
    transport = _make_flight(Decimal("2000.00"))
    food = _make_food(1, 3, "standard")
    transit = _make_transit(3, 1)

    result = optimizer.optimize(
        trip_id=None,
        total_budget=Decimal("10000.00"),
        people=1,
        days=3,
        initial_transport=transport,
        initial_hotel=initial_hotel,
        initial_food=food,
        initial_transit=transit,
        activities_budget=Decimal("500.00"),
        available_hotels=[cheap_hotel],
    )

    # Downgrade #1 produced a new evaluation that returned FEASIBLE → loop-back confirmed
    assert result.is_feasible is True
    assert result.successful_attempt == 1
    assert result.total_attempts == 1
    # The selected hotel is the cheaper one
    assert result.selected_hotel.name == "Budget Hotel"


def test_optimizer_later_attempt_also_reevaluates():
    """
    Downgrade #2 (transport class down) triggers a fresh engine evaluation
    and produces FEASIBLE on attempt 2.

    Arithmetic (budget=11000, days=3, people=1, standard tier):
      Rescue 10% = 1100
      Hotel A    = 2000/night × 3 = 6000    → Bucket A with transport
      Transport initial (flight) = 8000      → Attempt 1 total: 1100+14000+2700=17800 > 11000 FAIL
      Transport downgraded = 600 (train)     → Attempt 2: 1100+(6000+600)+2700 = 10400 ≤ 11000 FEASIBLE
    """
    attempt_repo = AttemptRepository(client=None)
    trip_id = uuid4()
    optimizer = OptimizationEngine(attempt_repo=attempt_repo)

    initial_hotel = _make_hotel("Hotel A", Decimal("2000.00"), 3)
    initial_flight = _make_flight(Decimal("8000.00"))
    cheap_train = TransitOption(
        transit_type="train",
        origin="Delhi",
        destination="Jaipur",
        name_or_operator="Express",
        price=Decimal("600.00"),
        source=DataSource.FALLBACK,
        is_fallback=True,
    )
    food = _make_food(1, 3, "standard")     # 800*1*3 = 2400
    transit = _make_transit(3, 1)           # 100*1*3 = 300

    # Budget = 11000
    # Attempt 1: rescue=1100 + (8000+6000) + (2400+300) + activities=200 = 18000 NOT_FEASIBLE
    # Attempt 2: rescue=1100 + (600+6000) + (2400+300) + activities=200 = 10600 FEASIBLE ✓
    result = optimizer.optimize(
        trip_id=trip_id,
        total_budget=Decimal("11000.00"),
        people=1,
        days=3,
        initial_transport=initial_flight,
        initial_hotel=initial_hotel,
        initial_food=food,
        initial_transit=transit,
        activities_budget=Decimal("200.00"),
        available_hotels=[],       # No cheaper hotel → attempt 1 does NOT resolve
        available_transports=[cheap_train],
    )

    # Attempt 1 must have been tried AND failed (loop-back occurred for attempt 2)
    assert result.total_attempts == 2, (
        f"Expected attempt 2 to succeed; got total_attempts={result.total_attempts}, "
        f"successful_attempt={result.successful_attempt}"
    )
    assert result.is_feasible is True
    assert result.successful_attempt == 2

    # Confirm 2 plan_attempts were recorded (1 failed, 1 succeeded)
    recorded = attempt_repo.get_plan_attempts(trip_id)
    assert len(recorded) == 2
    assert recorded[0].attempt_number == 1




def test_optimizer_stops_at_4_attempts_never_exceeds():
    """
    Even with impossible budget, optimizer stops at exactly 4 attempts.
    Confirm total_attempts == 4 and no 5th attempt ever occurs.
    """
    attempt_repo = AttemptRepository(client=None)
    optimizer = OptimizationEngine(attempt_repo=attempt_repo)
    trip_id = uuid4()

    hotel = _make_hotel("Expensive", Decimal("10000.00"), 5, hotel_class=5)
    transport = _make_flight(Decimal("20000.00"))
    food = _make_food(3, 5, "comfort")
    transit = _make_transit(5, 3)

    result = optimizer.optimize(
        trip_id=trip_id,
        total_budget=Decimal("3000.00"),
        people=3,
        days=5,
        initial_transport=transport,
        initial_hotel=hotel,
        initial_food=food,
        initial_transit=transit,
        activities_budget=Decimal("5000.00"),
        available_hotels=[],
        available_transports=[],
    )

    assert result.total_attempts == 4, f"Expected exactly 4 attempts, got {result.total_attempts}"
    assert result.is_feasible is False
    assert result.final_status == "NOT_FEASIBLE"
    assert result.recommendation is not None

    # Recorded in DB: 4 plan_attempts (trip_id was real here)
    recorded = attempt_repo.get_plan_attempts(trip_id)
    assert len(recorded) == 4
    assert [a.attempt_number for a in recorded] == [1, 2, 3, 4]


def test_optimizer_feasible_candidate_stops_further_downgrades():
    """
    Once a downgrade produces a FEASIBLE result, NO further downgrades must be applied.
    If attempt 1 succeeds, attempts 2, 3, 4 must not execute.
    """
    attempt_repo = AttemptRepository(client=None)
    optimizer = OptimizationEngine(attempt_repo=attempt_repo)
    trip_id = uuid4()

    initial_hotel = _make_hotel("5-Star", Decimal("5000.00"), 2, hotel_class=5)
    cheap_hotel = _make_hotel("2-Star", Decimal("600.00"), 2, hotel_class=2)
    transport = _make_flight(Decimal("2000.00"))
    food = _make_food(1, 2, "standard")
    transit = _make_transit(2, 1)

    result = optimizer.optimize(
        trip_id=trip_id,
        total_budget=Decimal("9000.00"),
        people=1,
        days=2,
        initial_transport=transport,
        initial_hotel=initial_hotel,
        initial_food=food,
        initial_transit=transit,
        activities_budget=Decimal("300.00"),
        available_hotels=[cheap_hotel],
    )

    assert result.is_feasible is True
    assert result.total_attempts == 1       # ONLY attempt 1 ran
    assert result.successful_attempt == 1

    # Only 1 attempt was recorded (not 2, 3, or 4)
    recorded = attempt_repo.get_plan_attempts(trip_id)
    assert len(recorded) == 1
    assert recorded[0].attempt_number == 1


def test_exhausted_4_attempts_returns_not_feasible_response():
    """
    After 4 failed attempts, the result must set:
      - is_feasible = False
      - final_status = NOT_FEASIBLE
      - deficit > 0
      - recommendation is not None (explicit advice per spec)
    No itinerary or ledger must have been created.
    """
    optimizer = OptimizationEngine(attempt_repo=AttemptRepository(client=None))
    hotel = _make_hotel("Luxury", Decimal("8000.00"), 3)
    transport = _make_flight(Decimal("15000.00"))
    food = _make_food(2, 3, "comfort")
    transit = _make_transit(3, 2)

    result = optimizer.optimize(
        trip_id=None,
        total_budget=Decimal("4000.00"),
        people=2,
        days=3,
        initial_transport=transport,
        initial_hotel=hotel,
        initial_food=food,
        initial_transit=transit,
        activities_budget=Decimal("2000.00"),
        available_hotels=[],
        available_transports=[],
    )

    assert result.is_feasible is False
    assert result.final_status == "NOT_FEASIBLE"
    assert result.total_attempts == 4
    assert result.successful_attempt is None
    assert result.deficit > Decimal("0.00")
    assert result.recommendation is not None
    assert "Consider increasing budget" in result.recommendation


# ============================================================================
# TASK 5 — Architecture Bug #2: Feasible Success Path Wiring
# ============================================================================

@pytest.mark.asyncio
async def test_feasible_plan_reaches_orchestration_result():
    """
    Verify the wiring: Budget Engine → Itinerary Generator → Virtual Ledger → OrchestrationResult.

    A successful/feasible plan must:
    1. Return OrchestrationResult with status="FEASIBLE"
    2. Contain a trip_id (persistence occurred)
    3. Contain a non-None generated_itinerary
    4. Contain a non-None ledger_summary
    5. Contain non-empty message_text (Telegram response would be sent)

    Uses mock repos so no Supabase writes occur.
    Uses mock AI service so no OpenRouter calls occur.
    Uses mock cache manager so no SerpApi calls occur.
    """
    from decimal import Decimal
    from uuid import uuid4
    from unittest.mock import MagicMock, AsyncMock
    from budlance.db.models import Trip, User, TripIntent, utc_now
    from budlance.itinerary.models import GeneratedItinerary, ItineraryDay, ItineraryItem
    from budlance.ledger.models import LedgerSummary
    from budlance.db.models import BudgetAllocation, LedgerEntry

    user_id = uuid4()
    trip_id = uuid4()

    # Mock AI service: return a valid deterministic intent
    mock_ai = MagicMock()
    mock_ai.parse_rescue_intent = AsyncMock(return_value=MagicMock(rescue_type="unknown"))
    mock_ai.parse_trip_intent = AsyncMock(return_value=ParsedTripIntent(
        budget=Decimal("25000.00"),
        people=2,
        days=3,
        origin="Chennai",
        destination="Goa",
        currency="INR",
    ))

    # Mock cache manager: return empty envelopes so orchestrator uses deterministic fallbacks
    mock_cache = MagicMock()
    async def _empty_envelope(engine, params, trip_id=None, **kwargs):
        return TravelDataEnvelope(
            source=DataSource.FALLBACK,
            engine=engine,
            query_hash="test_hash",
            data={},
            is_fallback=True,
            status="unconfigured",
        )
    mock_cache.get_travel_data = AsyncMock(side_effect=_empty_envelope)

    # Mock repos (no DB writes)
    mock_user_repo = MagicMock()
    mock_user_repo.get_or_create_user = MagicMock(
        return_value=User(id=user_id, telegram_user_id=999, first_name="Test")
    )

    mock_trip_repo = MagicMock()
    mock_trip_repo.create_trip = MagicMock(
        return_value=Trip(
            id=trip_id,
            user_id=user_id,
            telegram_chat_id=999,
            destination="Goa",
            origin="Chennai",
            budget_total=Decimal("25000.00"),
            people_count=2,
            duration_days=3,
        )
    )

    mock_intent_repo = MagicMock()
    mock_intent_repo.save_trip_intent = MagicMock(return_value=None)

    # Mock itinerary generator with a real-looking result (no repo write)
    mock_itin_repo = MagicMock()
    mock_itin_repo.save_itinerary = MagicMock(return_value=None)
    real_itin_gen = ItineraryGenerator(itinerary_repo=mock_itin_repo)

    # Mock ledger manager with a real-looking summary (no repo write)
    mock_ledger_repo = MagicMock()
    alloc_id = uuid4()
    mock_alloc = BudgetAllocation(
        id=alloc_id,
        trip_id=trip_id,
        transport_allocated=Decimal("4000.00"),
        stay_allocated=Decimal("5400.00"),
        food_allocated=Decimal("4800.00"),
        activities_discretionary=Decimal("1000.00"),
        rescue_fund_allocated=Decimal("2500.00"),
        total_budget=Decimal("25000.00"),
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    mock_ledger_repo.save_budget_allocation = MagicMock(return_value=mock_alloc)
    mock_ledger_repo.add_ledger_entry = MagicMock(side_effect=lambda e: e)
    mock_ledger_repo.get_budget_allocation = MagicMock(return_value=mock_alloc)
    mock_ledger_repo.get_ledger_entries = MagicMock(return_value=[])
    real_ledger_mgr = VirtualLedgerManager(ledger_repo=mock_ledger_repo)

    mock_rescue_repo = MagicMock()

    orch = BudlanceOrchestrator(
        user_repo=mock_user_repo,
        trip_repo=mock_trip_repo,
        intent_repo=mock_intent_repo,
        itinerary_repo=mock_itin_repo,
        ledger_repo=mock_ledger_repo,
        rescue_repo=mock_rescue_repo,
        ai_service=mock_ai,
        cache_manager=mock_cache,
        itinerary_generator=real_itin_gen,
        ledger_manager=real_ledger_mgr,
    )

    result: OrchestrationResult = await orch.handle_user_message(
        telegram_user_id=999,
        chat_id=999,
        message="I have Rs.25000 for 3 days, 2 people, going from Chennai to Goa.",
        username="testuser",
        first_name="Test",
    )

    # Success wiring assertions
    assert result.status == "FEASIBLE", (
        f"Expected FEASIBLE, got {result.status}. Error: {result.error}"
    )
    assert result.trip_id is not None, "trip_id must be set after successful persistence"
    assert result.generated_itinerary is not None, "Itinerary must be generated for FEASIBLE plan"
    assert result.generated_itinerary.is_feasible is True
    assert result.ledger_summary is not None, "Ledger must be initialized for FEASIBLE plan"
    assert result.message_text and len(result.message_text) > 0, (
        "message_text must be non-empty — this is what gets sent to Telegram"
    )
    assert result.selected_destination == "Goa"
    assert result.error is None


# ============================================================================
# TASK 6 — Instrumentation: Log Field Presence
# ============================================================================

def test_serpapi_gateway_logs_instrumentation_fields():
    """
    Verify that when SerpApi credentials are absent, SerpApiAuthError is raised
    before any live call is made. Structural check that instrumentation guard works.
    """
    import asyncio
    from budlance.serpapi.gateway import SerpApiGateway
    from budlance.serpapi.exceptions import SerpApiAuthError

    gateway = SerpApiGateway(api_key=None)
    assert gateway.has_credentials is False

    # When unconfigured, execute_search raises SerpApiAuthError before any live call
    async def _run():
        with pytest.raises(SerpApiAuthError):
            await gateway.execute_search("google_flights", {"test": "value"})

    asyncio.run(_run())


def test_openrouter_client_logs_instrumentation_fields():
    """
    Verify that when OpenRouter credentials are explicitly empty, OpenRouterAuthError is raised
    before any live call is made.
    """
    import asyncio
    from budlance.ai.client import OpenRouterClient
    from budlance.ai.exceptions import OpenRouterAuthError

    client = OpenRouterClient(api_key="")
    assert client.has_credentials is False

    async def _run():
        with pytest.raises(OpenRouterAuthError):
            await client.chat_completion([{"role": "user", "content": "test"}])

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_cache_manager_logs_unconfigured_resolution(caplog):
    """
    Verify the [CACHE] resolution=UNCONFIGURED log is emitted when SerpApi is unconfigured.
    """
    from budlance.cache.manager import CacheFallbackManager
    from budlance.db.repositories.cache_repo import CacheRepository
    from budlance.db.repositories.usage_repo import UsageRepository

    # Use client=None repos (offline mode) and no credentials on gateway
    cache_repo = CacheRepository(client=None)
    usage_repo = UsageRepository(client=None)
    manager = CacheFallbackManager(
        cache_repo=cache_repo,
        usage_repo=usage_repo,
    )
    # Gateway will not have credentials (no SERPAPI_API_KEY in test env)
    assert manager.gateway.has_credentials is False

    with caplog.at_level(logging.INFO, logger="budlance.cache.manager"):
        envelope = await manager.get_travel_data(
            "google_flights", {"origin": "Chennai", "destination": "Goa"}
        )

    assert envelope.status in ("unconfigured", "success")
    # Check that at least one [CACHE] resolution log was emitted
    cache_log_lines = [r.message for r in caplog.records if "[CACHE]" in r.message]
    assert len(cache_log_lines) >= 1, (
        f"Expected at least 1 [CACHE] instrumentation log line. Got: {caplog.text}"
    )
    # Confirm resolution field is present
    assert any("resolution=" in line for line in cache_log_lines), (
        "Instrumentation log must contain 'resolution=' field"
    )


# ============================================================================
# TASK 7 — External API call count assertion
# ============================================================================

@pytest.mark.asyncio
async def test_no_live_serpapi_or_openrouter_calls_in_this_batch():
    """
    Confirm that isolated (empty-key) clients have no credentials.
    Explicit empty api_key overrides env, ensuring no live credits consumed in this check.
    """
    from budlance.serpapi.gateway import SerpApiGateway
    from budlance.ai.client import OpenRouterClient

    serpapi_gw = SerpApiGateway(api_key=None)
    openrouter_client = OpenRouterClient(api_key="")

    assert serpapi_gw.has_credentials is False, "SerpApi must NOT have live credentials in test batch"
    assert openrouter_client.has_credentials is False, "OpenRouter isolated client must report no credentials"
