"""Unit and integration tests for Reverse-Budget Engine and 4-Step Optimizer."""

from decimal import Decimal
from uuid import uuid4
import pytest

from budlance.db.repositories.attempt_repo import AttemptRepository
from budlance.engine.budget import ReverseBudgetEngine
from budlance.engine.optimizer import OptimizationEngine
from budlance.estimation.estimator import EstimationLayer
from budlance.schemas.travel import (
    FlightOption,
    FoodEstimate,
    HotelOption,
    LocalTransitEstimate,
    TransitOption,
)
from budlance.serpapi.models import DataSource


# ============================================================================
# Test Fixtures / Helpers
# ============================================================================
def create_sample_components(
    flight_price: Decimal = Decimal("4000.00"),
    hotel_night_price: Decimal = Decimal("2000.00"),
    days: int = 4,
    people: int = 2,
    food_tier: str = "standard",
):
    estimation = EstimationLayer()
    transport = FlightOption(
        airline="IndiGo",
        price=flight_price,
        source=DataSource.LIVE,
    )
    hotel = HotelOption(
        name="Goa Beach Resort",
        hotel_class=3,
        price_per_night=hotel_night_price,
        total_price=hotel_night_price * Decimal(days),
        source=DataSource.LIVE,
    )
    food = estimation.estimate_food(people=people, days=days, tier=food_tier)
    transit = estimation.estimate_local_transit_daily(days=days, people=people)

    return transport, hotel, food, transit


# ============================================================================
# 1. Reverse-Budget Engine Tests
# ============================================================================
def test_clearly_feasible_trip():
    """Verify evaluation of a comfortable trip well within budget."""
    engine = ReverseBudgetEngine()
    transport, hotel, food, transit = create_sample_components(
        flight_price=Decimal("3000.00"),
        hotel_night_price=Decimal("1500.00"),
        days=3,
        people=2,
    )
    # Total costs:
    # Rescue (10% of 25,000) = 2,500
    # Fixed = 3,000 + (1,500 * 3 = 4,500) = 7,500
    # Survival = (800 * 2 * 3 = 4,800) + (100 * 2 * 3 = 600) = 5,400
    # Mandatory = 2,500 + 7,500 + 5,400 = 15,400
    # Activities = 2,000 => Total = 17,400 <= 25,000 => Surplus = 7,600
    result = engine.evaluate(
        total_budget=Decimal("25000.00"),
        people=2,
        days=3,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
        activities_budget=Decimal("2000.00"),
    )

    assert result.is_feasible is True
    assert result.status == "FEASIBLE"
    assert result.deficit == Decimal("0.00")
    assert result.breakdown.total_allocated <= Decimal("25000.00")
    assert result.breakdown.bucket_d_rescue == Decimal("2500.00")
    assert result.breakdown.bucket_a_fixed == Decimal("7500.00")
    assert result.breakdown.bucket_b_survival == Decimal("5400.00")
    assert result.breakdown.bucket_c_activities == Decimal("2000.00")
    assert result.breakdown.remaining_surplus == Decimal("7600.00")


def test_clearly_infeasible_trip():
    """Verify evaluation of an over-budget trip correctly flags NOT_FEASIBLE."""
    engine = ReverseBudgetEngine()
    transport, hotel, food, transit = create_sample_components(
        flight_price=Decimal("10000.00"),
        hotel_night_price=Decimal("5000.00"),
        days=4,
        people=2,
    )
    # Mandatory costs alone:
    # Rescue: 2,000
    # Fixed: 10,000 + 20,000 = 30,000
    # Survival: 6,400 + 800 = 7,200
    # Total = 39,200 > 20,000 budget!
    result = engine.evaluate(
        total_budget=Decimal("20000.00"),
        people=2,
        days=4,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
        activities_budget=Decimal("3000.00"),
    )

    assert result.is_feasible is False
    assert result.status == "NOT_FEASIBLE"
    assert result.deficit > Decimal("0.00")
    assert len(result.major_cost_contributors) > 0


