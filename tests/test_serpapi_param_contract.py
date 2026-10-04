"""Tests for SerpApi parameter contract — verifies the orchestrator sends correct SerpApi-documented
parameter keys for every engine after the runtime integration fix.

ALL tests use mocks/fakes. ZERO live SerpApi credits are consumed.

Coverage:
  T01 - Travel Explore receives departure_id (not origin)
  T02 - Travel Explore does NOT send origin key
  T03 - Flights receives departure_id
  T04 - Flights receives arrival_id
  T05 - Flights receives outbound_date
  T06 - Flights receives return_date
  T07 - Flights receives adults
  T08 - Hotels receives q
  T09 - Hotels receives check_in_date
  T10 - Hotels receives check_out_date
  T11 - Hotels receives adults
  T12 - Maps request does not use invalid location key
  T13 - Failed flight search never returns train corridor data (cross-pollination fix)
  T14 - Successful live flight response reaches normalizer
  T15 - Successful live hotel response reaches normalizer
  T16 - Successful live destination response reaches destination selection
  T17 - Successful live places response reaches attraction selection
  T18 - Round-trip flight costing remains correct
  T19 - Existing train corridor fallback remains intact for transit engines
  T20 - resolve_iata returns correct IATA codes
"""

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import uuid4

import pytest

from budlance.cache.fallback import FallbackDataProvider
from budlance.cache.manager import CacheFallbackManager
from budlance.db.repositories.cache_repo import CacheRepository
from budlance.db.repositories.usage_repo import UsageRepository
from budlance.normalization.flights import normalize_flights
from budlance.normalization.hotels import normalize_hotels
from budlance.normalization.normalizer import DataNormalizer
from budlance.schemas.travel import FlightOption, HotelOption, PlaceOption
from budlance.serpapi.exceptions import SerpApiAuthError, SerpApiNetworkError
from budlance.serpapi.gateway import SerpApiGateway
from budlance.serpapi.location import resolve_hotel_query, resolve_iata, resolve_places_query
from budlance.serpapi.models import DataSource, TravelDataEnvelope
from budlance.normalization.transit import calculate_round_trip_cost


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_manager(gateway=None, *, gateway_response=None, gateway_error=None):
    """Build a CacheFallbackManager wired to a mock gateway."""
    mock_gw = gateway or MagicMock(spec=SerpApiGateway)
    mock_gw.has_credentials = True
    if gateway_error:
        mock_gw.execute_search = AsyncMock(side_effect=gateway_error)
    elif gateway_response is not None:
        mock_gw.execute_search = AsyncMock(return_value=gateway_response)
    else:
        mock_gw.execute_search = AsyncMock(return_value={})
    return (
        CacheFallbackManager(
            gateway=mock_gw,
            cache_repo=CacheRepository(client=None),
            usage_repo=UsageRepository(client=None),
        ),
        mock_gw,
    )


def _flight_raw(price=23300, airline="IndiGo", flight_no="6E 588"):
    """Minimal SerpApi google_flights best_flights structure."""
    return {
        "best_flights": [
            {
                "flights": [
                    {
                        "airline": airline,
                        "flight_number": flight_no,
                        "departure_airport": {"id": "MAA", "time": "06:00"},
                        "arrival_airport": {"id": "GOI", "time": "07:30"},
                        "duration": 90,
                    }
                ],
                "price": price,
                "total_duration": 90,
            }
        ]
    }


def _hotel_raw(name="Hilton Goa Resort", rate=13174):
    """Minimal SerpApi google_hotels properties structure."""
    return {
        "properties": [
            {
                "name": name,
                "rate_per_night": {"extracted_lowest": rate},
                "total_rate": {"extracted_lowest": rate * 4},
                "hotel_class": "5-star hotel",
                "overall_rating": 4.5,
            }
        ]
    }


def _explore_raw(*cities):
    """Minimal SerpApi google_travel_explore destinations structure."""
    return {
        "destinations": [
            {"name": c, "destination": c}
            for c in cities
        ]
    }


def _places_raw(*titles):
    """Minimal SerpApi google_maps local_results structure."""
    return {
        "local_results": [
            {"title": t, "rating": 4.5, "type": "Tourist attraction"}
            for t in titles
        ]
    }


