"""Bounded retry with exponential backoff for transient SerpApi failures."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar
import httpx
from budlance.serpapi.exceptions import (
    SerpApiAuthError,
    SerpApiNetworkError,
    SerpApiRateLimitError,
    SerpApiResponseError,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


def is_transient_error(exc: Exception) -> bool:
    """Determine whether an error is transient and eligible for retry."""
    if isinstance(exc, (SerpApiAuthError, ValueError)):
        return False

    if isinstance(exc, (SerpApiNetworkError, SerpApiRateLimitError)):
        return True

    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.ConnectTimeout)):
        return True

    if isinstance(exc, SerpApiResponseError):
        # 5xx server errors are retryable; 4xx client errors (except 429) are not
        err_msg = str(exc)
        if any(f"HTTP {code}" in err_msg for code in [500, 502, 503, 504]):
            return True

    return False


async def retry_with_backoff(
    operation: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    initial_delay: float = 0.5,
    backoff_factor: float = 2.0,
    max_delay: float = 5.0,
) -> T:
    """Execute an async operation with bounded exponential backoff on transient errors."""
    delay = initial_delay

    for attempt in range(1, max_retries + 1):
        try:
            return await operation()
        except Exception as exc:
            if not is_transient_error(exc) or attempt == max_retries:
                logger.warning(
                    "Operation failed permanently on attempt %d/%d (%s: %s)",
                    attempt,
                    max_retries,
                    type(exc).__name__,
                    exc,
                )
                raise

            logger.info(
                "Transient error on attempt %d/%d (%s: %s). Retrying in %.2fs...",
                attempt,
                max_retries,
                type(exc).__name__,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * backoff_factor, max_delay)

    raise RuntimeError("Retry loop exited unexpectedly")
