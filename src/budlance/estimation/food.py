"""Food cost estimator based on configurable rate matrices."""

import logging
from decimal import Decimal
from typing import Any
from budlance.cache.fallback import FallbackDataProvider
from budlance.schemas.travel import FoodEstimate
from budlance.serpapi.models import DataSource

logger = logging.getLogger(__name__)

DEFAULT_FOOD_RATES = {
    "budget": Decimal("400.00"),
    "standard": Decimal("800.00"),
    "comfort": Decimal("1500.00"),
}


class FoodEstimator:
    """Calculates advisory food costs for trip duration and group size."""

    def __init__(self, fallback_provider: FallbackDataProvider | None = None) -> None:
        self.fallback = fallback_provider or FallbackDataProvider()
        self._load_rates()

    def _load_rates(self) -> None:
        data = self.fallback.get_rate_tables()
        rates_dict = data.get("food_estimates_per_day_inr") or {}
        self.rates = {
            k: Decimal(str(v)) for k, v in rates_dict.items()
        } if rates_dict else DEFAULT_FOOD_RATES

    def estimate_food(
        self,
        people: int,
        days: int,
        tier: str = "standard",
    ) -> FoodEstimate:
        """Calculate estimated food expenses.

        Does not claim to be a live restaurant menu price; labeled as ESTIMATED.
        """
        people = max(1, people)
        days = max(1, days)
        normalized_tier = tier.lower() if tier.lower() in self.rates else "standard"
        daily_per_person = self.rates[normalized_tier]
        total = daily_per_person * Decimal(people) * Decimal(days)

        return FoodEstimate(
            tier=normalized_tier,
            daily_cost_per_person=daily_per_person,
            total_cost=total,
            people=people,
            days=days,
            currency="INR",
            source=DataSource.ESTIMATED,
        )
