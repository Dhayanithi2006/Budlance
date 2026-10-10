"""AI client for calling LLM inference APIs (OpenRouter and Google Gemini)."""

import asyncio
import json
import logging
import time
from typing import Any, Callable
import httpx

from budlance.config import get_settings
from budlance.ai.exceptions import (
    OpenRouterAuthError,
    OpenRouterNetworkError,
    OpenRouterResponseError,
    OpenRouterValidationError,
)
from budlance.ai.telemetry import AITelemetry

logger = logging.getLogger(__name__)

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterClient:
    """Async client dedicated to communicating with OpenRouter with model-level fallback."""

    def __init__(
        self,
        api_key: str | None = None,
        fallback_api_key: str | None = None,
        model: str | None = None,
        fallback_models: list[str] | None = None,
        http_client: httpx.AsyncClient | None = None,
        telemetry: AITelemetry | None = None,
        request_timeout: float | None = None,
    ) -> None:
        settings = get_settings()
        self._api_key = settings.openrouter_api_key if api_key is None else api_key
        self._fallback_api_key = (
            settings.openrouter_fallback_api_key
            if fallback_api_key is None
            else fallback_api_key
        )
        self.model = settings.openrouter_model if model is None else model
        self.fallback_models = (
            list(fallback_models)
            if fallback_models is not None
            else list(settings.openrouter_fallback_models)
        )
        self._http_client = http_client
        self.telemetry = telemetry or AITelemetry()
        self.request_timeout = (
            settings.openrouter_request_timeout_seconds
            if request_timeout is None
            else request_timeout
        )

    @property
    def has_credentials(self) -> bool:
        """Check if OPENROUTER_API_KEY or fallback key is configured."""
        return bool(
            (self._api_key and self._api_key.strip())
            or (self._fallback_api_key and self._fallback_api_key.strip())
        )

    def get_candidate_models(self) -> list[str]:
        """Return ordered list of models: primary followed by fallbacks without duplicates."""
        candidates = [self.model.strip()] if self.model and self.model.strip() else []
        for fb in self.fallback_models:
            clean_fb = fb.strip()
            if clean_fb and clean_fb not in candidates:
                candidates.append(clean_fb)

        if not self.fallback_models:
            settings = get_settings()
            legacy_fb = getattr(settings, "openrouter_fallback_model", None)
            if legacy_fb and legacy_fb.strip() and legacy_fb.strip() not in candidates:
                candidates.append(legacy_fb.strip())
        return candidates

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.1,
        response_validator: Callable[[dict[str, Any]], None] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Execute chat completion with ordered model-level fallback.

        Cascade:
          Primary Model (OPENROUTER_MODEL)
            -> Fallback Model 1
            -> Fallback Model 2
            -> raises OpenRouterResponseError (enabling heuristic fallback)
        """
        if not self.has_credentials:
            raise OpenRouterAuthError(
                "OpenRouter API key is not configured. Set OPENROUTER_API_KEY in .env."
            )

        keys_to_try: list[str] = []
        if self._api_key and self._api_key.strip():
            keys_to_try.append(self._api_key.strip())
        if (
            self._fallback_api_key
            and self._fallback_api_key.strip()
            and self._fallback_api_key.strip() not in keys_to_try
        ):
            keys_to_try.append(self._fallback_api_key.strip())

        candidate_models = self.get_candidate_models()
        if not candidate_models:
            candidate_models = ["google/gemma-4-26b-a4b-it:free"]

        should_close = False
        client = self._http_client
        if client is None:
            client = httpx.AsyncClient(timeout=self.request_timeout)
            should_close = True

        start_time = time.perf_counter()
        attempts_log: list[dict[str, Any]] = []
        http_attempts = 0
        models_attempted = 0
        successful_result: dict[str, Any] | None = None
        selected_model: str | None = None
        selected_candidate_index: int | None = None
        actual_provider_model: str | None = None
        fallback_used = False
        fallback_index: int | None = None
        last_error: Exception | None = None

        try:
            for model_idx, active_model in enumerate(candidate_models):
                models_attempted += 1
                payload = {
                    "model": active_model,
                    "messages": messages,
                    "response_format": {"type": "json_object"},
                    "temperature": temperature,
                }

                model_succeeded = False

                for key_idx, active_key in enumerate(keys_to_try):
                    headers = {
                        "Authorization": f"Bearer {active_key}",
                        "HTTP-Referer": "https://budlance.app",
                        "X-Title": "Budlance",
                        "Content-Type": "application/json",
                    }
                    http_attempts += 1
                    attempt_start = time.perf_counter()

                    logger.info(
                        "[OPENROUTER] provider=openrouter operation=chat_completion model=%s "
                        "outcome=attempting call_type=live model_idx=%d key_idx=%d attempt=%d",
                        active_model,
                        model_idx,
                        key_idx,
                        http_attempts,
                    )

                    try:
                        response = await asyncio.wait_for(
                            client.post(OPENROUTER_API_URL, headers=headers, json=payload),
                            timeout=self.request_timeout,
                        )
                    except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
                        attempt_latency = (time.perf_counter() - attempt_start) * 1000
                        last_error = OpenRouterNetworkError(
                            f"OpenRouter request timed out for model {active_model}: {exc}"
                        )
                        attempts_log.append({
                            "model": active_model,
                            "requested_model": active_model,
                            "actual_model": None,
                            "status": "timeout",
                            "api_key_index": key_idx,
                            "key_idx": key_idx,
                            "latency_ms": round(attempt_latency, 1),
                            "error": str(exc),
                        })
                        logger.warning("[OPENROUTER] Timeout for model=%s: %s", active_model, exc)
                        # Advance to next model on timeout
                        break
                    except httpx.RequestError as exc:
                        attempt_latency = (time.perf_counter() - attempt_start) * 1000
                        last_error = OpenRouterNetworkError(
                            f"OpenRouter network communication error: {exc}"
                        )
                        attempts_log.append({
                            "model": active_model,
                            "requested_model": active_model,
                            "actual_model": None,
                            "status": "network_error",
                            "api_key_index": key_idx,
                            "key_idx": key_idx,
                            "latency_ms": round(attempt_latency, 1),
                            "error": str(exc),
                        })
                        logger.warning("[OPENROUTER] Network error for model=%s: %s", active_model, exc)
                        # Advance to next model on connection error
                        break

                    attempt_latency = (time.perf_counter() - attempt_start) * 1000

                    # 1. Auth failure (401, 403)
                    if response.status_code in (401, 403):
                        logger.warning(
                            "[OPENROUTER] provider=openrouter operation=chat_completion model=%s "
                            "outcome=auth_failure http_status=%d key_idx=%d",
                            active_model,
                            response.status_code,
                            key_idx,
                        )
                        last_error = OpenRouterAuthError(
                            "OpenRouter authentication failed. Please verify OPENROUTER_API_KEY."
                        )
                        attempts_log.append({
                            "model": active_model,
                            "requested_model": active_model,
                            "actual_model": None,
                            "status": "auth_failure",
                            "http_status": response.status_code,
                            "api_key_index": key_idx,
                            "key_idx": key_idx,
                            "latency_ms": round(attempt_latency, 1),
                        })
                        if key_idx + 1 < len(keys_to_try):
                            logger.info("[OPENROUTER] Failing over to secondary API key.")
                            continue
                        # If all keys are unauthorized, fail immediately without looping models
                        raise last_error

                    # 2. Non-retryable client errors (400, 422)
                    if response.status_code in (400, 422):
                        logger.error(
                            "[OPENROUTER] provider=openrouter operation=chat_completion model=%s "
                            "outcome=client_error http_status=%d: %s",
                            active_model,
                            response.status_code,
                            response.text[:200],
                        )
                        last_error = OpenRouterResponseError(
                            f"OpenRouter client error HTTP {response.status_code}: {response.text[:200]}"
                        )
                        attempts_log.append({
                            "model": active_model,
                            "requested_model": active_model,
                            "actual_model": None,
                            "status": "client_error",
                            "http_status": response.status_code,
                            "api_key_index": key_idx,
                            "key_idx": key_idx,
                            "latency_ms": round(attempt_latency, 1),
                            "error": response.text[:200],
                        })
                        # Malformed request caused by client code: do not blindly loop models
                        raise last_error

                    # 3. Rate limited (429) or upstream server error (500, 502, 503, 504)
                    if response.status_code in (429, 500, 502, 503, 504):
                        outcome_str = "rate_limited" if response.status_code == 429 else "upstream_error"
                        logger.warning(
                            "[OPENROUTER] provider=openrouter operation=chat_completion model=%s "
                            "outcome=%s http_status=%d key_idx=%d attempt=%d",
                            active_model,
                            outcome_str,
                            response.status_code,
                            key_idx,
                            http_attempts,
                        )
                        last_error = OpenRouterResponseError(
                            f"OpenRouter API returned HTTP {response.status_code} for {active_model}: {response.text[:200]}"
                        )
                        attempts_log.append({
                            "model": active_model,
                            "requested_model": active_model,
                            "actual_model": None,
                            "status": outcome_str,
                            "http_status": response.status_code,
                            "api_key_index": key_idx,
                            "key_idx": key_idx,
                            "latency_ms": round(attempt_latency, 1),
                            "error": response.text[:200],
                        })
                        # If a secondary API key is available, try it before moving to the next model
                        if key_idx + 1 < len(keys_to_try):
                            logger.info(
                                "[OPENROUTER] provider=openrouter operation=chat_completion "
                                "outcome=failover_to_fallback reason=RateLimit key_idx=%d",
                                key_idx + 1,
                            )
                            continue
                        # Advance directly to the next model in candidate_models
                        break

                    # 4. Other non-200 HTTP responses
                    if response.status_code != 200:
                        logger.warning(
                            "[OPENROUTER] provider=openrouter operation=chat_completion model=%s "
                            "outcome=http_error http_status=%d key_idx=%d",
                            active_model,
                            response.status_code,
                            key_idx,
                        )
                        last_error = OpenRouterResponseError(
                            f"OpenRouter API returned HTTP {response.status_code}: {response.text[:200]}"
                        )
                        attempts_log.append({
                            "model": active_model,
                            "requested_model": active_model,
                            "actual_model": None,
                            "status": "http_error",
                            "http_status": response.status_code,
                            "api_key_index": key_idx,
                            "key_idx": key_idx,
                            "latency_ms": round(attempt_latency, 1),
                            "error": response.text[:200],
                        })
                        break

                    # 5. HTTP 200 OK — decode envelope
                    try:
                        data = response.json()
                    except json.JSONDecodeError as exc:
                        last_error = OpenRouterResponseError("Failed to decode JSON from OpenRouter response.")
                        attempts_log.append({
                            "model": active_model,
                            "requested_model": active_model,
                            "actual_model": None,
                            "status": "envelope_json_error",
                            "http_status": response.status_code,
                            "api_key_index": key_idx,
                            "key_idx": key_idx,
                            "latency_ms": round(attempt_latency, 1),
                            "error": str(exc),
                        })
                        break

                    current_actual_model = data.get("model")

                    choices = data.get("choices")
                    if not choices or not isinstance(choices, list):
                        last_error = OpenRouterResponseError("OpenRouter response did not contain valid choices.")
                        attempts_log.append({
                            "model": active_model,
                            "requested_model": active_model,
                            "actual_model": current_actual_model,
                            "status": "empty_choices",
                            "http_status": response.status_code,
                            "api_key_index": key_idx,
                            "key_idx": key_idx,
                            "latency_ms": round(attempt_latency, 1),
                        })
                        break

                    content_str = choices[0].get("message", {}).get("content", "")
                    if not content_str:
                        last_error = OpenRouterResponseError("OpenRouter response message content was empty.")
                        attempts_log.append({
                            "model": active_model,
                            "requested_model": active_model,
                            "actual_model": current_actual_model,
                            "status": "empty_content",
                            "http_status": response.status_code,
                            "api_key_index": key_idx,
                            "key_idx": key_idx,
                            "latency_ms": round(attempt_latency, 1),
                        })
                        break

                    # Extract JSON from model text content
                    cleaned = content_str.strip()
                    if cleaned.startswith("```json"):
                        cleaned = cleaned[7:]
                    if cleaned.startswith("```"):
                        cleaned = cleaned[3:]
                    if cleaned.endswith("```"):
                        cleaned = cleaned[:-3]

                    try:
                        result = json.loads(cleaned.strip())
                    except json.JSONDecodeError as exc:
                        last_error = OpenRouterResponseError(
                            f"Model output was not valid JSON: {content_str[:200]}"
                        )
                        attempts_log.append({
                            "model": active_model,
                            "requested_model": active_model,
                            "actual_model": current_actual_model,
                            "status": "malformed_json",
                            "http_status": response.status_code,
                            "api_key_index": key_idx,
                            "key_idx": key_idx,
                            "latency_ms": round(attempt_latency, 1),
                            "error": str(exc),
                        })
                        logger.warning("[OPENROUTER] Malformed JSON from model=%s. Moving to next fallback.", active_model)
                        break

                    # Optional structured validation callback
                    if response_validator is not None:
                        try:
                            response_validator(result)
                        except Exception as val_exc:
                            last_error = OpenRouterValidationError(
                                f"Structured validation failed for model {active_model}: {val_exc}"
                            )
                            attempts_log.append({
                                "model": active_model,
                                "requested_model": active_model,
                                "actual_model": current_actual_model,
                                "status": "validation_failed",
                                "http_status": response.status_code,
                                "api_key_index": key_idx,
                                "key_idx": key_idx,
                                "latency_ms": round(attempt_latency, 1),
                                "error": str(val_exc),
                            })
                            logger.warning(
                                "[OPENROUTER] Pydantic validation failed for model=%s (%s). Moving to next fallback.",
                                active_model,
                                val_exc,
                            )
                            break

                    # Successful response & verified structured JSON
                    # Selected model MUST strictly match candidate_models[fallback_index]
                    selected_model = candidate_models[model_idx]
                    selected_candidate_index = model_idx
                    actual_provider_model = current_actual_model
                    fallback_used = (model_idx > 0)
                    fallback_index = model_idx
                    successful_result = result
                    attempts_log.append({
                        "model": active_model,
                        "requested_model": active_model,
                        "actual_model": actual_provider_model,
                        "selected_model": selected_model,
                        "status": "success",
                        "http_status": response.status_code,
                        "api_key_index": key_idx,
                        "key_idx": key_idx,
                        "latency_ms": round(attempt_latency, 1),
                    })
                    logger.info(
                        "[OPENROUTER] provider=openrouter operation=chat_completion model=%s actual_provider=%s "
                        "selected=%s outcome=success fallback_used=%s fallback_idx=%d",
                        active_model,
                        actual_provider_model,
                        selected_model,
                        fallback_used,
                        fallback_index,
                    )
                    model_succeeded = True
                    break

                if model_succeeded:
                    break

        finally:
            if should_close:
                await client.aclose()

        elapsed_ms = (time.perf_counter() - start_time) * 1000

        if successful_result is not None:
            self.telemetry.record_run(
                primary_model=candidate_models[0],
                candidate_models=candidate_models,
                selected_model=selected_model,
                selected_candidate_index=selected_candidate_index,
                actual_provider_model=actual_provider_model,
                fallback_used=fallback_used,
                fallback_index=selected_candidate_index,
                models_attempted=models_attempted,
                models_exhausted=False,
                http_attempts=http_attempts,
                total_attempts=http_attempts,
                failure_reason=None,
                heuristic_used=False,
                latency_ms=elapsed_ms,
                attempts=attempts_log,
            )
            return successful_result

        # All models in fallback list failed
        failure_msg = str(last_error) if last_error else "All configured OpenRouter models failed."
        self.telemetry.record_run(
            primary_model=candidate_models[0],
            candidate_models=candidate_models,
            selected_model=None,
            selected_candidate_index=None,
            actual_provider_model=None,
            fallback_used=True,
            fallback_index=None,
            models_attempted=models_attempted,
            models_exhausted=True,
            http_attempts=http_attempts,
            total_attempts=http_attempts,
            failure_reason=failure_msg,
            heuristic_used=True,
            latency_ms=elapsed_ms,
            attempts=attempts_log,
        )

        if last_error:
            raise last_error
        raise OpenRouterResponseError(failure_msg)


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
        response_validator: Callable[[dict[str, Any]], None] | None = None,
        **kwargs: Any,
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
