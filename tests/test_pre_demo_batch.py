"""Regression and validation tests for Pre-Demo Batch Fixes (offline only):
1. dynamic_transit: never invent a service name. Label results as
   "Estimated <mode> fare (distance-based)". Chennai->Port Blair and Chennai->Leh
   must not return a train. Missing transport must yield "no transport found for this route",
   never a "deficit 0" message.
2. CHANGE_BUDGET/DAYS/PEOPLE must preserve travel_party and interests.
3. If requested interests have no match at the destination, say so in one line and offer alternatives.
   Collapse repeated "self-guided exploration" lines into one line when no curated attractions exist.
4. For CHANGE_* actions, return a compact summary (what changed, new total, new surplus),
   with "full plan" available on request.
"""

from decimal import Decimal
import pytest
from uuid import uuid4

from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.ai.service import AIIntentService
from budlance.estimation.dynamic_transit import DynamicTransitGenerator
from budlance.orchestrator.formatter import (
    format_change_summary,
    format_feasibility_result,
    format_infeasible_plan,
    resolve_interest_mismatch_note,
)
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.engine.models import BudgetBreakdown


# =========================================================================
# Item 1 Tests: dynamic_transit names, no-train routes, missing transport message
# =========================================================================

def test_dynamic_transit_never_invents_service_names():
    """1a. Never invent a service name. Label results as 'Estimated <mode> fare (distance-based)'."""
    gen = DynamicTransitGenerator()
    options = gen.generate_options("Chennai", "Bangalore", people=2)
    assert len(options) > 0

    for opt in options:
        assert opt.name_or_operator in (
            "Estimated train fare (distance-based)",
            "Estimated bus fare (distance-based)",
        )
        assert opt.name_or_operator == f"Estimated {opt.transit_type} fare (distance-based)"
        # Confirm no invented names like 'Superfast Express' or 'Intercity AC Sleeper'
        assert "Superfast Express" not in opt.name_or_operator
        assert "Express (" not in opt.name_or_operator
        assert "Intercity AC" not in opt.name_or_operator


def test_dynamic_transit_no_train_for_port_blair_and_leh():
    """1b. Chennai->Port Blair and Chennai->Leh must not return a train."""
    gen = DynamicTransitGenerator()

    # Chennai -> Port Blair (island route separated by sea)
    pb_opts = gen.generate_options("Chennai", "Port Blair", people=1)
    assert not any(opt.transit_type == "train" for opt in pb_opts), "Chennai->Port Blair must not return a train"

    # Chennai -> Leh (high-altitude mountain route with no railway connectivity)
    leh_opts = gen.generate_options("Chennai", "Leh", people=1)
    assert not any(opt.transit_type == "train" for opt in leh_opts), "Chennai->Leh must not return a train"


@pytest.mark.asyncio
async def test_missing_transport_yields_no_transport_found_never_deficit_0(monkeypatch):
    """1c. Missing transport must yield 'no transport found for this route', never a 'deficit 0' message."""
    monkeypatch.setenv("SERPAPI_LIVE_ENABLED", "false")

    mock_ai = AIIntentService(use_mock=True)
    orch = BudlanceOrchestrator(ai_service=mock_ai)

    # Port Blair from Chennai has no surface transit (train/bus)
    res = await orch.handle_user_message(
        telegram_user_id=88991,
        chat_id=88991,
        message="Plan a 3-day trip from Chennai to Port Blair for 2 people with budget 20000",
    )

    assert res.status == "NOT_FEASIBLE"
    assert "no transport found for this route" in res.message_text.lower()
    # Must never present a budget deficit or "deficit 0" message when transport is absent
    assert "deficit: inr 0.00" not in res.message_text.lower()
    assert "deficit:" not in res.message_text.lower()
    assert "trip plan not feasible within budget" not in res.message_text.lower()


def test_format_infeasible_plan_missing_transport_never_deficit_0():
    """1d. format_infeasible_plan with transport rejection yields no transport message without deficit."""
    formatted = format_infeasible_plan(
        destination="Port Blair",
        budget=Decimal("20000.00"),
        deficit=Decimal("0.00"),
        explanation="No physical transport options could be resolved between Chennai and Port Blair.",
    )
    assert "no transport found for this route" in formatted.lower()
    assert "deficit" not in formatted.lower()


# =========================================================================
# Item 2 Tests: CHANGE_BUDGET/DAYS/PEOPLE must preserve travel_party & interests
# =========================================================================

