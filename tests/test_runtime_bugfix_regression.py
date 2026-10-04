"""Regression test suite for Telegram runtime bug fixes.

Covers:
1. Exact user sentence heuristic parsing.
2. OpenRouter 429 fallback to heuristic parser.
3. No repeated endless retries on 429 rate limit.
4. Unsupported international destination does not receive Indian train fallback.
5. Train requests bypass SerpApi gateway completely.
6. Bus requests bypass SerpApi gateway completely.
7. Duplicate search_cache query_hash upsert is idempotent.
8. Cache hash uniqueness across all provider-relevant parameters.
9. Google Maps queries include 'm' parameter when using 'location'.
10. Live destination is candidate, not automatic feasibility.
11. Missing transport blocks candidate destination on intercity trips.
12. Missing hotel does not become free accommodation.
13. Optimizer cannot make unsupported destination feasible without transport.
14. Exact Chennai input does not produce Tokyo + train + 1-day trip.
"""

from decimal import Decimal
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx

from budlance.ai.client import OpenRouterClient
from budlance.ai.exceptions import OpenRouterResponseError
from budlance.ai.service import AIIntentService
from budlance.cache.manager import CacheFallbackManager, compute_query_hash
from budlance.config import get_settings
from budlance.db.models import SearchCache, Trip, utc_now
from budlance.db.repositories.cache_repo import CacheRepository
from budlance.engine.budget import ReverseBudgetEngine
from budlance.estimation.estimator import EstimationLayer
from budlance.engine.optimizer import OptimizationEngine
from budlance.schemas.travel import FlightOption, HotelOption, TransitOption
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.serpapi.gateway import SerpApiGateway


# =============================================================================
# 1. Exact User Sentence Parsing
# =============================================================================

def test_exact_user_sentence_heuristic_parsing():
    """Verify exact sentence heuristic parsing extracts correct parameters without Tokyo."""
    sentence = "I have 15000 for 3 people for 2 days. I like to explore local food of that place and theme park from chennai"
    service = AIIntentService(use_mock=True)

    intent = service._mock_parse_trip_intent(sentence)

    assert intent.budget == Decimal("15000")
    assert intent.people == 3
    assert intent.days == 2
    assert intent.origin == "Chennai"
    assert intent.destination is None, f"Destination should be None, got {intent.destination}"
    assert intent.interests is not None
    assert "local food" in intent.interests or "food" in intent.interests
    assert "theme park" in intent.interests
    assert intent.destination != "Tokyo"
    assert intent.origin != intent.destination


# =============================================================================
# 2 & 3. OpenRouter 429 Fallback & No Endless Retries
# =============================================================================

@pytest.mark.asyncio
async def test_openrouter_429_fast_fallback_to_heuristic():
    """Verify OpenRouter 429 triggers fast failover to heuristic parser without dead retry loops."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.text = '{"error": {"message": "Rate limit reached"}}'
    mock_http.post = AsyncMock(return_value=mock_resp)

    client = OpenRouterClient(api_key="test_key", http_client=mock_http)
    service = AIIntentService(client=client, use_mock=False)

    sentence = "I have 15000 for 3 people for 2 days. I like to explore local food of that place and theme park from chennai"
    intent = await service.parse_trip_intent(sentence)

    # Fast failover to heuristic parser
    assert intent.budget == Decimal("15000")
    assert intent.people == 3
    assert intent.days == 2
    assert intent.origin == "Chennai"
    assert intent.destination is None
    # Maximum 2 attempts on 429
    assert mock_http.post.call_count <= 2


@pytest.mark.asyncio
async def test_openrouter_models_array_payload():
    """Verify chat_completion sends models array with primary and fallback models."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"action": "NEW_TRIP", "budget": 15000, "people": 3, "days": 2, "origin": "Chennai"})}}]
    }
    mock_http.post = AsyncMock(return_value=mock_resp)

    client = OpenRouterClient(api_key="test_key", http_client=mock_http)
    await client.chat_completion([{"role": "user", "content": "hi"}])

    call_kwargs = mock_http.post.call_args[1]
    payload = call_kwargs["json"]
    assert "models" in payload
    assert isinstance(payload["models"], list)
    assert client.model in payload["models"]


# =============================================================================
# 4. Train Fallback Restrictions (Unsupported Routes Never Get Fake Rail)
# =============================================================================

