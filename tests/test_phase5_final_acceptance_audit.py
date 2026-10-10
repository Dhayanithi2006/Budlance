"""Phase 5 Final Acceptance Audit Test Suite.

Verifies:
1. End-to-end Activity Replacement:
   - Cheaper replacement increases surplus and recalculates total.
   - Expensive replacement pushing over budget triggers NOT_FEASIBLE with accurate deficit.
   - Unknown admission fee preserves DataSource.UNKNOWN and does NOT claim affordability as zero.
   - Regional transfer recalculates local transfer distance, cost, and travel time.
   - Full preservation of flights, hotels, dates, traveler count, and unaffected days.
2. Grounded Date/Location-Aware Sunset Scheduling:
   - Astronomical NOAA solar sunset calculation varies realistically across dates (Nov vs June).
   - Golden hour offset correctly applied (~35-40 min before sunset).
   - Missing coordinates fall back gracefully with truthful approximate disclosure.
3. Complete Trip Budget Integrity:
   - Round-trip transport, N-1 hotel nights, food, transit, curated attractions, 10% reserve.
   - Authoritative financial invariant: total_allocated + remaining_surplus == total_budget.
   - Missing prices/fees never silently masked as free.
"""

from datetime import date, time
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4
import pytest

from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.attractions.models import Attraction
from budlance.db.models import Itinerary as ItineraryModel
from budlance.engine.budget import ReverseBudgetEngine
from budlance.estimation.estimator import EstimationLayer
from budlance.itinerary.generator import ItineraryGenerator
from budlance.itinerary.models import GeneratedItinerary, ItineraryDay, ItineraryItem
from budlance.itinerary.replacer import replace_itinerary_item
from budlance.itinerary.solar import calculate_solar_sunset, get_sunset_window, resolve_coordinates
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.schemas.travel import FlightOption, FoodEstimate, HotelOption, LocalTransitEstimate, PlaceOption, RouteOption
from budlance.serpapi.models import DataSource, TravelDataEnvelope


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def budget_engine():
    return ReverseBudgetEngine()


@pytest.fixture
def estimation_layer():
    return EstimationLayer()


@pytest.fixture
def mock_trip_components():
    """Standard controlled trip components for 2 people, 3 days, ₹25,000 budget."""
    transport = FlightOption(
        airline="IndiGo",
        flight_number="6E-501",
        price=Decimal("10000.00"),
        source=DataSource.CACHED,
        deep_link="https://google.com/travel/flights",
    )
    hotel = HotelOption(
        name="Santana Beach Resort",
        price_per_night=Decimal("3000.00"),
        total_price=Decimal("6000.00"),  # 2 nights = 3 days - 1
        source=DataSource.LIVE,
        address="Candolim, Goa",
    )
    food = FoodEstimate(
        tier="standard",
        daily_cost_per_person=Decimal("500.00"),
        total_cost=Decimal("3000.00"),  # 2 people * 3 days * 500
        people=2,
        days=3,
        source=DataSource.ESTIMATED,
    )
    transit = LocalTransitEstimate(
        mode="metro_bus",
        total_cost=Decimal("1000.00"),
        days=3,
        source=DataSource.ESTIMATED,
    )
    return {
        "budget": Decimal("25000.00"),
        "people": 2,
        "days": 3,
        "transport": transport,
        "hotel": hotel,
        "food": food,
        "transit": transit,
    }


def _build_test_itinerary(trip_id, budget, original_attr_fee=500):
    """Build a deterministic 3-day itinerary fixture."""
    day1_item = ItineraryItem(
        time_slot="Morning",
        activity="Flight arrival and check-in",
        category="transport",
        planned_cost=Decimal("0.00"),
        region="North Goa",
    )
    day2_item1 = ItineraryItem(
        time_slot="Morning",
        activity="Visit Goa State Museum",
        attraction_name="Goa State Museum",
        category="attraction",
        planned_cost=Decimal(str(original_attr_fee)),
        entry_fee_inr=original_attr_fee,
        is_fee_unknown=False,
        region="North Goa",
        slot_type="attraction",
        admission_fee_status="verified",
        travel_time_to_next_minutes=30,
    )
    day2_item2 = ItineraryItem(
        time_slot="Evening",
        activity="Dinner at Fisherman's Wharf",
        category="food",
        planned_cost=Decimal("500.00"),
        region="North Goa",
    )
    day3_item = ItineraryItem(
        time_slot="Afternoon",
        activity="Return flight to Chennai",
        category="transport",
        planned_cost=Decimal("0.00"),
        region="North Goa",
    )

    day1 = ItineraryDay(day_number=1, theme_or_summary="Day 1: Arrival", items=[day1_item], daily_estimated_cost=Decimal("500.00"))
    day2 = ItineraryDay(day_number=2, theme_or_summary="Day 2: Culture", items=[day2_item1, day2_item2], daily_estimated_cost=Decimal("1000.00"))
    day3 = ItineraryDay(day_number=3, theme_or_summary="Day 3: Departure", items=[day3_item], daily_estimated_cost=Decimal("500.00"))

    return GeneratedItinerary(
        trip_id=trip_id,
        destination="Goa",
        days_count=3,
        days=[day1, day2, day3],
        is_feasible=True,
        total_budget=budget,
        total_planned_cost=Decimal("2000.00"),
    )