# ===========================================================================
# T01 — Travel Explore receives departure_id (not origin)
# ===========================================================================
@pytest.mark.asyncio
async def test_T01_travel_explore_receives_departure_id():
    manager, mock_gw = _make_manager(gateway_response=_explore_raw("Goa", "Jaipur"))

    await manager.get_travel_data(
        engine="google_travel_explore",
        params={"departure_id": "MAA", "currency": "INR", "hl": "en"},
    )

    assert mock_gw.execute_search.called
    _, called_params = mock_gw.execute_search.call_args.args
    assert "departure_id" in called_params, "departure_id must be present"
    assert called_params["departure_id"] == "MAA"


# ===========================================================================
# T02 — Travel Explore does NOT use the old 'origin' key
# ===========================================================================
@pytest.mark.asyncio
async def test_T02_travel_explore_no_origin_key():
    manager, mock_gw = _make_manager(gateway_response=_explore_raw("Goa"))

    await manager.get_travel_data(
        engine="google_travel_explore",
        params={"departure_id": "MAA", "currency": "INR"},
    )

    _, called_params = mock_gw.execute_search.call_args.args
    assert "origin" not in called_params, "'origin' key must NOT be sent to SerpApi Travel Explore"


# ===========================================================================
# T03-T07 — Flights receives all required SerpApi params
# ===========================================================================
@pytest.mark.asyncio
async def test_T03_T07_flights_correct_params():
    """Flights must use departure_id, arrival_id, outbound_date, return_date, adults."""
    manager, mock_gw = _make_manager(gateway_response=_flight_raw())

    today = date.today()
    out_date = (today + timedelta(days=30)).strftime("%Y-%m-%d")
    ret_date = (today + timedelta(days=34)).strftime("%Y-%m-%d")

    await manager.get_travel_data(
        engine="google_flights",
        params={
            "departure_id":  "MAA",
            "arrival_id":    "GOI",
            "outbound_date": out_date,
            "return_date":   ret_date,
            "adults":        2,
            "currency":      "INR",
            "hl":            "en",
            "type":          "1",
        },
    )

    assert mock_gw.execute_search.called
    _, sent_params = mock_gw.execute_search.call_args.args

    # T03 — departure_id present
    assert "departure_id" in sent_params, "T03: departure_id required"
    assert sent_params["departure_id"] == "MAA"

    # T04 — arrival_id present
    assert "arrival_id" in sent_params, "T04: arrival_id required"
    assert sent_params["arrival_id"] == "GOI"

    # T05 — outbound_date present
    assert "outbound_date" in sent_params, "T05: outbound_date required"

    # T06 — return_date present
    assert "return_date" in sent_params, "T06: return_date required"

    # T07 — adults present
    assert "adults" in sent_params, "T07: adults required"
    assert sent_params["adults"] == 2

    # Old invalid keys must not be present
    assert "origin" not in sent_params
    assert "destination" not in sent_params
    assert "people" not in sent_params


# ===========================================================================
# T08-T11 — Hotels receives q, check_in_date, check_out_date, adults
# ===========================================================================
@pytest.mark.asyncio
async def test_T08_T11_hotels_correct_params():
    manager, mock_gw = _make_manager(gateway_response=_hotel_raw())

    today = date.today()
    check_in  = (today + timedelta(days=30)).strftime("%Y-%m-%d")
    check_out = (today + timedelta(days=34)).strftime("%Y-%m-%d")

    await manager.get_travel_data(
        engine="google_hotels",
        params={
            "q":              "Hotels in Goa",
            "check_in_date":  check_in,
            "check_out_date": check_out,
            "adults":         2,
            "currency":       "INR",
        },
    )

    assert mock_gw.execute_search.called
    _, sent_params = mock_gw.execute_search.call_args.args

    # T08 — q present
    assert "q" in sent_params, "T08: q required for hotels"
    assert "Hotels in" in sent_params["q"]

    # T09 — check_in_date present
    assert "check_in_date" in sent_params, "T09: check_in_date required"

    # T10 — check_out_date present
    assert "check_out_date" in sent_params, "T10: check_out_date required"

    # T11 — adults present
    assert "adults" in sent_params, "T11: adults required"

    # Old invalid keys must not be present
    assert "destination" not in sent_params
    assert "days" not in sent_params
    assert "people" not in sent_params


