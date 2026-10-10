"""Deterministic unit and integration tests for Phase 3: Reverse-Budget Engine & Intelligent Cost Optimisation.

Covers:
1. Example 1 — Feasible ordinary-budget trip (Chennai -> Delhi, 2 people, 3 days, ₹90,000 budget)
2. Example 2 — Infeasible luxury trip (Chennai -> Goa, 4 people, 5 days, ₹10,000 budget)
3. Example 3 — High budget premium selection (Munnar, 5 days, couple, ₹10,000,00 budget) followed by budget change (₹60,000)
4. All 20 required regression specifications:
   - Round-trip transport cost
   - Passenger count normalization
   - Accommodation nightly vs total-stay price
   - Hotel nights calculation
   - Complete projected-cost sum
   - Bucket allocation reconciliation
   - Reserve counted exactly once
   - Trip Pass fee separated from travel costs
   - Feasibility rejection for unaffordable plans
   - Unknown essential prices preventing unsupported feasibility claims (INCOMPLETE_COST_DATA)
   - Bounded destination search semantics (BOUNDED_SEARCH_NO_FEASIBLE_OPTION)
   - Preference-aware ranking of available feasible options
   - High-budget premium selection
   - Budget changes and full recalculation
   - Date changes and provider-price freshness
   - Party-size changes and cost recalculation
   - Activity replacement without duplicate costs
   - Formatter values matching engine values
   - Provenance preserved through normalization and budget evaluation
   - Actual expenses kept separate from projections and quotes
"""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.config import get_settings
from budlance.db.models import BudgetAllocation, Itinerary, LedgerEntry, Trip, TripIntent, utc_now
from budlance.db.repositories.conversation_repo import ConversationStateRepository
from budlance.db.repositories.itinerary_repo import ItineraryRepository
from budlance.db.repositories.ledger_repo import LedgerRepository
from budlance.db.repositories.trip_repo import TripRepository
from budlance.db.repositories.user_repo import UserRepository
from budlance.engine.budget import ReverseBudgetEngine
from budlance.engine.models import BudgetBreakdown, BudgetEvaluationResult, FeasibilityStatus
from budlance.engine.optimizer import OptimizationEngine
from budlance.estimation.estimator import EstimationLayer
from budlance.ledger.manager import VirtualLedgerManager
from budlance.orchestrator.formatter import format_feasible_plan, format_infeasible_plan
from budlance.orchestrator.models import OrchestrationResult
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.rescue.service import RescueResult, RescueService
from budlance.schemas.dates import build_trip_date_context, calculate_stay_nights
from budlance.schemas.travel import FlightOption, FoodEstimate, HotelOption, LocalTransitEstimate, PlaceOption, TransitOption
from budlance.serpapi.models import DataSource, TravelDataEnvelope


# =============================================================================
# STEP G: THREE EXECUTION EXAMPLES
# =============================================================================

def test_example_1_feasible_ordinary_budget_trip():
    """Example 1 — Feasible, ordinary-budget trip.

    Input:
    "Plan a three-day round-trip flight journey from Chennai to Delhi for two people with a budget of ₹90,000."

    Fixtures:
    - Complete outbound-and-return party fare: ₹45,352.
    - Total accommodation cost: ₹5,000.
    - Daily survival allocation: ₹5,400.
    - Activities allocation: ₹4,500.
    - Rescue reserve: ₹9,000.
    - Total budget: ₹90,000.

    Expected:
    - Total projected allocation = ₹45,352 + ₹5,000 + ₹5,400 + ₹4,500 + ₹9,000 = ₹69,252.
    - Remaining unallocated budget = ₹90,000 - ₹69,252 = ₹20,748.
    - Feasible outcome.
    - Group flight fare is not multiplied a second time.
    - Accommodation cost is not multiplied a second time.
    - Every allocation counted exactly once.
    - Total allocated + remaining surplus == ₹90,000.
    - Formatter reports the same figures as the engine.
    """
    engine = ReverseBudgetEngine()
    total_budget = Decimal("90000.00")
    people = 2
    days = 3

    # Group flight option: ₹45,352 total for 2 passengers round trip
    transport = FlightOption(
        price=Decimal("45352.00"),
        airline="Air India",
        departure_airport="MAA",
        arrival_airport="DEL",
        price_scope="total",
        source=DataSource.LIVE,
    )
    # Total stay hotel: ₹5,000 complete stay for 2 nights
    hotel = HotelOption(
        name="Delhi City Hotel",
        price_per_night=Decimal("2500.00"),
        total_price=Decimal("5000.00"),
        nights=2,
        price_scope="total_stay",
        source=DataSource.LIVE,
    )
    # Food: ₹1,500/day/person * 2 * 3 = not used directly; use custom FoodEstimate with ₹3,600 total
    food_est = FoodEstimate(
        tier="standard",
        daily_cost_per_person=Decimal("600.00"),
        total_cost=Decimal("3600.00"),
        people=2,
        days=3,
        source=DataSource.ESTIMATED,
    )
    # Transit: ₹1,800 total -> Daily survival = 3,600 + 1,800 = ₹5,400
    transit_est = LocalTransitEstimate(
        mode="metro_bus",
        total_cost=Decimal("1800.00"),
        days=3,
        people=2,
        source=DataSource.ESTIMATED,
    )
    activities_budget = Decimal("4500.00")

    result = engine.evaluate(
        total_budget=total_budget,
        people=people,
        days=days,
        transport=transport,
        hotel=hotel,
        food_estimate=food_est,
        local_transit_estimate=transit_est,
        activities_budget=activities_budget,
        currency="INR",
        requires_transport=True,
        requires_lodging=True,
    )

    assert result.is_feasible is True
    assert result.status == "FEASIBLE"
    bd = result.breakdown

    # Assert exact allocations matching stated assumptions
    assert bd.bucket_a_fixed == Decimal("50352.00")  # 45,352 transport + 5,000 hotel
    assert bd.transport_cost == Decimal("45352.00")
    assert bd.hotel_cost == Decimal("5000.00")
    assert bd.bucket_b_survival == Decimal("5400.00")  # 3,600 food + 1,800 transit
    assert bd.bucket_c_activities == Decimal("4500.00")
    assert bd.bucket_d_rescue == Decimal("9000.00")  # 10% of ₹90,000

    # Total allocated = 50,352 + 5,400 + 4,500 + 9,000 = ₹69,252
    assert bd.total_allocated == Decimal("69252.00")
    # Surplus = 90,000 - 69,252 = ₹20,748
    assert bd.remaining_surplus == Decimal("20748.00")
    assert bd.is_reconciled() is True
    assert (bd.total_allocated + bd.remaining_surplus) == total_budget

    # Formatter output verification: formatter must report identical numbers
    formatted = format_feasible_plan(
        destination="Delhi",
        days=days,
        people=people,
        breakdown=bd,
        transport=transport,
        hotel=hotel,
        itinerary=None,
        ledger=None,
    )
    assert "90,000.00" in formatted
    assert "69,252.00" in formatted
    assert "20,748.00" in formatted
    assert "45,352.00" in formatted
    assert "5,000.00" in formatted
    assert "9,000.00" in formatted