def test_change_actions_preserve_travel_party_and_interests_unit():
    """2a. Unit test: apply_change_action preserves travel_party and interests for CHANGE_BUDGET, CHANGE_DAYS, CHANGE_PEOPLE."""
    base_intent = ParsedTripIntent(
        action=TripAction.NEW_TRIP,
        budget=Decimal("10000.00"),
        people=2,
        days=2,
        origin="Chennai",
        destination="Hosur",
        interests=["beach", "theme park"],
        travel_party="couple",
        traveler_type="couple",
    )

    # 1. CHANGE_BUDGET
    update_budget = ParsedTripIntent(action=TripAction.CHANGE_BUDGET, budget=Decimal("15000.00"))
    res_b = base_intent.apply_change_action(update_budget)
    assert res_b.budget == Decimal("15000.00")
    assert res_b.travel_party == "couple"
    assert res_b.interests == ["beach", "theme park"]

    # 1b. CHANGE_BUDGET with delta
    update_delta = ParsedTripIntent(action=TripAction.CHANGE_BUDGET, is_delta=True, budget_delta=Decimal("5000.00"))
    res_delta = base_intent.apply_change_action(update_delta)
    assert res_delta.budget == Decimal("15000.00")
    assert res_delta.travel_party == "couple"
    assert res_delta.interests == ["beach", "theme park"]

    # 2. CHANGE_DAYS
    update_days = ParsedTripIntent(action=TripAction.CHANGE_DAYS, days=4)
    res_d = base_intent.apply_change_action(update_days)
    assert res_d.days == 4
    assert res_d.travel_party == "couple"
    assert res_d.interests == ["beach", "theme park"]

    # 3. CHANGE_PEOPLE (preserves party like friends/family and interests)
    base_friends = base_intent.model_copy(update={"travel_party": "friends", "traveler_type": "friends", "people": 3})
    update_people = ParsedTripIntent(action=TripAction.CHANGE_PEOPLE, people=4)
    res_p = base_friends.apply_change_action(update_people)
    assert res_p.people == 4
    assert res_p.travel_party == "friends"
    assert res_p.interests == ["beach", "theme park"]


@pytest.mark.asyncio
async def test_orchestrator_multi_turn_preserves_travel_party_and_interests(monkeypatch):
    """2b. Multi-turn regression test: planning trip loaded on change action preserves party & interests."""
    monkeypatch.setenv("SERPAPI_LIVE_ENABLED", "false")

    mock_ai = AIIntentService(use_mock=True)
    orch = BudlanceOrchestrator(ai_service=mock_ai)
    chat_id = 998877

    # Turn 1
    msg1 = "I would like to explore beach and theme park, budget 10000, 2 people couple, 2 days from Chennai to Hosur"
    res1 = await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message=msg1)
    assert res1.status == "FEASIBLE"

    trip1 = orch.trip_repo.get_planning_trip(chat_id) or orch.trip_repo.get_active_trip(chat_id)
    assert trip1 is not None
    saved_intent1 = orch.intent_repo.get_trip_intent(trip1.id)
    assert saved_intent1 is not None
    assert saved_intent1.travel_party == "couple"
    assert "beach" in saved_intent1.interests
    assert "theme park" in saved_intent1.interests

    # Turn 2: Change budget
    msg2 = "Add 5000 to my budget"
    res2 = await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message=msg2)
    assert res2.status == "FEASIBLE"

    trip2 = orch.trip_repo.get_planning_trip(chat_id) or orch.trip_repo.get_active_trip(chat_id)
    saved_intent2 = orch.intent_repo.get_trip_intent(trip2.id)
    assert saved_intent2 is not None
    assert saved_intent2.travel_party == "couple"
    assert "beach" in saved_intent2.interests
    assert "theme park" in saved_intent2.interests


# =========================================================================
# Item 3 Tests: Interest mismatch 1-line note & Mode B self-guided collapse
# =========================================================================

def test_interest_mismatch_one_line_note_and_alternatives():
    """3a. If requested interests have no match at the destination, say so in one line and offer alternatives."""
    # Hosur has no beach or theme park
    note = resolve_interest_mismatch_note(
        destination="Hosur",
        requested_interests=["beach", "theme park"],
        curated_attractions=[],
        places=[],
    )
    assert note is not None
    assert "\n" not in note, "Must be exactly one line"
    assert "Note: Hosur has no matching beach and theme park attractions" in note
    assert "Pondicherry" in note or "Goa" in note or "Bangalore" in note


def test_interest_mismatch_excludes_origin_and_keys_to_unmatched_interests():
    """Interest-mismatch note must exclude user's origin city and key suggestions to unmatched interests."""
    # Origin is Chennai -> theme park suggestions must NOT include Chennai
    note_chennai = resolve_interest_mismatch_note(
        destination="Hosur",
        requested_interests=["beach", "theme park"],
        curated_attractions=[],
        places=[],
        origin="Chennai",
    )
    assert note_chennai is not None
    assert "\n" not in note_chennai
    assert "Chennai" not in note_chennai
    assert "Bangalore (Wonderla)" in note_chennai
    assert "for beach" in note_chennai
    assert "for theme park" in note_chennai

    # Origin is Bangalore -> theme park suggestions must NOT include Bangalore
    note_bangalore = resolve_interest_mismatch_note(
        destination="Hosur",
        requested_interests=["theme park"],
        curated_attractions=[],
        places=[],
        origin="Bangalore",
    )
    assert note_bangalore is not None
    assert "Bangalore" not in note_bangalore
    assert "Chennai (VGP/MGM)" in note_bangalore
    assert "for theme park" in note_bangalore


