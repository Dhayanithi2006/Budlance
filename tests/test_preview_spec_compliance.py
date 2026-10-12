"""Compliance tests verifying the exact Munnar plan preview spec and hardening items.

Ensures zero-live external API calls, price positivity invariants, nearest-airport routing,
proper date prompting, and clean template rendering with no premature booking handoffs.
"""

from decimal import Decimal
import pytest
from unittest.mock import MagicMock

from budlance.ai.service import AIIntentService
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.schemas.travel import FlightOption, HotelOption, TransitOption
from budlance.serpapi.location import resolve_nearest_airport_with_transfer
from budlance.serpapi.models import DataSource, TravelDataEnvelope


def test_01_flight_option_zero_price_rejected():
    """FlightOption must reject price <= 0."""
    with pytest.raises(ValueError, match="FlightOption.price must be > 0"):
        FlightOption(airline="IndiGo", price=Decimal("0.00"))

    with pytest.raises(ValueError, match="FlightOption.price must be > 0"):
        FlightOption(airline="IndiGo", price=Decimal("-100.00"))


def test_02_hotel_option_zero_price_rejected():
    """HotelOption must reject total_price <= 0."""
    with pytest.raises(ValueError, match="HotelOption.total_price must be > 0"):
        HotelOption(name="Grand Palace", total_price=Decimal("0.00"))


def test_03_transit_option_zero_price_rejected():
    """TransitOption must reject price <= 0."""
    with pytest.raises(ValueError, match="TransitOption.price must be > 0"):
        TransitOption(name_or_operator="KSRTC", price=Decimal("0.00"))


def test_04_nearest_airport_transfer_resolution():
    """resolve_nearest_airport_with_transfer resolves destinations without direct airports."""
    munnar_info = resolve_nearest_airport_with_transfer("Munnar")
    assert munnar_info is not None
    assert munnar_info["nearest_city"] == "Kochi"
    assert munnar_info["iata"] == "COK"
    assert munnar_info["transfer_km"] == 130
    assert munnar_info["transfer_hours"] == 3.5

    ooty_info = resolve_nearest_airport_with_transfer("Ooty")
    assert ooty_info is not None
    assert ooty_info["nearest_city"] == "Coimbatore"

    # Destination with its own direct airport returns None
    chennai_info = resolve_nearest_airport_with_transfer("Chennai")
    assert chennai_info is None


@pytest.mark.asyncio
async def test_05_munnar_flow_turn_1_prompts_start_date():
    """Turn 1 of the Munnar flight scenario asks for start date instead of emitting a plan."""
    orch = BudlanceOrchestrator(ai_service=AIIntentService(use_mock=True))
    chat_id = 992201

    res = await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message=(
            "I have 50000 for 1 people for 2 days. I like to explore local food of that place "
            "and theme park currently i am in chennai like to go munnar in flight"
        ),
    )

    assert res.status == "CLARIFICATION"
    assert "Which date do you want to start? (Example: 9 Nov)" in res.message_text