@pytest.mark.asyncio
async def test_unsupported_international_destination_no_train_fallback():
    """Verify Tokyo and Paris never receive Indian Railways fallback options."""
    orchestrator = BudlanceOrchestrator()

    tokyo_options = await orchestrator.lookup_transport_options(
        origin="Chennai",
        destination="Tokyo",
        people=3,
    )
    assert len(tokyo_options) == 0, f"Expected 0 options for Chennai->Tokyo, got {tokyo_options}"

    paris_options = await orchestrator.lookup_transport_options(
        origin="Chennai",
        destination="Paris",
        people=3,
    )
    assert len(paris_options) == 0, f"Expected 0 options for Chennai->Paris, got {paris_options}"


@pytest.mark.asyncio
async def test_supported_domestic_train_fallback_works():
    """Verify Chennai -> Goa uses supported offline train corridor."""
    orchestrator = BudlanceOrchestrator()

    goa_options = await orchestrator.lookup_transport_options(
        origin="Chennai",
        destination="Goa",
        people=2,
        transport_mode="train",
    )
    assert len(goa_options) > 0
    train_names = [getattr(o, "name_or_operator", "") for o in goa_options]
    assert any("Vasco Express" in name for name in train_names)


# =============================================================================
# 5 & 6. Trains and Buses Bypass SerpApi Gateway Completely
# =============================================================================

@pytest.mark.asyncio
async def test_train_engine_bypasses_serpapi():
    """Verify train requests never call SerpApiGateway."""
    mock_gateway = MagicMock(spec=SerpApiGateway)
    mock_gateway.execute_search = AsyncMock()

    cache_mgr = CacheFallbackManager(
        gateway=mock_gateway,
        cache_repo=CacheRepository(),
    )

    envelope = await cache_mgr.get_travel_data(
        engine="trains",
        params={"origin": "Chennai", "destination": "Bangalore"},
    )

    assert envelope.status == "success"
    assert envelope.is_fallback is True
    assert mock_gateway.execute_search.call_count == 0


@pytest.mark.asyncio
async def test_bus_engine_bypasses_serpapi():
    """Verify bus requests never call SerpApiGateway."""
    mock_gateway = MagicMock(spec=SerpApiGateway)
    mock_gateway.execute_search = AsyncMock()

    cache_mgr = CacheFallbackManager(
        gateway=mock_gateway,
        cache_repo=CacheRepository(),
    )

    envelope = await cache_mgr.get_travel_data(
        engine="buses",
        params={"origin": "Chennai", "destination": "Bangalore"},
    )

    assert envelope.status == "success"
    assert mock_gateway.execute_search.call_count == 0


@pytest.mark.asyncio
async def test_gateway_directly_rejects_train_engine():
    """Verify SerpApiGateway raises ValueError if transit engines are directly requested."""
    gateway = SerpApiGateway(api_key="test_key")
    with pytest.raises(ValueError, match="offline transit catalog"):
        await gateway.execute_search("trains", {"q": "Chennai to Bangalore"})

    with pytest.raises(ValueError, match="offline transit catalog"):
        await gateway.execute_search("buses", {"q": "Chennai to Bangalore"})


# =============================================================================
# 7. Search Cache Duplicate Upsert Idempotency
# =============================================================================

def test_cache_repo_duplicate_insert_is_idempotent():
    """Verify storing the same query_hash multiple times does not throw unique constraint violation."""
    repo = CacheRepository()
    record1 = SearchCache(
        query_hash="test_hash_123",
        engine="google_hotels",
        params_json={"q": "Hotels in Goa"},
        response_data={"hotels": [{"name": "Hotel A", "price": 2000}]},
        created_at=utc_now(),
        expires_at=utc_now(),
    )
    record2 = SearchCache(
        query_hash="test_hash_123",
        engine="google_hotels",
        params_json={"q": "Hotels in Goa"},
        response_data={"hotels": [{"name": "Hotel B", "price": 2500}]},
        created_at=utc_now(),
        expires_at=utc_now(),
    )

    # First insert
    saved1 = repo.set_cached_search(record1)
    assert saved1.query_hash == "test_hash_123"

    # Second insert with same hash must not raise
    saved2 = repo.set_cached_search(record2)
    assert saved2.query_hash == "test_hash_123"


# =============================================================================
# 8. Cache Hash Uniqueness
# =============================================================================

def test_cache_hash_includes_all_parameters():
    """Verify different flight and hotel parameters produce distinct query hashes."""
    hash_chennai_goa = compute_query_hash("google_flights", {"departure_id": "MAA", "arrival_id": "GOI", "adults": 3})
    hash_chennai_tokyo = compute_query_hash("google_flights", {"departure_id": "MAA", "arrival_id": "HND", "adults": 3})
    assert hash_chennai_goa != hash_chennai_tokyo

    hash_hotels_chennai = compute_query_hash("google_hotels", {"q": "Hotels in Chennai", "adults": 3})
    hash_hotels_tokyo = compute_query_hash("google_hotels", {"q": "Hotels in Tokyo", "adults": 3})
    assert hash_hotels_chennai != hash_hotels_tokyo


