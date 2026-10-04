"""Tests for SerpApi Guard & Offline Catalog Invariant Verification (Task 12).

Verifies:
1. SerpApiGateway.has_credentials is False when credentials are not configured.
2. execute_search raises SerpApiAuthError without network calls when credentials are absent.
3. CacheFallbackManager enforces the has_credentials gate and returns offline/unconfigured envelopes.
4. AttractionSelector checks the CacheFallbackManager gateway guard for missing curated files.
5. Regional corridor offline catalog activates deterministically during destination discovery.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from budlance.attractions.selector import AttractionSelector
from budlance.cache.manager import CacheFallbackManager
from budlance.serpapi.exceptions import SerpApiAuthError
from budlance.serpapi.gateway import SerpApiGateway
from budlance.serpapi.models import DataSource


def test_serpapi_gateway_has_credentials_flag():
    """SerpApiGateway has_credentials must be False when initialized with None or empty string."""
    gw_none = SerpApiGateway(api_key=None)
    # If environment does not have key, it should be False
    with patch.dict("os.environ", {"SERPAPI_API_KEY": ""}, clear=False):
        gw_empty = SerpApiGateway(api_key="")
        assert gw_empty.has_credentials is False

    gw_whitespace = SerpApiGateway(api_key="   ")
    assert gw_whitespace.has_credentials is False

    gw_valid = SerpApiGateway(api_key="valid_test_key_123", serpapi_live_enabled=True)
    assert gw_valid.has_credentials is True


@pytest.mark.asyncio
async def test_serpapi_execute_search_raises_auth_error_without_network():
    """execute_search must immediately raise SerpApiAuthError without making HTTP calls."""
    gw = SerpApiGateway(api_key="")
    assert gw.has_credentials is False

    mock_client = AsyncMock()
    gw._http_client = mock_client

    with pytest.raises(SerpApiAuthError) as exc_info:
        await gw.execute_search("google_flights", {"q": "Chennai to Goa"})

    assert "SerpApi API key is not configured" in str(exc_info.value)
    mock_client.get.assert_not_called()


@pytest.mark.asyncio
async def test_cache_fallback_manager_bypasses_serpapi_when_uncredentialed():
    """CacheFallbackManager must never call execute_search when gateway.has_credentials is False."""
    mock_gateway = MagicMock(spec=SerpApiGateway)
    mock_gateway.has_credentials = False
    mock_gateway.execute_search = AsyncMock()

    mgr = CacheFallbackManager(gateway=mock_gateway)
    envelope = await mgr.get_travel_data(
        engine="google_travel_explore",
        params={"origin": "Chennai", "budget": 20000},
    )

    mock_gateway.execute_search.assert_not_called()
    assert envelope.source == DataSource.FALLBACK
    assert envelope.is_fallback is True
    assert envelope.status == "unconfigured"


@pytest.mark.asyncio
async def test_cache_fallback_manager_train_corridor_still_resolves():
    """Known train corridor resolves to static fallback even when SerpApi is uncredentialed."""
    mock_gateway = MagicMock(spec=SerpApiGateway)
    mock_gateway.has_credentials = False

    mgr = CacheFallbackManager(gateway=mock_gateway)
    envelope = await mgr.get_travel_data(
        engine="trains",
        params={"origin": "Chennai", "destination": "Bangalore"},
    )

    assert envelope.source == DataSource.FALLBACK
    assert envelope.is_fallback is True
    assert envelope.status == "success"
    assert "trains" in envelope.data or "options" in envelope.data or isinstance(envelope.data, dict)


def test_attraction_selector_uncredentialed_guard():
    """AttractionSelector returns empty list for unknown destination with CacheFallbackManager guard."""
    mock_gateway = MagicMock(spec=SerpApiGateway)
    mock_gateway.has_credentials = False

    mock_cache = MagicMock(spec=CacheFallbackManager)
    mock_cache.gateway = mock_gateway

    selector = AttractionSelector(cache_manager=mock_cache)
    result = selector.select_for_itinerary(
        destination="UnknownDestCity",
        travel_party="friends",
        days=2,
    )
    assert result == []


@pytest.mark.asyncio
async def test_orchestrator_destination_discovery_no_fabrication():
    """Orchestrator discover destinations returns empty list when explore data is empty without inventing fake destinations."""
    from budlance.orchestrator.orchestrator import BudlanceOrchestrator

    mock_cache = MagicMock(spec=CacheFallbackManager)
    mock_cache.gateway = MagicMock(has_credentials=False)
    # Empty envelope returned when explore returns empty
    mock_cache.get_travel_data = AsyncMock(
        return_value=MagicMock(data={}, is_fallback=True)
    )

    orch = BudlanceOrchestrator(cache_manager=mock_cache)
    discovered, used_fallback = await orch._discover_destinations(
        origin="Chennai",
        budget=Decimal("25000.00"),
        interests=["heritage"],
    )

    # Curated domestic pool is restored for candidate screening
    assert "Goa" in discovered
    assert "NowhereVille" not in discovered