@pytest.mark.asyncio
async def test_06_munnar_flow_turn_2_outputs_spec_preview():
    """Turn 2 with '9 Nov' generates the exact spec-compliant Munnar plan preview."""
    orch = BudlanceOrchestrator(ai_service=AIIntentService(use_mock=True))

    # Mock flight to Kochi
    orig_get = orch.cache_manager.get_travel_data

    async def _mock_get(engine, params, **kw):
        if engine == "google_flights" and params.get("arrival_id") == "COK":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine="google_flights",
                query_hash="mock_cok",
                data={
                    "best_flights": [
                        {
                            "flights": [{"airline": "IndiGo", "flight_number": "6E-201"}],
                            "price": 3000,
                        }
                    ]
                },
            )
        return await orig_get(engine, params, **kw)

    orch.cache_manager.get_travel_data = _mock_get
    chat_id = 992202

    # Turn 1
    t1 = await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message=(
            "I have 50000 for 1 people for 2 days. I like to explore local food of that place "
            "and theme park currently i am in chennai like to go munnar in flight"
        ),
    )
    assert t1.status == "CLARIFICATION"

    # Turn 2: User provides "9 Nov"
    t2 = await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="9 Nov",
    )
    assert t2.status == "FEASIBLE_TRANSPORT"
    text = t2.message_text

    # Spec header assertions
    assert "Here is your Munnar plan preview" in text
    assert "Mon 9 Nov - Tue 10 Nov 2026 | 2 days, 1 night | 1 traveller | from Chennai" in text

    # Estimated cost breakdown assertions
    assert "Estimated cost" in text
    assert "- Flights Chennai to Kochi and back (estimate): Rs 3,000" in text
    assert "- Taxi Kochi to Munnar and back (estimate): Rs 5,720" in text
    assert "- Stay, 1 night (estimate): Rs" in text
    assert "- Food and local travel (estimate): Rs 1,800" in text
    assert "- Activities (estimate): Rs 2,500" in text
    assert "- Emergency buffer (kept aside for rain or price surprises): Rs 5,000" in text
    assert "Total planned: Rs" in text
    assert "of your Rs 50,000" in text
    assert "is left." in text

    # Highlights (omitted when no live Maps places exist)
    assert "Highlights:" not in text

    # Routing warning note (plain statement, no question)
    assert "Munnar has no airport, so you fly to Kochi and drive about 3.5 hours each way" in text
    assert "consider adding an extra day to explore more." in text
    assert "Want to add a day?" not in text

    # Interest mismatch note for theme park (truthful offline search report)
    assert "Theme park: I couldn't check for theme parks right now." in text
    assert "Wonderla Kochi" not in text

    # Trip Pass unlock CTA (single question)
    assert "Change anything first, or unlock the full plan with flight and hotel links for Rs 49." in text

    # Negative assertions (forbidden items)
    assert ".00" not in text
    assert "Reply with *Booked*" not in text
    assert "Provider Search Handoff" not in text
    assert "(assumed for planning)" not in text
    assert "Rs 0" not in text
    assert "₹0" not in text
    assert "http://" not in text
    assert "https://" not in text
    assert "bucket" not in text.lower()


def test_07_normalize_flights_filters_priceless_flight_without_exception():
    """Live-shaped flight response with one priced and one priceless flight returns only the priced one."""
    from budlance.normalization.flights import normalize_flights

    envelope = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_flights",
        query_hash="test_mixed_flights",
        data={
            "best_flights": [
                {
                    # Flight 1: Priceless (e.g. Etihad / unpriced flight)
                    "flights": [{"airline": "Etihad Airways", "flight_number": "EY-202"}],
                    "price": None,
                },
                {
                    # Flight 2: Valid priced flight
                    "flights": [{"airline": "IndiGo", "flight_number": "6E-201"}],
                    "price": 3500,
                },
            ]
        },
    )

    results = normalize_flights(envelope)
    assert len(results) == 1
    assert results[0].airline == "IndiGo"
    assert results[0].price == Decimal("3500.00")


def test_08_normalize_hotels_filters_priceless_hotel_without_exception():
    """Live-shaped hotel response with one priced and one priceless hotel returns only the priced one."""
    from budlance.normalization.hotels import normalize_hotels

    envelope = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_hotels",
        query_hash="test_mixed_hotels",
        data={
            "properties": [
                {
                    # Hotel 1: Priceless / missing rate
                    "name": "Unpriced Resort",
                    "rate_per_night": None,
                    "total_rate": None,
                },
                {
                    # Hotel 2: Valid priced property
                    "name": "Munnar Tea Hills Resort",
                    "rate_per_night": {"extracted_lowest": 2500},
                    "total_rate": {"extracted_lowest": 2500},
                },
            ]
        },
    )

    results = normalize_hotels(envelope, nights=1)
    assert len(results) == 1
    assert results[0].name == "Munnar Tea Hills Resort"
    assert results[0].total_price == Decimal("2500.00")


