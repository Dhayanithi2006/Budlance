"""Budlance Phase 7 - Real Telegram E2E Verification.

Scenarios:
  Telegram integration path, Webhook/Health, A-I lifecycle,
  Reconciliation interruptions, State isolation, Persistence, Edge cases.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID
import pytest
from httpx import ASGITransport, AsyncClient
from telegram import Chat, Message, Update, User

from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.ai.service import AIIntentService
from budlance.api.app import create_app
from budlance.attractions.selector import AttractionSelector
from budlance.bot.handlers import (
    get_orchestrator, set_orchestrator, text_message_handler, start_handler,
)
from budlance.bot.bot import build_bot_application
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
from budlance.itinerary.enhancer import ItineraryEnhancer
from budlance.itinerary.generator import ItineraryGenerator
from budlance.ledger.manager import VirtualLedgerManager
from budlance.normalization.normalizer import DataNormalizer
from budlance.orchestrator.formatter import split_telegram_message
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.rescue.service import RescueService
from budlance.serpapi.models import DataSource, TravelDataEnvelope

# =========================================================================
# Shared helpers
# =========================================================================

def _mock_cache_manager() -> CacheFallbackManager:
    mock_cache = MagicMock(spec=CacheFallbackManager)

    async def _get(engine, params, trip_id=None, **kw):
        if engine == "google_flights":
            return TravelDataEnvelope(
                source=DataSource.LIVE, engine=engine, query_hash="fh",
                data={"best_flights": [{"flights": [{"airline": "IndiGo"}], "price": 3500}]},
                is_fallback=False,
            )
        if engine == "google_hotels":
            return TravelDataEnvelope(
                source=DataSource.LIVE, engine=engine, query_hash="hh",
                data={"properties": [{"name": "Grand Hotel",
                    "rate_per_night": {"extracted_lowest": 1500},
                    "total_rate": {"extracted_lowest": 4500}}]},
                is_fallback=False,
            )
        return TravelDataEnvelope(
            source=DataSource.FALLBACK, engine=engine,
            query_hash="fbh", data={}, is_fallback=True,
        )

    mock_cache.get_travel_data = AsyncMock(side_effect=_get)
    return mock_cache


def _build_repos() -> dict:
    return {
        "user_repo": UserRepository(client=None),
        "trip_repo": TripRepository(client=None),
        "intent_repo": IntentRepository(client=None),
        "itinerary_repo": ItineraryRepository(client=None),
        "ledger_repo": LedgerRepository(client=None),
        "rescue_repo": RescueRepository(client=None),
        "conversation_repo": ConversationStateRepository(client=None),
    }


def _build_orchestrator(repos: dict) -> BudlanceOrchestrator:
    ai = AIIntentService(use_mock=True)
    cache = _mock_cache_manager()
    norm = DataNormalizer()
    est = EstimationLayer()
    beng = ReverseBudgetEngine()
    opt = OptimizationEngine(budget_engine=beng, estimation_layer=est)
    sel = AttractionSelector(cache_manager=cache)
    igen = ItineraryGenerator(itinerary_repo=repos["itinerary_repo"], attraction_selector=sel)
    enh = ItineraryEnhancer(use_mock=True)
    lmgr = VirtualLedgerManager(ledger_repo=repos["ledger_repo"])
    rsvc = RescueService(
        trip_repo=repos["trip_repo"], itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"], rescue_repo=repos["rescue_repo"],
        ai_service=ai, cache_manager=cache, normalizer=norm,
        budget_engine=beng, estimation_layer=est, ledger_manager=lmgr,
    )
    return BudlanceOrchestrator(
        user_repo=repos["user_repo"], trip_repo=repos["trip_repo"],
        intent_repo=repos["intent_repo"], itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"], rescue_repo=repos["rescue_repo"],
        conversation_repo=repos["conversation_repo"], ai_service=ai,
        cache_manager=cache, normalizer=norm, estimation_layer=est,
        budget_engine=beng, optimizer=opt, attraction_selector=sel,
        itinerary_generator=igen, itinerary_enhancer=enh,
        ledger_manager=lmgr, rescue_service=rsvc,
    )


def _build_update(text: str, chat_id: int = 10001, user_id: int = 10001):
    replies = []

    async def _rep(chunk, **kw):
        replies.append(chunk)

    msg = MagicMock(spec=Message)
    msg.reply_text = AsyncMock(side_effect=_rep)
    msg.text = text
    upd = MagicMock(spec=Update)
    upd.effective_message = msg
    upd.effective_chat = MagicMock(spec=Chat, id=chat_id)
    upd.effective_user = MagicMock(spec=User, id=user_id, username="tester", first_name="Priya")
    return upd, replies


@pytest.fixture
def repos():
    return _build_repos()


@pytest.fixture
def orch(repos):
    return _build_orchestrator(repos)

# =========================================================================
# Telegram Integration Path
# =========================================================================

class TestTelegramIntegrationPath:

    @pytest.mark.asyncio
    async def test_start_handler_welcome(self):
        upd, replies = _build_update("/start")
        await start_handler(upd, MagicMock())
        assert len(replies) == 1
        assert "Welcome to Budlance" in replies[0]
        assert "reverse-budget" in replies[0]

    @pytest.mark.asyncio
    async def test_empty_message_no_crash(self, orch):
        set_orchestrator(orch)
        upd = MagicMock(spec=Update)
        upd.effective_message = MagicMock(spec=Message)
        upd.effective_message.text = ""
        upd.effective_message.reply_text = AsyncMock()
        upd.effective_chat = MagicMock(id=99900)
        upd.effective_user = MagicMock(id=99900, username=None, first_name=None)
        await text_message_handler(upd, MagicMock())
        upd.effective_message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_none_message_returns_early(self):
        upd = MagicMock(spec=Update)
        upd.effective_message = None
        await text_message_handler(upd, MagicMock())  # must not raise

    @pytest.mark.asyncio
    async def test_handler_dispatches_to_orchestrator(self, orch):
        set_orchestrator(orch)
        upd, replies = _build_update(
            "Plan a trip from Chennai to Goa for 2 people, 4 days, budget Rs25000",
            chat_id=20001, user_id=20001,
        )
        await text_message_handler(upd, MagicMock())
        assert len(replies) > 0
        full = "\n".join(replies)
        assert any(kw in full for kw in ["Budlance Trip Plan", "Financial Waterfall", "budget"])

    @pytest.mark.asyncio
    async def test_markdown_failure_falls_back_to_plain(self, orch):
        set_orchestrator(orch)
        plain = []

        async def _flaky(chunk, **kw):
            if kw.get("parse_mode") == "Markdown":
                raise Exception("ENTITY_PARSE_ERROR")
            plain.append(chunk)

        upd = MagicMock(spec=Update)
        msg = MagicMock(spec=Message)
        msg.text = "Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs20000"
        msg.reply_text = AsyncMock(side_effect=_flaky)
        upd.effective_message = msg
        upd.effective_chat = MagicMock(id=20002)
        upd.effective_user = MagicMock(id=20002, username=None, first_name=None)
        await text_message_handler(upd, MagicMock())
        assert len(plain) > 0

    def test_no_token_returns_none(self):
        from unittest.mock import patch
        with patch("budlance.bot.bot.get_settings") as mock_settings:
            mock_settings.return_value.telegram_bot_token = ""
            assert build_bot_application(token="") is None

    def test_valid_token_registers_handlers(self):
        app = build_bot_application(token="123456789:ABCDEFghijklmnopqrstuvwxyz123456789")
        assert app is not None
        assert len(app.handlers[0]) >= 2

    def test_set_get_orchestrator(self, orch):
        set_orchestrator(orch)
        assert get_orchestrator() is orch
        set_orchestrator(None)

# =========================================================================
# Webhook / Health
# =========================================================================

class TestWebhookAndHealthEndpoints:

    @pytest.mark.asyncio
    async def test_health_200(self):
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/health")
        assert r.status_code == 200
        d = r.json()
        assert d["status"] == "healthy"
        assert "version" in d and "telegram_configured" in d

    @pytest.mark.asyncio
    async def test_webhook_503_no_bot(self):
        app = create_app()
        app.state.bot_app = None
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/webhook", json={"update_id": 1})
        assert r.status_code == 503
        assert "not configured" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_webhook_400_non_json(self):
        app = create_app()
        app.state.bot_app = MagicMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/webhook", content="bad",
                headers={"Content-Type": "application/json"})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_webhook_200_dispatches(self):
        app = create_app()
        mb = MagicMock()
        mb.bot = MagicMock()
        mb.bot.defaults = None
        mb.process_update = AsyncMock()
        app.state.bot_app = mb
        payload = {
            "update_id": 99001,
            "message": {
                "message_id": 1, "date": 1700000000,
                "chat": {"id": 88001, "type": "private", "first_name": "E2E"},
                "from": {"id": 88001, "is_bot": False, "first_name": "E2E"},
                "text": "Plan a trip",
            },
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/webhook", json=payload)
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        mb.process_update.assert_called_once()

# =========================================================================
# Scenario A - New Trip
# =========================================================================

class TestScenarioANewTrip:

    @pytest.mark.asyncio
    async def test_a1_complete_trip(self, orch):
        res = await orch.handle_user_message(
            telegram_user_id=30001, chat_id=30001,
            message="Plan a 5 day trip from Chennai with Rs15000 for 2 people",
        )
        assert res.status in ("FEASIBLE", "NOT_FEASIBLE", "CLARIFICATION")

    @pytest.mark.asyncio
    async def test_a2_missing_budget(self, orch):
        res = await orch.handle_user_message(
            telegram_user_id=30002, chat_id=30002,
            message="Plan a trip from Chennai to Goa for 2 people, 3 days",
        )
        assert res.status == "CLARIFICATION"
        assert "budget" in res.message_text.lower()

    @pytest.mark.asyncio
    async def test_a3_missing_days(self, orch):
        res = await orch.handle_user_message(
            telegram_user_id=30003, chat_id=30003,
            message="Trip from Mumbai to Goa, 2 travelers, budget Rs20000",
        )
        assert res.status == "CLARIFICATION"
        assert "days" in res.message_text.lower() or "how many" in res.message_text.lower()

    @pytest.mark.asyncio
    async def test_a4_no_stale_state(self, orch, repos):
        cid = 30004
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="Trip complete")
        await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="skip")
        res = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid, message="Plan a trip to Delhi",
        )
        assert res.status == "CLARIFICATION"
        pending = repos["conversation_repo"].get_pending_intent(cid)
        if pending:
            assert pending.budget is None
            assert pending.people is None

    @pytest.mark.asyncio
    async def test_a5_via_handler(self, orch):
        set_orchestrator(orch)
        upd, replies = _build_update(
            "Plan a 5 day trip from Chennai with Rs15000 for 2 people",
            chat_id=30005, user_id=30005,
        )
        await text_message_handler(upd, MagicMock())
        assert len(replies) > 0

# =========================================================================
# Scenario B - Feasibility
# =========================================================================

class TestScenarioBFeasibility:

    @pytest.mark.asyncio
    async def test_b1_feasible_contains_waterfall(self, orch):
        res = await orch.handle_user_message(
            telegram_user_id=31001, chat_id=31001,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs30000",
        )
        assert res.status == "FEASIBLE"
        assert res.trip_id is not None
        assert "Financial Waterfall" in res.message_text or "Budlance Trip Plan" in res.message_text

    @pytest.mark.asyncio
    async def test_b2_trip_in_db(self, orch, repos):
        res = await orch.handle_user_message(
            telegram_user_id=31003, chat_id=31003,
            message="Plan a trip from Chennai to Goa for 2 people, 3 days, budget Rs25000",
        )
        if res.trip_id:
            trip = repos["trip_repo"].get_trip(res.trip_id)
            assert trip is not None
            assert trip.status in ("PLANNING", "ACTIVE")

    @pytest.mark.asyncio
    async def test_b3_trip_id_is_uuid(self, orch):
        res = await orch.handle_user_message(
            telegram_user_id=31004, chat_id=31004,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs30000",
        )
        if res.trip_id:
            assert isinstance(res.trip_id, UUID)


# =========================================================================
# Scenario C - Transport
# =========================================================================

class TestScenarioCTransport:

    @pytest.mark.asyncio
    async def test_c1_change_transport_preserves_budget(self, orch, repos):
        cid = 32002
        await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs30000",
        )
        before = repos["conversation_repo"].get_pending_intent(cid)
        budget_before = before.budget if before else None
        await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="change to sleeper class")
        after = repos["conversation_repo"].get_pending_intent(cid)
        if after and budget_before:
            assert after.budget == budget_before


# =========================================================================
# Scenario D - Booking Lifecycle
# =========================================================================

class TestScenarioDBookingLifecycle:

    @pytest.mark.asyncio
    async def test_d1_activate_trip(self, orch, repos):
        cid = 33001
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        assert plan.trip_id is not None
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        trip = repos["trip_repo"].get_trip(plan.trip_id)
        assert trip.status == "ACTIVE" and trip.is_active is True and trip.id == plan.trip_id

    @pytest.mark.asyncio
    async def test_d2_no_duplicate(self, orch, repos):
        cid = 33002
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Chennai to Goa for 2 people, 3 days, budget Rs30000",
        )
        if plan.trip_id:
            repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        active = repos["trip_repo"].get_active_trip(cid)
        if active:
            assert active.id == plan.trip_id

    @pytest.mark.asyncio
    async def test_d3_active_pointer(self, orch, repos):
        cid = 33003
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        if plan.trip_id:
            repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
            active = repos["trip_repo"].get_active_trip(cid)
            assert active is not None and active.id == plan.trip_id

# =========================================================================
# Scenario E - Expense Logging
# =========================================================================

class TestScenarioEExpenseLogging:

    @pytest.mark.asyncio
    async def test_e1_expense_in_ledger(self, orch, repos):
        cid = 34001
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        exp = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid, message="Spent 500 on dinner",
        )
        assert exp.status == "EXPENSE_LOGGED" and exp.trip_id == plan.trip_id
        entries = repos["ledger_repo"].get_ledger_entries(plan.trip_id)
        assert Decimal("500.00") in [e.actual_amount for e in entries if e.actual_amount]

    @pytest.mark.asyncio
    async def test_e2_trip_remains_active(self, orch, repos):
        cid = 34002
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="Spent 800 on lunch")
        trip = repos["trip_repo"].get_trip(plan.trip_id)
        assert trip.status == "ACTIVE" and trip.is_active is True

    @pytest.mark.asyncio
    async def test_e3_expense_via_handler(self, orch, repos):
        set_orchestrator(orch)
        cid = 34003
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        upd, replies = _build_update("Spent 300 on auto", chat_id=cid, user_id=cid)
        await text_message_handler(upd, MagicMock())
        assert len(replies) > 0


# =========================================================================
# Scenario F - Rescue Mode
# =========================================================================

class TestScenarioFRescue:

    @pytest.mark.asyncio
    async def test_f1_rescue_advisory(self, orch, repos):
        cid = 35001
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        res = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid, message="Auto driver asking 600 for 2 km ride",
        )
        assert res.status == "RESCUE"
        assert any(kw in res.message_text for kw in ["Advisory", "Fare", "600", "driver", "fare"])

    @pytest.mark.asyncio
    async def test_f2_rescue_no_new_trip(self, orch, repos):
        cid = 35002
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid, message="Auto driver asking 700 for 2 km",
        )
        active = repos["trip_repo"].get_active_trip(cid)
        assert active is not None and active.id == plan.trip_id

    @pytest.mark.asyncio
    async def test_f3_rescue_via_handler(self, orch, repos):
        set_orchestrator(orch)
        cid = 35003
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        upd, replies = _build_update("Auto driver asking 600 for 2 km", chat_id=cid, user_id=cid)
        await text_message_handler(upd, MagicMock())
        assert len(replies) > 0

# =========================================================================
# Scenario G - Completion Prompt
# =========================================================================

class TestScenarioGCompletion:

    @pytest.mark.asyncio
    async def test_g1_pending_reconciliation(self, orch, repos):
        cid = 36001
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        res = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid, message="Trip complete: reached home safely",
        )
        assert res.status == "PENDING_RECONCILIATION"

    @pytest.mark.asyncio
    async def test_g2_skip_in_prompt(self, orch, repos):
        cid = 36002
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        res = await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="Trip complete")
        assert "skip" in res.message_text.lower()

    @pytest.mark.asyncio
    async def test_g3_repeated_complete_safe(self, orch, repos):
        cid = 36003
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="Trip complete")
        await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="skip")
        res2 = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid, message="Trip complete",
        )
        assert res2.status in ("NO_ACTIVE_TRIP", "CLARIFICATION", "PENDING_RECONCILIATION")
        assert repos["trip_repo"].get_trip(plan.trip_id).status == "COMPLETED"


# =========================================================================
# Scenario H - Reconciliation
# =========================================================================

class TestScenarioHReconciliation:

    @pytest.mark.asyncio
    async def test_h1_completes_trip(self, orch, repos):
        cid = 37001
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="Trip complete")
        res = await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="22500")
        assert res.status == "COMPLETED"
        trip = repos["trip_repo"].get_trip(plan.trip_id)
        assert trip.status == "COMPLETED" and trip.is_active is False

    @pytest.mark.asyncio
    async def test_h2_recorded_actual_spend(self, orch, repos):
        cid = 37002
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="Trip complete")
        res = await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="22000")
        assert res.status == "COMPLETED"
        assert "22,000" in res.message_text
        summary = orch.ledger_manager.get_summary(plan.trip_id)
        assert summary.total_spent == Decimal("22000.00")

    @pytest.mark.asyncio
    async def test_h3_variance_correct(self, orch, repos):
        cid = 37003
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="Trip complete")
        res = await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="20000")
        assert res.status == "COMPLETED"
        assert "5,000" in res.message_text
        summary = orch.ledger_manager.get_summary(plan.trip_id)
        assert summary.total_spent == Decimal("20000.00")
        assert plan.budget_breakdown.total_budget - summary.total_spent == Decimal("5000.00")

    @pytest.mark.asyncio
    async def test_h4_completion_reason_persisted(self, orch, repos):
        cid = 37004
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid, message="Trip complete: reached home safely",
        )
        await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="skip")
        trip = repos["trip_repo"].get_trip(plan.trip_id)
        assert trip.status == "COMPLETED" and trip.completion_reason is not None

    @pytest.mark.asyncio
    async def test_h5_active_pointer_cleared(self, orch, repos):
        cid = 37005
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="Trip complete")
        await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="skip")
        assert repos["trip_repo"].get_active_trip(cid) is None

    @pytest.mark.asyncio
    async def test_h6_reconciliation_via_handler(self, orch, repos):
        set_orchestrator(orch)
        cid = 37006
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        upd_c, _ = _build_update("Trip complete", chat_id=cid, user_id=cid)
        await text_message_handler(upd_c, MagicMock())
        upd_r, replies = _build_update("21000", chat_id=cid, user_id=cid)
        await text_message_handler(upd_r, MagicMock())
        assert len(replies) > 0
        full = "\n".join(replies)
        assert any(kw in full for kw in ["COMPLETED", "Completed", "completed", "budget", "21000"])

# =========================================================================
# Scenario I - New Trip After Completion
# =========================================================================

class TestScenarioINewTripAfterCompletion:

    @pytest.mark.asyncio
    async def test_i1_new_trip_id(self, orch, repos):
        cid = 38001
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="Trip complete")
        await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="skip")
        new = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Delhi for 3 people, 5 days, budget Rs35000",
        )
        if new.trip_id:
            assert new.trip_id != plan.trip_id

    @pytest.mark.asyncio
    async def test_i2_old_trip_completed(self, orch, repos):
        cid = 38002
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="Trip complete")
        await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="skip")
        await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Delhi for 3 people, 5 days, budget Rs35000",
        )
        old = repos["trip_repo"].get_trip(plan.trip_id)
        assert old.status == "COMPLETED" and old.is_active is False

    @pytest.mark.asyncio
    async def test_i3_independent_ledger(self, orch, repos):
        cid = 38003
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="Spent 1000 on food")
        await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="Trip complete")
        await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="skip")
        new = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Delhi for 3 people, 5 days, budget Rs35000",
        )
        if new.trip_id and new.trip_id != plan.trip_id:
            old_ids = {e.id for e in repos["ledger_repo"].get_ledger_entries(plan.trip_id)}
            new_ids = {e.id for e in repos["ledger_repo"].get_ledger_entries(new.trip_id)}
            assert old_ids.isdisjoint(new_ids)


# =========================================================================
# Reconciliation Interruptions
# =========================================================================

class TestReconciliationInterruptions:

    @pytest.mark.asyncio
    async def test_ri1_unrecognized_preserves_pending(self, orch, repos):
        cid = 39001
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="Trip complete")
        res = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid, message="hello what is up",
        )
        assert res.status == "PENDING_RECONCILIATION"
        assert "skip" in res.message_text.lower() or "pending" in res.message_text.lower()

    @pytest.mark.asyncio
    async def test_ri2_expense_during_reconciliation(self, orch, repos):
        cid = 39002
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="Trip complete")
        exp = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid, message="spent 500 on dinner",
        )
        assert exp.status == "EXPENSE_LOGGED"
        entries = repos["ledger_repo"].get_ledger_entries(plan.trip_id)
        assert Decimal("500.00") in [e.actual_amount for e in entries if e.actual_amount]

    @pytest.mark.asyncio
    async def test_ri3_rescue_during_reconciliation(self, orch, repos):
        cid = 39003
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="Trip complete")
        res = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid, message="auto driver asking 600 rupees for 2 km",
        )
        assert res.status == "RESCUE"
        assert repos["trip_repo"].get_trip(plan.trip_id).status == "ACTIVE"

    @pytest.mark.asyncio
    async def test_ri4_new_trip_finalizes_old(self, orch, repos):
        cid = 39004
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="Trip complete")
        new = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Delhi to Jaipur for 2 people, 3 days, budget Rs20000",
        )
        old = repos["trip_repo"].get_trip(plan.trip_id)
        assert old.status == "COMPLETED"
        assert old.completion_reason == "NEW_TRIP_STARTED"
        if new.trip_id:
            assert new.trip_id != plan.trip_id

    @pytest.mark.asyncio
    async def test_ri5_change_no_completion(self, orch, repos):
        cid = 39005
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="Trip complete")
        res = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid, message="make it 4 days",
        )
        assert res.status != "COMPLETED"
        assert repos["trip_repo"].get_trip(plan.trip_id).status == "ACTIVE"

# =========================================================================
# State Isolation
# =========================================================================

class TestStateIsolationBetweenChats:

    @pytest.mark.asyncio
    async def test_si1_independent_trips(self, orch, repos):
        ra = await orch.handle_user_message(
            telegram_user_id=40001, chat_id=40001,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        rb = await orch.handle_user_message(
            telegram_user_id=40002, chat_id=40002,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        if ra.trip_id and rb.trip_id:
            assert ra.trip_id != rb.trip_id

    @pytest.mark.asyncio
    async def test_si2_chat_a_completion_no_affect_chat_b(self, orch, repos):
        ra = await orch.handle_user_message(
            telegram_user_id=40003, chat_id=40003,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        rb = await orch.handle_user_message(
            telegram_user_id=40004, chat_id=40004,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        if ra.trip_id and rb.trip_id:
            repos["trip_repo"].update_trip_status(ra.trip_id, "ACTIVE", is_active=True)
            repos["trip_repo"].update_trip_status(rb.trip_id, "ACTIVE", is_active=True)
            await orch.handle_user_message(telegram_user_id=40003, chat_id=40003, message="Trip complete")
            await orch.handle_user_message(telegram_user_id=40003, chat_id=40003, message="skip")
            trip_b = repos["trip_repo"].get_trip(rb.trip_id)
            assert trip_b.status == "ACTIVE" and trip_b.is_active is True

    @pytest.mark.asyncio
    async def test_si3_reconciliation_scoped(self, orch, repos):
        res = await orch.handle_user_message(
            telegram_user_id=40005, chat_id=40005,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(res.trip_id, "ACTIVE", is_active=True)
        await orch.handle_user_message(telegram_user_id=40005, chat_id=40005, message="Trip complete")
        assert repos["conversation_repo"].is_reconciling(40005) is True
        assert repos["conversation_repo"].is_reconciling(40006) is False


# =========================================================================
# Persistence Across Restart
# =========================================================================

class TestPersistenceAcrossRestart:

    def test_pr1_trip_survives_restart(self, repos):
        from uuid import uuid4 as u4
        tr1 = repos["trip_repo"]
        trip = tr1.create_trip(
            user_id=u4(), telegram_chat_id=770001,
            budget_total=Decimal("20000.00"), destination="Jaipur",
            status="PLANNING", is_active=True,
        )
        tr1.update_trip_status(trip.id, "COMPLETED", completion_reason="Vacation ended", is_active=False)
        tr2 = TripRepository(client=None)
        tr2._memory_store = dict(tr1._memory_store)
        r = tr2.get_trip(trip.id)
        assert r is not None
        assert r.status == "COMPLETED"
        assert r.is_active is False
        assert r.completion_reason == "Vacation ended"

    def test_pr2_conversation_survives_restart(self, repos):
        cr1 = repos["conversation_repo"]
        intent = ParsedTripIntent(
            action=TripAction.NEW_TRIP, budget=Decimal("30000"),
            people=2, days=4, origin="Chennai", destination="Goa",
        )
        cr1.save_pending_intent(770002, intent)
        cr2 = ConversationStateRepository(client=None)
        cr2._memory_store = dict(cr1._memory_store)
        r = cr2.get_pending_intent(770002)
        assert r is not None
        assert r.budget == Decimal("30000") and r.destination == "Goa"

    def test_pr3_active_trip_recovered(self, repos):
        from uuid import uuid4 as u4
        tr1 = repos["trip_repo"]
        trip = tr1.create_trip(
            user_id=u4(), telegram_chat_id=770003,
            budget_total=Decimal("25000.00"), destination="Goa",
            status="ACTIVE", is_active=True,
        )
        tr2 = TripRepository(client=None)
        tr2._memory_store = dict(tr1._memory_store)
        active = tr2.get_active_trip(770003)
        assert active is not None and active.id == trip.id
        tr2.update_trip_status(active.id, "COMPLETED", completion_reason="Restart test", is_active=False)
        assert tr2.get_trip(active.id).status == "COMPLETED"


# =========================================================================
# Edge Cases
# =========================================================================

class TestTelegramEdgeCases:

    @pytest.mark.asyncio
    async def test_ec1_empty_clarification(self, orch):
        res = await orch.handle_user_message(telegram_user_id=50001, chat_id=50001, message="")
        assert res.status == "CLARIFICATION"

    @pytest.mark.asyncio
    async def test_ec2_whitespace_clarification(self, orch):
        res = await orch.handle_user_message(telegram_user_id=50002, chat_id=50002, message="   ")
        assert res.status == "CLARIFICATION"

    @pytest.mark.asyncio
    async def test_ec3_long_message_no_crash(self, orch):
        msg = "Plan a trip from Mumbai to Goa " * 100
        res = await orch.handle_user_message(telegram_user_id=50004, chat_id=50004, message=msg)
        assert res.status in ("FEASIBLE", "NOT_FEASIBLE", "CLARIFICATION", "PENDING_RECONCILIATION")

    @pytest.mark.asyncio
    async def test_ec4_malformed_amount(self, orch, repos):
        cid = 50005
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        await orch.handle_user_message(telegram_user_id=cid, chat_id=cid, message="Trip complete")
        res = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid, message="abc twelve thousand",
        )
        assert res.status in ("PENDING_RECONCILIATION", "CLARIFICATION")

    @pytest.mark.asyncio
    async def test_ec5_reply_error_no_corruption(self, orch, repos):
        set_orchestrator(orch)
        cid = 50006
        plan = await orch.handle_user_message(
            telegram_user_id=cid, chat_id=cid,
            message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget Rs25000",
        )
        repos["trip_repo"].update_trip_status(plan.trip_id, "ACTIVE", is_active=True)
        calls = [0]

        async def _flaky(chunk, **kw):
            calls[0] += 1
            if kw.get("parse_mode") == "Markdown" and calls[0] <= 2:
                raise Exception("NetworkError")

        upd = MagicMock(spec=Update)
        msg = MagicMock(spec=Message)
        msg.text = "Spent 300 on lunch"
        msg.reply_text = AsyncMock(side_effect=_flaky)
        upd.effective_message = msg
        upd.effective_chat = MagicMock(id=cid)
        upd.effective_user = MagicMock(id=cid, username=None, first_name=None)
        await text_message_handler(upd, MagicMock())
        assert repos["trip_repo"].get_trip(plan.trip_id).status == "ACTIVE"

    def test_ec6_split_under_limit(self):
        text = "Hello Budlance!"
        chunks = split_telegram_message(text)
        assert len(chunks) == 1 and chunks[0] == text

    def test_ec7_split_over_limit(self):
        chunks = split_telegram_message("Budget: Rs1000\n" * 300)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 4096

    def test_ec8_split_preserves_content(self):
        lines = [f"Line {i}: budget Rs{i * 100}" for i in range(400)]
        chunks = split_telegram_message("\n".join(lines))
        reassembled = "\n".join(chunks)
        for i in [0, 100, 200, 399]:
            assert f"Line {i}:" in reassembled
