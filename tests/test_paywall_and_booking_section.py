"""Tests for Item 4: Paywall behavior.

Rules tested:
1. Free summary has no booking links (zero travel/hotel deep links or IRCTC links).
2. Free summary locks itinerary (generated_itinerary is None, zero day schedule).
3. After Trip Pass (or demo flag) show itinerary plus booking section.
"""

from decimal import Decimal
from unittest.mock import AsyncMock
import pytest

from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.ai.service import AIIntentService
from budlance.db.repositories.conversation_repo import ConversationStateRepository
from budlance.db.repositories.intent_repo import IntentRepository
from budlance.db.repositories.itinerary_repo import ItineraryRepository
from budlance.db.repositories.ledger_repo import LedgerRepository
from budlance.db.repositories.trip_pass_repo import TripPassRepository
from budlance.db.repositories.trip_repo import TripRepository
from budlance.db.repositories.user_repo import UserRepository
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.payment.service import PaymentService


def _build_test_orchestrator(enable_trip_pass: bool = True) -> tuple[BudlanceOrchestrator, dict]:
    client = None
    user_repo = UserRepository(client=client)
    trip_repo = TripRepository(client=client)
    itinerary_repo = ItineraryRepository(client=client)
    ledger_repo = LedgerRepository(client=client)
    conv_repo = ConversationStateRepository(client=client)
    intent_repo = IntentRepository(client=client)
    trip_pass_repo = TripPassRepository(client=client)

    payment_service = PaymentService(
        trip_pass_repo=trip_pass_repo,
        pass_amount=Decimal("49.00"),
        pass_currency="INR",
    )

    ai_service = AIIntentService(use_mock=True)

    orc = BudlanceOrchestrator(
        user_repo=user_repo,
        trip_repo=trip_repo,
        itinerary_repo=itinerary_repo,
        ledger_repo=ledger_repo,
        conversation_repo=conv_repo,
        intent_repo=intent_repo,
        trip_pass_repo=trip_pass_repo,
        payment_service=payment_service,
        ai_service=ai_service,
        enable_trip_pass=enable_trip_pass,
    )

    repos = {
        "user_repo": user_repo,
        "trip_repo": trip_repo,
        "itinerary_repo": itinerary_repo,
        "ledger_repo": ledger_repo,
        "trip_pass_repo": trip_pass_repo,
        "payment_service": payment_service,
    }
    return orc, repos


@pytest.mark.asyncio
async def test_free_summary_contains_no_booking_links_and_locks_itinerary():
    """Requirement 4a: Free summary has NO booking links and locks the day-by-day itinerary."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    msg = "Plan a 2-day trip from Chennai to Goa for 2 people with budget 20000"
    res = await orc.handle_user_message(telegram_user_id=701, chat_id=7001, message=msg)

    assert res.status == "FEASIBLE"
    assert res.is_pass_unlocked is False

    # 1. Objects hidden from free tier return
    assert res.generated_itinerary is None
    assert res.selected_transport is None
    assert res.selected_hotel is None

    # 2. Message contains Financial Waterfall & Lock CTA
    assert "Financial Waterfall" in res.message_text
    assert "Detailed Itinerary & Rescue Locked" in res.message_text
    assert "Budlance Trip Pass" in res.message_text

    # 3. ZERO booking links in message text (no irctc, no airline, no hotel deep link)
    assert "irctc.co.in" not in res.message_text
    assert "Selected Bookings" not in res.message_text
    assert "Day-by-Day Schedule" not in res.message_text
    assert "🔗 Booking:" not in res.message_text

    # The only link allowed in free summary is the Stripe checkout URL
    checkout_url = res.checkout_url
    assert checkout_url is not None
    lines_with_links = [
        line for line in res.message_text.splitlines()
        if "http://" in line or "https://" in line
    ]
    # Every link line must only be the checkout link
    for line in lines_with_links:
        assert checkout_url in line


@pytest.mark.asyncio
async def test_unlocked_via_trip_pass_shows_itinerary_plus_booking_section():
    """Requirement 4b: After Trip Pass payment, show itinerary plus booking section."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    msg = "Plan a 2-day trip from Chennai to Goa for 2 people with budget 20000"
    res_free = await orc.handle_user_message(telegram_user_id=702, chat_id=7002, message=msg)
    trip_id = res_free.trip_id

    # Unlock via Trip Pass payment confirmation
    v_res = await repos["payment_service"].verify_webhook_event(
        provider="demo",
        payload={"trip_id": str(trip_id), "status": "paid", "payment_id": "pay_test_702"},
    )
    assert v_res.status == "PAID"

    # User verifies or says "paid"
    res_unlocked = await orc.handle_user_message(telegram_user_id=702, chat_id=7002, message="paid")

    assert res_unlocked.is_pass_unlocked is True
    assert res_unlocked.generated_itinerary is not None
    assert res_unlocked.selected_transport is not None

    # Shows BOTH:
    # 1. Booking section
    assert "Selected Bookings:" in res_unlocked.message_text
    assert "Transport:" in res_unlocked.message_text
    assert "🔗 Booking:" in res_unlocked.message_text

    # 2. Day-by-Day Itinerary schedule
    assert "Day-by-Day Schedule:" in res_unlocked.message_text
    assert "Day 1:" in res_unlocked.message_text
    assert "Day 2:" in res_unlocked.message_text


@pytest.mark.asyncio
async def test_unlocked_via_demo_pass_command_shows_itinerary_plus_booking_section():
    """Requirement 4c: After demo command (/demo_pass), show itinerary plus booking section."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    msg = "Plan a 2-day trip from Chennai to Goa for 2 people with budget 20000"
    await orc.handle_user_message(telegram_user_id=703, chat_id=7003, message=msg)

    # Bypass via /demo_pass
    res_demo = await orc.handle_user_message(telegram_user_id=703, chat_id=7003, message="/demo_pass")

    assert res_demo.is_pass_unlocked is True
    assert res_demo.generated_itinerary is not None
    assert res_demo.selected_transport is not None

    # Booking section present
    assert "Selected Bookings:" in res_demo.message_text
    assert "Transport:" in res_demo.message_text
    assert "🔗 Booking:" in res_demo.message_text

    # Itinerary present
    assert "Day-by-Day Schedule:" in res_demo.message_text
    assert "Day 1:" in res_demo.message_text


@pytest.mark.asyncio
async def test_demo_bypass_flag_immediately_shows_itinerary_plus_booking_section():
    """Requirement 4d: Request with demo flag bypasses paywall and immediately shows itinerary plus booking section."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    msg = "Plan a 2-day trip from Chennai to Goa for 2 people with budget 20000"
    res = await orc.handle_user_message(
        telegram_user_id=704,
        chat_id=7004,
        message=msg,
        demo_bypass=True,
    )

    assert res.is_pass_unlocked is True
    assert res.generated_itinerary is not None
    assert res.selected_transport is not None

    # Immediately shows both booking section and itinerary
    assert "Selected Bookings:" in res.message_text
    assert "Transport:" in res.message_text
    assert "Day-by-Day Schedule:" in res.message_text
    assert "Detailed Itinerary & Rescue Locked" not in res.message_text