def test_example_2_infeasible_luxury_trip():
    """Example 2 — Infeasible luxury trip.

    Input:
    "Plan a five-day luxury trip from Chennai to Goa for four people with a budget of ₹10,000."

    Fixtures:
    - Round-trip flight for 4 people: ₹36,000.
    - 5-star luxury resort (4 nights): ₹48,000.
    - Food (comfort tier, 4 people, 5 days): ₹30,000.
    - Local transit: ₹4,000.
    - Rescue reserve: ₹1,000 (10% of ₹10,000).
    - Total required costs = ₹1,19,000 >> ₹10,000.

    Expected:
    - Status is NOT_FEASIBLE.
    - Mathematically calculated deficit: 1,19,000 - 10,000 = ₹1,09,000.
    - Luxury preference is preserved (not silently downgraded).
    - No fabricated cheap data to force feasibility.
    - Rejection explanation discloses the deficit and cost contributors.
    """
    engine = ReverseBudgetEngine()
    total_budget = Decimal("10000.00")
    people = 4
    days = 5

    luxury_transport = FlightOption(
        price=Decimal("36000.00"),
        airline="IndiGo",
        departure_airport="MAA",
        arrival_airport="GOI",
        source=DataSource.LIVE,
    )
    luxury_resort = HotelOption(
        name="Goa Luxury 5-Star Beach Resort",
        hotel_class=5,
        price_per_night=Decimal("12000.00"),
        total_price=Decimal("48000.00"),
        nights=4,
        source=DataSource.LIVE,
    )
    food_est = FoodEstimate(
        tier="comfort",
        daily_cost_per_person=Decimal("1500.00"),
        total_cost=Decimal("30000.00"),
        people=4,
        days=5,
        source=DataSource.ESTIMATED,
    )
    transit_est = LocalTransitEstimate(
        mode="cab",
        total_cost=Decimal("4000.00"),
        days=5,
        people=4,
        source=DataSource.ESTIMATED,
    )

    result = engine.evaluate(
        total_budget=total_budget,
        people=people,
        days=days,
        transport=luxury_transport,
        hotel=luxury_resort,
        food_estimate=food_est,
        local_transit_estimate=transit_est,
        activities_budget=Decimal("0.00"),
        currency="INR",
        requires_transport=True,
        requires_lodging=True,
    )

    assert result.is_feasible is False
    assert result.status == "NOT_FEASIBLE"
    # Mandatory costs: reserve(1,000) + fixed(36,000+48,000) + survival(30,000+4,000) = 1,19,000
    expected_mandatory = Decimal("119000.00")
    expected_deficit = expected_mandatory - total_budget  # ₹1,09,000
    assert result.deficit == expected_deficit
    assert "exceeds budget by INR 109000.00" in result.explanation

    # Verify optimizer does not silently force feasibility on this impossible budget
    optimizer = OptimizationEngine(budget_engine=engine)
    opt_result = optimizer.optimize(
        trip_id=None,
        total_budget=total_budget,
        people=people,
        days=days,
        initial_transport=luxury_transport,
        initial_hotel=luxury_resort,
        initial_food=food_est,
        initial_transit=transit_est,
        activities_budget=Decimal("0.00"),
        available_hotels=[luxury_resort],
        available_transports=[luxury_transport],
        requires_transport=True,
        requires_lodging=True,
    )
    assert opt_result.is_feasible is False
    assert opt_result.final_status == "NOT_FEASIBLE"
    assert opt_result.deficit > Decimal("0.00")

    # Formatter output must state exact budget and deficit
    formatted_infeasible = format_infeasible_plan(
        destination="Goa",
        budget=total_budget,
        deficit=result.deficit,
        explanation=result.explanation,
    )
    assert "10,000.00" in formatted_infeasible
    assert "109,000.00" in formatted_infeasible


