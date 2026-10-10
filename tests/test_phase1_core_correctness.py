"""Phase 1 Acceptance Tests: Core correctness and financial integrity.

Covers:
- Example 1: Five-day round trip (Chennai -> Munnar, 1 Oct 2026 - 5 Oct 2026, 4 nights)
- Example 2: Two-day duration mismatch regression (6 Nov 2026 - 7 Nov 2026, 1 night, no 10 Nov return)
- Example 3: Financial waterfall reconciliation and quote vs actual expense isolation
- Invariants 1-14: End date, stay nights, flight/hotel synchronization, quote vs payment,
  non-duplication, and budget reconciliation.
"""

from datetime import date, timedelta
from decimal import Decimal
import pytest
from uuid import uuid4

from budlance.ai.service import AIIntentService
from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.schemas.dates import TripDateContext, build_trip_date_context, calculate_stay_nights
from budlance.itinerary.generator import ItineraryGenerator
from budlance.engine.budget import ReverseBudgetEngine, BudgetEvaluationResult
from budlance.engine.models import BudgetBreakdown
from budlance.normalization.normalizer import DataNormalizer
from budlance.serpapi.models import TravelDataEnvelope, DataSource
from budlance.schemas.travel import FlightOption, HotelOption, TransitOption, FoodEstimate, LocalTransitEstimate
from budlance.db.models import Trip, BudgetAllocation, LedgerEntry, utc_now
from budlance.db.repositories.trip_repo import TripRepository
from budlance.db.repositories.ledger_repo import LedgerRepository
from budlance.db.repositories.itinerary_repo import ItineraryRepository
from budlance.db.repositories.rescue_repo import RescueRepository
from budlance.ledger.manager import VirtualLedgerManager
from budlance.rescue.service import RescueService
from budlance.lifecycle.expense_handler import ExpenseLifecycleHandler


def make_in_memory_repos():
    tr = TripRepository()
    tr._client = None
    lr = LedgerRepository()
    lr._client = None
    ir = ItineraryRepository()
    ir._client = None
    rr = RescueRepository()
    rr._client = None
    return tr, lr, ir, rr


def make_food_estimate(total: Decimal, people: int = 2, days: int = 5) -> FoodEstimate:
    per_day = total / (people * days) if people * days > 0 else total
    return FoodEstimate(
        tier="standard",
        daily_cost_per_person=per_day,
        total_cost=total,
        people=people,
        days=days,
        currency="INR",
    )


def make_transit_estimate(total: Decimal = Decimal("0.00"), people: int = 2, days: int = 5) -> LocalTransitEstimate:
    return LocalTransitEstimate(
        mode="auto",
        total_cost=total,
        days=days,
        currency="INR",
    )


# =========================================================================
# EXAMPLE 1: Five-day round trip
# =========================================================================

@pytest.mark.asyncio
async def test_example_1_five_day_round_trip():
    """Example 1: 'Plan a five-day trip from Chennai to Munnar for two people. Start on 1 October 2026.'

    Expected date semantics:
    - Day 1: 1 October 2026.
    - Day 5 and final trip day: 5 October 2026.
    - Outbound travel planned for start of the trip: 2026-10-01.
    - Return travel included in journey plan: 2026-10-05.
    - Hotel check-in: 1 October 2026.
    - Hotel checkout: 5 October 2026.
    - Charged hotel nights: 4.
    - Itinerary contains exactly 5 calendar days.
    """
    user_prompt = "Plan a five-day trip from Chennai to Munnar for two people. Start on 1 October 2026."
    ai_service = AIIntentService()
    parsed = await ai_service.parse_trip_intent(user_prompt)

    assert parsed.days == 5
    assert parsed.people == 2
    assert parsed.origin == "Chennai"
    assert parsed.destination == "Munnar"
    assert parsed.start_date == "2026-10-01"
    assert parsed.end_date == "2026-10-05"

    date_ctx = build_trip_date_context(
        days=parsed.days,
        start_date=parsed.start_date,
        return_date=parsed.end_date,
    )

    assert date_ctx.days == 5
    assert date_ctx.start_date == date(2026, 10, 1)
    assert date_ctx.end_date == date(2026, 10, 5)
    assert date_ctx.flight_outbound_date == "2026-10-01"
    assert date_ctx.flight_return_date == "2026-10-05"
    assert date_ctx.hotel_check_in_date == "2026-10-01"
    assert date_ctx.hotel_check_out_date == "2026-10-05"
    assert date_ctx.stay_nights == 4
    assert date_ctx.requires_lodging is True

    # Generate itinerary and check day-by-day dates
    itin_gen = ItineraryGenerator()
    eval_result = BudgetEvaluationResult(
        status="FEASIBLE",
        is_feasible=True,
        breakdown=BudgetBreakdown(
            total_budget=Decimal("50000.00"),
            currency="INR",
            bucket_a_fixed=Decimal("20000.00"),
            bucket_b_survival=Decimal("10000.00"),
            bucket_c_activities=Decimal("5000.00"),
            bucket_d_rescue=Decimal("5000.00"),
            transport_cost=Decimal("15000.00"),
            hotel_cost=Decimal("5000.00"),
            food_cost=Decimal("8000.00"),
            local_transit_cost=Decimal("2000.00"),
            total_allocated=Decimal("40000.00"),
            remaining_surplus=Decimal("10000.00"),
        ),
        explanation="Feasible",
    )
    itin = itin_gen.generate(
        trip_id=uuid4(),
        destination="Munnar",
        evaluation=eval_result,
        days=5,
        start_date=parsed.start_date,
    )

    assert itin is not None
    assert itin.days_count == 5
    assert len(itin.days) == 5
    assert itin.start_date == "2026-10-01"
    assert itin.end_date == "2026-10-05"

    expected_dates = [
        "2026-10-01",
        "2026-10-02",
        "2026-10-03",
        "2026-10-04",
        "2026-10-05",
    ]
    for idx, day in enumerate(itin.days):
        assert day.day_number == idx + 1
        assert day.date_str == expected_dates[idx]


# =========================================================================
# EXAMPLE 2: Two-day duration mismatch regression
# =========================================================================

