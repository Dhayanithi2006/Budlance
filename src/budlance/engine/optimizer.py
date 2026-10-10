"""Optimization Engine executing the frozen 4-step downgrade sequence."""

import logging
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from budlance.db.models import PlanAttempt, utc_now
from budlance.db.repositories.attempt_repo import AttemptRepository
from budlance.engine.budget import ReverseBudgetEngine
from budlance.engine.models import BudgetEvaluationResult, OptimizationResult
from budlance.estimation.estimator import EstimationLayer
from budlance.schemas.travel import FlightOption, FoodEstimate, HotelOption, LocalTransitEstimate, TransitOption

logger = logging.getLogger(__name__)


class OptimizationEngine:
    """Manages the frozen 4-step downgrade loop when a trip is initially NOT_FEASIBLE.

    Frozen Downgrade Sequence (Max 4 attempts):
    Attempt 1: Hotel tier down ➔ Re-evaluate
    Attempt 2: Transport class down ➔ Re-evaluate
    Attempt 3: Reduce trip length by 1 day ➔ Re-evaluate
    Attempt 4: Trim discretionary Bucket B ➔ Re-evaluate
    """

    def __init__(
        self,
        budget_engine: ReverseBudgetEngine | None = None,
        estimation_layer: EstimationLayer | None = None,
        attempt_repo: AttemptRepository | None = None,
    ) -> None:
        self.budget_engine = budget_engine or ReverseBudgetEngine()
        self.estimation = estimation_layer or EstimationLayer()
        self.attempt_repo = attempt_repo or AttemptRepository()

    def optimize(
        self,
        trip_id: UUID | None,
        total_budget: Decimal,
        people: int,
        days: int,
        initial_transport: FlightOption | TransitOption | None,
        initial_hotel: HotelOption | None,
        initial_food: FoodEstimate,
        initial_transit: LocalTransitEstimate,
        activities_budget: Decimal = Decimal("0.00"),
        available_hotels: list[HotelOption] | None = None,
        available_transports: list[FlightOption | TransitOption] | None = None,
        currency: str = "INR",
        selected_attractions: list[Any] | None = None,
        locked_days: set[int] | None = None,
        requires_transport: bool = False,
        requires_lodging: bool = False,
        requires_attraction_fees: bool = False,
        explicit_transport_mode: str | None = None,
        explicit_hotel_tier: str | None = None,
        strict_preferences: list[str] | None = None,
    ) -> OptimizationResult:
        """Run the optimization loop up to a maximum of 4 attempts."""
        # Baseline evaluation
        current_days = days
        current_transport = initial_transport
        current_hotel = initial_hotel
        current_food = initial_food
        current_transit = initial_transit
        current_activities = activities_budget
        downgrades_applied: list[str] = []
        alternatives_available: list[str] = []

        baseline_eval = self.budget_engine.evaluate(
            total_budget=total_budget,
            people=people,
            days=current_days,
            transport=current_transport,
            hotel=current_hotel,
            food_estimate=current_food,
            local_transit_estimate=current_transit,
            activities_budget=current_activities,
            currency=currency,
            selected_attractions=selected_attractions,
            requires_transport=requires_transport,
            requires_lodging=requires_lodging,
            requires_attraction_fees=requires_attraction_fees,
        )

        if baseline_eval.status == "INCOMPLETE_COST_DATA":
            return OptimizationResult(
                initial_status="INCOMPLETE_COST_DATA",
                final_status="INCOMPLETE_COST_DATA",
                is_feasible=False,
                successful_attempt=None,
                total_attempts=0,
                final_evaluation=baseline_eval,
                selected_transport=current_transport,
                selected_hotel=current_hotel,
                days=current_days,
                explanation=baseline_eval.explanation,
                deficit=Decimal("0.00"),
            )

        has_invalid_transport = requires_transport and (
            current_transport is None or current_transport.price <= Decimal("0.00")
        )
        has_invalid_lodging = requires_lodging and (
            (current_hotel is None or current_hotel.total_price <= Decimal("0.00"))
            and baseline_eval.breakdown.hotel_cost <= Decimal("0.00")
        )

        # Gate constraint: Optimizer cannot fabricate or bypass missing physical transport or lodging
        if has_invalid_transport or has_invalid_lodging:
            reason = "Missing valid physical transport" if has_invalid_transport else "Missing valid accommodation"
            return OptimizationResult(
                initial_status="NOT_FEASIBLE",
                final_status="NOT_FEASIBLE",
                is_feasible=False,
                successful_attempt=None,
                total_attempts=0,
                final_evaluation=baseline_eval,
                selected_transport=current_transport,
                selected_hotel=current_hotel,
                days=current_days,
                explanation=f"{reason} cannot be optimized into feasibility.",
                deficit=baseline_eval.deficit,
            )

        if baseline_eval.is_feasible:
            # Already feasible, no downgrades needed
            return OptimizationResult(
                initial_status="FEASIBLE",
                final_status="FEASIBLE",
                is_feasible=True,
                successful_attempt=None,
                total_attempts=0,
                final_evaluation=baseline_eval,
                selected_transport=current_transport,
                selected_hotel=current_hotel,
                days=current_days,
                explanation="Initial configuration is already feasible within budget.",
                deficit=Decimal("0.00"),
            )

        latest_eval = baseline_eval

        # =====================================================================
        # Attempt 1: Hotel tier down
        # =====================================================================
        current_hotel_tier = "standard"
        is_explicit_luxury_hotel = (
            (explicit_hotel_tier and explicit_hotel_tier.lower() in ("luxury", "4-star", "4 star", "5-star", "5 star"))
            or any(k in (strict_preferences or []) for k in ("luxury", "5-star", "5 star", "4-star", "4 star", "luxury_hotel", "resort"))
        )
        downgraded_hotel = self._downgrade_hotel(
            current_hotel,
            available_hotels,
            preserve_luxury=is_explicit_luxury_hotel,
        )
        if downgraded_hotel and (current_hotel is None or downgraded_hotel.total_price < current_hotel.total_price):
            current_hotel = downgraded_hotel
            downgrades_applied.append(f"Hotel downgraded to {current_hotel.name} ({currency} {current_hotel.total_price})")
        elif current_hotel is None and current_days > 1:
            if not is_explicit_luxury_hotel:
                current_hotel_tier = "budget"
                downgrades_applied.append("Lodging tier adjusted to budget")

        if is_explicit_luxury_hotel and available_hotels and current_hotel:
            cheaper_standard = [h for h in available_hotels if (h.hotel_class or 0) < 4 and h.total_price < current_hotel.total_price]
            if cheaper_standard:
                cheaper_standard.sort(key=lambda h: h.total_price)
                diff = current_hotel.total_price - cheaper_standard[0].total_price
                alternatives_available.append(
                    f"Switching from luxury hotel to standard accommodation ({cheaper_standard[0].name}) would save {currency} {diff:,.2f}"
                )

        latest_eval = self.budget_engine.evaluate(
            total_budget=total_budget,
            people=people,
            days=current_days,
            transport=current_transport,
            hotel=current_hotel,
            food_estimate=current_food,
            local_transit_estimate=current_transit,
            activities_budget=current_activities,
            currency=currency,
            selected_attractions=selected_attractions,
            hotel_tier=current_hotel_tier,
            requires_transport=requires_transport,
            requires_lodging=requires_lodging,
            requires_attraction_fees=requires_attraction_fees,
        )

        self._record_attempt(trip_id, 1, "hotel_tier_down", latest_eval)
        if latest_eval.is_feasible and not (requires_transport and (current_transport is None or current_transport.price <= Decimal("0.00"))) and not (requires_lodging and (current_hotel is None or current_hotel.total_price <= Decimal("0.00")) and latest_eval.breakdown.hotel_cost <= Decimal("0.00")):
            return self._build_success_result(
                attempt_num=1,
                evaluation=latest_eval,
                transport=current_transport,
                hotel=current_hotel,
                days=current_days,
                downgrades=downgrades_applied,
                alternatives=alternatives_available,
            )

        # =====================================================================
        # Attempt 2: Transport class down
        # =====================================================================
        is_explicit_flight = (
            (explicit_transport_mode and explicit_transport_mode.lower() == "flight")
            or any(k in (strict_preferences or []) for k in ("flight", "fly", "plane", "flights"))
        )
        downgraded_transport = self._downgrade_transport(
            current_transport,
            available_transports,
            explicit_mode="flight" if is_explicit_flight else explicit_transport_mode,
        )
        if downgraded_transport and (current_transport is None or downgraded_transport.price < current_transport.price):
            current_transport = downgraded_transport
            name = getattr(current_transport, "airline", None) or getattr(current_transport, "name_or_operator", "Transport")
            downgrades_applied.append(f"Transport downgraded to {name} ({currency} {current_transport.price})")

        if is_explicit_flight and available_transports and current_transport:
            cheaper_transit = [
                t for t in available_transports
                if t.price < current_transport.price and not isinstance(t, FlightOption)
            ]
            if cheaper_transit:
                cheaper_transit.sort(key=lambda t: t.price)
                diff = current_transport.price - cheaper_transit[0].price
                t_name = getattr(cheaper_transit[0], "name_or_operator", "train/bus")
                alternatives_available.append(
                    f"Switching transport mode from flight to {t_name} would save {currency} {diff:,.2f}"
                )

        latest_eval = self.budget_engine.evaluate(
            total_budget=total_budget,
            people=people,
            days=current_days,
            transport=current_transport,
            hotel=current_hotel,
            food_estimate=current_food,
            local_transit_estimate=current_transit,
            activities_budget=current_activities,
            currency=currency,
            selected_attractions=selected_attractions,
            hotel_tier=current_hotel_tier,
            requires_transport=requires_transport,
            requires_lodging=requires_lodging,
            requires_attraction_fees=requires_attraction_fees,
        )

        self._record_attempt(trip_id, 2, "transport_class_down", latest_eval)
        if latest_eval.is_feasible and not (requires_transport and (current_transport is None or current_transport.price <= Decimal("0.00"))) and not (requires_lodging and (current_hotel is None or current_hotel.total_price <= Decimal("0.00")) and latest_eval.breakdown.hotel_cost <= Decimal("0.00")):
            return self._build_success_result(
                attempt_num=2,
                evaluation=latest_eval,
                transport=current_transport,
                hotel=current_hotel,
                days=current_days,
                downgrades=downgrades_applied,
                alternatives=alternatives_available,
            )

        # =====================================================================
        # Attempt 3: Reduce trip length by 1 day
        # =====================================================================
        if locked_days is True:
            min_allowed_days = current_days
        elif isinstance(locked_days, (set, list, tuple)) and locked_days:
            min_allowed_days = max(locked_days)
        else:
            min_allowed_days = 1
        if current_days > min_allowed_days:
            current_days -= 1
            # Re-scale hotel stay for (current_days) nights
            if current_hotel and current_hotel.price_per_night:
                current_hotel = current_hotel.model_copy(
                    update={"total_price": current_hotel.price_per_night * Decimal(current_days)}
                )
            # Re-estimate daily food and local transit
            current_food = self.estimation.estimate_food(people, current_days, tier=current_food.tier)
            current_transit = self.estimation.estimate_local_transit_daily(current_days, people)
            downgrades_applied.append(f"Trip duration reduced to {current_days} days")

        latest_eval = self.budget_engine.evaluate(
            total_budget=total_budget,
            people=people,
            days=current_days,
            transport=current_transport,
            hotel=current_hotel,
            food_estimate=current_food,
            local_transit_estimate=current_transit,
            activities_budget=current_activities,
            currency=currency,
            selected_attractions=selected_attractions,
            hotel_tier=current_hotel_tier,
            requires_transport=requires_transport,
            requires_lodging=requires_lodging,
            requires_attraction_fees=requires_attraction_fees,
        )

        self._record_attempt(trip_id, 3, "reduce_trip_length", latest_eval)
        if latest_eval.is_feasible and not (requires_transport and (current_transport is None or current_transport.price <= Decimal("0.00"))) and not (requires_lodging and (current_hotel is None or current_hotel.total_price <= Decimal("0.00")) and latest_eval.breakdown.hotel_cost <= Decimal("0.00")):
            return self._build_success_result(
                attempt_num=3,
                evaluation=latest_eval,
                transport=current_transport,
                hotel=current_hotel,
                days=current_days,
                downgrades=downgrades_applied,
                alternatives=alternatives_available,
            )

        # =====================================================================
        # Attempt 4: Trim discretionary Bucket B
        # =====================================================================
        # Switch food to budget tier and trim optional activities
        current_food = self.estimation.estimate_food(people, current_days, tier="budget")
        current_activities = Decimal("0.00")
        downgrades_applied.append("Food trimmed to budget tier and discretionary activities removed")

        latest_eval = self.budget_engine.evaluate(
            total_budget=total_budget,
            people=people,
            days=current_days,
            transport=current_transport,
            hotel=current_hotel,
            food_estimate=current_food,
            local_transit_estimate=current_transit,
            activities_budget=current_activities,
            currency=currency,
            selected_attractions=selected_attractions,
            hotel_tier=current_hotel_tier,
            requires_transport=requires_transport,
            requires_lodging=requires_lodging,
            requires_attraction_fees=requires_attraction_fees,
        )

        self._record_attempt(trip_id, 4, "trim_discretionary_b", latest_eval)
        if latest_eval.is_feasible and not (requires_transport and (current_transport is None or current_transport.price <= Decimal("0.00"))) and not (requires_lodging and (current_hotel is None or current_hotel.total_price <= Decimal("0.00")) and latest_eval.breakdown.hotel_cost <= Decimal("0.00")):
            return self._build_success_result(
                attempt_num=4,
                evaluation=latest_eval,
                transport=current_transport,
                hotel=current_hotel,
                days=current_days,
                downgrades=downgrades_applied,
                alternatives=alternatives_available,
            )

        # Still impossible after 4 attempts (no attempt 5)
        rec = (
            f"Trip remains over budget by {currency} {latest_eval.deficit} after all 4 optimization steps. "
            f"Consider increasing budget to {currency} {latest_eval.breakdown.total_allocated} "
            f"or further reducing the trip duration."
        )
        if alternatives_available:
            rec += " " + " ".join(alternatives_available)

        return OptimizationResult(
            initial_status="NOT_FEASIBLE",
            final_status="NOT_FEASIBLE",
            is_feasible=False,
            successful_attempt=None,
            total_attempts=4,
            final_evaluation=latest_eval,
            selected_transport=current_transport,
            selected_hotel=current_hotel,
            days=current_days,
            downgrades_applied=downgrades_applied,
            explanation=f"Trip is still impossible within {currency} {total_budget} after 4 optimization attempts.",
            deficit=latest_eval.deficit,
            recommendation=rec,
            alternatives_available=alternatives_available,
        )

    # =========================================================================
    # Downgrade Selection Helpers
    # =========================================================================
    def _downgrade_hotel(
        self,
        current_hotel: HotelOption | None,
        available_hotels: list[HotelOption] | None,
        preserve_luxury: bool = False,
    ) -> HotelOption | None:
        if not available_hotels or not current_hotel:
            return current_hotel

        # Filter hotels strictly cheaper than current
        cheaper = [h for h in available_hotels if h.total_price < current_hotel.total_price]
        if preserve_luxury:
            # If user explicitly requested luxury, do not silently downgrade below 4-star
            cheaper = [h for h in cheaper if (h.hotel_class or 0) >= 4]

        if not cheaper:
            return current_hotel

        # Sort by total_price descending to take the next closest tier down
        cheaper.sort(key=lambda h: h.total_price, reverse=True)
        return cheaper[0]

    def _downgrade_transport(
        self,
        current_transport: FlightOption | TransitOption | None,
        available_transports: list[FlightOption | TransitOption] | None,
        explicit_mode: str | None = None,
    ) -> FlightOption | TransitOption | None:
        if not available_transports or not current_transport:
            return current_transport

        cheaper = [t for t in available_transports if t.price < current_transport.price]
        if explicit_mode and explicit_mode.lower() == "flight":
            # If user explicitly requested flights, do not silently switch to ground transit
            cheaper = [t for t in cheaper if isinstance(t, FlightOption)]

        if not cheaper:
            return current_transport

        cheaper.sort(key=lambda t: t.price, reverse=True)
        return cheaper[0]

    def _record_attempt(
        self,
        trip_id: UUID | None,
        attempt_number: int,
        downgrade_type: Any,
        evaluation: BudgetEvaluationResult,
    ) -> None:
        # Skip persistence when trip_id is None (pre-persistence evaluation phase).
        # At that stage no trips row exists yet, so inserting plan_attempts would
        # violate the FK constraint. The orchestrator records attempts with a real
        # trip_id only after the trip is persisted.
        if trip_id is None:
            return
        attempt_record = PlanAttempt(
            id=uuid4(),
            trip_id=trip_id,
            attempt_number=attempt_number,
            downgrade_type=downgrade_type,
            was_feasible=evaluation.is_feasible,
            cost_calculated=evaluation.breakdown.total_allocated,
            notes=evaluation.explanation,
            created_at=utc_now(),
        )
        self.attempt_repo.record_plan_attempt(attempt_record)

    def _build_success_result(
        self,
        attempt_num: int,
        evaluation: BudgetEvaluationResult,
        transport: Any,
        hotel: Any,
        days: int,
        downgrades: list[str],
        alternatives: list[str] | None = None,
    ) -> OptimizationResult:
        return OptimizationResult(
            initial_status="NOT_FEASIBLE",
            final_status="FEASIBLE",
            is_feasible=True,
            successful_attempt=attempt_num,
            total_attempts=attempt_num,
            final_evaluation=evaluation,
            selected_transport=transport,
            selected_hotel=hotel,
            days=days,
            downgrades_applied=downgrades,
            explanation=f"Trip became feasible on attempt {attempt_num}: {downgrades[-1]}.",
            deficit=Decimal("0.00"),
            alternatives_available=alternatives or [],
        )

    # =========================================================================
    # Quality & Preference-Aware Selection Helpers
    # =========================================================================
    def select_preferred_hotel(
        self,
        available_hotels: list[HotelOption] | None,
        budget_limit: Decimal | None = None,
        preferences: list[str] | None = None,
        hotel_tier: str = "standard",
        is_generous_budget: bool = False,
    ) -> HotelOption | None:
        """Select preferred hotel option considering user quality preferences, tier, and budget.

        When user requests luxury/premium accommodation or budget is generous:
        Ranks by (hotel_class descending, rating descending, review_count descending),
        picking the highest quality stay within budget without spending money blindly.
        When normal/budget requested:
        Selects best-value stay matching the target tier.
        """
        if not available_hotels:
            return None

        # Filter valid hotels
        valid = [
            h for h in available_hotels
            if h.total_price is not None and h.total_price > Decimal("0.00")
        ]
        if not valid:
            return None

        # Filter within budget limit if provided
        if budget_limit is not None and budget_limit > Decimal("0.00"):
            affordable = [h for h in valid if h.total_price <= budget_limit]
            candidates = list(affordable) if affordable else list(valid)
        else:
            candidates = list(valid)

        # Hard quality relaxation ladder: 4.0/50 -> 3.8/25 -> 3.5/10 -> unrated -> reject
        step1 = [
            h for h in candidates
            if (h.rating is not None and float(h.rating) >= 4.0)
            and (h.review_count is None or h.review_count >= 50)
        ]
        if step1:
            candidates = step1
        else:
            step2 = [
                h for h in candidates
                if (h.rating is not None and float(h.rating) >= 3.8)
                and (h.review_count is None or h.review_count >= 25)
            ]
            if step2:
                candidates = step2
            else:
                step3 = [
                    h for h in candidates
                    if (h.rating is not None and float(h.rating) >= 3.5)
                    and (h.review_count is None or h.review_count >= 10)
                ]
                if step3:
                    candidates = step3
                else:
                    unrated = [h for h in candidates if h.rating is None and h.review_count is None]
                    if unrated:
                        candidates = unrated
                    else:
                        # Zero hotels pass quality threshold (rating < 3.5 or insufficient reviews)
                        return None


        pref_str = " ".join(preferences or []).lower() if preferences else ""
        is_luxury_pref = (
            any(k in pref_str for k in ("luxury", "premium", "5 star", "resort", "5-star", "4 star", "4-star"))
            or (hotel_tier.lower() in ("luxury", "4-star", "4 star", "5-star", "5 star"))
        )

        def _pref_match_score(h: HotelOption) -> int:
            score = 0
            name_lower = (h.name or "").lower()
            amenities_lower = " ".join([a.lower() for a in (h.amenities or [])])
            for p in (preferences or []):
                p_clean = p.lower().strip()
                if not p_clean:
                    continue
                for kw in p_clean.split():
                    if len(kw) >= 4 and (kw in name_lower or kw in amenities_lower):
                        score += 1
            return score

        if is_luxury_pref or is_generous_budget:
            # Sort by hotel_class desc, rating desc, pref_match desc, review_count desc, then price asc
            candidates.sort(
                key=lambda h: (
                    -(h.hotel_class or 0),
                    -(float(h.rating) if h.rating is not None else 0.0),
                    -_pref_match_score(h),
                    -(h.review_count or 0),
                    h.total_price,
                )
            )
            return candidates[0]

        # Normal/value sorting: sort by pref_match desc, rating desc within affordable, or value price
        candidates.sort(
            key=lambda h: (
                -_pref_match_score(h),
                -(float(h.rating) if h.rating is not None else 0.0),
                h.total_price,
            )
        )
        return candidates[0]

    def select_preferred_transport(
        self,
        available_transports: list[FlightOption | TransitOption] | None,
        budget_limit: Decimal | None = None,
        preferences: list[str] | None = None,
        transport_class: str | None = None,
        is_generous_budget: bool = False,
    ) -> FlightOption | TransitOption | None:
        """Select preferred transport option matching user class, convenience, and budget."""
        if not available_transports:
            return None

        valid = [
            t for t in available_transports
            if t.price is not None and t.price > Decimal("0.00")
        ]
        if not valid:
            return None

        if budget_limit is not None and budget_limit > Decimal("0.00"):
            affordable = [t for t in valid if t.price <= budget_limit]
            candidates = list(affordable) if affordable else list(valid)
        else:
            candidates = list(valid)

        cls = (transport_class or "").lower()
        if cls:
            matching = []
            for t in candidates:
                t_cls = (getattr(t, "class_or_type", None) or getattr(t, "airline", "")).lower()
                if cls in t_cls:
                    matching.append(t)
            if matching:
                candidates = matching

        pref_str = " ".join(preferences or []).lower() if preferences else ""
        is_premium_pref = any(k in pref_str for k in ("premium", "luxury", "private", "chauffeur", "first class", "business", "suv"))

        if is_generous_budget or is_premium_pref:
            def _premium_key(t):
                name_text = f"{getattr(t, 'name_or_operator', '')} {getattr(t, 'airline', '')} {getattr(t, 'class_or_type', '')}".lower()
                pref_matches = sum(1 for kw in (preferences or []) if kw.lower() in name_text)
                is_premium_named = any(kw in name_text for kw in ("premium", "luxury", "private", "chauffeur", "suv"))
                return (
                    -(1 if is_premium_named else 0),
                    -pref_matches,
                    0 if isinstance(t, FlightOption) else 1,
                    getattr(t, "duration_minutes", 9999) or 9999,
                    t.price,
                )

            candidates.sort(key=_premium_key)
            return candidates[0]

        # Standard value preference
        candidates.sort(key=lambda t: t.price)
        return candidates[0]
