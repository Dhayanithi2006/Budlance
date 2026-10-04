"""End-to-End Orchestrator Pipeline Tests (Task 10).

Verifies the full pipeline integration:
1. Real curated attraction selection for Gujarat with travel_party filtering.
2. Mode B structured free time for destinations without curated data (zero fake landmarks).
3. SerpApi guard: SerpApi is never invoked when credentials are absent.
4. AI Call Invariant: exactly 1 intent call + 1 batched enhancement call (max 2 AI calls per message).
5. State preservation: multi-turn clarification preserves travel_party end-to-end.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
import pytest

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
from budlance.itinerary.enhancer import ItineraryEnhancer
from budlance.itinerary.generator import ItineraryGenerator
from budlance.ledger.manager import VirtualLedgerManager
from budlance.normalization.normalizer import DataNormalizer
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.rescue.service import RescueService


@pytest.fixture
def orchestrator():
    """Build an orchestrator with in-memory repositories and mock AI/cache services."""
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
    itinerary_enhancer = ItineraryEnhancer(use_mock=True)
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
        itinerary_enhancer=itinerary_enhancer,
        ledger_manager=ledger_manager,
        rescue_service=rescue_service,
    )


@pytest.mark.asyncio
async def test_end_to_end_gujarat_family_itinerary(orchestrator):
    """Test full pipeline for Gujarat trip with travel_party='family'."""
    # Mock AI response with explicit travel_party="family"
    intent = ParsedTripIntent(
        action=TripAction.NEW_TRIP,
        budget=Decimal("60000.00"),
        people=4,
        days=2,
        origin="Chennai",
        destination="Gujarat",
        travel_party="family",
        interests=["heritage"],
        currency="INR",
    )
    orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=intent)

    result = await orchestrator.handle_user_message(
        telegram_user_id=12345,
        chat_id=12345,
        message="Chennai to Gujarat, 4 people, family trip, budget 60000, 2 days, heritage",
    )

    assert result.status == "FEASIBLE"
    assert result.selected_destination == "Gujarat"
    assert result.generated_itinerary is not None
    assert result.generated_itinerary.is_feasible is True
    assert len(result.generated_itinerary.days) == 2

    # Check that real curated attractions suitable for family were selected
    all_attraction_items = [
        item for day in result.generated_itinerary.days
        for item in day.items if item.is_curated
    ]
    assert len(all_attraction_items) > 0

    curated_names = [item.attraction_name for item in all_attraction_items]
    # Sabarmati Ashram is suitable for family and heritage in gujarat.json
    assert any("Sabarmati Ashram" in name for name in curated_names)

    # Check attraction cost is in breakdown
    assert result.budget_breakdown is not None
    assert result.budget_breakdown.attraction_cost > Decimal("0.00")

    # Check descriptions are enhanced and tailored
    for day in result.generated_itinerary.days:
        for item in day.items:
            assert len(item.description) > 0
            # Descriptions should be informative and strictly fewer than 20 words
            words = item.description.split()
            assert 1 <= len(words) < 20


@pytest.mark.asyncio
async def test_end_to_end_mode_b_no_curated_data_zero_fake_landmarks(orchestrator):
    """Test pipeline for destination without curated data allocates Mode B structured free time."""
    intent = ParsedTripIntent(
        action=TripAction.NEW_TRIP,
        budget=Decimal("40000.00"),
        people=2,
        days=2,
        origin="Chennai",
        destination="Delhi",
        travel_party="couple",
        currency="INR",
    )
    orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=intent)

    result = await orchestrator.handle_user_message(
        telegram_user_id=54321,
        chat_id=54321,
        message="Chennai to Delhi, 2 people, couple, budget 40000, 2 days",
    )

    assert result.status == "FEASIBLE"
    assert result.selected_destination == "Delhi"
    assert result.generated_itinerary is not None

    # Check ZERO fake landmarks
    all_items = [
        item for day in result.generated_itinerary.days
        for item in day.items
    ]
    for item in all_items:
        assert "Central Landmark" not in (item.place_name or "")
        assert "Heritage Palace" not in (item.place_name or "")
        assert "Old Town" not in (item.place_name or "")
        assert "Local Market" not in (item.place_name or "")
        assert "Main Square" not in (item.place_name or "")
        # Slots should be free_time or food
        assert item.category in ("free_time", "food")


@pytest.mark.asyncio
async def test_end_to_end_serpapi_is_not_called(orchestrator):
    """Verify SerpApi client is completely bypassed during orchestration."""
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
    orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=intent)

    with patch("budlance.serpapi.gateway.SerpApiGateway.execute_search") as mock_serp:
        result = await orchestrator.handle_user_message(
            telegram_user_id=11111,
            chat_id=11111,
            message="Chennai to Gujarat, 2 people, we are a couple, budget 50000, 2 days",
        )
        assert result.status == "FEASIBLE"
        # SerpApi should never be called when credentials are off
        mock_serp.assert_not_called()


@pytest.mark.asyncio
async def test_end_to_end_ai_call_budget_invariant(orchestrator):
    """Verify exactly 1 intent AI call and 1 batched enhancer call per plannable message."""
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

    intent_call_count = 0
    enhancer_call_count = 0

    async def mock_parse(*args, **kwargs):
        nonlocal intent_call_count
        intent_call_count += 1
        return intent

    async def mock_enhance(itinerary, travel_party=None):
        nonlocal enhancer_call_count
        enhancer_call_count += 1
        return itinerary

    orchestrator.ai_service.parse_trip_intent = mock_parse
    orchestrator.itinerary_enhancer.enhance_itinerary = mock_enhance

    result = await orchestrator.handle_user_message(
        telegram_user_id=22222,
        chat_id=22222,
        message="Chennai to Gujarat, 2 people, couple, budget 50000, 2 days",
    )

    assert result.status == "FEASIBLE"
    assert intent_call_count == 1
    assert enhancer_call_count == 1
    # Total AI calls across the message = 2
    assert intent_call_count + enhancer_call_count == 2


@pytest.mark.asyncio
async def test_end_to_end_clarification_preserves_travel_party(orchestrator):
    """Multi-turn test: missing days clarification preserves travel_party for next turn."""
    chat_id = 99999

    # Turn 1: Message missing duration (days)
    turn1_intent = ParsedTripIntent(
        action=TripAction.NEW_TRIP,
        budget=Decimal("50000.00"),
        people=2,
        days=None,
        origin="Chennai",
        destination="Gujarat",
        travel_party="couple",
        interests=["heritage"],
        currency="INR",
    )
    orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=turn1_intent)

    turn1_res = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Chennai to Gujarat, 2 people, we are a couple, budget 50000, heritage",
    )

    assert turn1_res.status == "CLARIFICATION"
    pending = orchestrator.conversation_repo.get_pending_intent(chat_id)
    assert pending is not None
    assert pending.travel_party == "couple"
    assert pending.budget == Decimal("50000.00")
    assert pending.people == 2

    # Turn 2: User provides missing days
    turn2_intent = ParsedTripIntent(
        action=TripAction.CHANGE_DAYS,
        days=2,
    )
    orchestrator.ai_service.parse_trip_intent_with_context = AsyncMock(return_value=turn2_intent)

    turn2_res = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="2 days",
    )

    assert turn2_res.status == "FEASIBLE"
    assert turn2_res.selected_destination == "Gujarat"
    assert turn2_res.generated_itinerary is not None
    assert len(turn2_res.generated_itinerary.days) == 2

    # Pending intent should now be cleared
    assert orchestrator.conversation_repo.get_pending_intent(chat_id) is None