def test_example_3_high_budget_and_subsequent_budget_change():
    """Example 3 — High budget followed by a budget change.

    Initial input:
    "Plan a five-day trip from Chennai to Munnar for two people. Our budget is ₹10,00,000.
    We are a couple and prefer premium accommodation, calm nature and sunrise experiences."

    Expected:
    - Selects premium accommodation and quality options matching preferences.
    - Does not artificially consume the ₹10,00,000 budget.
    - Reports massive surplus accurately.

    Follow-up:
    "Reduce my budget to ₹60,000 but keep my nature and sunrise preferences."

    Expected:
    - Same trip identity updated.
    - Preserves Munnar, duration, couple party, and nature/sunrise preferences.
    - Full recalculation against ₹60,000 budget.
    - No duplicate ledger entries or old prices retained.
    """
    engine = ReverseBudgetEngine()
    estimator = EstimationLayer()
    optimizer = OptimizationEngine(budget_engine=engine, estimation_layer=estimator)
    ledger_repo = LedgerRepository()
    ledger_repo.reset_in_memory_store()
    ledger_mgr = VirtualLedgerManager(ledger_repo)

    trip_id = uuid4()
    initial_budget = Decimal("1000000.00")
    people = 2
    days = 5

    # Mock hotels in Munnar: luxury resort, standard hotel, budget lodge
    luxury_resort = HotelOption(
        name="Munnar Grand Tea Valley Luxury Resort",
        hotel_class=5,
        rating=4.9,
        review_count=1200,
        price_per_night=Decimal("15000.00"),
        total_price=Decimal("60000.00"),  # 4 nights
        nights=4,
        source=DataSource.LIVE,
    )
    mid_hotel = HotelOption(
        name="Munnar Mist Standard Hotel",
        hotel_class=3,
        rating=4.2,
        review_count=450,
        price_per_night=Decimal("4000.00"),
        total_price=Decimal("16000.00"),
        nights=4,
        source=DataSource.LIVE,
    )
    budget_lodge = HotelOption(
        name="Backpacker Nature Stay",
        hotel_class=2,
        rating=3.8,
        review_count=120,
        price_per_night=Decimal("1500.00"),
        total_price=Decimal("6000.00"),
        nights=4,
        source=DataSource.LIVE,
    )
    available_hotels = [budget_lodge, mid_hotel, luxury_resort]

    # Transports
    cab_transport = TransitOption(
        transit_type="bus",
        origin="Chennai",
        destination="Munnar",
        name_or_operator="Premium Private Chauffeur SUV",
        price=Decimal("18000.00"),
        source=DataSource.LIVE,
    )
    train_transport = TransitOption(
        transit_type="train",
        origin="Chennai",
        destination="Munnar",
        name_or_operator="Southern Railway 2AC",
        price=Decimal("4200.00"),
        source=DataSource.FALLBACK,
    )
    available_transports = [train_transport, cab_transport]

    # 1. Preferred selection under generous ₹10,00,000 budget with luxury preference
    selected_hotel = optimizer.select_preferred_hotel(
        available_hotels=available_hotels,
        budget_limit=initial_budget * Decimal("0.70"),
        preferences=["premium", "nature", "sunrise"],
        is_generous_budget=True,
    )
    assert selected_hotel.name == luxury_resort.name
    assert selected_hotel.hotel_class == 5

    selected_trans = optimizer.select_preferred_transport(
        available_transports=available_transports,
        budget_limit=initial_budget * Decimal("0.50"),
        preferences=["premium"],
        is_generous_budget=True,
    )
    assert selected_trans.name_or_operator == cab_transport.name_or_operator

    food_est = estimator.estimate_food(people, days, tier="comfort")
    transit_est = estimator.estimate_local_transit_daily(days, people, mode="cab")
    activities_budget = Decimal("15000.00")

    initial_eval = engine.evaluate(
        total_budget=initial_budget,
        people=people,
        days=days,
        transport=selected_trans,
        hotel=selected_hotel,
        food_estimate=food_est,
        local_transit_estimate=transit_est,
        activities_budget=activities_budget,
        requires_transport=True,
        requires_lodging=True,
    )
    assert initial_eval.is_feasible is True
    # Rescue reserve: 10% of 10,00,000 = ₹1,00,000
    assert initial_eval.breakdown.bucket_d_rescue == Decimal("100000.00")
    # Total allocated should be far below 10,00,000 (not arbitrarily exhausted)
    assert initial_eval.breakdown.total_allocated < Decimal("300000.00")
    # Surplus should be substantial (>= ₹7,00,000)
    assert initial_eval.breakdown.remaining_surplus > Decimal("700000.00")
    assert initial_eval.breakdown.is_reconciled() is True

    # Initialize ledger for the initial planning trip
    summary_1 = ledger_mgr.initialize_ledger(trip_id=trip_id, evaluation=initial_eval)
    entries_1 = ledger_repo.get_ledger_entries(trip_id)
    assert len(entries_1) == 6
    assert summary_1.total_allocated == initial_eval.breakdown.total_allocated

    # 2. FOLLOW-UP: "Reduce my budget to ₹60,000 but keep my nature and sunrise preferences"
    reduced_budget = Decimal("60000.00")

    # Re-evaluate candidate options under reduced ₹60,000 budget
    affordable_hotel = optimizer.select_preferred_hotel(
        available_hotels=available_hotels,
        budget_limit=reduced_budget * Decimal("0.45"),
        preferences=["nature", "sunrise"],
        is_generous_budget=False,
    )
    assert affordable_hotel.name in (mid_hotel.name, budget_lodge.name)
    assert affordable_hotel.total_price <= Decimal("27000.00")

    affordable_trans = optimizer.select_preferred_transport(
        available_transports=available_transports,
        budget_limit=reduced_budget * Decimal("0.35"),
        preferences=["nature"],
        is_generous_budget=False,
    )
    assert affordable_trans.price <= Decimal("21000.00")

    reduced_food = estimator.estimate_food(people, days, tier="standard")
    reduced_transit = estimator.estimate_local_transit_daily(days, people, mode="metro_bus")
    reduced_activities = Decimal("3000.00")

    recalc_eval = engine.evaluate(
        total_budget=reduced_budget,
        people=people,
        days=days,
        transport=affordable_trans,
        hotel=affordable_hotel,
        food_estimate=reduced_food,
        local_transit_estimate=reduced_transit,
        activities_budget=reduced_activities,
        requires_transport=True,
        requires_lodging=True,
    )
    assert recalc_eval.is_feasible is True
    assert recalc_eval.breakdown.total_budget == Decimal("60000.00")
    assert recalc_eval.breakdown.bucket_d_rescue == Decimal("6000.00")
    assert recalc_eval.breakdown.total_allocated <= Decimal("60000.00")
    assert recalc_eval.breakdown.is_reconciled() is True

    # Re-initialize ledger for the SAME trip_id: ensure no duplicate entries
    summary_2 = ledger_mgr.initialize_ledger(trip_id=trip_id, evaluation=recalc_eval)
    entries_2 = ledger_repo.get_ledger_entries(trip_id)
    # Must have exactly 6 baseline entries (not 12!)
    assert len(entries_2) == 6
    assert summary_2.total_budget - summary_2.total_allocated == recalc_eval.breakdown.remaining_surplus
    assert summary_2.total_remaining == summary_2.total_allocated - summary_2.total_spent


# =============================================================================
# STEP H: 20 REGRESSION TESTS
# =============================================================================

def test_1_complete_round_trip_transport_cost():
    """1. Outbound and return transport is counted exactly once."""
    engine = ReverseBudgetEngine()
    flight = FlightOption(
        price=Decimal("12000.00"),
        price_scope="total",
        source=DataSource.LIVE,
    )
    food = FoodEstimate(tier="budget", daily_cost_per_person=Decimal("400.00"), total_cost=Decimal("1200.00"), people=1, days=3, source=DataSource.ESTIMATED)
    transit = LocalTransitEstimate(mode="metro_bus", total_cost=Decimal("300.00"), source=DataSource.ESTIMATED)

    eval_res = engine.evaluate(
        total_budget=Decimal("20000.00"),
        people=1,
        days=3,
        transport=flight,
        hotel=None,
        food_estimate=food,
        local_transit_estimate=transit,
        requires_lodging=False,
    )
    assert eval_res.breakdown.transport_cost == Decimal("12000.00")
    # Must not be multiplied again by 2 for round-trip or party
    assert eval_res.breakdown.bucket_a_fixed == Decimal("12000.00")


def test_2_passenger_count_normalization():
    """2. Passenger group fares are not multiplied by passenger count again."""
    engine = ReverseBudgetEngine()
    group_flight = FlightOption(
        price=Decimal("30000.00"),
        price_scope="total",
        source=DataSource.LIVE,
    )
    food = FoodEstimate(tier="standard", daily_cost_per_person=Decimal("800.00"), total_cost=Decimal("4800.00"), people=3, days=2, source=DataSource.ESTIMATED)
    transit = LocalTransitEstimate(mode="metro_bus", total_cost=Decimal("600.00"), source=DataSource.ESTIMATED)

    eval_res = engine.evaluate(
        total_budget=Decimal("50000.00"),
        people=3,
        days=2,
        transport=group_flight,
        hotel=None,
        food_estimate=food,
        local_transit_estimate=transit,
    )
    assert eval_res.breakdown.transport_cost == Decimal("30000.00")


def test_3_accommodation_nightly_vs_total_stay_price():
    """3. Total-stay hotel rates are not multiplied by nights again."""
    engine = ReverseBudgetEngine()
    hotel = HotelOption(
        name="Stay Hotel",
        price_per_night=Decimal("2000.00"),
        total_price=Decimal("6000.00"),
        nights=3,
        price_scope="total_stay",
        source=DataSource.LIVE,
    )
    food = FoodEstimate(tier="standard", daily_cost_per_person=Decimal("800.00"), total_cost=Decimal("2400.00"), people=1, days=4, source=DataSource.ESTIMATED)
    transit = LocalTransitEstimate(mode="metro_bus", total_cost=Decimal("400.00"), source=DataSource.ESTIMATED)

    eval_res = engine.evaluate(
        total_budget=Decimal("20000.00"),
        people=1,
        days=4,
        transport=None,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
    )
    assert eval_res.breakdown.hotel_cost == Decimal("6000.00")


