"""Exceptions for the OpenRouter AI Intent Layer."""


class AIIntentError(Exception):
    """Base exception for AI intent extraction errors."""


class OpenRouterAuthError(AIIntentError):
    """Raised when OpenRouter API credentials are missing or invalid."""


class OpenRouterNetworkError(AIIntentError):
    """Raised when an outbound network call to OpenRouter fails or times out."""


class OpenRouterResponseError(AIIntentError):
    """Raised when OpenRouter returns an error code or malformed JSON."""


class OpenRouterValidationError(AIIntentError):
    """Raised when the model response fails Pydantic schema validation."""
