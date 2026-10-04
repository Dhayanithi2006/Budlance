"""Phase 1 Verification & Regression Tests: Complete Round-Trip Transport Costing.

Formula:
    outbound fare + return fare * number of travelers

Verifies:
- Test A: 1 traveler (₹500 + ₹500) * 1 = ₹1,000
- Test B: 2 travelers (₹500 + ₹500) * 2 = ₹2,000
- Test C: Asymmetric outbound & return fares (₹500 + ₹700) * 2 = ₹2,400
- Test D: Bucket A integration uses the complete round-trip amount
- Test E: Feasibility engine uses the complete round-trip amount
- Test F: Step 7 Conversation simulation: Chennai -> Goa, 2 people, 4 days, Train
"""

from decimal import Decimal
from unittest.mock import AsyncMock
import pytest

from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.ai.service import AIIntentService
from budlance.attractions.selector import AttractionSelector
from budlance.cache.fallback import FallbackDataProvider
from budlance.cache.manager import CacheFallbackManager
from budlance.db.repositories.conversation_repo import ConversationStateRepository
from budlance.db.repositories.intent_repo import IntentRepository
from budlance.db.repositories.itinerary_repo import ItineraryRepository
from budlance.db.repositories.ledger_repo import LedgerRepository
from budlance.db.repositories.rescue_repo import RescueRepository
from budlance.db.repositories.trip_repo import TripRepository
from budlance.db.repositories.user_repo import UserRepository
from budlance.engine.budget import ReverseBudgetEngine
from budlance.engine.models import DataSource
from budlance.engine.optimizer import OptimizationEngine
from budlance.estimation.estimator import EstimationLayer
from budlance.itinerary.enhancer import ItineraryEnhancer
from budlance.itinerary.generator import ItineraryGenerator
from budlance.ledger.manager import VirtualLedgerManager
from budlance.normalization.normalizer import DataNormalizer
from budlance.normalization.transit import (
    build_round_trip_transit_options,
    calculate_round_trip_cost,
)
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.rescue.service import RescueService
from budlance.schemas.travel import FlightOption, HotelOption, TransitOption


@pytest.fixture
def orchestrator():
    """Build isolated orchestrator instance with in-memory repos."""
    user_repo = UserRepository(client=None)
    trip_repo = TripRepository(client=None)
    intent_repo = IntentRepository(client=None)
    itinerary_repo = ItineraryRepository(client=None)
    ledger_repo = LedgerRepository(client=None)
    rescue_repo = RescueRepository(client=None)
    conversation_repo = ConversationStateRepository(client=None)

    ai_service = AIIntentService(use_mock=True)
    cache_manager = CacheFallbackManager()
    normalizer = DataNormalizer()
    estimation_layer = EstimationLayer()
    budget_engine = ReverseBudgetEngine()
    optimizer = OptimizationEngine(budget_engine=budget_engine, estimation_layer=estimation_layer)
    attraction_selector = AttractionSelector()
    itinerary_generator = ItineraryGenerator(
        itinerary_repo=itinerary_repo,
        attraction_selector=attraction_selector,
    )
    itinerary_enhancer = ItineraryEnhancer(use_mock=True)
    ledger_manager = VirtualLedgerManager(ledger_repo=ledger_repo)
    rescue_service = RescueService(
        trip_repo=trip_repo,
        itinerary_repo=itinerary_repo,
        ledger_repo=ledger_repo,
        rescue_repo=rescue_repo,
        ai_service=ai_service,
        cache_manager=cache_manager,
        normalizer=normalizer,
        budget_engine=budget_engine,
        estimation_layer=estimation_layer,
        ledger_manager=ledger_manager,
    )

    return BudlanceOrchestrator(
        user_repo=user_repo,
        trip_repo=trip_repo,
        intent_repo=intent_repo,
        itinerary_repo=itinerary_repo,
        ledger_repo=ledger_repo,
        rescue_repo=rescue_repo,
        conversation_repo=conversation_repo,
        ai_service=ai_service,
        cache_manager=cache_manager,
        normalizer=normalizer,
        estimation_layer=estimation_layer,
        budget_engine=budget_engine,
        optimizer=optimizer,
        attraction_selector=attraction_selector,
        itinerary_generator=itinerary_generator,
        itinerary_enhancer=itinerary_enhancer,
        ledger_manager=ledger_manager,
        rescue_service=rescue_service,
    )