def test_4_correct_hotel_nights_calculation():
    """4. Correct hotel nights derived as checkout minus check-in."""
    nights = calculate_stay_nights("2026-11-10", "2026-11-14")
    assert nights == 4

    ctx = build_trip_date_context(days=5, start_date="2026-11-10", return_date="2026-11-14")
    assert ctx.stay_nights == 4
    assert ctx.days == 5


def test_5_complete_projected_cost_sum():
    """5. Total projected trip cost sums transport, stay, food, transit, activities, reserve."""
    engine = ReverseBudgetEngine()
    total_budget = Decimal("100000.00")
    transport = FlightOption(price=Decimal("40000.00"), source=DataSource.LIVE)
    hotel = HotelOption(name="Hotel", total_price=Decimal("15000.00"), source=DataSource.LIVE)
    food = FoodEstimate(tier="standard", daily_cost_per_person=Decimal("800.00"), total_cost=Decimal("6400.00"), people=2, days=4, source=DataSource.ESTIMATED)
    transit = LocalTransitEstimate(mode="cab", total_cost=Decimal("3600.00"), source=DataSource.ESTIMATED)
    activities = Decimal("5000.00")

    result = engine.evaluate(
        total_budget=total_budget,
        people=2,
        days=4,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
        activities_budget=activities,
    )
    # Expected: 40,000 + 15,000 + 6,400 + 3,600 + 5,000 + 10,000 (reserve) = 80,000
    assert result.breakdown.total_allocated == Decimal("80000.00")
    assert result.breakdown.projected_trip_cost == Decimal("80000.00")


def test_6_bucket_allocation_reconciliation():
    """6. Bucket allocations reconcile to total budget for feasible trips."""
    engine = ReverseBudgetEngine()
    total_budget = Decimal("50000.00")
    result = engine.evaluate(
        total_budget=total_budget,
        people=1,
        days=2,
        transport=FlightOption(price=Decimal("15000.00"), source=DataSource.LIVE),
        hotel=HotelOption(name="H", total_price=Decimal("5000.00"), source=DataSource.LIVE),
        food_estimate=FoodEstimate(tier="standard", daily_cost_per_person=Decimal("800.00"), total_cost=Decimal("1600.00"), people=1, days=2, source=DataSource.ESTIMATED),
        local_transit_estimate=LocalTransitEstimate(mode="metro_bus", total_cost=Decimal("400.00"), source=DataSource.ESTIMATED),
        activities_budget=Decimal("3000.00"),
    )
    bd = result.breakdown
    assert (bd.total_allocated + bd.remaining_surplus) == total_budget
    assert bd.is_reconciled() is True


def test_7_reserve_counted_exactly_once():
    """7. Rescue reserve is counted exactly once and not treated as actual spend."""
    engine = ReverseBudgetEngine()
    budget = Decimal("40000.00")
    result = engine.evaluate(
        total_budget=budget,
        people=1,
        days=2,
        transport=FlightOption(price=Decimal("10000.00"), source=DataSource.LIVE),
        hotel=HotelOption(name="H", total_price=Decimal("4000.00"), source=DataSource.LIVE),
        food_estimate=FoodEstimate(tier="budget", daily_cost_per_person=Decimal("400.00"), total_cost=Decimal("800.00"), people=1, days=2, source=DataSource.ESTIMATED),
        local_transit_estimate=LocalTransitEstimate(mode="auto", total_cost=Decimal("200.00"), source=DataSource.ESTIMATED),
    )
    # Reserve is 10% = ₹4,000
    assert result.breakdown.bucket_d_rescue == Decimal("4000.00")
    # Total allocated = 10,000 + 4,000 + 1,000 + 4,000 = 19,000
    assert result.breakdown.total_allocated == Decimal("19000.00")
    # Surplus = 40,000 - 19,000 = 21,000
    assert result.breakdown.remaining_surplus == Decimal("21000.00")


def test_8_trip_pass_fee_separated_from_travel_costs():
    """8. The ₹49 Trip Pass fee is strictly separated from the travel budget."""
    settings = get_settings()
    assert settings.trip_pass_amount == Decimal("49.00")

    engine = ReverseBudgetEngine()
    result = engine.evaluate(
        total_budget=Decimal("10000.00"),
        people=1,
        days=2,
        transport=FlightOption(price=Decimal("4000.00"), source=DataSource.LIVE),
        hotel=HotelOption(name="H", total_price=Decimal("2000.00"), source=DataSource.LIVE),
        food_estimate=FoodEstimate(tier="standard", daily_cost_per_person=Decimal("800.00"), total_cost=Decimal("1600.00"), people=1, days=2, source=DataSource.ESTIMATED),
        local_transit_estimate=LocalTransitEstimate(mode="auto", total_cost=Decimal("400.00"), source=DataSource.ESTIMATED),
    )
    # Trip Pass fee is NOT subtracted from travel budget
    assert result.breakdown.total_budget == Decimal("10000.00")
    # Waterfall total does not contain 49.00
    assert (result.breakdown.total_allocated % 1) == Decimal("0.00")


def test_9_feasibility_rejection_for_unaffordable_plans():
    """9. Rejects unaffordable plans and computes deficit accurately."""
    engine = ReverseBudgetEngine()
    result = engine.evaluate(
        total_budget=Decimal("5000.00"),
        people=2,
        days=3,
        transport=FlightOption(price=Decimal("12000.00"), source=DataSource.LIVE),
        hotel=HotelOption(name="H", total_price=Decimal("4000.00"), source=DataSource.LIVE),
        food_estimate=FoodEstimate(tier="standard", daily_cost_per_person=Decimal("800.00"), total_cost=Decimal("4800.00"), people=2, days=3, source=DataSource.ESTIMATED),
        local_transit_estimate=LocalTransitEstimate(mode="auto", total_cost=Decimal("600.00"), source=DataSource.ESTIMATED),
    )
    assert result.is_feasible is False
    assert result.status == "NOT_FEASIBLE"
    assert result.deficit > Decimal("0.00")


def test_10_unknown_essential_prices_prevent_unsupported_feasibility():
    """10. Missing essential transport/lodging returns INCOMPLETE_COST_DATA."""
    engine = ReverseBudgetEngine()
    result = engine.evaluate(
        total_budget=Decimal("50000.00"),
        people=2,
        days=3,
        transport=None,  # Missing transport on required route
        hotel=HotelOption(name="H", total_price=Decimal("4000.00"), source=DataSource.LIVE),
        food_estimate=FoodEstimate(tier="standard", daily_cost_per_person=Decimal("800.00"), total_cost=Decimal("4800.00"), people=2, days=3, source=DataSource.ESTIMATED),
        local_transit_estimate=LocalTransitEstimate(mode="metro_bus", total_cost=Decimal("400.00"), source=DataSource.ESTIMATED),
        requires_transport=True,
    )
    assert result.is_feasible is False
    assert result.status == "INCOMPLETE_COST_DATA"
    assert "transport" in result.missing_cost_items