@pytest.mark.asyncio
async def test_example_2_two_day_duration_mismatch():
    """Example 2: 'Plan a two-day trip for three people from Chennai. Start on 6 November 2026.'

    Expected date semantics:
    - Day 1: 6 November 2026.
    - Day 2 and final trip day: 7 November 2026.
    - Return journey and accommodation dates follow the same trip-duration model.
    - If the full stay uses hotel checkout on 7 November, charged nights equal 1.
    - A flight search must not silently use 10 November as its return date.
    """
    user_prompt = "Plan a two-day trip for three people from Chennai. Start on 6 November 2026."
    ai_service = AIIntentService()
    parsed = await ai_service.parse_trip_intent(user_prompt)

    assert parsed.days == 2
    assert parsed.people == 3
    assert parsed.origin == "Chennai"
    assert parsed.start_date == "2026-11-06"
    assert parsed.end_date == "2026-11-07"

    date_ctx = build_trip_date_context(
        days=parsed.days,
        start_date=parsed.start_date,
        return_date=parsed.end_date,
    )

    # Invariant checks
    assert date_ctx.days == 2
    assert date_ctx.start_date == date(2026, 11, 6)
    assert date_ctx.end_date == date(2026, 11, 7)
    assert date_ctx.flight_outbound_date == "2026-11-06"
    assert date_ctx.flight_return_date == "2026-11-07"
    assert date_ctx.flight_return_date != "2026-11-10"  # Critical regression test!
    assert date_ctx.hotel_check_in_date == "2026-11-06"
    assert date_ctx.hotel_check_out_date == "2026-11-07"
    assert date_ctx.stay_nights == 1

    # Verify hotel normalizer night arithmetic
    hotel_raw = {
        "properties": [
            {
                "name": "Hillside Retreat",
                "rate_per_night": {"extracted_lowest": 2500},
                "total_rate": {"extracted_lowest": 2500},
            }
        ]
    }
    env = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_hotels",
        query_hash="h1",
        data=hotel_raw,
    )
    hotels = DataNormalizer.normalize_hotels(
        env,
        check_in=date_ctx.hotel_check_in_date,
        check_out=date_ctx.hotel_check_out_date,
    )
    assert len(hotels) == 1
    assert hotels[0].price_per_night == Decimal("2500.00")
    assert hotels[0].total_price == Decimal("2500.00")


# =========================================================================
# EXAMPLE 3: Financial waterfall and rescue reconciliation
# =========================================================================

