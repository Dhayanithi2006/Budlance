"""Tests for dedicated booking links requests (hotel, flight, all).

Validates:
- Plain keyword recognition in English and Tanglish.
- Single-entity responses (hotel only, flight only, all).
- Locked paywall enforcement with Stripe checkout link and zero booking details.
- No trip guard asking user to plan a trip first.
- Empty cache / fallback state graceful message.
- Forbidden-word scan on all replies (no asterisks, no emoji headers, no bare URLs, no .00, whole rupees only).
"""

import pytest
import re
from decimal import Decimal
from budlance.ai.schemas import classify_booking_links_request, TripAction
from budlance.ai.service import AIIntentService
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.schemas.travel import DataSource, FlightOption, HotelOption

FORBIDDEN_WORDS = [
    "Financial Waterfall",
    "Bucket",
    "Rescue Reserve",
    "Cached",
    "Trip Pass: Active",
    "Selected Bookings",
    ".00",
]


def assert_clean_reply(text: str, max_lines: int | None = None) -> None:
    """Validate forbidden-word, formatting, and line-count invariants on a reply."""
    for word in FORBIDDEN_WORDS:
        assert word not in text, f"Forbidden word '{word}' found in response: {text!r}"

    # No markdown asterisks
    assert "*" not in text, f"Markdown asterisk '*' found in response: {text!r}"

    # No bare URLs: any http:// or https:// must be inside <a href="...">
    text_without_tags = re.sub(r'<a\s+href="[^"]*">[^<]*</a>', '', text)
    assert "http://" not in text_without_tags and "https://" not in text_without_tags, (
        f"Bare URL found outside <a> tag in response: {text_without_tags!r}"
    )

    if max_lines is not None:
        lines = [line for line in text.strip().split("\n") if line.strip()]
        assert len(lines) <= max_lines, f"Expected <= {max_lines} lines, got {len(lines)} lines:\n{text}"


