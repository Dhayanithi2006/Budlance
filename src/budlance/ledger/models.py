"""Domain models for Virtual Ledger summaries and state."""

from decimal import Decimal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field
from budlance.db.models import BudgetAllocation, LedgerEntry


class LedgerSummary(BaseModel):
    """Aggregated financial state of a trip's virtual ledger."""
    model_config = ConfigDict(from_attributes=True)

    trip_id: UUID
    total_budget: Decimal
    total_allocated: Decimal
    total_planned: Decimal
    total_spent: Decimal
    total_remaining: Decimal
    allocation: BudgetAllocation
    entries: list[LedgerEntry] = Field(default_factory=list)