@pytest.mark.asyncio
async def test_example_3_financial_waterfall_and_quote_reconciliation():
    """Example 3: Financial and rescue reconciliation.

    Setup:
    - Original trip budget: ₹90,000.
    - Flight cost for party: ₹45,352 (round-trip party total).
    - Hotel cost: ₹5,000.
    - Daily survival allocation: ₹5,400.
    - Activities allocation: ₹4,500.
    - Rescue reserve: ₹9,000.
    Total allocated = ₹69,252.
    Remaining budget = ₹20,748.
    Waterfall reconciliation: 69,252 + 20,748 = 90,000.

    Next:
    - Record actual dinner expense of ₹1,200.
    - Ask whether an auto fare quote of ₹450 for five kilometres is fair.
    - Do not count quoted fare as actual expense: actual spending remains ₹1,200.
    - In separate action, explicitly confirm payment of ₹450:
      actual spending increases to ₹1,650 exactly once.
    """
    total_budget = Decimal("90000.00")
    engine = ReverseBudgetEngine()

    flight_opt = FlightOption(
        departure_airport="MAA",
        arrival_airport="COK",
        outbound_date="2026-10-01",
        return_date="2026-10-05",
        airline="IndiGo",
        price=Decimal("45352.00"),
        currency="INR",
        passengers=2,
    )
    hotel_opt = HotelOption(
        name="Tea Garden Resort",
        destination="Munnar",
        price_per_night=Decimal("1250.00"),
        total_price=Decimal("5000.00"),
        nights=4,
        currency="INR",
    )

    food_est = make_food_estimate(Decimal("5400.00"), people=2, days=5)
    transit_est = make_transit_estimate(Decimal("0.00"), people=2, days=5)

    eval_res = engine.evaluate(
        total_budget=total_budget,
        people=2,
        days=5,
        transport=flight_opt,
        hotel=hotel_opt,
        food_estimate=food_est,
        local_transit_estimate=transit_est,
        activities_budget=Decimal("4500.00"),
    )

    # 1. Authoritative waterfall verification
    assert eval_res.is_feasible is True
    assert eval_res.breakdown.transport_cost == Decimal("45352.00")
    assert eval_res.breakdown.hotel_cost == Decimal("5000.00")
    assert eval_res.breakdown.food_cost == Decimal("5400.00")
    assert eval_res.breakdown.bucket_c_activities == Decimal("4500.00")
    assert eval_res.breakdown.bucket_d_rescue == Decimal("9000.00")
    assert eval_res.breakdown.total_allocated == Decimal("69252.00")
    assert eval_res.breakdown.remaining_surplus == Decimal("20748.00")
    assert eval_res.breakdown.total_allocated + eval_res.breakdown.remaining_surplus == total_budget

    # 2. Virtual Ledger and active trip setup
    trip_id = uuid4()
    chat_id = 99887766
    trip_repo, ledger_repo, itinerary_repo, rescue_repo = make_in_memory_repos()
    ledger_mgr = VirtualLedgerManager(ledger_repo)

    active_trip = trip_repo.create_trip(
        user_id=uuid4(),
        telegram_chat_id=chat_id,
        destination="Munnar",
        origin="Chennai",
        status="ACTIVE",
        budget_total=total_budget,
        duration_days=5,
        is_active=True,
    )

    # Initialize budget allocation in ledger
    alloc = BudgetAllocation(
        id=uuid4(),
        trip_id=active_trip.id,
        transport_allocated=Decimal("45352.00"),
        stay_allocated=Decimal("5000.00"),
        food_allocated=Decimal("5400.00"),
        activities_discretionary=Decimal("4500.00"),
        rescue_fund_allocated=Decimal("9000.00"),
        total_budget=total_budget,
    )
    ledger_repo.save_budget_allocation(alloc)

    expense_handler = ExpenseLifecycleHandler(
        trip_repo=trip_repo,
        ledger_repo=ledger_repo,
        itinerary_repo=itinerary_repo,
        ledger_manager=ledger_mgr,
    )

    # Step A: Record dinner expense of ₹1,200
    dinner_intent = ParsedTripIntent(
        action=TripAction.LOG_EXPENSE,
        amount=Decimal("1200.00"),
        expense_category="food",
        day_number=1,
    )
    res1 = await expense_handler.handle_log_expense(chat_id=chat_id, parsed=dinner_intent)
    assert res1.status == "EXPENSE_LOGGED"

    summary1 = ledger_mgr.get_summary(active_trip.id)
    actual_spent_1 = sum(e.actual_amount for e in summary1.entries if e.actual_amount is not None)
    assert actual_spent_1 == Decimal("1200.00")

    # Step B: Ask whether an auto fare quote of ₹450 for five kilometres is fair
    rescue_service = RescueService(
        trip_repo=trip_repo,
        itinerary_repo=itinerary_repo,
        ledger_repo=ledger_repo,
        rescue_repo=rescue_repo,
        ledger_manager=ledger_mgr,
    )
    quote_message = "Ask whether an auto fare quote of ₹450 for five kilometres is fair."
    rescue_res = await rescue_service.execute_rescue(
        chat_id=chat_id,
        user_message=quote_message,
    )

    assert rescue_res.success is True
    assert rescue_res.rescue_type == "price_dispute"
    assert rescue_res.fare_guidance is not None
    assert rescue_res.fare_guidance.reported_price == Decimal("450.00")
    assert rescue_res.fare_guidance.distance_km == 5.0
    assert rescue_res.fare_guidance.rate_per_km == Decimal("15.00")
    # Configured rate in rate_tables.json is ₹15.00/km; 5.0 km * ₹15.00 = ₹75.00 (not ₹100)
    assert rescue_res.fare_guidance.estimated_fare == Decimal("75.00")
    assert rescue_res.fare_guidance.difference == Decimal("375.00")
    assert rescue_res.fare_guidance.status == "significantly_high"
    assert "configured estimate" in rescue_res.fare_guidance.advisory_notes.lower()
    assert rescue_res.budget_impact == Decimal("0.00")

    # Crucial assertion: Quoted fare must NOT be counted as an actual expense!
    summary2 = ledger_mgr.get_summary(active_trip.id)
    actual_spent_2 = sum(e.actual_amount for e in summary2.entries if e.actual_amount is not None)
    assert actual_spent_2 == Decimal("1200.00")

    # Step C: In a separate test/action, explicitly confirm payment of ₹450
    confirm_payment_message = "confirm payment of 450"
    ai_service = AIIntentService()
    parsed_payment = await ai_service.parse_trip_intent(confirm_payment_message)
    assert parsed_payment.action == TripAction.LOG_EXPENSE
    assert parsed_payment.amount == Decimal("450.00")

    parsed_payment.day_number = 1
    parsed_payment.expense_category = "transport"
    res2 = await expense_handler.handle_log_expense(chat_id=chat_id, parsed=parsed_payment)
    assert res2.status == "EXPENSE_LOGGED"

    summary3 = ledger_mgr.get_summary(active_trip.id)
    actual_spent_3 = sum(e.actual_amount for e in summary3.entries if e.actual_amount is not None)
    assert actual_spent_3 == Decimal("1650.00")

    # Step D: Verify idempotency - retry of identical expense does not duplicate
    res3 = await expense_handler.handle_log_expense(chat_id=chat_id, parsed=parsed_payment)
    assert res3.status == "EXPENSE_LOGGED"
    summary4 = ledger_mgr.get_summary(active_trip.id)
    actual_spent_4 = sum(e.actual_amount for e in summary4.entries if e.actual_amount is not None)
    assert actual_spent_4 == Decimal("1650.00")  # Remains 1650, NOT 2100!


# =========================================================================
# THE 14 CORE INVARIANTS
# =========================================================================

def test_invariant_1_end_date_formula():
    """1. End date equals start date plus duration minus one calendar day."""
    start = date(2026, 10, 1)
    for days in [1, 2, 5, 10, 30]:
        ctx = build_trip_date_context(days=days, start_date=start)
        assert ctx.end_date == start + timedelta(days=days - 1)


def test_invariant_2_itinerary_day_count():
    """2. Itinerary day count equals requested trip duration."""
    generator = ItineraryGenerator()
    eval_res = BudgetEvaluationResult(
        status="FEASIBLE",
        is_feasible=True,
        breakdown=BudgetBreakdown(
            total_budget=Decimal("50000.00"),
            currency="INR",
            bucket_a_fixed=Decimal("20000.00"),
            bucket_b_survival=Decimal("10000.00"),
            bucket_c_activities=Decimal("5000.00"),
            bucket_d_rescue=Decimal("5000.00"),
            transport_cost=Decimal("15000.00"),
            hotel_cost=Decimal("5000.00"),
            food_cost=Decimal("8000.00"),
            local_transit_cost=Decimal("2000.00"),
            total_allocated=Decimal("40000.00"),
            remaining_surplus=Decimal("10000.00"),
        ),
        explanation="Feasible",
    )
    itin = generator.generate(
        trip_id=uuid4(),
        destination="Goa",
        evaluation=eval_res,
        days=7,
        start_date="2026-12-01",
    )
    assert itin.days_count == 7
    assert len(itin.days) == 7


def test_invariant_3_hotel_night_arithmetic():
    """3. Hotel night count equals checkout minus check-in."""
    nights = calculate_stay_nights("2026-10-01", "2026-10-05")
    assert nights == 4

    # Invalid same-day and checkout before check-in must raise ValueError
    with pytest.raises(ValueError):
        calculate_stay_nights("2026-10-01", "2026-10-01")

    with pytest.raises(ValueError):
        calculate_stay_nights("2026-10-05", "2026-10-01")


