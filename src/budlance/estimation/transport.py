"""Local transport estimator based on distance and daily pass heuristics."""

import logging
from decimal import Decimal
from typing import Any
from budlance.cache.fallback import FallbackDataProvider
from budlance.schemas.travel import LocalTransitEstimate
from budlance.serpapi.models import DataSource

logger = logging.getLogger(__name__)

DEFAULT_TRANSIT_RATES = {
    "auto_per_km": Decimal("15.00"),
    "cab_per_km": Decimal("22.00"),
    "metro_bus_daily_pass": Decimal("100.00"),
}


class LocalTransitEstimator:
    """Calculates advisory local transport costs."""

    def __init__(self, fallback_provider: FallbackDataProvider | None = None) -> None:
        self.fallback = fallback_provider or FallbackDataProvider()
        self._load_rates()

    def _load_rates(self) -> None:
        data = self.fallback.get_rate_tables()
        rates_dict = data.get("local_transit_rates_inr") or {}
        self.rates = {
            k: Decimal(str(v)) for k, v in rates_dict.items()
        } if rates_dict else DEFAULT_TRANSIT_RATES

    def estimate_by_distance(
        self,
        distance_km: float,
        mode: str = "auto",
    ) -> LocalTransitEstimate:
        """Estimate point-to-point ride cost based on route distance and vehicle mode."""
        dist = max(0.0, distance_km)
        rate_key = f"{mode.lower()}_per_km"
        rate_per_km = self.rates.get(rate_key, self.rates.get("auto_per_km", Decimal("15.00")))
        total = round(rate_per_km * Decimal(str(dist)), 2)

        return LocalTransitEstimate(
            mode=mode,
            rate_per_km=rate_per_km,
            distance_km=dist,
            days=None,
            total_cost=total,
            currency="INR",
            source=DataSource.ESTIMATED,
        )

    def estimate_daily_transit(
        self,
        days: int,
        people: int = 1,
        mode: str = "metro_bus",
    ) -> LocalTransitEstimate:
        """Estimate general in-city transit for trip duration."""
        days = max(1, days)
        people = max(1, people)
        daily_pass = self.rates.get("metro_bus_daily_pass", Decimal("100.00"))
        total = daily_pass * Decimal(people) * Decimal(days)

        return LocalTransitEstimate(
            mode=mode,
            rate_per_km=None,
            distance_km=None,
            days=days,
            total_cost=total,
            currency="INR",
            source=DataSource.ESTIMATED,
        )
