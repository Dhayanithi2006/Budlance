"""Deterministic AI telemetry tracking and inference accounting for Budlance."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AIModelAttemptRecord:
    """Record of an individual attempt against a specific LLM model."""
    requested_model: str
    actual_model: str | None = None
    status: str = "unknown"  # "success", "rate_limited", "http_error", "timeout", "validation_failed"
    http_status: int | None = None
    error_message: str | None = None
    attempt_number: int = 1
    api_key_index: int = 0
    key_idx: int = 0
    latency_ms: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def model(self) -> str:
        """Alias for backward compatibility."""
        return self.requested_model


@dataclass
class AIRunSummary:
    """Telemetry report for a complete logical AI intent extraction pass."""
    ai_primary_model: str
    candidate_models: list[str] = field(default_factory=list)
    ai_selected_model: str | None = None
    selected_candidate_index: int | None = None
    ai_actual_provider_model: str | None = None
    ai_fallback_used: bool = False
    ai_fallback_index: int | None = None
    models_attempted: int = 0
    models_exhausted: bool = False
    http_attempts: int = 0
    ai_failure_reason: str | None = None
    ai_total_attempts: int = 0
    ai_heuristic_used: bool = False
    ai_latency_ms: float = 0.0
    attempts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def selected_model(self) -> str | None:
        return self.ai_selected_model

    @property
    def fallback_index(self) -> int | None:
        return self.selected_candidate_index

    @property
    def fallback_used(self) -> bool:
        return self.ai_fallback_used

    @property
    def heuristic_used(self) -> bool:
        return self.ai_heuristic_used

    @property
    def model_candidates_attempted(self) -> int:
        return self.models_attempted

    @property
    def model_candidates_exhausted(self) -> bool:
        return self.models_exhausted


class AITelemetry:
    """Deterministic, thread-safe telemetry collector for AI Intent Service calls.

    Tracks model-level fallbacks, rate-limit failovers, validation rejects,
    and heuristic usage without masking original errors.
    """

    def __init__(self) -> None:
        self.last_run: AIRunSummary | None = None
        self._history: list[AIRunSummary] = []

    def record_run(
        self,
        primary_model: str,
        selected_model: str | None,
        fallback_used: bool,
        fallback_index: int | None,
        failure_reason: str | None,
        total_attempts: int,
        heuristic_used: bool,
        latency_ms: float,
        attempts: list[dict[str, Any]],
        actual_provider_model: str | None = None,
        candidate_models: list[str] | None = None,
        selected_candidate_index: int | None = None,
        models_attempted: int | None = None,
        models_exhausted: bool | None = None,
        http_attempts: int | None = None,
    ) -> AIRunSummary:
        """Record telemetry for a completed intent resolution run."""
        resolved_candidate_idx = (
            selected_candidate_index
            if selected_candidate_index is not None
            else (fallback_index if selected_model is not None else None)
        )
        resolved_models_exhausted = (
            models_exhausted
            if models_exhausted is not None
            else (selected_model is None and heuristic_used)
        )
        resolved_http_attempts = (
            http_attempts if http_attempts is not None else total_attempts
        )
        resolved_models_attempted = (
            models_attempted
            if models_attempted is not None
            else (len(candidate_models) if candidate_models else total_attempts)
        )

        summary = AIRunSummary(
            ai_primary_model=primary_model,
            candidate_models=list(candidate_models) if candidate_models else [primary_model],
            ai_selected_model=selected_model,
            selected_candidate_index=resolved_candidate_idx,
            ai_actual_provider_model=actual_provider_model,
            ai_fallback_used=fallback_used,
            ai_fallback_index=resolved_candidate_idx,
            models_attempted=resolved_models_attempted,
            models_exhausted=resolved_models_exhausted,
            http_attempts=resolved_http_attempts,
            ai_failure_reason=failure_reason,
            ai_total_attempts=resolved_http_attempts,
            ai_heuristic_used=heuristic_used,
            ai_latency_ms=round(latency_ms, 2),
            attempts=list(attempts),
        )
        self.last_run = summary
        self._history.append(summary)

        logger.info(
            "[AI_TELEMETRY] primary=%s selected=%s candidate_idx=%s actual_provider=%s fallback_used=%s "
            "models_attempted=%d models_exhausted=%s http_attempts=%d heuristic_used=%s latency_ms=%.1f failure=%s",
            primary_model,
            selected_model,
            resolved_candidate_idx,
            actual_provider_model,
            fallback_used,
            resolved_models_attempted,
            resolved_models_exhausted,
            resolved_http_attempts,
            heuristic_used,
            latency_ms,
            failure_reason,
        )
        return summary

    def get_summary(self) -> dict[str, Any]:
        """Return the most recent AI inference telemetry summary dictionary."""
        if not self.last_run:
            return {
                "ai_primary_model": None,
                "ai_selected_model": None,
                "selected_model": None,
                "candidate_models": [],
                "selected_candidate_index": None,
                "ai_actual_provider_model": None,
                "ai_fallback_used": False,
                "fallback_used": False,
                "ai_fallback_index": None,
                "fallback_index": None,
                "models_attempted": 0,
                "model_candidates_attempted": 0,
                "models_exhausted": False,
                "model_candidates_exhausted": False,
                "http_attempts": 0,
                "ai_failure_reason": None,
                "ai_total_attempts": 0,
                "ai_heuristic_used": False,
                "heuristic_used": False,
                "ai_latency_ms": 0.0,
                "attempts": [],
            }
        return {
            "ai_primary_model": self.last_run.ai_primary_model,
            "ai_selected_model": self.last_run.ai_selected_model,
            "selected_model": self.last_run.ai_selected_model,
            "candidate_models": self.last_run.candidate_models,
            "selected_candidate_index": self.last_run.selected_candidate_index,
            "ai_actual_provider_model": self.last_run.ai_actual_provider_model,
            "ai_fallback_used": self.last_run.ai_fallback_used,
            "fallback_used": self.last_run.ai_fallback_used,
            "ai_fallback_index": self.last_run.ai_fallback_index,
            "fallback_index": self.last_run.ai_fallback_index,
            "models_attempted": self.last_run.models_attempted,
            "model_candidates_attempted": self.last_run.models_attempted,
            "models_exhausted": self.last_run.models_exhausted,
            "model_candidates_exhausted": self.last_run.models_exhausted,
            "http_attempts": self.last_run.http_attempts,
            "ai_failure_reason": self.last_run.ai_failure_reason,
            "ai_total_attempts": self.last_run.ai_total_attempts,
            "ai_heuristic_used": self.last_run.ai_heuristic_used,
            "heuristic_used": self.last_run.ai_heuristic_used,
            "ai_latency_ms": self.last_run.ai_latency_ms,
            "attempts": self.last_run.attempts,
        }

    def reset(self) -> None:
        """Reset telemetry collector state."""
        self.last_run = None
        self._history.clear()
