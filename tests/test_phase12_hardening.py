"""Phase 12 - Full System Hardening Tests.

Covers the remaining audit tasks not yet addressed by earlier test modules:

Task J -- Persistence partial-failure scenarios:
    J1. Trip repo create_trip raises -> ERROR status, no stacktrace leaked.
    J2. Intent repo save fails after trip created -> ERROR, trip_id not returned.
    J3. Itinerary repo save fails after trip + intent saved -> ERROR status.
    J4. Ledger init fails after itinerary saved -> ERROR status.
    J5. Rescue repo record_rescue_event raises -> rescue result still returned safely.

Task M -- External API failure message safety:
    M1. AI parse_trip_intent raises -> ERROR, no RuntimeError text in reply.
    M2. AI parse_rescue_intent raises during rescue routing -> ERROR, no stacktrace.
    M3. Cache get_travel_data raises ValueError -> ERROR, no detail.
    M4. Normalizer raises unexpectedly -> ERROR, clean user message.

Additional hardening:
    H1. Empty-string message returns CLARIFICATION, not ERROR.
    H2. Whitespace-only message returns CLARIFICATION, not ERROR.
    H3. Rescue message with AI returning unknown type is handled safely.
    H4. Telegram handler: Orchestrator ERROR status still calls reply_text once.
    H5. Telegram handler: reply_text Markdown failure falls back to plain text.
    H6. NOT_FEASIBLE response never contains trip_id (no partial ledger).
    H7. CLARIFICATION response never contains trip_id.
    H8. Second trip for same user/chat deactivates the first trip.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
import pytest
from telegram import Chat, Message, Update, User

from budlance.ai.service import AIIntentService
from budlance.bot.handlers import set_orchestrator, text_message_handler
from budlance.cache.manager import CacheFallbackManager
from budlance.db.models import User as DbUser, utc_now
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
from budlance.normalization.normalizer import DataNormalizer
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.rescue.service import RescueService
from budlance.serpapi.models import DataSource, TravelDataEnvelope


# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def standard_cache():
    """Standard deterministic cache returning stable live envelopes."""
    mgr = MagicMock(spec=CacheFallbackManager)

    async def _get(engine, params, trip_id=None, **kwargs):
        if engine == "google_flights":
            return TravelDataEnvelope(
                source=DataSource.LIVE, engine=engine, query_hash="fh",
                data={"best_flights": [{"flights": [{"airline": "IndiGo"}], "price": 3000}]},
                is_fallback=False,
            )
        if engine == "google_hotels":
            return TravelDataEnvelope(
                source=DataSource.LIVE, engine=engine, query_hash="hh",
                data={
                    "properties": [
                        {
                            "name": "City Grand Hotel", "hotel_class": 4,
                            "rate_per_night": {"extracted_lowest": 2000},
                            "total_rate": {"extracted_lowest": 6000},
                        },
                        {
                            "name": "City Budget Inn", "hotel_class": 3,
                            "rate_per_night": {"extracted_lowest": 800},
                            "total_rate": {"extracted_lowest": 2400},
                        },
                    ]
                },
                is_fallback=False,
            )
        if engine == "google_maps":
            return TravelDataEnvelope(
                source=DataSource.LIVE, engine=engine, query_hash="ph",
                data={"local_results": [{"title": "Marina Beach", "type": "Beach", "rating": 4.5}]},
                is_fallback=False,
            )
        if engine == "google_maps_directions":
            return TravelDataEnvelope(
                source=DataSource.LIVE, engine=engine, query_hash="rh",
                data={"routes": [{"summary": "NH 44", "legs": [
                    {"distance": {"text": "350 km", "value": 350000},
                     "duration": {"text": "6 hours", "value": 21600}}
                ]}]},
                is_fallback=False,
            )
        return TravelDataEnvelope(
            source=DataSource.FALLBACK, engine=engine, query_hash="empty",
            data={}, is_fallback=True,
        )

    mgr.get_travel_data = AsyncMock(side_effect=_get)
    return mgr


def _build_orchestrator(
    cache,
    user_repo=None,
    trip_repo=None,
    intent_repo=None,
    itinerary_repo=None,
    ledger_repo=None,
    rescue_repo=None,
    ai_service=None,
):
    """Build a fully wired orchestrator with optional repo overrides."""
    user_repo = user_repo or UserRepository(client=None)
    trip_repo = trip_repo or TripRepository(client=None)
    intent_repo = intent_repo or IntentRepository(client=None)
    itinerary_repo = itinerary_repo or ItineraryRepository(client=None)
    ledger_repo = ledger_repo or LedgerRepository(client=None)
    rescue_repo = rescue_repo or RescueRepository(client=None)
    ai_service = ai_service or AIIntentService()
    normalizer = DataNormalizer()
    estimation = EstimationLayer()
    budget_engine = ReverseBudgetEngine()
    optimizer = OptimizationEngine(budget_engine=budget_engine, estimation_layer=estimation)
    itin_gen = ItineraryGenerator(itinerary_repo=itinerary_repo)
    ledger_mgr = VirtualLedgerManager(ledger_repo=ledger_repo)
    rescue_service = RescueService(
        trip_repo=trip_repo,
        itinerary_repo=itinerary_repo,
        ledger_repo=ledger_repo,
        rescue_repo=rescue_repo,
        ai_service=ai_service,
        cache_manager=cache,
        normalizer=normalizer,
        budget_engine=budget_engine,
        estimation_layer=estimation,
        ledger_manager=ledger_mgr,
    )
    return BudlanceOrchestrator(
        user_repo=user_repo,
        trip_repo=trip_repo,
        intent_repo=intent_repo,
        itinerary_repo=itinerary_repo,
        ledger_repo=ledger_repo,
        rescue_repo=rescue_repo,
        ai_service=ai_service,
        cache_manager=cache,
        normalizer=normalizer,
        estimation_layer=estimation,
        budget_engine=budget_engine,
        optimizer=optimizer,
        itinerary_generator=itin_gen,
        ledger_manager=ledger_mgr,
        rescue_service=rescue_service,
    )


# ---------------------------------------------------------------------------
# Task J -- Persistence partial-failure scenarios
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_J1_trip_repo_create_raises_returns_error(standard_cache):
    """J1: trip_repo.create_trip raising -> ERROR status, no internal detail leaked."""
    trip_repo = MagicMock(spec=TripRepository)
    trip_repo.create_trip = MagicMock(side_effect=RuntimeError("DB connection refused"))
    trip_repo.get_active_trip = MagicMock(return_value=None)
    trip_repo.deactivate_previous_trips = MagicMock()

    user_repo = MagicMock(spec=UserRepository)
    user_repo.get_or_create_user = MagicMock(
        return_value=DbUser(id=uuid4(), telegram_user_id=2001, created_at=utc_now(), updated_at=utc_now())
    )

    orch = _build_orchestrator(standard_cache, user_repo=user_repo, trip_repo=trip_repo)
    result = await orch.handle_user_message(
        telegram_user_id=2001,
        chat_id=8001,
        message="Plan a trip from Chennai to Goa for 2 people, 3 days, with budget 25000",
    )

    assert result.status == "ERROR"
    assert result.trip_id is None
    assert "RuntimeError" not in result.message_text
    assert "DB connection refused" not in result.message_text
    assert "error occurred" in result.message_text.lower()


@pytest.mark.asyncio
async def test_J2_intent_repo_save_raises_returns_error(standard_cache):
    """J2: intent_repo.save_trip_intent raising after trip is created -> ERROR, no stacktrace."""
    intent_repo = MagicMock(spec=IntentRepository)
    intent_repo.save_trip_intent = MagicMock(side_effect=OSError("Disk quota exceeded"))
    intent_repo.get_trip_intent = MagicMock(return_value=None)

    orch = _build_orchestrator(standard_cache, intent_repo=intent_repo)
    result = await orch.handle_user_message(
        telegram_user_id=2002,
        chat_id=8002,
        message="Plan a trip from Chennai to Goa for 2 people, 3 days, with budget 25000",
    )

    assert result.status == "ERROR"
    assert "OSError" not in result.message_text
    assert "Disk quota" not in result.message_text
    assert "error occurred" in result.message_text.lower()


@pytest.mark.asyncio
async def test_J3_itinerary_repo_save_raises_returns_error(standard_cache):
    """J3: itinerary_repo.save_itinerary raising -> ERROR status, no internal detail."""
    itinerary_repo = MagicMock(spec=ItineraryRepository)
    itinerary_repo.save_itinerary = MagicMock(side_effect=ValueError("Itinerary schema mismatch"))
    itinerary_repo.get_itinerary = MagicMock(return_value=None)

    orch = _build_orchestrator(standard_cache, itinerary_repo=itinerary_repo)
    result = await orch.handle_user_message(
        telegram_user_id=2003,
        chat_id=8003,
        message="Plan a trip from Chennai to Goa for 2 people, 3 days, with budget 25000",
    )

    assert result.status == "ERROR"
    assert "ValueError" not in result.message_text
    assert "schema mismatch" not in result.message_text


@pytest.mark.asyncio
async def test_J4_ledger_init_raises_returns_error(standard_cache):
    """J4: ledger_manager.initialize_ledger raising -> ERROR status, clean message."""
    itinerary_repo = ItineraryRepository(client=None)
    ledger_repo = MagicMock(spec=LedgerRepository)

    orch = _build_orchestrator(standard_cache, itinerary_repo=itinerary_repo, ledger_repo=ledger_repo)

    ledger_mgr = MagicMock(spec=VirtualLedgerManager)
    ledger_mgr.initialize_ledger = MagicMock(side_effect=RuntimeError("Ledger write failure"))
    ledger_mgr.get_summary = MagicMock(return_value=None)
    orch.ledger_manager = ledger_mgr

    result = await orch.handle_user_message(
        telegram_user_id=2004,
        chat_id=8004,
        message="Plan a trip from Chennai to Goa for 2 people, 3 days, with budget 25000",
    )

    assert result.status == "ERROR"
    assert "Ledger write failure" not in result.message_text
    assert "RuntimeError" not in result.message_text


@pytest.mark.asyncio
async def test_J5_rescue_event_persist_raises_result_still_returned(standard_cache):
    """J5: rescue_repo.record_rescue_event raising -> rescue pipeline returns safely."""
    rescue_repo = MagicMock(spec=RescueRepository)
    rescue_repo.record_rescue_event = MagicMock(side_effect=RuntimeError("Rescue table locked"))

    working_orch = _build_orchestrator(standard_cache)
    plan = await working_orch.handle_user_message(
        telegram_user_id=2005,
        chat_id=8005,
        message="Plan a trip from Chennai to Goa for 2 people, 3 days, with budget 25000",
    )
    assert plan.status == "FEASIBLE"

    ledger_mgr = VirtualLedgerManager(working_orch.ledger_repo)
    rescue_service = RescueService(
        trip_repo=working_orch.trip_repo,
        itinerary_repo=working_orch.itinerary_repo,
        ledger_repo=working_orch.ledger_repo,
        rescue_repo=rescue_repo,
        ai_service=working_orch.ai_service,
        cache_manager=standard_cache,
        normalizer=working_orch.normalizer,
        budget_engine=working_orch.budget_engine,
        estimation_layer=working_orch.estimation,
        ledger_manager=ledger_mgr,
    )
    working_orch.rescue_service = rescue_service

    rescue_result = await working_orch.handle_user_message(
        telegram_user_id=2005,
        chat_id=8005,
        message="It is raining heavily at the beach",
    )

    assert rescue_result.status in ("RESCUE", "ERROR")
    assert "Rescue table locked" not in rescue_result.message_text
    assert "RuntimeError" not in rescue_result.message_text


# ---------------------------------------------------------------------------
# Task M -- External API failure message safety
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_M1_ai_parse_trip_intent_raises_returns_error(standard_cache):
    """M1: AI parse_trip_intent raising -> ERROR status, no exception detail in message."""
    ai_service = MagicMock(spec=AIIntentService)
    ai_service.parse_rescue_intent = AsyncMock(
        return_value=MagicMock(rescue_type="none")
    )
    ai_service.parse_trip_intent = AsyncMock(side_effect=ConnectionError("OpenRouter timeout"))

    orch = _build_orchestrator(standard_cache, ai_service=ai_service)
    result = await orch.handle_user_message(
        telegram_user_id=3001,
        chat_id=9001,
        message="Plan a trip from Delhi to Goa for 2 people, 3 days, budget 20000",
    )

    assert result.status == "ERROR"
    assert "OpenRouter timeout" not in result.message_text
    assert "ConnectionError" not in result.message_text
    assert "error occurred" in result.message_text.lower()


@pytest.mark.asyncio
async def test_M2_ai_parse_rescue_intent_raises_returns_error(standard_cache):
    """M2: AI parse_rescue_intent raising -> ERROR, no stacktrace in message."""
    ai_service = MagicMock(spec=AIIntentService)
    ai_service.parse_rescue_intent = AsyncMock(
        side_effect=TimeoutError("AI model unreachable")
    )

    orch = _build_orchestrator(standard_cache, ai_service=ai_service)
    result = await orch.handle_user_message(
        telegram_user_id=3002,
        chat_id=9002,
        message="The auto driver is asking 700",
    )

    assert result.status == "ERROR"
    assert "TimeoutError" not in result.message_text
    assert "AI model unreachable" not in result.message_text


@pytest.mark.asyncio
async def test_M3_cache_raises_value_error_returns_error():
    """M3: Cache raising ValueError -> ERROR, no internal detail in message."""
    bad_cache = MagicMock(spec=CacheFallbackManager)
    bad_cache.get_travel_data = AsyncMock(side_effect=ValueError("Unexpected SerpApi schema"))

    orch = _build_orchestrator(bad_cache)
    result = await orch.handle_user_message(
        telegram_user_id=3003,
        chat_id=9003,
        message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget 25000",
    )

    assert result.status == "ERROR"
    assert "ValueError" not in result.message_text
    assert "Unexpected SerpApi schema" not in result.message_text
    assert "error occurred" in result.message_text.lower()


@pytest.mark.asyncio
async def test_M4_normalizer_raises_unexpectedly_returns_error(standard_cache):
    """M4: DataNormalizer raising mid-pipeline -> ERROR, clean user-safe message."""
    orch = _build_orchestrator(standard_cache)
    orch.normalizer = MagicMock(spec=DataNormalizer)
    orch.normalizer.normalize_flights = MagicMock(
        side_effect=AttributeError("NoneType object has no attribute items")
    )
    orch.rescue_service.normalizer = orch.normalizer

    result = await orch.handle_user_message(
        telegram_user_id=3004,
        chat_id=9004,
        message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget 25000",
    )

    assert result.status == "ERROR"
    assert "AttributeError" not in result.message_text
    assert "NoneType" not in result.message_text


# ---------------------------------------------------------------------------
# Additional hardening tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_H1_empty_string_message_returns_clarification(standard_cache):
    """H1: Empty message string -> CLARIFICATION, never ERROR or crash."""
    orch = _build_orchestrator(standard_cache)
    result = await orch.handle_user_message(
        telegram_user_id=4001,
        chat_id=7001,
        message="",
    )

    assert result.status == "CLARIFICATION"
    assert result.trip_id is None
    assert len(result.message_text) > 0


@pytest.mark.asyncio
async def test_H2_whitespace_only_message_returns_clarification(standard_cache):
    """H2: Whitespace-only message -> CLARIFICATION (same guard as empty)."""
    orch = _build_orchestrator(standard_cache)
    result = await orch.handle_user_message(
        telegram_user_id=4002,
        chat_id=7002,
        message="   \t\n  ",
    )

    assert result.status == "CLARIFICATION"
    assert result.trip_id is None


@pytest.mark.asyncio
async def test_H3_rescue_unknown_type_handled_safely(standard_cache):
    """H3: AI returning unrecognised rescue_type -> handled without crash."""
    working_orch = _build_orchestrator(standard_cache)
    plan = await working_orch.handle_user_message(
        telegram_user_id=4003,
        chat_id=7003,
        message="Plan a trip from Delhi to Goa for 2 people, 3 days, budget 25000",
    )
    assert plan.status == "FEASIBLE"

    ai_service = MagicMock(spec=AIIntentService)
    ai_service.parse_rescue_intent = AsyncMock(
        return_value=MagicMock(
            rescue_type="earthquake",
            user_issue="Building collapsed",
            reported_price=None,
            service_type=None,
            location_or_context="Hotel",
        )
    )
    ai_service.parse_trip_intent = AsyncMock(
        side_effect=RuntimeError("should not be called for rescue")
    )
    working_orch.ai_service = ai_service
    working_orch.rescue_service.ai_service = ai_service

    result = await working_orch.handle_user_message(
        telegram_user_id=4003,
        chat_id=7003,
        message="There was an earthquake!",
    )

    assert result.status in ("RESCUE", "CLARIFICATION", "ERROR")
    assert "RuntimeError" not in result.message_text
    assert "should not be called" not in result.message_text


@pytest.mark.asyncio
async def test_H4_telegram_handler_error_status_replies_once():
    """H4: Orchestrator ERROR status -> Telegram handler calls reply_text exactly once."""
    bad_cache = MagicMock(spec=CacheFallbackManager)
    bad_cache.get_travel_data = AsyncMock(side_effect=RuntimeError("SerpApi down"))

    orch = _build_orchestrator(bad_cache)
    set_orchestrator(orch)

    update = MagicMock(spec=Update)
    message = MagicMock(spec=Message)
    message.text = "Plan a trip from Mumbai to Goa for 2 people, 3 days, budget 25000"
    message.reply_text = AsyncMock()
    update.effective_message = message
    update.effective_chat = MagicMock(spec=Chat, id=7004)
    update.effective_user = MagicMock(spec=User, id=4004, username="err_user", first_name="Err")
    context = MagicMock()

    await text_message_handler(update, context)

    assert message.reply_text.call_count == 1
    reply_content = message.reply_text.call_args[0][0]
    assert "error occurred" in reply_content.lower() or "oops" in reply_content.lower()


@pytest.mark.asyncio
async def test_H5_telegram_handler_markdown_fallback_on_exception(standard_cache):
    """H5: reply_text Markdown mode raising -> handler retries with plain text."""
    orch = _build_orchestrator(standard_cache)
    set_orchestrator(orch)

    update = MagicMock(spec=Update)
    message = MagicMock(spec=Message)
    message.text = "Plan a trip from Mumbai to Goa for 2 people, 3 days, budget 25000"
    message.reply_text = AsyncMock(
        side_effect=[Exception("Bad markdown entity"), None]
    )
    update.effective_message = message
    update.effective_chat = MagicMock(spec=Chat, id=7005)
    update.effective_user = MagicMock(spec=User, id=4005, username="md_user", first_name="Md")
    context = MagicMock()

    await text_message_handler(update, context)

    assert message.reply_text.call_count == 2


@pytest.mark.asyncio
async def test_H6_not_feasible_response_has_no_trip_id(standard_cache):
    """H6: NOT_FEASIBLE result never carries a trip_id (no partial ledger committed).

    Uses a MagicMock ParsedTripIntent so the orchestrator reaches the budget-engine
    gate with an impossible budget (Rs.100 for 5 days) and returns NOT_FEASIBLE.
    """
    from budlance.db.models import TripIntent, utc_now as _utc
    from uuid import uuid4 as _uuid4

    # Build a duck-typed intent object — Pydantic model blocks attribute assignment,
    # so we use MagicMock to freely attach .to_trip_intent_record.
    mock_intent = MagicMock()
    mock_intent.budget = Decimal("100.00")
    mock_intent.people = 2
    mock_intent.days = 5
    mock_intent.origin = "Mumbai"
    mock_intent.destination = "Goa"
    mock_intent.currency = "INR"
    mock_intent.interests = []
    mock_intent.to_trip_intent_record = MagicMock(
        return_value=TripIntent(
            id=_uuid4(),
            trip_id=_uuid4(),
            destination="Goa",
            origin="Mumbai",
            budget=Decimal("100.00"),
            people=2,
            days=5,
            currency="INR",
            raw_prompt="impossible budget trip",
            extracted_at=_utc(),
        )
    )

    ai_service = MagicMock(spec=AIIntentService)
    ai_service.parse_rescue_intent = AsyncMock(return_value=MagicMock(rescue_type="none"))
    ai_service.parse_trip_intent = AsyncMock(return_value=mock_intent)

    orch = _build_orchestrator(standard_cache, ai_service=ai_service)
    result = await orch.handle_user_message(
        telegram_user_id=4006,
        chat_id=7006,
        message="Plan a trip from Mumbai to Goa for 2 people, 5 days, budget 100",
    )

    assert result.status == "NOT_FEASIBLE"
    assert result.trip_id is None
    assert result.ledger_summary is None
    assert result.generated_itinerary is None


@pytest.mark.asyncio
async def test_H7_clarification_response_has_no_trip_id(standard_cache):
    """H7: CLARIFICATION result never carries a trip_id."""
    orch = _build_orchestrator(standard_cache)
    result = await orch.handle_user_message(
        telegram_user_id=4007,
        chat_id=7007,
        message="I want to travel somewhere nice",
    )

    assert result.status == "CLARIFICATION"
    assert result.trip_id is None


@pytest.mark.asyncio
async def test_H8_second_trip_deactivates_first(standard_cache):
    """H8: Planning a second trip for same chat deactivates the first trip."""
    orch = _build_orchestrator(standard_cache)

    first = await orch.handle_user_message(
        telegram_user_id=4008,
        chat_id=7008,
        message="Plan a trip from Mumbai to Goa for 2 people, 3 days, budget 25000",
    )
    assert first.status == "FEASIBLE"
    first_trip_id = first.trip_id
    assert first_trip_id is not None

    second = await orch.handle_user_message(
        telegram_user_id=4008,
        chat_id=7008,
        message="Plan a trip from Delhi to Jaipur for 2 people, 2 days, budget 25000",
    )
    assert second.status == "FEASIBLE"
    second_trip_id = second.trip_id
    assert second_trip_id is not None
    assert second_trip_id != first_trip_id

    first_trip = orch.trip_repo.get_trip(first_trip_id)
    second_trip = orch.trip_repo.get_trip(second_trip_id)
    assert first_trip is not None
    assert second_trip is not None
    assert first_trip.is_active is False
    assert second_trip.is_active is True

    active = orch.trip_repo.get_active_trip(7008)
    assert active is not None
    assert active.id == second_trip_id