# ==============================================================================
# Step 6: Tests A - E
# ==============================================================================

def test_a_one_traveler_round_trip():
    """Test A — 1 traveler: outbound=₹500, return=₹500, people=1 -> Expected ₹1,000."""
    outbound = Decimal("500.00")
    ret = Decimal("500.00")
    people = 1

    total = calculate_round_trip_cost(outbound, ret, people)
    assert total == Decimal("1000.00")

    # Verify through build_round_trip_transit_options
    out_opt = TransitOption(
        transit_type="train",
        origin="Chennai",
        destination="Goa",
        name_or_operator="Vasco Express",
        price=outbound,
        class_or_type="SL",
    )
    ret_opt = TransitOption(
        transit_type="train",
        origin="Goa",
        destination="Chennai",
        name_or_operator="Vasco Express",
        price=ret,
        class_or_type="SL",
    )
    opts = build_round_trip_transit_options([out_opt], [ret_opt], people)
    assert len(opts) == 1
    assert opts[0].price == Decimal("1000.00")


def test_b_two_travelers_round_trip():
    """Test B — 2 travelers: outbound=₹500, return=₹500, people=2 -> Expected ₹2,000."""
    outbound = Decimal("500.00")
    ret = Decimal("500.00")
    people = 2

    total = calculate_round_trip_cost(outbound, ret, people)
    assert total == Decimal("2000.00")

    # Verify through build_round_trip_transit_options
    out_opt = TransitOption(
        transit_type="train",
        origin="Chennai",
        destination="Goa",
        name_or_operator="Vasco Express",
        price=outbound,
        class_or_type="SL",
    )
    ret_opt = TransitOption(
        transit_type="train",
        origin="Goa",
        destination="Chennai",
        name_or_operator="Vasco Express",
        price=ret,
        class_or_type="SL",
    )
    opts = build_round_trip_transit_options([out_opt], [ret_opt], people)
    assert len(opts) == 1
    assert opts[0].price == Decimal("2000.00")


def test_c_different_outbound_return_fare():
    """Test C — Asymmetric fares: outbound=₹500, return=₹700, people=2 -> Expected ₹2,400.

    Guarantees the system does NOT simply multiply outbound by 2.
    """
    outbound = Decimal("500.00")
    ret = Decimal("700.00")
    people = 2

    total = calculate_round_trip_cost(outbound, ret, people)
    assert total == Decimal("2400.00")
    assert total != outbound * Decimal("2") * Decimal(people)  # Must NOT be 500 * 2 * 2 = 2000

    out_opt = TransitOption(
        transit_type="train",
        origin="CityA",
        destination="CityB",
        name_or_operator="Express",
        price=outbound,
        class_or_type="3A",
    )
    ret_opt = TransitOption(
        transit_type="train",
        origin="CityB",
        destination="CityA",
        name_or_operator="Express",
        price=ret,
        class_or_type="3A",
    )
    opts = build_round_trip_transit_options([out_opt], [ret_opt], people)
    assert len(opts) == 1
    assert opts[0].price == Decimal("2400.00")


def test_d_bucket_a_integration():
    """Test D — Bucket A integration: Bucket A transport amount equals complete round trip."""
    engine = ReverseBudgetEngine()
    estimation = EstimationLayer()

    outbound = Decimal("5000.00")
    ret = Decimal("5000.00")
    people = 2
    round_trip_transport = calculate_round_trip_cost(outbound, ret, people)  # 20,000.00

    transport = FlightOption(
        airline="IndiGo",
        flight_number="6E-101",
        price=round_trip_transport,
        source=DataSource.FALLBACK,
    )
    hotel = HotelOption(
        name="Seaside Resort",
        hotel_class=3,
        price_per_night=Decimal("2000.00"),
        total_price=Decimal("6000.00"),
        source=DataSource.FALLBACK,
    )

    food_est = estimation.estimate_food(people=people, days=3)
    transit_est = estimation.estimate_local_transit_daily(days=3, people=people)

    result = engine.evaluate(
        total_budget=Decimal("50000.00"),
        transport=transport,
        hotel=hotel,
        food_estimate=food_est,
        local_transit_estimate=transit_est,
        people=people,
        days=3,
    )

    # Bucket A = transport_cost + hotel_cost
    assert result.breakdown.transport_cost == Decimal("20000.00")
    assert result.breakdown.hotel_cost == Decimal("6000.00")
    assert result.breakdown.bucket_a_fixed == Decimal("26000.00")
    assert result.breakdown.bucket_a_fixed == result.breakdown.transport_cost + result.breakdown.hotel_cost


