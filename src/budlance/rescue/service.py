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
import re
from typing import Any
from uuid import UUID, uuid4

from budlance.ai.schemas import ParsedRescueIntent
from budlance.ai.service import AIIntentService
from budlance.cache.manager import CacheFallbackManager
from budlance.config import get_settings
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
from budlance.serpapi.models import DataSource
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
        as_proposal: bool = False,
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
                as_proposal=as_proposal,
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
        as_proposal: bool = False,
    ) -> RescueResult:
        """Handle weather, venue closure, or transit disruption by finding an alternative."""
        msg_lower = (user_message or "").lower()

        # Handle transit cancellation (e.g. "airport bus is cancelled, find alternative under ₹700")
        if parsed_intent.service_type == "transit" or ("airport" in msg_lower and any(w in msg_lower for w in ("bus", "transit", "cab"))):
            budget_limit = Decimal("700.00")
            lim_match = re.search(r"(?:under|below|max|within)\s*(?:₹|rs\.?|inr)?\s*(\d+)", msg_lower)
            if lim_match:
                try:
                    budget_limit = Decimal(lim_match.group(1))
                except Exception:
                    pass

            est_transit_cost = Decimal("600.00")
            transit_alt = PlaceOption(
                name="App Cab / Pre-paid Airport Taxi",
                category="transit",
                address=f"{trip.destination or 'City'} to Airport",
                estimated_cost=est_transit_cost,
                source=DataSource.ESTIMATED,
            )
            is_feasible = est_transit_cost <= budget_limit
            reserve_alloc = trip.budget_total * Decimal("0.10")

            proposal_data = {
                "trip_id": str(trip.id),
                "is_transit": True,
                "selected_alt": transit_alt.model_dump(mode="json"),
                "estimated_cost": float(est_transit_cost),
                "budget_limit": float(budget_limit),
                "user_issue": "Airport bus cancelled",
            }

            res_text = (
                f"🚌 *Emergency Transport Alternative:*\n\n"
                f"• Option: *App Cab / Pre-paid Airport Taxi*\n"
                f"• Estimated Fare: ₹{est_transit_cost:,.2f} (Estimate, within your limit of ₹{budget_limit:,.2f})\n"
                f"• Potential Reserve Impact: ₹{est_transit_cost:,.2f} (Available Reserve: ₹{reserve_alloc:,.2f})\n"
                f"• Booking Status: *Not Booked* (Budlance did not purchase or charge this journey)\n\n"
                f"Your flight and hotel bookings remain untouched. Reply 'Confirm' if you would like to record this transfer, or 'Cancel' to dismiss."
            )

            return RescueResult(
                trip_id=trip.id,
                success=True,
                rescue_type="weather_closure",
                user_issue="Transit cancellation",
                resolution_summary=res_text,
                is_feasible=is_feasible,
                budget_impact=est_transit_cost,
                selected_alternative=transit_alt,
                ledger_summary=self.ledger_manager.get_summary(trip.id),
                is_proposal=as_proposal,
                pending_proposal=proposal_data if as_proposal else None,
            )

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
        if any(w in msg_lower for w in ("nature", "nature spot", "park", "garden")):
            search_query = f"nature spots parks places to visit in {dest}"

        settings = get_settings()
        envelope = await self.cache_manager.get_travel_data(
            engine="google_maps",
            params={
                "q": search_query,
                "location": dest,
                "m": settings.maps_search_radius_meters,
            },
            trip_id=trip.id,
        )

        # 4. Normalize candidates through existing DataNormalizer
        candidates = self.normalizer.normalize_places(envelope)
        valid_candidates = [
            c for c in candidates
            if c.name and (target_item is None or c.name.lower() != (target_item.place_name or "").lower())
        ]

        # Prioritize nature spots if requested by user
        if any(w in msg_lower for w in ("nature", "nature spot")):
            nature_matches = [
                c for c in valid_candidates
                if "nature" in (c.category or "").lower() or any(kw in c.name.lower() for kw in ("nature", "park", "garden", "wildlife", "sanctuary", "beach", "lake", "falls", "hill", "viewpoint"))
            ]
            if nature_matches:
                valid_candidates = nature_matches

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

        # Estimate transfer distance & taxi cost from Bucket D (Buffer / Reserve)
        transfer_dist_km = 4.5
        dist_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:km|kms|kilometer|kilometers)", user_message, re.IGNORECASE)
        if dist_match:
            try:
                transfer_dist_km = float(dist_match.group(1))
            except ValueError:
                pass
        taxi_est = self.estimation_layer.estimate_local_transit_distance(
            distance_km=transfer_dist_km,
            mode="taxi",
        )
        taxi_fare = taxi_est.total_cost if (taxi_est and taxi_est.total_cost) else Decimal("180.00")

        # If presented as proposal (Phase 7 invariant: proposal is not an applied change)
        if as_proposal:
            proposal_dict = {
                "trip_id": str(trip.id),
                "target_day_num": target_day.day_number if target_day else trip.current_day,
                "target_item_place": target_item.place_name if target_item else None,
                "selected_alt": selected_alt.model_dump(mode="json"),
                "old_cost": float(old_cost),
                "new_cost": float(new_cost),
                "cost_delta": float(cost_delta),
                "transfer_distance_km": float(transfer_dist_km),
                "taxi_fare": float(taxi_fare),
                "user_issue": parsed_intent.user_issue,
            }
            summary_txt = (
                f"Proposed replacement for {target_item.place_name if target_item else 'scheduled activity'}: "
                f"*{selected_alt.name}* ({selected_alt.category or 'nature/attraction'}). "
                f"Known admission cost: ₹{selected_alt.estimated_cost:,.2f} (budget impact: ₹{cost_delta:,.2f}).\n"
                f"• Estimated taxi transfer ({transfer_dist_km:g} km): ₹{taxi_fare:,.2f} (Allocated from Bucket D: Buffer / Reserve)\n"
                f"• Your hotel and return flight remain untouched.\n\n"
                f"Reply 'I will go', 'Yes', or 'Confirm' to apply this change to your itinerary, or 'No' / 'Cancel' to keep your current plan."
            )
            return RescueResult(
                trip_id=trip.id,
                success=True,
                rescue_type="weather_closure",
                user_issue=parsed_intent.user_issue,
                resolution_summary=summary_txt,
                is_feasible=True,
                budget_impact=cost_delta,
                selected_alternative=selected_alt,
                updated_itinerary=None,
                ledger_summary=self.ledger_manager.get_summary(trip.id),
                is_proposal=True,
                pending_proposal=proposal_dict,
            )

        # FEASIBLE (auto-commit): Apply replacement to itinerary immediately
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

        # Parse distance from parsed_intent or directly from user_message
        word_to_dist = {
            "one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0, "five": 5.0,
            "six": 6.0, "seven": 7.0, "eight": 8.0, "nine": 9.0, "ten": 10.0,
            "eleven": 11.0, "twelve": 12.0, "fifteen": 15.0, "twenty": 20.0,
        }
        distance_km: float | None = parsed_intent.distance_km
        if distance_km is None:
            dist_match = re.search(
                r"(?:(\d+(?:\.\d+)?)|(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty))\s*(?:km|kms|kilometer|kilometers|kilometre|kilometres)\b",
                user_message,
                re.IGNORECASE,
            )
            if dist_match:
                try:
                    if dist_match.group(1):
                        distance_km = float(dist_match.group(1))
                    else:
                        distance_km = word_to_dist.get(dist_match.group(2).lower())
                except (ValueError, TypeError):
                    distance_km = None

        if distance_km is not None:
            estimate = self.estimation_layer.estimate_local_transit_distance(
                distance_km=distance_km,
                mode=transit_mode,
            )
            if not estimate.is_available or estimate.total_cost is None:
                guidance = FareGuidance(
                    mode=transit_mode,
                    reported_price=reported_price,
                    estimated_fare=None,
                    difference=None,
                    rate_per_km=None,
                    distance_km=distance_km,
                    status="unavailable",
                    advisory_notes=(
                        f"Fare estimate unavailable: {estimate.basis or f'No configured rate table for transit mode {transit_mode}'}. "
                        f"{estimate.limitations or ''}"
                    ).strip(),
                    is_available=False,
                )
                res_summary = (
                    f"Fare guidance unavailable for '{transit_mode}': no defensible configured rate or live fare available."
                )
            else:
                diff = reported_price - estimate.total_cost
                if diff <= Decimal("0.00"):
                    status = "fair"
                elif diff <= Decimal("100.00"):
                    status = "slightly_high"
                else:
                    status = "significantly_high"

                advisory_notes = (
                    f"Configured estimate based on default rate tables ({trip.currency} {estimate.rate_per_km}/km). "
                    f"Quoted {trip.currency} {reported_price} vs configured estimate {trip.currency} {estimate.total_cost} for {distance_km:g} km. "
                    "This is a configured estimate heuristic, not an authoritative statutory government tariff or legally binding price."
                )

                guidance = FareGuidance(
                    mode=transit_mode,
                    reported_price=reported_price,
                    estimated_fare=estimate.total_cost,
                    difference=diff,
                    rate_per_km=estimate.rate_per_km,
                    distance_km=distance_km,
                    status=status,
                    advisory_notes=advisory_notes,
                    is_available=True,
                )
                res_summary = (
                    f"Fare guidance provided: quoted {trip.currency} {reported_price} vs est. "
                    f"{trip.currency} {estimate.total_cost} for {distance_km:g} km ({status})."
                )
        else:
            # Distance missing: remove silent 10 km default.
            # Explicitly state the estimate is approximate and prompt user for distance.
            approx_baseline_km = 8.0
            estimate = self.estimation_layer.estimate_local_transit_distance(
                distance_km=approx_baseline_km,
                mode=transit_mode,
            )
            if not estimate.is_available or estimate.total_cost is None:
                guidance = FareGuidance(
                    mode=transit_mode,
                    reported_price=reported_price,
                    estimated_fare=None,
                    difference=None,
                    rate_per_km=None,
                    distance_km=None,
                    status="unavailable",
                    advisory_notes=(
                        f"Fare estimate unavailable: {estimate.basis or f'No configured rate table for transit mode {transit_mode}'}. "
                        f"{estimate.limitations or ''}"
                    ).strip(),
                    is_available=False,
                )
                res_summary = (
                    f"Fare guidance unavailable for '{transit_mode}': no defensible configured rate or live fare available."
                )
            else:
                diff = reported_price - estimate.total_cost
                if diff <= Decimal("0.00"):
                    status = "fair"
                elif diff <= Decimal("100.00"):
                    status = "slightly_high"
                else:
                    status = "significantly_high"

                advisory_notes = (
                    f"Configured estimate: this estimate is approximate as no ride distance was specified. "
                    f"Standard configured {transit_mode} rate table is {trip.currency} {estimate.rate_per_km}/km (approximate baseline {trip.currency} {estimate.total_cost}). "
                    f"Quoted {trip.currency} {reported_price}. Please provide your ride distance (e.g. '5 km') for an exact fare check."
                )

                guidance = FareGuidance(
                    mode=transit_mode,
                    reported_price=reported_price,
                    estimated_fare=estimate.total_cost,
                    difference=diff,
                    rate_per_km=estimate.rate_per_km,
                    distance_km=None,  # Explicitly None - silent 10 km default removed
                    status=status,
                    advisory_notes=advisory_notes,
                    is_available=True,
                )
                res_summary = (
                    f"Fare guidance provided (approximate estimate): quoted {trip.currency} {reported_price} vs approx. "
                    f"{trip.currency} {estimate.total_cost} ({status}). Please specify ride distance for an exact check."
                )

        # Distinguish advisory quote inquiries from actual spending.
        # An advisory quote check must NOT automatically become actual spending in the Virtual Ledger.
        is_quote_inquiry = bool(
            re.search(
                r"\b(quote|quoted|asking whether)\b|\bfair\b",
                user_message,
                re.IGNORECASE,
            )
        )

        ledger_impact = Decimal("0.00") if is_quote_inquiry else reported_price

        # Record user-reported spending in Virtual Ledger only if not an advisory quote inquiry
        if not is_quote_inquiry and reported_price > Decimal("0.00"):
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
            resolution_summary=res_summary,
            ledger_impact=ledger_impact,
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
            budget_impact=ledger_impact,
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

    def apply_confirmed_rescue(
        self,
        chat_id: int,
        proposal: dict[str, Any],
    ) -> RescueResult:
        """Apply an approved rescue proposal to the itinerary and ledger."""
        trip_id = UUID(proposal["trip_id"])
        trip = self.trip_repo.get_trip(trip_id)
        if not trip:
            return RescueResult(
                trip_id=trip_id,
                success=False,
                rescue_type="weather_closure",
                user_issue="Rescue confirmed",
                resolution_summary="No active trip found.",
                error="NO_TRIP",
            )

        # Handle transit rescue approval
        if proposal.get("is_transit"):
            est_cost = Decimal(str(proposal.get("estimated_cost", "0.00")))
            alt_dict = proposal["selected_alt"]
            transit_alt = PlaceOption.model_validate(alt_dict)
            return RescueResult(
                trip_id=trip_id,
                success=True,
                rescue_type="weather_closure",
                user_issue=proposal.get("user_issue", "Transit confirmed"),
                resolution_summary=(
                    f"✅ Confirmed: Transport alternative *{transit_alt.name}* noted for your schedule. "
                    f"Estimated fare: ₹{est_cost:,.2f}. No booking or payment has been made automatically; "
                    f"record expenses as usual after your ride."
                ),
                is_feasible=True,
                budget_impact=est_cost,
                selected_alternative=transit_alt,
                ledger_summary=self.ledger_manager.get_summary(trip_id),
                is_proposal=False,
            )

        itinerary_record = self.itinerary_repo.get_itinerary(trip_id)
        if not itinerary_record or not itinerary_record.days:
            return RescueResult(
                trip_id=trip_id,
                success=False,
                rescue_type="weather_closure",
                user_issue=proposal.get("user_issue", "Rescue"),
                resolution_summary="No active itinerary found to update.",
                error="NO_ITINERARY",
            )

        days = [ItineraryDay.model_validate(d) for d in itinerary_record.days]
        target_day_num = proposal.get("target_day_num", 1)
        target_day = next((d for d in days if d.day_number == target_day_num), None)
        target_place = proposal.get("target_item_place")

        alt_dict = proposal["selected_alt"]
        selected_alt = PlaceOption.model_validate(alt_dict)
        cost_delta = Decimal(str(proposal.get("cost_delta", 0)))

        target_item = None
        if target_day and target_day.items:
            if target_place:
                target_item = next((it for it in target_day.items if (it.place_name or "").lower() == target_place.lower()), None)
            if not target_item:
                target_item = target_day.items[0]

        if target_item:
            target_item.place_name = selected_alt.name
            target_item.activity = f"Visit {selected_alt.name} ({selected_alt.category or 'attraction'})"
            target_item.planned_cost = selected_alt.estimated_cost
            target_item.source = selected_alt.source
            target_item.notes = f"Rescued: Replaced due to {proposal.get('user_issue')}"

        if target_day:
            target_day.daily_estimated_cost = sum(i.planned_cost for i in target_day.items)

        itinerary_record.days = [d.model_dump(mode="json") for d in days]
        self.itinerary_repo.save_itinerary(itinerary_record)

        self.ledger_manager.record_rescue_adjustment(
            trip_id=trip_id,
            category="activities",
            cost_delta=cost_delta,
            description=f"Rescue replacement: {selected_alt.name}",
            source="estimated",
        )
        ledger_summary = self.ledger_manager.get_summary(trip_id)

        # Persist RescueEvent
        rescue_event = RescueEvent(
            trip_id=trip_id,
            rescue_type="weather_closure",
            user_message=proposal.get("user_issue", "Rescue confirmed"),
            resolution_summary=f"Replaced {target_place} with {selected_alt.name} upon user confirmation",
            ledger_impact=cost_delta,
            created_at=utc_now(),
        )
        self.rescue_repo.record_rescue_event(rescue_event)

        updated_itin = GeneratedItinerary(
            trip_id=trip_id,
            destination=trip.destination or "Destination",
            days_count=len(days),
            days=days,
            total_budget=trip.budget_total or Decimal("0.00"),
            total_planned_cost=sum(d.daily_estimated_cost for d in days),
        )

        transfer_dist_km = float(proposal.get("transfer_distance_km", 4.5))
        taxi_fare = float(proposal.get("taxi_fare", 180.0))
        taxi_text = f" Taxi transfer ({transfer_dist_km:g} km): ~₹{taxi_fare:,.2f} from Bucket D Buffer Reserve." if "taxi_fare" in proposal else ""
        resolution_msg = f"✅ Confirmed: Replaced {target_place or 'activity'} with *{selected_alt.name}* in your itinerary.{taxi_text} Your hotel and return flight remain unchanged."

        return RescueResult(
            trip_id=trip_id,
            success=True,
            rescue_type="weather_closure",
            user_issue=proposal.get("user_issue", ""),
            resolution_summary=resolution_msg,
            is_feasible=True,
            budget_impact=cost_delta,
            selected_alternative=selected_alt,
            updated_itinerary=updated_itin,
            ledger_summary=ledger_summary,
            is_proposal=False,
        )

    def cancel_pending_rescue(
        self,
        chat_id: int,
        proposal: dict[str, Any] | None = None,
    ) -> RescueResult:
        """Cancel a pending rescue proposal, leaving the original itinerary unchanged."""
        trip_id = UUID(proposal["trip_id"]) if proposal and "trip_id" in proposal else None
        target_place = proposal.get("target_item_place") if proposal else "scheduled activity"
        return RescueResult(
            trip_id=trip_id,
            success=True,
            rescue_type="weather_closure",
            user_issue="Rescue proposal cancelled",
            resolution_summary=f"❌ Cancelled: Kept your original plan with {target_place} unchanged. No modifications were made.",
            is_feasible=True,
            is_proposal=False,
        )
