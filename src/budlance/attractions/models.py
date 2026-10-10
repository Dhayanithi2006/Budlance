"""Pydantic models for curated attractions."""

from typing import Literal
from pydantic import BaseModel, Field

TimeOfDay = Literal["morning", "afternoon", "evening"]


class Attraction(BaseModel):
    """Verified attraction data loaded from curated datasets."""

    name: str
    category: str
    suitable_for: list[str] = Field(default_factory=list)
    typical_time_hours: float
    opening_hours: str
    entry_fee_inr: int | None = None
    is_fee_unknown: bool = False
    description: str
    location: str
    best_time_of_day: str
    source: str = "CURATED"
    region: str | None = None
