"""Tests for Data Normalizer and Estimation Layer."""

from decimal import Decimal
from uuid import uuid4
import pytest

from budlance.estimation.estimator import EstimationLayer
from budlance.normalization.flights import normalize_flights
from budlance.normalization.hotels import normalize_hotels
from budlance.normalization.normalizer import DataNormalizer
from budlance.normalization.places import normalize_places
from budlance.normalization.routes import normalize_routes
from budlance.normalization.transit import normalize_transit_fallback
from budlance.normalization.utils import parse_price_and_currency
from budlance.schemas.travel import FlightOption, HotelOption, PlaceOption, RouteOption, TransitOption
from budlance.serpapi.models import DataSource, TravelDataEnvelope


# ============================================================================
# 1. Price and Currency Parsing
# ============================================================================
def test_parse_price_and_currency():
    """Verify numeric and string price/currency extraction."""
    assert parse_price_and_currency(4500) == (Decimal("4500"), "INR")
    assert parse_price_and_currency("₹12,500") == (Decimal("12500"), "INR")
    assert parse_price_and_currency("$199.99") == (Decimal("199.99"), "USD")
    assert parse_price_and_currency("€85") == (Decimal("85"), "EUR")
    assert parse_price_and_currency(None) == (Decimal("0.00"), "INR")
    assert parse_price_and_currency("invalid") == (Decimal("0.00"), "INR")


# ============================================================================
# 2. Flight Normalization
# ============================================================================
def test_flight_normalization_valid():
    """Verify parsing valid Google Flights envelope."""
    fixture_data = {
        "best_flights": [
            {
                "flights": [
                    {
                        "airline": "IndiGo",
                        "flight_number": "6E-202",
                        "departure_airport": {"id": "BOM", "time": "2026-10-01 08:00"},
                        "arrival_airport": {"id": "GOI", "time": "2026-10-01 09:15"},
                        "duration": 75,
                    }
                ],
                "price": 4200,
                "total_duration": 75,
                "booking_token": "https://airline.com/booking?id=123",
            }
        ]
    }
    envelope = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_flights",
        query_hash="hash_flights",
        data=fixture_data,
    )

    options = normalize_flights(envelope)
    assert len(options) == 1
    flight = options[0]
    assert flight.airline == "IndiGo"
    assert flight.flight_number == "6E-202"
    assert flight.departure_airport == "BOM"
    assert flight.arrival_airport == "GOI"
    assert flight.price == Decimal("4200")
    assert flight.currency == "INR"
    assert flight.duration_minutes == 75
    assert flight.stops == 0
    assert flight.deep_link == "https://airline.com/booking?id=123"
    assert flight.source == DataSource.LIVE
    assert flight.is_fallback is False


def test_flight_normalization_missing_optional_fields():
    """Verify flight normalizer handles partial or sparse flight data safely when price is valid."""
    fixture_data = {
        "best_flights": [
            {
                "flights": [
                    {
                        "airline": "Air India",
                        # Missing flight_number, time, airports
                    }
                ],
                "price": 3500,
            }
        ]
    }
    envelope = TravelDataEnvelope(
        source=DataSource.CACHED,
        engine="google_flights",
        query_hash="hash_flights_sparse",
        data=fixture_data,
    )

    options = normalize_flights(envelope)
    assert len(options) == 1
    flight = options[0]
    assert flight.airline == "Air India"
    assert flight.flight_number is None
    assert flight.price == Decimal("3500")
    assert flight.source == DataSource.CACHED


def test_flight_normalization_rejects_missing_price():
    """Verify flight normalizer rejects items without a price at the normalization source."""
    fixture_data = {
        "best_flights": [
            {
                "flights": [{"airline": "Air India"}],
                # Missing price field
            }
        ]
    }
    envelope = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_flights",
        query_hash="hash_flights_no_price",
        data=fixture_data,
    )
    assert normalize_flights(envelope) == []