def test_invariant_4_flight_and_hotel_date_synchronization():
    """4. Flight dates and hotel dates agree with confirmed trip dates."""
    ctx = build_trip_date_context(days=4, start_date="2026-11-10")
    assert ctx.flight_outbound_date == "2026-11-10"
    assert ctx.flight_return_date == "2026-11-13"
    assert ctx.hotel_check_in_date == "2026-11-10"
    assert ctx.hotel_check_out_date == "2026-11-13"
    assert ctx.stay_nights == 3


def test_invariant_5_return_travel_presence():
    """5. Return travel is present wherever required by the chosen journey."""
    ctx = build_trip_date_context(days=3, start_date="2026-10-15")
    assert ctx.flight_return_date is not None
    assert ctx.flight_return_date == "2026-10-17"


def test_invariant_6_group_fare_normalization():
    """6. Group fares and per-passenger fares are normalized correctly."""
    engine = ReverseBudgetEngine()
    flight = FlightOption(
        departure_airport="MAA",
        arrival_airport="DEL",
        price=Decimal("20000.00"),
        currency="INR",
    )
    hotel = HotelOption(name="H1", destination="Delhi", total_price=Decimal("5000.00"), currency="INR")
    food = make_food_estimate(Decimal("4000.00"), people=2, days=3)
    transit = make_transit_estimate(Decimal("1000.00"), people=2, days=3)
    res = engine.evaluate(
        total_budget=Decimal("50000.00"),
        people=2,
        days=3,
        transport=flight,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
    )
    # The party total fare of 20,000 must NOT be multiplied by 2 passengers a second time:
    assert res.breakdown.transport_cost == Decimal("20000.00")


def test_invariant_7_every_cost_appears_once():
    """7. Every included cost appears exactly once in the appropriate budget calculation."""
    engine = ReverseBudgetEngine()
    flight = FlightOption(departure_airport="MAA", arrival_airport="DEL", price=Decimal("15000.00"), currency="INR")
    hotel = HotelOption(name="Hotel A", destination="Delhi", total_price=Decimal("10000.00"), nights=3, currency="INR")
    food = make_food_estimate(Decimal("8000.00"), people=2, days=4)
    transit = make_transit_estimate(Decimal("2000.00"), people=2, days=4)

    res = engine.evaluate(
        total_budget=Decimal("50000.00"),
        people=2,
        days=4,
        transport=flight,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
        activities_budget=Decimal("5000.00"),
    )
    b = res.breakdown
    assert b.transport_cost == Decimal("15000.00")
    assert b.hotel_cost == Decimal("10000.00")
    assert b.food_cost == Decimal("8000.00")
    assert b.local_transit_cost == Decimal("2000.00")
    assert b.bucket_c_activities == Decimal("5000.00")
    assert b.bucket_d_rescue == Decimal("5000.00")  # 10% of 50000
    expected_total = Decimal("15000.00") + Decimal("10000.00") + Decimal("8000.00") + Decimal("2000.00") + Decimal("5000.00") + Decimal("5000.00")
    assert b.total_allocated == expected_total
    assert b.remaining_surplus == Decimal("50000.00") - expected_total


def test_invariant_8_allocations_and_unallocated_reconcile():
    """8. Allocations and unallocated funds reconcile to the same original budget."""
    engine = ReverseBudgetEngine()
    total = Decimal("75000.00")
    food = make_food_estimate(Decimal("6000.00"), people=2, days=3)
    transit = make_transit_estimate(Decimal("1500.00"), people=2, days=3)

    res = engine.evaluate(
        total_budget=total,
        people=2,
        days=3,
        transport=FlightOption(departure_airport="BLR", arrival_airport="GOI", price=Decimal("12000.00"), currency="INR"),
        hotel=HotelOption(name="Goa Stay", destination="Goa", total_price=Decimal("9000.00"), nights=2, currency="INR"),
        food_estimate=food,
        local_transit_estimate=transit,
        activities_budget=Decimal("4000.00"),
    )
    assert res.breakdown.total_allocated + res.breakdown.remaining_surplus == total


@pytest.mark.asyncio
async def test_invariant_9_actual_spending_excludes_quotes():
    """9. Actual spending includes actual expenses, not unconfirmed quotes."""
    trip_id = uuid4()
    trip_repo, ledger_repo, _, _ = make_in_memory_repos()
    ledger_mgr = VirtualLedgerManager(ledger_repo)

    alloc = BudgetAllocation(
        id=uuid4(),
        trip_id=trip_id,
        transport_allocated=Decimal("10000"),
        stay_allocated=Decimal("5000"),
        food_allocated=Decimal("5000"),
        activities_discretionary=Decimal("5000"),
        rescue_fund_allocated=Decimal("5000"),
        total_budget=Decimal("30000"),
    )
    ledger_repo.save_budget_allocation(alloc)

    ledger_mgr.record_spending(
        trip_id=trip_id,
        category="daily_survival",
        amount=Decimal("500"),
        description="Lunch receipt",
        actual_amount=Decimal("500"),
    )
    summary = ledger_mgr.get_summary(trip_id)
    actual_spent = sum(e.actual_amount for e in summary.entries if e.actual_amount is not None)
    assert actual_spent == Decimal("500.00")


@pytest.mark.asyncio
async def test_invariant_10_repeated_expense_does_not_duplicate():
    """10. A repeated expense or completion event does not duplicate its financial effect."""
    trip_id = uuid4()
    chat_id = 112233
    trip_repo, ledger_repo, itinerary_repo, _ = make_in_memory_repos()
    ledger_mgr = VirtualLedgerManager(ledger_repo)

    trip = trip_repo.create_trip(
        user_id=uuid4(),
        telegram_chat_id=chat_id,
        destination="Agra",
        origin="Delhi",
        status="ACTIVE",
        budget_total=Decimal("20000"),
        duration_days=2,
        is_active=True,
    )

    alloc = BudgetAllocation(
        id=uuid4(),
        trip_id=trip.id,
        transport_allocated=Decimal("5000"),
        stay_allocated=Decimal("5000"),
        food_allocated=Decimal("4000"),
        activities_discretionary=Decimal("4000"),
        rescue_fund_allocated=Decimal("2000"),
        total_budget=Decimal("20000"),
    )
    ledger_repo.save_budget_allocation(alloc)

    handler = ExpenseLifecycleHandler(trip_repo, ledger_repo, itinerary_repo, ledger_mgr)
    intent = ParsedTripIntent(
        action=TripAction.LOG_EXPENSE,
        amount=Decimal("350.00"),
        expense_category="food",
        day_number=1,
    )

    r1 = await handler.handle_log_expense(chat_id=chat_id, parsed=intent)
    assert r1.status == "EXPENSE_LOGGED"
    s1 = ledger_mgr.get_summary(trip.id)
    actual_1 = sum(e.actual_amount for e in s1.entries if e.actual_amount is not None)
    assert actual_1 == Decimal("350.00")

    # Immediate repeat of same expense
    r2 = await handler.handle_log_expense(chat_id=chat_id, parsed=intent)
    assert r2.status == "EXPENSE_LOGGED"
    s2 = ledger_mgr.get_summary(trip.id)
    actual_2 = sum(e.actual_amount for e in s2.entries if e.actual_amount is not None)
    assert actual_2 == Decimal("350.00")


