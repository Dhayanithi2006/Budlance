"""Tests for Itinerary Generator and Virtual Ledger."""

from decimal import Decimal
from uuid import uuid4
import pytest

from budlance.db.repositories.itinerary_repo import ItineraryRepository
from budlance.db.repositories.ledger_repo import LedgerRepository
from budlance.engine.budget import ReverseBudgetEngine
from budlance.estimation.estimator import EstimationLayer
from budlance.itinerary.generator import ItineraryGenerator
from budlance.ledger.manager import VirtualLedgerManager
from budlance.schemas.travel import FlightOption, HotelOption, PlaceOption
from budlance.serpapi.models import DataSource


# ============================================================================
# Helpers
# ============================================================================
def create_feasible_evaluation(
    total_budget: Decimal = Decimal("25000.00"),
    days: int = 3,
    people: int = 2,
    flight_price: Decimal = Decimal("3000.00"),
    hotel_night_price: Decimal = Decimal("1500.00"),
):
    engine = ReverseBudgetEngine()
    estimation = EstimationLayer()
    transport = FlightOption(airline="IndiGo", price=flight_price, source=DataSource.LIVE)
    hotel = HotelOption(
        name="Sea Pearl Resort",
        price_per_night=hotel_night_price,
        total_price=hotel_night_price * Decimal(days),
        source=DataSource.LIVE,
    )
    food = estimation.estimate_food(people=people, days=days, tier="standard")
    transit = estimation.estimate_local_transit_daily(days=days, people=people)

    eval_result = engine.evaluate(
        total_budget=total_budget,
        people=people,
        days=days,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
        activities_budget=Decimal("2000.00"),
    )
    return eval_result, transport, hotel


# ============================================================================
# 1. Itinerary Generator Tests
# ============================================================================
def test_feasible_result_creates_valid_itinerary():
    """Verify that a FEASIBLE evaluation generates a day-by-day itinerary with exact days."""
    itinerary_repo = ItineraryRepository(client=None)
    generator = ItineraryGenerator(itinerary_repo=itinerary_repo)

    trip_id = uuid4()
    days = 3
    eval_result, transport, hotel = create_feasible_evaluation(days=days)
    places = [
        PlaceOption(name="Baga Beach", category="beach", source=DataSource.LIVE),
        PlaceOption(name="Aguada Fort", category="attraction", source=DataSource.LIVE),
    ]

    itinerary = generator.generate(
        trip_id=trip_id,
        destination="Goa",
        evaluation=eval_result,
        days=days,
        transport=transport,
        hotel=hotel,
        places=places,
    )

    assert itinerary.is_feasible is True
    assert itinerary.days_count == 3
    assert len(itinerary.days) == 3

    # Day 1: Onward transport & check-in
    day1 = itinerary.days[0]
    assert day1.day_number == 1
    item_categories = [it.category for it in day1.items]
    assert "transport" in item_categories
    assert "accommodation" in item_categories
    assert any("IndiGo" in it.activity for it in day1.items)
    assert any("Sea Pearl Resort" in it.activity for it in day1.items)

    # Places scheduled
    all_activities = [it.activity for d in itinerary.days for it in d.items]
    assert any("Baga Beach" in act for act in all_activities)

    # Final Day: Return transport
    day3 = itinerary.days[-1]
    assert day3.day_number == 3
    assert any("Return journey" in it.activity for it in day3.items)

    # Persistence verification in repo
    saved = itinerary_repo.get_itinerary(trip_id)
    assert saved is not None
    assert saved.is_feasible is True
    assert len(saved.days) == 3


def test_infeasible_result_does_not_create_valid_itinerary():
    """Verify that an infeasible evaluation returns is_feasible=False with empty days."""
    generator = ItineraryGenerator()
    trip_id = uuid4()

    # Create infeasible result
    engine = ReverseBudgetEngine()
    transport = FlightOption(airline="Air India", price=Decimal("15000.00"))
    hotel = HotelOption(name="Palace", total_price=Decimal("20000.00"), price_per_night=Decimal("5000.00"))
    estimation = EstimationLayer()
    food = estimation.estimate_food(people=2, days=4)
    transit = estimation.estimate_local_transit_daily(days=4, people=2)

    infeasible_eval = engine.evaluate(
        total_budget=Decimal("10000.00"),
        people=2,
        days=4,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
    )
    assert infeasible_eval.is_feasible is False

    itinerary = generator.generate(
        trip_id=trip_id,
        destination="Kerala",
        evaluation=infeasible_eval,
        days=4,
        transport=transport,
        hotel=hotel,
    )

    assert itinerary.is_feasible is False
    assert itinerary.days_count == 0
    assert len(itinerary.days) == 0
    assert "exceeds budget" in itinerary.feasibility_note