@pytest.mark.asyncio
async def test_09_no_booking_link_before_pass_unlock():
    """Confirm zero booking links appear before Trip Pass or /demo_pass."""
    orch = BudlanceOrchestrator(ai_service=AIIntentService(use_mock=True))
    orig_get = orch.cache_manager.get_travel_data

    async def _mock_get(engine, params, **kw):
        if engine == "google_flights" and params.get("arrival_id") == "COK":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine="google_flights",
                query_hash="mock_cok",
                data={"best_flights": [{"flights": [{"airline": "IndiGo", "flight_number": "6E-201"}], "price": 3000}]},
            )
        return await orig_get(engine, params, **kw)

    orch.cache_manager.get_travel_data = _mock_get
    chat_id = 992209

    await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="I have 50000 for 1 people for 2 days from chennai to munnar in flight",
    )
    t2 = await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message="9 Nov")
    assert "http://" not in t2.message_text
    assert "https://" not in t2.message_text
    assert "Provider Search Handoff" not in t2.message_text
    assert "Reply with *Booked*" not in t2.message_text


@pytest.mark.asyncio
async def test_10_past_date_rejection_and_month_rollover():
    """Past dates are rejected with clarification; passed month resolves to next year."""
    orch = BudlanceOrchestrator(ai_service=AIIntentService(use_mock=True))
    chat_id = 992210

    # Past date
    res_past = await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Plan a 3-day trip from Chennai to Munnar starting 2020-01-01 for 2 people with budget 30000",
    )
    assert res_past.status == "CLARIFICATION"
    assert "passed" in res_past.message_text.lower()

    # Rule-based rollover: "9 Jan" when current date is in late 2026 resolves to 2027-01-09
    ai_service = AIIntentService(use_mock=True)
    parsed = await ai_service.parse_trip_intent("Plan a 3-day trip from Chennai to Munnar starting 9 Jan for 2 people with budget 30000")
    assert parsed.start_date is not None
    assert parsed.start_date.startswith("2027-01-09")


@pytest.mark.asyncio
async def test_11_unlock_and_booking_links_flow():
    """Verify complete Judge/Demo bypass unlock delivers flight link, taxi transfer, and hotel selection."""
    orch = BudlanceOrchestrator(ai_service=AIIntentService(use_mock=True))
    orig_get = orch.cache_manager.get_travel_data

    async def _mock_get(engine, params, **kw):
        if engine == "google_flights" and params.get("arrival_id") == "COK":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine="google_flights",
                query_hash="mock_cok",
                data={
                    "best_flights": [
                        {
                            "flights": [{"airline": "IndiGo", "flight_number": "6E-201"}],
                            "price": 3000,
                            "booking_token": "token_munnar_123",
                        }
                    ]
                },
            )
        if engine == "google_hotels":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine="google_hotels",
                query_hash="mock_hotels",
                data={
                    "properties": [
                        {
                            "name": "Munnar Tea Hills Resort",
                            "rate_per_night": {"extracted_lowest": 2500},
                            "total_rate": {"extracted_lowest": 2500},
                            "overall_rating": 4.5,
                            "link": "https://www.google.com/travel/hotels/s/munnar_tea_hills",
                        }
                    ]
                },
            )
        return await orig_get(engine, params, **kw)

    orch.cache_manager.get_travel_data = _mock_get
    chat_id = 992211

    # Turn 1: Dateless message prompts for date
    t1 = await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="I have 50000 for 1 people for 2 days. I like to explore local food of that place and theme park currently i am in chennai like to go munnar in flight",
    )
    assert t1.status == "CLARIFICATION"
    assert "Which date do you want to start? (Example: 9 Nov)" in t1.message_text

    # Turn 2: Providing date returns locked preview without booking links
    t2 = await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message="9 Nov")
    assert t2.status == "FEASIBLE_TRANSPORT"
    assert "https://" not in t2.message_text

    # Turn 3: Judge sends /demo_pass to unlock
    t3 = await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message="/demo_pass")
    assert t3.is_pass_unlocked is True
    assert "Judge/Demo Bypass" not in t3.message_text
    assert "Budlance Trip Pass Unlocked" in t3.message_text
    assert "Verified" not in t3.message_text
    assert "Handoff" not in t3.message_text
    assert "/book/" in t3.message_text
    assert "Rs 3,000" in t3.message_text
    assert ".00" not in t3.message_text

    # Turn 4: User asks for links (all)
    t4 = await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message="send me the links")
    assert t4.is_pass_unlocked is True
    assert "Verified" not in t4.message_text
    assert "Handoff" not in t4.message_text
    assert "IndiGo (6E-201)" in t4.message_text
    assert "/book/" in t4.message_text
    assert "Munnar Tea Hills Resort" in t4.message_text
    assert "Book this flight:" in t4.message_text
    assert "Book this hotel:" in t4.message_text
    assert "*" not in t4.message_text
    assert ".00" not in t4.message_text