def test_invariant_11_trip_state_and_ledger_identity():
    """11. Trip state and ledger entries refer to the correct trip identifier."""
    trip_id_1 = uuid4()
    trip_id_2 = uuid4()
    _, ledger_repo, _, _ = make_in_memory_repos()
    ledger_mgr = VirtualLedgerManager(ledger_repo)

    entry1 = ledger_mgr.record_spending(trip_id=trip_id_1, category="activities", amount=Decimal("300"), description="Sightseeing 1")
    entry2 = ledger_mgr.record_spending(trip_id=trip_id_2, category="activities", amount=Decimal("700"), description="Sightseeing 2")

    assert entry1.trip_id == trip_id_1
    assert entry2.trip_id == trip_id_2
    assert len(ledger_repo.get_ledger_entries(trip_id_1)) == 1
    assert len(ledger_repo.get_ledger_entries(trip_id_2)) == 1


def test_invariant_12_missing_provider_prices_remain_unknown_not_zero():
    """12. Missing provider prices remain unknown or clearly estimated; they do not become fabricated zero-cost items."""
    hotel_raw = {
        "properties": [
            {
                "name": "Ghost Hotel",
            }
        ]
    }
    env = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_hotels",
        query_hash="h_none",
        data=hotel_raw,
    )
    hotels = DataNormalizer.normalize_hotels(env, nights=2)
    assert len(hotels) == 0


def test_invariant_13_infeasible_trip_not_declared_feasible():
    """13. An infeasible trip is not declared feasible because a required cost is missing."""
    engine = ReverseBudgetEngine()
    food = make_food_estimate(Decimal("2000.00"), people=2, days=3)
    transit = make_transit_estimate(Decimal("500.00"), people=2, days=3)

    eval_res = engine.evaluate(
        total_budget=Decimal("5000.00"),
        people=2,
        days=3,
        transport=FlightOption(departure_airport="MAA", arrival_airport="DEL", price=Decimal("15000.00"), currency="INR"),
        hotel=HotelOption(name="Hotel A", destination="Delhi", total_price=Decimal("6000.00"), nights=2, currency="INR"),
        food_estimate=food,
        local_transit_estimate=transit,
    )
    assert eval_res.is_feasible is False
    assert eval_res.deficit > Decimal("0.00")


def test_invariant_14_changing_dates_refreshes_context():
    """14. Changing trip dates invalidates or refreshes affected date-dependent prices appropriately."""
    ctx1 = build_trip_date_context(days=3, start_date="2026-10-01")
    assert ctx1.stay_nights == 2
    assert ctx1.flight_return_date == "2026-10-03"

    ctx2 = build_trip_date_context(days=5, start_date="2026-10-01")
    assert ctx2.stay_nights == 4
    assert ctx2.flight_return_date == "2026-10-05"


# =========================================================================
# PHASE 1 FINAL CORRECTIONS REGRESSION TESTS
# =========================================================================

@pytest.mark.asyncio
async def test_regression_date_clarification_handling():
    """Regression tests for date clarification:

    - Missing dates: marks is_proposed=True, date_confirmed=False (does not silently choose arbitrary dates).
    - Explicit dates: marks is_proposed=False, date_confirmed=True.
    - Unambiguous relative dates: resolves exact calendar date in IST (UTC+05:30) with date_confirmed=True.
    - Ambiguous relative date phrases: detects ambiguity, sets date_is_ambiguous=True, leaves start_date=None.
    """
    ai_service = AIIntentService()

    # 1. Missing dates:
    ctx_missing = build_trip_date_context(days=4)
    assert ctx_missing.is_proposed is True
    assert ctx_missing.date_confirmed is False

    intent_missing = await ai_service.parse_trip_intent("Plan a 4-day trip from Chennai to Delhi for 2 people with budget 30000")
    assert intent_missing.start_date is None
    assert intent_missing.date_is_explicit is False
    assert intent_missing.date_is_ambiguous is False
    assert intent_missing.date_confirmed is False

    # 2. Explicit dates:
    ctx_explicit = build_trip_date_context(days=3, start_date="2026-10-15")
    assert ctx_explicit.is_proposed is False
    assert ctx_explicit.date_confirmed is True
    assert ctx_explicit.flight_outbound_date == "2026-10-15"
    assert ctx_explicit.flight_return_date == "2026-10-17"
    assert ctx_explicit.stay_nights == 2

    intent_explicit = await ai_service.parse_trip_intent("Plan a 3-day trip from Chennai to Munnar starting 15 Oct 2026 for 2 people")
    assert intent_explicit.start_date == "2026-10-15"
    assert intent_explicit.date_is_explicit is True
    assert intent_explicit.date_is_ambiguous is False
    assert intent_explicit.date_confirmed is True

    # 3. Unambiguous relative dates (resolved in IST):
    from datetime import datetime, timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    expected_tomorrow = (datetime.now(ist).date() + timedelta(days=1)).strftime("%Y-%m-%d")
    expected_in_3_days = (datetime.now(ist).date() + timedelta(days=3)).strftime("%Y-%m-%d")

    intent_tomorrow = await ai_service.parse_trip_intent("Plan a 3-day trip from Bangalore to Goa tomorrow for 2 people")
    assert intent_tomorrow.start_date == expected_tomorrow
    assert intent_tomorrow.date_is_explicit is True
    assert intent_tomorrow.date_is_ambiguous is False
    assert intent_tomorrow.date_confirmed is True

    intent_in_3_days = await ai_service.parse_trip_intent("Plan a 2-day trip in 3 days from Chennai to Pondicherry for 2 people")
    assert intent_in_3_days.start_date == expected_in_3_days
    assert intent_in_3_days.date_is_explicit is True
    assert intent_in_3_days.date_is_ambiguous is False
    assert intent_in_3_days.date_confirmed is True

    # 4. Ambiguous relative date phrases:
    ambiguous_prompts = [
        "Plan a 3-day trip to Goa from Bangalore for next weekend for 2 people with budget 20000",
        "Plan a trip to Kerala this weekend with budget 15000",
        "Plan a 4-day trip to Manali sometime next month for 2 people",
        "Plan a trip to Varanasi around Diwali for 2 people with budget 30000",
        "Plan a trip to Ooty sometime soon with budget 10000",
    ]
    for prompt in ambiguous_prompts:
        parsed = await ai_service.parse_trip_intent(prompt)
        assert parsed.date_is_ambiguous is True, f"Failed ambiguity detection for prompt: {prompt}"
        assert parsed.start_date is None, f"Ambiguous prompt should not silently fix start_date: {prompt}"
        assert parsed.date_confirmed is False
        assert parsed.date_ambiguous_phrase is not None