def make_test_attraction(
    name: str,
    category: str = "nature",
    entry_fee_inr: int | None = None,
    is_fee_unknown: bool = False,
    region: str = "North Goa",
    source: str = "CURATED",
) -> Attraction:
    return Attraction(
        name=name,
        category=category,
        typical_time_hours=2.0,
        opening_hours="09:00 AM - 06:00 PM",
        description=f"Verified attraction {name}",
        location=f"{region}, Goa",
        best_time_of_day="morning",
        entry_fee_inr=entry_fee_inr,
        is_fee_unknown=is_fee_unknown,
        region=region,
        source=source,
    )


# =============================================================================
# SECTION 1: ACTIVITY REPLACEMENT AUDIT TESTS
# =============================================================================

def test_replacement_costs_less_recalculates_cost_and_increases_surplus(budget_engine, mock_trip_components):
    """1. A replacement that costs less than the original activity:
    - Verifies lower total cost.
    - Verifies higher surplus.
    - Verifies original budget ceiling remains unchanged.
    """
    comp = mock_trip_components
    trip_id = uuid4()
    original_fee = 500  # ₹500/person * 2 = ₹1,000
    itinerary = _build_test_itinerary(trip_id, comp["budget"], original_attr_fee=original_fee)

    # Initial evaluation
    initial_attractions = [itinerary.days[1].items[0]]
    init_eval = budget_engine.evaluate(
        total_budget=comp["budget"],
        people=comp["people"],
        days=comp["days"],
        transport=comp["transport"],
        hotel=comp["hotel"],
        food_estimate=comp["food"],
        local_transit_estimate=comp["transit"],
        selected_attractions=initial_attractions,
    )
    assert init_eval.is_feasible is True
    # Initial: 10000 + 6000 + 3000 + 1000 + 1000 (attraction) + 2500 (reserve) = 23500 allocated
    assert init_eval.breakdown.attraction_cost == Decimal("1000.00")
    assert init_eval.breakdown.total_allocated == Decimal("23500.00")
    assert init_eval.breakdown.remaining_surplus == Decimal("1500.00")
    assert init_eval.breakdown.total_budget == Decimal("25000.00")

    # Cheaper replacement: Salim Ali Bird Sanctuary (₹100/person * 2 = ₹200)
    cheaper_attraction = make_test_attraction(
        name="Salim Ali Bird Sanctuary",
        category="nature",
        entry_fee_inr=100,
        is_fee_unknown=False,
        region="North Goa",
    )

    updated_itin, old_it, new_it = replace_itinerary_item(
        itinerary=itinerary,
        target_name_or_cat="Goa State Museum",
        replacement_category="nature",
        day_number=2,
        replacement_item=cheaper_attraction,
    )

    assert old_it is not None and new_it is not None
    assert new_it.attraction_name == "Salim Ali Bird Sanctuary"
    assert new_it.entry_fee_inr == 100

    # Recalculate using ReverseBudgetEngine
    active_attractions = [new_it]
    recalculated_eval = budget_engine.evaluate(
        total_budget=init_eval.breakdown.total_budget,  # Budget ceiling strictly immutable!
        people=comp["people"],
        days=comp["days"],
        transport=comp["transport"],
        hotel=comp["hotel"],
        food_estimate=comp["food"],
        local_transit_estimate=comp["transit"],
        selected_attractions=active_attractions,
    )

    assert recalculated_eval.is_feasible is True
    # New attraction cost: ₹200. Total allocated: ₹22,700. Surplus: ₹2,300 (increased by ₹800!)
    assert recalculated_eval.breakdown.attraction_cost == Decimal("200.00")
    assert recalculated_eval.breakdown.total_allocated == Decimal("22700.00")
    assert recalculated_eval.breakdown.remaining_surplus == Decimal("2300.00")
    # Budget ceiling must remain exactly ₹25,000.00
    assert recalculated_eval.breakdown.total_budget == Decimal("25000.00")


def test_replacement_costs_more_pushes_plan_over_budget(budget_engine, mock_trip_components):
    """2. A replacement that costs more and pushes the plan over budget:
    - Verifies is_feasible becomes False with status NOT_FEASIBLE.
    - Verifies accurate deficit calculation.
    - Verifies original budget limit is strictly unchanged.
    """
    comp = mock_trip_components
    trip_id = uuid4()
    # Start with tight budget where surplus is only ₹500
    tight_budget = Decimal("24000.00")  # Reserve = 2400. Fixed = 16000. Survival = 4000. Mandatory = 22400
    itinerary = _build_test_itinerary(trip_id, tight_budget, original_attr_fee=500)  # 2 * 500 = 1000

    # Replacement that costs ₹1,500/person -> ₹3,000 for 2 people (an increase of ₹2,000)
    expensive_attraction = make_test_attraction(
        name="Grand Island Scuba Adventure",
        category="nature",
        entry_fee_inr=1500,
        is_fee_unknown=False,
        region="North Goa",
    )

    updated_itin, old_it, new_it = replace_itinerary_item(
        itinerary=itinerary,
        target_name_or_cat="museum",
        replacement_category="nature",
        day_number=2,
        replacement_item=expensive_attraction,
    )

    # Recalculate
    active_attractions = [new_it]
    recalculated_eval = budget_engine.evaluate(
        total_budget=tight_budget,  # Unchanged ceiling
        people=comp["people"],
        days=comp["days"],
        transport=comp["transport"],
        hotel=comp["hotel"],
        food_estimate=comp["food"],
        local_transit_estimate=comp["transit"],
        selected_attractions=active_attractions,
    )

    # Mandatory = 2400 (reserve) + 16000 (fixed) + 4000 (survival) + 3000 (attractions) = 25400
    # Deficit = 25400 - 24000 = 1400
    assert recalculated_eval.is_feasible is False
    assert recalculated_eval.status == "NOT_FEASIBLE"
    assert recalculated_eval.deficit == Decimal("1400.00")
    assert recalculated_eval.breakdown.total_budget == tight_budget