@pytest.mark.asyncio
async def test_mode_b_collapses_repeated_self_guided_exploration_lines(monkeypatch):
    """3b. Collapse repeated 'self-guided exploration' lines into one line when no curated attractions exist."""
    monkeypatch.setenv("SERPAPI_LIVE_ENABLED", "false")

    mock_ai = AIIntentService(use_mock=True)
    orch = BudlanceOrchestrator(ai_service=mock_ai)
    chat_id = 771122

    # Hosur has no curated attractions (Mode B)
    res = await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="I would like to explore beach and theme park, budget 10000, 2 people couple, 2 days from Chennai to Hosur",
    )
    assert res.status == "FEASIBLE"
    # One line note for unmatched interests
    assert "Note: Hosur has no matching beach and theme park attractions" in res.message_text

    # Verify self-guided exploration is collapsed to 1 line, not repeated 4-6 times across every day/slot
    schedule_text = res.message_text.lower()
    assert "self-guided exploration and local dining in hosur at your own pace." in schedule_text
    assert schedule_text.count("self-guided exploration") == 1


# =========================================================================
# Item 4 Tests: CHANGE_* compact summary with 'full plan' on request
# =========================================================================

@pytest.mark.asyncio
async def test_change_action_compact_summary_and_full_plan_request(monkeypatch):
    """4. For CHANGE_* actions, return a compact summary (what changed, new total, new surplus), with 'full plan' on request."""
    monkeypatch.setenv("SERPAPI_LIVE_ENABLED", "false")

    mock_ai = AIIntentService(use_mock=True)
    orch = BudlanceOrchestrator(ai_service=mock_ai)
    chat_id = 665544

    # Turn 1: Initial trip
    t1_text = "I would like to explore beach and theme park, budget 10000, 2 people couple, 2 days from Chennai to Hosur"
    res1 = await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message=t1_text)
    assert res1.status == "FEASIBLE"
    assert "Budlance Trip Plan: Hosur" in res1.message_text

    # Turn 2: CHANGE_BUDGET action
    t2_text = "Add 5000 to my budget"
    res2 = await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message=t2_text)
    assert res2.status == "FEASIBLE"
    assert res2.action == TripAction.CHANGE_BUDGET

    # Compact summary checks
    assert "Trip Plan Updated for Hosur" in res2.message_text
    assert "*Change:*" in res2.message_text
    assert "Budget updated to INR 15,000.00" in res2.message_text
    assert "*New Total Budget:* INR 15,000.00" in res2.message_text
    assert "*New Surplus:*" in res2.message_text
    assert 'Reply "full plan" to view the complete schedule.' in res2.message_text
    # Compact summary should NOT contain full day-by-day schedule
    assert "Day-by-Day Schedule" not in res2.message_text

    # Turn 3: User requests 'full plan'
    t3_text = "full plan"
    res3 = await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message=t3_text)
    assert res3.status == "FEASIBLE"
    assert "Budlance Trip Plan: Hosur" in res3.message_text
    assert "Total Budget: INR 15,000.00" in res3.message_text
    assert "Financial Waterfall" in res3.message_text
    assert "Day-by-Day Schedule" in res3.message_text


# =========================================================================
# Formatter Agreement Tests
# =========================================================================

def test_feasibility_formatter_single_source_of_truth_agreement():
    """Verify that format_infeasible_plan and format_feasibility_result never contradict."""
    contradictory_explanation = "Trip is feasible within INR 10000 with a surplus of INR 2400.00."

    formatted = format_infeasible_plan(
        destination="Hosur",
        budget=Decimal("10000.00"),
        deficit=Decimal("0.00"),
        explanation=contradictory_explanation,
    )

    assert "Trip Plan Not Feasible within Budget" in formatted
    assert "surplus of INR 2400.00" not in formatted
    assert "Trip is feasible within" not in formatted
    assert "• *Deficit:* INR 0.00" not in formatted
    assert "• *Deficit:* INR 1,000.00" in formatted or "• *Deficit:* INR 500.00" in formatted


def test_format_feasibility_result_agreement():
    """Verify unified dispatcher maintains single source of truth."""
    breakdown = BudgetBreakdown(
        total_budget=Decimal("10000.00"),
        bucket_a_fixed=Decimal("4000.00"),
        bucket_b_survival=Decimal("3000.00"),
        bucket_c_activities=Decimal("500.00"),
        bucket_d_rescue=Decimal("1000.00"),
        transport_cost=Decimal("2000.00"),
        hotel_cost=Decimal("2000.00"),
        food_cost=Decimal("2500.00"),
        local_transit_cost=Decimal("500.00"),
        attraction_cost=Decimal("0.00"),
        total_allocated=Decimal("8500.00"),
        remaining_surplus=Decimal("1500.00"),
    )

    # 1. Feasible result
    msg_feasible = format_feasibility_result(
        is_feasible=True,
        is_pass_unlocked=True,
        destination="Hosur",
        days=2,
        people=2,
        breakdown=breakdown,
    )
    assert "🌴 *Budlance Trip Plan: Hosur*" in msg_feasible
    assert "Trip Plan Not Feasible" not in msg_feasible

    # 2. Infeasible result
    msg_infeasible = format_feasibility_result(
        is_feasible=False,
        is_pass_unlocked=False,
        destination="Hosur",
        days=2,
        people=2,
        breakdown=breakdown,
        deficit=Decimal("0.00"),
        explanation="Trip is feasible with surplus of 2000",
    )
    assert "❌ *Trip Plan Not Feasible within Budget*" in msg_infeasible
    assert "surplus of 2000" not in msg_infeasible