@pytest.mark.asyncio
async def test_12_required_flight_missing_returns_no_plan_and_no_zero_cost():
    """When requested flight returns no results, Budlance returns NOT_FEASIBLE and never produces a 0-cost plan."""
    orch = BudlanceOrchestrator(ai_service=AIIntentService(use_mock=True))

    async def _mock_get_empty_flights(engine, params, **kw):
        if engine == "google_flights":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine="google_flights",
                query_hash="mock_empty_flight",
                data={"best_flights": [], "other_flights": []},
            )
        return TravelDataEnvelope(
            source=DataSource.FALLBACK,
            engine=engine,
            query_hash="empty",
            data={},
            is_fallback=True,
        )

    orch.cache_manager.get_travel_data = _mock_get_empty_flights
    chat_id = 771122

    res = await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Plan a trip from Chennai to Leh for 2 days with 50000 by flight starting 2026-11-09",
    )
    assert res.status == "NOT_FEASIBLE"
    assert "No live flight data found" in res.message_text
    # Proves no ₹0 cost plan was produced
    assert "Rs 0" not in res.message_text
    assert "Total planned" not in res.message_text


@pytest.mark.asyncio
async def test_13_required_train_missing_returns_no_plan_and_no_zero_cost():
    """When requested train returns no results, Budlance returns NOT_FEASIBLE and never produces a 0-cost plan."""
    orch = BudlanceOrchestrator(ai_service=AIIntentService(use_mock=True))

    async def _mock_get_empty_trains(engine, params, **kw):
        return TravelDataEnvelope(
            source=DataSource.FALLBACK,
            engine=engine,
            query_hash="empty_train",
            data={},
            is_fallback=True,
        )

    orch.cache_manager.get_travel_data = _mock_get_empty_trains
    # Clear fallback provider train corridors for this test
    orch.cache_manager.fallback.get_train_corridor = lambda orig, dest: None

    chat_id = 771133
    res = await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Plan a trip from Port Blair to Leh for 2 days with 50000 by 2AC train starting 2026-11-09",
    )
    assert res.status == "NOT_FEASIBLE"
    assert "No live train data found" in res.message_text
    assert "Total planned" not in res.message_text


@pytest.mark.asyncio
async def test_14_required_bus_missing_returns_no_plan_and_no_zero_cost():
    """When requested bus returns no results, Budlance returns NOT_FEASIBLE and never produces a 0-cost plan."""
    orch = BudlanceOrchestrator(ai_service=AIIntentService(use_mock=True))

    orch.cache_manager.fallback.get_bus_corridor = lambda orig, dest: None
    async def _mock_empty(engine, params, **kw):
        return TravelDataEnvelope(
            source=DataSource.FALLBACK,
            engine=engine,
            query_hash="empty",
            data={},
            is_fallback=True,
        )
    orch.cache_manager.get_travel_data = _mock_empty

    chat_id = 771144
    res = await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Plan a trip from Chennai to Port Blair for 2 days with 50000 by bus starting 2026-11-09",
    )
    assert res.status == "NOT_FEASIBLE"
    assert "No transport found" in res.message_text or "No bus options found" in res.message_text
    assert "Total planned" not in res.message_text


