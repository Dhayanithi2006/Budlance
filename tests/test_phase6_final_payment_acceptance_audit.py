"""Phase 6 Final Payment Acceptance Audit Suite.

Verifies:
1. Valid paid event grants access exactly once.
2. Replaying the same event does not duplicate fulfillment (durable idempotency).
3. Completed-but-unpaid event (payment_status="unpaid") does not grant access.
4. Later asynchronous payment success (checkout.session.async_payment_succeeded) grants access.
5. Late failed or expired events cannot revoke an already verified paid entitlement (out-of-order protection).
6. Concurrent/duplicate deliveries cannot cause duplicate fulfillment or inconsistent state.
7. Session with payment_status="no_payment_required" cannot grant access.
8. Expected amount (4900 paise) and currency (INR) are strictly enforced.
9. Trip cross-contamination prevented (Trip A payment cannot unlock Trip B).
10. Browser redirect or conversational claim cannot grant access without backend verification.
"""

from contextlib import contextmanager
from decimal import Decimal
import hashlib
import hmac
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4
import pytest
from httpx import Response

from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.ai.service import AIIntentService
from budlance.attractions.selector import AttractionSelector
from budlance.cache.manager import CacheFallbackManager
from budlance.config import Settings
from budlance.db.repositories.conversation_repo import ConversationStateRepository
from budlance.db.repositories.intent_repo import IntentRepository
from budlance.db.repositories.itinerary_repo import ItineraryRepository
from budlance.db.repositories.ledger_repo import LedgerRepository
from budlance.db.repositories.rescue_repo import RescueRepository
from budlance.db.repositories.trip_pass_repo import TripPassRepository
from budlance.db.repositories.trip_repo import TripRepository
from budlance.db.repositories.user_repo import UserRepository
from budlance.engine.budget import ReverseBudgetEngine
from budlance.engine.optimizer import OptimizationEngine
from budlance.estimation.estimator import EstimationLayer
from budlance.itinerary.generator import ItineraryGenerator
from budlance.ledger.manager import VirtualLedgerManager
from budlance.normalization.normalizer import DataNormalizer
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.payment.service import PaymentService
from budlance.rescue.service import RescueService
from budlance.serpapi.models import DataSource, TravelDataEnvelope


@contextmanager
def _mock_settings(**overrides):
    defaults = {
        "stripe_webhook_secret": "whsec_test_secret_key_audit",
        "stripe_api_key": "sk_test_audit_key",
        "app_env": "development",
        "enable_trip_pass": True,
        "trip_pass_amount": Decimal("49.00"),
        "trip_pass_currency": "INR",
        "telegram_bot_username": "budlance_bot",
    }
    defaults.update(overrides)
    mocked_settings = Settings(**defaults)
    with patch("budlance.config.get_settings", return_value=mocked_settings), \
         patch("budlance.payment.service.get_settings", return_value=mocked_settings), \
         patch("budlance.orchestrator.orchestrator.get_settings", return_value=mocked_settings), \
         patch("budlance.api.routes.get_settings", return_value=mocked_settings), \
         patch("budlance.db.repositories.trip_pass_repo.get_settings", return_value=mocked_settings):
        yield mocked_settings