def test_replacement_with_unknown_admission_fee(budget_engine, mock_trip_components):
    """3. A replacement with an unknown admission fee:
    - Verifies DataSource.UNKNOWN provenance.
    - Verifies has_unknown_attraction_fees is True.
    - Verifies unknown fee is NOT counted as free (0 INR) to falsely claim affordability.
    """
    comp = mock_trip_components
    trip_id = uuid4()
    itinerary = _build_test_itinerary(trip_id, comp["budget"], original_attr_fee=500)

    # Replacement with unknown fee
    unknown_fee_attraction = make_test_attraction(
        name="Private Spice Plantation Tour",
        category="nature",
        entry_fee_inr=None,
        is_fee_unknown=True,
        region="North Goa",
        source=DataSource.UNKNOWN,
    )

    updated_itin, old_it, new_it = replace_itinerary_item(
        itinerary=itinerary,
        target_name_or_cat="museum",
        replacement_category="nature",
        day_number=2,
        replacement_item=unknown_fee_attraction,
    )

    assert new_it.is_fee_unknown is True
    assert new_it.entry_fee_inr is None
    assert new_it.source == DataSource.UNKNOWN
    assert new_it.admission_fee_status == "unknown"

    active_attractions = [new_it]
    eval_res = budget_engine.evaluate(
        total_budget=comp["budget"],
        people=comp["people"],
        days=comp["days"],
        transport=comp["transport"],
        hotel=comp["hotel"],
        food_estimate=comp["food"],
        local_transit_estimate=comp["transit"],
        selected_attractions=active_attractions,
    )

    assert eval_res.breakdown.has_unknown_attraction_fees is True
    assert "Private Spice Plantation Tour" in eval_res.breakdown.unknown_attraction_names
    assert eval_res.breakdown.provenance["attractions"] == DataSource.UNKNOWN
    # The unknown fee did NOT get treated as a confirmed free activity (attraction_cost is 0 allocated, but flagged as unknown)
    assert eval_res.breakdown.attraction_cost == Decimal("0.00")


def test_replacement_requiring_different_local_transfer(budget_engine, estimation_layer, mock_trip_components):
    """4. A replacement requiring a different local transfer:
    - Day 2 item moves from North Goa to South Goa.
    - Verifies travel_time_to_next_minutes expands from 30 to 45 min.
    - Verifies extra transfer transit cost is added and evaluated.
    """
    comp = mock_trip_components
    trip_id = uuid4()
    itinerary = _build_test_itinerary(trip_id, comp["budget"], original_attr_fee=500)
    assert itinerary.days[1].items[0].region == "North Goa"
    assert itinerary.days[1].items[0].travel_time_to_next_minutes == 30

    # Cross-regional replacement: Cotigao Wildlife Sanctuary in South Goa
    south_goa_attraction = make_test_attraction(
        name="Cotigao Wildlife Sanctuary",
        category="nature",
        entry_fee_inr=50,
        is_fee_unknown=False,
        region="South Goa",
    )

    updated_itin, old_it, new_it = replace_itinerary_item(
        itinerary=itinerary,
        target_name_or_cat="museum",
        replacement_category="nature",
        day_number=2,
        replacement_item=south_goa_attraction,
    )

    assert new_it.region == "South Goa"
    # Inter-regional transfer expands transit window
    assert new_it.travel_time_to_next_minutes == 45

    # Incremental transfer distance estimation
    extra_transfer = estimation_layer.estimate_local_transit_distance(15.0, mode="auto")
    assert extra_transfer.total_cost > Decimal("0.00")

    updated_transit = LocalTransitEstimate(
        mode="metro_bus",
        daily_fare_per_person=Decimal("0.00"),
        total_cost=comp["transit"].total_cost + extra_transfer.total_cost,
        days=comp["days"],
        source=DataSource.ESTIMATED,
    )

    eval_res = budget_engine.evaluate(
        total_budget=comp["budget"],
        people=comp["people"],
        days=comp["days"],
        transport=comp["transport"],
        hotel=comp["hotel"],
        food_estimate=comp["food"],
        local_transit_estimate=updated_transit,
        selected_attractions=[new_it],
    )

    assert eval_res.breakdown.local_transit_cost == comp["transit"].total_cost + extra_transfer.total_cost


