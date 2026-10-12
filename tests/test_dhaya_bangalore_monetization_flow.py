"""Test suite verifying Dhaya's Bangalore plan preview, Stripe confirmation,
pre-payment customization blocking, post-payment unlocking, and insufficient budget detection.
"""

from decimal import Decimal
import pytest
from unittest.mock import AsyncMock

from budlance.ai.service import AIIntentService
from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.payment.service import PaymentService
from budlance.schemas.travel import FlightOption, HotelOption
from budlance.serpapi.models import DataSource, TravelDataEnvelope


@pytest.fixture
def mock_orch():
    """Build orchestrator with live trip pass enabled and mocked search/intent layers."""
    ai_service = AIIntentService(use_mock=True)
    orch = BudlanceOrchestrator(
        ai_service=ai_service,
        enable_trip_pass=True,
    )
    return orch


@pytest.mark.asyncio
async def test_01_bangalore_preview_has_no_booking_links_and_no_schedule(mock_orch):
    """Direct flight destination (Bangalore) must return cost preview with ZERO booking links and no daily schedule."""
    chat_id = 112233

    # Mock flights Chennai -> Bangalore (round-trip 8422)
    async def _mock_get(engine, params, **kw):
        if engine == "google_flights":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine="google_flights",
                query_hash="mock_blr_flight",
                data={
                    "best_flights": [
                        {
                            "flights": [{"airline": "Air India Express", "flight_number": "IX-101"}],
                            "price": 8422,
                        }
                    ]
                },
            )
        elif engine == "google_hotels":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine="google_hotels",
                query_hash="mock_blr_hotel",
                data={
                    "properties": [
                        {
                            "name": "Insta Rooms 1BHK",
                            "rate_per_night": {"extracted_lowest": 1498},
                            "total_rate": {"extracted_lowest": 1498},
                            "overall_rating": 4.5,
                            "reviews": 120,
                        }
                    ]
                },
            )
        elif engine == "google_maps":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine="google_maps",
                query_hash="mock_blr_maps",
                data={
                    "local_results": [
                        {"title": "Wonderla Amusement Park", "rating": 4.6}
                    ]
                },
            )
        return TravelDataEnvelope(source=DataSource.FALLBACK, engine=engine, query_hash="none", data={})

    mock_orch.cache_manager.get_travel_data = _mock_get

    res = await mock_orch.handle_user_message(
        chat_id=chat_id,
        telegram_user_id=chat_id,
        message=(
            "I have 50000 for 1 person for 2 days.i like to explore local food of that place "
            "and theme park currently I am in Chennai like to go bangalore in flight date Oct 15"
        ),
    )

    assert res.status == "FEASIBLE_TRANSPORT"
    text = res.message_text

    # Must contain clean preview header
    assert "Here is your Bangalore plan preview" in text or "Bengaluru" in text
    assert "Estimated cost" in text

    # Must show round-trip flight and stay cost
    assert "Flights Chennai to Bangalore and back: Rs 8,422" in text or "8,422" in text
    assert "Stay, 1 night: Rs 1,498" in text or "1,498" in text
    assert "Food and local travel (estimate): Rs 1,800" in text
    assert "Activities (estimate): Rs 2,500" in text
    assert "Emergency buffer" in text
    assert "Total planned: Rs" in text

    # Must prompt user to unlock for Rs 49
    assert "Change anything first, or unlock the full plan with flight and hotel links for Rs 49." in text
    assert "Reply 'Confirm plan' to proceed to checkout." in text

    # FORBIDDEN before payment:
    assert "🔗" not in text
    assert "http://" not in text
    assert "https://" not in text
    assert "Day 1:" not in text
    assert "Day 2:" not in text
    assert "Book flights" not in text


@pytest.mark.asyncio
async def test_02_confirm_plan_triggers_stripe_checkout(mock_orch):
    """Sending 'Confirm plan' generates Stripe checkout link for the Rs 49 fee."""
    chat_id = 112234

    # Setup planning trip
    user = mock_orch.user_repo.get_or_create_user(telegram_user_id=chat_id)
    trip = mock_orch.trip_repo.create_trip(
        user_id=user.id,
        telegram_chat_id=chat_id,
        budget_total=Decimal("50000.00"),
        destination="Bengaluru",
        origin="Chennai",
        duration_days=2,
        people_count=1,
        status="PLANNING",
        is_active=True,
    )

    res = await mock_orch.handle_user_message(
        chat_id=chat_id,
        telegram_user_id=chat_id,
        message="Confirm plan",
    )

    assert res.status == "CHECKOUT_PENDING"
    assert res.is_pass_unlocked is False
    assert "Budlance Trip Pass: Unlock Full Plan" in res.message_text
    assert "₹49" in res.message_text or "Rs 49" in res.message_text
    assert "Proceed to Secure Checkout" in res.message_text
    assert res.checkout_url is not None


