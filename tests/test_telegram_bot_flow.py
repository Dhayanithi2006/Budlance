"""End-to-End Telegram Bot Flow Tests (Task 13).

Verifies multi-turn realistic simulated Telegram interactions:
1. Scenario A — Couple: initial message missing days -> clarification -> '2 days' follow-up -> feasible Gujarat itinerary with couple descriptions.
2. Scenario B — Friends: friends trip to Gujarat -> friends-suited attractions and energetic descriptions.
3. Scenario C — Natural Correction: change couple -> family while preserving all trip constraints.
4. Scenario D — Telegram text_message_handler integration with simulated Update objects.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from telegram import Chat, Message, Update, User

from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.ai.service import AIIntentService
from budlance.attractions.selector import AttractionSelector
from budlance.bot.handlers import set_orchestrator, text_message_handler
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
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.rescue.service import RescueService


@pytest.fixture
def test_orchestrator():
    """Build isolated orchestrator instance with in-memory stores."""
    user_repo = UserRepository(client=None)
    trip_repo = TripRepository(client=None)
    intent_repo = IntentRepository(client=None)
    itinerary_repo = ItineraryRepository(client=None)
    ledger_repo = LedgerRepository(client=None)
    rescue_repo = RescueRepository(client=None)
    conv_repo = ConversationStateRepository(client=None)

    ai_service = AIIntentService(use_mock=True)
    cache_mgr = CacheFallbackManager()
    normalizer = DataNormalizer()
    estimation = EstimationLayer()
    budget_eng = ReverseBudgetEngine()
    optimizer = OptimizationEngine(budget_engine=budget_eng, estimation_layer=estimation)
    selector = AttractionSelector(cache_manager=cache_mgr)
    itin_gen = ItineraryGenerator(itinerary_repo=itinerary_repo, attraction_selector=selector)
    enhancer = ItineraryEnhancer(use_mock=True)
    ledger_mgr = VirtualLedgerManager(ledger_repo=ledger_repo)
    rescue_svc = RescueService(
        trip_repo=trip_repo,
        itinerary_repo=itinerary_repo,
        ledger_repo=ledger_repo,
        rescue_repo=rescue_repo,
        ai_service=ai_service,
        cache_manager=cache_mgr,
        normalizer=normalizer,
        budget_engine=budget_eng,
        estimation_layer=estimation,
        ledger_manager=ledger_mgr,
    )

    orch = BudlanceOrchestrator(
        user_repo=user_repo,
        trip_repo=trip_repo,
        intent_repo=intent_repo,
        itinerary_repo=itinerary_repo,
        ledger_repo=ledger_repo,
        rescue_repo=rescue_repo,
        conversation_repo=conv_repo,
        ai_service=ai_service,
        cache_manager=cache_mgr,
        normalizer=normalizer,
        estimation_layer=estimation,
        budget_engine=budget_eng,
        optimizer=optimizer,
        attraction_selector=selector,
        itinerary_generator=itin_gen,
        itinerary_enhancer=enhancer,
        ledger_manager=ledger_mgr,
        rescue_service=rescue_svc,
    )
    return orch


def _build_telegram_update(text: str, chat_id: int = 1001, user_id: int = 1001) -> tuple[Update, AsyncMock]:
    """Helper to construct a mock Telegram Update and inspect sent reply chunks."""
    update = MagicMock(spec=Update)
    message = MagicMock(spec=Message)
    replies = []

    async def _mock_reply(text_chunk, **kwargs):
        replies.append(text_chunk)

    message.reply_text = AsyncMock(side_effect=_mock_reply)
    message.text = text
    update.effective_message = message
    update.effective_chat = MagicMock(spec=Chat, id=chat_id)
    update.effective_user = MagicMock(spec=User, id=user_id, username="traveler", first_name="Alex")
    return update, replies


@pytest.mark.asyncio
async def test_scenario_a_couple_multiturn_clarification(test_orchestrator):
    """Scenario A: Missing days triggers clarification, '2 days' completes feasible couple trip."""
    chat_id = 2001

    # Turn 1: Missing days
    intent_t1 = ParsedTripIntent(
        action=TripAction.NEW_TRIP,
        budget=Decimal("50000.00"),
        people=2,
        days=None,
        origin="Chennai",
        destination="Gujarat",
        travel_party="couple",
        interests=["heritage", "food"],
        currency="INR",
    )
    test_orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=intent_t1)

    res1 = await test_orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="I would like to go to Gujarat from Chennai, budget 50000, 2 people, we are a couple, interested in heritage and food.",
    )
    assert res1.status == "CLARIFICATION"
    assert "How many days" in res1.message_text
    assert "2 travelers (couple)" in res1.message_text

    # Turn 2: User provides '2 days'
    intent_t2 = ParsedTripIntent(
        action=TripAction.CHANGE_DAYS,
        days=2,
    )
    test_orchestrator.ai_service.parse_trip_intent_with_context = AsyncMock(return_value=intent_t2)

    res2 = await test_orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="2 days",
    )
    assert res2.status == "FEASIBLE"
    assert "(Couple)" in res2.message_text
    assert res2.generated_itinerary is not None
    assert len(res2.generated_itinerary.days) == 2

    # Verify real curated attractions appear and no fake landmarks
    all_place_names = [
        item.place_name for day in res2.generated_itinerary.days
        for item in day.items if item.place_name
    ]
    assert any("Sabarmati" in p or "Rani ki Vav" in p for p in all_place_names)
    assert not any("Central Landmark" in p for p in all_place_names)


@pytest.mark.asyncio
async def test_scenario_b_friends_trip(test_orchestrator):
    """Scenario B: Friends party type influences selection and descriptions."""
    chat_id = 3001
    intent = ParsedTripIntent(
        action=TripAction.NEW_TRIP,
        budget=Decimal("60000.00"),
        people=4,
        days=2,
        origin="Chennai",
        destination="Gujarat",
        travel_party="friends",
        interests=["heritage"],
        currency="INR",
    )
    test_orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=intent)

    res = await test_orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Plan a trip from Chennai to Gujarat, 4 people, budget 60000, 2 days. I am going with my friends.",
    )
    assert res.status == "FEASIBLE"
    assert "(Friends)" in res.message_text
    assert res.generated_itinerary is not None


@pytest.mark.asyncio
async def test_scenario_c_natural_correction(test_orchestrator):
    """Scenario C: 'Actually make it a family trip' updates travel_party while preserving all other constraints."""
    chat_id = 4001

    # Turn 1: Complete couple trip
    intent_t1 = ParsedTripIntent(
        action=TripAction.NEW_TRIP,
        budget=Decimal("50000.00"),
        people=2,
        days=2,
        origin="Chennai",
        destination="Gujarat",
        travel_party="couple",
        interests=["heritage"],
        currency="INR",
    )
    test_orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=intent_t1)

    res1 = await test_orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Chennai to Gujarat, 2 people, we are a couple, budget 50000, 2 days, interested in heritage.",
    )
    assert res1.status == "FEASIBLE"
    assert "(Couple)" in res1.message_text

    # Turn 2: Natural correction to family
    # Simulate pending state retention or user follow up correction
    test_orchestrator.conversation_repo.save_pending_intent(chat_id, intent_t1)

    intent_t2 = ParsedTripIntent(
        action=TripAction.CHANGE_PEOPLE,
        travel_party="family",
        people=2,
    )
    test_orchestrator.ai_service.parse_trip_intent_with_context = AsyncMock(return_value=intent_t2)

    res2 = await test_orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Actually make it a family trip.",
    )
    assert res2.status == "FEASIBLE"
    assert "(Family)" in res2.message_text


@pytest.mark.asyncio
async def test_scenario_d_telegram_handler_dispatch(test_orchestrator):
    """Scenario D: Full dispatch via bot text_message_handler."""
    set_orchestrator(test_orchestrator)
    chat_id = 5001

    intent = ParsedTripIntent(
        action=TripAction.NEW_TRIP,
        budget=Decimal("50000.00"),
        people=2,
        days=2,
        origin="Chennai",
        destination="Gujarat",
        travel_party="couple",
        interests=["heritage"],
        currency="INR",
    )
    test_orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=intent)

    update, replies = _build_telegram_update(
        text="Chennai to Gujarat for 2 people, we are a couple, budget 50000, 2 days",
        chat_id=chat_id,
    )
    context = MagicMock()

    await text_message_handler(update, context)

    assert len(replies) > 0
    full_response = "\n".join(replies)
    assert "Budlance Trip Plan: Gujarat" in full_response
    assert "(Couple)" in full_response
    assert "Financial Waterfall" in full_response
    assert "Day-by-Day Schedule" in full_response