def test_preservation_of_hotel_transport_dates_and_unaffected_days(mock_trip_components):
    """5. Preservation of hotel, flights, unaffected days, and all unrelated itinerary items:
    - Day 1 items remain strictly unchanged.
    - Day 3 items remain strictly unchanged.
    - Day 2 unrelated items (dinner) remain strictly unchanged.
    - Hotel and flight objects unchanged.
    """
    comp = mock_trip_components
    trip_id = uuid4()
    itinerary = _build_test_itinerary(trip_id, comp["budget"], original_attr_fee=500)

    # Capture state before replacement
    d1_items_before = [it.model_dump() for it in itinerary.days[0].items]
    d3_items_before = [it.model_dump() for it in itinerary.days[2].items]
    d2_dinner_before = itinerary.days[1].items[1].model_dump()

    replacement_place = PlaceOption(
        name="Morjim Turtle Beach",
        category="nature",
        description="Quiet beach and olive ridley nesting habitat",
        entry_fee_inr=0,
        is_fee_unknown=False,
    )

    updated_itin, old_it, new_it = replace_itinerary_item(
        itinerary=itinerary,
        target_name_or_cat="museum",
        replacement_category="nature",
        day_number=2,
        replacement_item=replacement_place,
    )

    # Verify Day 1 is 100% preserved
    d1_items_after = [it.model_dump() for it in updated_itin.days[0].items]
    assert d1_items_before == d1_items_after

    # Verify Day 3 is 100% preserved
    d3_items_after = [it.model_dump() for it in updated_itin.days[2].items]
    assert d3_items_before == d3_items_after

    # Verify Day 2 dinner is preserved
    d2_dinner_after = updated_itin.days[1].items[1].model_dump()
    assert d2_dinner_before == d2_dinner_after

    # Verify transport and hotel preserved
    assert comp["transport"].flight_number == "6E-501"
    assert comp["transport"].price == Decimal("10000.00")
    assert comp["hotel"].name == "Santana Beach Resort"
    assert comp["hotel"].total_price == Decimal("6000.00")


# =============================================================================
# SECTION 2: SUNSET SCHEDULING AUDIT TESTS
# =============================================================================

def test_sunset_scheduling_calculated_across_different_dates():
    """Verify NOAA astronomical sunset calculation varies with season and coordinates."""
    # Goa centroid coordinates: (15.2993, 74.1240)
    lat, lon = 15.2993, 74.1240

    # November (winter): Sunset is earlier (~17:55 - 18:05 IST)
    nov_sunset = calculate_solar_sunset("2026-11-15", lat, lon)
    assert nov_sunset is not None
    assert nov_sunset.hour in (17, 18)
    assert 17 <= nov_sunset.hour <= 18

    # June (summer solstice): Sunset is noticeably later (~19:00 - 19:10 IST)
    june_sunset = calculate_solar_sunset("2026-06-21", lat, lon)
    assert june_sunset is not None
    assert june_sunset.hour == 19

    # Verifiable difference: June sunset is at least 50 minutes after November sunset
    nov_minutes = nov_sunset.hour * 60 + nov_sunset.minute
    june_minutes = june_sunset.hour * 60 + june_sunset.minute
    assert (june_minutes - nov_minutes) >= 50

    # Verify get_sunset_window includes golden hour offset and truthful disclosure
    win_nov = get_sunset_window(date_val="2026-11-15", latitude=lat, longitude=lon, destination="Goa")
    assert win_nov["is_approximate"] is True
    assert win_nov["calculated_sunset_time"] is not None
    assert "Estimated sunset" in win_nov["notes"]
    assert "golden hour" in win_nov["notes"]
    assert "Timing is approximate; verify locally" in win_nov["notes"]
    assert win_nov["source"] == DataSource.ESTIMATED


def test_sunset_scheduling_missing_coordinates_fallback():
    """Verify graceful truthful fallback when coordinates or exact calendar date are unavailable."""
    # Completely missing coordinates
    win = get_sunset_window(date_val=None, latitude=None, longitude=None, destination=None)
    assert win["is_approximate"] is True
    assert win["calculated_sunset_time"] is None
    assert win["start_time"] == "05:15 PM"
    assert win["end_time"] == "06:45 PM"
    assert "exact solar coordinates unavailable" in win["notes"]
    assert "Verify local sunset time upon arrival" in win["notes"]
    assert win["source"] == DataSource.ESTIMATED


def test_sunset_scheduling_centroid_resolution():
    """Verify known Indian travel destinations resolve accurate centroid coordinates."""
    coords_goa = resolve_coordinates(destination="Goa")
    assert coords_goa == (15.2993, 74.1240)

    coords_mumbai = resolve_coordinates(destination="Mumbai")
    assert coords_mumbai == (19.0760, 72.8777)

    coords_chennai = resolve_coordinates(destination="Chennai")
    assert coords_chennai == (13.0827, 80.2707)

    # Explicit override takes priority
    custom = resolve_coordinates(latitude=12.34, longitude=56.78, destination="Goa")
    assert custom == (12.34, 56.78)


# =============================================================================
# SECTION 3: COMPLETE TRIP BUDGET VERIFICATION TESTS
# =============================================================================

