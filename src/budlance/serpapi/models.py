"""Data envelope models for travel data responses."""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc)


class DataSource(StrEnum):
    """Origin source of travel data for transparency and trust."""
    LIVE = "LIVE"
    CACHED = "CACHED"
    FALLBACK = "FALLBACK"
    ESTIMATED = "ESTIMATED"
    USER_REPORTED = "USER_REPORTED"


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
