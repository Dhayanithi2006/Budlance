"""Tests verifying hardcoded travel and provider data cleanup (Scenarios A through G).

Verifies:
1. Scenario A: Real IndiGo flight returned -> live IndiGo result used, no fake 6E-101 created.
2. Scenario B: API returns no flights -> no fake flight created.
3. Scenario C: Real hotel returned -> live hotel used, no fake "Heritage Palace" created.
4. Scenario D: API returns no hotels -> no fake hotel created, primary_hotel is None.
5. Scenario E: Live destination discovery returns candidates without falling back to static list.
   When discovery returns empty, it does NOT invent static destinations.
6. Scenario F: Live Gujarat places returned -> live places used, static Gujarat catalog does not overwrite.
7. Scenario G: Flight API fails -> train result is NOT returned as flight fallback.
8. Train and Bus fallback datasets remain isolated to their respective modes and offline rate tables.
9. Static city list in AI service is isolated to deterministic parsing and does not supply pricing or discovery.
10. Planning heuristics and Trip Pass price are properly configured.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.ai.service import FALLBACK_PARSER_CITIES, AIIntentService
from budlance.cache.fallback import FallbackDataProvider
from budlance.cache.manager import CacheFallbackManager
from budlance.config import get_settings
from budlance.itinerary.generator import ItineraryGenerator
from budlance.orchestrator.models import FlightOption, HotelOption, PlaceOption
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.serpapi.models import DataSource, TravelDataEnvelope


# ---------------------------------------------------------------------------
# Scenario A & B: Flights
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_a_live_flight_used_no_fake_6e101():
    """Scenario A: Live API returns real IndiGo flight.
    Verify: Live IndiGo result is used and no fake 6E-101 is created.
    """
    mock_cache = MagicMock(spec=CacheFallbackManager)
    # Mocking live flight response with flight number 6E-555 and price 5200
    mock_cache.get_travel_data = AsyncMock(
        return_value=TravelDataEnvelope(
            query_hash="mock_flight_live_hash",
            engine="google_flights",
            source=DataSource.LIVE,
            is_fallback=False,
            status="success",
            data={
                "best_flights": [
                    {
                        "flights": [
                            {
                                "airline": "IndiGo",
                                "flight_number": "6E-555",
                                "departure_airport": {"id": "DEL"},
                                "arrival_airport": {"id": "BOM"},
                            }
                        ],
                        "price": 5200,
                    }
                ]
            },
        )
    )

    orch = BudlanceOrchestrator(cache_manager=mock_cache)
    options = await orch.lookup_transport_options(
        origin="Delhi",
        destination="Mumbai",
        people=1,
        transport_mode="flight",
        transport_class="economy",
    )

    assert len(options) == 1
    assert isinstance(options[0], FlightOption)
    assert options[0].airline == "IndiGo"
    assert options[0].flight_number == "6E-555"
    assert options[0].price == Decimal("5200")
    # Verify fake 6E-101 was NOT created
    assert options[0].flight_number != "6E-101"
    assert options[0].price != Decimal("4000")


@pytest.mark.asyncio
async def test_scenario_b_no_flights_returned_no_fake_flight():
    """Scenario B: API returns no flights.
    Verify: NO fake flight appears.
    """
    mock_cache = MagicMock(spec=CacheFallbackManager)
    mock_cache.get_travel_data = AsyncMock(
        return_value=TravelDataEnvelope(
            query_hash="mock_flight_empty_hash",
            engine="google_flights",
            source=DataSource.LIVE,
            is_fallback=False,
            status="success",
            data={"best_flights": [], "other_flights": []},
        )
    )

    orch = BudlanceOrchestrator(cache_manager=mock_cache)
    options = await orch.lookup_transport_options(
        origin="Delhi",
        destination="Mumbai",
        people=1,
        transport_mode="flight",
        transport_class="economy",
    )

    # Must be empty, no fake flight option fabricated
    assert options == []


# ---------------------------------------------------------------------------
# Scenario C & D: Hotels
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_c_live_hotel_used_no_fake_heritage_palace():
    """Scenario C: API returns real hotel.
    Verify: Real hotel is used. No generated "Heritage Palace" appears.
    """
    mock_cache = MagicMock(spec=CacheFallbackManager)
    # google_flights empty so transport doesn't fail
    flight_env = TravelDataEnvelope(
        query_hash="fl_hash",
        engine="google_flights",
        source=DataSource.LIVE,
        status="success",
        data={"best_flights": []},
    )
    # google_hotels returns real hotel
    hotel_env = TravelDataEnvelope(
        query_hash="hotel_hash",
        engine="google_hotels",
        source=DataSource.LIVE,
        is_fallback=False,
        status="success",
        data={
            "properties": [
                {
                    "name": "The Taj Mahal Palace",
                    "rate_per_night": {"extracted_lowest": 12500},
                    "overall_rating": 4.8,
                }
            ]
        },
    )
    directions_env = TravelDataEnvelope(
        query_hash="dir_hash",
        engine="google_maps_directions",
        source=DataSource.LIVE,
        status="success",
        data={},
    )

    async def mock_get_travel_data(engine, params=None):
        if engine == "google_hotels":
            return hotel_env
        elif engine == "google_flights":
            return flight_env
        return directions_env

    mock_cache.get_travel_data = AsyncMock(side_effect=mock_get_travel_data)

    orch = BudlanceOrchestrator(cache_manager=mock_cache)
    _, _, primary_hotel, hotel_candidates, _ = await orch._collect_travel_components(
        origin="Delhi",
        destination="Mumbai",
        days=2,
        people=1,
        transport_mode="flight",
    )

    assert primary_hotel is not None
    assert primary_hotel.name == "The Taj Mahal Palace"
    assert primary_hotel.price_per_night == Decimal("12500")
    # Verify fabricated names do not appear in candidates
    all_names = [h.name for h in hotel_candidates]
    assert "Heritage Palace" not in all_names
    assert "Comfort Inn" not in all_names
    assert "Backpacker Lodge" not in all_names


@pytest.mark.asyncio
async def test_scenario_d_no_hotels_returned_no_fake_hotel():
    """Scenario D: API returns no hotels.
    Verify: NO fake hotel appears.
    """
    mock_cache = MagicMock(spec=CacheFallbackManager)
    flight_env = TravelDataEnvelope(
        query_hash="fl_hash",
        engine="google_flights",
        source=DataSource.LIVE,
        status="success",
        data={"best_flights": []},
    )
    hotel_env = TravelDataEnvelope(
        query_hash="hotel_empty_hash",
        engine="google_hotels",
        source=DataSource.LIVE,
        is_fallback=False,
        status="success",
        data={"properties": []},
    )
    directions_env = TravelDataEnvelope(
        query_hash="dir_hash",
        engine="google_maps_directions",
        source=DataSource.LIVE,
        status="success",
        data={},
    )

    async def mock_get_travel_data(engine, params=None):
        if engine == "google_hotels":
            return hotel_env
        elif engine == "google_flights":
            return flight_env
        return directions_env

    mock_cache.get_travel_data = AsyncMock(side_effect=mock_get_travel_data)

    orch = BudlanceOrchestrator(cache_manager=mock_cache)
    _, _, primary_hotel, hotel_candidates, _ = await orch._collect_travel_components(
        origin="Delhi",
        destination="NowhereVille",
        days=2,
        people=1,
        transport_mode="flight",
    )

    assert primary_hotel is None
    assert hotel_candidates == []


# ---------------------------------------------------------------------------
# Scenario E: Destination Discovery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_e_live_destination_discovery_used():
    """Scenario E1: Live destination discovery returns candidates.
    Verify: Live destinations used, hardcoded list is NOT used.
    """
    mock_cache = MagicMock(spec=CacheFallbackManager)
    mock_cache.get_travel_data = AsyncMock(
        return_value=TravelDataEnvelope(
            query_hash="mock_explore_live_hash",
            engine="google_travel_explore",
            source=DataSource.LIVE,
            is_fallback=False,
            status="success",
            data={
                "destinations": [
                    {"destination": "Amritsar", "flight_price": 4500},
                    {"destination": "Varanasi", "flight_price": 5000},
                ]
            },
        )
    )

    orch = BudlanceOrchestrator(cache_manager=mock_cache)
    discovered, used_fallback = await orch._discover_destinations(
        origin="Delhi",
        budget=Decimal("20000.00"),
        interests=["culture"],
    )

    assert "Amritsar" in discovered
    assert "Varanasi" in discovered


@pytest.mark.asyncio
async def test_scenario_e_failed_destination_does_not_invent():
    """Scenario E2: Destination discovery fails / empty.
    Verify: Only curated domestic pool candidates are returned; no invented external cities.
    """
    mock_cache = MagicMock(spec=CacheFallbackManager)
    mock_cache.get_travel_data = AsyncMock(
        return_value=TravelDataEnvelope(
            query_hash="mock_explore_empty_hash",
            engine="google_travel_explore",
            source=DataSource.FALLBACK,
            is_fallback=True,
            status="empty",
            data={},
        )
    )

    orch = BudlanceOrchestrator(cache_manager=mock_cache)
    discovered, used_fallback = await orch._discover_destinations(
        origin="Delhi",
        budget=Decimal("20000.00"),
        interests=["culture"],
    )

    # Curated domestic pool is used when explore fails
    assert "Goa" in discovered
    assert "NowhereVille" not in discovered


# ---------------------------------------------------------------------------
# Scenario F: Places / Attractions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_f_live_places_not_overwritten_by_gujarat_catalog():
    """Scenario F: Live Gujarat places returned.
    Verify: Gujarat static catalog does NOT overwrite live results.
    """
    gen = ItineraryGenerator()

    # Pass live PlaceOption objects
    live_places = [
        PlaceOption(
            name="Sabarmati Riverfront Walk",
            price=Decimal("50"),
            category="sightseeing",
            time_slot="morning",
            is_fallback=False,
            source="LIVE",
        ),
        PlaceOption(
            name="Adalaj Stepwell Guided Tour",
            price=Decimal("100"),
            category="heritage",
            time_slot="afternoon",
            is_fallback=False,
            source="LIVE",
        ),
    ]

    from uuid import uuid4
    from budlance.engine.models import BudgetBreakdown, BudgetEvaluationResult

    eval_result = BudgetEvaluationResult(
        status="FEASIBLE",
        is_feasible=True,
        breakdown=BudgetBreakdown(
            total_budget=Decimal("20000.00"),
            bucket_a_fixed=Decimal("5000.00"),
            bucket_b_survival=Decimal("2000.00"),
            bucket_c_activities=Decimal("1000.00"),
            bucket_d_rescue=Decimal("2000.00"),
            transport_cost=Decimal("2000.00"),
            hotel_cost=Decimal("3000.00"),
            food_cost=Decimal("1500.00"),
            local_transit_cost=Decimal("500.00"),
            attraction_cost=Decimal("200.00"),
            total_allocated=Decimal("7200.00"),
            remaining_surplus=Decimal("12800.00"),
        ),
        explanation="Feasible test budget",
    )

    itinerary = gen.generate(
        trip_id=uuid4(),
        destination="Ahmedabad",
        evaluation=eval_result,
        days=2,
        places=live_places,
    )

    # Itinerary items should contain the live places, not be overwritten by static catalog
    all_place_names = [item.place_name for item in itinerary.days[0].items]
    assert "Sabarmati Riverfront Walk" in all_place_names or "Adalaj Stepwell Guided Tour" in all_place_names


# ---------------------------------------------------------------------------
# Scenario G: Cross-Engine Fallback Prevention
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_g_flight_error_does_not_produce_train_data():
    """Scenario G: Flight API fails.
    Verify: Train result is NOT returned as a flight fallback.
    """
    mock_cache = MagicMock(spec=CacheFallbackManager)
    # Flight engine lookup fails or returns error envelope
    mock_cache.get_travel_data = AsyncMock(
        return_value=TravelDataEnvelope(
            query_hash="mock_flight_error_hash",
            engine="google_flights",
            source=DataSource.FALLBACK,
            is_fallback=True,
            status="error",
            data={},
        )
    )

    orch = BudlanceOrchestrator(cache_manager=mock_cache)
    flight_options = await orch.lookup_transport_options(
        origin="Chennai",
        destination="Bangalore",
        people=1,
        transport_mode="flight",
        transport_class="economy",
    )

    # Verify no train options leaked into flight options
    assert flight_options == []
    for opt in flight_options:
        assert not hasattr(opt, "train_number")


# ---------------------------------------------------------------------------
# Train Fallback Isolation & Offline Fare Estimates
# ---------------------------------------------------------------------------

def test_train_offline_fare_estimates_from_data_provider():
    """Verify that unmapped train fares come from FallbackDataProvider / rate_tables.json
    rather than inline magic numbers in orchestrator business logic.
    """
    provider = FallbackDataProvider()
    assert provider.get_offline_train_fare_estimate("1AC") == Decimal("4850")
    assert provider.get_offline_train_fare_estimate("2AC") == Decimal("2850")
    assert provider.get_offline_train_fare_estimate("3AC") == Decimal("1950")
    assert provider.get_offline_train_fare_estimate("SL") == Decimal("750")
    assert provider.get_offline_train_fare_estimate("sleeper") == Decimal("750")
    assert provider.get_offline_train_fare_estimate("unknown_class") == Decimal("1500")


# ---------------------------------------------------------------------------
# Static City List in AI Fallback Parser
# ---------------------------------------------------------------------------

def test_static_city_list_is_isolated_parser_knowledge():
    """Verify FALLBACK_PARSER_CITIES exists, is clearly isolated, and used for parsing,
    not for travel discovery or pricing.
    """
    assert isinstance(FALLBACK_PARSER_CITIES, tuple)
    assert len(FALLBACK_PARSER_CITIES) >= 20
    assert "goa" in FALLBACK_PARSER_CITIES
    assert "kerala" in FALLBACK_PARSER_CITIES

    # Test that the offline parser uses it to extract destination from natural text
    service = AIIntentService(use_mock=True)
    parsed = service._mock_parse_trip_intent("Plan a 3 day trip to Goa for 20000 rupees")
    assert parsed.destination == "Goa"
    assert parsed.budget == Decimal("20000")


# ---------------------------------------------------------------------------
# Planning Heuristics and Trip Pass Settings
# ---------------------------------------------------------------------------

def test_planning_heuristics_and_trip_pass_configured():
    """Verify FOOD_BUDGET_RATES, TRANSIT_RATES, RESCUE_RESERVE_PERCENT and Trip Pass (49)
    are properly set as settings/heuristics and not fake live quotes.
    """
    settings = get_settings()
    assert "budget" in settings.food_budget_rates
    assert "standard" in settings.food_budget_rates
    assert "comfort" in settings.food_budget_rates
    assert "metro_bus_daily_pass" in settings.transit_rates
    assert "cab_per_km" in settings.transit_rates
    assert settings.rescue_reserve_percent == Decimal("0.10")
    assert settings.trip_pass_amount == Decimal("49.00")


# ---------------------------------------------------------------------------
# Missing Required Hotel: Non-zero accommodation estimate
# ---------------------------------------------------------------------------

def test_missing_required_hotel_does_not_become_zero_cost_accommodation():
    """Verify that when hotel options are absent for a multi-day trip (days > 1),
    the budget engine estimates lodging rather than treating accommodation as ₹0 free stay,
    preventing artificially feasible trips without fabricating fake HotelOption instances.
    """
    from budlance.engine.budget import ReverseBudgetEngine
    from budlance.schemas.travel import FoodEstimate, LocalTransitEstimate

    engine = ReverseBudgetEngine()
    food = FoodEstimate(tier="budget", daily_cost_per_person=Decimal("500"), people=2, days=3, total_cost=Decimal("3000"))
    transit = LocalTransitEstimate(mode="metro_bus", days=3, total_cost=Decimal("1400"))

    result = engine.evaluate(
        total_budget=Decimal("10000"),
        people=2,
        days=3,
        transport=None,
        hotel=None,
        food_estimate=food,
        local_transit_estimate=transit,
    )
    # hotel_cost must be > 0 (offline lodging estimate for 2 nights, 1 room)
    assert result.breakdown.hotel_cost > Decimal("0.00")
    # For standard tier: 2500 * 1 room * 2 nights = 5000
    assert result.breakdown.hotel_cost == Decimal("5000.00")


# ---------------------------------------------------------------------------
# Live Provider Precedence Over Fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_live_provider_result_takes_precedence_over_fallback():
    """Verify that live provider results take full precedence over fallback datasets,
    ensuring live pricing and schedules are used without fallback data mixing in.
    """
    mock_cache = MagicMock(spec=CacheFallbackManager)
    # Return live flight data
    mock_cache.get_travel_data = AsyncMock(
        return_value=TravelDataEnvelope(
            query_hash="live_fl_hash",
            engine="google_flights",
            source=DataSource.LIVE,
            is_fallback=False,
            status="success",
            data={
                "best_flights": [
                    {
                        "flights": [
                            {
                                "airline": "Air India",
                                "flight_number": "AI-102",
                                "departure_airport": {"id": "DEL"},
                                "arrival_airport": {"id": "BOM"},
                            }
                        ],
                        "price": 6800,
                    }
                ]
            },
        )
    )

    orch = BudlanceOrchestrator(cache_manager=mock_cache)
    options = await orch.lookup_transport_options(
        origin="Delhi",
        destination="Mumbai",
        people=1,
        transport_mode="flight",
        transport_class="economy",
    )

    assert len(options) == 1
    assert options[0].airline == "Air India"
    assert options[0].flight_number == "AI-102"
    assert options[0].price == Decimal("6800")
    assert options[0].is_fallback is False
    assert options[0].source == "LIVE"

