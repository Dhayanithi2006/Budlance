"""End-to-end integration and orchestrator wiring tests for Phase 11.

Verifies all Task O and Task P criteria:
1. Complete feasible trip.
2. Destination discovery.
3. Incomplete request with clarification.
4. Over-budget -> optimizer -> feasible.
5. All optimizer attempts fail -> NOT_FEASIBLE without creating itinerary.
6. Rescue weather/closure via orchestrator.
7. Rescue price dispute via orchestrator.
8. Rescue with no active trip handled safely.
9. External API failure handled gracefully.
10. Persistence order and database coherence.
11. Telegram handler integration.
12. SerpApi material contribution to budget decision.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
import pytest
from telegram import Chat, Message, Update, User

from budlance.ai.service import AIIntentService
from budlance.bot.handlers import set_orchestrator, text_message_handler
from budlance.cache.manager import CacheFallbackManager
from budlance.db.models import BudgetAllocation, Itinerary, LedgerEntry, Trip, utc_now
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
from budlance.itinerary.models import GeneratedItinerary, ItineraryDay, ItineraryItem
from budlance.ledger.manager import VirtualLedgerManager
from budlance.normalization.normalizer import DataNormalizer
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.rescue.service import RescueService
from budlance.schemas.travel import FlightOption, HotelOption, PlaceOption, RouteOption
from budlance.serpapi.models import DataSource, TravelDataEnvelope


@pytest.fixture
def mock_cache_manager():
    """Deterministic CacheFallbackManager mock returning standard envelopes."""
    mgr = MagicMock(spec=CacheFallbackManager)

    async def _mock_get_travel_data(engine, params, trip_id=None, **kwargs):
        if engine == "google_travel_explore":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="explore_hash",
                data={
                    "destinations": [
                        {"destination": "Goa", "city": "Goa"},
                        {"destination": "Jaipur", "city": "Jaipur"},
                    ]
                },
                is_fallback=False,
            )
        if engine == "google_flights":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="flights_hash",
                data={
                    "best_flights": [
                        {
                            "flights": [{"airline": "IndiGo", "flight_number": "6E-201"}],
                            "price": 3000,
                        }
                    ]
                },
                is_fallback=False,
            )
        if engine == "google_hotels":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="hotels_hash",
                data={
                    "properties": [
                        {
                            "name": "Goa Bay Resort",
                            "hotel_class": 4,
                            "rate_per_night": {"extracted_lowest": 2000},
                            "total_rate": {"extracted_lowest": 6000},
                        },
                        {
                            "name": "Goa Budget Inn",
                            "hotel_class": 3,
                            "rate_per_night": {"extracted_lowest": 1000},
                            "total_rate": {"extracted_lowest": 3000},
                        },
                    ]
                },
                is_fallback=False,
            )
        if engine == "google_maps":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="maps_hash",
                data={
                    "local_results": [
                        {"title": "Calangute Beach", "type": "Beach", "rating": 4.5},
                        {"title": "Museum of Christian Art", "type": "Museum", "rating": 4.6},
                    ]
                },
                is_fallback=False,
            )
        if engine == "google_maps_directions":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="directions_hash",
                data={
                    "routes": [
                        {
                            "summary": "NH 66",
                            "legs": [
                                {
                                    "distance": {"text": "550 km", "value": 550000},
                                    "duration": {"text": "10 hours", "value": 36000},
                                }
                            ],
                        }
                    ]
                },
                is_fallback=False,
            )
        # Default empty fallback
        return TravelDataEnvelope(
            source=DataSource.FALLBACK,
            engine=engine,
            query_hash="empty_hash",
            data={},
            is_fallback=True,
        )

    mgr.get_travel_data = AsyncMock(side_effect=_mock_get_travel_data)
    return mgr


@pytest.fixture
def orchestrator_fixture(mock_cache_manager):
    """Instantiate a fully wired BudlanceOrchestrator with in-memory stores."""
    user_repo = UserRepository(client=None)
    trip_repo = TripRepository(client=None)
    intent_repo = IntentRepository(client=None)
    itinerary_repo = ItineraryRepository(client=None)
    ledger_repo = LedgerRepository(client=None)
    rescue_repo = RescueRepository(client=None)

    ai_service = AIIntentService()
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
        cache_manager=mock_cache_manager,
        normalizer=normalizer,
        budget_engine=budget_engine,
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
        ai_service=ai_service,
        cache_manager=mock_cache_manager,
        normalizer=normalizer,
        estimation_layer=estimation,
        budget_engine=budget_engine,
        optimizer=optimizer,
        itinerary_generator=itin_gen,
        ledger_manager=ledger_mgr,
        rescue_service=rescue_service,
    )
    return orch


# ============================================================================
# Tests
# ============================================================================

@pytest.mark.asyncio
async def test_complete_feasible_trip(orchestrator_fixture):
    """1. Complete feasible trip request through integrated pipeline."""
    result = await orchestrator_fixture.handle_user_message(
        telegram_user_id=1001,
        chat_id=5001,
        message="Plan a trip from Mumbai to Goa for 2 people, 3 days, with budget ₹25,000",
        username="john_traveler",
        first_name="John",
    )

    assert result.status == "FEASIBLE"
    assert result.trip_id is not None
    assert result.selected_destination == "Goa"
    assert result.generated_itinerary is not None
    assert len(result.generated_itinerary.days) == 3
    assert result.ledger_summary is not None
    assert result.ledger_summary.total_budget == Decimal("25000.00")
    assert result.ledger_summary.total_allocated <= Decimal("25000.00")
    assert "Budlance Trip Plan: Goa" in result.message_text
    assert "Financial Waterfall" in result.message_text


@pytest.mark.asyncio
async def test_destination_discovery(orchestrator_fixture):
    """2. Destination-less request triggers Travel Explore discovery."""
    result = await orchestrator_fixture.handle_user_message(
        telegram_user_id=1002,
        chat_id=5002,
        message="Plan a trip from Mumbai for 2 people for 3 days with budget ₹25,000",
    )

    assert result.status == "FEASIBLE"
    assert result.selected_destination in ["Goa", "Jaipur"]
    assert result.trip_id is not None
    assert result.generated_itinerary is not None


@pytest.mark.asyncio
async def test_incomplete_request_produces_clarification(orchestrator_fixture):
    """3. Incomplete request missing mandatory inputs asks for clarification."""
    result = await orchestrator_fixture.handle_user_message(
        telegram_user_id=1003,
        chat_id=5003,
        message="Plan a trip to Goa with ₹20,000",
    )

    assert result.status == "CLARIFICATION"
    assert result.trip_id is None
    assert "I need a few more details" in result.message_text
    assert "travelers" in result.message_text.lower() or "people" in result.message_text.lower()


@pytest.mark.asyncio
async def test_over_budget_triggers_optimizer_to_feasible(orchestrator_fixture):
    """4. Over-budget plan triggers 4-step optimizer to reach feasibility."""
    # ₹14,000 budget for 2 people for 3 days.
    # Flights = 6,000. 4-star Hotel = 6,000. Food = ~3,000. Fixed + survival = 15,000 + 1,400 reserve > 14,000.
    # Optimizer step 1 downgrades hotel from 6,000 to 3,000 (Goa Budget Inn).
    # Then mandatory costs = 6,000 + 3,000 + 3,000 + 1,400 = 13,400 <= 14,000 -> FEASIBLE!
    result = await orchestrator_fixture.handle_user_message(
        telegram_user_id=1004,
        chat_id=5004,
        message="Plan a trip from Mumbai to Goa for 2 people, 3 days, with budget ₹14,000",
    )

    assert result.status == "FEASIBLE"
    assert result.optimization_attempts >= 1
    assert len(result.downgrades_applied) >= 1
    assert "Goa Budget Inn" in result.selected_hotel.name
    assert result.generated_itinerary is not None
    assert result.ledger_summary.total_allocated <= Decimal("14000.00")


@pytest.mark.asyncio
async def test_all_optimization_attempts_fail(orchestrator_fixture):
    """5. All 4 optimizer attempts fail on impossible budget -> NOT_FEASIBLE."""
    # Impossible budget of ₹2,000 for 2 people for 5 days.
    result = await orchestrator_fixture.handle_user_message(
        telegram_user_id=1005,
        chat_id=5005,
        message="Plan a trip from Mumbai to Goa for 2 people, 5 days, with budget ₹2,000",
    )

    assert result.status == "NOT_FEASIBLE"
    assert result.feasibility_status == "NOT_FEASIBLE"
    assert result.generated_itinerary is None
    assert result.ledger_summary is None
    assert "Not Feasible within Budget" in result.message_text
    assert "Deficit" in result.message_text


@pytest.mark.asyncio
async def test_rescue_weather_closure_routed(orchestrator_fixture):
    """6. Weather/closure rescue message routes to RescueService."""
    # First create an active trip
    plan_res = await orchestrator_fixture.handle_user_message(
        telegram_user_id=1006,
        chat_id=5006,
        message="Plan a trip from Mumbai to Goa for 2 people, 3 days, with budget ₹25,000",
    )
    assert plan_res.status == "FEASIBLE"

    # Send weather disruption message
    rescue_msg = await orchestrator_fixture.handle_user_message(
        telegram_user_id=1006,
        chat_id=5006,
        message="It is storming and heavy rain at Calangute Beach",
    )

    assert rescue_msg.status == "RESCUE"
    assert "Rescue Mode: Alternative Found" in rescue_msg.message_text


@pytest.mark.asyncio
async def test_rescue_price_dispute_routed(orchestrator_fixture):
    """7. Price dispute message routes to RescueService without SerpApi."""
    # Create an active trip
    await orchestrator_fixture.handle_user_message(
        telegram_user_id=1007,
        chat_id=5007,
        message="Plan a trip from Mumbai to Goa for 2 people, 3 days, with budget ₹25,000",
    )

    # Report price dispute
    dispute_msg = await orchestrator_fixture.handle_user_message(
        telegram_user_id=1007,
        chat_id=5007,
        message="The auto driver is asking ₹600",
    )

    assert dispute_msg.status == "RESCUE"
    assert "Advisory Transit Fare Guidance" in dispute_msg.message_text
    assert "600" in dispute_msg.message_text


@pytest.mark.asyncio
async def test_rescue_no_active_trip(orchestrator_fixture):
    """8. Rescue message with no active trip returns helpful guidance."""
    result = await orchestrator_fixture.handle_user_message(
        telegram_user_id=9999,
        chat_id=9999,
        message="The auto driver is charging ₹500",
    )

    assert result.status == "RESCUE"
    assert "No Active Trip Found" in result.message_text


@pytest.mark.asyncio
async def test_external_api_failure_handled_gracefully(orchestrator_fixture, mock_cache_manager):
    """9. External API failure is caught and returns clean error message."""
    mock_cache_manager.get_travel_data = AsyncMock(side_effect=RuntimeError("SerpApi upstream unreachable"))

    result = await orchestrator_fixture.handle_user_message(
        telegram_user_id=1008,
        chat_id=5008,
        message="Plan a trip from Mumbai to Goa for 2 people, 3 days, with budget ₹25,000",
    )

    assert result.status == "ERROR"
    assert "error occurred" in result.message_text.lower()
    # Stack trace is not leaked
    assert "RuntimeError" not in result.message_text


@pytest.mark.asyncio
async def test_persistence_coherent_order(orchestrator_fixture):
    """10. Coherent persistence sequence: User -> Trip -> Intent -> Itinerary -> Ledger."""
    result = await orchestrator_fixture.handle_user_message(
        telegram_user_id=1009,
        chat_id=5009,
        message="Plan a trip from Mumbai to Goa for 2 people, 3 days, with budget ₹25,000",
        username="coherence_user",
        first_name="Alice",
    )

    trip_id = result.trip_id
    assert trip_id is not None

    # Verify all linked entities exist and share trip_id
    trip = orchestrator_fixture.trip_repo.get_trip(trip_id)
    assert trip is not None
    assert trip.telegram_chat_id == 5009

    intent = orchestrator_fixture.intent_repo.get_trip_intent(trip_id)
    assert intent is not None
    assert intent.trip_id == trip_id
    assert intent.destination == "Goa"

    itin = orchestrator_fixture.itinerary_repo.get_itinerary(trip_id)
    assert itin is not None
    assert itin.trip_id == trip_id

    alloc = orchestrator_fixture.ledger_repo.get_budget_allocation(trip_id)
    assert alloc is not None
    assert alloc.trip_id == trip_id

    entries = orchestrator_fixture.ledger_repo.get_ledger_entries(trip_id)
    assert len(entries) > 0
    assert all(e.trip_id == trip_id for e in entries)


@pytest.mark.asyncio
async def test_telegram_handler_wiring(orchestrator_fixture):
    """11. Telegram text_message_handler invokes orchestrator and replies."""
    set_orchestrator(orchestrator_fixture)

    update = MagicMock(spec=Update)
    message = MagicMock(spec=Message)
    message.text = "Plan a trip from Mumbai to Goa for 2 people, 3 days, with budget ₹25,000"
    message.reply_text = AsyncMock()
    update.effective_message = message
    update.effective_chat = MagicMock(spec=Chat, id=5010)
    update.effective_user = MagicMock(spec=User, id=1010, username="tele_user", first_name="Bob")
    context = MagicMock()

    await text_message_handler(update, context)

    assert message.reply_text.called
    reply_content = message.reply_text.call_args[0][0]
    assert "Budlance Trip Plan: Goa" in reply_content


@pytest.mark.asyncio
async def test_serpapi_material_contribution(orchestrator_fixture, mock_cache_manager):
    """12. SerpApi prices materially affect selected plan costs and feasibility decisions."""
    # When SerpApi returns live flights at ₹7,500 and hotel at ₹4,000:
    # 1. Budget of ₹8,000 cannot afford the live costs (₹7,500 + ₹4,000 > ₹8,000) -> NOT_FEASIBLE
    async def _live_pricing(engine, params, trip_id=None, **kwargs):
        if engine == "google_flights":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="live_flight_hash",
                data={
                    "best_flights": [
                        {"flights": [{"airline": "Air India Express"}], "price": 7500}
                    ]
                },
                is_fallback=False,
            )
        if engine == "google_hotels":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="live_hotel_hash",
                data={
                    "properties": [
                        {"name": "Live Grand Hotel", "total_rate": {"extracted_lowest": 4000}}
                    ]
                },
                is_fallback=False,
            )
        return TravelDataEnvelope(
            source=DataSource.LIVE,
            engine=engine,
            query_hash="hash",
            data={"local_results": [{"title": "Sunset Beach"}]},
            is_fallback=False,
        )

    mock_cache_manager.get_travel_data = AsyncMock(side_effect=_live_pricing)

    # Tight budget: ₹4,000 cannot support live costs even after optimizer
    result_tight = await orchestrator_fixture.handle_user_message(
        telegram_user_id=1011,
        chat_id=5011,
        message="Plan a trip from Delhi to Jaipur for 2 people, 3 days, with budget ₹4,000",
    )
    assert result_tight.status == "NOT_FEASIBLE"
    assert result_tight.generated_itinerary is None

    # Adequate budget: ₹25,000 accepts the live prices
    result_adequate = await orchestrator_fixture.handle_user_message(
        telegram_user_id=1012,
        chat_id=5012,
        message="Plan a trip from Delhi to Jaipur for 2 people, 3 days, with budget ₹25,000",
    )
    assert result_adequate.status == "FEASIBLE"
    # Material contribution: Live price from SerpApi is the exact figure in the budget breakdown
    assert result_adequate.selected_transport.price == Decimal("7500")
    assert result_adequate.selected_hotel.total_price == Decimal("4000")
    assert result_adequate.budget_breakdown.transport_cost == Decimal("7500")
    assert result_adequate.budget_breakdown.hotel_cost == Decimal("4000")
    assert result_adequate.selected_transport.source == DataSource.LIVE
    assert result_adequate.selected_hotel.source == DataSource.LIVE

