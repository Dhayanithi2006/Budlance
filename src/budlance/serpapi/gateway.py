"""SerpApi Gateway providing verified engine access for Budlance."""

import logging
from typing import Any
import httpx
from budlance.config import get_settings
from budlance.serpapi.exceptions import (
    SerpApiAuthError,
    SerpApiNetworkError,
    SerpApiRateLimitError,
    SerpApiResponseError,
)
from budlance.serpapi.rate_limiter import AsyncRateLimiter
from budlance.serpapi.retry import retry_with_backoff

logger = logging.getLogger(__name__)

SERPAPI_BASE_URL = "https://serpapi.com/search.json"


class SerpApiGateway:
    """Async gateway client managing calls to official SerpApi travel engines."""

    def __init__(
        self,
        api_key: str | None = None,
        rate_limiter: AsyncRateLimiter | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self._api_key = api_key or settings.serpapi_api_key
        self.rate_limiter = rate_limiter or AsyncRateLimiter(max_calls_per_minute=30, max_concurrent=5)
        self._http_client = http_client

    @property
    def has_credentials(self) -> bool:
        """Check if SERPAPI_API_KEY is configured."""
        return bool(self._api_key and self._api_key.strip())

    async def execute_search(
        self,
        engine: str,
        params: dict[str, Any],
        max_retries: int = 3,
    ) -> dict[str, Any]:
        """Execute a rate-limited and retry-protected SerpApi search."""
        if not self.has_credentials:
            raise SerpApiAuthError(
                "SerpApi API key is not configured. Set SERPAPI_API_KEY in .env."
            )

        # Instrumentation: log call attempt boundary (no secrets, no user data)
        logger.info(
            "[SERPAPI] provider=serpapi operation=%s outcome=attempting call_type=live",
            engine,
        )

        # Merge engine and API key into request payload (key excluded from logs)
        request_params = {
            "api_key": self._api_key.strip(),
            "engine": engine,
            **params,
        }

        attempt_counter = {"count": 0}

        async def _single_call() -> dict[str, Any]:
            attempt_counter["count"] += 1
            logger.info(
                "[SERPAPI] provider=serpapi operation=%s attempt=%d max_retries=%d",
                engine,
                attempt_counter["count"],
                max_retries,
            )
            await self.rate_limiter.acquire()
            should_close = False
            client = self._http_client
            if client is None:
                client = httpx.AsyncClient(timeout=25.0)
                should_close = True

            try:
                response = await client.get(SERPAPI_BASE_URL, params=request_params)
            except httpx.TimeoutException as exc:
                logger.warning(
                    "[SERPAPI] provider=serpapi operation=%s outcome=timeout attempt=%d",
                    engine,
                    attempt_counter["count"],
                )
                raise SerpApiNetworkError(f"SerpApi call timed out: {exc}") from exc
            except httpx.RequestError as exc:
                logger.warning(
                    "[SERPAPI] provider=serpapi operation=%s outcome=network_error attempt=%d",
                    engine,
                    attempt_counter["count"],
                )
                raise SerpApiNetworkError(f"SerpApi network request error: {exc}") from exc
            finally:
                self.rate_limiter.release()
                if should_close:
                    await client.aclose()

            if response.status_code in (401, 403):
                logger.warning(
                    "[SERPAPI] provider=serpapi operation=%s outcome=auth_failure http_status=%d",
                    engine,
                    response.status_code,
                )
                raise SerpApiAuthError("SerpApi authentication failed. Check your API key.")
            if response.status_code == 429:
                logger.warning(
                    "[SERPAPI] provider=serpapi operation=%s outcome=rate_limited",
                    engine,
                )
                raise SerpApiRateLimitError("SerpApi rate limit or monthly search quota exceeded.")
            if response.status_code != 200:
                logger.warning(
                    "[SERPAPI] provider=serpapi operation=%s outcome=http_error http_status=%d",
                    engine,
                    response.status_code,
                )
                raise SerpApiResponseError(
                    f"SerpApi error HTTP {response.status_code}: {response.text[:200]}"
                )

            data = response.json()
            if "error" in data:
                err_msg = str(data["error"])
                if "Invalid API key" in err_msg:
                    logger.warning(
                        "[SERPAPI] provider=serpapi operation=%s outcome=invalid_key",
                        engine,
                    )
                    raise SerpApiAuthError("SerpApi reports: Invalid API key")
                raise SerpApiResponseError(f"SerpApi API error: {err_msg}")

            logger.info(
                "[SERPAPI] provider=serpapi operation=%s outcome=success attempts_used=%d",
                engine,
                attempt_counter["count"],
            )
            return data

        result = await retry_with_backoff(_single_call, max_retries=max_retries)
        return result

    # =========================================================================
    # Verified Engine Methods
    # =========================================================================
    async def search_travel_explore(self, params: dict[str, Any]) -> dict[str, Any]:
        """Destination discovery via Google Travel Explore."""
        return await self.execute_search("google_travel_explore", params)

    async def search_flights(self, params: dict[str, Any]) -> dict[str, Any]:
        """Flight schedules and fares via Google Flights."""
        return await self.execute_search("google_flights", params)

    async def search_hotels(self, params: dict[str, Any]) -> dict[str, Any]:
        """Hotel pricing and availability via Google Hotels."""
        return await self.execute_search("google_hotels", params)

    async def search_local_places(self, params: dict[str, Any]) -> dict[str, Any]:
        """Attractions, restaurants, beaches, and local points of interest via Google Maps."""
        return await self.execute_search("google_maps", params)

    async def search_directions(self, params: dict[str, Any]) -> dict[str, Any]:
        """Routes, distance, and transit time via Google Maps Directions."""
        return await self.execute_search("google_maps_directions", params)

    async def search_reviews(self, params: dict[str, Any]) -> dict[str, Any]:
        """Venue reviews and sentiment signals via Google Maps Reviews."""
        return await self.execute_search("google_maps_reviews", params)

    async def search_general(self, params: dict[str, Any]) -> dict[str, Any]:
        """Fallback general web knowledge via Google Search."""
        return await self.execute_search("google", params)