# ============================================================================
# 3. Hotel Normalization
# ============================================================================
def test_hotel_normalization_valid():
    """Verify parsing valid Google Hotels envelope."""
    fixture_data = {
        "properties": [
            {
                "name": "Taj Exotica Resort & Spa",
                "hotel_class": "5-star hotel",
                "address": "Benaulim Beach, Goa",
                "rate_per_night": {"extracted_lowest": 12000, "lowest": "₹12,000"},
                "total_rate": {"extracted_lowest": 36000},
                "overall_rating": 4.7,
                "reviews": 1820,
                "link": "https://tajhotels.com/exotica",
            }
        ]
    }
    envelope = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_hotels",
        query_hash="hash_hotels",
        data=fixture_data,
    )

    options = normalize_hotels(envelope)
    assert len(options) == 1
    hotel = options[0]
    assert hotel.name == "Taj Exotica Resort & Spa"
    assert hotel.hotel_class == 5
    assert hotel.address == "Benaulim Beach, Goa"
    assert hotel.price_per_night == Decimal("12000")
    assert hotel.total_price == Decimal("36000")
    assert hotel.rating == 4.7
    assert hotel.review_count == 1820
    assert hotel.source == DataSource.LIVE


def test_hotel_normalization_valid_positive_price_accepts():
    """Verify hotel with valid property and positive price is accepted."""
    fixture_data = {
        "properties": [
            {
                "name": "Positive Hotel One",
                "rate_per_night": {"extracted_lowest": 2500},
                "total_rate": {"extracted_lowest": 5000},
            },
            {
                "name": "Positive Hotel Night Only",
                "rate_per_night": {"lowest": "₹3,200"},
            },
            {
                "name": "Positive Hotel Total Only",
                "total_rate": {"extracted_lowest": 7500},
            },
        ]
    }
    envelope = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_hotels",
        query_hash="hash_pos_hotels",
        data=fixture_data,
    )
    hotels = normalize_hotels(envelope)
    assert len(hotels) == 3

    assert hotels[0].name == "Positive Hotel One"
    assert hotels[0].price_per_night == Decimal("2500")
    assert hotels[0].total_price == Decimal("5000")

    assert hotels[1].name == "Positive Hotel Night Only"
    assert hotels[1].price_per_night == Decimal("3200")
    assert hotels[1].total_price == Decimal("3200")

    assert hotels[2].name == "Positive Hotel Total Only"
    assert hotels[2].price_per_night == Decimal("7500")
    assert hotels[2].total_price == Decimal("7500")


def test_hotel_normalization_missing_invalid_zero_price_rejects():
    """Verify hotel with missing, invalid, zero, or negative price is strictly rejected."""
    fixture_data = {
        "properties": [
            # 1. Missing price completely
            {"name": "Missing Price Hotel"},
            # 2. None prices
            {"name": "None Price Hotel", "rate_per_night": None, "total_rate": None},
            # 3. Zero night price
            {"name": "Zero Night Hotel", "rate_per_night": {"extracted_lowest": 0}},
            # 4. Zero total price
            {"name": "Zero Total Hotel", "rate_per_night": {"extracted_lowest": 2000}, "total_rate": {"extracted_lowest": 0}},
            # 5. Zero night price with positive total
            {"name": "Zero Night Pos Total Hotel", "rate_per_night": {"extracted_lowest": 0}, "total_rate": {"extracted_lowest": 4000}},
            # 6. Invalid non-numeric price string
            {"name": "Invalid Price Hotel", "rate_per_night": {"lowest": "Free / Contact Hotel"}},
            # 7. Sold out / N/A
            {"name": "Sold Out Hotel", "total_rate": {"lowest": "Sold Out"}},
            # 8. Negative numeric price
            {"name": "Negative Price Hotel", "rate_per_night": {"extracted_lowest": -500}},
            # 9. Negative string price
            {"name": "Negative String Hotel", "rate_per_night": {"lowest": "-₹1,200"}},
            # 10. Valid hotel to verify filtering preserves valid ones alongside invalid ones
            {"name": "Valid Hotel", "rate_per_night": {"extracted_lowest": 1800}},
        ]
    }
    envelope = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_hotels",
        query_hash="hash_reject_hotels",
        data=fixture_data,
    )
    hotels = normalize_hotels(envelope)
    assert len(hotels) == 1
    assert hotels[0].name == "Valid Hotel"
    assert hotels[0].price_per_night == Decimal("1800")
    assert hotels[0].total_price == Decimal("1800")