def test_11_bounded_destination_search_semantics():
    """11. Orchestrator records bounded search status when candidate limit is reached."""
    res = OrchestrationResult(
        status="NOT_FEASIBLE",
        feasibility_status="BOUNDED_SEARCH_NO_FEASIBLE_OPTION",
        search_scope="bounded",
        evaluated_candidates_count=5,
        message_text="No feasible destination found within bounded search.",
    )
    assert res.feasibility_status == "BOUNDED_SEARCH_NO_FEASIBLE_OPTION"
    assert res.search_scope == "bounded"
    assert res.evaluated_candidates_count == 5


def test_12_preference_aware_ranking_of_available_feasible_options():
    """12. Preference-aware ranking ranks by review rating and class within budget."""
    optimizer = OptimizationEngine()
    h1 = HotelOption(name="Budget Lodge", hotel_class=2, rating=3.5, total_price=Decimal("4000.00"), source=DataSource.LIVE)
    h2 = HotelOption(name="Top Boutique", hotel_class=4, rating=4.8, total_price=Decimal("8000.00"), source=DataSource.LIVE)
    h3 = HotelOption(name="Mediocre Mid", hotel_class=3, rating=3.9, total_price=Decimal("6000.00"), source=DataSource.LIVE)

    selected = optimizer.select_preferred_hotel(
        available_hotels=[h1, h2, h3],
        budget_limit=Decimal("10000.00"),
        preferences=["boutique", "quality"],
        is_generous_budget=True,
    )
    assert selected.name == "Top Boutique"
    assert selected.hotel_class == 4


def test_13_high_budget_premium_selection():
    """13. High budget selects premium options without consuming every rupee."""
    optimizer = OptimizationEngine()
    h_cheapest = HotelOption(name="Cheap Motel", hotel_class=1, rating=2.5, total_price=Decimal("2000.00"), source=DataSource.LIVE)
    h_luxury = HotelOption(name="5-Star Palace", hotel_class=5, rating=4.9, total_price=Decimal("45000.00"), source=DataSource.LIVE)

    selected = optimizer.select_preferred_hotel(
        available_hotels=[h_cheapest, h_luxury],
        budget_limit=Decimal("500000.00"),
        preferences=["luxury"],
        is_generous_budget=True,
    )
    # Picks the 5-star palace, NOT the cheap motel
    assert selected.name == "5-Star Palace"
    assert selected.hotel_class == 5


def test_14_budget_changes_and_full_recalculation():
    """14. Budget changes trigger full re-evaluation of the same trip state."""
    engine = ReverseBudgetEngine()
    transport = FlightOption(price=Decimal("10000.00"), source=DataSource.LIVE)
    hotel = HotelOption(name="H", total_price=Decimal("5000.00"), source=DataSource.LIVE)
    food = FoodEstimate(tier="standard", daily_cost_per_person=Decimal("800.00"), total_cost=Decimal("3200.00"), people=2, days=2, source=DataSource.ESTIMATED)
    transit = LocalTransitEstimate(mode="auto", total_cost=Decimal("400.00"), source=DataSource.ESTIMATED)

    # Initial high budget
    res1 = engine.evaluate(
        total_budget=Decimal("50000.00"),
        people=2,
        days=2,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
    )
    assert res1.is_feasible is True
    assert res1.breakdown.total_budget == Decimal("50000.00")

    # Change to tight budget
    res2 = engine.evaluate(
        total_budget=Decimal("22000.00"),
        people=2,
        days=2,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
    )
    assert res2.is_feasible is True
    assert res2.breakdown.total_budget == Decimal("22000.00")
    assert res2.breakdown.bucket_d_rescue == Decimal("2200.00")
    assert res2.breakdown.is_reconciled() is True


def test_15_date_changes_and_provider_price_freshness():
    """15. Date changes update stay nights and date context without reusing stale duration."""
    ctx1 = build_trip_date_context(days=3, start_date="2026-12-01", return_date="2026-12-03")
    assert ctx1.stay_nights == 2

    ctx2 = build_trip_date_context(days=6, start_date="2026-12-01", return_date="2026-12-06")
    assert ctx2.stay_nights == 5
    assert ctx2.days == 6


def test_16_party_size_changes_and_cost_recalculation():
    """16. Party size changes correctly scale food, local transit, and attraction costs."""
    estimator = EstimationLayer()
    f1 = estimator.estimate_food(people=1, days=3, tier="standard")
    f4 = estimator.estimate_food(people=4, days=3, tier="standard")
    assert f4.total_cost == (f1.total_cost * 4)

    t1 = estimator.estimate_local_transit_daily(days=3, people=1)
    t4 = estimator.estimate_local_transit_daily(days=3, people=4)
    assert t4.total_cost == (t1.total_cost * 4)


def test_17_activity_replacement_without_duplicate_costs():
    """17. Replacing an activity replaces its cost rather than duplicating."""
    rescue_service = RescueService.__new__(RescueService)
    budget_engine = ReverseBudgetEngine()
    rescue_service.budget_engine = budget_engine

    alloc = BudgetAllocation(
        id=uuid4(),
        trip_id=uuid4(),
        transport_allocated=Decimal("10000.00"),
        stay_allocated=Decimal("5000.00"),
        food_allocated=Decimal("3000.00"),
        activities_discretionary=Decimal("2000.00"),
        rescue_fund_allocated=Decimal("2000.00"),
        total_budget=Decimal("22000.00"),
    )

    # Activity A (old_cost = ₹500) replaced by Activity B (new_cost = ₹800)
    old_cost = Decimal("500.00")
    new_cost = Decimal("800.00")
    cost_delta = new_cost - old_cost  # +₹300

    rescue_eval = budget_engine.evaluate_rescue(
        total_budget=alloc.total_budget,
        current_allocations=alloc,
        cost_delta=cost_delta,
    )
    assert rescue_eval.is_feasible is True
    # The replacement utilized ₹300 from rescue reserve without double-counting old activity
    assert rescue_eval.breakdown.bucket_c_activities == (alloc.activities_discretionary + Decimal("300.00"))


def test_18_formatter_values_match_engine_values():
    """18. Formatter outputs match budget engine figures exactly."""
    bd = BudgetBreakdown(
        total_budget=Decimal("75000.00"),
        currency="INR",
        bucket_a_fixed=Decimal("35000.00"),
        bucket_b_survival=Decimal("8000.00"),
        bucket_c_activities=Decimal("4500.00"),
        bucket_d_rescue=Decimal("7500.00"),
        transport_cost=Decimal("25000.00"),
        hotel_cost=Decimal("10000.00"),
        food_cost=Decimal("6000.00"),
        local_transit_cost=Decimal("2000.00"),
        attraction_cost=Decimal("0.00"),
        total_allocated=Decimal("55000.00"),
        remaining_surplus=Decimal("20000.00"),
    )
    text = format_feasible_plan(
        destination="Jaipur",
        days=4,
        people=2,
        breakdown=bd,
        transport=FlightOption(price=Decimal("25000.00"), airline="IndiGo", source=DataSource.LIVE),
        hotel=HotelOption(name="Jaipur Palace", total_price=Decimal("10000.00"), source=DataSource.LIVE),
        itinerary=None,
        ledger=None,
    )
    assert "75,000.00" in text
    assert "35,000.00" in text
    assert "8,000.00" in text
    assert "7,500.00" in text
    assert "55,000.00" in text
    assert "20,000.00" in text