def test_exact_budget_trip():
    """Verify edge case where total allocations exactly equal the user's budget."""
    engine = ReverseBudgetEngine()
    transport, hotel, food, transit = create_sample_components(
        flight_price=Decimal("3000.00"),
        hotel_night_price=Decimal("1500.00"),
        days=3,
        people=2,
    )
    # Total budget = 20,000.00
    # Mandatory = 2,000 (rescue) + 7,500 (fixed) + 5,400 (survival) = 14,900
    # Remaining = 5,100.
    # Set activities exactly = 5,100
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

    assert result.is_feasible is True
    assert result.breakdown.total_allocated == Decimal("20000.00")
    assert result.breakdown.remaining_surplus == Decimal("0.00")
    assert result.deficit == Decimal("0.00")


def test_budget_invariant():
    """Verify invariant: Total Allocations <= User Budget holds for any feasible result."""
    engine = ReverseBudgetEngine()
    transport, hotel, food, transit = create_sample_components(
        flight_price=Decimal("2000.00"),
        hotel_night_price=Decimal("1000.00"),
        days=2,
        people=1,
    )
    result = engine.evaluate(
        total_budget=Decimal("15000.00"),
        people=1,
        days=2,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
        activities_budget=Decimal("1500.00"),
    )
    assert result.is_feasible is True
    assert result.breakdown.total_allocated <= Decimal("15000.00")


def test_zero_or_negative_budget():
    """Verify handling of invalid or non-positive budget values."""
    engine = ReverseBudgetEngine()
    transport, hotel, food, transit = create_sample_components()
    result = engine.evaluate(
        total_budget=Decimal("0.00"),
        people=2,
        days=4,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
    )
    assert result.is_feasible is False
    assert "greater than zero" in result.explanation


def test_source_provenance_preservation():
    """Verify that input provenance (LIVE, ESTIMATED, FALLBACK) is preserved in breakdown."""
    engine = ReverseBudgetEngine()
    transport = TransitOption(
        transit_type="train",
        origin="Chennai",
        destination="Bangalore",
        name_or_operator="Shatabdi",
        price=Decimal("750.00"),
        source=DataSource.FALLBACK,
        is_fallback=True,
    )
    hotel = HotelOption(
        name="Hotel Grand",
        price_per_night=Decimal("1000.00"),
        total_price=Decimal("2000.00"),
        source=DataSource.LIVE,
    )
    estimation = EstimationLayer()
    food = estimation.estimate_food(people=1, days=2, tier="standard")
    transit = estimation.estimate_local_transit_daily(days=2, people=1)

    result = engine.evaluate(
        total_budget=Decimal("10000.00"),
        people=1,
        days=2,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
    )

    prov = result.breakdown.provenance
    assert prov["transport"] == DataSource.FALLBACK
    assert prov["hotel"] == DataSource.LIVE
    assert prov["food"] == DataSource.ESTIMATED
    assert prov["local_transit"] == DataSource.ESTIMATED


# ============================================================================
# 2. Optimization Engine Tests (The Frozen 4 Downgrade Steps)
# ============================================================================
def test_optimizer_attempt_1_hotel_tier_down():
    """Verify Attempt 1: Hotel tier down successfully resolves budget deficit."""
    optimizer = OptimizationEngine()
    trip_id = uuid4()
    total_budget = Decimal("15000.00")

    # Initial expensive 5-star hotel: ₹10,000 for 3 nights
    initial_hotel = HotelOption(
        name="Luxury 5-Star Resort",
        hotel_class=5,
        price_per_night=Decimal("3333.33"),
        total_price=Decimal("10000.00"),
    )
    # Available cheaper 3-star hotel: ₹3,600 for 3 nights
    cheaper_hotel = HotelOption(
        name="Comfort 3-Star Inn",
        hotel_class=3,
        price_per_night=Decimal("1200.00"),
        total_price=Decimal("3600.00"),
    )

    transport = FlightOption(airline="IndiGo", price=Decimal("3000.00"))
    estimation = EstimationLayer()
    food = estimation.estimate_food(people=1, days=3, tier="standard")
    transit = estimation.estimate_local_transit_daily(days=3, people=1)

    result = optimizer.optimize(
        trip_id=trip_id,
        total_budget=total_budget,
        people=1,
        days=3,
        initial_transport=transport,
        initial_hotel=initial_hotel,
        initial_food=food,
        initial_transit=transit,
        available_hotels=[cheaper_hotel],
    )

    assert result.is_feasible is True
    assert result.successful_attempt == 1
    assert result.total_attempts == 1
    assert result.selected_hotel.name == "Comfort 3-Star Inn"
    assert "Hotel downgraded" in result.downgrades_applied[0]