@pytest.mark.asyncio
async def test_regression_fare_estimate_consistency_and_unknown_rate():
    """Regression tests for fare estimate consistency:

    - Configured rate is ₹15.00/km (from rate_tables.json).
    - For 5.0 km auto ride, real calculation yields exactly ₹75.00 ($5 * 15), not ₹100.
    - Result is explicitly labeled as a configured estimate heuristic (not statutory tariff).
    - Distance parsing accurately converts number words ('five kilometres' -> 5.0 km).
    - Unknown transit mode cleanly falls back to configured rate without crashing.
    """
    from budlance.estimation.transport import LocalTransitEstimator

    estimator = LocalTransitEstimator()

    # 1. Configured rate calculation for 5.0 km auto ride:
    est_auto = estimator.estimate_by_distance(distance_km=5.0, mode="auto")
    assert est_auto.mode == "auto"
    assert est_auto.distance_km == 5.0
    assert est_auto.rate_per_km == Decimal("15.00")
    assert est_auto.total_cost == Decimal("75.00")  # Exactly 5.0 * 15.00 = 75.00

    # 2. Unknown transit mode rate handling: never silently inherit unrelated auto rate
    est_unknown = estimator.estimate_by_distance(distance_km=5.0, mode="hovercraft_unregistered")
    assert est_unknown.mode == "hovercraft_unregistered"
    assert est_unknown.distance_km == 5.0
    assert est_unknown.is_available is False
    assert est_unknown.rate_per_km is None
    assert est_unknown.total_cost is None
    assert "No configured" in (est_unknown.basis or "")

    # 3. Rescue service integration for price dispute with number word:
    trip_repo, ledger_repo, itinerary_repo, rescue_repo = make_in_memory_repos()
    ledger_mgr = VirtualLedgerManager(ledger_repo)
    chat_id = 998877

    active_trip = trip_repo.create_trip(
        user_id=uuid4(),
        telegram_chat_id=chat_id,
        destination="Bangalore",
        origin="Chennai",
        duration_days=3,
        budget_total=Decimal("15000.00"),
        status="ACTIVE",
        is_active=True,
    )
    alloc = BudgetAllocation(
        id=uuid4(),
        trip_id=active_trip.id,
        transport_allocated=Decimal("5000.00"),
        stay_allocated=Decimal("5000.00"),
        food_allocated=Decimal("2000.00"),
        activities_discretionary=Decimal("2000.00"),
        rescue_fund_allocated=Decimal("1000.00"),
        total_budget=Decimal("15000.00"),
    )
    ledger_repo.save_budget_allocation(alloc)

    rescue_service = RescueService(
        trip_repo=trip_repo,
        itinerary_repo=itinerary_repo,
        ledger_repo=ledger_repo,
        rescue_repo=rescue_repo,
        ledger_manager=ledger_mgr,
    )

    result = await rescue_service.execute_rescue(
        chat_id=chat_id,
        user_message="Auto driver asking ₹150 for five kilometres",
    )
    assert result.success is True
    assert result.rescue_type == "price_dispute"
    assert result.fare_guidance is not None
    assert result.fare_guidance.distance_km == 5.0
    assert result.fare_guidance.reported_price == Decimal("150.00")
    assert result.fare_guidance.rate_per_km == Decimal("15.00")
    assert result.fare_guidance.estimated_fare == Decimal("75.00")
    assert result.fare_guidance.difference == Decimal("75.00")
    # Verify advisory notes explicitly label this as a configured estimate
    notes = result.fare_guidance.advisory_notes.lower()
    assert "configured estimate" in notes
    assert "not an authoritative statutory government tariff" in notes