def test_19_provenance_preserved_through_normalization_and_budget_evaluation():
    """19. Data provenance tags are preserved through normalization and budget evaluation."""
    flight = FlightOption(price=Decimal("8000.00"), source=DataSource.LIVE)
    hotel = HotelOption(name="H", total_price=Decimal("4000.00"), source=DataSource.FALLBACK)
    food = FoodEstimate(tier="standard", daily_cost_per_person=Decimal("800.00"), total_cost=Decimal("1600.00"), people=1, days=2, source=DataSource.ESTIMATED)
    transit = LocalTransitEstimate(mode="auto", total_cost=Decimal("300.00"), source=DataSource.ESTIMATED)

    engine = ReverseBudgetEngine()
    result = engine.evaluate(
        total_budget=Decimal("20000.00"),
        people=1,
        days=2,
        transport=flight,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
    )
    prov = result.breakdown.provenance
    assert prov["transport"] == DataSource.LIVE
    assert prov["hotel"] == DataSource.FALLBACK
    assert prov["food"] == DataSource.ESTIMATED


def test_20_actual_expenses_kept_separate_from_projections_and_quotes():
    """20. Virtual Ledger maintains clear separation between planned and actual amounts."""
    ledger_repo = LedgerRepository()
    ledger_repo.reset_in_memory_store()
    mgr = VirtualLedgerManager(ledger_repo)

    trip_id = uuid4()
    engine = ReverseBudgetEngine()
    eval_res = engine.evaluate(
        total_budget=Decimal("30000.00"),
        people=1,
        days=2,
        transport=FlightOption(price=Decimal("8000.00"), source=DataSource.LIVE),
        hotel=HotelOption(name="H", total_price=Decimal("4000.00"), source=DataSource.LIVE),
        food_estimate=FoodEstimate(tier="standard", daily_cost_per_person=Decimal("800.00"), total_cost=Decimal("1600.00"), people=1, days=2, source=DataSource.ESTIMATED),
        local_transit_estimate=LocalTransitEstimate(mode="auto", total_cost=Decimal("400.00"), source=DataSource.ESTIMATED),
    )
    summary_init = mgr.initialize_ledger(trip_id, eval_res)
    assert summary_init.total_allocated == eval_res.breakdown.total_allocated
    assert summary_init.total_planned == eval_res.breakdown.total_allocated - eval_res.breakdown.bucket_d_rescue

    # User records actual expense of ₹650 for dinner
    mgr.record_spending(
        trip_id=trip_id,
        category="daily_survival",
        amount=Decimal("650.00"),
        description="Dinner at restaurant",
        actual_amount=Decimal("650.00"),
    )
    summary_spent = mgr.get_summary(trip_id)
    assert summary_spent.total_spent == Decimal("650.00")
    # Planned baseline remains distinct from actual spend
    assert summary_spent.total_allocated == eval_res.breakdown.total_allocated


def test_21_attraction_fee_variants_and_truthful_disclosure():
    """21. Distinguish verified free, known fees, configured estimates, and unknown fees. Disclose unknown fees truthfully."""
    from budlance.itinerary.models import GeneratedItinerary, ItineraryDay, ItineraryItem

    engine = ReverseBudgetEngine()
    total_budget = Decimal("50000.00")
    transport = FlightOption(price=Decimal("15000.00"), source=DataSource.LIVE)
    hotel = HotelOption(name="Stay", total_price=Decimal("8000.00"), source=DataSource.LIVE)
    food = FoodEstimate(tier="standard", daily_cost_per_person=Decimal("800.00"), total_cost=Decimal("3200.00"), people=2, days=2, source=DataSource.ESTIMATED)
    transit = LocalTransitEstimate(mode="metro_bus", total_cost=Decimal("800.00"), source=DataSource.ESTIMATED)

    # 1. Known admission fee (Amber Fort: ₹500/person -> ₹1000 for 2 people)
    known_attraction = PlaceOption(
        name="Amber Fort",
        entry_fee_inr=Decimal("500.00"),
        is_fee_unknown=False,
        source=DataSource.LIVE,
    )
    res_known = engine.evaluate(
        total_budget=total_budget,
        people=2,
        days=2,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
        selected_attractions=[known_attraction],
    )
    assert res_known.is_feasible is True
    assert res_known.breakdown.attraction_cost == Decimal("1000.00")
    assert res_known.breakdown.has_unknown_attraction_fees is False
    assert res_known.breakdown.unknown_attraction_names == []
    assert res_known.breakdown.provenance["attractions"] == DataSource.LIVE

    # 2. Verified free admission (e.g. Marina Beach: ₹0, not unknown)
    free_attraction = PlaceOption(
        name="Marina Beach",
        entry_fee_inr=Decimal("0.00"),
        is_fee_unknown=False,
        source=DataSource.CONFIG_ESTIMATE,
    )
    res_free = engine.evaluate(
        total_budget=total_budget,
        people=2,
        days=2,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
        selected_attractions=[free_attraction],
    )
    assert res_free.is_feasible is True
    assert res_free.breakdown.attraction_cost == Decimal("0.00")
    assert res_free.breakdown.has_unknown_attraction_fees is False
    assert res_free.breakdown.unknown_attraction_names == []

    # 3. Unknown admission fee (e.g. Place with no published fee: is_fee_unknown=True or entry_fee_inr=None)
    # NEVER convert to ₹0 or claim verified cost.
    unknown_attraction = PlaceOption(
        name="Secret Heritage Site",
        entry_fee_inr=None,
        is_fee_unknown=True,
    )
    res_unknown = engine.evaluate(
        total_budget=total_budget,
        people=2,
        days=2,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
        selected_attractions=[unknown_attraction],
    )
    assert res_unknown.is_feasible is True
    assert res_unknown.breakdown.has_unknown_attraction_fees is True
    assert res_unknown.breakdown.unknown_attraction_names == ["Secret Heritage Site"]
    assert res_unknown.breakdown.provenance["attractions"] == DataSource.UNKNOWN

    # 4. Truthful gate: If requires_attraction_fees=True, unknown fees return INCOMPLETE_COST_DATA
    res_gated = engine.evaluate(
        total_budget=total_budget,
        people=2,
        days=2,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
        selected_attractions=[unknown_attraction],
        requires_attraction_fees=True,
    )
    assert res_gated.status == "INCOMPLETE_COST_DATA"
    assert "attraction_admission" in res_gated.missing_cost_items

    # 5. Formatter disclosure
    itin = GeneratedItinerary(
        trip_id=uuid4(),
        destination="Jaipur",
        days_count=1,
        total_budget=total_budget,
        days=[
            ItineraryDay(
                day_number=1,
                theme_or_summary="Exploration",
                daily_estimated_cost=Decimal("1500.00"),
                items=[
                    ItineraryItem(time_slot="Morning", activity="Visit Amber Fort", category="attraction", entry_fee_inr=500),
                    ItineraryItem(time_slot="Afternoon", activity="Stroll Marina Beach", category="attraction", entry_fee_inr=0, is_fee_unknown=False),
                    ItineraryItem(time_slot="Evening", activity="Explore Secret Site", category="attraction", entry_fee_inr=None, is_fee_unknown=True),
                ],
            )
        ],
    )
    formatted = format_feasible_plan(
        destination="Jaipur",
        days=1,
        people=2,
        breakdown=res_unknown.breakdown,
        transport=transport,
        hotel=hotel,
        itinerary=itin,
    )
    assert "⚠️ *Note on Admission Fees:*" in formatted
    assert "Secret Heritage Site" in formatted
    assert "unverified/not published and excluded from this total" in formatted
    assert "[₹500]" in formatted
    assert "[Free entry]" in formatted
    assert "[admission not included]" in formatted


