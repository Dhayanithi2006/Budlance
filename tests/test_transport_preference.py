"""Comprehensive tests for Transport Preference and Immediate Reverse-Budget Feasibility.

Covers:
- Group A: AI Intent extraction (train, 1AC train, first AC, 2AC, flight economy, etc.)
- Group B: Conversation state persistence across CHANGE_* and FIND_ALTERNATIVE
- Group C: Immediate Transport Feasibility check via ReverseBudgetEngine
- Group D: Transport change loop & stale data prevention (1AC -> Try 2AC)
- Group E: Zero booking fabrication (no fake seat, coach, berth, PNR)
- Group F: Cache/Fallback manager routing and SerpApi credential guard
- Group G: End-to-end simulations of Scenarios 1, 2, and 3 (Tasks 18, 19, 20)
"""

from decimal import Decimal
from unittest.mock import AsyncMock, patch
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
from budlance.engine.models import DataSource
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


# ==============================================================================
# GROUP A: Intent Extraction
# ==============================================================================

class TestGroupAIntentExtraction:
    """Verify natural language extraction of transport_mode and transport_class according to spec:

    - "train" -> mode=train, class=null
    - "1AC train" -> mode=train, class=1ac
    - "first AC" -> mode=train, class=1ac
    - "2AC" -> mode=null, class=2ac
    - "flight economy" -> mode=flight, class=economy
    - Do not infer unspecified class or mode when message is ambiguous.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "message, expected_mode, expected_class",
        [
            ("train", "train", None),
            ("I prefer train", "train", None),
            ("1AC train", "train", "1ac"),
            ("first AC", "train", "1ac"),
            ("Let's go by 1AC", "train", "1ac"),
            ("2AC", None, "2ac"),
            ("Try 2AC instead", None, "2ac"),
            ("flight economy", "flight", "economy"),
            ("business class flight", "flight", "business"),
            ("try sleeper", None, "sleeper"),
            ("3AC", None, "3ac"),
        ],
    )
    async def test_transport_intent_extraction(self, message, expected_mode, expected_class):
        service = AIIntentService(use_mock=True)
        intent = await service.parse_trip_intent(message)
        assert intent.transport_mode == expected_mode
        assert intent.transport_class == expected_class


# ==============================================================================
# GROUP B: Conversation State Persistence
# ==============================================================================

class TestGroupBConversationPersistence:
    """Verify transport preference survives all follow-ups, change actions, and find alternative."""

    def test_apply_change_action_preserves_transport(self):
        base = ParsedTripIntent(
            action=TripAction.NEW_TRIP,
            budget=Decimal("100000.00"),
            people=2,
            days=3,
            origin="Chennai",
            destination="Delhi",
            travel_party="couple",
            transport_mode="train",
            transport_class="1ac",
        )

        # CHANGE_BUDGET preserves transport
        updated = base.apply_change_action(
            ParsedTripIntent(action=TripAction.CHANGE_BUDGET, budget=Decimal("80000.00"))
        )
        assert updated.budget == Decimal("80000.00")
        assert updated.transport_mode == "train"
        assert updated.transport_class == "1ac"

        # CHANGE_DAYS preserves transport
        updated = base.apply_change_action(
            ParsedTripIntent(action=TripAction.CHANGE_DAYS, days=4)
        )
        assert updated.days == 4
        assert updated.transport_mode == "train"
        assert updated.transport_class == "1ac"

        # CHANGE_PEOPLE preserves transport
        updated = base.apply_change_action(
            ParsedTripIntent(action=TripAction.CHANGE_PEOPLE, people=3)
        )
        assert updated.people == 3
        assert updated.transport_mode == "train"
        assert updated.transport_class == "1ac"

        # CHANGE_DESTINATION preserves transport
        updated = base.apply_change_action(
            ParsedTripIntent(action=TripAction.CHANGE_DESTINATION, destination="Mumbai")
        )
        assert updated.destination == "Mumbai"
        assert updated.transport_mode == "train"
        assert updated.transport_class == "1ac"

    def test_find_alternative_preserves_transport_clears_destination(self):
        base = ParsedTripIntent(
            action=TripAction.NEW_TRIP,
            budget=Decimal("50000.00"),
            people=2,
            days=3,
            origin="Chennai",
            destination="Delhi",
            travel_party="couple",
            transport_mode="train",
            transport_class="2ac",
        )
        updated = base.apply_change_action(
            ParsedTripIntent(action=TripAction.FIND_ALTERNATIVE)
        )
        assert updated.destination is None  # cleared to trigger discovery
        assert updated.origin == "Chennai"
        assert updated.budget == Decimal("50000.00")
        assert updated.people == 2
        assert updated.days == 3
        assert updated.transport_mode == "train"
        assert updated.transport_class == "2ac"

    def test_change_transport_updates_only_transport(self):
        base = ParsedTripIntent(
            action=TripAction.NEW_TRIP,
            budget=Decimal("100000.00"),
            people=2,
            days=3,
            origin="Chennai",
            destination="Delhi",
            travel_party="couple",
            transport_mode="train",
            transport_class="1ac",
        )
        updated = base.apply_change_action(
            ParsedTripIntent(
                action=TripAction.CHANGE_TRANSPORT,
                transport_mode="train",
                transport_class="2ac",
            )
        )
        assert updated.transport_class == "2ac"
        assert updated.transport_mode == "train"
        assert updated.budget == Decimal("100000.00")
        assert updated.destination == "Delhi"
        assert updated.origin == "Chennai"
        assert updated.people == 2
        assert updated.days == 3


# ==============================================================================
# GROUP C & D: Immediate Feasibility, Change Loop, & Stale Data Prevention
# ==============================================================================

class TestGroupCTransportFeasibilityAndChangeLoop:
    """Verify immediate reverse-budget feasibility and prevention of stale data on preference change."""

    @pytest.mark.asyncio
    async def test_immediate_transport_feasibility_check(self, orchestrator):
        """1AC lookup -> cost calculation -> ReverseBudgetEngine evaluation."""
        chat_id = 1001
        intent = ParsedTripIntent(
            action=TripAction.NEW_TRIP,
            budget=Decimal("100000.00"),
            people=2,
            days=3,
            origin="Chennai",
            destination="Delhi",
            travel_party="couple",
            transport_mode="train",
            transport_class="1ac",
            currency="INR",
        )
        orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=intent)

        result = await orchestrator.handle_user_message(
            telegram_user_id=chat_id,
            chat_id=chat_id,
            message="Chennai to Delhi, 2 people, couple, 3 days, budget 100000, 1AC train",
        )

        assert result.status == "FEASIBLE_TRANSPORT"
        assert result.selected_transport is not None
        # 1AC for 2 people on Chennai-Delhi corridor round-trip: (4850 + 4850) * 2 = 19400.00
        assert result.selected_transport.price == Decimal("19400.00")
        assert "fits the current trip budget" in result.message_text
        assert "irctc.co.in" in result.message_text

    @pytest.mark.asyncio
    async def test_stale_data_prevention_on_preference_change(self, orchestrator):
        """User tests 1AC (infeasible under tight budget) -> tries 2AC -> fresh lookup (price changes)."""
        chat_id = 1002

        # Step 1: User requests 1AC with small budget (15000) for 2 people, 3 days
        intent_1ac = ParsedTripIntent(
            action=TripAction.NEW_TRIP,
            budget=Decimal("15000.00"),
            people=2,
            days=3,
            origin="Chennai",
            destination="Delhi",
            travel_party="couple",
            transport_mode="train",
            transport_class="1ac",
            currency="INR",
        )
        orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=intent_1ac)

        res1 = await orchestrator.handle_user_message(
            telegram_user_id=chat_id,
            chat_id=chat_id,
            message="Chennai to Delhi, 2 people, couple, 3 days, budget 15000, 1AC train",
        )

        assert res1.status == "NOT_FEASIBLE"
        assert res1.selected_transport.price == Decimal("19400.00")  # 1AC round-trip price
        assert "leaves insufficient room" in res1.message_text
        assert "2AC" in res1.message_text or "3AC" in res1.message_text

        # Step 2: User says "Try 2AC"
        intent_2ac = ParsedTripIntent(
            action=TripAction.CHANGE_TRANSPORT,
            transport_mode="train",
            transport_class="2ac",
        )
        orchestrator.ai_service.parse_trip_intent_with_context = AsyncMock(return_value=intent_2ac)

        res2 = await orchestrator.handle_user_message(
            telegram_user_id=chat_id,
            chat_id=chat_id,
            message="Try 2AC",
        )

        # Ensure FRESH lookup occurred: price must match 2AC round-trip ((2850 + 2850) * 2 = 11400.00), NOT 19400.00
        assert res2.selected_transport is not None
        assert res2.selected_transport.price == Decimal("11400.00")
        assert res2.selected_transport.price != Decimal("19400.00")  # PROVES STALE DATA WAS NOT REUSED
        assert res2.selected_transport.class_or_type in ("2A", "2AC")


# ==============================================================================
# GROUP E: Zero Booking Fabrication
# ==============================================================================

class TestGroupEZeroBookingFabrication:
    """Verify application never invents seat numbers, coach numbers, berths, or PNRs."""

    @pytest.mark.asyncio
    async def test_no_fabricated_booking_fields(self, orchestrator):
        chat_id = 1003
        intent = ParsedTripIntent(
            action=TripAction.NEW_TRIP,
            budget=Decimal("100000.00"),
            people=2,
            days=3,
            origin="Chennai",
            destination="Delhi",
            travel_party="couple",
            transport_mode="train",
            transport_class="1ac",
            currency="INR",
        )
        orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=intent)

        result = await orchestrator.handle_user_message(
            telegram_user_id=chat_id,
            chat_id=chat_id,
            message="Chennai to Delhi, 2 people, 1AC train, budget 100000, 3 days",
        )

        msg = result.message_text.lower()
        forbidden_hallucinations = ["seat no", "coach no", "berth", "pnr", "ticket confirmed", "seat assigned"]
        for forbidden in forbidden_hallucinations:
            assert forbidden not in msg, f"Found forbidden hallucination: {forbidden} in message: {msg}"


# ==============================================================================
# GROUP F: Cache / Fallback Flow & SerpApi Guard
# ==============================================================================

class TestGroupFCacheFallbackAndSerpApiGuard:
    """Verify all transport queries pass through CacheFallbackManager and SerpApi remains disabled."""

    @pytest.mark.asyncio
    async def test_transport_lookup_uses_cache_fallback_no_serpapi(self, orchestrator):
        with patch.object(
            orchestrator.cache_manager,
            "get_travel_data",
            wraps=orchestrator.cache_manager.get_travel_data,
        ) as mock_cache_get, patch(
            "budlance.serpapi.gateway.SerpApiGateway.execute_search"
        ) as mock_serp:
            options = await orchestrator.lookup_transport_options(
                origin="Chennai",
                destination="Delhi",
                people=2,
                transport_mode="train",
                transport_class="1ac",
            )
            assert len(options) > 0
            assert mock_cache_get.called
            mock_serp.assert_not_called()


# ==============================================================================
# GROUP G: Manual Telegram Scenarios 1, 2, 3 (Tasks 18, 19, 20)
# ==============================================================================

class TestGroupGManualScenarios:
    """Automated simulations of Manual Telegram Scenarios 1, 2, and 3."""

    @pytest.mark.asyncio
    async def test_scenario_1_task_18_clarification_and_immediate_evaluation(self, orchestrator):
        """Scenario 1 (Task 18):
        User requests trip with interests -> bot asks for missing transport -> user specifies 1AC -> immediate evaluation without booking.
        """
        chat_id = 2001

        # Turn 1: User provides complete trip info, missing transport
        t1_intent = ParsedTripIntent(
            action=TripAction.NEW_TRIP,
            origin="Chennai",
            destination="Delhi",
            budget=Decimal("100000.00"),
            people=2,
            travel_party="couple",
            days=3,
            interests=["sunrise", "India Gate", "nearby famous places"],
            currency="INR",
        )
        orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=t1_intent)

        res1 = await orchestrator.handle_user_message(
            telegram_user_id=chat_id,
            chat_id=chat_id,
            message="I would like to go to Delhi from Chennai, budget 100000, 2 people, we are a couple, 3 days, I want sunrise, India Gate and nearby famous places.",
        )

        assert res1.status == "CLARIFICATION"
        assert "How would you like to travel — train or flight?" in res1.message_text

        # Turn 2: User says "1AC train"
        t2_intent = ParsedTripIntent(
            action=TripAction.CHANGE_TRANSPORT,
            transport_mode="train",
            transport_class="1ac",
        )
        orchestrator.ai_service.parse_trip_intent_with_context = AsyncMock(return_value=t2_intent)

        res2 = await orchestrator.handle_user_message(
            telegram_user_id=chat_id,
            chat_id=chat_id,
            message="1AC train",
        )

        assert res2.status == "FEASIBLE_TRANSPORT"
        assert res2.selected_transport is not None
        assert res2.selected_transport.price == Decimal("19400.00")
        assert "fits the current trip budget" in res2.message_text
        assert "https://www.irctc.co.in" in res2.message_text
        # Proves it did NOT book seat
        assert "pnr" not in res2.message_text.lower()
        assert "seat" not in res2.message_text.lower()

    @pytest.mark.asyncio
    async def test_scenario_2_task_19_infeasible_and_correction_loop(self, orchestrator):
        """Scenario 2 (Task 19):
        User asks for 1AC with budget 15000 -> infeasible -> suggests 2AC/3AC -> user corrects with 'Try 2AC' -> new evaluation.
        """
        chat_id = 2002

        # Turn 1: 1AC with budget 15000
        t1_intent = ParsedTripIntent(
            action=TripAction.NEW_TRIP,
            origin="Chennai",
            destination="Delhi",
            budget=Decimal("15000.00"),
            people=2,
            travel_party="couple",
            days=3,
            transport_mode="train",
            transport_class="1ac",
            currency="INR",
        )
        orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=t1_intent)

        res1 = await orchestrator.handle_user_message(
            telegram_user_id=chat_id,
            chat_id=chat_id,
            message="Chennai to Delhi, 2 people, couple, 3 days, budget 15000, I want 1AC.",
        )

        assert res1.status == "NOT_FEASIBLE"
        assert "leaves insufficient room" in res1.message_text
        assert "2AC" in res1.message_text

        # Turn 2: User corrects preference: "Try 2AC"
        t2_intent = ParsedTripIntent(
            action=TripAction.CHANGE_TRANSPORT,
            transport_mode="train",
            transport_class="2ac",
        )
        orchestrator.ai_service.parse_trip_intent_with_context = AsyncMock(return_value=t2_intent)

        res2 = await orchestrator.handle_user_message(
            telegram_user_id=chat_id,
            chat_id=chat_id,
            message="Try 2AC",
        )

        # New lookup was performed
        assert res2.selected_transport.price == Decimal("11400.00")
        assert res2.selected_transport.price != Decimal("19400.00")

    @pytest.mark.asyncio
    async def test_scenario_3_task_20_successful_end_to_end_flow(self, orchestrator):
        """Scenario 3 (Task 20):
        Turn 1: User asks trip -> bot asks transport
        Turn 2: User says 'Train, 2AC.' -> bot evaluates and gives IRCTC handoff link
        Turn 3: User says 'Booked.' -> bot confirms booking and transitions to full planning pipeline
        """
        chat_id = 2003

        # Turn 1:
        t1_intent = ParsedTripIntent(
            action=TripAction.NEW_TRIP,
            origin="Chennai",
            destination="Delhi",
            budget=Decimal("100000.00"),
            people=2,
            travel_party="couple",
            days=3,
            interests=["sunrise", "India Gate", "nearby famous places"],
            currency="INR",
        )
        orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=t1_intent)

        res1 = await orchestrator.handle_user_message(
            telegram_user_id=chat_id,
            chat_id=chat_id,
            message="I want to go to Delhi from Chennai, ₹100000, 2 people, we are a couple, 3 days, interested in sunrise, India Gate and nearby famous places.",
        )
        assert res1.status == "CLARIFICATION"
        assert "How would you like to travel — train or flight?" in res1.message_text

        # Turn 2:
        t2_intent = ParsedTripIntent(
            action=TripAction.CHANGE_TRANSPORT,
            transport_mode="train",
            transport_class="2ac",
        )
        orchestrator.ai_service.parse_trip_intent_with_context = AsyncMock(return_value=t2_intent)

        res2 = await orchestrator.handle_user_message(
            telegram_user_id=chat_id,
            chat_id=chat_id,
            message="Train, 2AC.",
        )
        assert res2.status == "FEASIBLE_TRANSPORT"
        assert "https://www.irctc.co.in" in res2.message_text

        # Turn 3: User returns: "Booked."
        t3_intent = ParsedTripIntent(
            action=TripAction.CONFIRM_BOOKING,
            booking_confirmed=True,
        )
        orchestrator.ai_service.parse_trip_intent_with_context = AsyncMock(return_value=t3_intent)

        res3 = await orchestrator.handle_user_message(
            telegram_user_id=chat_id,
            chat_id=chat_id,
            message="Booked.",
        )

        assert res3.status == "FEASIBLE"
        assert res3.generated_itinerary is not None
        assert len(res3.generated_itinerary.days) == 3
        assert res3.budget_breakdown is not None
        assert res3.ledger_summary is not None

        # Verify no fake seats or PNRs
        msg = res3.message_text.lower()
        for forbidden in ["seat no", "coach no", "pnr"]:
            assert forbidden not in msg