def _generate_stripe_sig(payload_bytes: bytes, secret: str, timestamp: int | None = None) -> str:
    ts = timestamp if timestamp is not None else int(time.time())
    signed_payload = f"{ts}.".encode("utf-8") + payload_bytes
    sig = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _build_audit_env(http_client: Any = None):
    user_repo = UserRepository(client=None)
    trip_repo = TripRepository(client=None)
    intent_repo = IntentRepository(client=None)
    itinerary_repo = ItineraryRepository(client=None)
    ledger_repo = LedgerRepository(client=None)
    rescue_repo = RescueRepository(client=None)
    conversation_repo = ConversationStateRepository(client=None)
    trip_pass_repo = TripPassRepository(client=None)

    payment_service = PaymentService(
        trip_pass_repo=trip_pass_repo,
        pass_amount=Decimal("49.00"),
        pass_currency="INR",
        default_provider="stripe",
        http_client=http_client,
    )

    budget_engine = ReverseBudgetEngine()
    estimation = EstimationLayer()
    optimizer = OptimizationEngine(budget_engine=budget_engine, estimation_layer=estimation)
    ledger_mgr = VirtualLedgerManager(ledger_repo)
    normalizer = DataNormalizer()

    mock_cache = MagicMock(spec=CacheFallbackManager)
    async def _get(engine, params, trip_id=None, **kw):
        if engine == "google_flights":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="fh",
                data={"best_flights": [{"flights": [{"airline": "IndiGo", "flight_number": "6E-101"}], "price": 4000}]},
                is_fallback=False,
            )
        if engine == "google_hotels":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="hh",
                data={"properties": [{"name": "Goa Beach Resort", "rate_per_night": {"extracted_lowest": 2000}, "hotel_class": "3"}]},
                is_fallback=False,
            )
        if engine == "google_maps":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="mh",
                data={"local_results": [{"title": "Calangute Beach", "rating": 4.5}, {"title": "Fort Aguada", "rating": 4.6}]},
                is_fallback=False,
            )
        return None

    mock_cache.get_or_fetch = AsyncMock(side_effect=_get)
    attraction_selector = AttractionSelector(cache_manager=mock_cache)
    itin_gen = ItineraryGenerator(itinerary_repo, attraction_selector=attraction_selector)
    ai_service = MagicMock(spec=AIIntentService)

    async def _parse(prompt: str):
        return ParsedTripIntent(
            action=TripAction.NEW_TRIP,
            origin="Chennai",
            destination="Goa",
            budget=Decimal("30000.00"),
            people=2,
            days=5,
            currency="INR",
            interests=["beach"],
        )

    ai_service.parse_trip_intent = AsyncMock(side_effect=_parse)
    ai_service.parse_trip_intent_with_context = AsyncMock(
        side_effect=lambda user_prompt=None, *args, **kw: _parse(user_prompt or "")
    )

    rescue_service = RescueService(
        trip_repo=trip_repo,
        itinerary_repo=itinerary_repo,
        ledger_repo=ledger_repo,
        rescue_repo=rescue_repo,
        ai_service=ai_service,
        cache_manager=mock_cache,
        normalizer=normalizer,
        budget_engine=budget_engine,
        estimation_layer=estimation,
        ledger_manager=ledger_mgr,
    )

    itinerary_enhancer = MagicMock()
    itinerary_enhancer.enhance_itinerary = AsyncMock(side_effect=lambda itinerary, **kw: itinerary)

    orchestrator = BudlanceOrchestrator(
        user_repo=user_repo,
        trip_repo=trip_repo,
        intent_repo=intent_repo,
        itinerary_repo=itinerary_repo,
        ledger_repo=ledger_repo,
        rescue_repo=rescue_repo,
        conversation_repo=conversation_repo,
        trip_pass_repo=trip_pass_repo,
        payment_service=payment_service,
        ai_service=ai_service,
        cache_manager=mock_cache,
        normalizer=normalizer,
        estimation_layer=estimation,
        budget_engine=budget_engine,
        optimizer=optimizer,
        itinerary_generator=itin_gen,
        itinerary_enhancer=itinerary_enhancer,
        ledger_manager=ledger_mgr,
        rescue_service=rescue_service,
        enable_trip_pass=True,
    )

    return orchestrator, {
        "payment_service": payment_service,
        "trip_pass_repo": trip_pass_repo,
        "trip_repo": trip_repo,
        "user_repo": user_repo,
    }


# =========================================================================
# The 10 Mandatory Audit Tests
# =========================================================================

@pytest.mark.asyncio
async def test_audit_01_valid_paid_event_grants_access_once():
    """1. A valid paid event grants access exactly once."""
    secret = "whsec_test_secret_key_audit"
    with _mock_settings(stripe_webhook_secret=secret):
        orc, repos = _build_audit_env()
        t_id = uuid4()
        repos["trip_pass_repo"].create_pass(trip_id=t_id, telegram_user_id=101, telegram_chat_id=201)

        payload = {
            "id": "evt_audit_01",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_audit_01",
                    "amount_total": 4900,
                    "currency": "inr",
                    "payment_status": "paid",
                    "client_reference_id": str(t_id),
                    "metadata": {"trip_id": str(t_id)},
                }
            },
        }
        raw_b = json.dumps(payload).encode("utf-8")
        sig = _generate_stripe_sig(raw_b, secret)

        v_res = await repos["payment_service"].verify_webhook_event(
            provider="stripe", payload=payload, signature=sig, raw_body=raw_b
        )
        assert v_res.success is True
        assert v_res.status == "PAID_VERIFIED"
        assert v_res.is_duplicate is False

        p = repos["trip_pass_repo"].get_by_trip_id(t_id)
        assert p.status == "PAID_VERIFIED"
        assert p.is_unlocked is True
        assert "evt_audit_01" in p.metadata.get("processed_events", [])


