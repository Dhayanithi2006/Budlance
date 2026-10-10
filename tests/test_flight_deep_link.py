"""Phase 2 Verification & Regression Tests: Flight Booking Deep-Link Verification & Fix.

Tests:
- Test A: Correct route (Chennai -> Delhi; rejects "From", "To", "None", empty route fields).
- Test B: Round trip representation (outbound Chennai -> Delhi, return Delhi -> Chennai).
- Test C: Booking token flow (SerpApi booking_token is preserved and triggers booking options query).
- Test D: Booking option extraction (seller, price, GET direct deep-link vs POST data).
- Test E: Malformed-link regression (explicit reproduction ensuring 'Flights to From from Chennai' cannot occur).
- Test F: Missing booking URL safe fallback (does not fabricate exact booking, provides safe search handoff).
- Test G: Step 9 Realistic conversation simulation (Chennai -> Delhi, 2 people, 3 days, Flight).
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
import pytest
import urllib.parse

from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.ai.service import AIIntentService
from budlance.attractions.selector import AttractionSelector
from budlance.cache.manager import CacheFallbackManager
from budlance.db.repositories.conversation_repo import ConversationStateRepository
from budlance.db.repositories.intent_repo import IntentRepository
from budlance.db.repositories.itinerary_repo import ItineraryRepository
from budlance.db.repositories.ledger_repo import LedgerRepository
from budlance.db.repositories.rescue_repo import RescueRepository
from budlance.db.repositories.trip_repo import TripRepository
from budlance.db.repositories.user_repo import UserRepository
from budlance.engine.budget import ReverseBudgetEngine
from budlance.engine.optimizer import OptimizationEngine
from budlance.estimation.estimator import EstimationLayer
from budlance.itinerary.generator import ItineraryGenerator
from budlance.ledger.manager import VirtualLedgerManager
from budlance.normalization.flights import (
    build_safe_flight_search_url,
    extract_best_booking_option,
    is_valid_route_endpoint,
    normalize_flight_booking_options,
    normalize_flights,
    validate_flight_route,
)
from budlance.normalization.normalizer import DataNormalizer
from budlance.orchestrator.formatter import format_feasible_transport
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.rescue.service import RescueService
from budlance.schemas.travel import FlightOption
from budlance.serpapi.gateway import SerpApiGateway
from budlance.serpapi.models import DataSource, TravelDataEnvelope


@pytest.fixture
def orchestrator():
    """Build isolated orchestrator instance with in-memory repos for flight flow testing."""
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
        ledger_manager=ledger_manager,
        rescue_service=rescue_service,
    )


# ============================================================================
# Test A — Correct route validation and URL encoding
# ============================================================================

def test_a_correct_route():
    """Test A — Input: origin=Chennai, destination=Delhi.

    Verify generated handoff does not contain 'From', 'To', 'None', or empty route fields,
    and accurately represents Chennai -> Delhi.
    """
    assert is_valid_route_endpoint("Chennai") is True
    assert is_valid_route_endpoint("Delhi") is True
    assert is_valid_route_endpoint("From") is False
    assert is_valid_route_endpoint("To") is False
    assert is_valid_route_endpoint("None") is False
    assert is_valid_route_endpoint("") is False
    assert is_valid_route_endpoint(None) is False

    assert validate_flight_route("Chennai", "Delhi") is True
    assert validate_flight_route("Chennai", "From") is False
    assert validate_flight_route("None", "Delhi") is False
    assert validate_flight_route("Chennai", "Chennai") is False

    handoff_url = build_safe_flight_search_url(
        origin="Chennai",
        destination="Delhi",
        people=2,
    )
    assert "https://www.google.com/travel/flights?q=" in handoff_url
    # Query must be URL-encoded
    assert "Flights+to+DEL+from+MAA" in handoff_url or "Flights%20to%20DEL%20from%20MAA" in handoff_url
    assert "From+from" not in handoff_url
    assert "Flights+to+From" not in handoff_url
    assert "None" not in handoff_url


# ============================================================================
# Test B — Round-trip semantics in flight data and handoff
# ============================================================================

def test_b_round_trip_semantics():
    """Test B — Verify outbound = Chennai -> Delhi and return = Delhi -> Chennai

    are both represented in selected flight data and booking handoff.
    """
    envelope = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_flights",
        query_hash="round_trip_hash",
        data={
            "best_flights": [
                {
                    "flights": [
                        {
                            "airline": "IndiGo",
                            "flight_number": "6E-101",
                            "departure_airport": {"id": "MAA", "name": "Chennai", "time": "06:00"},
                            "arrival_airport": {"id": "DEL", "name": "Delhi", "time": "08:45"},
                        },
                        {
                            "airline": "IndiGo",
                            "flight_number": "6E-102",
                            "departure_airport": {"id": "DEL", "name": "Delhi", "time": "19:00"},
                            "arrival_airport": {"id": "MAA", "name": "Chennai", "time": "21:45"},
                        },
                    ],
                    "price": 8000,
                    "total_duration": 175,
                    "booking_token": "token_round_trip_123",
                }
            ]
        },
    )

    options = normalize_flights(envelope)
    assert len(options) == 1
    opt = options[0]

    assert opt.departure_airport == "MAA"
    assert opt.arrival_airport == "DEL"
    assert opt.flight_number == "6E-101"
    assert opt.return_flight_number == "6E-102"
    assert opt.departure_time == "06:00"
    assert opt.return_arrival_time == "21:45"
    assert opt.booking_token == "token_round_trip_123"

    handoff = build_safe_flight_search_url("Chennai", "Delhi", outbound_date="2026-11-01", return_date="2026-11-04", people=2)
    unquoted = urllib.parse.unquote_plus(handoff)
    assert "Flights to DEL from MAA on 2026-11-01 through 2026-11-04" in unquoted


# ============================================================================
# Test C — Booking token flow without discarding token
# ============================================================================

@pytest.mark.asyncio
async def test_c_booking_token_flow(orchestrator):
    """Test C — When normalized flight contains booking_token, verify system

    can construct expected booking-option request path without discarding token.
    """
    token = "opaque_serpapi_booking_token_xyz"
    raw_payload = {
        "best_flights": [
            {
                "flights": [
                    {
                        "airline": "IndiGo",
                        "flight_number": "6E-505",
                        "departure_airport": {"id": "MAA", "name": "Chennai"},
                        "arrival_airport": {"id": "DEL", "name": "Delhi"},
                    }
                ],
                "price": 5000,
                "booking_token": token,
            }
        ]
    }

    envelope = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_flights",
        query_hash="hash_token",
        data=raw_payload,
    )
    candidates = orchestrator.normalizer.normalize_flights(envelope)
    assert len(candidates) == 1
    assert candidates[0].booking_token == token

    # Mock cache_manager to verify request path with booking_token
    orchestrator.cache_manager.get_flight_booking_options = AsyncMock(
        return_value=TravelDataEnvelope(
            source=DataSource.LIVE,
            engine="google_flights",
            query_hash="token_hash",
            data={
                "booking_options": [
                    {
                        "book_with": "IndiGo",
                        "price": 5000,
                        "booking_request": {
                            "url": "https://www.goindigo.in/booking/view?token=123",
                            "post_data": None,
                        },
                    }
                ]
            },
        )
    )

    # When lookup_transport_options executes, it queries booking options using token
    orchestrator.cache_manager.get_travel_data = AsyncMock(return_value=envelope)
    opts = await orchestrator.lookup_transport_options(
        origin="Chennai",
        destination="Delhi",
        people=1,
        transport_mode="flight",
    )
    assert len(opts) == 1
    selected = opts[0]
    assert selected.booking_token == token
    assert selected.is_exact_booking is True
    assert selected.seller == "IndiGo"
    assert selected.deep_link == "https://www.goindigo.in/booking/view?token=123"
    orchestrator.cache_manager.get_flight_booking_options.assert_awaited_with(token)


# ============================================================================
# Test D — Booking option extraction (GET URL vs POST data)
# ============================================================================

def test_d_booking_option_extraction():
    """Test D — Given representative SerpApi booking-options payload, verify:

    seller, price, booking request/deep-link are extracted correctly.
    GET direct URL is followed, while POST data is NOT converted to fake GET URL.
    """
    # 1. Direct GET booking request
    payload_get = {
        "booking_options": [
            {
                "book_with": "IndiGo",
                "price": 8500,
                "booking_request": {
                    "url": "https://www.goindigo.in/deep-link/flight101",
                    "post_data": None,
                },
                "marketed_by": "IndiGo",
            }
        ]
    }
    extracted_get = extract_best_booking_option(payload_get)
    assert extracted_get is not None
    assert extracted_get["seller"] == "IndiGo"
    assert extracted_get["price"] == Decimal("8500.00")
    assert extracted_get["direct_url"] == "https://www.goindigo.in/deep-link/flight101"
    assert extracted_get["has_post_data"] is False

    # 2. POST data booking request — must NOT create fake GET URL
    payload_post = {
        "booking_options": [
            {
                "book_with": "MakeMyTrip",
                "price": 8900,
                "booking_request": {
                    "url": "https://www.makemytrip.com/booking/post-endpoint",
                    "post_data": "session_id=abc&token=xyz",
                },
            }
        ]
    }
    extracted_post = extract_best_booking_option(payload_post)
    assert extracted_post is not None
    assert extracted_post["seller"] == "MakeMyTrip"
    assert extracted_post["has_post_data"] is True
    # direct_url must be None so we do not fabricate a fake GET URL
    assert extracted_post["direct_url"] is None
    assert extracted_post["booking_request"]["post_data"] == "session_id=abc&token=xyz"


# ============================================================================
# Test E — Malformed-link regression (Never produce 'Flights to From from Chennai')
# ============================================================================

def test_e_malformed_link_regression():
    """Test E — Explicitly reproduce old malformed-link conditions and verify

    it can no longer produce 'Flights to From from Chennai', 'Flights to None', etc.
    """
    # Case 1: destination is "From"
    url1 = build_safe_flight_search_url("Chennai", "From")
    assert "Flights to From from Chennai" not in url1
    assert "From from Chennai" not in url1
    assert url1 == "https://www.google.com/travel/flights"

    # Case 2: destination is "None"
    url2 = build_safe_flight_search_url("Chennai", "None")
    assert "None" not in url2
    assert url2 == "https://www.google.com/travel/flights"

    # Case 3: origin is None
    url3 = build_safe_flight_search_url(None, "Delhi")
    assert "None" not in url3
    assert url3 == "https://www.google.com/travel/flights"

    # Case 4: empty strings
    url4 = build_safe_flight_search_url("", "")
    assert url4 == "https://www.google.com/travel/flights"

    # Case 5: AI rule-based parser must NOT extract "From" or "To" as destination
    ai_service = AIIntentService(use_mock=True)
    clean_prompt = "i want flights to from chennai"
    cand = ai_service._extract_single_destination(clean_prompt)
    assert cand is None or cand.lower() not in {"from", "to", "none"}


# ============================================================================
# Test F — Missing booking URL uses safe fallback without fabricating
# ============================================================================

def test_f_missing_booking_url_safe_fallback():
    """Test F — When no exact booking link is available, verify system does not

    fabricate an exact booking URL and instead uses safe provider search handoff.
    """
    envelope = TravelDataEnvelope(
        source=DataSource.FALLBACK,
        engine="google_flights",
        query_hash="fallback_hash",
        data={},
        is_fallback=True,
    )
    options = normalize_flights(envelope)
    assert options == []

    # When fallback flight is generated:
    safe_url = build_safe_flight_search_url("Chennai", "Delhi", people=2, travel_class="economy")
    flight = FlightOption(
        airline="IndiGo",
        flight_number="6E-101",
        departure_airport="Chennai",
        arrival_airport="Delhi",
        price=Decimal("8000.00"),
        source=DataSource.FALLBACK,
        is_fallback=True,
        seller="IndiGo",
        deep_link=safe_url,
        is_exact_booking=False,
    )

    # Formatter must indicate search handoff, not exact completed booking
    formatted = format_feasible_transport(
        transport_mode="flight",
        transport_class="economy",
        estimated_cost=flight.price,
        remaining_budget=Decimal("42000.00"),
        currency="INR",
        booking_link=flight.deep_link,
        operator=flight.airline,
        people=2,
        origin=flight.departure_airport,
        destination=flight.arrival_airport,
        is_exact_booking=flight.is_exact_booking,
        seller=flight.seller,
        flight_number=flight.flight_number,
    )

    assert "• Handoff Type: Provider Search Handoff" in formatted
    assert "Direct Provider Option" not in formatted
    assert "Chennai → Delhi (Round Trip)" in formatted
    assert safe_url in formatted
    assert "Reply with *Booked* once you have completed your external booking" in formatted
    assert "booked" not in formatted.lower().split("*booked*")[0]  # does not claim it was booked


# ============================================================================
# Test G — Step 9 Realistic Conversation Simulation
# ============================================================================

@pytest.mark.asyncio
async def test_g_realistic_conversation_simulation(orchestrator):
    """Test G — Realistic conversation scenario:

    Chennai -> Delhi, 2 people, 3 days, Flight, budget ₹100,000.
    Verifies:
    1. Flight intent detected, origin = Chennai, destination = Delhi.
    2. When no live/cached flight result:
       - No fake FlightOption fabricated (selected_transport is None).
       - No fake airline ("IndiGo") or flight number ("6E-101") produced.
       - Clean provider search handoff URL generated.
       - Does not claim flight was booked.
    3. When actual flight option envelope exists:
       - Selected flight represents live provider data.
       - Round-trip costing and budget impact verified.
       - Full handoff with carrier and schedule preserved.
    """
    chat_id = 9922001
    user_id = 8822001

    msg = "I want to go from Chennai to Delhi, budget 100000, 2 people, 3 days. We want flight economy"

    # Part 1: Offline without live/cached flight — NO fake flight fabrication
    result = await orchestrator.handle_user_message(
        telegram_user_id=user_id,
        chat_id=chat_id,
        message=msg,
        username="flight_traveler",
        first_name="Ravi",
    )

    assert result.status == "FEASIBLE_TRANSPORT"
    assert result.selected_destination == "Delhi"
    # No fake flight option fabricated
    assert result.selected_transport is None

    text = result.message_text
    assert "IndiGo" not in text
    assert "6E-101" not in text
    assert "Chennai → Delhi (Round Trip)" in text
    assert "Flights to From from Chennai" not in text
    assert "Flights to None" not in text
    assert "🔗 *External Booking Handoff:*" in text
    assert "https://www.google.com/travel/flights?q=" in text
    assert "Delhi" in text
    assert "Chennai" in text
    assert "Reply with *Booked* once you have completed your external booking" in text
    assert "Trip confirmed and flight booked" not in text

    # Part 2: When an actual flight result is returned from live/mock provider
    flight_envelope = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_flights",
        query_hash="hash_live_delhi",
        data={
            "best_flights": [
                {
                    "flights": [
                        {
                            "airline": "Air India",
                            "flight_number": "AI-505",
                            "departure_airport": {"id": "MAA", "name": "Chennai"},
                            "arrival_airport": {"id": "DEL", "name": "Delhi"},
                        }
                    ],
                    "price": 16000,
                }
            ]
        },
    )
    orchestrator.cache_manager.get_travel_data = AsyncMock(return_value=flight_envelope)

    result2 = await orchestrator.handle_user_message(
        telegram_user_id=user_id + 1,
        chat_id=chat_id + 1,
        message=msg,
        username="flight_traveler2",
        first_name="Ravi2",
    )

    assert result2.status == "FEASIBLE_TRANSPORT"
    assert result2.selected_destination == "Delhi"
    assert result2.selected_transport is not None
    assert isinstance(result2.selected_transport, FlightOption)
    assert result2.selected_transport.airline == "Air India"
    assert result2.selected_transport.flight_number == "AI-505"
    assert result2.selected_transport.price == Decimal("16000.00")

    text2 = result2.message_text
    assert "Chennai → Delhi (Round Trip)" in text2
    assert "Air India" in text2
    assert "AI-505" in text2
    assert "Estimated travel cost: ₹16,000.00" in text2
    assert "Remaining budget impact: ₹84,000.00" in text2
    assert "🔗 *External Booking Handoff:*" in text2
    assert "Reply with *Booked* once you have completed your external booking" in text2


# ============================================================================
# Test G — GET /book/{id} relay & fallback URL with real dates
# ============================================================================

def test_get_book_id_relay_endpoint_auto_submits_post_unmodified():
    """Verify GET /book/{id} returns HTML auto-submitting POST form with unmodified post_data."""
    from fastapi.testclient import TestClient
    from budlance.api.app import app
    from budlance.db.repositories.cache_repo import CacheRepository

    client = TestClient(app)
    repo = CacheRepository()

    # 1. 404 on non-existent booking ID
    res_404 = client.get("/book/non_existent_id")
    assert res_404.status_code == 404
    assert "Booking session expired or not found" in res_404.json()["detail"]

    # 2. Store booking request with dict post_data
    b_id1 = "test_book_123"
    post_payload1 = {
        "url": "https://www.goindigo.in/booking/checkout",
        "post_data": {
            "session_token": "tok_xyz_999",
            "flight_id": "6E-204",
            "fare_type": "regular",
        },
    }
    repo.store_booking_request(b_id1, post_payload1, ttl_seconds=300)

    res1 = client.get(f"/book/{b_id1}")
    assert res1.status_code == 200
    assert "text/html" in res1.headers["content-type"]
    html_text1 = res1.text
    assert 'action="https://www.goindigo.in/booking/checkout"' in html_text1
    assert 'method="POST"' in html_text1
    assert 'name="session_token" value="tok_xyz_999"' in html_text1
    assert 'name="flight_id" value="6E-204"' in html_text1
    assert 'name="fare_type" value="regular"' in html_text1
    assert "document.getElementById('bookForm').submit();" in html_text1

    # 3. Store booking request with string query-param post_data
    b_id2 = "test_book_456"
    post_payload2 = {
        "url": "https://www.airindia.com/book-flight",
        "post_data": "ref=promo2026&client_id=budlance_app",
    }
    repo.store_booking_request(b_id2, post_payload2, ttl_seconds=300)

    res2 = client.get(f"/book/{b_id2}")
    assert res2.status_code == 200
    html_text2 = res2.text
    assert 'action="https://www.airindia.com/book-flight"' in html_text2
    assert 'name="ref" value="promo2026"' in html_text2
    assert 'name="client_id" value="budlance_app"' in html_text2


def test_plan_uses_book_id_when_options_exist_otherwise_fallback_search_url_with_dates():
    """Verify plan uses /book/{id} when booking_options exist, otherwise safe fallback with real dates."""
    from budlance.normalization.flights import build_safe_flight_search_url, normalize_flights
    from budlance.orchestrator.formatter import format_feasible_plan
    from budlance.engine.models import BudgetBreakdown
    from budlance.db.repositories.cache_repo import CacheRepository

    repo = CacheRepository()

    # Case A: booking_options exist with post_data -> produces /book/{id}
    env_with_options = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_flights",
        query_hash="hash_with_opts",
        data={
            "flights": [
                {
                    "price": 8500,
                    "legs": [{"airline": "IndiGo", "flight_number": "6E-101"}],
                    "departure_airport": {"id": "MAA", "name": "Chennai"},
                    "arrival_airport": {"id": "DEL", "name": "Delhi"},
                    "booking_options": [
                        {
                            "seller": "IndiGo",
                            "price": 8500,
                            "booking_request": {
                                "url": "https://www.goindigo.in/booking/post-pay",
                                "post_data": {"booking_ref": "REF888"},
                            },
                        }
                    ],
                }
            ]
        },
    )
    flights_with_opts = normalize_flights(env_with_options)
    assert len(flights_with_opts) == 1
    fl_opt = flights_with_opts[0]
    assert fl_opt.deep_link is not None
    assert "/book/" in fl_opt.deep_link

    # Print Example 1: /book/{id}
    example_relay_url = fl_opt.deep_link if fl_opt.deep_link.startswith("http") else f"http://localhost:8000{fl_opt.deep_link}"
    print(f"\n[EXAMPLE_1_RELAY_URL]: {example_relay_url}")

    breakdown = BudgetBreakdown(
        total_budget=Decimal("50000.00"),
        currency="INR",
        bucket_a_fixed=Decimal("8500.00"),
        bucket_b_survival=Decimal("5000.00"),
        bucket_c_activities=Decimal("5000.00"),
        bucket_d_rescue=Decimal("5000.00"),
        transport_cost=Decimal("8500.00"),
        hotel_cost=Decimal("0.00"),
        food_cost=Decimal("5000.00"),
        local_transit_cost=Decimal("0.00"),
        total_allocated=Decimal("23500.00"),
        remaining_surplus=Decimal("26500.00"),
    )

    plan_text_with_opt = format_feasible_plan(
        destination="Delhi",
        days=3,
        people=1,
        breakdown=breakdown,
        transport=fl_opt,
        hotel=None,
        itinerary=None,
        is_pass_unlocked=True,
    )
    assert f"🔗 Booking: {fl_opt.deep_link}" in plan_text_with_opt

    # Case B: No booking_options exist -> produces Google Flights search URL with real dates
    fallback_search_url = build_safe_flight_search_url(
        origin="Chennai",
        destination="Delhi",
        outbound_date="2026-11-08",
        return_date="2026-11-10",
        people=1,
    )
    # Print Example 2: Fallback search URL with real dates
    print(f"[EXAMPLE_2_FALLBACK_URL_WITH_DATES]: {fallback_search_url}")
    assert "https://www.google.com/travel/flights?q=Flights+to+DEL+from+MAA+on+2026-11-08+through+2026-11-10" == fallback_search_url

    fl_fallback = FlightOption(
        airline="Air India",
        flight_number="AI-101",
        price=Decimal("9000.00"),
        deep_link=fallback_search_url,
    )
    plan_text_fallback = format_feasible_plan(
        destination="Delhi",
        days=3,
        people=1,
        breakdown=breakdown,
        transport=fl_fallback,
        hotel=None,
        itinerary=None,
        is_pass_unlocked=True,
    )
    assert f"🔗 Booking: {fallback_search_url}" in plan_text_fallback

