"""Budlance Orchestrator coordinating AI intent, travel data, budget engine, optimization, and persistence.

Architectural Boundaries:
- Coordinates existing services; contains NO low-level formulas, raw math, or SerpApi logic.
- Enforces strict reverse-budget feasibility gate before itinerary and ledger creation.
- Dispatches in-trip rescue messages to RescueService.
- Provides clean error boundaries protecting against exposed secrets.

Action-First Routing Architecture:
  ONE AI call per message returns both `action` and extracted fields.
  ActionRouter decides which state source to use and which merge rule to apply.
  The LLM is NOT responsible for state decisions.

State Sources:
  pending_intent   — stored in Supabase search_cache via ConversationStateRepository.
                     Used for: NEW_TRIP, CHANGE_*, FIND_ALTERNATIVE, UNRECOGNIZED.
                     Preserved after NOT_FEASIBLE so the user can ask for alternatives.
  active confirmed trip — stored in trip/ledger tables.
                     Used for: RESCUE only.
"""

import asyncio
from decimal import Decimal
import logging
from typing import Any
from uuid import UUID, uuid4

from budlance.db.models import utc_now
from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.ai.service import AIIntentService
from budlance.cache.manager import CacheFallbackManager
from budlance.db.repositories.conversation_repo import ConversationStateRepository
from budlance.db.repositories.intent_repo import IntentRepository
from budlance.db.repositories.itinerary_repo import ItineraryRepository
from budlance.db.repositories.ledger_repo import LedgerRepository
from budlance.db.repositories.rescue_repo import RescueRepository
from budlance.db.repositories.trip_repo import TripRepository
from budlance.db.repositories.user_repo import UserRepository
from budlance.attractions.selector import AttractionSelector
from budlance.engine.budget import ReverseBudgetEngine
from budlance.engine.models import BudgetEvaluationResult, OptimizationResult
from budlance.engine.optimizer import OptimizationEngine
from budlance.estimation.estimator import EstimationLayer
from budlance.itinerary.enhancer import ItineraryEnhancer
from budlance.itinerary.generator import ItineraryGenerator
from budlance.ledger.manager import VirtualLedgerManager
from budlance.lifecycle.completion_handler import (
    TripCompletionHandler,
    extract_reconciliation_amount,
    is_new_trip_message,
    is_skip_response,
)
from budlance.lifecycle.expense_handler import ExpenseLifecycleHandler
from budlance.normalization.flights import build_safe_flight_search_url, extract_best_booking_option
from budlance.normalization.normalizer import DataNormalizer
from budlance.normalization.utils import parse_price_and_currency
from budlance.config import get_settings
from budlance.db.repositories.trip_pass_repo import TripPassRepository
from budlance.normalization.transit import build_round_trip_transit_options, calculate_round_trip_cost
from budlance.orchestrator.formatter import (
    format_clarification,
    format_feasible_plan,
    format_feasible_transport,
    format_free_summary,
    format_infeasible_plan,
    format_infeasible_transport,
    format_rescue_result,
)
from budlance.orchestrator.models import OrchestrationResult
from budlance.payment.service import PaymentService
from budlance.rescue.service import RescueService
from budlance.schemas.travel import FlightOption, HotelOption, PlaceOption, RouteOption, TransitOption
from budlance.serpapi.models import DataSource
from budlance.serpapi.location import resolve_iata, resolve_hotel_query, resolve_places_query
from budlance.cache.fallback import FallbackDataProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Curated domestic candidate pool (candidate SOURCE only — not pre-approved).
# These are passed through the same Gate 1 → Gate 2 → lodging → budget
# pipeline as Travel Explore candidates. Having a corridor does NOT make a
# destination feasible — it only saves a live flight call at Gate 1.
# ---------------------------------------------------------------------------
_CURATED_DOMESTIC_POOL: list[dict[str, Any]] = [
    # name must match corridor origin/destination casing in train/bus JSON files
    {"destination": "Goa",       "origin_corridor": "Chennai",   "tags": ["beach", "food", "nightlife"]},
    {"destination": "Ooty",      "origin_corridor": "Chennai",   "tags": ["nature", "hill station", "scenic"]},
    {"destination": "Coorg",     "origin_corridor": "Bangalore", "tags": ["nature", "coffee", "trekking"]},
    {"destination": "Manali",    "origin_corridor": "Delhi",     "tags": ["adventure", "snow", "trekking"]},
    {"destination": "Udaipur",   "origin_corridor": "Delhi",     "tags": ["heritage", "lake", "culture"]},
    {"destination": "Kerala",    "origin_corridor": "Chennai",   "tags": ["backwaters", "nature", "food"]},
]

# Sentinel used as sort key for missing/unknown prices — treated as "expensive",
# never as free. Must be larger than any realistic per-person budget.
_MISSING_PRICE_SENTINEL: Decimal = Decimal("999_999_999")