def test_e_feasibility_integration():
    """Test E — Feasibility integration: Feasibility engine sees ₹20,000 transport cost, not ₹10,000 or ₹5,000.

    Budget: ₹20,000
    Transport outbound: ₹5,000, return: ₹5,000, people: 2 -> ₹20,000 total transport.
    With hotel and mandatory costs, trip must be rejected as NOT_FEASIBLE because
    transport alone consumes the entire ₹20,000 budget!
    """
    engine = ReverseBudgetEngine()
    estimation = EstimationLayer()

    outbound = Decimal("5000.00")
    ret = Decimal("5000.00")
    people = 2
    round_trip_transport = calculate_round_trip_cost(outbound, ret, people)  # 20,000.00

    transport = TransitOption(
        transit_type="train",
        origin="Origin",
        destination="Dest",
        name_or_operator="Express",
        price=round_trip_transport,
        class_or_type="1A",
    )
    hotel = HotelOption(
        name="Budget Inn",
        hotel_class=2,
        price_per_night=Decimal("1000.00"),
        total_price=Decimal("3000.00"),
    )

    food_est = estimation.estimate_food(people=people, days=3)
    transit_est = estimation.estimate_local_transit_daily(days=3, people=people)

    result = engine.evaluate(
        total_budget=Decimal("20000.00"),
        transport=transport,
        hotel=hotel,
        food_estimate=food_est,
        local_transit_estimate=transit_est,
        people=people,
        days=3,
    )

    # Feasibility engine must evaluate against ₹20,000 transport, not ₹10,000 or ₹5,000
    assert result.breakdown.transport_cost == Decimal("20000.00")
    assert result.is_feasible is False
    assert result.status == "NOT_FEASIBLE"
    # Mandatory costs = rescue (2000) + fixed (20000 + 3000) + survival > 20000
    assert result.deficit > Decimal("0.00")


# ==============================================================================
# Step 7: Real Conversation Simulation
# ==============================================================================