def test_optimizer_attempt_2_transport_class_down():
    """Verify Attempt 2: Transport class down resolves deficit when hotel down wasn't enough."""
    optimizer = OptimizationEngine()
    trip_id = uuid4()
    total_budget = Decimal("12000.00")

    initial_hotel = HotelOption(name="Hotel A", total_price=Decimal("4000.00"), price_per_night=Decimal("1333.33"))
    # Expensive initial flight: ₹8,000
    initial_flight = FlightOption(airline="Air India", price=Decimal("8000.00"))
    # Cheaper train option: ₹1,200
    cheaper_train = TransitOption(
        transit_type="train",
        origin="Delhi",
        destination="Jaipur",
        name_or_operator="Shatabdi Express",
        price=Decimal("1200.00"),
        class_or_type="CC",
    )

    estimation = EstimationLayer()
    food = estimation.estimate_food(people=1, days=3, tier="standard")
    transit = estimation.estimate_local_transit_daily(days=3, people=1)

    result = optimizer.optimize(
        trip_id=trip_id,
        total_budget=total_budget,
        people=1,
        days=3,
        initial_transport=initial_flight,
        initial_hotel=initial_hotel,
        initial_food=food,
        initial_transit=transit,
        available_hotels=[],  # No cheaper hotel available
        available_transports=[cheaper_train],
    )

    assert result.is_feasible is True
    assert result.successful_attempt == 2
    assert result.total_attempts == 2
    assert result.selected_transport.price == Decimal("1200.00")
    assert "Transport downgraded" in result.downgrades_applied[0]


def test_optimizer_attempt_3_reduce_trip_length():
    """Verify Attempt 3: Reducing trip length by 1 day achieves feasibility."""
    optimizer = OptimizationEngine()
    trip_id = uuid4()
    # Budget of 12,000: 4 days is slightly too high, but 3 days fits!
    total_budget = Decimal("12000.00")

    hotel = HotelOption(name="Budget Stay", price_per_night=Decimal("1500.00"), total_price=Decimal("6000.00"))
    transport = FlightOption(airline="IndiGo", price=Decimal("3500.00"))

    estimation = EstimationLayer()
    food = estimation.estimate_food(people=1, days=4, tier="standard")
    transit = estimation.estimate_local_transit_daily(days=4, people=1)

    result = optimizer.optimize(
        trip_id=trip_id,
        total_budget=total_budget,
        people=1,
        days=4,
        initial_transport=transport,
        initial_hotel=hotel,
        initial_food=food,
        initial_transit=transit,
        available_hotels=[],
        available_transports=[],
    )

    assert result.is_feasible is True
    assert result.successful_attempt == 3
    assert result.total_attempts == 3
    assert result.days == 3
    assert "reduced to 3 days" in result.downgrades_applied[0]