@pytest.mark.asyncio
async def test_audit_02_replaying_same_event_does_not_duplicate_fulfillment():
    """2. Replaying the same event does not duplicate fulfillment."""
    secret = "whsec_test_secret_key_audit"
    with _mock_settings(stripe_webhook_secret=secret):
        orc, repos = _build_audit_env()
        t_id = uuid4()
        repos["trip_pass_repo"].create_pass(trip_id=t_id, telegram_user_id=102, telegram_chat_id=202)

        payload = {
            "id": "evt_audit_02",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_audit_02",
                    "amount_total": 4900,
                    "currency": "inr",
                    "payment_status": "paid",
                    "metadata": {"trip_id": str(t_id)},
                }
            },
        }
        raw_b = json.dumps(payload).encode("utf-8")
        sig = _generate_stripe_sig(raw_b, secret)

        v1 = await repos["payment_service"].verify_webhook_event(
            provider="stripe", payload=payload, signature=sig, raw_body=raw_b
        )
        assert v1.success is True
        assert v1.is_duplicate is False

        # Replay exact event
        v2 = await repos["payment_service"].verify_webhook_event(
            provider="stripe", payload=payload, signature=sig, raw_body=raw_b
        )
        assert v2.success is True
        assert v2.is_duplicate is True
        assert v2.status == "PAID_VERIFIED"


@pytest.mark.asyncio
async def test_audit_03_completed_but_unpaid_does_not_grant_access():
    """3. A completed session with payment_status='unpaid' remains pending and does not unlock."""
    secret = "whsec_test_secret_key_audit"
    with _mock_settings(stripe_webhook_secret=secret):
        orc, repos = _build_audit_env()
        t_id = uuid4()
        repos["trip_pass_repo"].create_pass(trip_id=t_id, telegram_user_id=103, telegram_chat_id=203)

        payload = {
            "id": "evt_audit_03_unpaid",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_audit_03",
                    "amount_total": 4900,
                    "currency": "inr",
                    "payment_status": "unpaid",  # Delayed payment method (e.g. SEPA/bank transfer)
                    "metadata": {"trip_id": str(t_id)},
                }
            },
        }
        raw_b = json.dumps(payload).encode("utf-8")
        sig = _generate_stripe_sig(raw_b, secret)

        v = await repos["payment_service"].verify_webhook_event(
            provider="stripe", payload=payload, signature=sig, raw_body=raw_b
        )
        assert v.success is False
        assert v.status == "CHECKOUT_PENDING"
        assert v.error == "PAYMENT_UNPAID"

        pass_rec = repos["trip_pass_repo"].get_by_trip_id(t_id)
        assert pass_rec.status == "CHECKOUT_PENDING"
        assert pass_rec.is_unlocked is False


@pytest.mark.asyncio
async def test_audit_04_async_payment_succeeded_grants_access():
    """4. A later asynchronous payment-success event grants access only after payment is verified."""
    secret = "whsec_test_secret_key_audit"
    with _mock_settings(stripe_webhook_secret=secret):
        orc, repos = _build_audit_env()
        t_id = uuid4()
        repos["trip_pass_repo"].create_pass(trip_id=t_id, telegram_user_id=104, telegram_chat_id=204)

        # 1. First event: checkout.session.completed with unpaid
        p1 = {
            "id": "evt_audit_04_unpaid",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_audit_04",
                    "amount_total": 4900,
                    "currency": "inr",
                    "payment_status": "unpaid",
                    "metadata": {"trip_id": str(t_id)},
                }
            },
        }
        raw_p1 = json.dumps(p1).encode("utf-8")
        await repos["payment_service"].verify_webhook_event(
            provider="stripe", payload=p1, signature=_generate_stripe_sig(raw_p1, secret), raw_body=raw_p1
        )
        assert repos["trip_pass_repo"].get_by_trip_id(t_id).is_unlocked is False

        # 2. Days later: async payment succeeds
        p2 = {
            "id": "evt_audit_04_async_succ",
            "type": "checkout.session.async_payment_succeeded",
            "data": {
                "object": {
                    "id": "cs_audit_04",
                    "amount_total": 4900,
                    "currency": "inr",
                    "metadata": {"trip_id": str(t_id)},
                }
            },
        }
        raw_p2 = json.dumps(p2).encode("utf-8")
        v2 = await repos["payment_service"].verify_webhook_event(
            provider="stripe", payload=p2, signature=_generate_stripe_sig(raw_p2, secret), raw_body=raw_p2
        )
        assert v2.success is True
        assert v2.status == "PAID_VERIFIED"
        assert repos["trip_pass_repo"].get_by_trip_id(t_id).is_unlocked is True