# =============================================================================
# 9. Google Maps Location Parameters
# =============================================================================

@pytest.mark.asyncio
async def test_google_maps_location_includes_m_radius():
    """Verify SerpApiGateway injects 'm' search radius parameter when location is passed."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"local_results": []}
    mock_http.get = AsyncMock(return_value=mock_resp)

    gateway = SerpApiGateway(api_key="test_key", http_client=mock_http, serpapi_live_enabled=True)
    await gateway.execute_search("google_maps", {"q": "places to visit", "location": "Chennai"})

    call_params = mock_http.get.call_args[1]["params"]
    assert "location" in call_params
    assert "m" in call_params, "Expected 'm' parameter to be present in google_maps search"
    assert call_params["m"] == get_settings().maps_search_radius_meters


# =============================================================================
# 10, 11, 12, 13. Candidate Feasibility & Optimizer Guards
# =============================================================================

@pytest.mark.asyncio
async def test_missing_transport_blocks_intercity_candidate_feasibility():
    """Verify candidate destination without transport is rejected as NOT_FEASIBLE."""
    orchestrator = BudlanceOrchestrator()

    # Mock _collect_travel_components to return NO transport for Tokyo
    orchestrator._collect_travel_components = AsyncMock(
        return_value=(
            None,  # primary_transport
            [],    # available_transports
            HotelOption(name="Tokyo Hotel", total_price=Decimal("4000"), price_per_night=Decimal("2000")),
            [],    # available_hotels
            None,  # route
        )
    )

    plan = await orchestrator._evaluate_trip_candidate(
        origin="Chennai",
        destination="Tokyo",
        people=3,
        days=2,
        budget=Decimal("15000"),
        currency="INR",
        travel_party=None,
        interests=["food"],
    )

    assert plan["is_feasible"] is False
    assert plan.get("rejection_reason") == "NO_TRANSPORT_AVAILABLE"


def test_missing_hotel_does_not_become_free():
    """Verify multi-day trip with no hotel uses non-zero offline lodging estimate."""
    budget_engine = ReverseBudgetEngine()
    eval_result = budget_engine.evaluate(
        total_budget=Decimal("15000"),
        people=2,
        days=3,
        transport=None,
        hotel=None,
        food_estimate=EstimationLayer().estimate_food(2, 3),
        local_transit_estimate=EstimationLayer().estimate_local_transit_daily(3, 2),
    )

    assert eval_result.breakdown.hotel_cost > Decimal("0.00")


def test_optimizer_cannot_make_missing_transport_feasible():
    """Verify Optimizer cannot declare feasibility when requires_transport=True and transport is None."""
    optimizer = OptimizationEngine()
    food_est = EstimationLayer().estimate_food(3, 2)
    transit_est = EstimationLayer().estimate_local_transit_daily(2, 3)

    result = optimizer.optimize(
        trip_id=None,
        total_budget=Decimal("15000"),
        people=3,
        days=2,
        initial_transport=None,
        initial_hotel=HotelOption(name="Tokyo Hotel", total_price=Decimal("4000"), price_per_night=Decimal("2000")),
        initial_food=food_est,
        initial_transit=transit_est,
        requires_transport=True,
    )

    assert result.is_feasible is False


# =============================================================================
# 14. Full End-to-End Orchestrator Candidate Feasibility
# =============================================================================

@pytest.mark.asyncio
async def test_tokyo_cannot_be_selected_from_chennai_budget_request():
    """Verify the exact user input cannot select Tokyo with an Indian train option."""
    from budlance.db.repositories.conversation_repo import ConversationStateRepository
    from budlance.db.repositories.trip_repo import TripRepository
    from budlance.db.repositories.user_repo import UserRepository

    orchestrator = BudlanceOrchestrator(
        user_repo=UserRepository(),
        trip_repo=TripRepository(),
        conversation_repo=ConversationStateRepository(),
    )

    # Force candidate destinations to include Tokyo
    orchestrator._discover_destinations_from_explore = AsyncMock(return_value=["Tokyo"])

    result = await orchestrator.handle_user_message(
        telegram_user_id=123456,
        chat_id=123456,
        message="I have 15000 for 3 people for 2 days. I like to explore local food of that place and theme park from chennai",
    )

    # Tokyo must not be selected as a planned trip
    assert result.status in ("NOT_FEASIBLE", "CLARIFICATION")
    assert result.trip_id is None
    assert "Tokyo" not in (result.message_text or "") or "not enough" in result.message_text.lower() or "adjust" in result.message_text.lower()