def test_hotel_normalization_invalid_property_rejects():
    """Verify property with empty, blank, or missing name or non-dict is rejected."""
    fixture_data = {
        "properties": [
            "not a dict",
            None,
            {"name": "", "rate_per_night": {"extracted_lowest": 3000}},
            {"name": "   ", "rate_per_night": {"extracted_lowest": 3000}},
            {"rate_per_night": {"extracted_lowest": 3000}},  # no name key
            {"name": "Legit Inn", "rate_per_night": {"extracted_lowest": 3000}},
        ]
    }
    envelope = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_hotels",
        query_hash="hash_invalid_props",
        data=fixture_data,
    )
    hotels = normalize_hotels(envelope)
    assert len(hotels) == 1
    assert hotels[0].name == "Legit Inn"


# ============================================================================
# 4. Place Normalization
# ============================================================================
def test_place_normalization_valid():
    """Verify parsing Google Maps local places envelope."""
    fixture_data = {
        "local_results": [
            {
                "title": "Palolem Beach",
                "type": "Beach",
                "address": "Canacona, South Goa",
                "rating": 4.6,
                "reviews": 3200,
                "price": "₹",
            }
        ]
    }
    envelope = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_maps",
        query_hash="hash_places",
        data=fixture_data,
    )

    places = normalize_places(envelope)
    assert len(places) == 1
    place = places[0]
    assert place.name == "Palolem Beach"
    assert place.category == "Beach"
    assert place.rating == 4.6
    assert place.price_level == "₹"
    assert place.estimated_cost == Decimal("0.00")
    assert place.source == DataSource.LIVE


# ============================================================================
# 5. Route Normalization
# ============================================================================
def test_route_normalization_valid():
    """Verify parsing Google Maps Directions envelope."""
    fixture_data = {
        "routes": [
            {
                "summary": "NH66",
                "legs": [
                    {
                        "start_address": "Panaji, Goa",
                        "end_address": "Dabolim Airport, Goa",
                        "distance": {"value": 28500},  # 28.5 km
                        "duration": {"value": 2400},   # 40 mins
                    }
                ],
            }
        ]
    }
    envelope = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_maps_directions",
        query_hash="hash_directions",
        data=fixture_data,
    )

    routes = normalize_routes(envelope)
    assert len(routes) == 1
    route = routes[0]
    assert route.origin == "Panaji, Goa"
    assert route.destination == "Dabolim Airport, Goa"
    assert route.distance_km == 28.5
    assert route.duration_minutes == 40
    assert route.summary == "NH66"
    assert route.source == DataSource.LIVE


# ============================================================================
# 6. Malformed and Empty Responses
# ============================================================================
def test_malformed_and_empty_responses():
    """Verify normalizers fail gracefully when encountering corrupt data."""
    bad_envelope = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_flights",
        query_hash="hash_bad",
        data={"best_flights": "not_a_list"},
    )
    assert normalize_flights(bad_envelope) == []
    assert normalize_hotels(bad_envelope) == []
    assert normalize_places(bad_envelope) == []
    assert normalize_routes(bad_envelope) == []


# ============================================================================
# 7. Fallback Train and Bus Normalization
# ============================================================================
def test_train_fallback_normalization():
    """Verify static train corridor JSON normalization preserves FALLBACK source."""
    train_fixture = {
        "corridors": [
            {
                "origin": "Chennai",
                "destination": "Bangalore",
                "distance_km": 350,
                "train_name": "Shatabdi Express",
                "duration_hours": 5.0,
                "classes": {"CC": 750, "EC": 1450},
                "is_fallback": True,
            }
        ]
    }
    envelope = TravelDataEnvelope(
        source=DataSource.FALLBACK,
        engine="train_corridors",
        query_hash="hash_train",
        data=train_fixture,
        is_fallback=True,
    )

    transits = normalize_transit_fallback(envelope)
    assert len(transits) == 2  # One per class (CC and EC)
    cc_option = next(t for t in transits if t.class_or_type == "CC")
    assert cc_option.price == Decimal("750")
    assert cc_option.transit_type == "train"
    assert cc_option.source == DataSource.FALLBACK
    assert cc_option.is_fallback is True


