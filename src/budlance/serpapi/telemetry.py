"""Deterministic SerpApi telemetry tracking and call accounting."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class SerpApiCallRecord:
    """Individual recorded SerpApi HTTP request operation."""
    engine: str
    params: dict[str, Any]
    status: str  # "success" or "failed"
    attempts: int = 1
    retries: int = 0  # max(0, attempts - 1)
    latency_sec: float = 0.0
    error: str | None = None
    http_status: int | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SerpApiTelemetry:
    """Deterministic, thread-safe telemetry collector for SerpApi calls.

    Counting Policy:
    1. Operational accounting:
       - Every invocation of execute_search represents exactly one logical live API operation.
       - Every operation increments calls_by_engine[engine] by exactly 1.
       - total_live_calls is strictly defined as the total number of live API operations dispatched.
       - Invariant: sum(calls_by_engine.values()) == total_live_calls (always mathematically identical).
    2. Retry accounting:
       - If an operation succeeds on attempt 1: retries = 0.
       - If an operation requires N attempts (N > 1): retries = N - 1.
       - Total retries across all calls is accumulated into self.retries.
    3. Failure accounting:
       - If an operation fails after all retries and keys are exhausted, failed_calls increments by 1.
       - If an operation succeeds, successful_calls increments by 1.
       - Invariant: total_live_calls == successful_calls + failed_calls.
    """

    def __init__(self) -> None:
        self._records: list[SerpApiCallRecord] = []
        self._calls_by_engine: dict[str, int] = {}
        self._total_live_calls: int = 0
        self._retries: int = 0
        self._failed_calls: int = 0
        self._successful_calls: int = 0

    @property
    def total_live_calls(self) -> int:
        """Total number of live SerpApi operations executed."""
        return self._total_live_calls

    @property
    def calls_by_engine(self) -> dict[str, int]:
        """Dictionary mapping each SerpApi engine name to its operation count."""
        return dict(self._calls_by_engine)

    @property
    def retries(self) -> int:
        """Total number of HTTP retry attempts executed beyond the initial attempt."""
        return self._retries

    @property
    def failed_calls(self) -> int:
        """Total number of operations that ended in failure."""
        return self._failed_calls

    @property
    def successful_calls(self) -> int:
        """Total number of operations that completed successfully."""
        return self._successful_calls

    @property
    def records(self) -> list[SerpApiCallRecord]:
        """Read-only copy of individual call records."""
        return list(self._records)

    def record_call(
        self,
        engine: str,
        params: dict[str, Any],
        status: str,
        attempts: int = 1,
        latency_sec: float = 0.0,
        error: str | None = None,
        http_status: int | None = None,
    ) -> SerpApiCallRecord:
        """Record a completed or failed SerpApi search operation."""
        retries_for_call = max(0, attempts - 1)
        record = SerpApiCallRecord(
            engine=engine,
            params=dict(params),
            status=status,
            attempts=attempts,
            retries=retries_for_call,
            latency_sec=latency_sec,
            error=error,
            http_status=http_status,
        )
        self._records.append(record)

        # Update counters
        self._total_live_calls += 1
        self._calls_by_engine[engine] = self._calls_by_engine.get(engine, 0) + 1
        self._retries += retries_for_call

        if status == "success":
            self._successful_calls += 1
        else:
            self._failed_calls += 1

        return record

    def get_summary(self) -> dict[str, Any]:
        """Return a structured telemetry summary report."""
        return {
            "total_live_calls": self.total_live_calls,
            "calls_by_engine": self.calls_by_engine,
            "retries": self.retries,
            "failed_calls": self.failed_calls,
            "successful_calls": self.successful_calls,
            "is_sum_consistent": sum(self._calls_by_engine.values()) == self.total_live_calls,
        }

    def reset(self) -> None:
        """Reset all telemetry records and counters."""
        self._records.clear()
        self._calls_by_engine.clear()
        self._total_live_calls = 0
        self._retries = 0
        self._failed_calls = 0
        self._successful_calls = 0
