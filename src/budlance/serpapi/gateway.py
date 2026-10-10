"""SerpApi Gateway providing verified engine access for Budlance."""

import logging
import time
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
from budlance.serpapi.telemetry import SerpApiTelemetry

logger = logging.getLogger(__name__)

SERPAPI_BASE_URL = "https://serpapi.com/search.json"


class SerpApiGateway:
    """Async gateway client managing calls to official SerpApi travel engines."""

    def __init__(
        self,
        api_key: str | None = None,
        fallback_api_key: str | None = None,
        rate_limiter: AsyncRateLimiter | None = None,
        http_client: httpx.AsyncClient | None = None,
        serpapi_live_enabled: bool | None = None,
        telemetry: SerpApiTelemetry | None = None,
    ) -> None:
        settings = get_settings()
        if api_key is not None:
            self._api_key = api_key
            self._fallback_api_key = fallback_api_key or ""
        else:
            self._api_key = settings.serpapi_api_key
            self._fallback_api_key = (
                fallback_api_key
                if fallback_api_key is not None
                else settings.serpapi_fallback_api_key
            )
        self.rate_limiter = rate_limiter or AsyncRateLimiter(max_calls_per_minute=30, max_concurrent=5)
        self._http_client = http_client
        self._live_enabled = (
            serpapi_live_enabled
            if serpapi_live_enabled is not None
            else getattr(settings, "serpapi_live_enabled", True)
        )
        self.telemetry = telemetry or SerpApiTelemetry()

    @property
    def has_credentials(self) -> bool:
        """Check if SERPAPI_API_KEY or SERPAPI_FALLBACK_API_KEY is configured and live search is enabled."""
        if not self._live_enabled:
            return False
        return bool(
            (self._api_key and self._api_key.strip())
            or (self._fallback_api_key and self._fallback_api_key.strip())
        )

    @property
    def is_live_mode(self) -> bool:
        """Alias for has_credentials: True when live search is enabled with valid API key."""
        return self.has_credentials

    async def execute_search(
        self,
        engine: str,
        params: dict[str, Any],
        max_retries: int = 3,
    ) -> dict[str, Any]:
        if engine in ("trains", "train_corridors", "buses", "bus_corridors"):
            raise ValueError(f"Engine '{engine}' is an offline transit catalog and cannot be queried via SerpApi.")

        if not self.has_credentials:
            raise SerpApiAuthError(
                "SerpApi API key is not configured. Set SERPAPI_API_KEY in .env."
            )

        # Instrumentation: log call attempt boundary (no secrets, no user data)
        logger.info(
            "[SERPAPI] provider=serpapi operation=%s outcome=attempting call_type=live",
            engine,
        )

        # SerpApi google_maps: when using 'location', SerpApi requires either 'z' or 'm' parameter
        search_params = dict(params)
        if engine == "google_maps" and "location" in search_params and not ("m" in search_params or "z" in search_params):
            from budlance.config import get_settings
            search_params["m"] = get_settings().maps_search_radius_meters

        keys_to_try: list[str] = []
        if self._api_key and self._api_key.strip():
            keys_to_try.append(self._api_key.strip())
        if (
            self._fallback_api_key
            and self._fallback_api_key.strip()
            and self._fallback_api_key.strip() not in keys_to_try
        ):
            keys_to_try.append(self._fallback_api_key.strip())

        if not keys_to_try:
            raise SerpApiAuthError(
                "SerpApi API key is not configured. Set SERPAPI_API_KEY in .env."
            )

        start_time = time.perf_counter()
        total_attempts = 0
        last_error: Exception | None = None

        for key_idx, current_api_key in enumerate(keys_to_try):
            # Merge engine and API key into request payload (key excluded from logs)
            request_params = {
                "api_key": current_api_key,
                "engine": engine,
                **search_params,
            }

            attempt_counter = {"count": 0}

            async def _single_call() -> dict[str, Any]:
                nonlocal total_attempts
                attempt_counter["count"] += 1
                total_attempts += 1
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
                    if (
                        response.status_code == 400
                        and engine == "google_maps"
                        and "location" in request_params
                        and "q" in request_params
                        and "unsupported" in response.text.lower()
                    ):
                        logger.info(
                            "[SERPAPI] google_maps location '%s' unsupported by SerpApi. Retrying with query 'q' only.",
                            request_params.get("location"),
                        )
                        fallback_params = dict(request_params)
                        fallback_params.pop("location", None)
                        fallback_params.pop("m", None)
                        fallback_params.pop("z", None)
                        async with httpx.AsyncClient(timeout=25.0) as retry_client:
                            retry_resp = await retry_client.get(SERPAPI_BASE_URL, params=fallback_params)
                            if retry_resp.status_code == 200:
                                data = retry_resp.json()
                                if isinstance(data, dict) and "error" not in data:
                                    return data
                    logger.warning(
                        "[SERPAPI] provider=serpapi operation=%s outcome=http_error http_status=%d",
                        engine,
                        response.status_code,
                    )
                    raise SerpApiResponseError(
                        f"SerpApi error HTTP {response.status_code}: {response.text[:200]}"
                    )

                data = response.json()
                if isinstance(data, dict) and "error" in data:
                    err_msg = str(data["error"])
                    if "Invalid API key" in err_msg:
                        logger.warning(
                            "[SERPAPI] provider=serpapi operation=%s outcome=invalid_key",
                            engine,
                        )
                        raise SerpApiAuthError("SerpApi reports: Invalid API key")
                    if "rate limit" in err_msg.lower() or "quota" in err_msg.lower():
                        logger.warning(
                            "[SERPAPI] provider=serpapi operation=%s outcome=quota_exceeded",
                            engine,
                        )
                        raise SerpApiRateLimitError(f"SerpApi quota exceeded: {err_msg}")
                    raise SerpApiResponseError(f"SerpApi API error: {err_msg}")

                logger.info(
                    "[SERPAPI] provider=serpapi operation=%s outcome=success attempts_used=%d",
                    engine,
                    attempt_counter["count"],
                )
                return data

            try:
                result = await retry_with_backoff(_single_call, max_retries=max_retries)
                self.telemetry.record_call(
                    engine=engine,
                    params=params,
                    status="success",
                    attempts=total_attempts,
                    latency_sec=time.perf_counter() - start_time,
                )
                return result
            except (SerpApiAuthError, SerpApiRateLimitError) as exc:
                last_error = exc
                if key_idx + 1 < len(keys_to_try):
                    logger.warning(
                        "[SERPAPI] provider=serpapi operation=%s outcome=failover_to_fallback reason=%s",
                        engine,
                        type(exc).__name__,
                    )
                    continue
                self.telemetry.record_call(
                    engine=engine,
                    params=params,
                    status="failed",
                    attempts=max(1, total_attempts),
                    latency_sec=time.perf_counter() - start_time,
                    error=str(exc),
                )
                raise
            except Exception as exc:
                self.telemetry.record_call(
                    engine=engine,
                    params=params,
                    status="failed",
                    attempts=max(1, total_attempts),
                    latency_sec=time.perf_counter() - start_time,
                    error=str(exc),
                )
                raise

        if last_error:
            self.telemetry.record_call(
                engine=engine,
                params=params,
                status="failed",
                attempts=max(1, total_attempts),
                latency_sec=time.perf_counter() - start_time,
                error=str(last_error),
            )
            raise last_error
        raise SerpApiAuthError("SerpApi API key is not configured. Set SERPAPI_API_KEY in .env.")

    def get_telemetry_summary(self) -> dict[str, Any]:
        """Return the current SerpApi telemetry summary."""
        return self.telemetry.get_summary()

    # =========================================================================
    # Verified Engine Methods
    # =========================================================================
    async def search_travel_explore(self, params: dict[str, Any]) -> dict[str, Any]:
        """Destination discovery via Google Travel Explore."""
        return await self.execute_search("google_travel_explore", params)

    async def search_flights(self, params: dict[str, Any]) -> dict[str, Any]:
        """Flight schedules and fares via Google Flights."""
        return await self.execute_search("google_flights", params)

    async def get_flight_booking_options(self, booking_token: str) -> dict[str, Any]:
        """Booking options for a selected flight using its booking_token via Google Flights."""
        return await self.execute_search("google_flights", {"booking_token": booking_token})

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