# =========================================================================
# Payment Tests: Real Stripe Checkout Session & Clearly Labeled Simulated Flow
# =========================================================================

@pytest.mark.asyncio
async def test_stripe_real_checkout_session_when_credentials_exist(monkeypatch):
    """When STRIPE_API_KEY exists, create real Stripe test Checkout Session via API."""
    from uuid import uuid4
    from budlance.config import get_settings
    from budlance.payment.service import PaymentService
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setenv("STRIPE_API_KEY", "rk_test_dummy_key_12345")
    get_settings.cache_clear()

    mock_http = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "id": "cs_test_real_issued_123",
        "url": "https://checkout.stripe.com/c/pay/cs_test_real_issued_123",
    }
    mock_http.post = AsyncMock(return_value=mock_resp)

    svc = PaymentService(http_client=mock_http)
    trip_id = uuid4()
    session = await svc.create_checkout_session(trip_id=trip_id, chat_id=1234, provider="stripe")

    assert session.provider == "stripe"
    assert session.payment_reference == "cs_test_real_issued_123"
    assert session.checkout_url == "https://checkout.stripe.com/c/pay/cs_test_real_issued_123"
    assert mock_http.post.call_count == 1
    call_args = mock_http.post.call_args
    assert call_args[0][0] == "https://api.stripe.com/v1/checkout/sessions"
    assert call_args[1]["headers"]["Authorization"] == "Bearer rk_test_dummy_key_12345"

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_simulated_flow_when_stripe_credentials_absent_never_fake_provider_url(monkeypatch):
    """When STRIPE_API_KEY absent, return clearly labeled simulated flow; never return unissued Stripe/Razorpay URL."""
    from uuid import uuid4
    from budlance.config import get_settings
    from budlance.payment.service import PaymentService

    monkeypatch.setenv("STRIPE_API_KEY", "")
    get_settings.cache_clear()

    svc = PaymentService()
    trip_id = uuid4()
    session = await svc.create_checkout_session(trip_id=trip_id, chat_id=5678, provider="stripe")

    assert session.provider == "demo"
    assert "checkout.stripe.com" not in session.checkout_url
    assert "rzp.io" not in session.checkout_url
    assert "budlance.travel" in session.checkout_url
    assert str(trip_id) in session.checkout_url

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_stripe_error_or_timeout_falls_back_to_simulated_flow(monkeypatch):
    """If Stripe returns an error or times out, fall back to the labeled simulated flow."""
    import httpx
    from uuid import uuid4
    from budlance.config import get_settings
    from budlance.payment.service import PaymentService
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setenv("STRIPE_API_KEY", "rk_test_dummy_key_12345")
    get_settings.cache_clear()

    # 1. Timeout scenario
    mock_http_timeout = MagicMock()
    mock_http_timeout.post = AsyncMock(side_effect=httpx.ConnectTimeout("Connection timed out"))

    svc_timeout = PaymentService(http_client=mock_http_timeout)
    trip_id1 = uuid4()
    session1 = await svc_timeout.create_checkout_session(trip_id=trip_id1, chat_id=111, provider="stripe")
    assert session1.provider == "demo"
    assert "budlance.travel" in session1.checkout_url
    assert "checkout.stripe.com" not in session1.checkout_url

    # 2. 500 error scenario
    mock_http_500 = MagicMock()
    resp_500 = MagicMock()
    resp_500.status_code = 500
    resp_500.raise_for_status.side_effect = httpx.HTTPStatusError("Server error", request=MagicMock(), response=resp_500)
    mock_http_500.post = AsyncMock(return_value=resp_500)

    svc_500 = PaymentService(http_client=mock_http_500)
    trip_id2 = uuid4()
    session2 = await svc_500.create_checkout_session(trip_id=trip_id2, chat_id=222, provider="stripe")
    assert session2.provider == "demo"
    assert "budlance.travel" in session2.checkout_url
    assert "checkout.stripe.com" not in session2.checkout_url

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_user_says_paid_confirms_via_stripe_session_poll(monkeypatch):
    """When user says 'paid', orchestrator checks Stripe Checkout Session and unlocks if payment_status=='paid'."""
    from uuid import uuid4
    from budlance.config import get_settings
    from budlance.orchestrator.orchestrator import BudlanceOrchestrator
    from budlance.payment.service import PaymentService
    from budlance.db.repositories.trip_pass_repo import TripPassRepository
    from budlance.db.repositories.trip_repo import TripRepository
    from budlance.db.repositories.user_repo import UserRepository
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setenv("SERPAPI_LIVE_ENABLED", "false")
    monkeypatch.setenv("STRIPE_API_KEY", "rk_test_dummy_key_12345")
    get_settings.cache_clear()

    mock_http = MagicMock()
    # Mock Stripe session fetch returning paid
    resp_paid = MagicMock()
    resp_paid.status_code = 200
    resp_paid.json.return_value = {
        "id": "cs_test_session_poll_123",
        "payment_status": "paid",
        "status": "complete",
    }
    mock_http.get = AsyncMock(return_value=resp_paid)

    pass_repo = TripPassRepository()
    payment_svc = PaymentService(trip_pass_repo=pass_repo, http_client=mock_http)
    user_repo = UserRepository()
    trip_repo = TripRepository()

    orch = BudlanceOrchestrator(
        user_repo=user_repo,
        trip_repo=trip_repo,
        payment_service=payment_svc,
        trip_pass_repo=pass_repo,
    )

    chat_id = 991122
    user = user_repo.get_or_create_user(telegram_user_id=chat_id)
    trip = trip_repo.create_trip(
        user_id=user.id,
        telegram_chat_id=chat_id,
        budget_total=Decimal("15000.00"),
        destination="Ooty",
        origin="Chennai",
        status="PLANNING",
        duration_days=3,
        people_count=2,
    )

    # Create pending pass with Stripe session ref
    pass_repo.create_pass(
        trip_id=trip.id,
        telegram_user_id=chat_id,
        telegram_chat_id=chat_id,
        amount=Decimal("49.00"),
        currency="INR",
        provider="stripe",
        payment_reference="cs_test_session_poll_123",
        status="CHECKOUT_PENDING",
    )

    # User says "I paid"
    res = await orch.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message="I paid")
    assert res.status == "FEASIBLE"
    assert res.is_pass_unlocked is True
    assert res.pass_status in ("PAID", "PAID_VERIFIED")
    assert "Trip Pass: Active ✅" in res.message_text or "Trip Pass: ACTIVE ✅" in res.message_text or "unlocked" in res.message_text.lower()

    # Verify pass updated in DB
    updated_pass = pass_repo.get_by_trip_id(trip.id)
    assert updated_pass.status in ("PAID", "PAID_VERIFIED")

    get_settings.cache_clear()


