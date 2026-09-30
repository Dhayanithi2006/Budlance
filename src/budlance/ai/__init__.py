"""AI Intent Layer package for Budlance powered by OpenRouter."""

from budlance.ai.client import GeminiClient, OpenRouterClient
from budlance.ai.exceptions import (
    AIIntentError,
    OpenRouterAuthError,
    OpenRouterNetworkError,
    OpenRouterResponseError,
    OpenRouterValidationError,
)
from budlance.ai.schemas import ParsedRescueIntent, ParsedTripIntent
from budlance.ai.service import AIIntentService

__all__ = [
    "OpenRouterClient",
    "GeminiClient",
    "AIIntentService",
    "ParsedTripIntent",
    "ParsedRescueIntent",
    "AIIntentError",
    "OpenRouterAuthError",
    "OpenRouterNetworkError",
    "OpenRouterResponseError",
    "OpenRouterValidationError",
]