@pytest.mark.asyncio
async def test_15_upward_optimization_triggers_below_60_percent_budget():
    """When total planned is < 60% of budget, stay is upgraded within nightly cap and extra day suggested."""
    orch = BudlanceOrchestrator(ai_service=AIIntentService(use_mock=True))

    async def _mock_get_options(engine, params, **kw):
        if engine == "google_flights" and params.get("arrival_id") == "COK":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine="google_flights",
                query_hash="mock_cok",
                data={
                    "best_flights": [
                        {"flights": [{"airline": "IndiGo", "flight_number": "6E-201"}], "price": 3000, "booking_token": "tok1"}
                    ]
                },
            )
        if engine == "google_hotels":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine="google_hotels",
                query_hash="mock_hotels",
                data={
                    "properties": [
                        {
                            "name": "Standard Lodge",
                            "rate_per_night": {"extracted_lowest": 2500},
                            "total_rate": {"extracted_lowest": 2500},
                            "overall_rating": 4.1,
                            "reviews": 100,
                        },
                        {
                            "name": "Grand Munnar Luxury Stay",
                            "rate_per_night": {"extracted_lowest": 4200},
                            "total_rate": {"extracted_lowest": 4200},
                            "overall_rating": 4.8,
                            "reviews": 85,
                        },
                    ]
                },
            )
        return TravelDataEnvelope(source=DataSource.FALLBACK, engine=engine, query_hash="fallback", data={}, is_fallback=True)

    orch.cache_manager.get_travel_data = _mock_get_options
    chat_id = 771155

    await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="I have 50000 for 1 people for 2 days. Currently in chennai like to go munnar in flight",
    )
    t2 = await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message="9 Nov")
    assert t2.status == "FEASIBLE_TRANSPORT"
    # Upward optimization selected upgraded stay ₹4,200 instead of standard ₹2,500
    assert "Stay, 1 night (estimate): Rs 4,200" in t2.message_text
    assert "consider adding an extra day to explore more." in t2.message_text


def test_16_get_book_id_handles_redirect_and_post_relay():
    """Verify /book/{id} performs 302 Redirect for search URLs and HTML form for POST payloads."""
    from fastapi.testclient import TestClient
    from budlance.api.app import app
    from budlance.db.repositories.cache_repo import CacheRepository

    client = TestClient(app)
    repo = CacheRepository()

    # 1. URL-only relay performs 302 redirect
    b_id1 = "test_redir_1"
    repo.store_booking_request(b_id1, {"url": "https://www.google.com/travel/flights?q=sample"})
    res1 = client.get(f"/book/{b_id1}", follow_redirects=False)
    assert res1.status_code == 302
    assert res1.headers["location"] == "https://www.google.com/travel/flights?q=sample"

    # 2. POST payload returns auto-submitting form HTML
    b_id2 = "test_post_2"
    repo.store_booking_request(b_id2, {"url": "https://www.goindigo.in/checkout", "post_data": {"id": "123"}})
    res2 = client.get(f"/book/{b_id2}")
    assert res2.status_code == 200
    assert 'action="https://www.goindigo.in/checkout"' in res2.text
    assert 'name="id" value="123"' in res2.text


def test_17_hotel_quality_threshold_ladder():
    """Verify hotel relaxation ladder from 4.0/50 -> 3.8/25 -> 3.5/10."""
    from decimal import Decimal
    from budlance.engine.optimizer import OptimizationEngine
    from budlance.engine.budget import ReverseBudgetEngine
    from budlance.estimation.estimator import EstimationLayer
    from budlance.schemas.travel import HotelOption

    opt = OptimizationEngine(budget_engine=ReverseBudgetEngine(), estimation_layer=EstimationLayer())

    h_poor = HotelOption(name="Poor", total_price=Decimal("2000"), rating=3.2, review_count=200)
    h_mid = HotelOption(name="Mid", total_price=Decimal("2500"), rating=3.6, review_count=15)
    h_good = HotelOption(name="Good", total_price=Decimal("3000"), rating=3.9, review_count=30)
    h_top = HotelOption(name="Top", total_price=Decimal("3500"), rating=4.5, review_count=60)

    # 1. Top hotel qualifies under 4.0/50 threshold
    selected = opt.select_preferred_hotel([h_poor, h_mid, h_good, h_top], hotel_tier="standard")
    assert selected.name == "Top"
    assert selected.is_relaxed_tier is False

    # 2. Relaxed to 3.8/30 when top is absent
    selected2 = opt.select_preferred_hotel([h_poor, h_mid, h_good], hotel_tier="standard")
    assert selected2.name == "Good"
    assert selected2.is_relaxed_tier is True

    # 3. Relaxed to 3.5/10 when top and good are absent
    selected3 = opt.select_preferred_hotel([h_poor, h_mid], hotel_tier="standard")
    assert selected3.name == "Mid"
    assert selected3.is_relaxed_tier is True

    # 4. Rejected when only below 3.5 available
    selected4 = opt.select_preferred_hotel([h_poor], hotel_tier="standard")
    assert selected4 is None


