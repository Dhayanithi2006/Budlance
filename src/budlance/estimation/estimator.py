"""EstimationLayer combining food and local transport estimators."""

from budlance.cache.fallback import FallbackDataProvider
from budlance.estimation.food import FoodEstimator
from budlance.estimation.transport import LocalTransitEstimator
from budlance.schemas.travel import FoodEstimate, LocalTransitEstimate


class EstimationLayer:
    """Unified estimation service for non-live travel expenses."""

    def __init__(self, fallback_provider: FallbackDataProvider | None = None) -> None:
        provider = fallback_provider or FallbackDataProvider()
        self.food_estimator = FoodEstimator(provider)
        self.transit_estimator = LocalTransitEstimator(provider)

    def estimate_food(
        self,
        people: int,
        days: int,
        tier: str = "standard",
    ) -> FoodEstimate:
        """Estimate food expenses for the trip duration and party size."""
        return self.food_estimator.estimate_food(people=people, days=days, tier=tier)

    def estimate_local_transit_distance(
        self,
        distance_km: float,
        mode: str = "auto",
    ) -> LocalTransitEstimate:
        """Estimate local point-to-point transit based on route distance."""
        return self.transit_estimator.estimate_by_distance(distance_km=distance_km, mode=mode)

    def estimate_local_transit_daily(
        self,
        days: int,
        people: int = 1,
        mode: str = "metro_bus",
    ) -> LocalTransitEstimate:
        """Estimate general local transit passes for trip duration."""
        return self.transit_estimator.estimate_daily_transit(days=days, people=people, mode=mode)
