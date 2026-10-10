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
        live_fare: Decimal | None = None,
        is_live: bool = False,
    ) -> LocalTransitEstimate:
        """Estimate point-to-point ride cost based on route distance and vehicle mode."""
        dist = max(0.0, distance_km)
        mode_clean = mode.lower().strip()
        rate_key = f"{mode_clean}_per_km"

        rate_per_km: Decimal | None = None
        if rate_key in self.rates:
            rate_per_km = self.rates[rate_key]
        elif mode_clean == "taxi" and "cab_per_km" in self.rates:
            rate_per_km = self.rates["cab_per_km"]

        # Case 1: Verified live fare supplied
        if live_fare is not None and is_live:
            total_live = round(Decimal(str(live_fare)), 2)
            computed_rate = round(total_live / Decimal(str(dist)), 2) if dist > 0 else None
            return LocalTransitEstimate(
                mode=mode,
                rate_per_km=computed_rate,
                distance_km=dist,
                days=None,
                total_cost=total_live,
                currency="INR",
                source=DataSource.LIVE,
                is_available=True,
                basis=f"Verified live price for {mode} ride ({dist:g} km).",
                limitations="Subject to real-time seat availability and booking terms.",
            )

        # Case 2: Live fare was expected/attempted but is unavailable
        if is_live:
            if rate_per_km is not None:
                total_fallback = round(rate_per_km * Decimal(str(dist)), 2)
                return LocalTransitEstimate(
                    mode=mode,
                    rate_per_km=rate_per_km,
                    distance_km=dist,
                    days=None,
                    total_cost=total_fallback,
                    currency="INR",
                    source=DataSource.FALLBACK,
                    is_available=True,
                    basis=f"Configured fallback rate table for {mode} ({rate_per_km} INR/km); live fare was unavailable.",
                    limitations="Advisory fallback heuristic; not a verified live price. Does not account for surge, waiting time, or tolls.",
                )
            else:
                logger.warning("Live fare unavailable and transit mode '%s' has no configured rate table.", mode)
                return LocalTransitEstimate(
                    mode=mode,
                    rate_per_km=None,
                    distance_km=dist,
                    days=None,
                    total_cost=None,
                    currency="INR",
                    source=DataSource.FALLBACK,
                    is_available=False,
                    basis=f"Live fare unavailable and no configured rate table exists for transit mode '{mode}'.",
                    limitations=f"Fare cannot be defensibly calculated for unsupported mode '{mode}' without verified rate data.",
                )

        # Case 3: Standard heuristic estimation by distance
        if rate_per_km is not None:
            total = round(rate_per_km * Decimal(str(dist)), 2)
            return LocalTransitEstimate(
                mode=mode,
                rate_per_km=rate_per_km,
                distance_km=dist,
                days=None,
                total_cost=total,
                currency="INR",
                source=DataSource.ESTIMATED,
                is_available=True,
                basis=f"Configured rate table for {mode} ({rate_per_km} INR/km) for {dist:g} km.",
                limitations="Advisory heuristic based on distance; does not account for surge, waiting time, tolls, or live traffic conditions.",
            )

        # Case 4: Unsupported transit mode without configured rate table
        logger.warning(
            "Transit mode '%s' has no configured rate table; reporting fare as unavailable without arbitrary fallback.",
            mode,
        )
        return LocalTransitEstimate(
            mode=mode,
            rate_per_km=None,
            distance_km=dist,
            days=None,
            total_cost=None,
            currency="INR",
            source=DataSource.ESTIMATED,
            is_available=False,
            basis=f"No configured transit rate table exists for transport mode '{mode}'.",
            limitations=f"Fare cannot be defensibly calculated for unsupported mode '{mode}' without verified rate data.",
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
            is_available=True,
            basis=f"Configured daily transit pass rate table: INR {daily_pass}/person/day for {people} people across {days} days.",
            limitations="Advisory heuristic for in-city mass transit (metro/bus); does not account for private cab rides or peak surges.",
        )