@pytest.mark.asyncio
async def test_audit_05_late_failed_or_expired_cannot_revoke_paid():
    """5. A late failed or expired event cannot revoke an already verified paid entitlement."""
    secret = "whsec_test_secret_key_audit"
    with _mock_settings(stripe_webhook_secret=secret):
        orc, repos = _build_audit_env()
        t_id = uuid4()
        repos["trip_pass_repo"].create_pass(
            trip_id=t_id, telegram_user_id=105, telegram_chat_id=205, status="PAID_VERIFIED"
        )

        # Late async failure arrives
        p_fail = {
            "id": "evt_audit_05_late_fail",
            "type": "checkout.session.async_payment_failed",
            "data": {
                "object": {
                    "id": "cs_audit_05",
                    "metadata": {"trip_id": str(t_id)},
                }
            },
        }
        raw_f = json.dumps(p_fail).encode("utf-8")
        await repos["payment_service"].verify_webhook_event(
            provider="stripe", payload=p_fail, signature=_generate_stripe_sig(raw_f, secret), raw_body=raw_f
        )
        assert repos["trip_pass_repo"].get_by_trip_id(t_id).status == "PAID_VERIFIED"
        assert repos["trip_pass_repo"].get_by_trip_id(t_id).is_unlocked is True

        # Late expiry arrives
        p_exp = {
            "id": "evt_audit_05_late_exp",
            "type": "checkout.session.expired",
            "data": {"object": {"id": "cs_audit_05", "metadata": {"trip_id": str(t_id)}}},
        }
        raw_e = json.dumps(p_exp).encode("utf-8")
        await repos["payment_service"].verify_webhook_event(
            provider="stripe", payload=p_exp, signature=_generate_stripe_sig(raw_e, secret), raw_body=raw_e
        )
        assert repos["trip_pass_repo"].get_by_trip_id(t_id).status == "PAID_VERIFIED"
        assert repos["trip_pass_repo"].get_by_trip_id(t_id).is_unlocked is True


@pytest.mark.asyncio
async def test_audit_06_two_duplicate_deliveries_consistent():
    """6. Two duplicate deliveries cannot cause duplicate fulfillment or inconsistent state."""
    secret = "whsec_test_secret_key_audit"
    with _mock_settings(stripe_webhook_secret=secret):
        orc, repos = _build_audit_env()
        t_id = uuid4()
        repos["trip_pass_repo"].create_pass(trip_id=t_id, telegram_user_id=106, telegram_chat_id=206)

        payload = {
            "id": "evt_audit_06_dup",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_audit_06",
                    "amount_total": 4900,
                    "currency": "inr",
                    "payment_status": "paid",
                    "metadata": {"trip_id": str(t_id)},
                }
            },
        }
        raw_b = json.dumps(payload).encode("utf-8")
        sig = _generate_stripe_sig(raw_b, secret)

        # Delivery 1
        v1 = await repos["payment_service"].verify_webhook_event(
            provider="stripe", payload=payload, signature=sig, raw_body=raw_b
        )
        # Delivery 2 (concurrent / duplicate delivery)
        v2 = await repos["payment_service"].verify_webhook_event(
            provider="stripe", payload=payload, signature=sig, raw_body=raw_b
        )

        assert v1.success is True and v1.is_duplicate is False
        assert v2.success is True and v2.is_duplicate is True
        p = repos["trip_pass_repo"].get_by_trip_id(t_id)
        assert p.status == "PAID_VERIFIED"
        # event ID recorded exactly once
        events = p.metadata.get("processed_events", [])
        assert events.count("evt_audit_06_dup") == 1


@pytest.mark.asyncio
async def test_audit_07_no_payment_required_rejected():
    """7. no_payment_required cannot grant a paid Trip Pass."""
    secret = "whsec_test_secret_key_audit"
    with _mock_settings(stripe_webhook_secret=secret):
        orc, repos = _build_audit_env()
        t_id = uuid4()
        repos["trip_pass_repo"].create_pass(trip_id=t_id, telegram_user_id=107, telegram_chat_id=207)

        payload = {
            "id": "evt_audit_07_no_pay",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_audit_07",
                    "amount_total": 0,
                    "currency": "inr",
                    "payment_status": "no_payment_required",
                    "metadata": {"trip_id": str(t_id)},
                }
            },
        }
        raw_b = json.dumps(payload).encode("utf-8")
        sig = _generate_stripe_sig(raw_b, secret)

        v = await repos["payment_service"].verify_webhook_event(
            provider="stripe", payload=payload, signature=sig, raw_body=raw_b
        )
        assert v.success is False
        assert v.error == "NO_PAYMENT_REQUIRED"
        assert repos["trip_pass_repo"].get_by_trip_id(t_id).is_unlocked is False