@pytest.mark.parametrize(
    "query,expected_target",
    [
        ("link for booking hotels", "hotel"),
        ("hotel link", "hotel"),
        ("book hotel", "hotel"),
        ("send hotel booking link", "hotel"),
        ("hotel booking link anuppu", "hotel"),
        ("flight link", "flight"),
        ("book flights", "flight"),
        ("booking links", "all"),
        ("send me the links", "all"),
    ],
)
def test_keyword_classifier(query: str, expected_target: str):
    """Verify all Step 2 English and Tanglish phrases classify correctly."""
    is_bl, target = classify_booking_links_request(query)
    assert is_bl is True
    assert target == expected_target


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,expected_target",
    [
        ("link for booking hotels", "hotel"),
        ("hotel link", "hotel"),
        ("book hotel", "hotel"),
        ("send hotel booking link", "hotel"),
        ("hotel booking link anuppu", "hotel"),
        ("flight link", "flight"),
        ("book flights", "flight"),
        ("booking links", "all"),
        ("send me the links", "all"),
    ],
)
async def test_unlocked_booking_links_replies(query: str, expected_target: str):
    """Verify unlocked trip replies for each query in Step 2."""
    orch = BudlanceOrchestrator(ai_service=AIIntentService(use_mock=True))
    chat_id = 81001 + hash(query) % 1000

    # Step 1: Plan trip
    await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Plan a trip to Munnar from Chennai for 2 people (couple), budget 100000, 5 days",
    )
    await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message="9 Nov")

    # Step 2: Unlock trip
    await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message="/demo_pass")

    # Ensure live bookings are cached for testing live response format
    trip = orch.trip_repo.get_planning_trip(chat_id) or orch.trip_repo.get_active_trip(chat_id)
    live_flight = FlightOption(
        airline="IndiGo",
        flight_number="6E 847",
        price=Decimal("39440"),
        deep_link="https://www.google.com/travel/flights?q=Flights+to+Munnar",
        outbound_date="2026-11-09",
        return_date="2026-11-13",
        source=DataSource.LIVE,
    )
    live_hotel = HotelOption(
        name="Tea County Munnar",
        rating=4.4,
        review_count=1280,
        price_per_night=Decimal("6500"),
        total_price=Decimal("26000"),
        nights=4,
        check_in_date="2026-11-09",
        check_out_date="2026-11-13",
        deep_link="https://www.google.com/travel/hotels?q=Hotels+in+Munnar",
        source=DataSource.LIVE,
    )
    orch._cached_trip_bookings[trip.id] = (live_flight, live_hotel)

    # Step 3: Request links
    res = await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message=query)
    text = res.message_text

    if expected_target == "hotel":
        assert_clean_reply(text, max_lines=8)
        assert "Your stay in Munnar" in text
        assert "Book this hotel:" in text
        assert "You pay the hotel directly." in text
        assert "Flight" not in text
    elif expected_target == "flight":
        assert_clean_reply(text, max_lines=8)
        assert "Your flight to Munnar" in text
        assert "Book this flight:" in text
        assert "You pay the airline directly." in text
        assert "Hotel" not in text
    else:
        assert_clean_reply(text, max_lines=16)
        assert "Your flight to Munnar" in text
        assert "Your stay in Munnar" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "link for booking hotels",
        "hotel booking link anuppu",
        "flight link",
        "send me the links",
    ],
)
async def test_locked_booking_links_refusal(query: str):
    """Verify locked trip sends Trip Pass checkout link and shows zero travel/hotel details."""
    orch = BudlanceOrchestrator(ai_service=AIIntentService(use_mock=True))
    chat_id = 82001 + hash(query) % 1000

    await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Plan a trip to Munnar from Chennai for 2 people, budget 100000, 5 days",
    )
    await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message="9 Nov")

    res = await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message=query)
    text = res.message_text

    assert_clean_reply(text)
    assert res.is_pass_unlocked is False
    assert "Trip Pass" in text
    assert "checkout.stripe.com" in text or "Proceed to Secure Checkout" in text
    assert "IndiGo" not in text
    assert "Cottage" not in text
    assert "Resort" not in text
    assert "Book this hotel" not in text
    assert "Book this flight" not in text


@pytest.mark.asyncio
async def test_no_active_trip_booking_links_reply():
    """Verify request without an active trip asks user to plan a trip first in one short line."""
    orch = BudlanceOrchestrator(ai_service=AIIntentService(use_mock=True))
    chat_id = 83001

    res = await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message="Link for booking hotels")
    text = res.message_text.strip()

    assert_clean_reply(text, max_lines=1)
    assert text == "Please plan a trip first."


@pytest.mark.asyncio
async def test_empty_cache_fallback_hotel_reply():
    """Verify that when live data is off and cache is empty, fallback displays graceful message."""
    orch = BudlanceOrchestrator(ai_service=AIIntentService(use_mock=True))
    chat_id = 84001

    # Plan trip
    await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Plan a trip to Munnar from Chennai for 2 people, budget 100000, 5 days",
    )
    await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message="9 Nov")
    await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message="/demo_pass")

    # Manually simulate empty cache / offline fallback hotel (no live data)
    trip = orch.trip_repo.get_planning_trip(chat_id) or orch.trip_repo.get_active_trip(chat_id)
    cached_trans, _ = orch._cached_trip_bookings.get(trip.id, (None, None))
    fallback_hotel = HotelOption(
        name="Munnar Budget Lodging",
        total_price=Decimal("8000"),
        price_per_night=Decimal("2000"),
        nights=4,
        source=DataSource.FALLBACK,
    )
    orch._cached_trip_bookings[trip.id] = (cached_trans, fallback_hotel)

    res = await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message="Link for booking hotels")
    text = res.message_text

    assert_clean_reply(text, max_lines=4)
    assert "Your stay in Munnar" in text
    assert "I could not get live hotel details for these dates." in text
    assert "Munnar Budget Lodging" not in text
    assert "stars" not in text
