"""AI client for calling LLM inference APIs (OpenRouter and Google Gemini)."""

import asyncio
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
        self._api_key = settings.openrouter_api_key if api_key is None else api_key
        self.model = settings.openrouter_model if model is None else model
        self._http_client = http_client

    @property
    def has_credentials(self) -> bool:
        """Check if OPENROUTER_API_KEY is configured."""
        return bool(self._api_key and self._api_key.strip())

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.1,
    ) -> dict[str, Any]:
        """Execute a chat completion requesting JSON output with retry for transient rate-limits."""
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

        max_retries = 3

        try:
            for attempt in range(1, max_retries + 1):
                logger.info(
                    "[OPENROUTER] provider=openrouter operation=chat_completion model=%s outcome=attempting call_type=live attempt=%d",
                    self.model,
                    attempt,
                )
                try:
                    response = await client.post(OPENROUTER_API_URL, headers=headers, json=payload)
                except httpx.TimeoutException as exc:
                    if attempt < max_retries:
                        await asyncio.sleep(1.5 * attempt)
                        continue
                    raise OpenRouterNetworkError(f"OpenRouter request timed out: {exc}") from exc
                except httpx.RequestError as exc:
                    if attempt < max_retries:
                        await asyncio.sleep(1.5 * attempt)
                        continue
                    raise OpenRouterNetworkError(f"OpenRouter network communication error: {exc}") from exc

                if response.status_code in (401, 403):
                    logger.warning(
                        "[OPENROUTER] provider=openrouter operation=chat_completion model=%s outcome=auth_failure http_status=%d",
                        self.model,
                        response.status_code,
                    )
                    raise OpenRouterAuthError("OpenRouter authentication failed. Please verify OPENROUTER_API_KEY.")

                if response.status_code in (429, 503):
                    logger.warning(
                        "[OPENROUTER] provider=openrouter operation=chat_completion model=%s outcome=rate_limited http_status=%d attempt=%d",
                        self.model,
                        response.status_code,
                        attempt,
                    )
                    if attempt < max_retries:
                        await asyncio.sleep(2.0 * attempt)
                        continue
                    raise OpenRouterResponseError(
                        f"OpenRouter API returned HTTP {response.status_code} after {max_retries} attempts: {response.text[:200]}"
                    )

                if response.status_code != 200:
                    logger.warning(
                        "[OPENROUTER] provider=openrouter operation=chat_completion model=%s outcome=http_error http_status=%d",
                        self.model,
                        response.status_code,
                    )
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
                    result = json.loads(content_str)
                    logger.info(
                        "[OPENROUTER] provider=openrouter operation=chat_completion model=%s outcome=success",
                        self.model,
                    )
                    return result
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
                        result = json.loads(cleaned.strip())
                        logger.info(
                            "[OPENROUTER] provider=openrouter operation=chat_completion model=%s outcome=success",
                            self.model,
                        )
                        return result
                    except json.JSONDecodeError:
                        raise OpenRouterResponseError(
                            f"Model output was not valid JSON: {content_str[:200]}"
                        ) from exc

        finally:
            if should_close:
                await client.aclose()

        raise OpenRouterResponseError(f"OpenRouter call failed after {max_retries} attempts.")


class GeminiClient:
    """Async client dedicated to communicating with Google Gemini API."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self._api_key = settings.gemini_api_key if api_key is None else api_key
        self.model = model or settings.gemini_model or "gemini-flash-latest"
        self._http_client = http_client

    @property
    def has_credentials(self) -> bool:
        """Check if GEMINI_API_KEY is configured."""
        return bool(self._api_key and self._api_key.strip())

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.1,
    ) -> dict[str, Any]:
        """Execute chat completion using Google Gemini API with JSON output mode."""
        if not self.has_credentials:
            raise OpenRouterAuthError(
                "Gemini API key is not configured. Set GEMINI_API_KEY in .env."
            )

        logger.info(
            "[GEMINI] provider=google_gemini operation=generate_content model=%s outcome=attempting call_type=live",
            self.model,
        )

        system_parts = []
        user_parts = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")
            if role == "system":
                system_parts.append(content)
            else:
                user_parts.append(content)

        prompt_text = "\n\n".join(system_parts + user_parts) if system_parts else "\n\n".join(user_parts)

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
            "generationConfig": {
                "response_mime_type": "application/json",
                "temperature": temperature,
            },
        }

        should_close = False
        client = self._http_client
        if client is None:
            client = httpx.AsyncClient(timeout=30.0)
            should_close = True

        max_retries = 3
        last_error = None

        try:
            for attempt in range(1, max_retries + 1):
                try:
                    response = await client.post(
                        url,
                        headers={
                            "Content-Type": "application/json",
                            "x-goog-api-key": self._api_key.strip(),
                        },
                        json=payload,
                    )
                    if response.status_code in (401, 403):
                        logger.warning(
                            "[GEMINI] provider=google_gemini outcome=auth_failure http_status=%d",
                            response.status_code,
                        )
                        raise OpenRouterAuthError("Gemini authentication failed. Please verify GEMINI_API_KEY.")
                    if response.status_code in (429, 503):
                        logger.warning(
                            "[GEMINI] provider=google_gemini outcome=transient_rate_or_service_unavailable http_status=%d attempt=%d",
                            response.status_code,
                            attempt,
                        )
                        if attempt < max_retries:
                            await asyncio.sleep(1.5 * attempt)
                            continue
                        raise OpenRouterResponseError(f"Gemini API returned HTTP {response.status_code}: {response.text[:200]}")
                    if response.status_code != 200:
                        raise OpenRouterResponseError(f"Gemini API returned HTTP {response.status_code}: {response.text[:200]}")

                    data = response.json()
                    candidates = data.get("candidates")
                    if not candidates:
                        raise OpenRouterResponseError("Gemini response did not contain candidates.")
                    content_str = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                    if not content_str:
                        raise OpenRouterResponseError("Gemini response candidate was empty.")

                    cleaned = content_str.strip()
                    if cleaned.startswith("```json"):
                        cleaned = cleaned[7:]
                    if cleaned.startswith("```"):
                        cleaned = cleaned[3:]
                    if cleaned.endswith("```"):
                        cleaned = cleaned[:-3]

                    result = json.loads(cleaned.strip())
                    logger.info(
                        "[GEMINI] provider=google_gemini operation=generate_content model=%s outcome=success",
                        self.model,
                    )
                    return result

                except (httpx.TimeoutException, httpx.RequestError) as exc:
                    last_error = exc
                    if attempt < max_retries:
                        await asyncio.sleep(1.5 * attempt)
                        continue
                    raise OpenRouterNetworkError(f"Gemini network error: {exc}") from exc

            raise OpenRouterResponseError(f"Gemini call failed after {max_retries} attempts: {last_error}")
        finally:
            if should_close:
                await client.aclose()
