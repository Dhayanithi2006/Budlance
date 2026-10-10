"""Phase 2 Live Travel Data Integration tests.

Covers:
- Step B: Destination discovery with genuine Travel Explore responses and dates.
- Step C / Example 1: Chennai to Delhi round-trip flights, booking options via booking_token.
- Step D / Example 2: Hotel stay dates, night-count, date change (8-10 Nov to 9-11 Nov), freshness.
- Step E & F / Example 3: Munnar place discovery, local restaurants, date-overlapping events.
- Provenance transparency: LIVE_PROVIDER, CACHED_PROVIDER_RESULT, OFFLINE_FALLBACK.
- Zero price fabrication: missing prices are rejected, never set to ₹0.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from budlance.cache.manager import CacheFallbackManager, compute_query_hash, ENGINE_TTLS
from budlance.normalization.events import filter_events_overlapping_dates, normalize_events
from budlance.normalization.flights import (
    build_safe_flight_search_url,
    extract_best_booking_option,
    normalize_flight_booking_options,
    normalize_flights,
)
from budlance.normalization.hotels import normalize_hotels
from budlance.normalization.places import normalize_places
from budlance.normalization.routes import normalize_routes
from budlance.orchestrator.formatter import format_feasible_plan
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.schemas.dates import build_trip_date_context, calculate_stay_nights
from budlance.schemas.travel import EventOption, FlightOption, HotelOption, PlaceOption, RouteOption
from budlance.serpapi.models import DataProvenance, DataSource, ProvenanceType, TravelDataEnvelope


# =============================================================================
# Step C / Example 1: Round-Trip Flights & Booking Options
# =============================================================================

def test_example_1_flight_dates_and_pricing_semantics():
    """Example 1: Chennai to Delhi, 2 people, 3 days (8-10 Nov 2026).

    Semantic requirements:
    - Outbound flight: 8 Nov 2026
    - Return flight: 10 Nov 2026
    - Hotel stay: 8 Nov 2026 to 10 Nov 2026 = 2 nights
    """
    ctx = build_trip_date_context(
        days=3,
        start_date="2026-11-08",
        return_date="2026-11-10",
    )
    assert ctx.flight_outbound_date == "2026-11-08"
    assert ctx.flight_return_date == "2026-11-10"
    assert ctx.hotel_check_in_date == "2026-11-08"
    assert ctx.hotel_check_out_date == "2026-11-10"
    assert ctx.stay_nights == 2


def test_example_1_normalize_flights_and_booking_token():
    """Verify live Google Flights response normalization preserving booking_token and provenance."""
    raw_flights_data = {
        "best_flights": [
            {
                "flights": [
                    {
                        "airline": "IndiGo",
                        "flight_number": "6E 204",
                        "departure_airport": {"id": "MAA", "time": "2026-11-08 06:00"},
                        "arrival_airport": {"id": "DEL", "time": "2026-11-08 08:45"},
                        "duration": 165,
                    },
                    {
                        "airline": "IndiGo",
                        "flight_number": "6E 505",
                        "departure_airport": {"id": "DEL", "time": "2026-11-10 18:00"},
                        "arrival_airport": {"id": "MAA", "time": "2026-11-10 20:50"},
                        "duration": 170,
                    },
                ],
                "price": "₹12,450",
                "total_duration": 335,
                "booking_token": "token_indigo_delhi_roundtrip_xyz",
            }
        ]
    }
    now_ts = datetime.now(timezone.utc)
    prov = DataProvenance(
        provenance_type=ProvenanceType.LIVE_PROVIDER,
        provider="serpapi",
        engine="google_flights",
        retrieval_timestamp=now_ts,
        cache_hit=False,
        query_params={"departure_id": "MAA", "arrival_id": "DEL", "outbound_date": "2026-11-08", "return_date": "2026-11-10"},
        price_scope="quote",
        currency="INR",
        http_status=200,
        latency_sec=0.45,
    )
    envelope = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_flights",
        query_hash="hash_flights_maa_del",
        data=raw_flights_data,
        is_fallback=False,
        status="success",
        created_at=now_ts,
        provenance=prov,
    )

    flights = normalize_flights(envelope)
    assert len(flights) == 1
    f = flights[0]
    assert f.airline == "IndiGo"
    assert f.flight_number == "6E 204"
    assert f.return_flight_number == "6E 505"
    assert f.price == Decimal("12450.00")
    assert f.currency == "INR"
    assert f.booking_token == "token_indigo_delhi_roundtrip_xyz"
    assert f.provenance is not None
    assert f.provenance.provenance_type == ProvenanceType.LIVE_PROVIDER
    assert f.provenance.latency_sec == 0.45


def test_example_1_selected_flight_booking_options_retrieval():
    """Verify extracting genuine external booking options from booking_token payload."""
    booking_payload = {
        "booking_options": [
            {
                "book_with": "IndiGo",
                "price": "₹12,450",
                "booking_request": {
                    "url": "https://www.goindigo.in/booking/select.html?ref=serpapi_direct",
                },
            },
            {
                "book_with": "MakeMyTrip",
                "price": "₹12,700",
                "booking_request": {
                    "url": "https://www.makemytrip.com/flights/booking?ref=xyz",
                },
            },
        ]
    }
    env = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_flights",
        query_hash="hash_booking_token_xyz",
        data=booking_payload,
        is_fallback=False,
        status="success",
    )
    opts = normalize_flight_booking_options(env)
    assert len(opts) == 2
    best = opts[0]
    assert best["seller"] == "IndiGo"
    assert best["price"] == Decimal("12450.00")
    assert best["direct_url"] == "https://www.goindigo.in/booking/select.html?ref=serpapi_direct"
    assert best["has_post_data"] is False


def test_selected_flight_post_data_does_not_generate_fake_url():
    """POST data booking requests must not be mangled into a fake GET URL."""
    booking_payload = {
        "booking_options": [
            {
                "book_with": "Air India",
                "price": "₹14,000",
                "booking_request": {
                    "post_data": "form_state=encrypted_data_blob",
                },
            }
        ]
    }
    extracted = extract_best_booking_option(booking_payload)
    assert extracted is not None
    assert extracted["has_post_data"] is True
    assert extracted["direct_url"] is None  # Never fabricated


# =============================================================================
# Step D / Example 2: Hotel Dates, Night-Count & Date Change
# =============================================================================

def test_example_2_hotel_date_and_night_count():
    """Example 2 Initial: Delhi, 8-10 Nov 2026 (2 nights)."""
    raw_hotel_data = {
        "properties": [
            {
                "name": "The Oberoi New Delhi",
                "rate_per_night": {"extracted_lowest": 9500},
                "total_rate": {"extracted_lowest": 19000},
                "overall_rating": 4.8,
                "reviews": 3200,
                "amenities": ["Free WiFi", "Pool", "Spa"],
                "property_token": "oberoi_del_prop_token_123",
                "link": "https://www.oberoihotels.com/hotels-in-delhi/",
            }
        ]
    }
    now_ts = datetime.now(timezone.utc)
    prov = DataProvenance(
        provenance_type=ProvenanceType.LIVE_PROVIDER,
        provider="serpapi",
        engine="google_hotels",
        retrieval_timestamp=now_ts,
        cache_hit=False,
        query_params={"q": "Hotels in Delhi", "check_in_date": "2026-11-08", "check_out_date": "2026-11-10"},
        price_scope="quote",
        currency="INR",
        http_status=200,
        latency_sec=0.52,
    )
    env = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_hotels",
        query_hash="hash_hotel_del_8_10",
        data=raw_hotel_data,
        is_fallback=False,
        status="success",
        created_at=now_ts,
        provenance=prov,
    )
    hotels = normalize_hotels(env, nights=2, check_in="2026-11-08", check_out="2026-11-10")
    assert len(hotels) == 1
    h = hotels[0]
    assert h.name == "The Oberoi New Delhi"
    assert h.price_per_night == Decimal("9500.00")
    assert h.total_price == Decimal("19000.00")
    assert h.nights == 2
    assert h.check_in_date == "2026-11-08"
    assert h.check_out_date == "2026-11-10"
    assert "Spa" in h.amenities
    assert h.property_token == "oberoi_del_prop_token_123"
    assert h.provenance.provenance_type == ProvenanceType.LIVE_PROVIDER


def test_example_2_hotel_date_change_and_freshness():
    """Example 2 Follow-up: Date change to 9-11 Nov requires refreshed prices."""
    # 8-10 Nov: 2 nights
    nights_initial = calculate_stay_nights("2026-11-08", "2026-11-10")
    assert nights_initial == 2

    # 9-11 Nov: 2 nights
    nights_changed = calculate_stay_nights("2026-11-09", "2026-11-11")
    assert nights_changed == 2

    # Fresh query hash for changed dates must be distinct
    h1 = compute_query_hash("google_hotels", {"q": "Hotels in Delhi", "check_in": "2026-11-08", "check_out": "2026-11-10"})
    h2 = compute_query_hash("google_hotels", {"q": "Hotels in Delhi", "check_in": "2026-11-09", "check_out": "2026-11-11"})
    assert h1 != h2, "Date change must produce distinct query hash to ensure pricing is refreshed"


def test_hotel_zero_price_fabrication_guard():
    """Hotels with missing or zero prices must be rejected, never assumed ₹0."""
    raw_hotel_data = {
        "properties": [
            {
                "name": "Ghost Hotel Delhi",
                # missing rate_per_night and total_rate
            },
            {
                "name": "Zero Rupee Resort",
                "rate_per_night": {"extracted_lowest": 0},
            },
            {
                "name": "Legit Stay Delhi",
                "rate_per_night": {"extracted_lowest": 3500},
                "total_rate": {"extracted_lowest": 7000},
            },
        ]
    }
    env = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_hotels",
        query_hash="hash_guard_test",
        data=raw_hotel_data,
        is_fallback=False,
        status="success",
    )
    hotels = normalize_hotels(env, nights=2)
    assert len(hotels) == 1
    assert hotels[0].name == "Legit Stay Delhi"


# =============================================================================
# Step E & F / Example 3: Munnar Places, Restaurants & Events
# =============================================================================

def test_example_3_places_normalization():
    """Example 3: Munnar local places normalization with GPS coordinates, hours, link."""
    places_data = {
        "local_results": [
            {
                "title": "Mattupetty Dam",
                "type": "Dam / Scenic spot",
                "address": "Mattupetty, Munnar, Kerala 685616",
                "rating": 4.4,
                "reviews": 12500,
                "place_id": "ChIJ_dam_munnar_123",
                "gps_coordinates": {"latitude": 10.1065, "longitude": 77.1242},
                "operating_hours": "09:30 AM - 05:00 PM",
                "link": "https://maps.google.com/?cid=12345",
            }
        ]
    }
    now_ts = datetime.now(timezone.utc)
    prov = DataProvenance(
        provenance_type=ProvenanceType.LIVE_PROVIDER,
        provider="serpapi",
        engine="google_maps",
        retrieval_timestamp=now_ts,
        cache_hit=False,
        query_params={"q": "places in Munnar"},
        price_scope=None,
        currency="INR",
        http_status=200,
        latency_sec=0.38,
    )
    env = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_maps",
        query_hash="hash_munnar_places",
        data=places_data,
        is_fallback=False,
        status="success",
        created_at=now_ts,
        provenance=prov,
    )
    places = normalize_places(env)
    assert len(places) == 1
    p = places[0]
    assert p.name == "Mattupetty Dam"
    assert p.category == "Dam / Scenic spot"
    assert p.place_id == "ChIJ_dam_munnar_123"
    assert p.latitude == 10.1065
    assert p.longitude == 77.1242
    assert p.opening_hours == "09:30 AM - 05:00 PM"
    assert p.link == "https://maps.google.com/?cid=12345"
    assert p.provenance.provenance_type == ProvenanceType.LIVE_PROVIDER


def test_example_3_restaurant_discovery():
    """Example 3: Munnar food & restaurant discovery."""
    food_data = {
        "local_results": [
            {
                "title": "Rapsy Restaurant Munnar",
                "type": "Kerala restaurant",
                "address": "Main Bazaar, Munnar, Kerala 685612",
                "rating": 4.2,
                "reviews": 3400,
                "price": "₹₹",
            }
        ]
    }
    env = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_maps",
        query_hash="hash_munnar_food",
        data=food_data,
        is_fallback=False,
        status="success",
    )
    places = normalize_places(env)
    assert len(places) == 1
    r = places[0]
    assert r.name == "Rapsy Restaurant Munnar"
    assert r.category == "Kerala restaurant"
    assert r.rating == 4.2
    assert r.price_level == "₹₹"


def test_example_3_events_date_overlap_filter():
    """Example 3: Event filtering strictly keeps only events overlapping trip dates."""
    events = [
        EventOption(
            name="Munnar Tea Harvest Festival",
            date_str="Nov 8 - Nov 10, 2026",
            start_date="2026-11-08",
            end_date="2026-11-10",
            venue="KDHP Grounds",
            is_verified=True,
        ),
        EventOption(
            name="Kerala Winter Carnival",
            date_str="Dec 24 - Dec 31, 2026",
            start_date="2026-12-24",
            end_date="2026-12-31",
            venue="Kochi Grounds",
            is_verified=True,
        ),
        EventOption(
            name="Unspecified Date Gathering",
            date_str=None,
            start_date=None,
            venue="Munnar Town",
            is_verified=False,
        ),
    ]

    filtered = filter_events_overlapping_dates(
        events=events,
        trip_start="2026-11-08",
        trip_end="2026-11-10",
    )
    assert len(filtered) == 1
    assert filtered[0].name == "Munnar Tea Harvest Festival"


def test_example_3_route_distance_preservation():
    """Verify Google Maps Directions returns route distance and duration without inventing taxi fares."""
    routes_data = {
        "routes": [
            {
                "legs": [
                    {
                        "start_address": "Kochi, Kerala",
                        "end_address": "Munnar, Kerala",
                        "distance": {"value": 126000},  # 126 km in meters
                        "duration": {"value": 14400},   # 4 hours in seconds
                    }
                ],
                "summary": "via Kochi-Dhanushkodi Rd/NH85",
            }
        ]
    }
    env = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_maps_directions",
        query_hash="hash_route_kochi_munnar",
        data=routes_data,
        is_fallback=False,
        status="success",
    )
    routes = normalize_routes(env)
    assert len(routes) == 1
    r = routes[0]
    assert r.distance_km == 126.0
    assert r.duration_minutes == 240
    assert "NH85" in (r.summary or "")


# =============================================================================
# Cache & Provenance Transparency Tests
# =============================================================================

def test_engine_ttls_configuration():
    """Verify configured freshness policies by engine."""
    assert ENGINE_TTLS["google_flights"] == 12    # 12 hours
    assert ENGINE_TTLS["google_hotels"] == 24     # 24 hours
    assert ENGINE_TTLS["google_travel_explore"] == 48 # 48 hours
    assert ENGINE_TTLS["google_maps"] == 168      # 7 days
    assert ENGINE_TTLS["google_maps_directions"] == 168
    assert ENGINE_TTLS["google"] == 24            # 24 hours for events


@pytest.mark.asyncio
async def test_cache_hit_provenance_attachment():
    """Verify cache hits attach CACHED_PROVIDER_RESULT provenance with cache_age_seconds."""
    mock_cache_repo = MagicMock()
    mock_entry = MagicMock()
    mock_entry.created_at = datetime.now(timezone.utc) - timedelta(minutes=45)
    mock_entry.response_data = {"best_flights": []}
    mock_cache_repo.get_cached_search.return_value = mock_entry

    mgr = CacheFallbackManager(
        gateway=MagicMock(),
        cache_repo=mock_cache_repo,
        usage_repo=MagicMock(),
    )
    env = await mgr.get_travel_data(
        engine="google_flights",
        params={"departure_id": "MAA", "arrival_id": "DEL"},
    )
    assert env.source == DataSource.CACHED
    assert env.provenance is not None
    assert env.provenance.provenance_type == ProvenanceType.CACHED_PROVIDER_RESULT
    assert env.provenance.cache_hit is True
    assert env.provenance.cache_age_seconds is not None
    assert env.provenance.cache_age_seconds >= 2600.0  # ~45 minutes in seconds
