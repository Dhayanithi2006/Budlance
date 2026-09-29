"""Budlance Orchestrator coordinating AI intent, travel data, budget engine, optimization, and persistence.

Architectural Boundaries:
- Coordinates existing services; contains NO low-level formulas, raw math, or SerpApi logic.
- Enforces strict reverse-budget feasibility gate before itinerary and ledger creation.
- Dispatches in-trip rescue messages to RescueService.
- Provides clean error boundaries protecting against exposed secrets.
"""

from decimal import Decimal
import logging
from typing import Any
from uuid import UUID, uuid4

from budlance.ai.schemas import ParsedTripIntent
from budlance.ai.service import AIIntentService
from budlance.cache.manager import CacheFallbackManager
from budlance.db.repositories.intent_repo import IntentRepository
from budlance.db.repositories.itinerary_repo import ItineraryRepository
from budlance.db.repositories.ledger_repo import LedgerRepository
from budlance.db.repositories.rescue_repo import RescueRepository
from budlance.db.repositories.trip_repo import TripRepository
from budlance.db.repositories.user_repo import UserRepository
from budlance.engine.budget import ReverseBudgetEngine
from budlance.engine.models import BudgetEvaluationResult, OptimizationResult
from budlance.engine.optimizer import OptimizationEngine
from budlance.estimation.estimator import EstimationLayer
from budlance.itinerary.generator import ItineraryGenerator
from budlance.ledger.manager import VirtualLedgerManager
from budlance.normalization.normalizer import DataNormalizer
from budlance.orchestrator.formatter import (
    format_clarification,
    format_feasible_plan,
    format_infeasible_plan,
    format_rescue_result,
)
from budlance.orchestrator.models import OrchestrationResult
from budlance.rescue.service import RescueService
from budlance.schemas.travel import FlightOption, HotelOption, PlaceOption, RouteOption, TransitOption
from budlance.serpapi.models import DataSource

logger = logging.getLogger(__name__)