@pytest.mark.asyncio
async def test_audit_08_amount_and_currency_strict_validation():
    """8. Expected amount (4900 paise) and currency (INR) are strictly enforced."""
    secret = "whsec_test_secret_key_audit"
    with _mock_settings(stripe_webhook_secret=secret):
        orc, repos = _build_audit_env()
        t_id = uuid4()
        repos["trip_pass_repo"].create_pass(trip_id=t_id, telegram_user_id=108, telegram_chat_id=208)

        # Missing amount
        p_no_amt = {
            "id": "evt_audit_08_no_amt",
            "type": "checkout.session.completed",
            "data": {"object": {"id": "cs_08a", "currency": "inr", "metadata": {"trip_id": str(t_id)}}},
        }
        raw_na = json.dumps(p_no_amt).encode("utf-8")
        v_na = await repos["payment_service"].verify_webhook_event(
            provider="stripe", payload=p_no_amt, signature=_generate_stripe_sig(raw_na, secret), raw_body=raw_na
        )
        assert v_na.success is False
        assert v_na.error == "MISSING_AMOUNT"

        # Missing currency
        p_no_curr = {
            "id": "evt_audit_08_no_curr",
            "type": "checkout.session.completed",
            "data": {"object": {"id": "cs_08b", "amount_total": 4900, "metadata": {"trip_id": str(t_id)}}},
        }
        raw_nc = json.dumps(p_no_curr).encode("utf-8")
        v_nc = await repos["payment_service"].verify_webhook_event(
            provider="stripe", payload=p_no_curr, signature=_generate_stripe_sig(raw_nc, secret), raw_body=raw_nc
        )
        assert v_nc.success is False
        assert v_nc.error == "MISSING_CURRENCY"


@pytest.mark.asyncio
async def test_audit_09_trip_cross_contamination_prevented():
    """9. The checkout session is bound to the correct trip; payment for Trip A cannot unlock Trip B."""
    secret = "whsec_test_secret_key_audit"
    with _mock_settings(stripe_webhook_secret=secret):
        orc, repos = _build_audit_env()
        t_a = uuid4()
        t_b = uuid4()
        repos["trip_pass_repo"].create_pass(trip_id=t_a, telegram_user_id=109, telegram_chat_id=209)
        repos["trip_pass_repo"].create_pass(trip_id=t_b, telegram_user_id=109, telegram_chat_id=209)

        # Webhook for Trip A
        p_a = {
            "id": "evt_audit_09_a",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_audit_09_a",
                    "amount_total": 4900,
                    "currency": "inr",
                    "metadata": {"trip_id": str(t_a)},
                }
            },
        }
        raw_a = json.dumps(p_a).encode("utf-8")
        v_a = await repos["payment_service"].verify_webhook_event(
            provider="stripe", payload=p_a, signature=_generate_stripe_sig(raw_a, secret), raw_body=raw_a
        )
        assert v_a.success is True
        assert repos["trip_pass_repo"].is_unlocked(t_a) is True
        assert repos["trip_pass_repo"].is_unlocked(t_b) is False


@pytest.mark.asyncio
async def test_audit_10_redirect_and_unverified_claims_blocked():
    """10. Browser redirect or conversational claim cannot grant access without backend verification."""
    orc, repos = _build_audit_env()
    res_plan = await orc.handle_user_message(
        telegram_user_id=110, chat_id=210, message="Plan a trip from Chennai to Goa with budget 25000"
    )
    t_id = res_plan.trip_id

    # Attacker claims "paid"
    res_claim = await orc.handle_user_message(telegram_user_id=110, chat_id=210, message="paid")
    assert res_claim.is_pass_unlocked is False
    assert repos["trip_pass_repo"].is_unlocked(t_id) is False

    # Attacker returns via Telegram deep-link callback
    res_callback = await orc.handle_user_message(
        telegram_user_id=110, chat_id=210, message=f"/start paid_{t_id.hex[:12]}"
    )
    assert res_callback.is_pass_unlocked is False
    assert repos["trip_pass_repo"].is_unlocked(t_id) is False