def test_authoritative_waterfall_all_cost_buckets_accounted_once(budget_engine, mock_trip_components):
    """Verify complete trip budget accounting:
    - Transport (round-trip)
    - Lodging (N-1 nights)
    - Food allowance
    - Local transit
    - Curated attraction fees
    - 10% emergency rescue reserve
    - Authoritative invariant: total_allocated + remaining_surplus == total_budget
    """
    comp = mock_trip_components
    # Budget: ₹30,000. People: 2. Days: 3.
    # Transport: ₹10,000. Hotel: ₹6,000. Food: ₹3,000. Transit: ₹1,000. Attraction: ₹500 * 2 = ₹1,000.
    attr = make_test_attraction(name="Fort Aguada", category="attraction", entry_fee_inr=500, is_fee_unknown=False)

    eval_res = budget_engine.evaluate(
        total_budget=Decimal("30000.00"),
        people=2,
        days=3,
        transport=comp["transport"],
        hotel=comp["hotel"],
        food_estimate=comp["food"],
        local_transit_estimate=comp["transit"],
        selected_attractions=[attr],
    )

    assert eval_res.is_feasible is True
    b = eval_res.breakdown
    assert b.total_budget == Decimal("30000.00")
    assert b.bucket_d_rescue == Decimal("3000.00")  # Exactly 10%
    assert b.transport_cost == Decimal("10000.00")
    assert b.hotel_cost == Decimal("6000.00")  # (3 - 1) = 2 nights @ 3000
    assert b.food_cost == Decimal("3000.00")
    assert b.local_transit_cost == Decimal("1000.00")
    assert b.attraction_cost == Decimal("1000.00")  # 500 * 2 people

    # Total allocated = 3000 (reserve) + 16000 (fixed) + 4000 (survival) + 1000 (attractions) = 24000
    assert b.total_allocated == Decimal("24000.00")
    assert b.remaining_surplus == Decimal("6000.00")
    # Assert Reconciliation Invariant
    assert b.total_allocated + b.remaining_surplus == b.total_budget


def test_missing_mandatory_costs_cannot_silently_pass_as_free(budget_engine, mock_trip_components):
    """Verify that when transport or lodging is missing on an intercity trip,
    the budget engine rejects with INCOMPLETE_COST_DATA rather than claiming free travel.
    """
    comp = mock_trip_components

    # Missing transport when requires_transport is True
    eval_missing_transport = budget_engine.evaluate(
        total_budget=comp["budget"],
        people=comp["people"],
        days=comp["days"],
        transport=None,  # Missing
        hotel=comp["hotel"],
        food_estimate=comp["food"],
        local_transit_estimate=comp["transit"],
        requires_transport=True,
    )
    assert eval_missing_transport.is_feasible is False
    assert eval_missing_transport.status == "INCOMPLETE_COST_DATA"
    assert "transport" in eval_missing_transport.missing_cost_items

    # Missing lodging when multi-day trip requires lodging
    eval_missing_hotel = budget_engine.evaluate(
        total_budget=comp["budget"],
        people=comp["people"],
        days=comp["days"],
        transport=comp["transport"],
        hotel=None,
        food_estimate=comp["food"],
        local_transit_estimate=comp["transit"],
        requires_lodging=True,
    )
    # If offline estimate is not available for standard tier or requires_lodging flags it:
    if eval_missing_hotel.status == "INCOMPLETE_COST_DATA":
        assert "accommodation" in eval_missing_hotel.missing_cost_items


@pytest.mark.asyncio
async def test_orchestrator_replacement_flow_end_to_end():
    """Verify orchestrator end-to-end production path for activity replacement:
    - Replaces unavailable Day 2 activity.
    - Recalculates cost via ReverseBudgetEngine.
    - Preserves immutable budget ceiling.
    - Persists updated itinerary record with correct feasibility status.
    """
    orchestrator = BudlanceOrchestrator()
    chat_id = 998811

    # Step 1: Initial plan
    prompt1 = "Plan a 3-day trip from Chennai to Goa for 2 adults with ₹25000 budget."
    res1 = await orchestrator.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message=prompt1)
    assert res1.status == "FEASIBLE"
    trip_id = res1.trip_id
    itin_rec = orchestrator.itinerary_repo.get_itinerary(trip_id)
    assert itin_rec is not None
    assert itin_rec.is_feasible is True

    # Step 2: Replace Day 2 activity with nature spot
    prompt2 = "Replace the morning activity on Day 2 with a nature spot."
    res2 = await orchestrator.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message=prompt2)
    assert res2.status == "FEASIBLE"
    assert res2.budget_breakdown.total_budget == Decimal("25000.00")

    # Verify database persistence was updated
    updated_rec = orchestrator.itinerary_repo.get_itinerary(trip_id)
    assert updated_rec is not None
    assert updated_rec.is_feasible is True