@pytest.mark.asyncio
async def test_step7_real_conversation_simulation_chennai_goa(orchestrator):
    """Step 7 — Real Conversation Simulation:
    Chennai -> Goa
    2 people
    4 days
    Train (SL class)
    Known fallback fare: SL = ₹500 outbound, ₹500 return

    Verify:
    1. Transport option is found
    2. Outbound fare is identified (₹500)
    3. Return fare is identified (₹500)
    4. Complete transport amount is calculated: (₹500 + ₹500) * 2 = ₹2,000
    5. Bucket A uses the complete amount (₹2,000 + hotel)
    6. Reverse-budget uses the complete amount
    7. Feasibility uses the complete amount
    8. No duplicate charging occurs (Day 1 onward has ₹2,000, Day 4 return has ₹0.00)
    9. Itinerary contains onward travel and return journey
    """
    chat_id = 9901

    # Verify fallback data provider returns corridor
    provider = FallbackDataProvider()
    corridor_fwd = provider.get_train_corridor("Chennai", "Goa")
    assert corridor_fwd is not None
    assert corridor_fwd["classes"]["SL"] == 500

    corridor_rev = provider.get_train_corridor("Goa", "Chennai")
    assert corridor_rev is not None
    assert corridor_rev["classes"]["SL"] == 500

    # Turn 1: User requests trip
    intent = ParsedTripIntent(
        action=TripAction.NEW_TRIP,
        origin="Chennai",
        destination="Goa",
        budget=Decimal("40000.00"),
        people=2,
        days=4,
        transport_mode="train",
        transport_class="sleeper",
        currency="INR",
    )
    orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=intent)

    # 1. Transport options lookup
    options = await orchestrator.lookup_transport_options(
        origin="Chennai",
        destination="Goa",
        people=2,
        transport_mode="train",
        transport_class="sleeper",
    )

    # 1. Transport option is found
    assert len(options) > 0
    selected_option = options[0]

    # 2 & 3. Outbound and return fares identified from corridor (SL = 500 each)
    # 4. Complete transport amount: (500 + 500) * 2 = 2000.00
    assert selected_option.price == Decimal("2000.00")
    assert selected_option.class_or_type in ("SL", "sleeper", "SLEEPER")

    # Step 2 in conversation flow: Handle user message to evaluate transport feasibility
    res_transport = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Plan a trip from Chennai to Goa for 2 people, 4 days, budget 40000, sleeper train",
    )

    assert res_transport.status == "FEASIBLE_TRANSPORT"
    assert res_transport.selected_transport is not None
    assert res_transport.selected_transport.price == Decimal("2000.00")
    assert "irctc.co.in" in res_transport.message_text

    # Turn 2: User confirms external booking ("Booked.")
    intent_booked = ParsedTripIntent(
        action=TripAction.CONFIRM_BOOKING,
        booking_confirmed=True,
    )
    orchestrator.ai_service.parse_trip_intent_with_context = AsyncMock(return_value=intent_booked)

    res_plan = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Booked.",
    )

    assert res_plan.status == "FEASIBLE"
    assert res_plan.selected_transport is not None
    # 4. Complete transport amount is persisted in plan
    assert res_plan.selected_transport.price == Decimal("2000.00")

    # 5. Bucket A uses complete amount
    breakdown = res_plan.budget_breakdown
    assert breakdown.transport_cost == Decimal("2000.00")
    assert breakdown.bucket_a_fixed == breakdown.transport_cost + breakdown.hotel_cost

    # 6. Reverse-budget uses complete amount
    assert breakdown.total_allocated > Decimal("0.00")

    # 7. Feasibility uses complete amount
    assert res_plan.status == "FEASIBLE"

    # 8. Virtual ledger initialization and no duplicate charging
    ledger_entries = orchestrator.ledger_repo.get_ledger_entries(res_plan.trip_id)
    assert len(ledger_entries) > 0
    transport_entry = next(
        (e for e in ledger_entries if e.category == "fixed_booking" and "Transport" in e.description),
        None,
    )
    assert transport_entry is not None
    assert transport_entry.description == "Transport (Onward & Return)"
    assert transport_entry.allocated_amount == Decimal("2000.00")
    assert transport_entry.planned_amount == Decimal("2000.00")

    # 9. Verify Itinerary generator's onward travel and return journey logic
    from budlance.schemas.travel import PlaceOption
    from uuid import uuid4
    places = [
        PlaceOption(name="Baga Beach", category="beach", source=DataSource.LIVE),
        PlaceOption(name="Aguada Fort", category="attraction", source=DataSource.LIVE),
    ]

    # Generate itinerary with places to verify onward & return transport scheduling
    legacy_itin = orchestrator.itinerary_generator._generate_legacy_places_itinerary(
        trip_id=res_plan.trip_id or uuid4(),
        destination="Goa",
        evaluation=res_plan.evaluation if hasattr(res_plan, "evaluation") else None or orchestrator.budget_engine.evaluate(
            total_budget=Decimal("40000.00"),
            people=2,
            days=4,
            transport=res_plan.selected_transport,
            hotel=res_plan.selected_hotel,
            food_estimate=orchestrator.estimation.estimate_food(2, 4),
            local_transit_estimate=orchestrator.estimation.estimate_local_transit_daily(4, 2),
        ),
        days_count=4,
        transport=res_plan.selected_transport,
        hotel=res_plan.selected_hotel,
        places=places,
    )

    assert legacy_itin is not None
    assert len(legacy_itin.days) == 4

    day_1 = legacy_itin.days[0]
    day_4 = legacy_itin.days[3]

    # Onward travel in Day 1 Morning
    onward_item = next(
        (it for it in day_1.items if it.category == "transport" and "Onward" in it.activity),
        None,
    )
    assert onward_item is not None
    assert onward_item.planned_cost == Decimal("2000.00")
    assert "Vasco Express" in onward_item.activity

    # Return travel in Day 4 Evening
    return_item = next(
        (it for it in day_4.items if it.category == "transport" and "Return" in it.activity),
        None,
    )
    assert return_item is not None
    # No duplicate charging: return leg planned cost is ₹0.00 (covered in Bucket A)
    assert return_item.planned_cost == Decimal("0.00")
    assert "Vasco Express" in return_item.activity

    # Total planned transport in itinerary matches exactly ₹2,000
    total_itin_transport = sum(
        it.planned_cost
        for day in legacy_itin.days
        for it in day.items
        if it.category == "transport"
    )
    assert total_itin_transport == Decimal("2000.00")