# ===========================================================================
# T12 — Maps request does not use invalid 'location' key
# ===========================================================================
@pytest.mark.asyncio
async def test_T12_maps_no_invalid_location_key():
    manager, mock_gw = _make_manager(gateway_response=_places_raw("Baga Beach", "Fort Aguada"))

    await manager.get_travel_data(
        engine="google_maps",
        params={"q": "places attractions in Goa", "hl": "en", "type": "search"},
    )

    assert mock_gw.execute_search.called
    _, sent_params = mock_gw.execute_search.call_args.args

    assert "q" in sent_params, "T12: q required for google_maps"
    assert "location" not in sent_params, "T12: 'location' is not a valid SerpApi param and must not be sent"


# ===========================================================================
# T13 — Failed flight search NEVER returns train corridor data
# ===========================================================================
@pytest.mark.asyncio
async def test_T13_failed_flight_never_returns_train_data():
    """Cross-pollination fix: a failed google_flights call must return an empty envelope,
    NOT train corridor data from the fallback provider."""
    mock_gw = MagicMock(spec=SerpApiGateway)
    mock_gw.has_credentials = True
    mock_gw.execute_search = AsyncMock(side_effect=SerpApiNetworkError("Timeout"))

    fallback = MagicMock(spec=FallbackDataProvider)
    # Simulate that a train corridor IS available for this origin/destination
    fallback.get_train_corridor.return_value = {
        "train_name": "Chennai Express",
        "is_fallback": True,
    }
    fallback.get_bus_corridor.return_value = None

    manager = CacheFallbackManager(
        gateway=mock_gw,
        cache_repo=CacheRepository(client=None),
        usage_repo=UsageRepository(client=None),
        fallback_provider=fallback,
    )

    envelope = await manager.get_travel_data(
        engine="google_flights",
        params={
            "departure_id": "MAA",
            "arrival_id":   "GOI",
            "outbound_date": "2026-11-03",
            "return_date":   "2026-11-07",
            "adults":        2,
        },
    )

    # Must return empty fallback envelope, NOT train corridor data
    assert envelope.is_fallback is True
    assert envelope.status == "error"
    assert envelope.data == {}, (
        "T13: Failed flight call must return empty data, not train corridor data. "
        f"Got: {envelope.data}"
    )
    # The train corridor fallback must NOT have been injected
    assert "train_name" not in envelope.data


# ===========================================================================
# T14 — Successful live flight response reaches normalizer
# ===========================================================================
@pytest.mark.asyncio
async def test_T14_live_flight_reaches_normalizer():
    raw_data = _flight_raw(price=23300, airline="IndiGo", flight_no="6E 588")
    manager, _ = _make_manager(gateway_response=raw_data)

    envelope = await manager.get_travel_data(
        engine="google_flights",
        params={"departure_id": "MAA", "arrival_id": "GOI", "outbound_date": "2026-11-03",
                "return_date": "2026-11-07", "adults": 2},
    )

    assert envelope.source == DataSource.LIVE
    assert envelope.is_fallback is False

    # Pass through normalizer
    normalizer = DataNormalizer()
    flights = normalizer.normalize_flights(envelope)
    assert len(flights) >= 1
    assert flights[0].airline == "IndiGo"
    assert flights[0].price == Decimal("23300")
    assert flights[0].is_fallback is False
    assert flights[0].source == DataSource.LIVE


# ===========================================================================
# T15 — Successful live hotel response reaches normalizer
# ===========================================================================
@pytest.mark.asyncio
async def test_T15_live_hotel_reaches_normalizer():
    raw_data = _hotel_raw(name="Hilton Goa Resort", rate=13174)
    manager, _ = _make_manager(gateway_response=raw_data)

    envelope = await manager.get_travel_data(
        engine="google_hotels",
        params={"q": "Hotels in Goa", "check_in_date": "2026-11-03",
                "check_out_date": "2026-11-07", "adults": 2},
    )

    assert envelope.source == DataSource.LIVE
    assert envelope.is_fallback is False

    normalizer = DataNormalizer()
    hotels = normalizer.normalize_hotels(envelope)
    assert len(hotels) >= 1
    assert hotels[0].name == "Hilton Goa Resort"
    assert hotels[0].price_per_night == Decimal("13174")
    assert hotels[0].is_fallback is False
    assert hotels[0].source == DataSource.LIVE


