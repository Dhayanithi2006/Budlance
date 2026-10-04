"""Regression tests for attraction discovery ordering.

Architecture requirement:
candidate destination
    ↓
transport/stay/destination-level feasibility
    ↓
feasible destination selected
    ↓
Attraction data request through CacheFallbackManager
    ↓
AttractionSelector
    ↓
Itinerary

Verifies:
1. An infeasible candidate does not trigger live attraction discovery.
2. A feasible selected destination does trigger attraction discovery.
3. Attraction requests still go through CacheFallbackManager.
4. Existing curated offline attraction behavior remains unchanged.
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
    """Build an orchestrator with in-memory repositories and mock services."""
    user_repo = UserRepository(client=None)
    trip_repo = TripRepository(client=None)
    intent_repo = IntentRepository(client=None)
    itinerary_repo = ItineraryRepository(client=None)
    ledger_repo = LedgerRepository(client=None)
    rescue_repo = RescueRepository(client=None)
    conversation_repo = ConversationStateRepository(client=None)

    ai_service = AIIntentService(use_mock=True)
    cache_manager = CacheFallbackManager()
    real_get_travel_data = cache_manager.get_travel_data

    async def mock_get_travel_data(engine, params=None, trip_id=None, **kwargs):
        if engine == "google_hotels":
            from budlance.serpapi.models import DataSource, TravelDataEnvelope
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="mock_hotel_hash",
                data={
                    "properties": [
                        {
                            "name": "Comfort Residency",
                            "rate_per_night": {"extracted_lowest": 1200},
                            "total_rate": {"extracted_lowest": 1200},
                        }
                    ]
                },
                is_fallback=False,
                status="success",
            )
        return await real_get_travel_data(engine, params=params, trip_id=trip_id, **kwargs)

    cache_manager.get_travel_data = mock_get_travel_data
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
async def test_infeasible_candidate_does_not_trigger_live_attraction_discovery(orchestrator):
    """Prove requirement 1: An infeasible candidate does not trigger live attraction discovery."""
    # Low budget of 500 INR for 4 people for 3 days from Delhi to Goa -> strictly NOT_FEASIBLE
    intent = ParsedTripIntent(
        action=TripAction.NEW_TRIP,
        budget=Decimal("500.00"),
        people=4,
        days=3,
        origin="Delhi",
        destination="Goa",
        currency="INR",
    )
    orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=intent)

    engine_calls: list[str] = []
    original_get_travel_data = orchestrator.cache_manager.get_travel_data

    async def tracking_get_travel_data(engine, params=None):
        engine_calls.append(engine)
        return await original_get_travel_data(engine=engine, params=params)

    orchestrator.cache_manager.get_travel_data = AsyncMock(side_effect=tracking_get_travel_data)

    result = await orchestrator.handle_user_message(
        telegram_user_id=88801,
        chat_id=88801,
        message="Delhi to Goa for 4 people with 500 INR for 3 days",
    )

    assert result.status == "NOT_FEASIBLE"
    assert result.feasibility_status == "NOT_FEASIBLE"

    # Transport, transit, hotel, routes queries are made for feasibility
    assert "google_flights" in engine_calls
    assert "google_hotels" in engine_calls
    # BUT live attraction discovery (google_maps) MUST NEVER BE CALLED for infeasible candidate
    assert "google_maps" not in engine_calls


@pytest.mark.asyncio
async def test_feasible_destination_triggers_attraction_discovery_after_feasibility(orchestrator):
    """Prove requirement 2: Feasible selected destination triggers attraction discovery ONLY after feasibility."""
    intent = ParsedTripIntent(
        action=TripAction.NEW_TRIP,
        budget=Decimal("50000.00"),
        people=2,
        days=2,
        origin="Mumbai",
        destination="Goa",
        currency="INR",
    )
    orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=intent)

    call_order: list[str] = []
    original_evaluate_candidate = orchestrator._evaluate_trip_candidate
    original_get_travel_data = orchestrator.cache_manager.get_travel_data

    async def tracking_evaluate_candidate(*args, **kwargs):
        call_order.append(f"start_feasibility_{kwargs.get('destination')}")
        res = await original_evaluate_candidate(*args, **kwargs)
        call_order.append(f"end_feasibility_{kwargs.get('destination')}_feasible={res.get('is_feasible')}")
        return res

    async def tracking_get_travel_data(engine, params=None):
        call_order.append(f"cache_request_{engine}")
        return await original_get_travel_data(engine=engine, params=params)

    orchestrator._evaluate_trip_candidate = tracking_evaluate_candidate
    orchestrator.cache_manager.get_travel_data = AsyncMock(side_effect=tracking_get_travel_data)

    result = await orchestrator.handle_user_message(
        telegram_user_id=88802,
        chat_id=88802,
        message="Mumbai to Goa for 2 people with 50000 INR for 2 days",
    )

    assert result.status == "FEASIBLE"
    assert result.selected_destination == "Goa"

    # Verify google_maps was called
    assert "cache_request_google_maps" in call_order

    # Verify google_maps was called AFTER destination feasibility was completed
    end_feasibility_idx = call_order.index("end_feasibility_Goa_feasible=True")
    google_maps_idx = call_order.index("cache_request_google_maps")
    assert google_maps_idx > end_feasibility_idx, (
        f"google_maps requested at index {google_maps_idx} before feasibility finished at {end_feasibility_idx}"
    )


@pytest.mark.asyncio
async def test_attraction_request_routes_through_cache_fallback_manager(orchestrator):
    """Prove requirement 3: Attraction requests still go through CacheFallbackManager."""
    intent = ParsedTripIntent(
        action=TripAction.NEW_TRIP,
        budget=Decimal("40000.00"),
        people=2,
        days=2,
        origin="Ahmedabad",
        destination="Gujarat",
        currency="INR",
    )
    orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=intent)

    cache_manager_queries: list[dict] = []
    original_get_travel_data = orchestrator.cache_manager.get_travel_data

    async def spy_get_travel_data(engine, params=None):
        cache_manager_queries.append({"engine": engine, "params": params})
        return await original_get_travel_data(engine=engine, params=params)

    orchestrator.cache_manager.get_travel_data = AsyncMock(side_effect=spy_get_travel_data)

    result = await orchestrator.handle_user_message(
        telegram_user_id=88803,
        chat_id=88803,
        message="Trip to Gujarat for 2 people, 40000 INR, 2 days",
    )

    assert result.status == "FEASIBLE"

    # Find the google_maps call in cache_manager_queries
    google_maps_queries = [q for q in cache_manager_queries if q["engine"] == "google_maps"]
    assert len(google_maps_queries) == 1, "Exactly one attraction data request through CacheFallbackManager"
    assert "Gujarat" in google_maps_queries[0]["params"]["location"]
    assert "Gujarat" in google_maps_queries[0]["params"]["q"]


@pytest.mark.asyncio
async def test_existing_curated_offline_attraction_behavior_unchanged(orchestrator):
    """Prove requirement 4: Existing curated offline attraction behavior remains unchanged."""
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
        telegram_user_id=88804,
        chat_id=88804,
        message="Chennai to Gujarat, 4 people, family trip, budget 60000, 2 days, heritage",
    )

    assert result.status == "FEASIBLE"
    assert result.selected_destination == "Gujarat"
    assert result.generated_itinerary is not None
    assert result.generated_itinerary.is_feasible is True
    assert len(result.generated_itinerary.days) == 2

    # Check curated attractions are correctly selected from offline catalog
    curated_items = [
        item for day in result.generated_itinerary.days
        for item in day.items if getattr(item, "is_curated", False)
    ]
    assert len(curated_items) > 0

    # Sabarmati Ashram is in curated offline gujarat.json
    names = [item.attraction_name for item in curated_items]
    assert any("Sabarmati Ashram" in name for name in names)

    # Check ticket costs were allocated from curated offline data
    for item in curated_items:
        if item.attraction_name == "Sabarmati Ashram":
            assert item.entry_fee_inr == 0  # Free admission in gujarat.json
            assert item.planned_cost == Decimal("0.00")

    # Check descriptions comply with single sentence < 20 words
    for item in curated_items:
        desc = item.description
        assert desc is not None
        words = desc.split()
        assert 1 <= len(words) < 20, f"Description '{desc}' has {len(words)} words, expected 1 <= words < 20"


@pytest.mark.asyncio
async def test_multi_candidate_discovery_only_queries_selected_destination(orchestrator):
    """Prove that during discovery with multiple candidates, only the feasible selected one queries google_maps."""
    # When destination is absent, destination discovery suggests candidates (e.g. Surat, Vadodara, Ahmedabad)
    intent = ParsedTripIntent(
        action=TripAction.NEW_TRIP,
        budget=Decimal("25000.00"),
        people=2,
        days=2,
        origin="Mumbai",
        destination=None,  # triggers discovery
        currency="INR",
    )
    orchestrator.ai_service.parse_trip_intent = AsyncMock(return_value=intent)

    # Mock candidate discovery to return 2 candidates: first one infeasible, second one feasible
    async def mock_discover(*args, **kwargs):
        return (["InfeasibleCity", "FeasibleCity"], False)

    orchestrator._discover_destinations = mock_discover

    original_evaluate = orchestrator._evaluate_trip_candidate
    async def selective_evaluate(*args, **kwargs):
        dest = kwargs.get("destination")
        if dest == "InfeasibleCity":
            return {
                "is_feasible": False,
                "destination": dest,
                "days": 2,
                "baseline_eval": None,
                "opt_result": None,
            }
        from budlance.schemas.travel import HotelOption, TransitOption
        from budlance.engine.models import BudgetEvaluationResult, BudgetBreakdown
        t_opt = TransitOption(
            transit_type="train",
            origin="Mumbai",
            destination="FeasibleCity",
            name_or_operator="Express Train",
            price=Decimal("1500.00"),
            is_fallback=True,
        )
        h_opt = HotelOption(
            name="Feasible Inn",
            total_price=Decimal("3000.00"),
            price_per_night=Decimal("1500.00"),
        )
        eval_res = BudgetEvaluationResult(
            status="FEASIBLE",
            is_feasible=True,
            breakdown=BudgetBreakdown(
                total_budget=Decimal("25000.00"),
                bucket_a_fixed=Decimal("4500.00"),
                bucket_b_survival=Decimal("2000.00"),
                bucket_c_activities=Decimal("500.00"),
                bucket_d_rescue=Decimal("2500.00"),
                transport_cost=Decimal("1500.00"),
                hotel_cost=Decimal("3000.00"),
                food_cost=Decimal("1500.00"),
                local_transit_cost=Decimal("500.00"),
                total_allocated=Decimal("9500.00"),
                remaining_surplus=Decimal("15500.00"),
                currency="INR",
            ),
            deficit=Decimal("0.00"),
            explanation="Feasible mock city",
        )
        return {
            "is_feasible": True,
            "destination": dest,
            "days": 2,
            "transport": t_opt,
            "hotel": h_opt,
            "route": None,
            "attractions": [],
            "evaluation": eval_res,
            "opt_result": None,
        }

    orchestrator._evaluate_trip_candidate = selective_evaluate

    maps_locations: list[str] = []
    original_get_travel_data = orchestrator.cache_manager.get_travel_data
    async def spy_get_travel_data(engine, params=None):
        if engine == "google_maps":
            maps_locations.append(params.get("location"))
        return await original_get_travel_data(engine=engine, params=params)

    orchestrator.cache_manager.get_travel_data = AsyncMock(side_effect=spy_get_travel_data)

    result = await orchestrator.handle_user_message(
        telegram_user_id=88805,
        chat_id=88805,
        message="Plan a weekend trip from Mumbai for 2 people with 25000 INR",
    )

    assert result.status == "FEASIBLE"
    assert result.selected_destination == "FeasibleCity"

    # InfeasibleCity was rejected during feasibility check, so it must NEVER have queried google_maps
    assert "InfeasibleCity" not in maps_locations
    # FeasibleCity was selected, so it DID query google_maps
    assert "FeasibleCity" in maps_locations
    assert len(maps_locations) == 1