@pytest.mark.asyncio
async def test_03_pre_payment_customization_blocked(mock_orch):
    """Customization / movie requests before paying must be blocked with checkout link."""
    chat_id = 112235

    user = mock_orch.user_repo.get_or_create_user(telegram_user_id=chat_id)
    trip = mock_orch.trip_repo.create_trip(
        user_id=user.id,
        telegram_chat_id=chat_id,
        budget_total=Decimal("50000.00"),
        destination="Bengaluru",
        origin="Chennai",
        duration_days=2,
        people_count=1,
        status="PLANNING",
        is_active=True,
    )

    # Attempt to ask for movie / customization without unlocking pass
    res = await mock_orch.handle_user_message(
        chat_id=chat_id,
        telegram_user_id=chat_id,
        message="On day 2 afternoon book a movie ticket",
    )

    assert res.status == "PASS_LOCKED"
    assert res.is_pass_unlocked is False
    assert "Locked" in res.message_text
    assert "Budlance Trip Pass" in res.message_text
    assert "Proceed to Secure Checkout" in res.message_text


@pytest.mark.asyncio
async def test_04_post_payment_unlocks_and_reveals_links_and_movies(mock_orch):
    """After paying (or demo pass), pass unlocks, revealing booking links and allowing movie discovery."""
    chat_id = 112236

    user = mock_orch.user_repo.get_or_create_user(telegram_user_id=chat_id)
    trip = mock_orch.trip_repo.create_trip(
        user_id=user.id,
        telegram_chat_id=chat_id,
        budget_total=Decimal("50000.00"),
        destination="Bengaluru",
        origin="Chennai",
        duration_days=2,
        people_count=1,
        status="PLANNING",
        is_active=True,
    )

    # Cache transport and hotel
    flight = FlightOption(
        airline="Air India Express",
        flight_number="IX-101",
        price=Decimal("8422.00"),
        deep_link="https://airindiaexpress.com/book",
        source=DataSource.LIVE,
    )
    hotel = HotelOption(
        name="Insta Rooms 1BHK",
        total_price=Decimal("1498.00"),
        deep_link="https://booking.com/instarooms",
        source=DataSource.LIVE,
    )
    mock_orch._cached_trip_bookings[trip.id] = (flight, hotel)

    # Unlock via demo_pass
    unlock_res = await mock_orch.handle_user_message(
        chat_id=chat_id,
        telegram_user_id=chat_id,
        message="/demo_pass",
    )
    assert unlock_res.is_pass_unlocked is True
    assert "Air India Express" in unlock_res.message_text
    assert "Book Air India Express Flight" in unlock_res.message_text
    assert "Insta Rooms 1BHK" in unlock_res.message_text

    # Now ask for movie recommendation - must be allowed!
    async def _mock_maps(engine, params, **kw):
        if engine == "google_maps" and "cinema" in params.get("q", ""):
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine="google_maps",
                query_hash="mock_cinemas",
                data={
                    "local_results": [
                        {"title": "PVR Cinemas Forum Mall", "rating": 4.4, "address": "Koramangala, Bengaluru"}
                    ]
                },
            )
        return TravelDataEnvelope(source=DataSource.FALLBACK, engine=engine, query_hash="none", data={})

    mock_orch.cache_manager.get_travel_data = _mock_maps

    movie_res = await mock_orch.handle_user_message(
        chat_id=chat_id,
        telegram_user_id=chat_id,
        message="Are there any movies playing nearby?",
    )
    assert movie_res.status == "IN_TRIP_QUERY"
    assert "Movie options" in movie_res.message_text or "PVR" in movie_res.message_text or "BookMyShow" in movie_res.message_text


@pytest.mark.asyncio
async def test_05_insufficient_budget_detection(mock_orch):
    """If budget is too low to cover transport + stay, must report insufficient money."""
    chat_id = 112237

    # Extremely low budget of Rs 1000 for Bangalore flight
    async def _mock_get(engine, params, **kw):
        if engine == "google_flights":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine="google_flights",
                query_hash="mock_blr_flight_inf",
                data={
                    "best_flights": [
                        {"flights": [{"airline": "IndiGo"}], "price": 8422}
                    ]
                },
            )
        return TravelDataEnvelope(source=DataSource.FALLBACK, engine=engine, query_hash="none", data={})

    mock_orch.cache_manager.get_travel_data = _mock_get

    res = await mock_orch.handle_user_message(
        chat_id=chat_id,
        telegram_user_id=chat_id,
        message="I have 1000 for 1 person for 2 days from Chennai to Bangalore in flight",
    )

    assert res.status == "NOT_FEASIBLE"
    assert "insufficient" in res.message_text.lower()
