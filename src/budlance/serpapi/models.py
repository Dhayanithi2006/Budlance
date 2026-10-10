"""Data envelope models for travel data responses."""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc)


class ProvenanceType(StrEnum):
    """Fine-grained data provenance classification required by Phase 2."""
    LIVE_PROVIDER = "LIVE_PROVIDER"
    CACHED_PROVIDER_RESULT = "CACHED_PROVIDER_RESULT"
    OFFLINE_FALLBACK = "OFFLINE_FALLBACK"
    CONFIG_ESTIMATE = "CONFIG_ESTIMATE"
    USER_INPUT = "USER_INPUT"
    DERIVED = "DERIVED"
    UNKNOWN = "UNKNOWN"


class DataProvenance(BaseModel):
    """Detailed provenance tracking record for live or cached travel data."""
    model_config = ConfigDict(from_attributes=True)

    provenance_type: ProvenanceType
    provider: str = "serpapi"
    engine: str | None = None
    retrieval_timestamp: datetime = Field(default_factory=utc_now)
    cache_hit: bool = False
    cache_age_seconds: float | None = None
    query_params: dict[str, Any] | None = None
    price_scope: str | None = None
    currency: str = "INR"
    http_status: int | None = None
    latency_sec: float | None = None


class DataSource(StrEnum):
    """Origin source of travel data for transparency and trust."""
    LIVE = "LIVE"
    CACHED = "CACHED"
    FALLBACK = "FALLBACK"
    ESTIMATED = "ESTIMATED"
    USER_REPORTED = "USER_REPORTED"

    # Canonical Phase 2 provenance values
    LIVE_PROVIDER = "LIVE_PROVIDER"
    CACHED_PROVIDER_RESULT = "CACHED_PROVIDER_RESULT"
    OFFLINE_FALLBACK = "OFFLINE_FALLBACK"
    CONFIG_ESTIMATE = "CONFIG_ESTIMATE"
    USER_INPUT = "USER_INPUT"
    DERIVED = "DERIVED"
    UNKNOWN = "UNKNOWN"


class TravelDataEnvelope(BaseModel):
    """Unified container for all external or fallback travel data."""
    model_config = ConfigDict(from_attributes=True)

    source: DataSource
    engine: str
    query_hash: str
    data: dict[str, Any] = Field(default_factory=dict)
    is_fallback: bool = False
    status: str = "success"  # success, error, cached
    error_message: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    provenance: DataProvenance | None = None

    @property
    def provenance_type(self) -> ProvenanceType:
        if self.provenance:
            return self.provenance.provenance_type
        if self.source in (DataSource.LIVE, DataSource.LIVE_PROVIDER):
            return ProvenanceType.LIVE_PROVIDER
        if self.source in (DataSource.CACHED, DataSource.CACHED_PROVIDER_RESULT):
            return ProvenanceType.CACHED_PROVIDER_RESULT
        if self.source in (DataSource.FALLBACK, DataSource.OFFLINE_FALLBACK):
            return ProvenanceType.OFFLINE_FALLBACK
        if self.source in (DataSource.ESTIMATED, DataSource.CONFIG_ESTIMATE):
            return ProvenanceType.CONFIG_ESTIMATE
        if self.source in (DataSource.USER_REPORTED, DataSource.USER_INPUT):
            return ProvenanceType.USER_INPUT
        return ProvenanceType.UNKNOWN