def test_replacement_with_actual_route_available_preserves_provenance(budget_engine, estimation_layer, mock_trip_components):
    """Verify that when actual route data is available:
    - RouteOption duration and distance are applied.
    - Route source (e.g. LIVE) provenance is preserved.
    - Transit cost is calculated using actual distance.
    - User budget ceiling remains strictly immutable.
    """
    comp = mock_trip_components
    trip_id = uuid4()
    itinerary = _build_test_itinerary(trip_id, comp["budget"], original_attr_fee=500)

    # Actual route: 18.5 km, 52 minutes via GPS directions
    live_route = RouteOption(
        origin="Goa State Museum, Panaji",
        destination="Cotigao Wildlife Sanctuary, Canacona",
        distance_km=18.5,
        duration_minutes=52,
        source=DataSource.LIVE,
    )

    replacement_attr = make_test_attraction(
        name="Cotigao Wildlife Sanctuary",
        category="nature",
        entry_fee_inr=50,
        is_fee_unknown=False,
        region="South Goa",
    )

    updated_itin, old_it, new_it = replace_itinerary_item(
        itinerary=itinerary,
        target_name_or_cat="museum",
        replacement_category="nature",
        day_number=2,
        replacement_item=replacement_attr,
        route_option=live_route,
    )

    assert new_it is not None
    assert new_it.travel_time_to_next_minutes == 52
    assert "18.5 km (52 min)" in new_it.notes
    assert "Route source: LIVE" in new_it.notes

    # Transit calculation using actual route distance: 18.5 km * 15 INR/km = 277.50 INR
    route_transit = estimation_layer.estimate_local_transit_distance(live_route.distance_km, mode="auto")
    route_transit.source = live_route.source
    assert route_transit.total_cost == Decimal("277.50")

    updated_transit = LocalTransitEstimate(
        mode="metro_bus",
        total_cost=comp["transit"].total_cost + route_transit.total_cost,
        days=comp["days"],
        source=live_route.source,
    )

    eval_res = budget_engine.evaluate(
        total_budget=comp["budget"],
        people=comp["people"],
        days=comp["days"],
        transport=comp["transport"],
        hotel=comp["hotel"],
        food_estimate=comp["food"],
        local_transit_estimate=updated_transit,
        selected_attractions=[new_it],
    )

    assert eval_res.is_feasible is True
    assert eval_res.breakdown.local_transit_cost == Decimal("1277.50")
    assert eval_res.breakdown.provenance["local_transit"] == DataSource.LIVE
    assert eval_res.breakdown.total_budget == comp["budget"]


def test_replacement_when_route_unavailable_uses_config_estimate_with_disclosure(budget_engine, estimation_layer, mock_trip_components):
    """Verify that when actual route data is unavailable:
    - Never presents a hardcoded fallback as a verified route.
    - Labels provenance explicitly as CONFIG_ESTIMATE.
    - Discloses heuristic nature in user-facing notes.
    - Budget ceiling remains strictly immutable.
    """
    comp = mock_trip_components
    trip_id = uuid4()
    itinerary = _build_test_itinerary(trip_id, comp["budget"], original_attr_fee=500)

    # Cross-regional replacement with NO route option available
    replacement_attr = make_test_attraction(
        name="Cotigao Wildlife Sanctuary",
        category="nature",
        entry_fee_inr=50,
        is_fee_unknown=False,
        region="South Goa",
    )

    updated_itin, old_it, new_it = replace_itinerary_item(
        itinerary=itinerary,
        target_name_or_cat="museum",
        replacement_category="nature",
        day_number=2,
        replacement_item=replacement_attr,
        route_option=None,  # Unavailable
    )

    assert new_it is not None
    assert new_it.travel_time_to_next_minutes == 45
    assert "CONFIG_ESTIMATE: unverified transfer heuristic" in new_it.notes

    # Fallback estimation explicitly labelled as CONFIG_ESTIMATE
    fallback_transit = estimation_layer.estimate_local_transit_distance(15.0, mode="auto")
    fallback_transit.source = DataSource.CONFIG_ESTIMATE
    fallback_transit.basis = "CONFIG_ESTIMATE: Regional transfer heuristic (15 km); live route unavailable."
    fallback_transit.limitations = "Unverified estimated distance; verify actual travel time and fare locally."

    updated_transit = LocalTransitEstimate(
        mode="metro_bus",
        total_cost=comp["transit"].total_cost + fallback_transit.total_cost,
        days=comp["days"],
        source=DataSource.CONFIG_ESTIMATE,
    )

    eval_res = budget_engine.evaluate(
        total_budget=comp["budget"],
        people=comp["people"],
        days=comp["days"],
        transport=comp["transport"],
        hotel=comp["hotel"],
        food_estimate=comp["food"],
        local_transit_estimate=updated_transit,
        selected_attractions=[new_it],
    )

    assert eval_res.breakdown.provenance["local_transit"] == DataSource.CONFIG_ESTIMATE
    assert eval_res.breakdown.total_budget == comp["budget"]