@pytest.mark.asyncio
async def test_18_live_off_empty_cache_unlock_has_no_names_or_flight_numbers():
    """When live mode is disabled and cache is empty, unlocked reply never leaks mock flight numbers or hotel names."""
    from budlance.config import get_settings
    settings = get_settings()
    settings.serpapi_live_enabled = False
    orch = BudlanceOrchestrator(ai_service=AIIntentService(use_mock=True))
    chat_id = 99887711

    await orch.handle_user_message(
        telegram_user_id=chat_id, chat_id=chat_id,
        message="I have 50000 for 1 people for 2 days to Munnar from Chennai"
    )
    await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message="9 Nov")
    t3 = await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message="/demo_pass")
    t4 = await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message="Show me flight link")

    for text in (t3.message_text, t4.message_text):
        assert "6E-201" not in text
        assert "Grand Munnar" not in text
        assert "(estimate)" in text


def test_19_book_id_friendly_expired_html_page():
    """Verify GET /book/{id} returns friendly HTML page when requested by a browser with Accept: text/html."""
    from fastapi.testclient import TestClient
    from budlance.api.app import app

    client = TestClient(app)
    res = client.get("/book/non_existent_id_xyz", headers={"Accept": "text/html"})
    assert res.status_code == 404
    assert "<title>Booking Link Expired - Budlance</title>" in res.text
    assert "https://www.google.com/travel/flights" in res.text


@pytest.mark.asyncio
async def test_20_highlights_omitted_when_no_live_maps_places():
    """Highlights line must be omitted completely when no live Maps places exist (offline mode)."""
    orch = BudlanceOrchestrator(ai_service=AIIntentService(use_mock=True))
    chat_id = 882233

    await orch.handle_user_message(
        telegram_user_id=chat_id, chat_id=chat_id,
        message="I have 50000 for 1 people for 2 days to Munnar from Chennai"
    )
    t2 = await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message="9 Nov")
    # In offline mode, no live places returned, so Highlights line must not appear
    assert "Highlights:" not in t2.message_text