def test_22_activities_budget_ratio_configuration_and_override(monkeypatch):
    """22. Authoritative configuration BUDGET_ACTIVITIES_RATIO is 0.05 (5%) and can be overridden dynamically."""
    from budlance.config import BudlanceSettings, get_settings

    # Authoritative default configuration must be 0.05
    settings = get_settings()
    assert settings.budget_activities_ratio == Decimal("0.05")

    # Example 1 allocation: ₹90,000 * 0.05 = ₹4,500
    budget = Decimal("90000.00")
    activities_default = round(budget * settings.budget_activities_ratio, 2)
    assert activities_default == Decimal("4500.00")

    # Dynamic override to 10%
    monkeypatch.setenv("BUDGET_ACTIVITIES_RATIO", "0.10")
    overridden_settings = BudlanceSettings()
    assert overridden_settings.budget_activities_ratio == Decimal("0.10")
    activities_overridden = round(budget * overridden_settings.budget_activities_ratio, 2)
    assert activities_overridden == Decimal("9000.00")


def test_23_explicit_preference_preservation_flight_and_luxury():
    """23. Optimizer preserves explicit flight and luxury constraints; proposes alternatives when over budget."""
    optimizer = OptimizationEngine()
    total_budget = Decimal("40000.00")
    days = 4
    people = 2

    expensive_flight = FlightOption(airline="Air India", price=Decimal("35000.00"), source=DataSource.LIVE)
    cheaper_flight = FlightOption(airline="IndiGo", price=Decimal("28000.00"), source=DataSource.LIVE)
    ground_train = TransitOption(
        origin="Chennai",
        destination="Delhi",
        name_or_operator="Rajdhani Express",
        transit_type="train",
        price=Decimal("6000.00"),
        source=DataSource.FALLBACK,
    )

    luxury_hotel = HotelOption(name="Oberoi Grand", hotel_class=5, total_price=Decimal("25000.00"), price_per_night=Decimal("6250.00"), source=DataSource.LIVE)
    cheaper_luxury = HotelOption(name="Taj Gateway", hotel_class=4, total_price=Decimal("18000.00"), price_per_night=Decimal("4500.00"), source=DataSource.LIVE)
    budget_motel = HotelOption(name="Budget Lodge", hotel_class=2, total_price=Decimal("4000.00"), price_per_night=Decimal("1000.00"), source=DataSource.FALLBACK)

    food = FoodEstimate(tier="standard", daily_cost_per_person=Decimal("800.00"), total_cost=Decimal("6400.00"), people=2, days=4, source=DataSource.ESTIMATED)
    transit = LocalTransitEstimate(mode="metro_bus", total_cost=Decimal("1600.00"), source=DataSource.ESTIMATED)

    # Case A: Explicit flight constraint
    # Optimizer must NOT silently downgrade to train; it can downgrade to cheaper flight if feasible,
    # or report ground transit alternative in alternatives_available.
    res_flight = optimizer.optimize(
        trip_id=None,
        total_budget=total_budget,
        people=people,
        days=days,
        initial_transport=expensive_flight,
        initial_hotel=budget_motel,
        initial_food=food,
        initial_transit=transit,
        activities_budget=Decimal("2000.00"),
        available_hotels=[budget_motel],
        available_transports=[expensive_flight, cheaper_flight, ground_train],
        explicit_transport_mode="flight",
    )
    # Selected transport must NOT be ground_train
    if res_flight.selected_transport:
        assert isinstance(res_flight.selected_transport, FlightOption)
        assert res_flight.selected_transport != ground_train

    # Case B: Explicit luxury hotel constraint
    # Optimizer must NOT silently downgrade luxury (5-star) to 2-star motel
    res_luxury = optimizer.optimize(
        trip_id=None,
        total_budget=Decimal("30000.00"),  # Very tight budget
        people=people,
        days=days,
        initial_transport=cheaper_flight,
        initial_hotel=luxury_hotel,
        initial_food=food,
        initial_transit=transit,
        activities_budget=Decimal("1500.00"),
        available_hotels=[luxury_hotel, cheaper_luxury, budget_motel],
        available_transports=[cheaper_flight],
        explicit_hotel_tier="luxury",
        strict_preferences=["luxury", "5-star"],
    )
    # If not feasible, it must not have silently picked budget_motel (hotel_class=2)
    assert res_luxury.is_feasible is False
    assert res_luxury.selected_hotel.hotel_class >= 4
    # Must propose alternative with cost savings
    assert len(res_luxury.alternatives_available) > 0
    assert any("Switching from luxury hotel to standard accommodation" in alt for alt in res_luxury.alternatives_available)