# =========================================================================
# Item 2 Tests: Railhead list gating & bus alongside train under ~350 km
# =========================================================================

def test_dynamic_transit_railhead_gating_direct_vs_nearest():
    """Item 2: Cities on explicit railhead list get distance-based train; non-railheads get nearest railhead + road."""
    gen = DynamicTransitGenerator()

    # 1. Direct railhead to direct railhead (Chennai to Bangalore)
    direct_opts = gen.generate_options("Chennai", "Bangalore", people=1)
    train_direct = [o for o in direct_opts if o.transit_type == "train"]
    assert len(train_direct) > 0
    for t in train_direct:
        assert t.name_or_operator == "Estimated train fare (distance-based)"

    # 2. Non-railhead gateway city (Chennai to Munnar - gateway Ernakulam/Aluva)
    munnar_opts = gen.generate_options("Chennai", "Munnar", people=1)
    train_munnar = [o for o in munnar_opts if o.transit_type == "train"]
    assert len(train_munnar) > 0
    for t in train_munnar:
        assert t.name_or_operator == "Estimated train fare (nearest railhead + road)"

    # 3. Non-railhead gateway city (Chennai to Ooty - gateway Mettupalayam/Coimbatore)
    ooty_opts = gen.generate_options("Chennai", "Ooty", people=1)
    train_ooty = [o for o in ooty_opts if o.transit_type == "train"]
    assert len(train_ooty) > 0
    for t in train_ooty:
        assert t.name_or_operator == "Estimated train fare (nearest railhead + road)"


def test_dynamic_transit_shows_bus_alongside_train_under_350km():
    """Item 2: Routes under ~350 km show bus option alongside train."""
    gen = DynamicTransitGenerator()

    # Chennai to Hosur (~280 km, under 350 km)
    hosur_opts = gen.generate_options("Chennai", "Hosur", people=2)
    assert len(hosur_opts) >= 2
    types_first_two = [opt.transit_type for opt in hosur_opts[:2]]
    assert "train" in types_first_two
    assert "bus" in types_first_two

    # Chennai to Bangalore (~310 km, under 350 km)
    blr_opts = gen.generate_options("Chennai", "Bangalore", people=2)
    assert len(blr_opts) >= 2
    types_first_two_blr = [opt.transit_type for opt in blr_opts[:2]]
    assert "train" in types_first_two_blr
    assert "bus" in types_first_two_blr


def test_stripe_precheck_bot_username_and_sk_live_guard(monkeypatch):
    """Pre-check: success_url reads bot username from config; sk_live_ rejected in demo mode."""
    import pytest
    from uuid import uuid4
    from budlance.config import get_settings
    from budlance.payment.service import PaymentService

    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "my_custom_budlance_bot")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("STRIPE_API_KEY", "sk_live_prohibited_key_12345")
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.telegram_bot_username == "my_custom_budlance_bot"
    # Guard: has_stripe_credentials must reject sk_live_ key in non-production mode
    assert settings.has_stripe_credentials is False

    svc = PaymentService()
    # Calling _create_stripe_checkout_session directly with sk_live_ must raise ValueError
    with pytest.raises(ValueError, match="strictly prohibited"):
        import asyncio
        asyncio.run(svc._create_stripe_checkout_session(
            trip_id=uuid4(),
            chat_id=12345,
            amount=Decimal("49.00"),
            currency="INR",
        ))

    get_settings.cache_clear()


# =========================================================================
# Flight Booking Handoff Tests (Items 2, 3, 4)
# =========================================================================

