"""Exceptions for SerpApi Gateway and Travel Data Layer."""


class SerpApiError(Exception):
    """Base exception for SerpApi Gateway errors."""


class SerpApiAuthError(SerpApiError):
    """Raised when SERPAPI_API_KEY is missing, unauthorized, or invalid."""


class SerpApiNetworkError(SerpApiError):
    """Raised on connection drop, network unreachable, or request timeouts."""


class SerpApiRateLimitError(SerpApiError):
    """Raised when SerpApi rate limit or monthly quota is exceeded."""


class SerpApiResponseError(SerpApiError):
    """Raised when SerpApi returns an unexpected HTTP status code or error payload."""