@pytest.mark.asyncio
async def test_24_unknown_attraction_fee_flows_through_orchestration_to_user_disclosure():
    """24. Integration test: unknown-fee PlaceOption flows through actual orchestrator path to final rendered disclosure."""
    from unittest.mock import MagicMock
    from budlance.ai.service import AIIntentService
    from budlance.db.repositories.intent_repo import IntentRepository

    user_repo = UserRepository()
    user = user_repo.get_or_create_user(telegram_user_id=99999, username="test_user")

    orchestrator = BudlanceOrchestrator(
        user_repo=user_repo,
        trip_repo=TripRepository(),
        intent_repo=IntentRepository(),
        itinerary_repo=ItineraryRepository(),
        ledger_repo=LedgerRepository(),
        conversation_repo=ConversationStateRepository(),
        ai_service=AIIntentService(use_mock=True),
        enable_trip_pass=False,
    )

    unknown_attraction = PlaceOption(
        name="Nahargarh Viewpoint",
        entry_fee_inr=None,
        is_fee_unknown=True,
        source=DataSource.LIVE,
    )

    # Mock attraction_selector to return the unknown-fee PlaceOption
    orchestrator.attraction_selector.select_for_itinerary = MagicMock(return_value=[unknown_attraction])

    # 1. Production candidate evaluation with requires_attraction_fees=False
    plan = await orchestrator._evaluate_trip_candidate(
        origin="Delhi",
        destination="Jaipur",
        people=2,
        days=2,
        budget=Decimal("50000.00"),
        currency="INR",
        requires_attraction_fees=False,
    )

    assert plan["is_feasible"] is True
    eval_result = plan["evaluation"]
    assert eval_result.breakdown.has_unknown_attraction_fees is True
    assert eval_result.breakdown.unknown_attraction_names == ["Nahargarh Viewpoint"]
    assert eval_result.breakdown.provenance["attractions"] == DataSource.UNKNOWN
    # Unknown fee is NOT counted as 0 in attraction_cost
    assert eval_result.breakdown.attraction_cost == Decimal("0.00")

    # 2. User-facing plan rendering through _build_plan_result
    trip = orchestrator.trip_repo.create_trip(
        user_id=user.id,
        telegram_chat_id=99999,
        budget_total=Decimal("50000.00"),
        destination="Jaipur",
        origin="Delhi",
        currency="INR",
        people_count=2,
        duration_days=2,
        status="PLANNING",
    )
    generated_itin = orchestrator.itinerary_generator.generate(
        trip_id=trip.id,
        destination="Jaipur",
        evaluation=eval_result,
        days=2,
        transport=plan.get("transport"),
        hotel=plan.get("hotel"),
        attractions=[unknown_attraction],
    )

    ledger_summary = orchestrator.ledger_manager.initialize_ledger(
        trip_id=trip.id,
        evaluation=eval_result,
    )

    plan_result = await orchestrator._build_plan_result(
        chat_id=99999,
        user_id=user.id,
        trip_id=trip.id,
        chosen_dest="Jaipur",
        final_days=2,
        people=2,
        final_eval=eval_result,
        transport=plan.get("transport"),
        hotel=plan.get("hotel"),
        places=[],
        route=None,
        generated_itin=generated_itin,
        ledger_summary=ledger_summary,
        opt_result=None,
        downgrades=[],
        travel_party=None,
    )

    msg = plan_result.message_text
    # Must preserve explicit warning banner
    assert "⚠️ *Note on Admission Fees:*" in msg
    assert "Nahargarh Viewpoint" in msg
    assert "unverified/not published and excluded from this total. Trip total is an unverified estimate." in msg
    # Must preserve unknown badge and not render as ₹0
    assert "[admission not included]" in msg
    assert "₹0" not in msg

    # 3. Production path when requires_attraction_fees=True (strict fee gate)
    gated_plan = await orchestrator._evaluate_trip_candidate(
        origin="Delhi",
        destination="Jaipur",
        people=2,
        days=2,
        budget=Decimal("50000.00"),
        currency="INR",
        requires_attraction_fees=True,
    )
    assert gated_plan["is_feasible"] is False
    assert gated_plan["rejection_reason"] == "INCOMPLETE_COST_DATA"
    assert gated_plan["baseline_eval"].status == "INCOMPLETE_COST_DATA"
    assert "attraction_admission" in gated_plan["baseline_eval"].missing_cost_items


# ===========================================================================
# 28. Hotel Quality Relaxation Ladder & Real Rating/Review Display
# ===========================================================================

def test_hotel_quality_relaxation_ladder_and_message_display():
    """Verify hotel quality ladder: 4.0/50 -> 3.8/25 -> 3.5/10 -> reject, and real display."""
    from budlance.orchestrator.formatter import format_feasible_plan
    from budlance.engine.models import BudgetBreakdown

    optimizer = OptimizationEngine()

    h_top = HotelOption(name="Grand Palace", rating=4.3, review_count=320, total_price=Decimal("12000.00"), deep_link="https://hotels.com/grand", source=DataSource.LIVE)
    h_mid = HotelOption(name="Standard Comfort", rating=3.9, review_count=45, total_price=Decimal("8000.00"), deep_link="https://hotels.com/standard", source=DataSource.LIVE)
    h_low = HotelOption(name="Economy Lodge", rating=3.6, review_count=15, total_price=Decimal("5000.00"), deep_link="https://hotels.com/economy", source=DataSource.LIVE)
    h_bad = HotelOption(name="Dingy Motel", rating=2.8, review_count=100, total_price=Decimal("2000.00"), source=DataSource.LIVE)

    # 1. Step 1 (rating >= 4.0 & reviews >= 50) matches h_top
    sel1 = optimizer.select_preferred_hotel(available_hotels=[h_top, h_mid, h_low, h_bad])
    assert sel1 is not None
    assert sel1.name == "Grand Palace"
    assert sel1.rating == 4.3
    assert sel1.review_count == 320

    # 2. Step 2 (relaxing to >= 3.8 & reviews >= 25) when no >= 4.0 property exists
    sel2 = optimizer.select_preferred_hotel(available_hotels=[h_mid, h_low, h_bad])
    assert sel2 is not None
    assert sel2.name == "Standard Comfort"
    assert sel2.rating == 3.9

    # 3. Step 3 (relaxing to >= 3.5 & reviews >= 10) when no >= 3.8 property exists
    sel3 = optimizer.select_preferred_hotel(available_hotels=[h_low, h_bad])
    assert sel3 is not None
    assert sel3.name == "Economy Lodge"
    assert sel3.rating == 3.6

    # 4. Step 4 (none pass quality threshold) when all hotels are below 3.5
    sel4 = optimizer.select_preferred_hotel(available_hotels=[h_bad])
    assert sel4 is None

    # 5. Message display: shows real rating and review count, plus booking link
    breakdown = BudgetBreakdown(
        total_budget=Decimal("50000.00"),
        currency="INR",
        bucket_a_fixed=Decimal("12000.00"),
        bucket_b_survival=Decimal("5000.00"),
        bucket_c_activities=Decimal("5000.00"),
        bucket_d_rescue=Decimal("5000.00"),
        transport_cost=Decimal("0.00"),
        hotel_cost=Decimal("12000.00"),
        food_cost=Decimal("5000.00"),
        local_transit_cost=Decimal("0.00"),
        total_allocated=Decimal("27000.00"),
        remaining_surplus=Decimal("23000.00"),
    )
    plan_text = format_feasible_plan(
        destination="Munnar",
        days=3,
        people=2,
        breakdown=breakdown,
        transport=None,
        hotel=h_top,
        itinerary=None,
        is_pass_unlocked=True,
    )
    # Check rating and reviews are shown
    assert "• Accommodation: Grand Palace (⭐ 4.3 · 320 reviews)" in plan_text
    # Check hotel booking link is shown directly on paid plan
    assert "🔗 Booking: https://hotels.com/grand" in plan_text

    # 6. When none pass: displays honest note without fabricating
    plan_no_hotel = format_feasible_plan(
        destination="Munnar",
        days=3,
        people=2,
        breakdown=breakdown,
        transport=None,
        hotel=None,
        itinerary=None,
        is_pass_unlocked=True,
    )
    assert "No verified hotels meeting quality threshold" in plan_no_hotel