def test_bus_fallback_normalization():
    """Verify static bus corridor JSON normalization preserves FALLBACK source."""
    bus_fixture = {
        "corridors": [
            {
                "origin": "Bangalore",
                "destination": "Ooty",
                "distance_km": 280,
                "bus_type": "KSRTC Airavat AC",
                "duration_hours": 7.0,
                "fare_inr": 700,
                "is_fallback": True,
            }
        ]
    }
    envelope = TravelDataEnvelope(
        source=DataSource.FALLBACK,
        engine="bus_corridors",
        query_hash="hash_bus",
        data=bus_fixture,
        is_fallback=True,
    )

    transits = normalize_transit_fallback(envelope)
    assert len(transits) == 1
    bus = transits[0]
    assert bus.transit_type == "bus"
    assert bus.price == Decimal("700")
    assert bus.source == DataSource.FALLBACK
    assert bus.is_fallback is True


# ============================================================================
# 8. Estimation Layer (Food & Transport)
# ============================================================================
def test_food_estimation_calculation():
    """Verify food costs are deterministically calculated and marked ESTIMATED."""
    estimator = EstimationLayer()

    # 3 people for 4 days on standard tier (800 INR/person/day)
    estimate = estimator.estimate_food(people=3, days=4, tier="standard")
    assert estimate.tier == "standard"
    assert estimate.daily_cost_per_person == Decimal("800.00")
    assert estimate.total_cost == Decimal("9600.00")  # 800 * 3 * 4
    assert estimate.source == DataSource.ESTIMATED

    # Budget tier (400 INR/person/day)
    budget_estimate = estimator.estimate_food(people=2, days=2, tier="budget")
    assert budget_estimate.total_cost == Decimal("1600.00")  # 400 * 2 * 2
    assert budget_estimate.source == DataSource.ESTIMATED


def test_local_transit_estimation_calculation():
    """Verify advisory local transport is calculated and marked ESTIMATED."""
    estimator = EstimationLayer()

    # Point to point auto: 15 km @ 15 INR/km = 225 INR
    dist_estimate = estimator.estimate_local_transit_distance(distance_km=15.0, mode="auto")
    assert dist_estimate.mode == "auto"
    assert dist_estimate.rate_per_km == Decimal("15.00")
    assert dist_estimate.total_cost == Decimal("225.00")
    assert dist_estimate.source == DataSource.ESTIMATED

    # Daily transit pass: 3 people for 4 days @ 100 INR/day/person = 1200 INR
    daily_estimate = estimator.estimate_local_transit_daily(days=4, people=3, mode="metro_bus")
    assert daily_estimate.total_cost == Decimal("1200.00")
    assert daily_estimate.source == DataSource.ESTIMATED


# ============================================================================
# 9. Source Preservation & Provenance Integrity
# ============================================================================
def test_source_preservation_across_normalizer():
    """Verify that DataNormalizer accurately preserves LIVE, CACHED, and FALLBACK tags."""
    normalizer = DataNormalizer()

    # Cached Flight Envelope
    cached_env = TravelDataEnvelope(
        source=DataSource.CACHED,
        engine="google_flights",
        query_hash="h1",
        data={"best_flights": [{"flights": [{"airline": "SpiceJet"}], "price": 3000}]},
    )
    flights = normalizer.normalize(cached_env)
    assert flights[0].source == DataSource.CACHED

    # Live Hotel Envelope
    live_env = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_hotels",
        query_hash="h2",
        data={"properties": [{"name": "Seaside Inn", "price": 2500}]},
    )
    hotels = normalizer.normalize(live_env)
    assert hotels[0].source == DataSource.LIVE

    # Fallback Transit Envelope
    fb_env = TravelDataEnvelope(
        source=DataSource.FALLBACK,
        engine="train_corridors",
        query_hash="h3",
        data={"origin": "A", "destination": "B", "fare_inr": 400},
        is_fallback=True,
    )
    transits = normalizer.normalize(fb_env)
    assert transits[0].source == DataSource.FALLBACK
    assert transits[0].is_fallback is True