@pytest.mark.asyncio
async def test_21_munnar_crowd_swap_two_step_proposal_and_confirmation():
    """Crowd swap classifies as crowd, truthful Munnar theme park response, and 2-step confirmation."""
    from budlance.db.models import BudgetAllocation, Itinerary, Trip, utc_now
    from uuid import uuid4

    orch = BudlanceOrchestrator(ai_service=AIIntentService(use_mock=True))
    chat_id = 991122

    user = orch.user_repo.get_or_create_user(telegram_user_id=chat_id, username="munnar_traveller")
    trip = orch.trip_repo.create_trip(
        user_id=user.id,
        telegram_chat_id=chat_id,
        origin="Chennai",
        destination="Munnar",
        duration_days=2,
        people_count=2,
        budget_total=Decimal("30000.00"),
        status="ACTIVE",
        is_active=True,
    )
    trip_id = trip.id
    orch.trip_pass_repo.create_pass(trip_id=trip_id, telegram_user_id=chat_id, telegram_chat_id=chat_id, status="PAID", amount=Decimal("49.00"))

    # Initial itinerary
    itin = Itinerary(
        id=uuid4(),
        trip_id=trip_id,
        days=[
            {
                "day_number": 1,
                "date_str": "Day 1",
                "theme_or_summary": "Nature sightseeing",
                "daily_estimated_cost": 0.0,
                "hotel_recommendation": "Tea County Munnar",
                "items": [
                    {
                        "time_slot": "Morning",
                        "place_name": "Mattupetty Lake",
                        "activity": "Boating and sightseeing at Mattupetty Lake",
                        "category": "attraction",
                        "planned_cost": 0.0,
                    }
                ],
            }
        ],
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    orch.itinerary_repo.save_itinerary(itin)

    # Initial budget allocation with Rs 5,000 emergency buffer
    alloc = BudgetAllocation(
        id=uuid4(),
        trip_id=trip_id,
        transport_allocated=Decimal("6000.00"),
        stay_allocated=Decimal("5000.00"),
        food_allocated=Decimal("4000.00"),
        activities_discretionary=Decimal("2000.00"),
        rescue_fund_allocated=Decimal("5000.00"),
        total_budget=Decimal("30000.00"),
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    orch.ledger_repo.save_budget_allocation(alloc)

    # Turn 1: Crowd disruption message
    res1 = await orch.handle_user_message(
        telegram_user_id=chat_id, chat_id=chat_id,
        message="too many people here, find a theme park"
    )
    msg1 = res1.message_text
    # Classification check: crowd
    assert "No theme park found in Munnar" in msg1
    assert "Wonder Valley Adventure and Amusement Park" in msg1
    assert "~14 km away" in msg1
    assert "Taxi to Wonder Valley (estimate): Rs 308" in msg1
    assert "New emergency buffer balance: Rs 4,692" in msg1
    assert "Go ahead? yes or no" in msg1
    # Forbidden scans: no emojis, no .00, no bucket words
    assert ".00" not in msg1
    assert "bucket" not in msg1.lower()
    assert "🌦️" not in msg1
    assert "⚠️" not in msg1

    # Invariant: plan is NOT changed yet!
    itin_check = orch.itinerary_repo.get_itinerary(trip_id)
    assert itin_check.days[0]["items"][0]["place_name"] == "Mattupetty Lake"

    # Turn 2: User says "yes"
    res2 = await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message="yes")
    msg2 = res2.message_text
    assert "Plan updated." in msg2
    assert "Wonder Valley Adventure and Amusement Park" in msg2
    assert "Taxi (estimate): Rs 308" in msg2
    assert "New emergency buffer balance: Rs 4,692" in msg2
    assert ".00" not in msg2
    assert "bucket" not in msg2.lower()
    assert "✅" not in msg2

    # Invariant: plan IS changed now!
    itin_updated = orch.itinerary_repo.get_itinerary(trip_id)
    assert itin_updated.days[0]["items"][0]["place_name"] == "Wonder Valley Adventure and Amusement Park"

    # Invariant: buffer updated in ledger
    alloc_updated = orch.ledger_repo.get_budget_allocation(trip_id)
    assert alloc_updated.rescue_fund_allocated == Decimal("4692")


@pytest.mark.asyncio
async def test_22_trip_end_single_total_unsplit_and_no_unspent_reserve():
    """Trip-end stores single total under total_spend, no unspent reserve, plain wording."""
    from budlance.db.models import BudgetAllocation, Trip, utc_now
    from uuid import uuid4

    orch = BudlanceOrchestrator(ai_service=AIIntentService(use_mock=True))
    chat_id = 991133

    user = orch.user_repo.get_or_create_user(telegram_user_id=chat_id, username="munnar_traveller_2")
    trip = orch.trip_repo.create_trip(
        user_id=user.id,
        telegram_chat_id=chat_id,
        origin="Chennai",
        destination="Munnar",
        duration_days=2,
        people_count=2,
        budget_total=Decimal("30000.00"),
        status="ACTIVE",
        is_active=True,
    )
    trip_id = trip.id

    # Prompt completion
    res1 = await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message="trip is done")
    assert "Your planned trip budget was Rs 30,000." in res1.message_text
    assert "Would you like to record your final actual spend? You can also say skip." in res1.message_text
    assert ".00" not in res1.message_text

    # Record actual spend Rs 18,500
    res2 = await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message="18500")
    msg2 = res2.message_text

    assert "Trip completed and reconciled." in msg2
    assert "Planned budget: Rs 30,000" in msg2
    assert "Recorded actual spend: Rs 18,500" in msg2
    assert "Difference: Rs 11,500 under planned budget" in msg2
    assert "Recorded actual spending is user-reported." in msg2

    # Invariants: no invented unspent reserve, no category breakdown, no emojis, no .00
    assert "Unspent Reserve" not in msg2
    assert "Actual Spending by Category" not in msg2
    assert "🎉" not in msg2
    assert "📊" not in msg2
    assert "🛡️" not in msg2
    assert ".00" not in msg2
    assert "₹" not in msg2

    # Invariant: stored under total_spend, NOT activities
    entries = orch.ledger_repo.get_ledger_entries(trip_id)
    recon_entries = [e for e in entries if e.description == "Total trip spend adjustment"]
    assert len(recon_entries) == 1
    assert recon_entries[0].category == "total_spend"
    assert recon_entries[0].actual_amount == Decimal("18500.00")