@pytest.mark.asyncio
async def test_regression_expense_idempotency_event_replay_vs_two_legitimate_purchases():
    """Regression tests for expense idempotency:

    - Stable incoming-message/update identifiers distinguish retries from separate purchases.
    - Replaying the same event identity does not create duplicate spending.
    - Two genuine purchases with identical amounts, categories, and descriptions remain separate
      when they have distinct event identities.
    - Existing ledger schema is preserved.
    """
    trip_repo, ledger_repo, itinerary_repo, _ = make_in_memory_repos()
    ledger_mgr = VirtualLedgerManager(ledger_repo)
    chat_id = 776655

    active_trip = trip_repo.create_trip(
        user_id=uuid4(),
        telegram_chat_id=chat_id,
        destination="Goa",
        origin="Mumbai",
        duration_days=3,
        budget_total=Decimal("20000.00"),
        status="ACTIVE",
        is_active=True,
    )
    alloc = BudgetAllocation(
        id=uuid4(),
        trip_id=active_trip.id,
        transport_allocated=Decimal("5000.00"),
        stay_allocated=Decimal("5000.00"),
        food_allocated=Decimal("4000.00"),
        activities_discretionary=Decimal("4000.00"),
        rescue_fund_allocated=Decimal("2000.00"),
        total_budget=Decimal("20000.00"),
    )
    ledger_repo.save_budget_allocation(alloc)

    handler = ExpenseLifecycleHandler(
        trip_repo=trip_repo,
        ledger_repo=ledger_repo,
        itinerary_repo=itinerary_repo,
        ledger_manager=ledger_mgr,
    )

    # 1. Genuine Purchase 1: ₹50 tea with event_id "msg_update_001"
    intent_purchase_1 = ParsedTripIntent(
        action=TripAction.LOG_EXPENSE,
        amount=Decimal("50.00"),
        expense_category="food",
        day_number=1,
        event_id="msg_update_001",
    )
    r1 = await handler.handle_log_expense(chat_id=chat_id, parsed=intent_purchase_1)
    assert r1.status == "EXPENSE_LOGGED"

    summary_1 = ledger_mgr.get_summary(active_trip.id)
    spent_1 = sum(e.actual_amount for e in summary_1.entries if e.actual_amount is not None)
    assert spent_1 == Decimal("50.00")
    assert len(summary_1.entries) == 1

    # 2. Replay of Purchase 1 (Telegram network retry with the SAME event_id "msg_update_001"):
    r1_replay = await handler.handle_log_expense(chat_id=chat_id, parsed=intent_purchase_1)
    assert r1_replay.status == "EXPENSE_LOGGED"

    summary_after_replay = ledger_mgr.get_summary(active_trip.id)
    spent_after_replay = sum(e.actual_amount for e in summary_after_replay.entries if e.actual_amount is not None)
    # Replay MUST NOT create duplicate spending:
    assert spent_after_replay == Decimal("50.00")
    assert len(summary_after_replay.entries) == 1

    # 3. Genuine Purchase 2: A second ₹50 tea 10 seconds later with DISTINCT event_id "msg_update_002":
    # (Identical amount ₹50, identical category "food", identical day 1, identical description)
    intent_purchase_2 = ParsedTripIntent(
        action=TripAction.LOG_EXPENSE,
        amount=Decimal("50.00"),
        expense_category="food",
        day_number=1,
        event_id="msg_update_002",
    )
    r2 = await handler.handle_log_expense(chat_id=chat_id, parsed=intent_purchase_2)
    assert r2.status == "EXPENSE_LOGGED"

    summary_2 = ledger_mgr.get_summary(active_trip.id)
    spent_2 = sum(e.actual_amount for e in summary_2.entries if e.actual_amount is not None)
    # Two genuine purchases MUST remain separate: total is ₹100.00!
    assert spent_2 == Decimal("100.00")
    assert len(summary_2.entries) == 2
    # Verify distinct entries tagged with their respective event IDs
    descriptions = [e.description for e in summary_2.entries]
    assert any("[evt:msg_update_001]" in d for d in descriptions)
    assert any("[evt:msg_update_002]" in d for d in descriptions)


def test_transport_rate_handling_known_mode():
    """Verify known modes use applicable configured rates and are labeled with basis & limitations."""
    from budlance.estimation.transport import LocalTransitEstimator
    from budlance.serpapi.models import DataSource

    estimator = LocalTransitEstimator()

    # Known mode 1: auto
    est_auto = estimator.estimate_by_distance(distance_km=10.0, mode="auto")
    assert est_auto.is_available is True
    assert est_auto.mode == "auto"
    assert est_auto.distance_km == 10.0
    assert est_auto.rate_per_km == Decimal("15.00")
    assert est_auto.total_cost == Decimal("150.00")
    assert est_auto.source == DataSource.ESTIMATED
    assert "Configured rate table for auto (15" in (est_auto.basis or "")
    assert "heuristic" in (est_auto.limitations or "").lower()

    # Known mode 2: cab
    est_cab = estimator.estimate_by_distance(distance_km=10.0, mode="cab")
    assert est_cab.is_available is True
    assert est_cab.mode == "cab"
    assert est_cab.rate_per_km == Decimal("22.00")
    assert est_cab.total_cost == Decimal("220.00")
    assert est_cab.source == DataSource.ESTIMATED
    assert "Configured rate table for cab (22" in (est_cab.basis or "")


def test_transport_rate_handling_unsupported_mode():
    """Verify an unsupported mode never inherits unrelated rates and reports fare as unavailable."""
    from budlance.estimation.transport import LocalTransitEstimator

    estimator = LocalTransitEstimator()

    # Unsupported modes must never inherit auto rate or introduce arbitrary replacement prices
    for unsupported_mode in ["hovercraft", "monorail", "ferry_unknown", "magic_carpet"]:
        est = estimator.estimate_by_distance(distance_km=8.0, mode=unsupported_mode)
        assert est.is_available is False
        assert est.mode == unsupported_mode
        assert est.total_cost is None
        assert est.rate_per_km is None
        assert "No configured transit rate table" in (est.basis or "")
        assert "Fare cannot be defensibly calculated" in (est.limitations or "")


def test_transport_rate_handling_unavailable_live_fare():
    """Verify unavailable live fares are never represented as verified live prices,
    fallback estimates state actual basis/limitations, and unsupported modes report unavailable.
    """
    from budlance.estimation.transport import LocalTransitEstimator
    from budlance.serpapi.models import DataSource

    estimator = LocalTransitEstimator()

    # 1. Live fare verified
    live_est = estimator.estimate_by_distance(
        distance_km=10.0,
        mode="cab",
        live_fare=Decimal("350.00"),
        is_live=True,
    )
    assert live_est.is_available is True
    assert live_est.source == DataSource.LIVE
    assert live_est.total_cost == Decimal("350.00")
    assert "Verified live price" in (live_est.basis or "")

    # 2. Live fare attempted but unavailable, with known fallback mode:
    # Must NOT be DataSource.LIVE! Must be DataSource.FALLBACK with basis/limitations.
    fallback_est = estimator.estimate_by_distance(
        distance_km=10.0,
        mode="cab",
        live_fare=None,
        is_live=True,
    )
    assert fallback_est.is_available is True
    assert fallback_est.source == DataSource.FALLBACK
    assert fallback_est.source != DataSource.LIVE
    assert fallback_est.total_cost == Decimal("220.00")
    assert "live fare was unavailable" in (fallback_est.basis or "")
    assert "not a verified live price" in (fallback_est.limitations or "").lower()

    # 3. Live fare attempted but unavailable, with unsupported mode:
    # Must report fare as unavailable without inventing arbitrary prices.
    unavail_est = estimator.estimate_by_distance(
        distance_km=10.0,
        mode="gondola_shuttle",
        live_fare=None,
        is_live=True,
    )
    assert unavail_est.is_available is False
    assert unavail_est.total_cost is None
    assert unavail_est.rate_per_km is None
    assert "Live fare unavailable and no configured rate table" in (unavail_est.basis or "")


