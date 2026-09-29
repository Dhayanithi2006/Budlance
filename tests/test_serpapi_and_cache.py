"""Tests for SerpApi Gateway, Cache/Fallback Manager, Rate Limiter, and Retry."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
import httpx
import pytest

from budlance.cache.fallback import FallbackDataProvider
from budlance.cache.manager import CacheFallbackManager, compute_query_hash
from budlance.db.models import SearchCache
from budlance.db.repositories.cache_repo import CacheRepository
from budlance.db.repositories.usage_repo import UsageRepository
from budlance.serpapi.exceptions import (
    SerpApiAuthError,
    SerpApiNetworkError,
    SerpApiRateLimitError,
    SerpApiResponseError,
)
from budlance.serpapi.gateway import SerpApiGateway
from budlance.serpapi.models import DataSource
from budlance.serpapi.rate_limiter import AsyncRateLimiter
from budlance.serpapi.retry import is_transient_error, retry_with_backoff


# ============================================================================
# 1. Cache Hit Avoids Live API Call
# ============================================================================
@pytest.mark.asyncio
async def test_cache_hit_avoids_live_call():
    """Verify that a cached entry returns CACHED envelope without invoking the gateway."""
    mock_gateway = MagicMock(spec=SerpApiGateway)
    mock_gateway.has_credentials = True
    mock_gateway.execute_search = AsyncMock()

    cache_repo = CacheRepository(client=None)
    usage_repo = UsageRepository(client=None)
    manager = CacheFallbackManager(gateway=mock_gateway, cache_repo=cache_repo, usage_repo=usage_repo)

    engine = "google_flights"
    params = {"departure_id": "BOM", "arrival_id": "GOI"}
    query_hash = compute_query_hash(engine, params)

    # Pre-populate cache with unexpired result
    cache_record = SearchCache(
        query_hash=query_hash,
        engine=engine,
        params_json=params,
        response_data={"flights": [{"airline": "IndiGo", "price": 4200}]},
        expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
    )
    cache_repo.set_cached_search(cache_record)

    trip_id = uuid4()
    envelope = await manager.get_travel_data(engine, params, trip_id=trip_id)

    assert envelope.source == DataSource.CACHED
    assert envelope.is_fallback is False
    assert envelope.data["flights"][0]["price"] == 4200
    assert not mock_gateway.execute_search.called

    # Verify usage was tracked as cached
    usage = usage_repo.get_trip_usage(trip_id)
    assert len(usage) == 1
    assert usage[0].cached_count == 1
    assert usage[0].call_count == 0


# ============================================================================
# 2. Cache Miss With Fallback Uses Fallback
# ============================================================================
@pytest.mark.asyncio
async def test_cache_miss_with_fallback_uses_fallback():
    """Verify that train/bus queries with known static corridors return FALLBACK envelope."""
    mock_gateway = MagicMock(spec=SerpApiGateway)
    mock_gateway.has_credentials = True
    mock_gateway.execute_search = AsyncMock()

    cache_repo = CacheRepository(client=None)
    usage_repo = UsageRepository(client=None)
    manager = CacheFallbackManager(gateway=mock_gateway, cache_repo=cache_repo, usage_repo=usage_repo)

    params = {"origin": "Chennai", "destination": "Bangalore"}
    envelope = await manager.get_travel_data("trains", params)

    assert envelope.source == DataSource.FALLBACK
    assert envelope.is_fallback is True
    assert "Shatabdi" in envelope.data.get("train_name", "")
    assert not mock_gateway.execute_search.called


# ============================================================================
# 3. Cache Miss Without Fallback Calls SerpApi
# ============================================================================
@pytest.mark.asyncio
async def test_cache_miss_calls_live_serpapi():
    """Verify that a novel query calls live SerpApi and saves to cache with TTL."""
    mock_gateway = MagicMock(spec=SerpApiGateway)
    mock_gateway.has_credentials = True
    mock_gateway.execute_search = AsyncMock(return_value={"hotels": [{"name": "Grand Palace", "rate": 5000}]})

    cache_repo = CacheRepository(client=None)
    usage_repo = UsageRepository(client=None)
    manager = CacheFallbackManager(gateway=mock_gateway, cache_repo=cache_repo, usage_repo=usage_repo)

    trip_id = uuid4()
    params = {"q": "Hotels in Goa", "check_in_date": "2026-10-01"}
    envelope = await manager.get_travel_data("google_hotels", params, trip_id=trip_id)

    assert envelope.source == DataSource.LIVE
    assert envelope.is_fallback is False
    assert envelope.data["hotels"][0]["name"] == "Grand Palace"
    assert mock_gateway.execute_search.called

    # Confirm newly cached
    query_hash = compute_query_hash("google_hotels", params)
    cached = cache_repo.get_cached_search(query_hash)
    assert cached is not None
    assert cached.response_data["hotels"][0]["name"] == "Grand Palace"

    # Confirm usage recorded as live call
    usage = usage_repo.get_trip_usage(trip_id)
    assert len(usage) == 1
    assert usage[0].call_count == 1
    assert usage[0].cached_count == 0


# ============================================================================
# 4. Missing SerpApi Credentials Fail Safely
# ============================================================================
@pytest.mark.asyncio
async def test_missing_serpapi_credentials_raises_auth_error():
    """Verify that gateway raises SerpApiAuthError when api_key is empty."""
    gateway = SerpApiGateway(api_key="")
    with pytest.raises(SerpApiAuthError):
        await gateway.execute_search("google_flights", {"q": "flights"})


# ============================================================================
# 5. Retry & Backoff Behavior
# ============================================================================
@pytest.mark.asyncio
async def test_retry_on_transient_failure():
    """Verify that transient failures retry up to max_retries before succeeding."""
    call_count = 0

    async def transient_operation():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise SerpApiNetworkError("Temporary timeout")
        return {"status": "recovered"}

    result = await retry_with_backoff(transient_operation, max_retries=3, initial_delay=0.01)
    assert result == {"status": "recovered"}
    assert call_count == 3


@pytest.mark.asyncio
async def test_non_retryable_error_fails_immediately():
    """Verify non-retryable errors (e.g. SerpApiAuthError) do not trigger retries."""
    call_count = 0

    async def auth_fail_operation():
        nonlocal call_count
        call_count += 1
        raise SerpApiAuthError("Invalid credentials")

    with pytest.raises(SerpApiAuthError):
        await retry_with_backoff(auth_fail_operation, max_retries=3, initial_delay=0.01)

    assert call_count == 1


def test_is_transient_error_classification():
    """Verify transient error categorization."""
    assert is_transient_error(SerpApiNetworkError("timeout")) is True
    assert is_transient_error(SerpApiRateLimitError("429")) is True
    assert is_transient_error(SerpApiResponseError("HTTP 502 Bad Gateway")) is True
    assert is_transient_error(SerpApiAuthError("Unauthorized")) is False
    assert is_transient_error(ValueError("Bad parameter")) is False


# ============================================================================
# 6. Rate Limiter Throttling
# ============================================================================
@pytest.mark.asyncio
async def test_rate_limiter_concurrency():
    """Verify that rate limiter limits concurrent access using semaphore."""
    limiter = AsyncRateLimiter(max_calls_per_minute=60, max_concurrent=2)
    active_count = 0
    max_observed = 0

    async def worker():
        nonlocal active_count, max_observed
        await limiter.acquire()
        active_count += 1
        max_observed = max(max_observed, active_count)
        await asyncio.sleep(0.02)
        active_count -= 1
        limiter.release()

    await asyncio.gather(*(worker() for _ in range(5)))
    assert max_observed <= 2


# ============================================================================
# 7. TTL Expiration Causes Live Re-Query
# ============================================================================
@pytest.mark.asyncio
async def test_cache_ttl_expiration_re_queries_live():
    """Verify that an expired cached search causes a live SerpApi re-query."""
    mock_gateway = MagicMock(spec=SerpApiGateway)
    mock_gateway.has_credentials = True
    mock_gateway.execute_search = AsyncMock(return_value={"data": "fresh_live_data"})

    cache_repo = CacheRepository(client=None)
    usage_repo = UsageRepository(client=None)
    manager = CacheFallbackManager(gateway=mock_gateway, cache_repo=cache_repo, usage_repo=usage_repo)

    params = {"destination": "Ooty"}
    query_hash = compute_query_hash("google_maps", params)

    # Insert an EXPIRED cache record (expired 10 minutes ago)
    expired_record = SearchCache(
        query_hash=query_hash,
        engine="google_maps",
        params_json=params,
        response_data={"data": "stale_data"},
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    cache_repo.set_cached_search(expired_record)

    envelope = await manager.get_travel_data("google_maps", params)

    assert envelope.source == DataSource.LIVE
    assert envelope.data["data"] == "fresh_live_data"
    assert mock_gateway.execute_search.called


# ============================================================================
# 8. Engine Method Routing on Gateway
# ============================================================================
@pytest.mark.asyncio
async def test_engine_routing_methods():
    """Verify that all frozen SerpApi gateway engine methods route to execute_search."""
    gateway = SerpApiGateway(api_key="mock_secret_key")
    gateway.execute_search = AsyncMock(return_value={"ok": True})

    await gateway.search_travel_explore({"explore": "destinations"})
    gateway.execute_search.assert_called_with("google_travel_explore", {"explore": "destinations"})

    await gateway.search_flights({"departure_id": "DEL"})
    gateway.execute_search.assert_called_with("google_flights", {"departure_id": "DEL"})

    await gateway.search_hotels({"q": "Resort"})
    gateway.execute_search.assert_called_with("google_hotels", {"q": "Resort"})

    await gateway.search_local_places({"q": "Beaches"})
    gateway.execute_search.assert_called_with("google_maps", {"q": "Beaches"})

    await gateway.search_directions({"start": "A", "end": "B"})
    gateway.execute_search.assert_called_with("google_maps_directions", {"start": "A", "end": "B"})

    await gateway.search_reviews({"place_id": "123"})
    gateway.execute_search.assert_called_with("google_maps_reviews", {"place_id": "123"})

    await gateway.search_general({"q": "weather"})
    gateway.execute_search.assert_called_with("google", {"q": "weather"})


# ============================================================================
# Regression: unconfigured SerpApi must NOT propagate SerpApiAuthError
# Bug: CacheFallbackManager re-raised when no origin+destination pair existed
# (e.g. destination-discovery calls), crashing the orchestrator.
# Fix: return empty fallback envelope so caller static catalogs can proceed.
# ============================================================================
@pytest.mark.asyncio
async def test_cache_manager_unconfigured_serpapi_no_destination_returns_empty_envelope():
    """Regression: get_travel_data must return an empty fallback envelope, not raise,
    when SERPAPI_API_KEY is unconfigured and no static corridor covers the query
    (e.g. google_travel_explore destination-discovery calls with no destination param).
    """
    gateway = MagicMock(spec=SerpApiGateway)
    gateway.has_credentials = False

    cache_repo = MagicMock(spec=CacheRepository)
    cache_repo.get_cached_search.return_value = None

    usage_repo = MagicMock(spec=UsageRepository)
    fallback = MagicMock(spec=FallbackDataProvider)
    fallback.get_train_corridor.return_value = None
    fallback.get_bus_corridor.return_value = None

    manager = CacheFallbackManager(
        gateway=gateway,
        cache_repo=cache_repo,
        usage_repo=usage_repo,
        fallback_provider=fallback,
    )

    # Simulates destination-discovery: origin only, no destination key
    envelope = await manager.get_travel_data(
        engine="google_travel_explore",
        params={"origin": "Chennai", "budget": 15000.0, "interests": "beaches,nature"},
    )

    # Must NOT raise — must return a fallback envelope with empty/safe data
    assert envelope.is_fallback is True
    assert envelope.engine == "google_travel_explore"
    assert envelope.data is not None
    assert envelope.status in ("unconfigured", "error", "success")
    gateway.execute_search.assert_not_called()


@pytest.mark.asyncio
async def test_cache_manager_live_call_failure_no_corridor_returns_empty_envelope():
    """Regression: when live SerpApi call fails and no corridor fallback exists,
    get_travel_data must return an empty fallback envelope, not re-raise.
    """
    gateway = MagicMock(spec=SerpApiGateway)
    gateway.has_credentials = True
    gateway.execute_search = AsyncMock(side_effect=SerpApiAuthError("Auth failed"))

    cache_repo = MagicMock(spec=CacheRepository)
    cache_repo.get_cached_search.return_value = None

    usage_repo = MagicMock(spec=UsageRepository)
    fallback = MagicMock(spec=FallbackDataProvider)
    fallback.get_train_corridor.return_value = None
    fallback.get_bus_corridor.return_value = None

    manager = CacheFallbackManager(
        gateway=gateway,
        cache_repo=cache_repo,
        usage_repo=usage_repo,
        fallback_provider=fallback,
    )

    envelope = await manager.get_travel_data(
        engine="google_travel_explore",
        params={"origin": "Chennai", "budget": 15000.0},
    )

    assert envelope.is_fallback is True
    assert envelope.status in ("error", "unconfigured", "success")
    assert envelope.data is not None
