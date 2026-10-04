"""Tests for attraction fee integration in the reverse-budget waterfall."""

from decimal import Decimal
import pytest
from budlance.attractions.models import Attraction
from budlance.engine.budget import ReverseBudgetEngine
from budlance.schemas.travel import FlightOption, FoodEstimate, HotelOption, LocalTransitEstimate
from budlance.serpapi.models import DataSource


@pytest.fixture
def budget_engine():
    return ReverseBudgetEngine()


@pytest.fixture
def base_components():
    """Standard baseline components for 1 person, 3 days."""
    transport = FlightOption(
        price=Decimal("3000.00"),
        airline="IndiGo",
        flight_number="6E-101",
        departure_time="08:00",
        arrival_time="10:00",
        duration="2h",
        source=DataSource.ESTIMATED,
    )
    hotel = HotelOption(
        name="Standard Hotel",
        total_price=Decimal("4000.00"),
        price_per_night=Decimal("2000.00"),
        rating=4.0,
        source=DataSource.ESTIMATED,
    )
    food = FoodEstimate(
        daily_cost_per_person=Decimal("600.00"),
        total_cost=Decimal("1800.00"),
        tier="standard",
        days=3,
        people=1,
    )
    transit = LocalTransitEstimate(
        daily_cost_per_person=Decimal("200.00"),
        total_cost=Decimal("600.00"),
        mode="metro_bus",
        days=3,
        people=1,
    )
    return transport, hotel, food, transit


def test_backwards_compatibility_without_attractions(budget_engine, base_components):
    """Calling evaluate without attractions sets attraction_cost to 0 and behaves normally."""
    transport, hotel, food, transit = base_components
    # Budget 15000: rescue=1500, fixed=7000, survival=2400 -> mandatory=10900
    res = budget_engine.evaluate(
        total_budget=Decimal("15000.00"),
        people=1,
        days=3,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
    )
    assert res.is_feasible is True
    assert res.breakdown.attraction_cost == Decimal("0.00")
    assert res.breakdown.total_allocated == Decimal("10900.00")


def test_zero_attraction_fees_when_attractions_free(budget_engine, base_components):
    """Free attractions (fee=0) contribute 0 to attraction_cost."""
    transport, hotel, food, transit = base_components
    free_attractions = [
        Attraction(
            name="Sabarmati Ashram",
            category="culture",
            suitable_for=["solo", "couple", "family"],
            typical_time_hours=2.0,
            opening_hours="08:30-18:30",
            entry_fee_inr=0,
            description="Historic Gandhi Ashram on Sabarmati banks.",
            location="Ahmedabad",
            best_time_of_day="morning",
        ),
        Attraction(
            name="Adalaj Stepwell",
            category="architecture",
            suitable_for=["solo", "couple", "family"],
            typical_time_hours=1.5,
            opening_hours="06:00-18:00",
            entry_fee_inr=0,
            description="Ornate subterranean stepwell.",
            location="Gandhinagar",
            best_time_of_day="morning",
        ),
    ]

    res = budget_engine.evaluate(
        total_budget=Decimal("15000.00"),
        people=1,
        days=3,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
        selected_attractions=free_attractions,
    )
    assert res.is_feasible is True
    assert res.breakdown.attraction_cost == Decimal("0.00")
    assert res.breakdown.total_allocated == Decimal("10900.00")


def test_attraction_fees_added_to_total_cost(budget_engine, base_components):
    """Paid attractions add correctly to mandatory cost and total allocations."""
    transport, hotel, food, transit = base_components
    paid_attractions = [
        Attraction(
            name="Kankaria Lake",
            category="family_fun",
            suitable_for=["family", "friends"],
            typical_time_hours=2.0,
            opening_hours="09:00-22:00",
            entry_fee_inr=25,
            description="Lakeside promenade.",
            location="Ahmedabad",
            best_time_of_day="evening",
        ),
        Attraction(
            name="Science City",
            category="family_fun",
            suitable_for=["family", "friends"],
            typical_time_hours=3.0,
            opening_hours="10:00-20:00",
            entry_fee_inr=50,
            description="Interactive science center.",
            location="Ahmedabad",
            best_time_of_day="afternoon",
        ),
    ]
    # For 1 person, 25 + 50 = 75 INR
    res = budget_engine.evaluate(
        total_budget=Decimal("15000.00"),
        people=1,
        days=3,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
        selected_attractions=paid_attractions,
    )
    assert res.is_feasible is True
    assert res.breakdown.attraction_cost == Decimal("75.00")
    # Base mandatory was 10900.00 + 75 = 10975.00
    assert res.breakdown.total_allocated == Decimal("10975.00")
    assert res.breakdown.remaining_surplus == Decimal("15000.00") - Decimal("10975.00")


def test_attraction_fees_multiplied_by_people_count(budget_engine, base_components):
    """Attraction fees are multiplied by the number of travelers."""
    transport, hotel, food, transit = base_components
    # Adjust food and transit for 4 people
    food_4 = FoodEstimate(
        daily_cost_per_person=Decimal("600.00"),
        total_cost=Decimal("7200.00"),
        tier="standard",
        days=3,
        people=4,
    )
    transit_4 = LocalTransitEstimate(
        daily_cost_per_person=Decimal("200.00"),
        total_cost=Decimal("2400.00"),
        mode="metro_bus",
        days=3,
        people=4,
    )
    paid_attractions = [
        Attraction(
            name="Science City",
            category="family_fun",
            suitable_for=["family"],
            typical_time_hours=3.0,
            opening_hours="10:00-20:00",
            entry_fee_inr=50,
            description="Science center.",
            location="Ahmedabad",
            best_time_of_day="afternoon",
        ),
    ]
    # 4 people * 50 = 200 INR
    res = budget_engine.evaluate(
        total_budget=Decimal("30000.00"),
        people=4,
        days=3,
        transport=transport,
        hotel=hotel,
        food_estimate=food_4,
        local_transit_estimate=transit_4,
        selected_attractions=paid_attractions,
    )
    assert res.is_feasible is True
    assert res.breakdown.attraction_cost == Decimal("200.00")


def test_attraction_fees_can_cause_infeasibility(budget_engine, base_components):
    """Large attraction fees exceeding remaining budget cause NOT_FEASIBLE."""
    transport, hotel, food, transit = base_components
    # Budget is 11000.
    # Base mandatory = 1100 (reserve) + 7000 (fixed) + 2400 (survival) = 10500.
    # Remaining before attractions = 500.
    # If attraction fee is 800, mandatory becomes 11300 > 11000 -> NOT_FEASIBLE.
    expensive_attraction = [
        Attraction(
            name="Exclusive Safari / Monument",
            category="heritage",
            suitable_for=["solo"],
            typical_time_hours=4.0,
            opening_hours="06:00-18:00",
            entry_fee_inr=800,
            description="Premium entry fee.",
            location="Gujarat",
            best_time_of_day="morning",
        ),
    ]
    res = budget_engine.evaluate(
        total_budget=Decimal("11000.00"),
        people=1,
        days=3,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
        selected_attractions=expensive_attraction,
    )
    assert res.is_feasible is False
    assert res.status == "NOT_FEASIBLE"
    assert res.breakdown.attraction_cost == Decimal("800.00")
    assert res.deficit == Decimal("300.00")
