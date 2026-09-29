"""Rescue Service orchestrating in-trip replanning for weather, closures, and price disputes.

Boundary Rules:
- Reuses the existing active trip, itinerary, ledger, and Reverse-Budget Engine.
- Does NOT create a second planning architecture.
- Does NOT call SerpApi directly (queries CacheFallbackManager).
- Price dispute uses local rate tables and estimation logic; does NOT make external API calls.
- ReverseBudgetEngine remains the authoritative financial decision-maker.
"""

from decimal import Decimal
import logging
from typing import Any
from uuid import UUID, uuid4

from budlance.ai.schemas import ParsedRescueIntent
from budlance.ai.service import AIIntentService
from budlance.cache.manager import CacheFallbackManager
from budlance.db.models import BudgetAllocation, RescueEvent, Trip, utc_now
from budlance.db.repositories.itinerary_repo import ItineraryRepository
from budlance.db.repositories.ledger_repo import LedgerRepository
from budlance.db.repositories.rescue_repo import RescueRepository
from budlance.db.repositories.trip_repo import TripRepository
from budlance.engine.budget import ReverseBudgetEngine
from budlance.estimation.estimator import EstimationLayer
from budlance.itinerary.models import GeneratedItinerary, ItineraryDay, ItineraryItem
from budlance.ledger.manager import VirtualLedgerManager
from budlance.normalization.normalizer import DataNormalizer
from budlance.rescue.models import FareGuidance, RescueRequest, RescueResult
from budlance.schemas.travel import PlaceOption

logger = logging.getLogger(__name__)