@pytest.mark.asyncio
async def test_23_in_trip_companion_today_plan_clean_plain_text():
    """In-trip companion today's plan is clean plain text with no emojis, no raw asterisks, and Rs."""
    from budlance.db.models import Itinerary, Trip, utc_now
    from uuid import uuid4

    orch = BudlanceOrchestrator(ai_service=AIIntentService(use_mock=True))
    chat_id = 991144

    user = orch.user_repo.get_or_create_user(telegram_user_id=chat_id, username="munnar_traveller_3")
    trip = orch.trip_repo.create_trip(
        user_id=user.id,
        telegram_chat_id=chat_id,
        origin="Chennai",
        destination="Munnar",
        duration_days=2,
        people_count=2,
        budget_total=Decimal("30000.00"),
        status="ACTIVE",
        is_active=True,
    )
    trip_id = trip.id
    orch.trip_pass_repo.create_pass(trip_id=trip_id, telegram_user_id=chat_id, telegram_chat_id=chat_id, status="PAID", amount=Decimal("49.00"))

    itin = Itinerary(
        id=uuid4(),
        trip_id=trip_id,
        days=[
            {
                "day_number": 1,
                "date_str": "Day 1",
                "theme_or_summary": "Nature sightseeing",
                "daily_estimated_cost": 0.0,
                "hotel_recommendation": "Tea County Munnar",
                "items": [
                    {
                        "time_slot": "Morning",
                        "place_name": "Tea Gardens",
                        "activity": "Walk through lush tea plantations",
                        "category": "nature",
                        "planned_cost": 0.0,
                    }
                ],
            }
        ],
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    orch.itinerary_repo.save_itinerary(itin)

    res = await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message="today's plan")
    msg = res.message_text

    assert "Plan for Day 1 in Munnar:" in msg
    assert "Tea Gardens: Walk through lush tea plantations" in msg
    assert "Stay: Tea County Munnar" in msg
    assert "🗓️" not in msg
    assert "🏨" not in msg
    assert "*Tea Gardens*" not in msg
    assert ".00" not in msg
    assert "₹" not in msg


@pytest.mark.asyncio
async def test_24_full_tanglish_lifecycle_munnar_flow():
    """Tanglish prompt produces date prompt, preview with 2 travellers and Rs 30,000, and unlock."""
    orch = BudlanceOrchestrator(ai_service=AIIntentService(use_mock=True))
    chat_id = 991155

    # Turn 1: Tanglish request
    res1 = await orch.handle_user_message(
        telegram_user_id=chat_id, chat_id=chat_id,
        message="Munnar ku 2 days trip plan pannu budget 30000 2 peru chennai la irundhu"
    )
    assert res1.status == "CLARIFICATION"
    assert "Which date do you want to start? (Example: 9 Nov)" in res1.message_text

    # Turn 2: Date specification
    res2 = await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message="9 Nov")
    msg2 = res2.message_text

    assert "Here is your Munnar plan preview" in msg2
    assert "2 days, 1 night" in msg2
    assert "2 travellers" in msg2
    assert "from Chennai" in msg2
    assert "of your Rs 30,000." in msg2
    assert "Emergency buffer" in msg2
    assert "Rs 5,000" in msg2
    assert "Change anything first, or unlock the full plan with flight and hotel links for Rs 49." in msg2
    assert ".00" not in msg2
    assert "bucket" not in msg2.lower()

    # Turn 3: Demo pass unlock
    res3 = await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message="/demo_pass")
    msg3 = res3.message_text
    assert "Budlance Trip Pass Unlocked!" in msg3 or "Trip Pass" in msg3