class BudlanceOrchestrator:
    """Central coordinator for end-to-end trip planning and in-trip rescue flows."""

    def __init__(
        self,
        user_repo: UserRepository | None = None,
        trip_repo: TripRepository | None = None,
        intent_repo: IntentRepository | None = None,
        itinerary_repo: ItineraryRepository | None = None,
        ledger_repo: LedgerRepository | None = None,
        rescue_repo: RescueRepository | None = None,
        ai_service: AIIntentService | None = None,
        cache_manager: CacheFallbackManager | None = None,
        normalizer: DataNormalizer | None = None,
        estimation_layer: EstimationLayer | None = None,
        budget_engine: ReverseBudgetEngine | None = None,
        optimizer: OptimizationEngine | None = None,
        itinerary_generator: ItineraryGenerator | None = None,
        ledger_manager: VirtualLedgerManager | None = None,
        rescue_service: RescueService | None = None,
    ) -> None:
        self.user_repo = user_repo or UserRepository()
        self.trip_repo = trip_repo or TripRepository()
        self.intent_repo = intent_repo or IntentRepository()
        self.itinerary_repo = itinerary_repo or ItineraryRepository()
        self.ledger_repo = ledger_repo or LedgerRepository()
        self.rescue_repo = rescue_repo or RescueRepository()

        self.ai_service = ai_service or AIIntentService()
        self.cache_manager = cache_manager or CacheFallbackManager()
        self.normalizer = normalizer or DataNormalizer()
        self.estimation = estimation_layer or EstimationLayer()
        self.budget_engine = budget_engine or ReverseBudgetEngine()
        self.optimizer = optimizer or OptimizationEngine(
            budget_engine=self.budget_engine,
            estimation_layer=self.estimation,
        )
        self.itinerary_generator = itinerary_generator or ItineraryGenerator(self.itinerary_repo)
        self.ledger_manager = ledger_manager or VirtualLedgerManager(self.ledger_repo)

        self.rescue_service = rescue_service or RescueService(
            trip_repo=self.trip_repo,
            itinerary_repo=self.itinerary_repo,
            ledger_repo=self.ledger_repo,
            rescue_repo=self.rescue_repo,
            ai_service=self.ai_service,
            cache_manager=self.cache_manager,
            normalizer=self.normalizer,
            budget_engine=self.budget_engine,
            estimation_layer=self.estimation,
            ledger_manager=self.ledger_manager,
        )

    async def handle_user_message(
        self,
        telegram_user_id: int,
        chat_id: int,
        message: str,
        username: str | None = None,
        first_name: str | None = None,
    ) -> OrchestrationResult:
        """Process an incoming Telegram message through the planning or rescue pipeline."""
        clean_text = message.strip()
        if not clean_text:
            return OrchestrationResult(
                status="CLARIFICATION",
                message_text="👋 Please send your trip request with budget, number of people, duration, and origin!",
            )

        try:
            # 1. First evaluate if this is a live trip Rescue message
            parsed_rescue = await self.ai_service.parse_rescue_intent(clean_text)
            if parsed_rescue.rescue_type in ("weather_closure", "price_dispute"):
                logger.info(
                    "Detected rescue intent (%s) for chat_id=%s. Routing to RescueService.",
                    parsed_rescue.rescue_type,
                    chat_id,
                )
                rescue_res = await self.rescue_service.execute_rescue(
                    chat_id=chat_id,
                    user_message=clean_text,
                )
                return OrchestrationResult(
                    trip_id=rescue_res.trip_id,
                    status="RESCUE",
                    selected_destination=None,
                    feasibility_status="FEASIBLE" if rescue_res.is_feasible else "NOT_FEASIBLE",
                    generated_itinerary=rescue_res.updated_itinerary,
                    ledger_summary=rescue_res.ledger_summary,
                    message_text=format_rescue_result(rescue_res),
                    error=rescue_res.error,
                )

            # 2. Planning Intent Extraction
            parsed_intent = await self.ai_service.parse_trip_intent(clean_text)

            # 3. Validate Required Planning Inputs (Task C)
            missing_fields = []
            if parsed_intent.budget is None or parsed_intent.budget <= Decimal("0.00"):
                missing_fields.append("budget")
            if parsed_intent.people is None or parsed_intent.people <= 0:
                missing_fields.append("people")
            if parsed_intent.days is None or parsed_intent.days <= 0:
                missing_fields.append("days")
            if not parsed_intent.origin:
                missing_fields.append("origin")

            if missing_fields:
                logger.info("Trip intent missing required fields: %s. Returning clarification.", missing_fields)
                return OrchestrationResult(
                    status="CLARIFICATION",
                    message_text=format_clarification(missing_fields),
                )

            # 4. User Resolution
            user = self.user_repo.get_or_create_user(
                telegram_user_id=telegram_user_id,
                username=username,
                first_name=first_name,
            )

            # 5. Destination Evaluation / Discovery (Task D)
            origin = parsed_intent.origin or "Origin"
            budget = parsed_intent.budget or Decimal("0.00")
            people = parsed_intent.people or 1
            days = parsed_intent.days or 1

            if parsed_intent.destination and parsed_intent.destination.strip():
                candidate_destinations = [parsed_intent.destination.strip().title()]
            else:
                logger.info("Destination absent. Engaging Destination Discovery via Travel Explore...")
                candidate_destinations = await self._discover_destinations(
                    origin=origin,
                    budget=budget,
                    interests=parsed_intent.interests,
                )

            # 6. Evaluate candidate destinations against Reverse-Budget Engine
            selected_plan: dict[str, Any] | None = None
            last_infeasible_result: BudgetEvaluationResult | None = None
            last_opt_result: OptimizationResult | None = None

            for dest in candidate_destinations:
                logger.info("Evaluating candidate destination: %s", dest)
                plan = await self._evaluate_trip_candidate(
                    origin=origin,
                    destination=dest,
                    people=people,
                    days=days,
                    budget=budget,
                    currency=parsed_intent.currency,
                )
                if plan["is_feasible"]:
                    selected_plan = plan
                    break
                else:
                    last_infeasible_result = plan.get("baseline_eval")
                    last_opt_result = plan.get("opt_result")

            # 7. Check final feasibility result
            if not selected_plan or not selected_plan["is_feasible"]:
                # NOT_FEASIBLE plan: Do NOT create itinerary or ledger!
                target_dest = candidate_destinations[0] if candidate_destinations else "Requested Destination"
                deficit = (
                    last_opt_result.deficit
                    if last_opt_result
                    else (last_infeasible_result.deficit if last_infeasible_result else budget * Decimal("0.20"))
                )
                explanation = (
                    last_opt_result.explanation
                    if last_opt_result
                    else (last_infeasible_result.explanation if last_infeasible_result else "Trip exceeds budget constraint.")
                )
                recommendation = last_opt_result.recommendation if last_opt_result else None

                return OrchestrationResult(
                    status="NOT_FEASIBLE",
                    selected_destination=target_dest,
                    feasibility_status="NOT_FEASIBLE",
                    message_text=format_infeasible_plan(
                        destination=target_dest,
                        budget=budget,
                        deficit=deficit,
                        explanation=explanation,
                        recommendation=recommendation,
                        currency=parsed_intent.currency,
                    ),
                )

            # 8. Successfully FEASIBLE: Execute Coherent Persistence Sequence (Task K)
            # a. Create Active Trip
            chosen_dest = selected_plan["destination"]
            final_days = selected_plan["days"]
            final_eval: BudgetEvaluationResult = selected_plan["evaluation"]
            transport = selected_plan["transport"]
            hotel = selected_plan["hotel"]
            places = selected_plan["places"]
            route = selected_plan["route"]
            opt_result: OptimizationResult | None = selected_plan.get("opt_result")

            trip = self.trip_repo.create_trip(
                user_id=user.id,
                telegram_chat_id=chat_id,
                budget_total=budget,
                destination=chosen_dest,
                origin=origin,
                currency=parsed_intent.currency,
                people_count=people,
                duration_days=final_days,
                is_active=True,
            )

            # b. Persist Trip Intent
            parsed_intent.destination = chosen_dest
            parsed_intent.days = final_days
            intent_record = parsed_intent.to_trip_intent_record(
                trip_id=trip.id,
                raw_prompt=clean_text,
            )
            self.intent_repo.save_trip_intent(intent_record)

            # c. Generate and Persist Itinerary
            generated_itin = self.itinerary_generator.generate(
                trip_id=trip.id,
                destination=chosen_dest,
                evaluation=final_eval,
                days=final_days,
                transport=transport,
                hotel=hotel,
                places=places,
                route=route,
            )

            # d. Initialize Virtual Ledger
            ledger_summary = self.ledger_manager.initialize_ledger(
                trip_id=trip.id,
                evaluation=final_eval,
            )

            # e. Format presentation message for Telegram
            downgrades = opt_result.downgrades_applied if opt_result else []
            msg_text = format_feasible_plan(
                destination=chosen_dest,
                days=final_days,
                people=people,
                breakdown=final_eval.breakdown,
                transport=transport,
                hotel=hotel,
                itinerary=generated_itin,
                ledger=ledger_summary,
                downgrades=downgrades,
            )

            return OrchestrationResult(
                trip_id=trip.id,
                status="FEASIBLE",
                selected_destination=chosen_dest,
                feasibility_status="FEASIBLE",
                selected_transport=transport,
                selected_hotel=hotel,
                selected_places=places,
                selected_route=route,
                budget_breakdown=final_eval.breakdown,
                optimization_attempts=opt_result.total_attempts if opt_result else 0,
                downgrades_applied=downgrades,
                generated_itinerary=generated_itin,
                ledger_summary=ledger_summary,
                message_text=msg_text,
            )

        except Exception as exc:
            logger.error("Unhandled error in Budlance Orchestrator: %s", exc, exc_info=True)
            return OrchestrationResult(
                status="ERROR",
                error="Internal processing error occurred.",
                message_text=(
                    "⚠️ *Oops! An unexpected error occurred while processing your request.*\n\n"
                    "Our team has been notified. Please try again in a moment or rephrase your request."
                ),
            )

    async def _discover_destinations(
        self,
        origin: str,
        budget: Decimal,
        interests: list[str],
    ) -> list[str]:
        """Discover candidate destinations using Google Travel Explore through Cache/Fallback."""
        envelope = await self.cache_manager.get_travel_data(
            engine="google_travel_explore",
            params={
                "origin": origin,
                "budget": float(budget),
                "interests": ",".join(interests),
            },
            trip_id=None,
        )

        discovered: list[str] = []
        if isinstance(envelope.data, dict):
            for item in envelope.data.get("destinations") or envelope.data.get("results") or []:
                name = item.get("destination") or item.get("city") or item.get("name")
                if name and str(name).lower() != origin.lower():
                    discovered.append(str(name).title())

        if not discovered:
            # Deterministic regional catalog matching budget corridors
            catalog = ["Goa", "Jaipur", "Udaipur", "Kerala", "Ooty", "Coorg", "Manali"]
            discovered = [c for c in catalog if c.lower() != origin.lower()]

        return discovered

    async def _evaluate_trip_candidate(
        self,
        origin: str,
        destination: str,
        people: int,
        days: int,
        budget: Decimal,
        currency: str = "INR",
    ) -> dict[str, Any]:
        """Collect travel components, normalize, estimate, and evaluate through Reverse-Budget Engine."""
        # 1. Collect required travel components through Cache/Fallback/SerpApi pipeline
        (
            primary_transport,
            available_transports,
            primary_hotel,
            available_hotels,
            places,
            route,
        ) = await self._collect_travel_components(
            origin=origin,
            destination=destination,
            people=people,
            days=days,
        )

        # 2. Estimation Layer for non-live costs
        food_est = self.estimation.estimate_food(people=people, days=days, tier="standard")
        transit_est = self.estimation.estimate_local_transit_daily(days=days, people=people, mode="metro_bus")
        activities_budget = round(budget * Decimal("0.05"), 2)

        # 3. Authoritative Baseline Evaluation via ReverseBudgetEngine
        baseline_eval = self.budget_engine.evaluate(
            total_budget=budget,
            people=people,
            days=days,
            transport=primary_transport,
            hotel=primary_hotel,
            food_estimate=food_est,
            local_transit_estimate=transit_est,
            activities_budget=activities_budget,
            currency=currency,
        )

        if baseline_eval.is_feasible:
            return {
                "is_feasible": True,
                "destination": destination,
                "days": days,
                "transport": primary_transport,
                "hotel": primary_hotel,
                "places": places,
                "route": route,
                "evaluation": baseline_eval,
                "opt_result": None,
            }

        # 4. If Over-Budget: Engage 4-Step OptimizationEngine (Task B)
        logger.info("Destination %s is initially NOT_FEASIBLE. Engaging 4-step Optimizer...", destination)
        opt_result = self.optimizer.optimize(
            trip_id=uuid4(),
            total_budget=budget,
            people=people,
            days=days,
            initial_transport=primary_transport,
            initial_hotel=primary_hotel,
            initial_food=food_est,
            initial_transit=transit_est,
            activities_budget=activities_budget,
            available_hotels=available_hotels,
            available_transports=available_transports,
            currency=currency,
        )

        if opt_result.is_feasible:
            return {
                "is_feasible": True,
                "destination": destination,
                "days": opt_result.days,
                "transport": opt_result.selected_transport,
                "hotel": opt_result.selected_hotel,
                "places": places,
                "route": route,
                "evaluation": opt_result.final_evaluation,
                "opt_result": opt_result,
            }

        return {
            "is_feasible": False,
            "destination": destination,
            "days": days,
            "baseline_eval": baseline_eval,
            "opt_result": opt_result,
        }

    async def _collect_travel_components(
        self,
        origin: str,
        destination: str,
        people: int,
        days: int,
    ) -> tuple[
        FlightOption | TransitOption | None,
        list[FlightOption | TransitOption],
        HotelOption | None,
        list[HotelOption],
        list[PlaceOption],
        RouteOption | None,
    ]:
        """Fetch and normalize travel, stay, places, and routes from Cache/Fallback/SerpApi."""
        # a. Transports (Flights + Trains/Buses)
        flight_env = await self.cache_manager.get_travel_data(
            engine="google_flights",
            params={"origin": origin, "destination": destination, "people": people},
        )
        flight_candidates = self.normalizer.normalize_flights(flight_env)

        transit_env = await self.cache_manager.get_travel_data(
            engine="trains",
            params={"origin": origin, "destination": destination},
        )
        transit_candidates = self.normalizer.normalize_transit(transit_env)

        all_transports: list[FlightOption | TransitOption] = []
        all_transports.extend(flight_candidates)
        all_transports.extend(transit_candidates)

        if not all_transports:
            # Deterministic fallback travel options
            all_transports = [
                FlightOption(
                    airline="IndiGo",
                    flight_number="6E-101",
                    price=Decimal("4000.00") * Decimal(people),
                    source=DataSource.FALLBACK,
                    is_fallback=True,
                ),
                TransitOption(
                    transit_type="train",
                    origin=origin,
                    destination=destination,
                    name_or_operator="Superfast Express (3A)",
                    price=Decimal("1500.00") * Decimal(people),
                    source=DataSource.FALLBACK,
                    is_fallback=True,
                ),
            ]

        primary_transport = all_transports[0]

        # b. Hotels
        hotel_env = await self.cache_manager.get_travel_data(
            engine="google_hotels",
            params={"destination": destination, "days": days, "people": people},
        )
        hotel_candidates = self.normalizer.normalize_hotels(hotel_env)

        if not hotel_candidates:
            # Deterministic fallback tiers for optimization support
            hotel_candidates = [
                HotelOption(
                    name=f"{destination} Heritage Palace",
                    hotel_class=4,
                    price_per_night=Decimal("3000.00"),
                    total_price=Decimal("3000.00") * Decimal(days),
                    source=DataSource.FALLBACK,
                    is_fallback=True,
                ),
                HotelOption(
                    name=f"{destination} Comfort Inn",
                    hotel_class=3,
                    price_per_night=Decimal("1800.00"),
                    total_price=Decimal("1800.00") * Decimal(days),
                    source=DataSource.FALLBACK,
                    is_fallback=True,
                ),
                HotelOption(
                    name=f"{destination} Backpacker Lodge",
                    hotel_class=2,
                    price_per_night=Decimal("800.00"),
                    total_price=Decimal("800.00") * Decimal(days),
                    source=DataSource.FALLBACK,
                    is_fallback=True,
                ),
            ]

        primary_hotel = hotel_candidates[0]

        # c. Places
        places_env = await self.cache_manager.get_travel_data(
            engine="google_maps",
            params={"q": f"places attractions in {destination}", "location": destination},
        )
        places = self.normalizer.normalize_places(places_env)
        if not places:
            places = [
                PlaceOption(
                    name=f"{destination} Central Landmark",
                    category="attraction",
                    source=DataSource.FALLBACK,
                    is_fallback=True,
                )
            ]

        # d. Routes
        routes_env = await self.cache_manager.get_travel_data(
            engine="google_maps_directions",
            params={"origin": origin, "destination": destination},
        )
        routes = self.normalizer.normalize_routes(routes_env)
        primary_route = routes[0] if routes else None

        return (
            primary_transport,
            all_transports,
            primary_hotel,
            hotel_candidates,
            places,
            primary_route,
        )