class RescueService:
    """Orchestrates live trip rescues without duplicating planning infrastructure."""

    def __init__(
        self,
        trip_repo: TripRepository | None = None,
        itinerary_repo: ItineraryRepository | None = None,
        ledger_repo: LedgerRepository | None = None,
        rescue_repo: RescueRepository | None = None,
        ai_service: AIIntentService | None = None,
        cache_manager: CacheFallbackManager | None = None,
        normalizer: DataNormalizer | None = None,
        budget_engine: ReverseBudgetEngine | None = None,
        estimation_layer: EstimationLayer | None = None,
        ledger_manager: VirtualLedgerManager | None = None,
    ) -> None:
        self.trip_repo = trip_repo or TripRepository()
        self.itinerary_repo = itinerary_repo or ItineraryRepository()
        self.ledger_repo = ledger_repo or LedgerRepository()
        self.rescue_repo = rescue_repo or RescueRepository()
        self.ai_service = ai_service or AIIntentService()
        self.cache_manager = cache_manager or CacheFallbackManager()
        self.normalizer = normalizer or DataNormalizer()
        self.budget_engine = budget_engine or ReverseBudgetEngine()
        self.estimation_layer = estimation_layer or EstimationLayer()
        self.ledger_manager = ledger_manager or VirtualLedgerManager(self.ledger_repo)

    async def execute_rescue(
        self,
        chat_id: int,
        user_message: str,
        current_location: str | None = None,
    ) -> RescueResult:
        """Execute the rescue pipeline for an active trip message."""
        # 1. Load active trip
        active_trip = self.trip_repo.get_active_trip(chat_id)
        if not active_trip:
            logger.info("Rescue requested for chat_id=%s, but no active trip found.", chat_id)
            return RescueResult(
                trip_id=None,
                success=False,
                rescue_type="none",
                user_issue="",
                resolution_summary="No active trip found for this chat. Please plan a trip first using /start.",
                error="NO_ACTIVE_TRIP",
            )

        # 2. Parse AI rescue intent (AI is strictly non-authoritative for math/decisions)
        parsed_intent = await self.ai_service.parse_rescue_intent(user_message)
        logger.info(
            "Parsed rescue intent: type=%s, issue=%s, price=%s",
            parsed_intent.rescue_type,
            parsed_intent.user_issue,
            parsed_intent.reported_price,
        )

        # 3. Route according to classification
        if parsed_intent.rescue_type == "weather_closure":
            return await self._handle_weather_closure(
                trip=active_trip,
                user_message=user_message,
                parsed_intent=parsed_intent,
                current_location=current_location,
            )

        if parsed_intent.rescue_type == "price_dispute":
            return self._handle_price_dispute(
                trip=active_trip,
                user_message=user_message,
                parsed_intent=parsed_intent,
            )

        # Unknown / unclassified rescue intent
        return self._handle_unknown(
            trip=active_trip,
            user_message=user_message,
            parsed_intent=parsed_intent,
        )

    async def _handle_weather_closure(
        self,
        trip: Trip,
        user_message: str,
        parsed_intent: ParsedRescueIntent,
        current_location: str | None = None,
    ) -> RescueResult:
        """Handle weather or venue closure disruption by finding an alternative place."""
        # 1. Load current itinerary
        itinerary_record = self.itinerary_repo.get_itinerary(trip.id)
        if not itinerary_record or not itinerary_record.days:
            return RescueResult(
                trip_id=trip.id,
                success=False,
                rescue_type="weather_closure",
                user_issue=parsed_intent.user_issue,
                resolution_summary="No active itinerary available to modify.",
                error="NO_ITINERARY",
            )

        days = [ItineraryDay.model_validate(d) for d in itinerary_record.days]

        # 2. Identify affected itinerary item
        target_day, target_item = self._find_affected_item(
            days=days,
            context=parsed_intent.location_or_context or current_location,
        )

        old_cost = target_item.planned_cost if target_item else Decimal("0.00")

        # 3. Search Local/Maps through existing CacheFallbackManager (NEVER SerpApi directly)
        dest = trip.destination or "Destination"
        search_query = f"indoor attractions places to visit in {dest}"
        if parsed_intent.location_or_context:
            search_query = f"places near {parsed_intent.location_or_context} {dest}"

        envelope = await self.cache_manager.get_travel_data(
            engine="google_maps",
            params={"q": search_query, "location": dest},
            trip_id=trip.id,
        )

        # 4. Normalize candidates through existing DataNormalizer
        candidates = self.normalizer.normalize_places(envelope)
        valid_candidates = [
            c for c in candidates
            if c.name and (target_item is None or c.name.lower() != (target_item.place_name or "").lower())
        ]

        if not valid_candidates:
            return RescueResult(
                trip_id=trip.id,
                success=False,
                rescue_type="weather_closure",
                user_issue=parsed_intent.user_issue,
                resolution_summary=f"No suitable alternative places could be discovered in {dest}.",
                is_feasible=True,
                error="NO_ALTERNATIVES_FOUND",
            )

        # 5. Deterministic alternative selection (by rating / place data availability)
        selected_alt = sorted(
            valid_candidates,
            key=lambda c: (c.rating is not None, c.rating or 0.0),
            reverse=True,
        )[0]

        # 6. Mini Feasibility Check via ReverseBudgetEngine
        budget_alloc = self.ledger_repo.get_budget_allocation(trip.id)
        if not budget_alloc:
            budget_alloc = BudgetAllocation(
                id=uuid4(),
                trip_id=trip.id,
                transport_allocated=Decimal("0.00"),
                stay_allocated=Decimal("0.00"),
                food_allocated=Decimal("0.00"),
                activities_discretionary=Decimal("0.00"),
                rescue_fund_allocated=trip.budget_total * Decimal("0.10"),
                total_budget=trip.budget_total,
                created_at=utc_now(),
                updated_at=utc_now(),
            )

        new_cost = selected_alt.estimated_cost
        cost_delta = new_cost - old_cost

        eval_result = self.budget_engine.evaluate_rescue(
            total_budget=trip.budget_total,
            current_allocations=budget_alloc,
            cost_delta=cost_delta,
            category="activities",
            currency=trip.currency,
        )

        # 7. Gate on Feasibility
        if not eval_result.is_feasible:
            # INFEASIBLE: Preserve original itinerary and ledger completely intact
            logger.info("Rescue replacement '%s' is NOT feasible: %s", selected_alt.name, eval_result.explanation)
            return RescueResult(
                trip_id=trip.id,
                success=False,
                rescue_type="weather_closure",
                user_issue=parsed_intent.user_issue,
                resolution_summary=f"Proposed replacement '{selected_alt.name}' exceeds budget: {eval_result.explanation}",
                is_feasible=False,
                budget_impact=eval_result.deficit,
                selected_alternative=selected_alt,
                updated_itinerary=None,
                ledger_summary=self.ledger_manager.get_summary(trip.id),
            )

        # FEASIBLE: Apply replacement to itinerary
        if target_item:
            target_item.place_name = selected_alt.name
            target_item.activity = f"Visit {selected_alt.name} ({selected_alt.category or 'attraction'})"
            target_item.planned_cost = selected_alt.estimated_cost
            target_item.source = selected_alt.source
            target_item.notes = f"Rescued: Replaced due to {parsed_intent.user_issue}"

        if target_day:
            target_day.daily_estimated_cost = sum(i.planned_cost for i in target_day.items)

        total_planned = sum(d.daily_estimated_cost for d in days)

        # Persist updated itinerary
        itinerary_record.days = [d.model_dump(mode="json") for d in days]
        self.itinerary_repo.save_itinerary(itinerary_record)

        # Update Virtual Ledger with auditable delta
        self.ledger_manager.record_rescue_adjustment(
            trip_id=trip.id,
            category="activities",
            cost_delta=cost_delta,
            description=f"Rescue replacement: {selected_alt.name}",
            source="estimated",
        )
        ledger_summary = self.ledger_manager.get_summary(trip.id)

        # Persist RescueEvent
        rescue_event = RescueEvent(
            id=uuid4(),
            trip_id=trip.id,
            rescue_type="weather_closure",
            user_message=user_message,
            resolution_summary=f"Replaced with '{selected_alt.name}'. {eval_result.explanation}",
            ledger_impact=cost_delta,
            created_at=utc_now(),
        )
        self.rescue_repo.record_rescue_event(rescue_event)

        updated_itinerary = GeneratedItinerary(
            trip_id=trip.id,
            destination=dest,
            days_count=len(days),
            days=days,
            is_feasible=True,
            total_budget=trip.budget_total,
            total_planned_cost=total_planned,
            feasibility_note=eval_result.explanation,
        )

        return RescueResult(
            trip_id=trip.id,
            success=True,
            rescue_type="weather_closure",
            user_issue=parsed_intent.user_issue,
            resolution_summary=f"Replaced with '{selected_alt.name}' within budget. {eval_result.explanation}",
            is_feasible=True,
            budget_impact=cost_delta,
            selected_alternative=selected_alt,
            updated_itinerary=updated_itinerary,
            ledger_summary=ledger_summary,
        )

    def _handle_price_dispute(
        self,
        trip: Trip,
        user_message: str,
        parsed_intent: ParsedRescueIntent,
    ) -> RescueResult:
        """Handle fare dispute using local estimation rate heuristics without SerpApi calls."""
        reported_price = parsed_intent.reported_price or Decimal("0.00")
        mode = (parsed_intent.service_type or "auto").lower()
        if "taxi" in mode or "cab" in mode:
            transit_mode = "cab"
        else:
            transit_mode = "auto"

        # Advisory distance heuristic for in-city point-to-point ride (default 10 km)
        standard_distance_km = 10.0
        estimate = self.estimation_layer.estimate_local_transit_distance(
            distance_km=standard_distance_km,
            mode=transit_mode,
        )

        diff = reported_price - estimate.total_cost
        if diff <= Decimal("0.00"):
            status = "fair"
        elif diff <= Decimal("100.00"):
            status = "slightly_high"
        else:
            status = "significantly_high"

        advisory_notes = (
            f"Advisory fare guidance based on standard {transit_mode} rate tables ({trip.currency} {estimate.rate_per_km}/km). "
            f"Quoted {trip.currency} {reported_price} vs estimated {trip.currency} {estimate.total_cost} for ~{standard_distance_km} km. "
            "This is an estimated heuristic, not a guaranteed or legally binding price."
        )

        guidance = FareGuidance(
            mode=transit_mode,
            reported_price=reported_price,
            estimated_fare=estimate.total_cost,
            difference=diff,
            rate_per_km=estimate.rate_per_km,
            distance_km=standard_distance_km,
            status=status,
            advisory_notes=advisory_notes,
        )

        # Record user-reported spending in Virtual Ledger (retains USER_REPORTED provenance)
        if reported_price > Decimal("0.00"):
            self.ledger_manager.record_spending(
                trip_id=trip.id,
                category="daily_survival",
                amount=reported_price,
                description=f"Reported {transit_mode} fare: {user_message}",
                source="user_reported",
            )
        ledger_summary = self.ledger_manager.get_summary(trip.id)

        # Persist RescueEvent
        rescue_event = RescueEvent(
            id=uuid4(),
            trip_id=trip.id,
            rescue_type="price_dispute",
            user_message=user_message,
            resolution_summary=f"Fare guidance provided: quoted {trip.currency} {reported_price} vs est. {trip.currency} {estimate.total_cost} ({status}).",
            ledger_impact=reported_price,
            created_at=utc_now(),
        )
        self.rescue_repo.record_rescue_event(rescue_event)

        return RescueResult(
            trip_id=trip.id,
            success=True,
            rescue_type="price_dispute",
            user_issue=parsed_intent.user_issue,
            resolution_summary=advisory_notes,
            is_feasible=True,
            budget_impact=reported_price,
            fare_guidance=guidance,
            ledger_summary=ledger_summary,
        )

    def _handle_unknown(
        self,
        trip: Trip,
        user_message: str,
        parsed_intent: ParsedRescueIntent,
    ) -> RescueResult:
        """Handle unclassified messages safely without touching trip, itinerary, or ledger."""
        logger.info("Unclassified rescue request received: '%s'", user_message)
        return RescueResult(
            trip_id=trip.id,
            success=False,
            rescue_type="unknown",
            user_issue=parsed_intent.user_issue,
            resolution_summary="Clarification required: Budlance Rescue Mode can assist with weather disruptions, venue closures, or transport fare disputes.",
            is_feasible=True,
            error=None,
        )

    def _find_affected_item(
        self,
        days: list[ItineraryDay],
        context: str | None,
    ) -> tuple[ItineraryDay | None, ItineraryItem | None]:
        """Find the most relevant itinerary item matching context or category."""
        if not days:
            return None, None

        # 1. Match context in place_name or activity
        if context:
            ctx_clean = context.lower()
            for day in days:
                for item in day.items:
                    if (item.place_name and ctx_clean in item.place_name.lower()) or (ctx_clean in item.activity.lower()):
                        return day, item

        # 2. Look for an attraction / beach / nature item
        for day in days:
            for item in day.items:
                if item.category in ("attraction", "beach", "nature"):
                    return day, item

        # 3. Fallback to first afternoon or morning activity in day 1
        day1 = days[0]
        if day1.items:
            for item in day1.items:
                if item.category not in ("transport", "accommodation"):
                    return day1, item
            return day1, day1.items[0]

        return None, None
