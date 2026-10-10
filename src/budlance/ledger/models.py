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

    @property
    def rescue_reserve_remaining(self) -> Decimal:
        """Calculate unspent rescue reserve balance."""
        if not self.allocation:
            return Decimal("0.00")
        fund = getattr(self.allocation, "rescue_fund_allocated", None) or getattr(self.allocation, "rescue_reserve", Decimal("0.00"))
        if not fund:
            return Decimal("0.00")
        rescue_spent = sum(
            e.actual_amount or Decimal("0.00")
            for e in self.entries
            if e.category in ("rescue", "contingency")
        )
        return max(Decimal("0.00"), fund - rescue_spent)