def _has_offline_corridor(origin: str, destination: str) -> bool:
    """Return True if a static train or bus corridor exists for origin→destination.

    Zero-cost, offline, no invented routes. Uses the same FallbackDataProvider
    that Gate 2 uses — so a positive check here guarantees Gate 2 can find transport.
    """
    provider = FallbackDataProvider()
    return bool(
        provider.get_train_corridor(origin, destination)
        or provider.get_bus_corridor(origin, destination)
    )


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
        conversation_repo: ConversationStateRepository | None = None,
        attraction_selector: AttractionSelector | None = None,
        itinerary_enhancer: ItineraryEnhancer | None = None,
        expense_handler: ExpenseLifecycleHandler | None = None,
        completion_handler: TripCompletionHandler | None = None,
        trip_pass_repo: TripPassRepository | None = None,
        payment_service: PaymentService | None = None,
        enable_trip_pass: bool | None = None,
    ) -> None:
        self.user_repo = user_repo or UserRepository()
        self.trip_repo = trip_repo or TripRepository()
        self.intent_repo = intent_repo or IntentRepository()
        self.itinerary_repo = itinerary_repo or ItineraryRepository()
        self.ledger_repo = ledger_repo or LedgerRepository()
        self.rescue_repo = rescue_repo or RescueRepository()
        self.conversation_repo = conversation_repo or ConversationStateRepository()
        self.trip_pass_repo = trip_pass_repo or TripPassRepository()
        self.payment_service = payment_service or PaymentService(trip_pass_repo=self.trip_pass_repo)
        self.enable_trip_pass = enable_trip_pass if enable_trip_pass is not None else get_settings().enable_trip_pass

        self.ai_service = ai_service or AIIntentService()
        self.cache_manager = cache_manager or CacheFallbackManager()
        self.normalizer = normalizer or DataNormalizer()
        self.estimation = estimation_layer or EstimationLayer()
        self.budget_engine = budget_engine or ReverseBudgetEngine()
        self.optimizer = optimizer or OptimizationEngine(
            budget_engine=self.budget_engine,
            estimation_layer=self.estimation,
        )
        self.attraction_selector = attraction_selector or AttractionSelector(cache_manager=self.cache_manager)
        self.itinerary_generator = itinerary_generator or ItineraryGenerator(
            self.itinerary_repo,
            attraction_selector=self.attraction_selector,
        )
        self.itinerary_enhancer = itinerary_enhancer or ItineraryEnhancer()
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
        self.expense_handler = expense_handler or ExpenseLifecycleHandler(
            trip_repo=self.trip_repo,
            ledger_repo=self.ledger_repo,
            itinerary_repo=self.itinerary_repo,
            ledger_manager=self.ledger_manager,
        )
        self.completion_handler = completion_handler or TripCompletionHandler(
            trip_repo=self.trip_repo,
            ledger_repo=self.ledger_repo,
            conversation_repo=self.conversation_repo,
            ledger_manager=self.ledger_manager,
        )

    # =========================================================================
    # Primary entry point
    # =========================================================================

    async def handle_user_message(
        self,
        telegram_user_id: int,
        chat_id: int,
        message: str,
        username: str | None = None,
        first_name: str | None = None,
    ) -> OrchestrationResult:
        """Process an incoming Telegram message through the action-first routing pipeline.

        Flow:
        1. ONE AI call → ParsedTripIntent (action + fields).
        2. ActionRouter selects state source and applies merge rule.
        3. Existing Budlance engines (budget, optimizer, itinerary, ledger, rescue).

        Key design invariant:
        - NOT_FEASIBLE does NOT clear the pending conversation state.
          The user can follow up with FIND_ALTERNATIVE without repeating all fields.
        - RESCUE loads the active confirmed trip — it never touches the pending draft.
        - UNRECOGNIZED changes nothing.
        """
        clean_text = message.strip()
        if not clean_text:
            return OrchestrationResult(
                status="CLARIFICATION",
                message_text="👋 Please send your trip request with budget, number of people, duration, and origin!",
            )

        clean_lower = clean_text.lower()
        if clean_lower in ("/demo_pass", "demo pass", "/bypass") or clean_lower.startswith(("/demo_pass ", "demo pass ")):
            target_id = clean_text.split()[-1] if len(clean_text.split()) > 1 else None
            return await self._handle_demo_pass_command(chat_id, target_trip_id=target_id)

        if clean_lower in ("/trip_pass", "/pass", "trip pass", "pass", "unlock", "unlock full trip plan", "unlock trip", "buy pass") or clean_lower.startswith(("/trip_pass ", "/pass ")):
            return await self._handle_pass_status_command(chat_id)

        if clean_lower in ("paid", "i paid", "i have paid", "payment completed", "verify payment"):
            return await self._handle_verify_payment_command(chat_id)

        try:
            pending_intent = self.conversation_repo.get_pending_intent(chat_id)

            # Check for pending reconciliation state (LOG_ACTUAL_SPEND)
            if pending_intent is not None and pending_intent.pending_action == "LOG_ACTUAL_SPEND":
                if is_skip_response(clean_text):
                    comp_res = await self.completion_handler.handle_skip_reconciliation(chat_id)
                    return OrchestrationResult(
                        trip_id=comp_res.trip_id,
                        status=comp_res.status,
                        message_text=comp_res.message_text,
                    )

                fresh_ai = await self.ai_service.parse_trip_intent(clean_text)
                if fresh_ai.action == TripAction.RESCUE:
                    return await self._handle_rescue(chat_id, clean_text)
                if fresh_ai.action == TripAction.LOG_EXPENSE:
                    return await self._handle_log_expense(chat_id, fresh_ai)
                if fresh_ai.action == TripAction.TRIP_COMPLETE:
                    return await self._handle_trip_complete(
                        chat_id=chat_id,
                        completion_reason=fresh_ai.completion_reason,
                    )
                if fresh_ai.action in (
                    TripAction.CHANGE_BUDGET,
                    TripAction.CHANGE_DAYS,
                    TripAction.CHANGE_PEOPLE,
                    TripAction.CHANGE_DESTINATION,
                    TripAction.CHANGE_TRANSPORT,
                ):
                    pass  # Fall through to change handling
                else:
                    rec_amount = extract_reconciliation_amount(clean_text)
                    if (
                        rec_amount is not None
                        and not is_new_trip_message(clean_text)
                        and fresh_ai.destination is None
                        and fresh_ai.origin is None
                    ):
                        comp_res = await self.completion_handler.handle_reconcile_amount(chat_id, rec_amount)
                        return OrchestrationResult(
                            trip_id=comp_res.trip_id,
                            status=comp_res.status,
                            message_text=comp_res.message_text,
                        )

                    is_substantive_new_trip = (
                        is_new_trip_message(clean_text)
                        or (
                            fresh_ai.action == TripAction.NEW_TRIP
                            and (
                                fresh_ai.destination is not None
                                or fresh_ai.budget is not None
                                or fresh_ai.origin is not None
                            )
                        )
                    )
                    if is_substantive_new_trip:
                        # Finalize old active trip with skip semantics and mark NEW_TRIP_STARTED
                        await self.completion_handler.handle_skip_reconciliation(
                            chat_id,
                            completion_reason="NEW_TRIP_STARTED",
                        )
                        self.conversation_repo.clear_pending_intent(chat_id)
                        pending_intent = None
                        ai_result = fresh_ai
                        action = ai_result.action
                    else:
                        # Unrecognized message during reconciliation
                        return OrchestrationResult(
                            status="PENDING_RECONCILIATION",
                            message_text=(
                                "Your trip completion is pending. "
                                "Would you like to record your final actual spend (e.g. '₹18,450'), or would you like to 'skip'?"
                            ),
                        )
            else:
                # ---- STEP 1: ONE AI call returns action + fields ----
                if pending_intent is not None:
                    # Context-aware parse: action classification + field extraction with existing context
                    ai_result = await self.ai_service.parse_trip_intent_with_context(
                        user_prompt=clean_text,
                        existing_intent=pending_intent,
                    )
                else:
                    # Fresh message: action classification + full field extraction
                    ai_result = await self.ai_service.parse_trip_intent(clean_text)

                action = ai_result.action

            logger.info(
                "[ACTION_ROUTER] chat_id=%s action=%s pending=%s",
                chat_id, action, pending_intent is not None,
            )

            # ---- STEP 2: Action Router — choose state source and apply merge rule ----
            previous_destination: str | None = None

            # RESCUE: Never touches pending draft. Uses active confirmed trip.
            if action == TripAction.RESCUE:
                return await self._handle_rescue(chat_id, clean_text)

            # LOG_EXPENSE: Never touches pending draft. Uses active confirmed trip.
            if action == TripAction.LOG_EXPENSE:
                return await self._handle_log_expense(chat_id, ai_result)

            # TRIP_COMPLETE: Load active confirmed trip and prompt for final reconciliation
            if action == TripAction.TRIP_COMPLETE:
                return await self._handle_trip_complete(
                    chat_id=chat_id,
                    completion_reason=ai_result.completion_reason,
                )

            # CONFIRM_BOOKING: Activate existing planning trip or finalize booking handoff
            if action == TripAction.CONFIRM_BOOKING:
                return await self._handle_confirm_booking(
                    chat_id=chat_id,
                    telegram_user_id=telegram_user_id,
                    username=username,
                    first_name=first_name,
                    clean_text=clean_text,
                    pending_intent=pending_intent,
                    ai_result=ai_result,
                )

            # UNRECOGNIZED: Change nothing.
            if action == TripAction.UNRECOGNIZED:
                return self._handle_unrecognized(pending_intent)

            # NEW_TRIP: Discard stale pending state. Use fields from this AI call as-is.
            if action == TripAction.NEW_TRIP:
                self.conversation_repo.clear_pending_intent(chat_id)
                resolved_intent = ai_result
                logger.info("[ACTION_ROUTER] NEW_TRIP — stale context cleared.")

            # FIND_ALTERNATIVE: Load pending state, keep all constraints, clear destination.
            elif action == TripAction.FIND_ALTERNATIVE:
                if pending_intent is not None:
                    previous_destination = pending_intent.destination
                    resolved_intent = pending_intent.model_copy(
                        update={"destination": None, "action": TripAction.FIND_ALTERNATIVE}
                    )
                    logger.info(
                        "[ACTION_ROUTER] FIND_ALTERNATIVE — keeping budget=%s people=%s days=%s origin=%s, "
                        "clearing destination (previously %s).",
                        resolved_intent.budget, resolved_intent.people,
                        resolved_intent.days, resolved_intent.origin,
                        previous_destination,
                    )
                else:
                    # No prior context — treat as new with no destination
                    resolved_intent = ParsedTripIntent(action=TripAction.FIND_ALTERNATIVE)
                    logger.info("[ACTION_ROUTER] FIND_ALTERNATIVE — no pending context, destination discovery.")

            # CHANGE_* actions: Load pending state, apply only the changed field.
            elif action in (
                TripAction.CHANGE_BUDGET,
                TripAction.CHANGE_DAYS,
                TripAction.CHANGE_PEOPLE,
                TripAction.CHANGE_DESTINATION,
                TripAction.CHANGE_TRANSPORT,
            ):
                if pending_intent is not None:
                    resolved_intent = pending_intent.apply_change_action(ai_result)
                    logger.info(
                        "[ACTION_ROUTER] %s applied. Result: budget=%s people=%s days=%s dest=%s mode=%s class=%s",
                        action, resolved_intent.budget, resolved_intent.people,
                        resolved_intent.days, resolved_intent.destination,
                        resolved_intent.transport_mode, resolved_intent.transport_class,
                    )
                else:
                    # No prior state — just use whatever the AI extracted
                    resolved_intent = ai_result
                    logger.info("[ACTION_ROUTER] %s but no pending context. Using AI result directly.", action)

            else:
                # Fallback safety net
                resolved_intent = ai_result
                logger.warning("[ACTION_ROUTER] Unexpected action=%s. Falling back to raw AI result.", action)

            # ---- STEP 3: Validate Required Planning Inputs ----
            missing_fields = []
            if resolved_intent.budget is None or resolved_intent.budget <= Decimal("0.00"):
                missing_fields.append("budget")
            if resolved_intent.people is None or resolved_intent.people <= 0:
                missing_fields.append("people")
            if resolved_intent.days is None or resolved_intent.days <= 0:
                missing_fields.append("days")
            if not resolved_intent.origin:
                missing_fields.append("origin")

            if missing_fields:
                logger.info(
                    "[CONVERSATION] Trip intent missing %s for chat_id=%s. Saving pending intent.",
                    missing_fields,
                    chat_id,
                )
                # Save the (possibly partially updated) intent so next message can merge
                self.conversation_repo.save_pending_intent(chat_id, resolved_intent)
                return OrchestrationResult(
                    status="CLARIFICATION",
                    message_text=format_clarification(missing_fields, known_context=resolved_intent),
                )

            # ---- STEP 3b: Transport Preference & Immediate Reverse-Budget Feasibility Gate ----
            if resolved_intent.destination and resolved_intent.destination.strip():
                msg_lower = (message or "").lower()
                transport_query_triggers = (
                    action in (TripAction.CHANGE_TRANSPORT, TripAction.CONFIRM_BOOKING)
                    or resolved_intent.transport_mode is not None
                    or resolved_intent.transport_class is not None
                    or resolved_intent.booking_confirmed
                    or (pending_intent is not None and (pending_intent.transport_mode is not None or pending_intent.transport_class is not None))
                    or ("sunrise" in msg_lower or "india gate" in msg_lower or "famous places" in msg_lower)
                    or any(word in msg_lower for word in ["train", "flight", "sleeper", "1ac", "2ac", "3ac", "economy", "business"])
                )

                if transport_query_triggers:
                    if not resolved_intent.transport_mode and not resolved_intent.transport_class:
                        self.conversation_repo.save_pending_intent(chat_id, resolved_intent)
                        return OrchestrationResult(
                            status="CLARIFICATION",
                            selected_destination=resolved_intent.destination,
                            message_text="How would you like to travel — train or flight?",
                        )
                    if resolved_intent.transport_mode == "train" and not resolved_intent.transport_class:
                        self.conversation_repo.save_pending_intent(chat_id, resolved_intent)
                        return OrchestrationResult(
                            status="CLARIFICATION",
                            selected_destination=resolved_intent.destination,
                            message_text="Which train class would you prefer — Sleeper, 3AC, 2AC or 1AC?",
                        )

                    # If transport preference is specified and external booking is NOT yet confirmed:
                    if not resolved_intent.booking_confirmed:
                        # Perform IMMEDIATE transport lookup via CacheFallbackManager
                        transport_options = await self.lookup_transport_options(
                            origin=resolved_intent.origin or "Origin",
                            destination=resolved_intent.destination,
                            people=resolved_intent.people or 1,
                            transport_mode=resolved_intent.transport_mode,
                            transport_class=resolved_intent.transport_class,
                        )
                        selected_transport = transport_options[0] if transport_options else None

                        # Perform IMMEDIATE Reverse-Budget feasibility check
                        budget_val = resolved_intent.budget or Decimal("0.00")
                        people_val = resolved_intent.people or 1
                        days_val = resolved_intent.days or 1

                        food_est = self.estimation.estimate_food(people=people_val, days=days_val, tier="standard")
                        transit_est = self.estimation.estimate_local_transit_daily(days=days_val, people=people_val, mode="metro_bus")
                        activities_budget = round(budget_val * Decimal("0.05"), 2)

                        from datetime import date, timedelta
                        _today = date.today()
                        _dest = resolved_intent.destination or "Destination"
                        hotel_env = await self.cache_manager.get_travel_data(
                            engine="google_hotels",
                            params={
                                "q": resolve_hotel_query(_dest),
                                "check_in_date": (_today + timedelta(days=30)).strftime("%Y-%m-%d"),
                                "check_out_date": (_today + timedelta(days=30 + days_val)).strftime("%Y-%m-%d"),
                                "adults": people_val,
                                "currency": "INR",
                                "hl": "en",
                            },
                        )
                        hotel_candidates = self.normalizer.normalize_hotels(hotel_env)
                        baseline_hotel = hotel_candidates[0] if hotel_candidates else None

                        transport_eval = self.budget_engine.evaluate(
                            total_budget=budget_val,
                            people=people_val,
                            days=days_val,
                            transport=selected_transport,
                            hotel=baseline_hotel,
                            food_estimate=food_est,
                            local_transit_estimate=transit_est,
                            activities_budget=activities_budget,
                            currency=resolved_intent.currency,
                        )

                        if transport_eval.is_feasible:
                            travel_cost = selected_transport.price if selected_transport else Decimal("0.00")
                            remaining_budget = budget_val - travel_cost
                            is_train = resolved_intent.transport_mode == "train" or resolved_intent.transport_class in ("1ac", "2ac", "3ac", "sleeper")
                            if is_train:
                                booking_link = "https://www.irctc.co.in/nget/train-search"
                            else:
                                raw_deep_link = getattr(selected_transport, "deep_link", None)
                                if raw_deep_link and str(raw_deep_link).startswith(("http://", "https://")):
                                    booking_link = str(raw_deep_link)
                                else:
                                    booking_link = build_safe_flight_search_url(
                                        origin=resolved_intent.origin,
                                        destination=resolved_intent.destination,
                                        people=people_val,
                                        travel_class=resolved_intent.transport_class,
                                    )

                            operator = (
                                getattr(selected_transport, "name_or_operator", None)
                                or getattr(selected_transport, "airline", None)
                            )
                            # Create user and initial trip record in PLANNING status
                            user = self.user_repo.get_or_create_user(
                                telegram_user_id=telegram_user_id,
                                username=username,
                                first_name=first_name,
                            )
                            planning_trip = self.trip_repo.create_trip(
                                user_id=user.id,
                                telegram_chat_id=chat_id,
                                budget_total=budget_val,
                                destination=resolved_intent.destination,
                                origin=resolved_intent.origin,
                                currency=resolved_intent.currency,
                                people_count=people_val,
                                duration_days=days_val,
                                status="PLANNING",
                                is_active=True,
                            )

                            self.conversation_repo.save_pending_intent(chat_id, resolved_intent)
                            return OrchestrationResult(
                                trip_id=planning_trip.id,
                                status="FEASIBLE_TRANSPORT",
                                selected_destination=resolved_intent.destination,
                                selected_transport=selected_transport,
                                message_text=format_feasible_transport(
                                    transport_mode=resolved_intent.transport_mode or "train",
                                    transport_class=resolved_intent.transport_class,
                                    estimated_cost=travel_cost,
                                    remaining_budget=remaining_budget,
                                    currency=resolved_intent.currency,
                                    booking_link=booking_link,
                                    operator=operator,
                                    people=people_val,
                                    origin=resolved_intent.origin,
                                    destination=resolved_intent.destination,
                                    is_exact_booking=getattr(selected_transport, "is_exact_booking", False),
                                    seller=getattr(selected_transport, "seller", None),
                                    flight_number=getattr(selected_transport, "flight_number", None),
                                    departure_time=getattr(selected_transport, "departure_time", None),
                                    arrival_time=getattr(selected_transport, "arrival_time", None),
                                ),
                            )
                        else:
                            travel_cost = selected_transport.price if selected_transport else Decimal("0.00")
                            cheaper_alts = []
                            cls_norm = (resolved_intent.transport_class or "").lower()
                            if cls_norm in ("1ac", "1a"):
                                cheaper_alts = ["2AC", "3AC"]
                            elif cls_norm in ("2ac", "2a"):
                                cheaper_alts = ["3AC", "Sleeper"]
                            elif cls_norm in ("3ac", "3a"):
                                cheaper_alts = ["Sleeper"]
                            elif resolved_intent.transport_mode == "flight":
                                cheaper_alts = ["train", "Sleeper"]

                            self.conversation_repo.save_pending_intent(chat_id, resolved_intent)
                            return OrchestrationResult(
                                status="NOT_FEASIBLE",
                                selected_destination=resolved_intent.destination,
                                selected_transport=selected_transport,
                                message_text=format_infeasible_transport(
                                    transport_mode=resolved_intent.transport_mode or "train",
                                    transport_class=resolved_intent.transport_class,
                                    people=people_val,
                                    cost=travel_cost,
                                    budget=budget_val,
                                    currency=resolved_intent.currency,
                                    cheaper_alternatives=cheaper_alts,
                                ),
                            )

            # ---- STEP 4: User Resolution ----
            user = self.user_repo.get_or_create_user(
                telegram_user_id=telegram_user_id,
                username=username,
                first_name=first_name,
            )

            # ---- STEP 5: Destination Evaluation / Discovery ----
            origin = resolved_intent.origin or "Origin"
            budget = resolved_intent.budget or Decimal("0.00")
            people = resolved_intent.people or 1
            days = resolved_intent.days or 1

            used_fallback_catalog = False
            if resolved_intent.destination and resolved_intent.destination.strip():
                candidate_destinations = [resolved_intent.destination.strip().title()]
            else:
                logger.info("Destination absent. Engaging Destination Discovery via Travel Explore...")
                candidate_destinations, used_fallback_catalog = await self._discover_destinations(
                    origin=origin,
                    budget=budget,
                    interests=resolved_intent.interests,
                    excluded_destinations=[previous_destination] if previous_destination else None,
                    people=people,
                    days=days,
                )

            if not candidate_destinations:
                self.conversation_repo.save_pending_intent(chat_id, resolved_intent)
                return OrchestrationResult(
                    chat_id=chat_id,
                    status="CLARIFICATION",
                    message_text=(
                        f"I couldn't find available destinations matching your budget from {origin}. "
                        "Where would you like to travel, or would you like to adjust your budget?"
                    ),
                    deficit=Decimal("0.00"),
                )

            # ---- STEP 6: Evaluate candidates (capped + early exit + timeout) ----
            selected_plan: dict[str, Any] | None = None
            last_infeasible_result: BudgetEvaluationResult | None = None
            last_opt_result: OptimizationResult | None = None

            settings = get_settings()
            max_candidates = settings.max_live_candidates_per_request
            max_provider_calls = settings.max_live_provider_calls_per_request
            eval_timeout = settings.destination_evaluation_timeout_seconds
            provider_calls_used: int = 0
            candidates_checked: int = 0
            early_exit_reason: str | None = None

            is_discovery = not bool(resolved_intent.destination and resolved_intent.destination.strip())
            for dest in candidate_destinations:
                # Hard candidate cap — stop before evaluating more than max_candidates
                if is_discovery and candidates_checked >= max_candidates:
                    early_exit_reason = f"MAX_CANDIDATES_REACHED({max_candidates})"
                    logger.info(
                        "[GATE] Candidate evaluation capped at %d candidates. Stopping.",
                        max_candidates,
                    )
                    break

                # Hard provider-call cap — stop if we've already spent our budget
                if is_discovery and provider_calls_used >= max_provider_calls:
                    early_exit_reason = f"MAX_PROVIDER_CALLS_REACHED({max_provider_calls})"
                    logger.info(
                        "[GATE] Provider call budget exhausted (%d calls). Stopping evaluation.",
                        max_provider_calls,
                    )
                    break

                candidates_checked += 1
                logger.info(
                    "Evaluating candidate destination: %s (discovery=%s candidates=%d/%d calls=%d/%d)",
                    dest, is_discovery, candidates_checked, max_candidates,
                    provider_calls_used, max_provider_calls,
                )

                # Has this candidate a known offline corridor? If so it won't need a flight call.
                has_corridor = _has_offline_corridor(origin, dest)

                try:
                    plan = await asyncio.wait_for(
                        self._evaluate_trip_candidate(
                            origin=origin,
                            destination=dest,
                            people=people,
                            days=days,
                            budget=budget,
                            currency=resolved_intent.currency,
                            travel_party=resolved_intent.travel_party,
                            interests=resolved_intent.interests,
                            transport_mode=resolved_intent.transport_mode,
                            transport_class=resolved_intent.transport_class,
                            is_discovery_candidate=is_discovery,
                            has_offline_corridor=has_corridor,
                            provider_calls_counter=provider_calls_used,
                            provider_calls_limit=max_provider_calls,
                        ),
                        timeout=eval_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "[GATE] Candidate %s evaluation timed out after %.1fs. Skipping.",
                        dest, eval_timeout,
                    )
                    plan = {
                        "is_feasible": False,
                        "destination": dest,
                        "days": days,
                        "baseline_eval": None,
                        "opt_result": None,
                        "rejection_reason": "TIMEOUT",
                        "provider_calls_used": 0,
                    }

                # Update running provider-call count from plan
                provider_calls_used += plan.get("provider_calls_used", 0)

                if plan["is_feasible"]:
                    early_exit_reason = "FIRST_FEASIBLE"
                    logger.info(
                        "[GATE] First feasible candidate found: %s. Early exit. "
                        "(candidates=%d provider_calls=%d)",
                        dest, candidates_checked, provider_calls_used,
                    )
                    selected_plan = plan
                    break
                else:
                    last_infeasible_result = plan.get("baseline_eval")
                    last_opt_result = plan.get("opt_result")

            logger.info(
                "[GATE] Evaluation complete: candidates_checked=%d provider_calls=%d early_exit=%s feasible=%s",
                candidates_checked, provider_calls_used, early_exit_reason or "EXHAUSTED",
                selected_plan is not None,
            )

            # ---- STEP 7: NOT_FEASIBLE — preserve pending state for follow-up ----
            if not selected_plan or not selected_plan["is_feasible"]:
                if action == TripAction.FIND_ALTERNATIVE:
                    # User requested an alternative destination; do not force candidate[0] as requested destination
                    target_dest = None
                else:
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

                # CRITICAL: Save pending state so the user can ask for alternatives
                # without repeating budget, people, days, and origin.
                self.conversation_repo.save_pending_intent(chat_id, resolved_intent)
                logger.info(
                    "[CONVERSATION] NOT_FEASIBLE for %s — pending intent preserved for chat_id=%s "
                    "(budget=%s, people=%s, days=%s, origin=%s).",
                    target_dest or "alternatives", chat_id, budget, people, days, origin,
                )

                is_discovery = not resolved_intent.destination or not resolved_intent.destination.strip()
                if is_discovery:
                    return OrchestrationResult(
                        status="NOT_FEASIBLE",
                        selected_destination=None,
                        feasibility_status="NOT_FEASIBLE",
                        message_text="No feasible destination found within your budget for the requested trip.",
                        deficit=deficit,
                    )

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
                        currency=resolved_intent.currency,
                    ),
                )

            # ---- STEP 8: FEASIBLE — persist trip, itinerary, ledger ----
            chosen_dest = selected_plan["destination"]
            final_days = selected_plan["days"]
            final_eval: BudgetEvaluationResult = selected_plan["evaluation"]
            transport = selected_plan["transport"]
            hotel = selected_plan["hotel"]
            route = selected_plan["route"]
            opt_result: OptimizationResult | None = selected_plan.get("opt_result")

            # Attraction data request through CacheFallbackManager (ONLY AFTER feasible destination selected)
            places_env = await self.cache_manager.get_travel_data(
                engine="google_maps",
                params={
                    "q": resolve_places_query(chosen_dest),
                    "location": chosen_dest,
                    "m": get_settings().maps_search_radius_meters,
                    "hl": "en",
                    "type": "search",
                },
            )
            places = self.normalizer.normalize_places(places_env) or []

            # If user specified interests (e.g. theme park, local food), discover matching places
            if resolved_intent.interests:
                for interest_item in resolved_intent.interests:
                    int_env = await self.cache_manager.get_travel_data(
                        engine="google_maps",
                        params={
                            "q": resolve_places_query(chosen_dest, interest=interest_item),
                            "location": chosen_dest,
                            "m": get_settings().maps_search_radius_meters,
                            "hl": "en",
                            "type": "search",
                        },
                    )
                    int_places = self.normalizer.normalize_places(int_env) or []
                    if int_places:
                        existing_names = {p.name.lower() for p in places}
                        for ip in int_places:
                            if ip.name.lower() not in existing_names:
                                places.append(ip)
                                existing_names.add(ip.name.lower())

            # AttractionSelector for feasible destination
            attractions = self.attraction_selector.select_for_itinerary(
                destination=chosen_dest,
                travel_party=resolved_intent.travel_party,
                interests=resolved_intent.interests,
                days=final_days,
            ) or (selected_plan.get("attractions") or [])

            # a. Create Trip (starts in PLANNING status; activation happens on CONFIRM_BOOKING)
            trip = self.trip_repo.create_trip(
                user_id=user.id,
                telegram_chat_id=chat_id,
                budget_total=budget,
                destination=chosen_dest,
                origin=origin,
                currency=resolved_intent.currency,
                people_count=people,
                duration_days=final_days,
                status="PLANNING",
                is_active=True,
            )
            if resolved_intent.booking_confirmed:
                self.trip_repo.update_trip_status(trip.id, status="ACTIVE", is_active=True)
                trip.status = "ACTIVE"

            # b. Persist Trip Intent
            resolved_intent.destination = chosen_dest
            resolved_intent.days = final_days
            intent_record = resolved_intent.to_trip_intent_record(
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
                travel_party=resolved_intent.travel_party,
                interests=resolved_intent.interests,
                attractions=attractions,
            )

            # Batched LLM description enhancement (1 call for entire itinerary)
            if generated_itin is not None:
                generated_itin = await self.itinerary_enhancer.enhance_itinerary(
                    itinerary=generated_itin,
                    travel_party=resolved_intent.travel_party,
                )

                # Persist enhanced descriptions to itinerary repo
                existing_record = self.itinerary_repo.get_itinerary(trip.id)
                if existing_record and getattr(generated_itin, "days", None):
                    from budlance.db.models import utc_now
                    existing_record.days = [day.model_dump(mode="json") for day in generated_itin.days]
                    existing_record.updated_at = utc_now()
                    self.itinerary_repo.save_itinerary(existing_record)

            # d. Initialize Virtual Ledger
            ledger_summary = self.ledger_manager.initialize_ledger(
                trip_id=trip.id,
                evaluation=final_eval,
            )

            # e. Format message and construct result (gated by Trip Pass if enabled)
            downgrades = list(opt_result.downgrades_applied) if opt_result else []
            if used_fallback_catalog and resolved_intent.interests:
                downgrades.append(
                    f"Requested interest ({', '.join(resolved_intent.interests)}) could not be matched in offline catalog; "
                    f"selected {chosen_dest} from regional budget corridors."
                )

            # Clear pending intent now that planning completed successfully
            self.conversation_repo.clear_pending_intent(chat_id)

            return await self._build_plan_result(
                chat_id=chat_id,
                user_id=user.id,
                trip_id=trip.id,
                chosen_dest=chosen_dest,
                final_days=final_days,
                people=people,
                final_eval=final_eval,
                transport=transport,
                hotel=hotel,
                places=places,
                route=route,
                generated_itin=generated_itin,
                ledger_summary=ledger_summary,
                opt_result=opt_result,
                downgrades=downgrades,
                travel_party=resolved_intent.travel_party,
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

    # =========================================================================
    # Action-specific helpers
    # =========================================================================

    async def _handle_rescue(self, chat_id: int, message: str) -> OrchestrationResult:
        """Route to RescueService using the ACTIVE CONFIRMED TRIP — never the pending draft."""
        logger.info(
            "[ACTION_ROUTER] RESCUE detected for chat_id=%s. Loading active trip (NOT pending draft).",
            chat_id,
        )
        if self.enable_trip_pass:
            active_trip = self.trip_repo.get_active_trip(chat_id)
            if active_trip:
                pass_rec = self.trip_pass_repo.get_by_trip_id(active_trip.id)
                if not pass_rec or pass_rec.status != "PAID":
                    return OrchestrationResult(
                        trip_id=active_trip.id,
                        status="PASS_LOCKED",
                        message_text=(
                            "🔒 *In-Trip Rescue is Locked*\n\n"
                            "Live attraction replanning and budget rescue require an active Budlance Trip Pass.\n"
                            "Send `/trip_pass` to unlock for ₹49, or use `/demo_pass` for demo evaluation."
                        ),
                        is_pass_unlocked=False,
                        pass_status=pass_rec.status if pass_rec else "FREE",
                    )

        # Re-parse as a rescue intent using the dedicated rescue classifier
        rescue_res = await self.rescue_service.execute_rescue(
            chat_id=chat_id,
            user_message=message,
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

    async def _handle_log_expense(self, chat_id: int, parsed_intent: ParsedTripIntent) -> OrchestrationResult:
        """Route to ExpenseLifecycleHandler using the ACTIVE CONFIRMED TRIP — never the pending draft."""
        logger.info(
            "[ACTION_ROUTER] LOG_EXPENSE detected for chat_id=%s. Loading active trip (NOT pending draft).",
            chat_id,
        )
        expense_res = await self.expense_handler.handle_log_expense(
            chat_id=chat_id,
            parsed=parsed_intent,
        )
        return OrchestrationResult(
            trip_id=expense_res.trip_id,
            status=expense_res.status,
            ledger_summary=expense_res.ledger_summary,
            message_text=expense_res.message_text,
            error=expense_res.error,
        )

    async def _handle_trip_complete(
        self,
        chat_id: int,
        completion_reason: str | None = None,
    ) -> OrchestrationResult:
        """Route to TripCompletionHandler to prompt for final reconciliation."""
        logger.info("[ACTION_ROUTER] TRIP_COMPLETE detected for chat_id=%s reason=%s.", chat_id, completion_reason)
        comp_res = await self.completion_handler.handle_trip_complete(
            chat_id=chat_id,
            completion_reason=completion_reason,
        )
        return OrchestrationResult(
            trip_id=comp_res.trip_id,
            status=comp_res.status,
            ledger_summary=comp_res.ledger_summary,
            message_text=comp_res.message_text,
            error=comp_res.error,
        )

    async def _handle_confirm_booking(
        self,
        chat_id: int,
        telegram_user_id: int,
        username: str | None,
        first_name: str | None,
        clean_text: str,
        pending_intent: ParsedTripIntent | None,
        ai_result: ParsedTripIntent,
    ) -> OrchestrationResult:
        """Handle CONFIRM_BOOKING action.

        Transitions trip state from PLANNING to ACTIVE, ensuring persistent
        synchronization so active-trip lookups, LOG_EXPENSE, and RESCUE succeed.
        """
        logger.info("[ACTION_ROUTER] CONFIRM_BOOKING detected for chat_id=%s.", chat_id)

        # 1. Check if chat already has an ACTIVE trip (Step 11 — Repeated "Booked" safety)
        active_trip = self.trip_repo.get_active_trip(chat_id)
        if active_trip and str(active_trip.status).upper() == "ACTIVE":
            logger.info(
                "[CONFIRM_BOOKING] Chat %s already has active trip %s (dest=%s). Idempotent return.",
                chat_id, active_trip.id, active_trip.destination,
            )
            self.conversation_repo.clear_pending_intent(chat_id)
            dest_suffix = f" to {active_trip.destination}" if active_trip.destination else ""
            return OrchestrationResult(
                trip_id=active_trip.id,
                status="ACTIVE",
                selected_destination=active_trip.destination,
                message_text=(
                    f"🎉 Your trip{dest_suffix} is already active!\n\n"
                    "You can log expenses (e.g. `Spent ₹500 on dinner`) or message `Rescue` anytime if your plans change."
                ),
            )

        # 2. Check if an existing trip in PLANNING status exists in repository
        planning_trip = self.trip_repo.get_planning_trip(chat_id)

        # Check if we have pending intent context
        resolved_intent = pending_intent or ai_result
        resolved_intent = resolved_intent.model_copy(update={"booking_confirmed": True})

        if planning_trip is None and (not resolved_intent.destination or not resolved_intent.budget):
            # Step 12 — Handle "Booked" With No Valid Planning Trip
            logger.info("[CONFIRM_BOOKING] No planning trip found to activate for chat_id=%s.", chat_id)
            return OrchestrationResult(
                status="NO_PLANNING_TRIP",
                message_text=(
                    "I couldn't find a planned trip to activate. "
                    "Please start by telling me your trip preferences "
                    "(e.g., `Plan a trip from Chennai to Delhi for 2 people, 3 days, budget ₹25,000`)."
                ),
                error="NO_PLANNING_TRIP",
            )

        # If a planning trip exists in DB:
        if planning_trip is not None:
            trip_id = planning_trip.id
            self.trip_repo.update_trip_status(trip_id=trip_id, status="ACTIVE", is_active=True)
            planning_trip.status = "ACTIVE"
            planning_trip.is_active = True
            logger.info(
                "[CONFIRM_BOOKING] Successfully transitioned trip %s: PLANNING -> ACTIVE (is_active=True).",
                trip_id,
            )

            # Check if this trip already has an itinerary and ledger generated
            itin = self.itinerary_repo.get_itinerary(trip_id)
            ledger_entries = self.ledger_repo.get_ledger_entries(trip_id)

            if (itin and ledger_entries) or pending_intent is None:
                self.conversation_repo.clear_pending_intent(chat_id)
                dest_suffix = f" to {planning_trip.destination}" if planning_trip.destination else ""
                return OrchestrationResult(
                    trip_id=trip_id,
                    status="ACTIVE",
                    selected_destination=planning_trip.destination,
                    message_text=(
                        f"🎉 Great, your trip{dest_suffix} is now active!\n\n"
                        "You can now log expenses during your trip (e.g., `Spent ₹1800 on food today`) "
                        "or message `Rescue` if you encounter unexpected disruptions."
                    ),
                )

        # Otherwise, candidate planning needs to be finalized (itinerary, ledger, etc.)
        user = self.user_repo.get_or_create_user(
            telegram_user_id=telegram_user_id,
            username=username,
            first_name=first_name,
        )

        origin = resolved_intent.origin or (planning_trip.origin if planning_trip else None) or "Origin"
        budget = resolved_intent.budget or (planning_trip.budget_total if planning_trip else None) or Decimal("0.00")
        people = resolved_intent.people or (planning_trip.people_count if planning_trip else None) or 1
        days = resolved_intent.days or (planning_trip.duration_days if planning_trip else None) or 1
        chosen_dest = (
            resolved_intent.destination
            or (planning_trip.destination if planning_trip else None)
            or "Destination"
        ).strip().title()
        currency = resolved_intent.currency or (planning_trip.currency if planning_trip else None) or "INR"

        resolved_intent.origin = origin
        resolved_intent.budget = budget
        resolved_intent.people = people
        resolved_intent.days = days
        resolved_intent.destination = chosen_dest
        resolved_intent.currency = currency

        plan = await self._evaluate_trip_candidate(
            origin=origin,
            destination=chosen_dest,
            people=people,
            days=days,
            budget=budget,
            currency=currency,
            travel_party=resolved_intent.travel_party,
            interests=resolved_intent.interests,
            transport_mode=resolved_intent.transport_mode,
            transport_class=resolved_intent.transport_class,
        )

        final_days = plan.get("days", days)
        final_eval: BudgetEvaluationResult = plan.get("evaluation") or plan.get("baseline_eval")
        if final_eval is None:
            final_eval = self.budget_engine.evaluate(
                total_budget=budget,
                people=people,
                days=final_days,
                transport=plan.get("transport"),
                hotel=plan.get("hotel"),
                food_estimate=self.estimation.estimate_food(people=people, days=final_days, tier="standard"),
                local_transit_estimate=self.estimation.estimate_local_transit_daily(days=final_days, people=people, mode="metro_bus"),
                currency=currency,
            )
        transport = plan.get("transport")
        hotel = plan.get("hotel")
        route = plan.get("route")
        opt_result: OptimizationResult | None = plan.get("opt_result")

        places_env = await self.cache_manager.get_travel_data(
            engine="google_maps",
            params={
                "q": resolve_places_query(chosen_dest),
                "location": chosen_dest,
                "m": get_settings().maps_search_radius_meters,
                "hl": "en",
                "type": "search",
            },
        )
        places = self.normalizer.normalize_places(places_env) or []

        attractions = self.attraction_selector.select_for_itinerary(
            destination=chosen_dest,
            travel_party=resolved_intent.travel_party,
            interests=resolved_intent.interests,
            days=final_days,
        ) or (plan.get("attractions") or [])

        if planning_trip is not None:
            trip = planning_trip
            self.trip_repo.update_trip_status(trip.id, status="ACTIVE", is_active=True)
            trip.status = "ACTIVE"
            trip.is_active = True
        else:
            trip = self.trip_repo.create_trip(
                user_id=user.id,
                telegram_chat_id=chat_id,
                budget_total=budget,
                destination=chosen_dest,
                origin=origin,
                currency=currency,
                people_count=people,
                duration_days=final_days,
                status="PLANNING",
                is_active=True,
            )
            self.trip_repo.update_trip_status(trip.id, status="ACTIVE", is_active=True)
            trip.status = "ACTIVE"
            trip.is_active = True

        # Persist Trip Intent
        resolved_intent.destination = chosen_dest
        resolved_intent.days = final_days
        intent_record = resolved_intent.to_trip_intent_record(
            trip_id=trip.id,
            raw_prompt=clean_text,
        )
        self.intent_repo.save_trip_intent(intent_record)

        # Generate and Persist Itinerary
        generated_itin = self.itinerary_generator.generate(
            trip_id=trip.id,
            destination=chosen_dest,
            evaluation=final_eval,
            days=final_days,
            transport=transport,
            hotel=hotel,
            places=places,
            route=route,
            travel_party=resolved_intent.travel_party,
            interests=resolved_intent.interests,
            attractions=attractions,
        )

        if generated_itin is not None:
            generated_itin = await self.itinerary_enhancer.enhance_itinerary(
                itinerary=generated_itin,
                travel_party=resolved_intent.travel_party,
            )
            existing_record = self.itinerary_repo.get_itinerary(trip.id)
            if existing_record and getattr(generated_itin, "days", None):
                existing_record.days = [day.model_dump(mode="json") for day in generated_itin.days]
                existing_record.updated_at = utc_now()
                self.itinerary_repo.save_itinerary(existing_record)

        # Initialize Virtual Ledger
        ledger_summary = self.ledger_manager.initialize_ledger(
            trip_id=trip.id,
            evaluation=final_eval,
        )

        downgrades = list(opt_result.downgrades_applied) if opt_result else []
        self.conversation_repo.clear_pending_intent(chat_id)

        return await self._build_plan_result(
            chat_id=chat_id,
            user_id=user.id,
            trip_id=trip.id,
            chosen_dest=chosen_dest,
            final_days=final_days,
            people=people,
            final_eval=final_eval,
            transport=transport,
            hotel=hotel,
            places=places,
            route=route,
            generated_itin=generated_itin,
            ledger_summary=ledger_summary,
            opt_result=opt_result,
            downgrades=downgrades,
            travel_party=resolved_intent.travel_party,
        )

    def _handle_unrecognized(self, pending_intent: ParsedTripIntent | None) -> OrchestrationResult:
        """UNRECOGNIZED: change nothing, ask a natural clarification."""
        logger.info("[ACTION_ROUTER] UNRECOGNIZED — no state mutation.")
        if pending_intent is not None:
            # Summarize what we know and ask what's next
            missing = []
            if pending_intent.budget is None or pending_intent.budget <= 0:
                missing.append("budget")
            if pending_intent.people is None or pending_intent.people <= 0:
                missing.append("people")
            if pending_intent.days is None or pending_intent.days <= 0:
                missing.append("days")
            if not pending_intent.origin:
                missing.append("origin")

            if missing:
                msg = format_clarification(missing, known_context=pending_intent)
            else:
                msg = (
                    "🤔 I'm not sure what you'd like to do. "
                    "You can say something like:\n"
                    "• \"Recommend another place\"\n"
                    "• \"Change destination to Delhi\"\n"
                    "• \"Make it 4 days\"\n"
                    "• \"Start over\""
                )
        else:
            msg = (
                "👋 I'm not sure what you mean. Please tell me your trip preferences!\n\n"
                "💡 *Example:* `Plan a trip from Chennai to Goa for 2 people, 3 days, budget ₹20,000`"
            )
        return OrchestrationResult(status="CLARIFICATION", message_text=msg)

    # =========================================================================
    # Discovery and Evaluation (unchanged)
    # =========================================================================

    async def _discover_destinations(
        self,
        origin: str,
        budget: Decimal,
        interests: list[str],
        excluded_destinations: list[str] | set[str] | None = None,
        people: int = 1,
        days: int = 1,
    ) -> tuple[list[str], bool]:
        """Discover candidate destinations via curated domestic pool + Google Travel Explore.

        Returns tuple of (candidate_destinations, used_fallback_catalog).

        Candidate ordering (deterministic):
          1. Curated domestic candidates with a valid known offline corridor from origin
          2. Other curated domestic candidates without a direct corridor
          3. Travel Explore candidates with a valid low estimated cost
          4. Remaining Travel Explore candidates (those with missing/no flight_price)

        Gate 1 corridor-awareness:
          When a candidate has a known valid offline corridor, the Travel Explore
          flight_price is NOT used to prune it. The offline corridor guarantees
          Gate 2 can find real transport. Only prune on flight_price when NO valid
          offline corridor exists for this origin→destination pair.

        Missing price rule:
          A missing flight_price is NOT treated as free or cheap. It receives
          _MISSING_PRICE_SENTINEL so it sorts LAST — but it is not pruned solely
          because the price is absent.
        """
        # If test or caller attached custom discovery hook, honor it
        if hasattr(self, "_discover_destinations_from_explore"):
            custom = getattr(self, "_discover_destinations_from_explore")
            if callable(custom):
                try:
                    res = custom(origin, budget, interests)
                except TypeError:
                    res = custom()
                if hasattr(res, "__await__"):
                    res = await res
                return res, False

        excluded = {d.strip().lower() for d in (excluded_destinations or []) if d and d.strip()}
        seen_names: set[str] = set()  # deduplication across curated + Explore

        # ---------------------------------------------------------------
        # Group 1 & 2: Curated domestic candidates (placed FIRST)
        # ---------------------------------------------------------------
        curated_with_corridor: list[str] = []
        curated_no_corridor: list[str] = []

        for entry in _CURATED_DOMESTIC_POOL:
            dest_name: str = entry["destination"]
            if dest_name.lower() in excluded or dest_name.lower() == origin.lower():
                continue
            # Use the corridor origin declared in the pool entry for lookup,
            # falling back to the actual request origin.
            corridor_origin = entry.get("origin_corridor", origin)
            has_corridor = (
                _has_offline_corridor(corridor_origin, dest_name)
                or _has_offline_corridor(origin, dest_name)
            )
            key = dest_name.lower()
            if key not in seen_names:
                seen_names.add(key)
                if has_corridor:
                    curated_with_corridor.append(dest_name)
                    logger.info(
                        "[CURATED] %s added with known offline corridor from %s.",
                        dest_name, corridor_origin,
                    )
                else:
                    curated_no_corridor.append(dest_name)
                    logger.info(
                        "[CURATED] %s added without direct corridor (will need live transport check).",
                        dest_name,
                    )

        # ---------------------------------------------------------------
        # Groups 3 & 4: Google Travel Explore candidates
        # ---------------------------------------------------------------
        explore_with_low_cost: list[str] = []
        explore_missing_price: list[str] = []

        departure_id = resolve_iata(origin)
        if departure_id:
            explore_params: dict[str, Any] = {
                "departure_id": departure_id,
                "currency": "INR",
                "hl": "en",
            }
            if interests:
                explore_params["interests"] = ",".join(interests)

            envelope = await self.cache_manager.get_travel_data(
                engine="google_travel_explore",
                params=explore_params,
                trip_id=None,
            )

            if isinstance(envelope.data, dict):
                for item in (
                    envelope.data.get("destinations")
                    or envelope.data.get("top_destinations")
                    or envelope.data.get("results")
                    or []
                ):
                    name = (
                        item.get("destination")
                        or item.get("city")
                        or item.get("name")
                    )
                    if not name:
                        continue
                    name_str = str(name).title()
                    name_lower = name_str.lower()
                    if name_lower == origin.lower() or name_lower in excluded:
                        continue
                    if name_lower in seen_names:
                        # Already added from curated pool — skip duplicate
                        continue

                    # ---- Corridor-aware Gate 1 ----
                    # If a valid offline corridor exists for this pair,
                    # the flight_price from Explore is irrelevant for pruning.
                    # Gate 2 will evaluate the real corridor transport.
                    corridor_rescues = _has_offline_corridor(origin, name_str)

                    raw_fp = item.get("flight_price") or item.get("price")
                    if raw_fp:
                        fp_dec, _ = parse_price_and_currency(raw_fp)
                        if fp_dec > Decimal("0.00"):
                            total_flight_cost = fp_dec * Decimal(max(1, people))
                            if total_flight_cost > budget and not corridor_rescues:
                                # Only prune when: expensive flight price AND no offline corridor
                                logger.info(
                                    "[SCREENING] Candidate %s rejected: flight cost (%s×%d=%s) > budget %s "
                                    "and no offline corridor available.",
                                    name_str, fp_dec, people, total_flight_cost, budget,
                                )
                                continue
                            elif total_flight_cost > budget and corridor_rescues:
                                logger.info(
                                    "[SCREENING] Candidate %s flight cost (%s×%d=%s) > budget %s "
                                    "BUT offline corridor exists — passing to Gate 2.",
                                    name_str, fp_dec, people, total_flight_cost, budget,
                                )

                    raw_hp = item.get("hotel_price")
                    if raw_hp and days > 1:
                        hp_dec, _ = parse_price_and_currency(raw_hp)
                        nights = max(1, days - 1)
                        if hp_dec > Decimal("0.00") and (hp_dec * Decimal(nights)) > budget:
                            logger.info(
                                "[SCREENING] Candidate %s rejected: lodging cost (%s×%d=%s) > budget %s.",
                                name_str, hp_dec, nights, hp_dec * Decimal(nights), budget,
                            )
                            continue

                    seen_names.add(name_lower)
                    # Sort Explore into low-cost vs missing-price groups
                    if raw_fp:
                        fp_dec, _ = parse_price_and_currency(raw_fp)
                        if fp_dec > Decimal("0.00"):
                            explore_with_low_cost.append(name_str)
                            continue
                    explore_missing_price.append(name_str)
        else:
            logger.info(
                "[DISCOVER] No IATA code for origin=%r — skipping Travel Explore live call.",
                origin,
            )

        # ---------------------------------------------------------------
        # Merge in priority order (curated first, Explore last)
        # ---------------------------------------------------------------
        merged = (
            curated_with_corridor
            + curated_no_corridor
            + explore_with_low_cost
            + explore_missing_price
        )

        logger.info(
            "[DISCOVER] Merged candidate list: curated_corridor=%d curated_no_corridor=%d "
            "explore_low_cost=%d explore_missing_price=%d total=%d",
            len(curated_with_corridor), len(curated_no_corridor),
            len(explore_with_low_cost), len(explore_missing_price),
            len(merged),
        )
        return merged, bool(curated_with_corridor or curated_no_corridor)

    async def _evaluate_trip_candidate(
        self,
        origin: str,
        destination: str,
        people: int,
        days: int,
        budget: Decimal,
        currency: str = "INR",
        travel_party: str | None = None,
        interests: list[str] | None = None,
        transport_mode: str | None = None,
        transport_class: str | None = None,
        is_discovery_candidate: bool = False,
        has_offline_corridor: bool = False,
        provider_calls_counter: int = 0,
        provider_calls_limit: int = 10,
    ) -> dict[str, Any]:
        """Collect travel components, normalize, estimate, and evaluate through Reverse-Budget Engine."""
        _local_call_count = 0
        # 1. Collect required travel components through Cache/Fallback/SerpApi pipeline.
        # For discovery candidates with a known offline corridor, we skip the live flight call
        # to conserve provider-call budget — Gate 2 will use the corridor directly.
        effective_transport_mode = transport_mode
        if is_discovery_candidate and has_offline_corridor and not transport_mode:
            # Force train mode so lookup_transport_options skips the flight branch
            # and goes directly to the train corridor lookup.
            effective_transport_mode = "train"
            logger.info(
                "[GATE] Candidate %s has offline corridor — skipping live flight call, using train mode.",
                destination,
            )
        _local_call_count += 1  # hotel call at minimum
        if not has_offline_corridor and effective_transport_mode != "train":
            _local_call_count += 1  # additional flight call if no corridor

        (
            primary_transport,
            available_transports,
            primary_hotel,
            available_hotels,
            route,
        ) = await self._collect_travel_components(
            origin=origin,
            destination=destination,
            people=people,
            days=days,
            transport_mode=effective_transport_mode,
            transport_class=transport_class,
        )

        if primary_transport is None and effective_transport_mode == "train" and not transport_mode:
            available_transports = await self.lookup_transport_options(
                origin=origin,
                destination=destination,
                people=people,
                transport_mode=None,
                transport_class=transport_class,
            )
            primary_transport = available_transports[0] if available_transports else None

        # Select curated offline attractions via AttractionSelector for offline phase feasibility
        selected_attractions = self.attraction_selector.select_for_itinerary(
            destination=destination,
            travel_party=travel_party,
            interests=interests,
            days=days,
        )

        # 2. Estimation Layer for non-live costs
        food_est = self.estimation.estimate_food(people=people, days=days, tier="standard")
        transit_est = self.estimation.estimate_local_transit_daily(days=days, people=people, mode="metro_bus")
        activities_budget = round(budget * Decimal("0.05"), 2)

        # Inter-city trips require a valid resolved physical transport option.
        # If no flight or train corridor exists, candidate destination is strictly NOT_FEASIBLE.
        is_intercity = origin.lower().strip() != destination.lower().strip()
        if is_intercity and (primary_transport is None or primary_transport.price <= Decimal("0.00")):
            logger.info("Candidate destination %s rejected: no valid transport resolved from %s", destination, origin)
            baseline_eval = self.budget_engine.evaluate(
                total_budget=budget,
                people=people,
                days=days,
                transport=None,
                hotel=primary_hotel,
                food_estimate=food_est,
                local_transit_estimate=transit_est,
                activities_budget=activities_budget,
                currency=currency,
                selected_attractions=selected_attractions,
            )
            return {
                "is_feasible": False,
                "destination": destination,
                "days": days,
                "attractions": selected_attractions,
                "baseline_eval": baseline_eval,
                "opt_result": None,
                "rejection_reason": "NO_TRANSPORT_AVAILABLE",
                "provider_calls_used": _local_call_count,
            }

        # Multi-day trips require a valid accommodation option.
        # For discovered candidates or when live hotel search returns no properties, candidate destination is strictly NOT_FEASIBLE.
        requires_lodging = days > 1
        missing_usable_hotel = primary_hotel is None or primary_hotel.total_price <= Decimal("0.00")
        gw = getattr(self.cache_manager, "gateway", None)
        raw_creds = getattr(gw, "has_credentials", False)
        has_real_creds = (raw_creds is True)
        is_offline_unconfigured = (
            not is_discovery_candidate
            and primary_hotel is None
            and not has_real_creds
        )
        if requires_lodging and missing_usable_hotel and not is_offline_unconfigured:
            logger.info("Candidate destination %s rejected: no valid hotel resolved for %s", destination, origin)
            baseline_eval = self.budget_engine.evaluate(
                total_budget=budget,
                people=people,
                days=days,
                transport=primary_transport,
                hotel=None,
                food_estimate=food_est,
                local_transit_estimate=transit_est,
                activities_budget=activities_budget,
                currency=currency,
                selected_attractions=selected_attractions,
            )
            return {
                "is_feasible": False,
                "destination": destination,
                "days": days,
                "attractions": selected_attractions,
                "baseline_eval": baseline_eval,
                "opt_result": None,
                "rejection_reason": "NO_ACCOMMODATION_AVAILABLE",
                "provider_calls_used": _local_call_count,
            }

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
            selected_attractions=selected_attractions,
        )

        if baseline_eval.is_feasible:
            return {
                "is_feasible": True,
                "destination": destination,
                "days": days,
                "transport": primary_transport,
                "hotel": primary_hotel,
                "route": route,
                "attractions": selected_attractions,
                "evaluation": baseline_eval,
                "opt_result": None,
                "provider_calls_used": _local_call_count,
            }

        # 4. If Over-Budget: Engage 4-Step OptimizationEngine
        logger.info("Destination %s is initially NOT_FEASIBLE. Engaging 4-step Optimizer...", destination)
        opt_result = self.optimizer.optimize(
            trip_id=None,
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
            selected_attractions=selected_attractions,
            requires_transport=is_intercity,
            requires_lodging=requires_lodging,
        )

        has_valid_lodging = (
            not requires_lodging
            or (opt_result.selected_hotel is not None and opt_result.selected_hotel.total_price > Decimal("0.00"))
            or (is_offline_unconfigured and opt_result.final_evaluation is not None and opt_result.final_evaluation.breakdown.hotel_cost > Decimal("0.00"))
        )
        if (
            opt_result.is_feasible
            and (not is_intercity or (opt_result.selected_transport is not None and opt_result.selected_transport.price > Decimal("0.00")))
            and has_valid_lodging
        ):
            return {
                "is_feasible": True,
                "destination": destination,
                "days": opt_result.days,
                "transport": opt_result.selected_transport,
                "hotel": opt_result.selected_hotel,
                "route": route,
                "attractions": selected_attractions,
                "evaluation": opt_result.final_evaluation,
                "opt_result": opt_result,
                "provider_calls_used": _local_call_count,
            }

        return {
            "is_feasible": False,
            "destination": destination,
            "days": days,
            "attractions": selected_attractions,
            "baseline_eval": baseline_eval,
            "opt_result": opt_result,
            "provider_calls_used": _local_call_count,
        }

    async def lookup_transport_options(
        self,
        origin: str,
        destination: str,
        people: int,
        transport_mode: str | None = None,
        transport_class: str | None = None,
        outbound_date: str | None = None,
        return_date: str | None = None,
    ) -> list[FlightOption | TransitOption]:
        """Fetch transport options from Cache/Fallback/SerpApi for the current preference.

        Guarantees that changed transport preferences trigger a fresh query and never reuse stale data.

        SerpApi google_flights requires: departure_id, arrival_id, outbound_date, return_date, adults.
        We resolve origin/destination city names to IATA codes via the location resolver before
        calling the live API.  If either endpoint has no IATA mapping (e.g. Manali), we skip the
        live flight call and go straight to the train corridor fallback.
        """
        from datetime import date, timedelta
        mode = (transport_mode or "").lower()
        cls = (transport_class or "").lower()

        if not mode and cls in ("1ac", "2ac", "3ac", "sleeper"):
            mode = "train"
        elif not mode and cls in ("economy", "premium_economy", "business", "first"):
            mode = "flight"

        results: list[FlightOption | TransitOption] = []

        if mode == "flight" or not mode:
            # Resolve IATA codes — required by SerpApi google_flights
            departure_id = resolve_iata(origin)
            arrival_id = resolve_iata(destination)

            if departure_id and arrival_id:
                # Build dates: use provided dates or default to ~30 days out (4-night trip)
                _today = date.today()
                out_date = outbound_date or (_today + timedelta(days=30)).strftime("%Y-%m-%d")
                ret_date = return_date or (_today + timedelta(days=34)).strftime("%Y-%m-%d")

                flight_params: dict[str, Any] = {
                    "departure_id": departure_id,  # SerpApi documented param
                    "arrival_id":   arrival_id,     # SerpApi documented param
                    "outbound_date": out_date,
                    "return_date":   ret_date,
                    "adults":        people,
                    "currency":      "INR",
                    "hl":            "en",
                    "type":          "1",            # 1 = round trip
                }
                flight_env = await self.cache_manager.get_travel_data(
                    engine="google_flights",
                    params=flight_params,
                )
            else:
                # No IATA mapping for one or both endpoints — return empty envelope
                # so train fallback below can activate
                logger.info(
                    "[FLIGHTS] No IATA mapping: origin=%r (%s) destination=%r (%s). Skipping live flight call.",
                    origin, departure_id, destination, arrival_id,
                )
                from budlance.serpapi.models import TravelDataEnvelope
                flight_env = TravelDataEnvelope(
                    source=DataSource.FALLBACK,
                    engine="google_flights",
                    query_hash="no_iata",
                    data={},
                    is_fallback=True,
                    status="skipped_no_iata",
                )

            flight_candidates = self.normalizer.normalize_flights(flight_env)
            valid_flight_candidates = [
                fc for fc in flight_candidates
                if fc.price is not None and fc.price > Decimal("0.00")
            ]
            if valid_flight_candidates:
                for fc in valid_flight_candidates:
                    if fc.booking_token and not fc.is_exact_booking:
                        try:
                            booking_env = await self.cache_manager.get_flight_booking_options(fc.booking_token)
                            if booking_env and isinstance(booking_env.data, dict) and "booking_options" in booking_env.data:
                                best_b = extract_best_booking_option(booking_env.data)
                                if best_b:
                                    fc.seller = best_b["seller"] or fc.seller
                                    fc.booking_request = best_b["booking_request"]
                                    if best_b["direct_url"]:
                                        fc.deep_link = best_b["direct_url"]
                                        fc.is_exact_booking = True
                        except Exception as exc:
                            logger.debug("Failed to resolve flight booking options for token: %s", exc)
                    if not fc.deep_link:
                        fc.deep_link = build_safe_flight_search_url(
                            origin=fc.departure_airport or origin,
                            destination=fc.arrival_airport or destination,
                            people=people,
                            travel_class=cls,
                        )
                results.extend(valid_flight_candidates)
            # When live flights return no results: do NOT fabricate fake FlightOption (IndiGo 6E-101).
            # Flight mode returns empty list so caller can handle controlled NO_OPTIONS.

        if mode == "train" or not mode:
            transit_env = await self.cache_manager.get_travel_data(
                engine="trains",
                params={"origin": origin, "destination": destination},
            )
            return_env = await self.cache_manager.get_travel_data(
                engine="trains",
                params={"origin": destination, "destination": origin},
            )
            raw_candidates = self.normalizer.normalize_transit(transit_env)
            return_candidates = self.normalizer.normalize_transit(return_env)
            scaled_candidates = build_round_trip_transit_options(
                raw_candidates, return_candidates, people
            )
            valid_scaled_candidates = [
                t for t in scaled_candidates
                if t.price is not None and t.price > Decimal("0.00")
            ]

            if cls:
                matching = []
                for t in valid_scaled_candidates:
                    c_type = (t.class_or_type or "").lower().replace("-", "").replace("_", "").replace(" ", "")
                    req = cls.replace("-", "").replace("_", "").replace(" ", "")
                    if req == "1ac" and c_type in ("1a", "1ac", "firstac", "1stac"):
                        matching.append(t)
                    elif req == "2ac" and c_type in ("2a", "2ac", "secondac", "2ndac"):
                        matching.append(t)
                    elif req == "3ac" and c_type in ("3a", "3ac", "thirdac", "3rdac"):
                        matching.append(t)
                    elif req == "sleeper" and c_type in ("sl", "sleeper"):
                        matching.append(t)
                    elif req in c_type or c_type in req:
                        matching.append(t)
                if matching:
                    results.extend(matching)

            if not results and valid_scaled_candidates:
                results.extend(valid_scaled_candidates)

        return results

    async def _collect_travel_components(
        self,
        origin: str,
        destination: str,
        people: int,
        days: int,
        transport_mode: str | None = None,
        transport_class: str | None = None,
    ) -> tuple[
        FlightOption | TransitOption | None,
        list[FlightOption | TransitOption],
        HotelOption | None,
        list[HotelOption],
        RouteOption | None,
    ]:
        """Fetch and normalize travel, stay, and routes from Cache/Fallback/SerpApi."""
        # a. Transports (Flights + Trains/Buses)
        preferred_transports = await self.lookup_transport_options(
            origin=origin,
            destination=destination,
            people=people,
            transport_mode=transport_mode,
            transport_class=transport_class,
        )

        all_transports: list[FlightOption | TransitOption] = list(preferred_transports)

        primary_transport = all_transports[0] if all_transports else None

        # b. Hotels
        # SerpApi google_hotels requires: q, check_in_date, check_out_date, adults
        from datetime import date, timedelta
        _today = date.today()
        hotel_check_in  = (_today + timedelta(days=30)).strftime("%Y-%m-%d")
        hotel_check_out = (_today + timedelta(days=30 + days)).strftime("%Y-%m-%d")
        hotel_params: dict[str, Any] = {
            "q":              resolve_hotel_query(destination),  # e.g. 'Hotels in Goa'
            "check_in_date":  hotel_check_in,
            "check_out_date": hotel_check_out,
            "adults":         people,
            "currency":       "INR",
            "hl":             "en",
        }
        hotel_env = await self.cache_manager.get_travel_data(
            engine="google_hotels",
            params=hotel_params,
        )
        hotel_candidates = self.normalizer.normalize_hotels(hotel_env)
        primary_hotel = hotel_candidates[0] if hotel_candidates else None

        # c. Routes
        routes_env = await self.cache_manager.get_travel_data(
            engine="google_maps_directions",
            params={"start_addr": origin, "end_addr": destination},
        )
        routes = self.normalizer.normalize_routes(routes_env)
        primary_route = routes[0] if routes else None

        return (
            primary_transport,
            all_transports,
            primary_hotel,
            hotel_candidates,
            primary_route,
        )

    # =========================================================================
    # Trip Pass monetization and gating helpers (Phase 8)
    # =========================================================================

    async def _build_plan_result(
        self,
        chat_id: int,
        user_id: UUID,
        trip_id: UUID,
        chosen_dest: str,
        final_days: int,
        people: int,
        final_eval: BudgetEvaluationResult,
        transport: Any,
        hotel: Any,
        places: Any,
        route: Any,
        generated_itin: Any,
        ledger_summary: Any,
        opt_result: OptimizationResult | None,
        downgrades: list[str],
        travel_party: str | None,
    ) -> OrchestrationResult:
        is_pass_unlocked = True
        pass_status = "PAID"
        checkout_url = None

        if self.enable_trip_pass:
            pass_record = self.payment_service.get_or_create_pass(
                user_id=user_id,
                chat_id=chat_id,
                trip_id=trip_id,
            )
            is_pass_unlocked = (pass_record.status == "PAID")
            pass_status = pass_record.status
            if not is_pass_unlocked:
                session = await self.payment_service.create_checkout_session(
                    trip_id=trip_id,
                    chat_id=chat_id,
                    user_id=user_id,
                )
                checkout_url = session.checkout_url
                msg_text = format_free_summary(
                    destination=chosen_dest,
                    days=final_days,
                    people=people,
                    breakdown=final_eval.breakdown,
                    pass_amount=self.payment_service.pass_amount,
                    checkout_url=checkout_url,
                    travel_party=travel_party,
                )
            else:
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
                    travel_party=travel_party,
                    is_pass_unlocked=True,
                )
        else:
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
                travel_party=travel_party,
                is_pass_unlocked=True,
            )

        return OrchestrationResult(
            trip_id=trip_id,
            status="FEASIBLE",
            selected_destination=chosen_dest,
            feasibility_status="FEASIBLE",
            selected_transport=transport if is_pass_unlocked else None,
            selected_hotel=hotel if is_pass_unlocked else None,
            selected_places=places if is_pass_unlocked else [],
            selected_route=route if is_pass_unlocked else None,
            budget_breakdown=final_eval.breakdown,
            optimization_attempts=opt_result.total_attempts if opt_result else 0,
            downgrades_applied=downgrades,
            generated_itinerary=generated_itin if is_pass_unlocked else None,
            ledger_summary=ledger_summary,
            message_text=msg_text,
            is_pass_unlocked=is_pass_unlocked,
            pass_status=pass_status,
            checkout_url=checkout_url,
        )

    async def _handle_demo_pass_command(self, chat_id: int, target_trip_id: str | None = None) -> OrchestrationResult:
        """Controlled demo/judge bypass to unlock Trip Pass immediately without real payment."""
        trip = None
        if target_trip_id:
            try:
                trip = self.trip_repo.get_trip(UUID(target_trip_id))
            except Exception:
                trip = None
        if not trip:
            trip = self.trip_repo.get_planning_trip(chat_id) or self.trip_repo.get_active_trip(chat_id)

        if not trip:
            return OrchestrationResult(
                status="NO_TRIP",
                message_text=(
                    "⚠️ *No Trip Found to Unlock*\n\n"
                    "You don't have an active or planned trip yet. "
                    "Please plan a trip first (e.g. `Plan a 3-day trip from Chennai to Goa for 2 people with budget ₹20,000`)."
                ),
            )

        pass_record = await self.payment_service.bypass_trip_pass(
            trip_id=trip.id,
            chat_id=chat_id,
            user_id=trip.user_id,
        )

        itin_record = self.itinerary_repo.get_itinerary(trip.id)
        ledger_summary = self.ledger_manager.get_summary(trip.id)

        gen_itin = None
        if itin_record and itin_record.days:
            from budlance.itinerary.models import GeneratedItinerary, ItineraryDay
            days_objs = [
                d if isinstance(d, ItineraryDay) else ItineraryDay.model_validate(d)
                for d in itin_record.days
            ]
            gen_itin = GeneratedItinerary(
                trip_id=trip.id,
                destination=trip.destination or "Destination",
                days_count=trip.duration_days,
                total_budget=trip.budget_total,
                days=days_objs,
                is_feasible=itin_record.is_feasible,
            )

        breakdown = None
        if ledger_summary and getattr(ledger_summary, "allocation", None):
            alloc = ledger_summary.allocation
            transit_cost = Decimal("0.00")
            for e in getattr(ledger_summary, "entries", []):
                if "transit" in e.description.lower() and e.category == "daily_survival":
                    transit_cost = e.allocated_amount
                    break
            from budlance.engine.models import BudgetBreakdown
            b_fixed = alloc.transport_allocated + alloc.stay_allocated
            b_survival = alloc.food_allocated + transit_cost
            b_act = alloc.activities_discretionary
            b_rescue = alloc.rescue_fund_allocated
            tot_alloc = b_fixed + b_survival + b_act + b_rescue
            rem_surplus = ledger_summary.total_budget - tot_alloc
            breakdown = BudgetBreakdown(
                total_budget=ledger_summary.total_budget,
                currency=trip.currency or "INR",
                bucket_a_fixed=b_fixed,
                bucket_b_survival=b_survival,
                bucket_c_activities=b_act,
                bucket_d_rescue=b_rescue,
                transport_cost=alloc.transport_allocated,
                hotel_cost=alloc.stay_allocated,
                food_cost=alloc.food_allocated,
                local_transit_cost=transit_cost,
                attraction_cost=Decimal("0.00"),
                total_allocated=tot_alloc,
                remaining_surplus=rem_surplus,
            )

        if breakdown:
            plan_text = format_feasible_plan(
                destination=trip.destination or "Destination",
                days=trip.duration_days,
                people=trip.people_count,
                breakdown=breakdown,
                transport=None,
                hotel=None,
                itinerary=gen_itin,
                ledger=ledger_summary,
                is_pass_unlocked=True,
            )
            msg_text = f"🎟️ *Judge/Demo Bypass Activated!* ✅\n\n{plan_text}"
        else:
            msg_text = (
                f"🎟️ *Budlance Trip Pass Unlocked via Judge/Demo Bypass!* ✅\n\n"
                f"Your trip to {trip.destination} is fully unlocked. Complete day-by-day attraction schedule, "
                f"booking links, and live In-Trip Rescue are now active."
            )

        return OrchestrationResult(
            trip_id=trip.id,
            status="FEASIBLE",
            selected_destination=trip.destination,
            feasibility_status="FEASIBLE",
            generated_itinerary=gen_itin,
            ledger_summary=ledger_summary,
            budget_breakdown=breakdown,
            message_text=msg_text,
            is_pass_unlocked=True,
            pass_status="PAID",
        )

    async def _handle_pass_status_command(self, chat_id: int) -> OrchestrationResult:
        """Display pass status or provide payment link for the current trip."""
        trip = self.trip_repo.get_planning_trip(chat_id) or self.trip_repo.get_active_trip(chat_id)
        if not trip:
            return OrchestrationResult(
                status="NO_TRIP",
                message_text="⚠️ No active or planned trip found. Please plan a trip first!",
            )

        pass_record = self.payment_service.get_or_create_pass(
            user_id=trip.user_id,
            chat_id=chat_id,
            trip_id=trip.id,
        )

        if pass_record.status == "PAID":
            return OrchestrationResult(
                trip_id=trip.id,
                status="PASS_UNLOCKED",
                message_text=(
                    f"🎟️ *Budlance Trip Pass: ACTIVE ✅*\n\n"
                    f"Your Trip Pass for {trip.destination} is paid and active.\n"
                    f"• Amount: {pass_record.currency} {pass_record.amount:,.2f}\n"
                    f"• Reference: `{pass_record.payment_reference or 'confirmed'}`\n\n"
                    f"Full itinerary, booking links, and live In-Trip Rescue are unlocked."
                ),
                is_pass_unlocked=True,
                pass_status="PAID",
            )

        session = await self.payment_service.create_checkout_session(
            trip_id=trip.id,
            chat_id=chat_id,
            user_id=trip.user_id,
        )

        return OrchestrationResult(
            trip_id=trip.id,
            status="CHECKOUT_PENDING",
            message_text=(
                f"🎟️ *Budlance Trip Pass: Unlock Full Plan*\n\n"
                f"Destination: {trip.destination}\n"
                f"Service Fee: {session.currency} {session.amount:,.2f} (one-time service fee)\n\n"
                f"Unlock full day-by-day itinerary, curated attraction schedule, and live In-Trip Rescue:\n"
                f"👉 [Proceed to Checkout]({session.checkout_url})\n\n"
                f"_(Judge/Demo review: send `/demo_pass` to unlock instantly without payment)_"
            ),
            is_pass_unlocked=False,
            pass_status=pass_record.status,
            checkout_url=session.checkout_url,
        )

    async def _handle_verify_payment_command(self, chat_id: int) -> OrchestrationResult:
        """Handle user claims of payment ('paid'), verifying authoritatively against backend status."""
        trip = self.trip_repo.get_planning_trip(chat_id) or self.trip_repo.get_active_trip(chat_id)
        if not trip:
            return OrchestrationResult(
                status="NO_TRIP",
                message_text="⚠️ No trip found to verify payment for. Please plan a trip first!",
            )

        pass_record = self.trip_pass_repo.get_by_trip_id(trip.id)
        if pass_record and pass_record.status == "PAID":
            return await self._handle_demo_pass_command(chat_id)

        return OrchestrationResult(
            trip_id=trip.id,
            status="PAYMENT_PENDING",
            message_text=(
                "⏳ *Payment Verification Pending*\n\n"
                "We have not yet received payment confirmation from the gateway for this trip.\n"
                "If you just completed payment, please wait a moment or send `/pass` to check again.\n\n"
                "💡 *Evaluator / Demo Bypass:* Send `/demo_pass` to unlock the full trip plan immediately."
            ),
            is_pass_unlocked=False,
            pass_status=pass_record.status if pass_record else "FREE",
        )
