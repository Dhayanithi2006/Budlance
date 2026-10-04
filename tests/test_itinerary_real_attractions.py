"""Tests for structured itinerary generation: Mode A (curated attractions) and Mode B (structured free time)."""

from decimal import Decimal
from uuid import uuid4
import pytest

from budlance.engine.budget import ReverseBudgetEngine
from budlance.estimation.estimator import EstimationLayer
from budlance.itinerary.generator import ItineraryGenerator
from budlance.itinerary.models import DayPlan, GeneratedItinerary
from budlance.schemas.travel import FlightOption, HotelOption
from budlance.serpapi.models import DataSource

FORBIDDEN_FAKE_STRINGS = [
    "Central Landmark",
    "Old Town",
    "Local Market",
    "Main Square",
]


@pytest.fixture
def generator():
    return ItineraryGenerator()


@pytest.fixture
def feasible_eval_gujarat():
    engine = ReverseBudgetEngine()
    estimation = EstimationLayer()
    transport = FlightOption(airline="IndiGo", price=Decimal("4000.00"), source=DataSource.ESTIMATED)
    hotel = HotelOption(name="Heritage Haveli", total_price=Decimal("6000.00"), price_per_night=Decimal("2000.00"))
    food = estimation.estimate_food(people=2, days=3)
    transit = estimation.estimate_local_transit_daily(days=3, people=2)

    eval_result = engine.evaluate(
        total_budget=Decimal("25000.00"),
        people=2,
        days=3,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
    )
    return eval_result, transport, hotel


def test_mode_a_gujarat_real_attractions(generator, feasible_eval_gujarat):
    """Mode A (Gujarat): all attractions are real names from curated dataset."""
    eval_result, transport, hotel = feasible_eval_gujarat
    trip_id = uuid4()

    itin = generator.generate(
        trip_id=trip_id,
        destination="Gujarat",
        evaluation=eval_result,
        days=3,
        transport=transport,
        hotel=hotel,
        travel_party="family",
    )

    assert itin.is_feasible is True
    assert itin.days_count == 3
    assert len(itin.days) == 3

    # Collect curated attractions
    curated_items = [
        item for day in itin.days for item in day.items if item.is_curated
    ]
    assert len(curated_items) > 0

    known_gujarat_names = {
        "Sabarmati Ashram",
        "Adalaj Stepwell",
        "Sidi Saiyyed Mosque",
        "Akshardham Temple Gandhinagar",
        "Kankaria Lake",
        "Science City",
        "Modhera Sun Temple",
        "Rani Ki Vav",
    }
    for item in curated_items:
        assert item.attraction_name in known_gujarat_names
        assert item.opening_hours is not None
        assert isinstance(item.entry_fee_inr, int)
        assert len(item.description) > 0


def test_mode_a_zero_fake_strings(generator, feasible_eval_gujarat):
    """Mode A: no fake landmark strings like 'Central Landmark' anywhere in itinerary."""
    eval_result, transport, hotel = feasible_eval_gujarat
    itin = generator.generate(
        trip_id=uuid4(),
        destination="Gujarat",
        evaluation=eval_result,
        days=3,
        transport=transport,
        hotel=hotel,
    )

    for day in itin.days:
        for forbidden in FORBIDDEN_FAKE_STRINGS:
            assert forbidden not in day.theme_or_summary
        for item in day.items:
            for forbidden in FORBIDDEN_FAKE_STRINGS:
                assert forbidden not in item.activity
                if item.place_name:
                    assert forbidden not in item.place_name
                if item.description:
                    assert forbidden not in item.description


def test_mode_b_unknown_destination_structured_free_time(generator, feasible_eval_gujarat):
    """Mode B (unknown destination): all days use structured free time without fake landmarks."""
    eval_result, transport, hotel = feasible_eval_gujarat
    destination = "UnknownWonderland"

    itin = generator.generate(
        trip_id=uuid4(),
        destination=destination,
        evaluation=eval_result,
        days=3,
        transport=transport,
        hotel=hotel,
    )

    assert itin.is_feasible is True
    assert itin.days_count == 3
    assert len(itin.days) == 3

    for day in itin.days:
        assert isinstance(day, DayPlan)
        for forbidden in FORBIDDEN_FAKE_STRINGS:
            assert forbidden not in day.theme_or_summary
        for item in day.items:
            # Mode B items are not curated
            assert item.is_curated is False
            assert item.attraction_name is None
            for forbidden in FORBIDDEN_FAKE_STRINGS:
                assert forbidden not in item.activity
                if item.description:
                    assert forbidden not in item.description

            # Afternoon is structured free time
            if item.time_slot == "Afternoon":
                assert item.slot_type == "free_time"
                assert "Free time for self-guided exploration" in item.activity
                assert item.planned_cost == Decimal("0.00")


def test_each_day_has_morning_afternoon_evening_slots(generator, feasible_eval_gujarat):
    """Every generated day has exact morning, afternoon, and evening time slots."""
    eval_result, transport, hotel = feasible_eval_gujarat

    # Test both Mode A and Mode B
    for dest in ("Gujarat", "UnknownCityXYZ"):
        itin = generator.generate(
            trip_id=uuid4(),
            destination=dest,
            evaluation=eval_result,
            days=2,
            transport=transport,
            hotel=hotel,
        )
        assert len(itin.days) == 2
        for day in itin.days:
            slots = [item.time_slot for item in day.items]
            assert slots == ["Morning", "Afternoon", "Evening"]


def test_day_count_matches_duration_exactly(generator, feasible_eval_gujarat):
    """Day count matches requested duration exactly (1, 4, 5 days -> 1, 4, 5 DayPlan objects)."""
    eval_result, transport, hotel = feasible_eval_gujarat

    for duration in (1, 4, 5):
        itin = generator.generate(
            trip_id=uuid4(),
            destination="Gujarat",
            evaluation=eval_result,
            days=duration,
            transport=transport,
            hotel=hotel,
        )
        assert itin.days_count == duration
        assert len(itin.days) == duration
        assert all(isinstance(d, DayPlan) for d in itin.days)