def test_flight_url_builder_fails_on_unfilled_placeholders():
    """Item 2: Fail if any unfilled template placeholder exists in generated flight URL."""
    import urllib.parse
    from budlance.normalization.flights import build_safe_flight_search_url

    # Check valid inputs
    url = build_safe_flight_search_url("Chennai", "Delhi", outbound_date="2026-11-08", return_date="2026-11-10")
    unquoted = urllib.parse.unquote_plus(url)

    # Must contain exact query form with IATA codes: q="Flights to DEL from MAA on 2026-11-08 through 2026-11-10"
    assert "Flights to DEL from MAA on 2026-11-08 through 2026-11-10" in unquoted

    # Disallowed unfilled placeholders
    forbidden_placeholders = [
        "{origin}", "{destination}", "{outbound_date}", "{return_date}",
        "{DATE}", "{X}", "{Y}", "<DATE>", "[DATE]", "None", "null", "undefined",
        "placeholder", "none",
    ]
    for ph in forbidden_placeholders:
        assert ph not in url, f"URL contains forbidden placeholder token: {ph!r} in {url!r}"

    # Edge cases: partial or invalid routes must safely return generic portal, never unfilled placeholders
    bad_urls = [
        build_safe_flight_search_url(None, "Delhi"),
        build_safe_flight_search_url("Chennai", None),
        build_safe_flight_search_url("", ""),
        build_safe_flight_search_url("From", "To"),
        build_safe_flight_search_url("Chennai", "From"),
    ]
    for b_url in bad_urls:
        assert b_url == "https://www.google.com/travel/flights"
        for ph in forbidden_placeholders:
            assert ph not in b_url


def test_flight_url_builder_uses_booking_token_when_present():
    """Item 2: If a booking_token is already present in cached result, use it."""
    from budlance.normalization.flights import build_safe_flight_search_url

    # Opaque token
    token_url = build_safe_flight_search_url(
        origin="Chennai",
        destination="Delhi",
        booking_token="tok_serpapi_cache_12345",
    )
    assert "https://www.google.com/travel/flights?booking_token=tok_serpapi_cache_12345" in token_url

    # Direct URL token
    direct_token_url = build_safe_flight_search_url(
        origin="Chennai",
        destination="Delhi",
        booking_token="https://airline.com/book?flight=123",
    )
    assert direct_token_url == "https://airline.com/book?flight=123"


@pytest.mark.asyncio
async def test_flight_travel_dates_passed_to_lookup_and_link(monkeypatch):
    """Item 3: State assumed date in reply and pass identical dates to lookup and booking link."""
    monkeypatch.setenv("SERPAPI_LIVE_ENABLED", "false")
    from budlance.orchestrator.orchestrator import BudlanceOrchestrator
    from budlance.ai.service import AIIntentService

    orch = BudlanceOrchestrator(ai_service=AIIntentService(use_mock=True))
    res = await orch.handle_user_message(
        telegram_user_id=88331,
        chat_id=88331,
        message="flight from Chennai to Delhi for 3 days, 2 people, budget 50000",
    )
    assert res.status == "FEASIBLE_TRANSPORT"
    text = res.message_text

    # Reply states the assumed travel dates
    assert "• Travel Dates:" in text
    assert "(assumed for planning)" in text
    assert "through" in text

    # Link must contain the exact same dates and IATA codes
    assert "https://www.google.com/travel/flights?q=" in text
    assert "Flights+to+DEL+from+MAA" in text


def test_selected_bookings_includes_real_links_only():
    """Item 4: Add booking links to Selected Bookings section only when built from real data."""
    from budlance.orchestrator.formatter import format_feasible_plan
    from budlance.schemas.travel import FlightOption, HotelOption, TransitOption
    from budlance.engine.models import BudgetBreakdown

    breakdown = BudgetBreakdown(
        total_budget=Decimal("50000.00"),
        bucket_a_fixed=Decimal("20000.00"),
        bucket_b_survival=Decimal("10000.00"),
        bucket_c_activities=Decimal("2500.00"),
        bucket_d_rescue=Decimal("5000.00"),
        transport_cost=Decimal("12000.00"),
        hotel_cost=Decimal("8000.00"),
        food_cost=Decimal("8000.00"),
        local_transit_cost=Decimal("2000.00"),
        attraction_cost=Decimal("0.00"),
        total_allocated=Decimal("37500.00"),
        remaining_surplus=Decimal("12500.00"),
        currency="INR",
    )

    # 1. Flight with real deep link
    flight = FlightOption(
        airline="Air India",
        flight_number="AI-505",
        departure_airport="MAA",
        arrival_airport="DEL",
        price=Decimal("12000.00"),
        deep_link="https://www.google.com/travel/flights?q=Flights+to+DEL+from+MAA",
    )
    # 2. Hotel with real booking link
    hotel_with_link = HotelOption(
        name="The Imperial Delhi",
        total_price=Decimal("8000.00"),
        deep_link="https://www.booking.com/hotel/the-imperial",
    )

    msg = format_feasible_plan(
        destination="Delhi",
        days=3,
        people=2,
        breakdown=breakdown,
        transport=flight,
        hotel=hotel_with_link,
        itinerary=None,
        ledger=None,
        is_pass_unlocked=True,
    )

    assert "🧳 *Selected Bookings:*" in msg
    assert "• Transport: Air India — INR 12,000.00" in msg
    assert "🔗 Booking: https://www.google.com/travel/flights?q=Flights+to+DEL+from+MAA" in msg
    assert "• Accommodation: The Imperial Delhi — INR 8,000.00" in msg
    assert "🔗 Booking: https://www.booking.com/hotel/the-imperial" in msg

    # 3. Hotel WITHOUT link — must NOT fabricate link
    hotel_no_link = HotelOption(
        name="Budget Residency",
        total_price=Decimal("4000.00"),
        deep_link=None,
    )
    msg2 = format_feasible_plan(
        destination="Delhi",
        days=3,
        people=2,
        breakdown=breakdown,
        transport=None,
        hotel=hotel_no_link,
        itinerary=None,
        ledger=None,
        is_pass_unlocked=True,
    )
    assert "• Accommodation: Budget Residency — INR 4,000.00" in msg2
    assert "🔗 Booking:" not in msg2