def test_optimizer_attempt_4_trim_discretionary():
    """Verify Attempt 4: Trimming food to budget tier & removing discretionary spend succeeds."""
    optimizer = OptimizationEngine()
    trip_id = uuid4()
    # Extremely tight budget
    total_budget = Decimal("7000.00")

    hotel = HotelOption(name="Hostel", price_per_night=Decimal("800.00"), total_price=Decimal("1600.00"))
    transport = TransitOption(
        transit_type="train",
        origin="A",
        destination="B",
        name_or_operator="Exp",
        price=Decimal("1500.00"),
    )

    estimation = EstimationLayer()
    # Days = 1 so attempt 3 cannot reduce days further (current_days == 1)
    food = estimation.estimate_food(people=2, days=1, tier="comfort")  # 1500 * 2 = 3000
    transit = estimation.estimate_local_transit_daily(days=1, people=2)

    result = optimizer.optimize(
        trip_id=trip_id,
        total_budget=total_budget,
        people=2,
        days=1,
        initial_transport=transport,
        initial_hotel=hotel,
        initial_food=food,
        initial_transit=transit,
        activities_budget=Decimal("1200.00"),
        available_hotels=[],
        available_transports=[],
    )

    assert result.is_feasible is True
    assert result.successful_attempt == 4
    assert result.total_attempts == 4
    assert result.final_evaluation.breakdown.food_cost == Decimal("800.00")  # Budget tier: 400 * 2 * 1
    assert result.final_evaluation.breakdown.bucket_c_activities == Decimal("0.00")


def test_optimizer_all_four_attempts_fail():
    """Verify that if all 4 attempts fail, optimizer stops at 4 and provides actionable advice."""
    attempt_repo = AttemptRepository(client=None)
    optimizer = OptimizationEngine(attempt_repo=attempt_repo)
    trip_id = uuid4()
    # Impossibly small budget for luxury requirements
    total_budget = Decimal("5000.00")

    hotel = HotelOption(name="Luxury Villa", price_per_night=Decimal("10000.00"), total_price=Decimal("40000.00"))
    transport = FlightOption(airline="Vistara", price=Decimal("15000.00"))
    estimation = EstimationLayer()
    food = estimation.estimate_food(people=2, days=4, tier="comfort")
    transit = estimation.estimate_local_transit_daily(days=4, people=2)

    result = optimizer.optimize(
        trip_id=trip_id,
        total_budget=total_budget,
        people=2,
        days=4,
        initial_transport=transport,
        initial_hotel=hotel,
        initial_food=food,
        initial_transit=transit,
        activities_budget=Decimal("5000.00"),
        available_hotels=[],
        available_transports=[],
    )

    assert result.is_feasible is False
    assert result.final_status == "NOT_FEASIBLE"
    assert result.total_attempts == 4  # NEVER exceeds 4
    assert result.successful_attempt is None
    assert result.deficit > Decimal("0.00")
    assert result.recommendation is not None
    assert "Consider increasing budget" in result.recommendation

    # Verify attempt repository persistence
    recorded_attempts = attempt_repo.get_plan_attempts(trip_id)
    assert len(recorded_attempts) == 4
    assert [a.attempt_number for a in recorded_attempts] == [1, 2, 3, 4]
    assert recorded_attempts[-1].attempt_number == 4


def test_optimizer_stops_immediately_when_feasible():
    """Verify optimizer stops as soon as an attempt becomes feasible without further downgrading."""
    attempt_repo = AttemptRepository(client=None)
    optimizer = OptimizationEngine(attempt_repo=attempt_repo)
    trip_id = uuid4()

    # Step 1 hotel downgrade is enough
    initial_hotel = HotelOption(name="5-star", price_per_night=Decimal("5000.00"), total_price=Decimal("15000.00"))
    cheaper_hotel = HotelOption(name="3-star", price_per_night=Decimal("1000.00"), total_price=Decimal("3000.00"))
    transport = FlightOption(airline="IndiGo", price=Decimal("2000.00"))

    estimation = EstimationLayer()
    food = estimation.estimate_food(people=1, days=3, tier="standard")
    transit = estimation.estimate_local_transit_daily(days=3, people=1)

    result = optimizer.optimize(
        trip_id=trip_id,
        total_budget=Decimal("12000.00"),
        people=1,
        days=3,
        initial_transport=transport,
        initial_hotel=initial_hotel,
        initial_food=food,
        initial_transit=transit,
        available_hotels=[cheaper_hotel],
    )

    assert result.is_feasible is True
    assert result.total_attempts == 1  # Did NOT proceed to attempt 2, 3, or 4!
    attempts = attempt_repo.get_plan_attempts(trip_id)
    assert len(attempts) == 1
    assert attempts[0].attempt_number == 1