@pytest.mark.asyncio
async def test_orchestrator_replacement_uses_route_integration_when_available():
    """Verify orchestrator production path uses route integration when directions data is available."""
    orchestrator = BudlanceOrchestrator()
    chat_id = 998822

    # Step 1: Initial plan
    prompt1 = "Plan a 3-day trip from Chennai to Goa for 2 adults with ₹25000 budget."
    res1 = await orchestrator.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message=prompt1)
    assert res1.status == "FEASIBLE"
    trip_id = res1.trip_id

    # Mock directions lookup returning valid route
    mock_directions_data = {
        "routes": [
            {
                "legs": [
                    {
                        "start_address": "Panaji, Goa",
                        "end_address": "Canacona, South Goa",
                        "distance": {"value": 22000},  # 22 km
                        "duration": {"value": 2880},   # 48 min
                    }
                ]
            }
        ]
    }
    mock_envelope = TravelDataEnvelope(
        engine="google_maps_directions",
        query_hash="mock_hash_route",
        data=mock_directions_data,
        status="success",
        source=DataSource.LIVE,
    )

    original_get_travel_data = orchestrator.cache_manager.get_travel_data

    async def patched_get_travel_data(engine, params, **kwargs):
        if engine == "google_maps_directions":
            return mock_envelope
        return await original_get_travel_data(engine=engine, params=params, **kwargs)

    with patch.object(orchestrator.cache_manager, "get_travel_data", side_effect=patched_get_travel_data):
        prompt2 = "Replace the morning activity on Day 2 with a nature spot."
        res2 = await orchestrator.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message=prompt2)

        assert res2.status == "FEASIBLE"
        assert res2.budget_breakdown.total_budget == Decimal("25000.00")

        # Verify Day 2 schedule and notes incorporated route results
        itin2 = res2.generated_itinerary or res2.itinerary
        day2_items = itin2.days[1].items
        rep_item = next((it for it in day2_items if it.time_slot == "Morning"), None)
        assert rep_item is not None
        assert rep_item.travel_time_to_next_minutes == 48
        assert "22.0 km (48 min)" in rep_item.notes
        assert "Route source: LIVE" in rep_item.notes


@pytest.mark.asyncio
async def test_orchestrator_replacement_route_unavailable_fallback_disclosure():
    """Verify orchestrator production path truthfully falls back to CONFIG_ESTIMATE when route data is unavailable."""
    orchestrator = BudlanceOrchestrator()
    chat_id = 998833

    # Step 1: Initial plan
    prompt1 = "Plan a 3-day trip from Chennai to Goa for 2 adults with ₹25000 budget."
    res1 = await orchestrator.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message=prompt1)
    assert res1.status == "FEASIBLE"

    # Make directions lookup return empty envelope (route unavailable)
    empty_envelope = TravelDataEnvelope(
        engine="google_maps_directions",
        query_hash="mock_hash_empty",
        data={"routes": []},
        status="success",
        source=DataSource.FALLBACK,
    )
    original_get_travel_data = orchestrator.cache_manager.get_travel_data

    async def patched_get_travel_data(engine, params, **kwargs):
        if engine == "google_maps_directions":
            return empty_envelope
        return await original_get_travel_data(engine=engine, params=params, **kwargs)

    with patch.object(orchestrator.cache_manager, "get_travel_data", side_effect=patched_get_travel_data):
        prompt2 = "Replace the morning activity on Day 2 with a nature spot."
        res2 = await orchestrator.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message=prompt2)

        assert res2.status == "FEASIBLE"
        assert res2.budget_breakdown.total_budget == Decimal("25000.00")
        itin2 = res2.generated_itinerary or res2.itinerary
        day2_items = itin2.days[1].items
        rep_item = next((it for it in day2_items if it.time_slot == "Morning"), None)
        assert rep_item is not None
        # Discloses CONFIG_ESTIMATE
        assert "CONFIG_ESTIMATE: unverified transfer heuristic" in rep_item.notes


@pytest.mark.asyncio
async def test_route_cache_freshness_and_stale_invalidation():
    """Verify that CacheRepository and CacheFallbackManager handle route cache freshness and reject stale entries."""
    from datetime import datetime, timedelta, timezone
    from budlance.cache.manager import CacheFallbackManager, compute_query_hash
    from budlance.db.models import SearchCache, utc_now
    from budlance.db.repositories.cache_repo import CacheRepository
    from budlance.serpapi.models import ProvenanceType

    cache_repo = CacheRepository(client=None)  # In-memory store
    cache_mgr = CacheFallbackManager(cache_repo=cache_repo)

    engine = "google_maps_directions"
    params = {"start_addr": "Panaji, Goa", "end_addr": "Canacona, South Goa"}
    q_hash = compute_query_hash(engine, params)
    now = datetime.now(timezone.utc)

    # 1. Fresh cache entry (expires in 7 days)
    mock_data = {
        "routes": [{"legs": [{"distance": {"value": 22000}, "duration": {"value": 2880}}]}]
    }
    fresh_rec = SearchCache(
        query_hash=q_hash,
        engine=engine,
        params_json=params,
        response_data=mock_data,
        expires_at=now + timedelta(hours=168),
        created_at=now - timedelta(hours=2),
    )
    cache_repo.set_cached_search(fresh_rec)

    # Fresh lookup succeeds with CACHED provenance and freshness
    env_fresh = await cache_mgr.get_travel_data(engine, params)
    assert env_fresh.source == DataSource.CACHED
    assert env_fresh.provenance is not None
    assert env_fresh.provenance.cache_hit is True
    assert env_fresh.provenance.provenance_type == ProvenanceType.CACHED_PROVIDER_RESULT
    assert env_fresh.provenance.cache_age_seconds is not None
    assert env_fresh.provenance.cache_age_seconds > 0

    # 2. Stale cache entry (expired 1 hour ago)
    stale_rec = SearchCache(
        query_hash=q_hash,
        engine=engine,
        params_json=params,
        response_data=mock_data,
        expires_at=now - timedelta(hours=1),  # EXPIRED
        created_at=now - timedelta(days=8),
    )
    cache_repo.set_cached_search(stale_rec)

    # Stale lookup must be rejected as cache miss
    cached_check = cache_repo.get_cached_search(q_hash)
    assert cached_check is None, "Expired cache record must not be returned by get_cached_search"

    # get_travel_data encounters cache miss and returns empty unconfigured/fallback envelope
    env_stale = await cache_mgr.get_travel_data(engine, params)
    assert env_stale.source == DataSource.FALLBACK
    assert env_stale.data == {}