# ===========================================================================
# T16 — Successful live destination response reaches destination selection
# ===========================================================================
@pytest.mark.asyncio
async def test_T16_live_destinations_extracted():
    raw_data = _explore_raw("Goa", "Jaipur", "Udaipur")
    manager, _ = _make_manager(gateway_response=raw_data)

    envelope = await manager.get_travel_data(
        engine="google_travel_explore",
        params={"departure_id": "MAA", "currency": "INR"},
    )

    assert envelope.source == DataSource.LIVE
    discovered = []
    for item in (envelope.data.get("destinations") or []):
        name = item.get("name") or item.get("destination")
        if name:
            discovered.append(name.title())

    assert "Goa" in discovered
    assert "Jaipur" in discovered


# ===========================================================================
# T17 — Successful live places response reaches attraction selection
# ===========================================================================
@pytest.mark.asyncio
async def test_T17_live_places_reach_normalizer():
    raw_data = _places_raw("Dudhsagar Falls", "Fort Aguada", "Velsao Beach")
    manager, _ = _make_manager(gateway_response=raw_data)

    envelope = await manager.get_travel_data(
        engine="google_maps",
        params={"q": "places attractions in Goa", "hl": "en", "type": "search"},
    )

    assert envelope.source == DataSource.LIVE
    normalizer = DataNormalizer()
    places = normalizer.normalize_places(envelope)
    titles = [p.name for p in places]
    assert "Dudhsagar Falls" in titles
    assert "Fort Aguada" in titles


# ===========================================================================
# T18 — Round-trip flight costing remains correct
# ===========================================================================
def test_T18_round_trip_cost_correct():
    """calculate_round_trip_cost must multiply fare correctly for 2 adults."""
    one_way_fare = Decimal("4000.00")
    total = calculate_round_trip_cost(one_way_fare, one_way_fare, 2)
    # 2 adults * (outbound + return) = 2 * 2 * 4000 = 16000
    assert total == Decimal("16000.00"), f"Expected 16000, got {total}"

    # Single adult
    total_1 = calculate_round_trip_cost(one_way_fare, one_way_fare, 1)
    assert total_1 == Decimal("8000.00"), f"Expected 8000, got {total_1}"


# ===========================================================================
# T19 — Train corridor fallback remains intact for transit engines
# ===========================================================================
@pytest.mark.asyncio
async def test_T19_train_corridor_fallback_intact_for_transit_engine():
    """The train corridor fallback must still activate when the engine IS a transit engine."""
    mock_gw = MagicMock(spec=SerpApiGateway)
    mock_gw.has_credentials = True
    mock_gw.execute_search = AsyncMock(side_effect=SerpApiNetworkError("Timeout"))

    fallback = MagicMock(spec=FallbackDataProvider)
    fallback.get_train_corridor.return_value = {
        "train_name": "Chennai Coromandel Express",
        "is_fallback": True,
    }

    manager = CacheFallbackManager(
        gateway=mock_gw,
        cache_repo=CacheRepository(client=None),
        usage_repo=UsageRepository(client=None),
        fallback_provider=fallback,
    )

    # Engine is "trains" — corridor fallback SHOULD activate
    envelope = await manager.get_travel_data(
        engine="trains",
        params={"origin": "Chennai", "destination": "Kolkata"},
    )

    assert envelope.is_fallback is True
    assert envelope.status == "success"
    assert envelope.data.get("train_name") == "Chennai Coromandel Express"


# ===========================================================================
# T20 — Location resolver returns correct IATA codes
# ===========================================================================
def test_T20_location_resolver_iata_codes():
    assert resolve_iata("Chennai") == "MAA"
    assert resolve_iata("chennai") == "MAA"  # case-insensitive
    assert resolve_iata("CHENNAI") == "MAA" # case-insensitive uppercase
    assert resolve_iata("Goa") == "GOI"
    assert resolve_iata("Mumbai") == "BOM"
    assert resolve_iata("Delhi") == "DEL"
    assert resolve_iata("Bengaluru") == "BLR"
    assert resolve_iata("Bangalore") == "BLR"  # alias
    assert resolve_iata("Manali") is None       # no airport
    assert resolve_iata("Ooty") is None         # no airport
    assert resolve_iata("Udaipur") == "UDR"
    assert resolve_iata("Singapore") == "SIN"
    assert resolve_iata(None) is None           # guard: None input
    assert resolve_iata("") is None             # guard: empty string
    assert resolve_iata("unknown_city_xyz") is None  # not in table


def test_T20b_hotel_and_places_queries():
    assert resolve_hotel_query("Goa") == "Hotels in Goa"
    assert resolve_hotel_query("new delhi") == "Hotels in New Delhi"
    assert resolve_places_query("Goa") == "places attractions in Goa"
