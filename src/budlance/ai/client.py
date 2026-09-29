"""OpenRouter client for calling LLM inference APIs."""

import json
import logging
from typing import Any
import httpx

from budlance.config import get_settings
from budlance.ai.exceptions import (
    OpenRouterAuthError,
    OpenRouterNetworkError,
    OpenRouterResponseError,
)

logger = logging.getLogger(__name__)

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterClient:
    """Async client dedicated to communicating with OpenRouter."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self._api_key = api_key or settings.openrouter_api_key
        self.model = model or settings.openrouter_model
        self._http_client = http_client

    @property
    def has_credentials(self) -> bool:
        """Check if an API key is configured."""
        return bool(self._api_key and self._api_key.strip())

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.1,
    ) -> dict[str, Any]:
        """Execute a chat completion on OpenRouter requesting JSON output."""
        if not self.has_credentials:
            raise OpenRouterAuthError(
                "OpenRouter API key is not configured. Set OPENROUTER_API_KEY in .env."
            )

        headers = {
            "Authorization": f"Bearer {self._api_key.strip()}",
            "HTTP-Referer": "https://budlance.app",
            "X-Title": "Budlance",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": temperature,
        }

        should_close = False
        client = self._http_client
        if client is None:
            client = httpx.AsyncClient(timeout=30.0)
            should_close = True

        try:
            response = await client.post(OPENROUTER_API_URL, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise OpenRouterNetworkError(f"OpenRouter request timed out: {exc}") from exc
        except httpx.RequestError as exc:
            raise OpenRouterNetworkError(f"OpenRouter network communication error: {exc}") from exc
        finally:
            if should_close:
                await client.aclose()

        if response.status_code in (401, 403):
            raise OpenRouterAuthError("OpenRouter authentication failed. Please verify OPENROUTER_API_KEY.")
        if response.status_code != 200:
            raise OpenRouterResponseError(
                f"OpenRouter API returned HTTP {response.status_code}: {response.text[:200]}"
            )

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise OpenRouterResponseError("Failed to decode JSON from OpenRouter response.") from exc

        choices = data.get("choices")
        if not choices or not isinstance(choices, list):
            raise OpenRouterResponseError("OpenRouter response did not contain valid choices.")

        content_str = choices[0].get("message", {}).get("content", "")
        if not content_str:
            raise OpenRouterResponseError("OpenRouter response message content was empty.")

        # Parse model's JSON string output
        try:
            return json.loads(content_str)
        except json.JSONDecodeError as exc:
            # Attempt to strip potential markdown code fence wrapping if present
            cleaned = content_str.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            try:
                return json.loads(cleaned.strip())
            except json.JSONDecodeError:
                raise OpenRouterResponseError(
                    f"Model output was not valid JSON: {content_str[:200]}"
                ) from exc