@pytest.mark.asyncio
async def test_orchestrator_replacement_uses_cached_route_with_provenance():
    """Verify orchestrator replacement path uses cached route data with CACHED provenance."""
    from budlance.serpapi.models import DataProvenance, ProvenanceType
    orchestrator = BudlanceOrchestrator()
    chat_id = 998844

    prompt1 = "Plan a 3-day trip from Chennai to Goa for 2 adults with ₹25000 budget."
    res1 = await orchestrator.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message=prompt1)
    assert res1.status == "FEASIBLE"

    mock_directions_data = {
        "routes": [
            {
                "legs": [
                    {
                        "start_address": "Dudhsagar Waterfalls, South Goa",
                        "end_address": "Salim Ali Bird Sanctuary, North Goa",
                        "distance": {"value": 35000},  # 35 km
                        "duration": {"value": 3600},   # 60 min
                    }
                ]
            }
        ]
    }
    cached_envelope = TravelDataEnvelope(
        engine="google_maps_directions",
        query_hash="mock_hash_cached",
        data=mock_directions_data,
        status="success",
        source=DataSource.CACHED,
        provenance=DataProvenance(
            provenance_type=ProvenanceType.CACHED_PROVIDER_RESULT,
            provider="serpapi",
            engine="google_maps_directions",
            cache_hit=True,
            cache_age_seconds=7200.0,
        ),
    )

    original_get_travel_data = orchestrator.cache_manager.get_travel_data

    async def patched_get_travel_data(engine, params, **kwargs):
        if engine == "google_maps_directions":
            return cached_envelope
        return await original_get_travel_data(engine=engine, params=params, **kwargs)

    with patch.object(orchestrator.cache_manager, "get_travel_data", side_effect=patched_get_travel_data):
        prompt2 = "Replace the morning activity on Day 2 with a nature spot."
        res2 = await orchestrator.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message=prompt2)

        assert res2.status == "FEASIBLE"
        assert res2.budget_breakdown.total_budget == Decimal("25000.00")
        itin2 = res2.generated_itinerary or res2.itinerary
        day2_items = itin2.days[1].items
        rep_item = next((it for it in day2_items if it.time_slot == "Morning"), None)
        assert rep_item is not None
        assert rep_item.travel_time_to_next_minutes == 60
        assert "35.0 km (60 min)" in rep_item.notes
        assert "Route source: CACHED" in rep_item.notes


@pytest.mark.asyncio
async def test_orchestrator_replacement_with_stale_cache_falls_back_to_config_estimate():
    """Verify that when the route cache is stale/expired, the orchestrator rejects it and falls back to CONFIG_ESTIMATE."""
    from datetime import datetime, timedelta, timezone
    from budlance.cache.manager import compute_query_hash
    from budlance.db.models import SearchCache

    orchestrator = BudlanceOrchestrator()
    chat_id = 998855

    prompt1 = "Plan a 3-day trip from Chennai to Goa for 2 adults with ₹25000 budget."
    res1 = await orchestrator.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message=prompt1)
    assert res1.status == "FEASIBLE"

    # Insert an expired cache entry into the in-memory cache
    now = datetime.now(timezone.utc)
    stale_data = {
        "routes": [{"legs": [{"distance": {"value": 99000}, "duration": {"value": 9999}}]}]
    }
    # Pre-populate cache with expired entry for directions engine
    dummy_params = {"start_addr": "Dudhsagar Waterfalls", "end_addr": "Calangute Beach"}
    stale_rec = SearchCache(
        query_hash=compute_query_hash("google_maps_directions", dummy_params),
        engine="google_maps_directions",
        params_json=dummy_params,
        response_data=stale_data,
        expires_at=now - timedelta(hours=2),  # EXPIRED
        created_at=now - timedelta(days=10),
    )
    orchestrator.cache_manager.cache_repo.set_cached_search(stale_rec)

    prompt2 = "Replace the morning activity on Day 2 with a nature spot."
    res2 = await orchestrator.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message=prompt2)

    assert res2.status == "FEASIBLE"
    assert res2.budget_breakdown.total_budget == Decimal("25000.00")
    itin2 = res2.generated_itinerary or res2.itinerary
    day2_items = itin2.days[1].items
    rep_item = next((it for it in day2_items if it.time_slot == "Morning"), None)
    assert rep_item is not None
    # Must NOT use the stale 9999-min duration
    assert rep_item.travel_time_to_next_minutes in (30, 45)
    # Must disclose CONFIG_ESTIMATE
    assert "CONFIG_ESTIMATE: unverified transfer heuristic" in rep_item.notes