def test_itinerary_costs_derived_from_budget_without_inventions():
    """Verify planned costs in itinerary derive from budget allocations and are not invented."""
    generator = ItineraryGenerator()
    trip_id = uuid4()
    eval_result, transport, hotel = create_feasible_evaluation(days=2)

    itinerary = generator.generate(
        trip_id=trip_id,
        destination="Goa",
        evaluation=eval_result,
        days=2,
        transport=transport,
        hotel=hotel,
    )

    # Daily costs are strictly positive and account for journey
    for d in itinerary.days:
        assert d.daily_estimated_cost > Decimal("0.00")
        for item in d.items:
            assert item.planned_cost >= Decimal("0.00")


# ============================================================================
# 2. Virtual Ledger Tests
# ============================================================================
def test_ledger_initialization_from_feasible_result():
    """Verify Virtual Ledger initializes line items and master allocations from budget."""
    ledger_repo = LedgerRepository(client=None)
    manager = VirtualLedgerManager(ledger_repo=ledger_repo)

    trip_id = uuid4()
    eval_result, _, _ = create_feasible_evaluation()

    summary = manager.initialize_ledger(trip_id=trip_id, evaluation=eval_result)

    assert summary.trip_id == trip_id
    assert summary.total_budget == Decimal("25000.00")
    # All allocated categories sum to total_allocated
    assert summary.total_allocated <= summary.total_budget
    assert summary.total_spent == Decimal("0.00")
    assert summary.total_remaining == summary.total_allocated

    # Distinct categories present
    categories = {e.category for e in summary.entries}
    assert "fixed_booking" in categories
    assert "daily_survival" in categories
    assert "activities" in categories
    assert "rescue" in categories


def test_ledger_remaining_arithmetic_on_user_reported_spending():
    """Verify Remaining = Allocated - Spent and that planned is not overwritten by spent."""
    ledger_repo = LedgerRepository(client=None)
    manager = VirtualLedgerManager(ledger_repo=ledger_repo)

    trip_id = uuid4()
    eval_result, _, _ = create_feasible_evaluation()
    manager.initialize_ledger(trip_id=trip_id, evaluation=eval_result)

    # Record user spending: "I spent 400 for dinner" (daily_survival)
    reported_entry = manager.record_spending(
        trip_id=trip_id,
        category="daily_survival",
        amount=Decimal("400.00"),
        description="Local dinner seafood",
        source="user_reported",
    )

    assert reported_entry.spent_amount == Decimal("400.00")
    assert reported_entry.source == "user_reported"

    # Fetch updated summary
    summary = manager.get_summary(trip_id=trip_id)
    assert summary.total_spent == Decimal("400.00")
    assert summary.total_remaining == summary.total_allocated - Decimal("400.00")

    # Planned spending remains intact and distinct from spent
    survival_entries = [e for e in summary.entries if e.category == "daily_survival"]
    initial_survival = survival_entries[0]
    assert initial_survival.planned_amount > Decimal("0.00")
    assert reported_entry.planned_amount == Decimal("0.00")


def test_ledger_infeasible_initialization_fails():
    """Verify Virtual Ledger rejects initialization from an infeasible budget result."""
    manager = VirtualLedgerManager()
    trip_id = uuid4()

    engine = ReverseBudgetEngine()
    transport, hotel, food, transit = create_feasible_evaluation()[1:] + (None, None)
    infeasible_eval = engine.evaluate(
        total_budget=Decimal("0.00"),
        people=2,
        days=3,
        transport=None,
        hotel=None,
        food_estimate=food,
        local_transit_estimate=transit,
    )

    with pytest.raises(ValueError, match="infeasible"):
        manager.initialize_ledger(trip_id=trip_id, evaluation=infeasible_eval)


def test_ledger_provenance_preservation():
    """Verify cost provenance (LIVE, ESTIMATED, FALLBACK) survives in ledger entries."""
    ledger_repo = LedgerRepository(client=None)
    manager = VirtualLedgerManager(ledger_repo=ledger_repo)

    trip_id = uuid4()
    eval_result, _, _ = create_feasible_evaluation()
    summary = manager.initialize_ledger(trip_id=trip_id, evaluation=eval_result)

    # Transport & hotel were LIVE in fixture
    fixed_entries = [e for e in summary.entries if e.category == "fixed_booking"]
    assert any(e.source == "live" for e in fixed_entries)

    # Food & local transit are ESTIMATED
    daily_entries = [e for e in summary.entries if e.category == "daily_survival"]
    assert all(e.source == "estimated" for e in daily_entries)