# =========================================================================
# Item 3 Tests: Fare dispute distance parsing & removal of silent 10 km default
# =========================================================================

def _setup_active_rescue_trip(chat_id: int):
    from budlance.ai.service import AIIntentService
    from budlance.rescue.service import RescueService
    from budlance.db.repositories.trip_repo import TripRepository
    from budlance.db.repositories.itinerary_repo import ItineraryRepository
    from budlance.db.repositories.ledger_repo import LedgerRepository
    from budlance.db.repositories.rescue_repo import RescueRepository
    from budlance.db.repositories.user_repo import UserRepository
    from budlance.db.models import BudgetAllocation, LedgerEntry, utc_now
    from uuid import uuid4

    user_repo = UserRepository()
    trip_repo = TripRepository()
    itin_repo = ItineraryRepository()
    ledger_repo = LedgerRepository()
    rescue_repo = RescueRepository()

    user = user_repo.get_or_create_user(telegram_user_id=chat_id)
    trip = trip_repo.create_trip(
        user_id=user.id,
        telegram_chat_id=chat_id,
        budget_total=Decimal("20000.00"),
        destination="Chennai",
        origin="Bangalore",
        status="ACTIVE",
        duration_days=3,
        people_count=1,
    )

    alloc = BudgetAllocation(
        id=uuid4(),
        trip_id=trip.id,
        transport_allocated=Decimal("6000.00"),
        stay_allocated=Decimal("8000.00"),
        food_allocated=Decimal("3000.00"),
        activities_discretionary=Decimal("1000.00"),
        rescue_fund_allocated=Decimal("2000.00"),
        total_budget=Decimal("20000.00"),
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    ledger_repo.save_budget_allocation(alloc)

    ledger_repo.add_ledger_entry(
        LedgerEntry(
            id=uuid4(),
            trip_id=trip.id,
            category="daily_survival",
            description="Local Transit Allowance",
            allocated_amount=Decimal("1000.00"),
            planned_amount=Decimal("1000.00"),
            spent_amount=Decimal("0.00"),
            remaining_amount=Decimal("1000.00"),
            source="estimated",
            created_at=utc_now(),
        )
    )

    rescue_svc = RescueService(
        trip_repo=trip_repo,
        itinerary_repo=itin_repo,
        ledger_repo=ledger_repo,
        rescue_repo=rescue_repo,
        ai_service=AIIntentService(use_mock=True),
    )
    return rescue_svc


@pytest.mark.asyncio
async def test_fare_dispute_parses_distance_from_message(monkeypatch):
    """Item 3: Parse distance from the user's message and compute fare accordingly."""
    monkeypatch.setenv("SERPAPI_LIVE_ENABLED", "false")
    from budlance.orchestrator.formatter import format_rescue_result

    rescue_svc = _setup_active_rescue_trip(771122)

    # 1. Distance specified: "for 12 km"
    res = await rescue_svc.execute_rescue(
        chat_id=771122,
        user_message="The auto driver is asking ₹500 for 12 km, is it fair?",
    )

    assert res.rescue_type == "price_dispute"
    assert res.success is True
    fg = res.fare_guidance
    assert fg is not None
    assert fg.reported_price == Decimal("500")
    assert fg.distance_km == 12.0
    # Auto standard rate is ₹15/km -> 12 * 15 = 180.00
    assert fg.estimated_fare == Decimal("180.00")
    assert fg.status == "significantly_high"
    assert "for 12 km" in fg.advisory_notes

    formatted = format_rescue_result(res)
    assert "for 12 km" in formatted
    assert "Distance Missing" not in formatted


@pytest.mark.asyncio
async def test_fare_dispute_missing_distance_removes_silent_10km_and_prompts_user(monkeypatch):
    """Item 3: If distance is absent, state estimate is approximate, prompt for distance, and remove silent 10 km default."""
    monkeypatch.setenv("SERPAPI_LIVE_ENABLED", "false")
    from budlance.orchestrator.formatter import format_rescue_result

    rescue_svc = _setup_active_rescue_trip(771123)

    # 2. Distance absent: "The auto driver is asking ₹500"
    res = await rescue_svc.execute_rescue(
        chat_id=771123,
        user_message="The auto driver is asking ₹500",
    )

    assert res.rescue_type == "price_dispute"
    assert res.success is True
    fg = res.fare_guidance
    assert fg is not None
    assert fg.reported_price == Decimal("500")

    # Silent 10 km default MUST be removed: distance_km is None
    assert fg.distance_km is None

    # Advisory notes must say estimate is approximate and prompt user for distance
    assert "approximate" in fg.advisory_notes.lower()
    assert "distance" in fg.advisory_notes.lower()

    # Formatted output must clearly present approximate estimate and ask for distance
    formatted = format_rescue_result(res)
    assert "approximate estimate" in formatted.lower()
    assert "Distance Missing" in formatted or "distance" in formatted.lower()
    assert "reply with your ride distance" in formatted.lower()

    # Ensure no silent assumption of 10.0 km
    assert "for ~10.0 km" not in formatted
    assert "for 10.0 km" not in formatted
    assert "for ~10 km" not in formatted


@pytest.mark.asyncio
async def test_fare_dispute_parses_various_distance_formats_and_modes(monkeypatch):
    """Item 3: Parse various distance units (kms, kilometers) and transportation modes (cab, auto)."""
    monkeypatch.setenv("SERPAPI_LIVE_ENABLED", "false")

    rescue_svc = _setup_active_rescue_trip(771124)

    # Decimal distance with 'kms' and cab mode
    res_cab = await rescue_svc.execute_rescue(
        chat_id=771124,
        user_message="Cab driver is asking 450 for 5.5 kms",
    )
    assert res_cab.fare_guidance.mode == "cab"
    assert res_cab.fare_guidance.distance_km == 5.5
    # Cab standard rate ₹22/km -> 5.5 * 22 = 121.00
    assert res_cab.fare_guidance.estimated_fare == Decimal("121.00")

    # 'kilometers' with auto mode
    res_auto = await rescue_svc.execute_rescue(
        chat_id=771124,
        user_message="Auto charging 300 for 8 kilometers",
    )
    assert res_auto.fare_guidance.mode == "auto"
    assert res_auto.fare_guidance.distance_km == 8.0
    # Auto standard rate ₹15/km -> 8 * 15 = 120.00
    assert res_auto.fare_guidance.estimated_fare == Decimal("120.00")


# =========================================================================
# Item 4 Tests: Interest-mismatch note origin exclusion & keying to interests
# =========================================================================

def test_interest_mismatch_robust_origin_exclusion_and_keying():
    """Item 4: Exclude origin city (including city, state strings) and key suggestions to unmatched interests."""
    from budlance.orchestrator.formatter import resolve_interest_mismatch_note

    # Case 1: Origin with state string "Chennai, Tamil Nadu" -> Chennai must be excluded
    note1 = resolve_interest_mismatch_note(
        destination="Hosur",
        requested_interests=["beach", "theme park"],
        curated_attractions=[],
        places=[],
        origin="Chennai, Tamil Nadu",
    )
    assert note1 is not None
    assert "\n" not in note1
    assert "Chennai" not in note1
    assert "Bangalore (Wonderla)" in note1
    assert "for beach" in note1
    assert "for theme park" in note1

    # Case 2: Origin with state string "Bangalore, Karnataka" -> Bangalore must be excluded
    note2 = resolve_interest_mismatch_note(
        destination="Hosur",
        requested_interests=["theme park"],
        curated_attractions=[],
        places=[],
        origin="Bangalore, Karnataka",
    )
    assert note2 is not None
    assert "Bangalore" not in note2
    assert "Chennai (VGP/MGM)" in note2
    assert "for theme park" in note2

    # Case 3: Multiple distinct interests (e.g. beach + wildlife) keyed separately
    note3 = resolve_interest_mismatch_note(
        destination="Hosur",
        requested_interests=["beach", "wildlife"],
        curated_attractions=[],
        places=[],
        origin="Chennai",
    )
    assert note3 is not None
    assert "for beach" in note3
    assert "for wildlife" in note3
    assert any(c in note3 for c in ["Pondicherry", "Goa", "Mahabalipuram"])
    assert any(w in note3 for w in ["Kabini", "Bandipur", "Thekkady"])


@pytest.mark.asyncio
async def test_interest_mismatch_rendered_in_orchestrator_output(monkeypatch):
    """Item 4: Full orchestrator offline flow includes interest-mismatch note excluding origin."""
    monkeypatch.setenv("SERPAPI_LIVE_ENABLED", "false")
    from budlance.orchestrator.orchestrator import BudlanceOrchestrator
    from budlance.ai.service import AIIntentService

    orch = BudlanceOrchestrator(ai_service=AIIntentService(use_mock=True))

    res = await orch.handle_user_message(
        telegram_user_id=882201,
        chat_id=882201,
        message="I would like to explore beach and theme park, budget 10000, 2 people couple, 2 days from Chennai to Hosur",
    )

    assert res.status == "FEASIBLE"
    text = res.message_text

    # Note must be present in the output
    assert "Note: Hosur has no matching beach and theme park attractions" in text
    # Origin (Chennai) must NOT be suggested for theme park
    assert "Bangalore (Wonderla)" in text
    assert "for theme park" in text
    assert "for beach" in text
    assert "Chennai (VGP/MGM)" not in text