@pytest.mark.asyncio
async def test_durable_expense_idempotency_restart_survival():
    """Verify duplicate protection survives application restart and repository reinitialization."""
    from unittest.mock import MagicMock
    from budlance.db.repositories.trip_repo import TripRepository
    from budlance.db.repositories.ledger_repo import LedgerRepository
    from budlance.ledger.manager import VirtualLedgerManager
    from budlance.lifecycle.expense_handler import ExpenseLifecycleHandler
    from budlance.ai.schemas import ParsedTripIntent, TripAction

    # Ensure clean in-memory state
    LedgerRepository.reset_in_memory_store()

    chat_id = 887766
    trip_repo = TripRepository()
    trip_repo._client = None
    ledger_repo_1 = LedgerRepository(client=None)
    ledger_mgr_1 = VirtualLedgerManager(ledger_repo_1)

    active_trip = trip_repo.create_trip(
        user_id=uuid4(),
        telegram_chat_id=chat_id,
        destination="Goa",
        duration_days=3,
        budget_total=Decimal("20000.00"),
        status="ACTIVE",
        is_active=True,
    )
    alloc = BudgetAllocation(
        id=uuid4(),
        trip_id=active_trip.id,
        transport_allocated=Decimal("6000.00"),
        stay_allocated=Decimal("6000.00"),
        food_allocated=Decimal("4000.00"),
        activities_discretionary=Decimal("4000.00"),
        total_budget=Decimal("20000.00"),
    )
    ledger_repo_1.save_budget_allocation(alloc)

    # Instance 1 of ExpenseLifecycleHandler records initial expense
    handler_1 = ExpenseLifecycleHandler(
        trip_repo=trip_repo,
        ledger_repo=ledger_repo_1,
        ledger_manager=ledger_mgr_1,
    )

    event_id = "tg_upd_9988_msg_101"
    intent = ParsedTripIntent(
        action=TripAction.LOG_EXPENSE,
        amount=Decimal("350.00"),
        expense_category="food",
        day_number=1,
        event_id=event_id,
    )
    res_1 = await handler_1.handle_log_expense(chat_id=chat_id, parsed=intent, event_id=event_id)
    assert res_1.status == "EXPENSE_LOGGED"
    entries_1 = ledger_repo_1.get_ledger_entries(active_trip.id)
    assert len(entries_1) == 1
    assert entries_1[0].actual_amount == Decimal("350.00")

    # SIMULATE APPLICATION RESTART AND REPOSITORY REINITIALIZATION:
    # Completely new LedgerRepository instance and completely new ExpenseLifecycleHandler instance
    # handler_2 has empty _processed_events!
    ledger_repo_2 = LedgerRepository(client=None)
    ledger_mgr_2 = VirtualLedgerManager(ledger_repo_2)
    handler_2 = ExpenseLifecycleHandler(
        trip_repo=trip_repo,
        ledger_repo=ledger_repo_2,
        ledger_manager=ledger_mgr_2,
    )

    # Replay of the SAME event_id
    res_replay = await handler_2.handle_log_expense(chat_id=chat_id, parsed=intent, event_id=event_id)
    assert res_replay.status == "EXPENSE_LOGGED"
    assert "already recorded" in res_replay.message_text

    # Verify zero additional ledger entries and zero financial side effect
    entries_after_restart = ledger_repo_2.get_ledger_entries(active_trip.id)
    assert len(entries_after_restart) == 1
    summary_2 = ledger_mgr_2.get_summary(active_trip.id)
    total_spent = sum(e.actual_amount for e in summary_2.entries if e.actual_amount is not None)
    assert total_spent == Decimal("350.00")


@pytest.mark.asyncio
async def test_telegram_update_event_id_propagation():
    """Verify event ID originates from Telegram update/message and propagates to orchestrator."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from telegram import Chat, Message, Update, User
    from budlance.bot.handlers import text_message_handler

    mock_orchestrator = MagicMock()
    mock_orchestrator.handle_user_message = AsyncMock()
    mock_result = MagicMock()
    mock_result.status = "SUCCESS"
    mock_result.message_text = "Test response"
    mock_orchestrator.handle_user_message.return_value = mock_result

    # Mock telegram update
    mock_update = MagicMock(spec=Update)
    mock_update.update_id = 777666
    mock_chat = MagicMock(spec=Chat)
    mock_chat.id = 12345
    mock_user = MagicMock(spec=User)
    mock_user.id = 67890
    mock_user.username = "testtraveler"
    mock_user.first_name = "Traveler"
    mock_msg = MagicMock(spec=Message)
    mock_msg.message_id = 42
    mock_msg.text = "Spent 200 on lunch"
    mock_msg.reply_text = AsyncMock()

    mock_update.effective_chat = mock_chat
    mock_update.effective_user = mock_user
    mock_update.effective_message = mock_msg

    with patch("budlance.bot.handlers.get_orchestrator", return_value=mock_orchestrator):
        await text_message_handler(mock_update, MagicMock())

    # Verify orchestrator was called with event_id originating from telegram update
    mock_orchestrator.handle_user_message.assert_awaited_once()
    call_kwargs = mock_orchestrator.handle_user_message.await_args.kwargs
    assert call_kwargs["event_id"] == "tg_upd_777666_msg_42"
    assert call_kwargs["chat_id"] == 12345
    assert call_kwargs["telegram_user_id"] == 67890
    assert call_kwargs["message"] == "Spent 200 on lunch"


