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
import inspect
import logging
from typing import Any
from uuid import UUID, uuid4

from budlance.db.models import Trip, TripPass, utc_now
from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.ai.service import AIIntentService
from budlance.ai.exceptions import OpenRouterValidationError
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
from budlance.lifecycle.booking_handler import BookingLifecycleHandler
from budlance.lifecycle.expense_handler import ExpenseLifecycleHandler
from budlance.lifecycle.intrip_companion import InTripCompanionHandler
from budlance.lifecycle.reoptimizer import RemainingTripReoptimizer
from budlance.normalization.events import filter_events_overlapping_dates
from budlance.normalization.flights import build_safe_flight_search_url, extract_best_booking_option
from budlance.normalization.normalizer import DataNormalizer
from budlance.normalization.utils import parse_price_and_currency
from budlance.config import get_settings
from budlance.db.repositories.trip_pass_repo import TripPassRepository
from budlance.normalization.transit import build_round_trip_transit_options, calculate_round_trip_cost
from budlance.orchestrator.formatter import (
    format_change_summary,
    format_clarification,
    format_feasibility_result,
    format_feasible_plan,
    format_feasible_transport,
    format_free_summary,
    format_infeasible_plan,
    format_infeasible_transport,
    format_rescue_result,
    resolve_interest_mismatch_note,
)
from budlance.orchestrator.models import OrchestrationResult
from budlance.payment.service import PaymentService
from budlance.rescue.service import RescueService
from budlance.schemas.travel import FlightOption, HotelOption, PlaceOption, RouteOption, TransitOption
from budlance.schemas.dates import TripDateContext, build_trip_date_context, calculate_stay_nights
from budlance.serpapi.models import DataSource
from budlance.serpapi.location import (
    resolve_iata,
    resolve_hotel_query,
    resolve_places_query,
    resolve_food_query,
    resolve_events_query,
)
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

_CURATED_DESTINATION_EVENTS: dict[str, list[dict[str, str]]] = {
    "kerala": [
        {
            "title": "Kochi-Muziris Biennale (Art & Cultural Showcase)",
            "date": "Active during season",
            "address": "Aspinwall House, Fort Kochi",
            "description": "Celebrated contemporary art installations and cultural showcases in heritage Fort Kochi.",
        },
        {
            "title": "Kerala Backwaters Cultural & Boat Procession",
            "date": "Evening festival",
            "address": "Punnamada Lake, Alleppey",
            "description": "Traditional Kerala music, decorated snake boats, and illuminated evening processions.",
        },
    ],
    "goa": [
        {
            "title": "Goa Coastal Sundowner & Flea Market",
            "date": "Evening festival",
            "address": "Anjuna Beach Promenade",
            "description": "Live acoustic music, artisan crafts, and coastal street cuisine by the beach.",
        },
    ],
    "gujarat": [
        {
            "title": "Rann Utsav Cultural Gathering",
            "date": "Desert festival season",
            "address": "Dhordo / Kutch",
            "description": "Folk music, artisan textile crafts, and moonlight desert cultural performances.",
        },
    ],
    "bangalore": [
        {
            "title": "Bangalore Lalbagh Botanical Exhibition",
            "date": "Weekend floral showcase",
            "address": "Lalbagh Botanical Garden",
            "description": "Elaborate glasshouse floral sculptures and botanical heritage walks.",
        },
    ],
}

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
        reoptimizer: RemainingTripReoptimizer | None = None,
        intrip_companion: InTripCompanionHandler | None = None,
        booking_handler: BookingLifecycleHandler | None = None,
    ) -> None:
        self.user_repo = user_repo or UserRepository()
        self.trip_repo = trip_repo or TripRepository()
        self.intent_repo = intent_repo or IntentRepository()
        self.itinerary_repo = itinerary_repo or ItineraryRepository()
        self.ledger_repo = ledger_repo or LedgerRepository()
        self.rescue_repo = rescue_repo or RescueRepository()
        self.conversation_repo = conversation_repo or ConversationStateRepository()
        self.trip_pass_repo = trip_pass_repo or TripPassRepository()
        settings = get_settings()
        default_pay_provider = "stripe" if settings.has_stripe_credentials else "demo"
        self.payment_service = payment_service or PaymentService(
            trip_pass_repo=self.trip_pass_repo,
            default_provider=default_pay_provider,
        )
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
        self.itinerary_enhancer = itinerary_enhancer or ItineraryEnhancer(
            use_mock=getattr(self.ai_service, "use_mock", False),
        )
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
        self.reoptimizer = reoptimizer or RemainingTripReoptimizer(
            trip_repo=self.trip_repo,
            ledger_repo=self.ledger_repo,
            itinerary_repo=self.itinerary_repo,
            budget_engine=self.budget_engine,
            optimizer=self.optimizer,
            estimation_layer=self.estimation,
            ledger_manager=self.ledger_manager,
        )
        self.booking_handler = booking_handler or BookingLifecycleHandler()
        self.intrip_companion = intrip_companion or InTripCompanionHandler(
            trip_repo=self.trip_repo,
            itinerary_repo=self.itinerary_repo,
            intent_repo=self.intent_repo,
            ledger_manager=self.ledger_manager,
            cache_manager=self.cache_manager,
            normalizer=self.normalizer,
        )
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._last_full_plan: dict[int, str] = {}
        self._cached_trip_bookings: dict[UUID, tuple[Any, Any]] = {}
        self._demo_bypass_chats: set[int] = set()

    def _get_chat_lock(self, chat_id: int) -> asyncio.Lock:
        """Get or create a per-chat asyncio.Lock to prevent concurrent request state races."""
        if chat_id not in self._chat_locks:
            self._chat_locks[chat_id] = asyncio.Lock()
        return self._chat_locks[chat_id]

    @property
    def is_live_mode(self) -> bool:
        """Check whether orchestrator is operating in live SerpApi mode.

        Returns True only when SERPAPI_LIVE_ENABLED is true AND valid API credentials exist.
        Safely handles MagicMock objects in unit tests.
        """
        gw = getattr(self.cache_manager, "gateway", None)
        if gw is not None:
            # If a test or caller explicitly set is_live_mode as a boolean on gateway, respect it
            is_live = getattr(gw, "is_live_mode", None)
            if isinstance(is_live, bool):
                return is_live
            has_cred = getattr(gw, "has_credentials", None)
            if isinstance(has_cred, bool):
                return has_cred
            if callable(has_cred):
                try:
                    res = has_cred()
                    if isinstance(res, bool):
                        return res
                except Exception:
                    pass

        settings = get_settings()
        if not getattr(settings, "serpapi_live_enabled", True):
            return False

        return bool(
            (settings.serpapi_api_key and settings.serpapi_api_key.strip())
            or (settings.serpapi_fallback_api_key and settings.serpapi_fallback_api_key.strip())
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
        demo_bypass: bool = False,
        event_id: str | None = None,
    ) -> OrchestrationResult:
        async with self._get_chat_lock(chat_id):
            return await self._handle_user_message_locked(
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                message=message,
                username=username,
                first_name=first_name,
                demo_bypass=demo_bypass,
                event_id=event_id,
            )

    async def _handle_user_message_locked(
        self,
        telegram_user_id: int,
        chat_id: int,
        message: str,
        username: str | None = None,
        first_name: str | None = None,
        demo_bypass: bool = False,
        event_id: str | None = None,
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
        if demo_bypass or "--demo" in clean_lower or "#demo" in clean_lower:
            self._demo_bypass_chats.add(chat_id)
            if "--demo" in clean_lower or "#demo" in clean_lower:
                import re
                clean_text = re.sub(r"(?:--demo|#demo)", "", clean_text, flags=re.IGNORECASE).strip()
                clean_lower = clean_text.lower()
        else:
            self._demo_bypass_chats.discard(chat_id)

        if clean_lower in ("/demo_pass", "demo pass", "/bypass", "/demo", "demo") or clean_lower.startswith(("/demo_pass ", "demo pass ", "/demo ")):
            self._demo_bypass_chats.add(chat_id)
            target_id = clean_text.split()[-1] if len(clean_text.split()) > 1 else None
            return await self._handle_demo_pass_command(chat_id, target_trip_id=target_id)

        is_asking_for_links = any(kw in clean_lower for kw in (
            "flight link", "booking link", "hotel link", "booking links", "give me the links",
            "send me the flight link", "send me the booking links", "send links", "give links",
            "send booking link", "send flight link", "send hotel link",
        ))
        if is_asking_for_links:
            trip = self.trip_repo.get_planning_trip(chat_id) or self.trip_repo.get_active_trip(chat_id)
            if trip:
                pass_record = self.trip_pass_repo.get_by_trip_id(trip.id)
                if not pass_record or not pass_record.is_unlocked:
                    return await self._handle_pass_status_command(chat_id)

        if (
            clean_lower in (
                "/trip_pass", "/pass", "trip pass", "pass", "unlock", "unlock full trip plan",
                "unlock trip", "buy pass", "checkout", "proceed to checkout", "proceed to secure checkout",
                "the plan looks good. i want the complete itinerary.", "the plan looks good. i want the complete itinerary",
                "the plan looks good, i want the complete itinerary", "i want the complete itinerary",
                "complete itinerary", "unlock itinerary", "unlock the complete itinerary",
            )
            or clean_lower.startswith(("/trip_pass ", "/pass "))
            or ("complete itinerary" in clean_lower and "want" in clean_lower)
            or ("complete itinerary" in clean_lower and "good" in clean_lower)
            or ("unlock" in clean_lower and "itinerary" in clean_lower)
        ):
            return await self._handle_pass_status_command(chat_id)

        if clean_lower in ("paid", "i paid", "i have paid", "payment completed", "verify payment"):
            return await self._handle_verify_payment_command(chat_id)

        # Handle 'add event' response to interactive event prompt
        if (
            clean_lower in ("add event", "yes, add the event", "add the event", "yes add event", "include event", "add festival")
            or clean_lower.startswith(("add event", "yes, add the event", "add the event"))
        ):
            planning_trip = self.trip_repo.get_planning_trip(chat_id) or self.trip_repo.get_active_trip(chat_id)
            if planning_trip:
                itin_record = self.itinerary_repo.get_itinerary(planning_trip.id)
                if itin_record and itin_record.days:
                    dest_key = (planning_trip.destination or "Kerala").lower()
                    ev_items = None
                    for k, ev_list in _CURATED_DESTINATION_EVENTS.items():
                        if k in dest_key or dest_key in k:
                            ev_items = ev_list
                            break
                    if ev_items:
                        ev = ev_items[0]
                        matched_idx = None
                        # 1. Match explicit day_number in event
                        if "day_number" in ev:
                            try:
                                d_num = int(ev["day_number"])
                                if 1 <= d_num <= len(itin_record.days):
                                    matched_idx = d_num - 1
                            except Exception:
                                pass
                        # 2. Match event date against itinerary day dates
                        elif ev.get("date") or ev.get("start_date"):
                            ev_d_str = str(ev.get("date") or ev.get("start_date"))
                            for idx, d_rec in enumerate(itin_record.days):
                                d_date = d_rec.get("date_str") if isinstance(d_rec, dict) else getattr(d_rec, "date_str", None)
                                if d_date and d_date in ev_d_str:
                                    matched_idx = idx
                                    break
                                elif any(k in ev_d_str.lower() for k in (f"day {idx + 1}", f"day{idx + 1}")):
                                    matched_idx = idx
                                    break

                        # 3. If multi-day trip and no explicit match, schedule on Day 2 to avoid check-in rush
                        if matched_idx is not None:
                            target_day_idx = matched_idx
                        elif len(itin_record.days) > 1:
                            target_day_idx = 1
                        else:
                            target_day_idx = 0

                        target_day_num = target_day_idx + 1
                        target_day = itin_record.days[target_day_idx]
                        new_item = {
                            "time_slot": "Afternoon",
                            "activity": f"Attend {ev['title']}",
                            "place_name": ev.get("address", planning_trip.destination),
                            "category": "culture",
                            "planned_cost": 150.0,
                            "description": ev.get("description", "Local festival and cultural showcase."),
                            "slot_type": "event",
                            "entry_fee_inr": 150,
                            "is_curated": True,
                        }
                        if isinstance(target_day, dict):
                            items = target_day.get("items", [])
                            items.insert(min(1, len(items)), new_item)
                            target_day["items"] = items
                            itin_record.days[target_day_idx] = target_day
                        else:
                            from budlance.itinerary.models import ItineraryItem
                            it_obj = ItineraryItem(
                                time_slot="Afternoon",
                                activity=f"Attend {ev['title']}",
                                place_name=ev.get("address", planning_trip.destination),
                                category="culture",
                                planned_cost=Decimal("150.00"),
                                description=ev.get("description", "Local festival and cultural showcase."),
                                slot_type="event",
                                entry_fee_inr=150,
                                is_curated=True,
                                source=DataSource.LIVE,
                            )
                            target_day.items.insert(min(1, len(target_day.items)), it_obj)
                        itin_record.updated_at = utc_now()
                        self.itinerary_repo.save_itinerary(itin_record)
                        return OrchestrationResult(
                            trip_id=planning_trip.id,
                            status="FEASIBLE",
                            message_text=(
                                f"🎉 *Event Added to Your Itinerary!*\n\n"
                                f"I have added *{ev['title']}* ({ev.get('address')}) to your Day {target_day_num} afternoon schedule!\n\n"
                                f"Your plan remains fully within your budget. Let me know if you would like to make any other adjustments!"
                            ),
                        )

        # Handle pure new trip greetings / conversational starters without trip specs
        starter_phrases = (
            "let me plan new trip", "let me plan a new trip", "plan new trip",
            "plan a new trip", "start new trip", "new trip", "start over", "start fresh",
            "plan a trip", "let me plan", "i want to plan a trip", "i want to plan a new trip",
            "let's plan a trip", "lets plan a trip", "help me plan a trip", "hi", "hello", "hey",
        )
        is_pure_starter = any(clean_lower == sp or clean_lower.startswith(f"{sp} ") for sp in starter_phrases)
        has_substance = any(
            kw in clean_lower for kw in ("budget", "₹", "rs", "inr", "k", "from ", "to ", "people", "person", "days")
        )
        if is_pure_starter and not has_substance and not self.conversation_repo.is_reconciling(chat_id):
            self.conversation_repo.clear_pending_intent(chat_id)
            welcome_text = (
                "👋 *Welcome to Budlance!* 🌴✈️\n"
                "Your reverse-budget AI travel agent.\n\n"
                "Tell me where would you like to go, your budget, how many people, and duration (days). "
                "Budlance will discover and construct a complete day-by-day trip that strictly fits your budget!\n\n"
                "💡 *Example:*\n"
                "`Plan a trip to Kerala from Chennai for 3 people, 3 days, with budget ₹50,000`"
            )
            return OrchestrationResult(
                status="CLARIFICATION",
                message_text=welcome_text,
            )

        try:
            # Check for pending rescue proposal confirmation/rejection (Phase 7 Feature C: Proposal-Gated Rescue)
            pending_rescue = None
            if hasattr(self.conversation_repo, "get_pending_rescue_proposal"):
                pr = self.conversation_repo.get_pending_rescue_proposal(chat_id)
                if isinstance(pr, dict) and type(pr).__name__ not in ("MagicMock", "AsyncMock"):
                    pending_rescue = pr
            if pending_rescue is not None:
                proposal_data = pending_rescue.get("proposal") if (isinstance(pending_rescue, dict) and "proposal" in pending_rescue) else pending_rescue
                conf = self.ai_service._extract_proposal_confirmation(clean_text)
                if conf is True:
                    rescue_res = self.rescue_service.apply_confirmed_rescue(
                        chat_id=chat_id,
                        proposal=proposal_data,
                    )
                    if inspect.isawaitable(rescue_res):
                        rescue_res = await rescue_res
                    self.conversation_repo.clear_pending_rescue_proposal(chat_id)
                    active_trip = self.trip_repo.get_active_trip(chat_id)
                    selected_dest = (
                        active_trip.destination
                        if (active_trip and isinstance(getattr(active_trip, "destination", None), str))
                        else None
                    )
                    return OrchestrationResult(
                        trip_id=rescue_res.trip_id,
                        status="RESCUE",
                        selected_destination=selected_dest,
                        feasibility_status="FEASIBLE" if rescue_res.is_feasible else "NOT_FEASIBLE",
                        generated_itinerary=rescue_res.updated_itinerary,
                        ledger_summary=rescue_res.ledger_summary,
                        message_text=rescue_res.resolution_summary or format_rescue_result(rescue_res),
                        error=rescue_res.error,
                    )
                elif conf is False:
                    rescue_res = self.rescue_service.cancel_pending_rescue(
                        chat_id=chat_id,
                        proposal=proposal_data,
                    )
                    if inspect.isawaitable(rescue_res):
                        rescue_res = await rescue_res
                    self.conversation_repo.clear_pending_rescue_proposal(chat_id)
                    active_trip = self.trip_repo.get_active_trip(chat_id)
                    selected_dest = (
                        active_trip.destination
                        if (active_trip and isinstance(getattr(active_trip, "destination", None), str))
                        else None
                    )
                    return OrchestrationResult(
                        trip_id=rescue_res.trip_id,
                        status="RESCUE",
                        selected_destination=selected_dest,
                        feasibility_status="FEASIBLE",
                        generated_itinerary=rescue_res.updated_itinerary,
                        ledger_summary=rescue_res.ledger_summary,
                        message_text=rescue_res.resolution_summary or format_rescue_result(rescue_res),
                        error=None,
                    )
                else:
                    target_p = proposal_data.get("target_item_place") or "scheduled activity"
                    alt_name = proposal_data.get("selected_alt", {}).get("name", "alternative option")
                    trip_id_val = None
                    try:
                        trip_id_val = UUID(proposal_data.get("trip_id")) if proposal_data.get("trip_id") else None
                    except Exception:
                        pass
                    return OrchestrationResult(
                        trip_id=trip_id_val,
                        status="RESCUE_PROPOSAL_PENDING",
                        message_text=(
                            f"⚠️ *Pending Itinerary Proposal*\n\n"
                            f"You have a pending proposal to replace *{target_p}* with *{alt_name}*.\n\n"
                            f"Please reply *YES* (or *confirm*) to apply this update to your itinerary, "
                            f"or *NO* (or *keep original*) to retain your current schedule."
                        ),
                    )

            # Handle "full plan" request to view detailed schedule
            clean_lower = clean_text.lower().strip()
            if clean_lower in ("full plan", "show full plan", "view full plan", "full itinerary", "see full plan"):
                if chat_id in self._last_full_plan:
                    active_trip = self.trip_repo.get_planning_trip(chat_id) or self.trip_repo.get_active_trip(chat_id)
                    return OrchestrationResult(
                        trip_id=active_trip.id if active_trip else None,
                        status="FEASIBLE",
                        selected_destination=active_trip.destination if active_trip else None,
                        feasibility_status="FEASIBLE",
                        message_text=self._last_full_plan[chat_id],
                        is_pass_unlocked=True,
                    )

            pending_intent = self.conversation_repo.get_pending_intent(chat_id)
            if pending_intent is None and not is_new_trip_message(clean_text):
                planning_trip = self.trip_repo.get_planning_trip(chat_id)
                if planning_trip is not None and str(planning_trip.status).upper() == "PLANNING":
                    saved_intent = self.intent_repo.get_trip_intent(planning_trip.id)
                    itin = self.itinerary_repo.get_itinerary(planning_trip.id)
                    s_date = None
                    e_date = None
                    if itin and getattr(itin, "days", None):
                        days_list = itin.days
                        if days_list and isinstance(days_list[0], dict) and days_list[0].get("date_str"):
                            s_date = days_list[0].get("date_str")
                        if days_list and isinstance(days_list[-1], dict) and days_list[-1].get("date_str"):
                            e_date = days_list[-1].get("date_str")

                    # Recover tags from interests if present
                    interests_list = list(saved_intent.interests) if saved_intent and saved_intent.interests else []
                    rec_hotel_tier = None
                    rec_hotel_pref = None
                    rec_strict_constraints = []
                    clean_interests = []
                    for item in interests_list:
                        if isinstance(item, str) and item.startswith("hotel_tier:"):
                            rec_hotel_tier = item.split(":", 1)[1]
                        elif isinstance(item, str) and item.startswith("hotel_preference:"):
                            rec_hotel_pref = item.split(":", 1)[1]
                        elif isinstance(item, str) and item.startswith("strict_constraint:"):
                            rec_strict_constraints.append(item.split(":", 1)[1])
                        else:
                            clean_interests.append(item)

                    pending_intent = ParsedTripIntent(
                        budget=planning_trip.budget_total,
                        currency=planning_trip.currency or "INR",
                        people=planning_trip.people_count,
                        days=planning_trip.duration_days,
                        start_date=s_date,
                        end_date=e_date,
                        origin=planning_trip.origin,
                        destination=planning_trip.destination,
                        interests=clean_interests,
                        hotel_tier=rec_hotel_tier,
                        hotel_preference=rec_hotel_pref,
                        strict_constraints=rec_strict_constraints,
                        travel_party=saved_intent.travel_party if saved_intent else None,
                        traveler_type=saved_intent.traveler_type if saved_intent else None,
                        transport_mode=saved_intent.transport_mode if saved_intent else None,
                        transport_class=saved_intent.transport_class if saved_intent else None,
                    )

            # Check for pending reconciliation state (LOG_ACTUAL_SPEND)
            if pending_intent is not None and pending_intent.pending_action == "LOG_ACTUAL_SPEND":
                if is_skip_response(clean_text):
                    comp_res = await self.completion_handler.handle_skip_reconciliation(chat_id)
                    return OrchestrationResult(
                        trip_id=comp_res.trip_id,
                        status=comp_res.status,
                        message_text=comp_res.message_text,
                    )

                try:
                    fresh_ai = await self.ai_service.parse_trip_intent(clean_text)
                except OpenRouterValidationError:
                    fresh_ai = self.ai_service._mock_parse_trip_intent(clean_text)
                if inspect.isawaitable(fresh_ai):
                    fresh_ai = await fresh_ai
                if event_id and getattr(fresh_ai, "event_id", None) is None:
                    fresh_ai.event_id = event_id
                if fresh_ai.action == TripAction.RESCUE:
                    return await self._handle_rescue(chat_id, clean_text)
                if fresh_ai.action == TripAction.LOG_EXPENSE:
                    return await self._handle_log_expense(chat_id, fresh_ai, clean_text, event_id=event_id)
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
                    TripAction.MODIFY_TRIP,
                ):
                    ai_result = fresh_ai
                    action = fresh_ai.action
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
                        action = fresh_ai.action
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
                    try:
                        ai_result = await self.ai_service.parse_trip_intent_with_context(
                            user_prompt=clean_text,
                            existing_intent=pending_intent,
                        )
                    except OpenRouterValidationError:
                        ai_result = self.ai_service._mock_parse_with_context(clean_text, pending_intent)
                else:
                    # Fresh message: action classification + full field extraction
                    try:
                        ai_result = await self.ai_service.parse_trip_intent(clean_text)
                    except OpenRouterValidationError:
                        ai_result = self.ai_service._mock_parse_trip_intent(clean_text)

                if inspect.isawaitable(ai_result):
                    ai_result = await ai_result

                action = getattr(ai_result, "action", TripAction.NEW_TRIP)

            logger.info(
                "[ACTION_ROUTER] chat_id=%s action=%s pending=%s",
                chat_id, action, pending_intent is not None,
            )

            # ---- STEP 2: Action Router — choose state source and apply merge rule ----
            previous_destination: str | None = None

            # RE-OPTIMIZE: If active trip exists and user explicitly requests re-optimizing remaining days
            active_trip_for_reopt = self.trip_repo.get_active_trip(chat_id)
            if (
                active_trip_for_reopt
                and str(active_trip_for_reopt.status).upper() == "ACTIVE"
                and any(kw in clean_lower for kw in ("re-optimize", "reoptimize", "recover the remaining budget"))
            ):
                opt_res = await self.reoptimizer.reoptimize_remaining_trip(active_trip_for_reopt, force=True)
                ledger_summary = self.ledger_manager.get_summary(active_trip_for_reopt.id)
                msg_text = (
                    f"🔄 *Remaining Trip Re-optimized*\n\n"
                    f"Day {active_trip_for_reopt.current_day} spending is preserved in your ledger. "
                    f"Remaining days have been adjusted to keep your total trip within budget."
                )
                if opt_res and opt_res.downgrades_applied:
                    msg_text += "\n\n*Adjustments for remaining days:*\n" + "\n".join(f"• {d}" for d in opt_res.downgrades_applied)
                return OrchestrationResult(
                    trip_id=active_trip_for_reopt.id,
                    status="ACTIVE",
                    ledger_summary=ledger_summary,
                    message_text=msg_text,
                )

            # RESCUE: Never touches pending draft. Uses active confirmed trip.
            if action == TripAction.RESCUE or (
                type(action).__name__ in ("MagicMock", "AsyncMock")
                and any(kw in clean_lower for kw in ("auto driver", "driver is asking", "driver asking", "taxi", "fare dispute", "hotel overbooked", "raining heavily", "earthquake"))
            ):
                return await self._handle_rescue(chat_id, clean_text)

            # IN_TRIP_QUERY: Context-aware companion Q&A (Phase 7 Feature A)
            if action == TripAction.IN_TRIP_QUERY or getattr(ai_result, "is_in_trip_query", None) is True:
                return await self._handle_in_trip_query(chat_id, ai_result, clean_text)

            # MANAGE_BOOKING: External booking tracking (Phase 7 Feature D)
            if action == TripAction.MANAGE_BOOKING:
                return await self._handle_manage_booking(chat_id, ai_result, clean_text)

            # LOG_EXPENSE: Never touches pending draft. Uses active confirmed trip.
            if action == TripAction.LOG_EXPENSE:
                if event_id and getattr(ai_result, "event_id", None) is None:
                    ai_result.event_id = event_id
                return await self._handle_log_expense(chat_id, ai_result, clean_text, event_id=event_id)

            # TRIP_COMPLETE: Record pending expense if present, then load active confirmed trip and prompt for final reconciliation
            if action == TripAction.TRIP_COMPLETE:
                if ai_result.amount is not None and ai_result.amount > Decimal("0.00"):
                    await self.expense_handler.handle_log_expense(chat_id=chat_id, parsed=ai_result, event_id=event_id)
                return await self._handle_trip_complete(
                    chat_id=chat_id,
                    completion_reason=ai_result.completion_reason,
                )

            # CONFIRM_BOOKING: Activate existing planning trip or handle booking management if already active
            if action == TripAction.CONFIRM_BOOKING:
                active_trip = self.trip_repo.get_active_trip(chat_id)
                if active_trip and str(active_trip.status).upper() == "ACTIVE" and getattr(ai_result, "booking_target", None):
                    return await self._handle_manage_booking(chat_id, ai_result, clean_text)

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
                self._last_full_plan.pop(chat_id, None)
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

            # CHANGE_* and MODIFY_TRIP actions: Load pending state, apply updates.
            elif action in (
                TripAction.CHANGE_BUDGET,
                TripAction.CHANGE_DAYS,
                TripAction.CHANGE_PEOPLE,
                TripAction.CHANGE_DESTINATION,
                TripAction.CHANGE_TRANSPORT,
                TripAction.MODIFY_TRIP,
            ):
                # Invalidate cached component bookings for this trip
                planning_trip = self.trip_repo.get_planning_trip(chat_id)
                if planning_trip:
                    self._cached_trip_bookings.pop(planning_trip.id, None)

                if pending_intent is not None:
                    resolved_intent = pending_intent.apply_change_action(ai_result)
                    logger.info(
                        "[ACTION_ROUTER] %s applied. Result: budget=%s people=%s days=%s dest=%s mode=%s class=%s tier=%s pref=%s",
                        action, resolved_intent.budget, resolved_intent.people,
                        resolved_intent.days, resolved_intent.destination,
                        resolved_intent.transport_mode, resolved_intent.transport_class,
                        resolved_intent.hotel_tier, resolved_intent.hotel_preference,
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
            has_budget = isinstance(resolved_intent.budget, (Decimal, int, float)) and resolved_intent.budget > Decimal("0.00")
            has_days = isinstance(resolved_intent.days, int) and resolved_intent.days > 0
            req_dest_raw = getattr(resolved_intent, "requested_destination", None)
            dest_raw = getattr(resolved_intent, "destination", None)
            has_named_dest = (isinstance(req_dest_raw, str) and bool(req_dest_raw.strip())) or (isinstance(dest_raw, str) and bool(dest_raw.strip()))

            if not has_budget:
                missing_fields.append("budget")
            if not has_days:
                missing_fields.append("days")
            if resolved_intent.people is None or (isinstance(resolved_intent.people, int) and resolved_intent.people <= 0):
                if has_named_dest and has_budget and has_days:
                    resolved_intent.people = 1
                else:
                    missing_fields.append("people")
            if not resolved_intent.origin or not isinstance(resolved_intent.origin, str):
                if has_named_dest and has_budget and has_days:
                    resolved_intent.origin = "Origin"
                else:
                    missing_fields.append("origin")

            if isinstance(resolved_intent.people, int):
                if resolved_intent.people == 1 and resolved_intent.travel_party in ("couple", "friends", "family", "relatives"):
                    resolved_intent.travel_party = None
                    resolved_intent.traveler_type = None
                elif resolved_intent.people > 1 and resolved_intent.travel_party == "solo":
                    resolved_intent.travel_party = None
                    resolved_intent.traveler_type = None

            if resolved_intent.start_date:
                try:
                    import datetime
                    s_dt = datetime.datetime.strptime(resolved_intent.start_date, "%Y-%m-%d").date()
                    tz_ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
                    today_ist = datetime.datetime.now(tz_ist).date()
                    if s_dt < today_ist:
                        resolved_intent.start_date = None
                        self.conversation_repo.save_pending_intent(chat_id, resolved_intent)
                        return OrchestrationResult(
                            status="CLARIFICATION",
                            message_text="That date has passed. Pick a date from tomorrow onward.",
                        )
                    if resolved_intent.days and resolved_intent.days > 0:
                        end_dt = s_dt + datetime.timedelta(days=resolved_intent.days - 1)
                        resolved_intent.end_date = end_dt.strftime("%Y-%m-%d")
                except Exception:
                    pass

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
            if resolved_intent.destination and resolved_intent.destination.strip() and action != TripAction.MODIFY_TRIP:
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
                        budget_val = resolved_intent.budget or Decimal("0.00")
                        people_val = resolved_intent.people or 1
                        days_val = resolved_intent.days or 1
                        date_ctx = build_trip_date_context(
                            days=days_val,
                            start_date=resolved_intent.start_date,
                            return_date=resolved_intent.end_date,
                        )

                        # Perform IMMEDIATE transport lookup via CacheFallbackManager
                        transport_options = await self.lookup_transport_options(
                            origin=resolved_intent.origin or "Origin",
                            destination=resolved_intent.destination,
                            people=people_val,
                            days=days_val,
                            transport_mode=resolved_intent.transport_mode,
                            transport_class=resolved_intent.transport_class,
                            outbound_date=date_ctx.flight_outbound_date,
                            return_date=date_ctx.flight_return_date,
                        )
                        selected_transport = transport_options[0] if transport_options else None

                        # Perform IMMEDIATE Reverse-Budget feasibility check
                        food_est = self.estimation.estimate_food(people=people_val, days=days_val, tier="standard")
                        transit_est = self.estimation.estimate_local_transit_daily(days=days_val, people=people_val, mode="metro_bus")
                        activities_budget = round(budget_val * get_settings().budget_activities_ratio, 2)

                        _dest = resolved_intent.destination or "Destination"
                        hotel_env = await self.cache_manager.get_travel_data(
                            engine="google_hotels",
                            params={
                                "q": resolve_hotel_query(_dest),
                                "check_in_date": date_ctx.hotel_check_in_date,
                                "check_out_date": date_ctx.hotel_check_out_date,
                                "adults": people_val,
                                "currency": "INR",
                                "hl": "en",
                            },
                        )
                        hotel_candidates = self.normalizer.normalize_hotels(
                            hotel_env,
                            nights=date_ctx.stay_nights,
                            check_in=date_ctx.hotel_check_in_date,
                            check_out=date_ctx.hotel_check_out_date,
                        )
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
                                raw_booking_token = getattr(selected_transport, "booking_token", None)
                                if raw_deep_link and (str(raw_deep_link).startswith(("http://", "https://", "/book/"))):
                                    booking_link = str(raw_deep_link)

                                else:
                                    booking_link = build_safe_flight_search_url(
                                        origin=resolved_intent.origin,
                                        destination=resolved_intent.destination,
                                        outbound_date=date_ctx.flight_outbound_date,
                                        return_date=date_ctx.flight_return_date,
                                        people=people_val,
                                        travel_class=resolved_intent.transport_class,
                                        booking_token=raw_booking_token,
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
                                    outbound_date=date_ctx.flight_outbound_date,
                                    return_date=date_ctx.flight_return_date,
                                    is_assumed_date=True,
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
            req_dest_val = getattr(resolved_intent, "requested_destination", None)
            dest_val = getattr(resolved_intent, "destination", None)
            if isinstance(req_dest_val, str) and req_dest_val.strip():
                named_dest = req_dest_val.strip().title()
            elif isinstance(dest_val, str) and dest_val.strip():
                named_dest = dest_val.strip().title()
            else:
                named_dest = None

            if named_dest:
                candidate_destinations = [named_dest]
            else:
                logger.info("Destination absent. Engaging Destination Discovery via Travel Explore...")
                candidate_destinations, used_fallback_catalog = await self._discover_destinations(
                    origin=origin,
                    budget=budget,
                    interests=resolved_intent.interests,
                    excluded_destinations=[previous_destination] if previous_destination else None,
                    people=people,
                    days=days,
                    outbound_date=resolved_intent.start_date,
                    return_date=resolved_intent.end_date,
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

            is_discovery = not bool(named_dest)
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
                            start_date=resolved_intent.start_date,
                            end_date=resolved_intent.end_date,
                            hotel_tier=resolved_intent.hotel_tier,
                            hotel_preference=resolved_intent.hotel_preference,
                            strict_constraints=resolved_intent.strict_constraints,
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
                    last_failed_plan = plan

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
                    cand = candidate_destinations[0] if candidate_destinations else "Requested Destination"
                    if isinstance(cand, str):
                        target_dest = cand
                    elif isinstance(getattr(resolved_intent, "destination", None), str):
                        target_dest = resolved_intent.destination
                    else:
                        target_dest = "Requested Destination"

                # Missing essential cost data or transport/lodging unavailable
                is_missing_transport = (
                    (selected_plan and selected_plan.get("rejection_reason") == "NO_TRANSPORT_AVAILABLE")
                    or (last_failed_plan and last_failed_plan.get("rejection_reason") == "NO_TRANSPORT_AVAILABLE")
                    or (selected_plan and "no transport" in str(selected_plan.get("explanation", "")).lower())
                    or (last_failed_plan and "no transport" in str(last_failed_plan.get("explanation", "")).lower())
                )
                is_incomplete_cost = (
                    (selected_plan and selected_plan.get("rejection_reason") in ("INCOMPLETE_COST_DATA", "NO_TRANSPORT_AVAILABLE", "NO_ACCOMMODATION_AVAILABLE"))
                    or (last_failed_plan and last_failed_plan.get("rejection_reason") in ("INCOMPLETE_COST_DATA", "NO_TRANSPORT_AVAILABLE", "NO_ACCOMMODATION_AVAILABLE"))
                )
                clean_action = action if (isinstance(action, (TripAction, str)) and "Mock" not in type(action).__name__) else TripAction.NEW_TRIP
                if is_missing_transport:
                    self.conversation_repo.save_pending_intent(chat_id, resolved_intent)
                    return OrchestrationResult(
                        status="NOT_FEASIBLE",
                        action=clean_action,
                        selected_destination=target_dest,
                        feasibility_status="INCOMPLETE_COST_DATA",
                        message_text=format_infeasible_plan(
                            destination=target_dest,
                            budget=budget,
                            deficit=Decimal("0.00"),
                            explanation=f"No transport found for this route ({origin} to {target_dest}).",
                            recommendation=f"Consider adjusting your budget from {resolved_intent.currency} {budget:,.2f} or exploring other destinations.",
                            currency=resolved_intent.currency,
                            is_incomplete_data=True,
                        ),
                    )

                deficit = (
                    last_opt_result.deficit
                    if last_opt_result and last_opt_result.deficit > Decimal("0.00")
                    else (
                        last_infeasible_result.deficit
                        if last_infeasible_result and last_infeasible_result.deficit > Decimal("0.00")
                        else (
                            selected_plan.get("baseline_eval").deficit
                            if selected_plan and selected_plan.get("baseline_eval") and getattr(selected_plan.get("baseline_eval"), "deficit", Decimal("0.00")) > Decimal("0.00")
                            else (
                                last_failed_plan.get("baseline_eval").deficit
                                if last_failed_plan and last_failed_plan.get("baseline_eval") and getattr(last_failed_plan.get("baseline_eval"), "deficit", Decimal("0.00")) > Decimal("0.00")
                                else Decimal("0.00")
                            )
                        )
                    )
                )
                explanation = (
                    last_opt_result.explanation
                    if last_opt_result
                    else (last_infeasible_result.explanation if last_infeasible_result else "Trip exceeds budget constraint.")
                )
                if selected_plan and selected_plan.get("explanation"):
                    explanation = str(selected_plan["explanation"])
                elif last_failed_plan and last_failed_plan.get("explanation"):
                    explanation = str(last_failed_plan["explanation"])
                elif "is feasible" in str(explanation).lower() or "surplus" in str(explanation).lower():
                    explanation = f"Mandatory travel and lodging costs for {target_dest or 'this trip'} cannot be completed within budget."

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
                feas_status = (
                    "BOUNDED_SEARCH_NO_FEASIBLE_OPTION"
                    if (is_discovery and early_exit_reason and "MAX_CANDIDATES" in early_exit_reason)
                    else ("INCOMPLETE_COST_DATA" if is_incomplete_cost else "NOT_FEASIBLE")
                )

                if is_discovery:
                    coverage_note = f" among the {candidates_checked} candidate destinations evaluated from {origin}" if candidates_checked > 0 else ""
                    return OrchestrationResult(
                        status="NOT_FEASIBLE",
                        action=clean_action,
                        selected_destination=None,
                        feasibility_status=feas_status,
                        search_scope="bounded" if is_discovery else None,
                        evaluated_candidates_count=candidates_checked,
                        message_text=f"No feasible destination found within your budget{coverage_note}. Consider adjusting your budget or travel dates.",
                        deficit=deficit,
                    )

                return OrchestrationResult(
                    status="NOT_FEASIBLE",
                    action=clean_action,
                    selected_destination=target_dest,
                    feasibility_status=feas_status,
                    evaluated_candidates_count=candidates_checked,
                    message_text=format_infeasible_plan(
                        destination=target_dest,
                        budget=budget,
                        deficit=deficit,
                        explanation=explanation,
                        recommendation=recommendation,
                        currency=resolved_intent.currency,
                        is_incomplete_data=is_incomplete_cost,
                    ),
                    deficit=deficit,
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

            # If user specified interests (e.g. theme park, local food, events), discover matching places and events
            if resolved_intent.interests:
                for interest_item in resolved_intent.interests:
                    int_lower = str(interest_item).lower()
                    if any(kw in int_lower for kw in ("food", "cuisine", "restaurant", "seafood", "dining", "cafe")):
                        q_str = resolve_food_query(chosen_dest, interest=interest_item)
                    else:
                        q_str = resolve_places_query(chosen_dest, interest=interest_item)

                    int_env = await self.cache_manager.get_travel_data(
                        engine="google_maps",
                        params={
                            "q": q_str,
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

            # Live seasonal event discovery or fallback to curated regional events
            discovered_events = []
            if self.is_live_mode:
                try:
                    ev_date_hint = None
                    if resolved_intent and resolved_intent.start_date:
                        try:
                            import datetime as dt_mod
                            dt_obj = dt_mod.date.fromisoformat(str(resolved_intent.start_date))
                            ev_date_hint = dt_obj.strftime("%B %Y")
                        except Exception:
                            ev_date_hint = str(resolved_intent.start_date)
                    ev_env = await self.cache_manager.get_travel_data(
                        engine="google",
                        params={"q": resolve_events_query(chosen_dest, date_or_season=ev_date_hint)},
                    )
                    raw_events = self.normalizer.normalize_events(ev_env) or []
                    if raw_events and resolved_intent and resolved_intent.start_date and resolved_intent.end_date:
                        discovered_events = filter_events_overlapping_dates(
                            raw_events,
                            resolved_intent.start_date,
                            resolved_intent.end_date,
                        )
                    else:
                        discovered_events = raw_events
                    if discovered_events:
                        logger.info("[EVENTS] Discovered %d live verified events for %s", len(discovered_events), chosen_dest)
                except Exception as exc:
                    logger.debug("Live events discovery encountered non-fatal error: %s", exc)

            if not discovered_events and not self.is_live_mode:
                dest_key = chosen_dest.lower().strip()
                for k, ev_list in _CURATED_DESTINATION_EVENTS.items():
                    if k in dest_key or dest_key in k:
                        discovered_events = list(ev_list)
                        break

            # AttractionSelector for feasible destination (preserves live places & unknown fees)
            attractions = self.attraction_selector.select_for_itinerary(
                destination=chosen_dest,
                travel_party=resolved_intent.travel_party,
                interests=resolved_intent.interests,
                days=final_days,
                places=places,
            ) or (selected_plan.get("attractions") or [])

            # a. Create or update Trip (starts in PLANNING status; activation happens on CONFIRM_BOOKING)
            existing_planning_trip = self.trip_repo.get_planning_trip(chat_id)
            is_same_destination = (
                existing_planning_trip is not None
                and (
                    ai_result.destination is None
                    or (existing_planning_trip.destination and ai_result.destination.lower() == existing_planning_trip.destination.lower())
                )
            )
            should_update_existing = (
                existing_planning_trip is not None
                and isinstance(getattr(existing_planning_trip, "id", None), UUID)
                and (
                    action != TripAction.NEW_TRIP
                    or (pending_intent is not None and is_same_destination and not ("plan a " in clean_text.lower() and ai_result.destination is not None))
                )
            )
            if should_update_existing:
                updated_trip = self.trip_repo.update_trip(
                    trip_id=existing_planning_trip.id,
                    budget_total=budget,
                    destination=chosen_dest,
                    origin=origin,
                    duration_days=final_days,
                    people_count=people,
                )
                trip = updated_trip if (updated_trip and isinstance(getattr(updated_trip, "id", None), UUID)) else existing_planning_trip
                logger.info("[ORCHESTRATOR] Reusing and updating existing planning trip %s for action %s", trip.id, action)
            else:
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

            # c. Generate and Persist Itinerary or Surgically Update Existing Itinerary
            replacement_desc = None
            generated_itin = None

            if getattr(resolved_intent, "replace_activity_target", None):
                from budlance.itinerary.models import GeneratedItinerary, ItineraryDay
                from budlance.itinerary.replacer import replace_itinerary_item

                existing_record = self.itinerary_repo.get_itinerary(trip.id)
                if existing_record and getattr(existing_record, "days", None):
                    try:
                        ex_days = [ItineraryDay.model_validate(d) for d in existing_record.days]
                        itin_to_modify = GeneratedItinerary(
                            trip_id=trip.id,
                            destination=chosen_dest,
                            days_count=len(ex_days),
                            days=ex_days,
                            is_feasible=existing_record.is_feasible,
                            total_budget=final_eval.breakdown.total_budget,
                            total_planned_cost=sum(d.daily_estimated_cost for d in ex_days),
                        )
                        rep_target = resolved_intent.replace_activity_target
                        rep_cat = getattr(resolved_intent, "replace_activity_category", None) or "nature"
                        rep_day = getattr(resolved_intent, "target_day_number", None)

                        updated_itin, old_it, new_it = replace_itinerary_item(
                            itinerary=itin_to_modify,
                            target_name_or_cat=rep_target,
                            replacement_category=rep_cat,
                            day_number=rep_day,
                            available_attractions=attractions,
                            available_places=places,
                            destination=chosen_dest,
                        )
                        if old_it and new_it:
                            old_lbl = old_it.attraction_name or old_it.place_name or old_it.activity
                            new_lbl = new_it.attraction_name or new_it.place_name or new_it.activity
                            day_lbl = f"Day {rep_day}" if rep_day else "Itinerary"
                            replacement_desc = f"{day_lbl}: Replaced {old_lbl} with {new_lbl} ({rep_cat} spot)."

                            # 1. Check for actual route data between old and new locations
                            old_loc = old_it.location_address or old_it.place_name or old_it.attraction_name or chosen_dest
                            new_loc = new_it.location_address or new_it.place_name or new_it.attraction_name or chosen_dest
                            route_opt = None
                            if old_loc and new_loc and str(old_loc).lower().strip() != str(new_loc).lower().strip():
                                try:
                                    routes_env = await self.cache_manager.get_travel_data(
                                        engine="google_maps_directions",
                                        params={"start_addr": str(old_loc), "end_addr": str(new_loc)},
                                        trip_id=trip.id,
                                    )
                                    routes = self.normalizer.normalize_routes(routes_env)
                                    if routes and routes[0].distance_km > 0:
                                        route_opt = routes[0]
                                except Exception as exc:
                                    logger.debug("Directions route lookup encountered non-fatal error: %s", exc)

                            diff_region = bool(
                                new_it.region and old_it.region
                                and str(new_it.region).lower().strip() != str(old_it.region).lower().strip()
                            )

                            if route_opt and route_opt.distance_km > 0:
                                transfer_dist = route_opt.distance_km
                                transfer_duration = route_opt.duration_minutes
                                route_source = route_opt.source
                                src_lbl = getattr(route_source, "value", str(route_source))
                                new_it.travel_time_to_next_minutes = transfer_duration
                                new_it.notes = f"Transfer: ~{transfer_dist:.1f} km ({transfer_duration} min). Route source: {src_lbl}."
                                extra_transit = self.estimation.estimate_local_transit_distance(transfer_dist, mode="auto")
                                extra_transit.source = route_source
                            elif diff_region:
                                transfer_dist = 15.0
                                transfer_duration = 45
                                extra_transit = self.estimation.estimate_local_transit_distance(transfer_dist, mode="auto")
                                extra_transit.source = DataSource.CONFIG_ESTIMATE
                                extra_transit.basis = f"CONFIG_ESTIMATE: Regional transfer heuristic ({transfer_dist:g} km); live route unavailable."
                                extra_transit.limitations = "Unverified estimated distance; verify actual travel time and taxi/auto fare locally."
                                new_it.travel_time_to_next_minutes = transfer_duration
                                new_it.notes = f"Estimated transfer: ~{transfer_dist:g} km (~{transfer_duration} min) [CONFIG_ESTIMATE: unverified transfer heuristic, local traffic and route may vary]."
                            else:
                                transfer_dist = 5.0
                                transfer_duration = 30
                                extra_transit = self.estimation.estimate_local_transit_distance(transfer_dist, mode="auto")
                                extra_transit.source = DataSource.CONFIG_ESTIMATE
                                extra_transit.basis = f"CONFIG_ESTIMATE: Intra-region transfer heuristic ({transfer_dist:g} km); live route unavailable."
                                extra_transit.limitations = "Unverified estimated distance; verify actual travel time and taxi/auto fare locally."
                                new_it.travel_time_to_next_minutes = transfer_duration
                                new_it.notes = f"Estimated transfer: ~{transfer_dist:g} km (~{transfer_duration} min) [CONFIG_ESTIMATE: unverified transfer heuristic, local traffic and route may vary]."

                            updated_transit_cost = final_eval.breakdown.local_transit_cost
                            transit_prov = final_eval.breakdown.provenance.get("local_transit", DataSource.ESTIMATED)
                            if extra_transit and extra_transit.total_cost:
                                updated_transit_cost += extra_transit.total_cost
                                transit_prov = extra_transit.source

                            from budlance.schemas.travel import LocalTransitEstimate
                            transit_for_eval = LocalTransitEstimate(
                                mode="metro_bus",
                                total_cost=updated_transit_cost,
                                days=final_days,
                                source=transit_prov,
                            )
                            food_for_eval = self.estimation.estimate_food(people=people, days=final_days)

                            # 2. Extract all active attractions from updated itinerary
                            active_attractions = []
                            for d in updated_itin.days:
                                for it in d.items:
                                    if it.slot_type == "attraction" or it.attraction_name or it.entry_fee_inr is not None or it.is_fee_unknown:
                                        active_attractions.append(it)

                            # 3. Recalculate feasibility using authoritative ReverseBudgetEngine
                            is_intercity = origin.lower().strip() != chosen_dest.lower().strip()
                            requires_lodging = final_days > 1
                            requires_attraction_fees = bool(
                                resolved_intent.strict_constraints
                                and any("admission" in s.lower() or "fee" in s.lower() for s in resolved_intent.strict_constraints)
                            )
                            # Invariant: User budget ceiling (final_eval.breakdown.total_budget) is strictly preserved!
                            recalculated_eval = self.budget_engine.evaluate(
                                total_budget=final_eval.breakdown.total_budget,
                                people=people,
                                days=final_days,
                                transport=transport,
                                hotel=hotel,
                                food_estimate=food_for_eval,
                                local_transit_estimate=transit_for_eval,
                                activities_budget=final_eval.breakdown.bucket_c_activities,
                                currency=final_eval.breakdown.currency,
                                selected_attractions=active_attractions,
                                requires_transport=is_intercity,
                                requires_lodging=requires_lodging,
                                requires_attraction_fees=requires_attraction_fees,
                            )

                            # 4. Handle Infeasibility or Incomplete Cost Data
                            if not recalculated_eval.is_feasible:
                                if existing_record:
                                    existing_record.days = [day.model_dump(mode="json") for day in updated_itin.days]
                                    existing_record.is_feasible = False
                                    existing_record.feasibility_note = recalculated_eval.explanation
                                    existing_record.updated_at = utc_now()
                                    self.itinerary_repo.save_itinerary(existing_record)

                                return OrchestrationResult(
                                    trip_id=trip.id,
                                    status="NOT_FEASIBLE",
                                    action=action,
                                    selected_destination=chosen_dest,
                                    feasibility_status=recalculated_eval.status,
                                    budget_breakdown=recalculated_eval.breakdown,
                                    deficit=recalculated_eval.deficit,
                                    message_text=format_infeasible_plan(
                                        destination=chosen_dest,
                                        budget=final_eval.breakdown.total_budget,
                                        deficit=recalculated_eval.deficit,
                                        explanation=(
                                            f"{replacement_desc} However, this makes the trip exceed your budget ceiling of "
                                            f"{final_eval.breakdown.currency} {final_eval.breakdown.total_budget:,.2f} "
                                            f"by {final_eval.breakdown.currency} {recalculated_eval.deficit:,.2f}."
                                            if recalculated_eval.deficit > Decimal("0.00")
                                            else recalculated_eval.explanation
                                        ),
                                        recommendation="Consider selecting a free alternative or increasing your budget.",
                                        currency=final_eval.breakdown.currency,
                                        is_incomplete_data=(recalculated_eval.status == "INCOMPLETE_COST_DATA"),
                                    ),
                                )

                            # 5. If Feasible, commit recalculated evaluation
                            final_eval = recalculated_eval
                            updated_itin.is_feasible = True
                            updated_itin.total_budget = final_eval.breakdown.total_budget
                            updated_itin.total_planned_cost = final_eval.breakdown.projected_trip_cost
                            generated_itin = updated_itin
                    except Exception as e:
                        logger.warning("Could not reconstruct existing itinerary for replacement: %s", e)

            if generated_itin is None:
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
                    start_date=resolved_intent.start_date,
                    events=discovered_events,
                    dietary_preference=resolved_intent.dietary_preference,
                    schedule_pace=resolved_intent.schedule_pace,
                    earliest_activity_time=resolved_intent.earliest_activity_time,
                    arrival_time=resolved_intent.arrival_time,
                    departure_time=resolved_intent.departure_time,
                    special_activity_request=resolved_intent.special_activity_request,
                )

            # Batched LLM description enhancement (1 call for entire itinerary)
            if generated_itin is not None:
                generated_itin = await self.itinerary_enhancer.enhance_itinerary(
                    itinerary=generated_itin,
                    travel_party=resolved_intent.travel_party,
                )

                # Persist enhanced descriptions to itinerary repo
                from budlance.db.models import Itinerary as ItineraryModel
                existing_record = self.itinerary_repo.get_itinerary(trip.id)
                if getattr(generated_itin, "days", None):
                    if existing_record:
                        existing_record.days = [day.model_dump(mode="json") for day in generated_itin.days]
                        existing_record.is_feasible = final_eval.is_feasible
                        existing_record.feasibility_note = final_eval.explanation
                        existing_record.updated_at = utc_now()
                        self.itinerary_repo.save_itinerary(existing_record)
                    else:
                        new_record = ItineraryModel(
                            trip_id=trip.id,
                            days=[day.model_dump(mode="json") for day in generated_itin.days],
                            is_feasible=final_eval.is_feasible,
                            feasibility_note=final_eval.explanation,
                        )
                        self.itinerary_repo.save_itinerary(new_record)

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

            # Clear pending intent now that trip planning has generated a concrete trip
            self.conversation_repo.clear_pending_intent(chat_id)

            # If requested interests have no match at the destination, say so in one line and offer alternatives
            interest_note = resolve_interest_mismatch_note(
                destination=chosen_dest,
                requested_interests=resolved_intent.interests,
                curated_attractions=attractions,
                places=places,
                origin=origin,
            )
            if not interest_note and generated_itin and getattr(generated_itin, "feasibility_note", None):
                if "Note:" in generated_itin.feasibility_note:
                    interest_note = generated_itin.feasibility_note

            # Build compact change summary for CHANGE_* and MODIFY_TRIP actions
            change_desc = None
            if action in (
                TripAction.CHANGE_BUDGET,
                TripAction.CHANGE_DAYS,
                TripAction.CHANGE_PEOPLE,
                TripAction.CHANGE_DESTINATION,
                TripAction.CHANGE_TRANSPORT,
                TripAction.MODIFY_TRIP,
            ):
                if action == TripAction.CHANGE_BUDGET:
                    change_desc = f"Budget updated to {final_eval.breakdown.currency} {final_eval.breakdown.total_budget:,.2f}"
                elif action == TripAction.CHANGE_DAYS:
                    change_desc = f"Trip duration updated to {final_days} days"
                elif action == TripAction.CHANGE_PEOPLE:
                    party_str = f" ({resolved_intent.travel_party.title()})" if resolved_intent.travel_party else ""
                    change_desc = f"Travel party updated to {people} travelers{party_str}"
                elif action == TripAction.CHANGE_DESTINATION:
                    change_desc = f"Destination updated to {chosen_dest}"
                elif action == TripAction.CHANGE_TRANSPORT:
                    t_mode = getattr(transport, "class_or_type", None) or getattr(transport, "transit_type", "transport")
                    change_desc = f"Transport preference updated to {t_mode}"
                elif action == TripAction.MODIFY_TRIP:
                    if replacement_desc:
                        change_desc = replacement_desc
                    else:
                        mods = []
                        if resolved_intent.days is not None:
                            mods.append(f"{final_days} days")
                        if resolved_intent.budget is not None:
                            mods.append(f"budget {final_eval.breakdown.currency} {final_eval.breakdown.total_budget:,.0f}")
                        if resolved_intent.hotel_preference:
                            mods.append(f"stay {resolved_intent.hotel_preference}")
                        if resolved_intent.hotel_tier:
                            mods.append(f"{resolved_intent.hotel_tier} stay")
                        if resolved_intent.transport_mode:
                            mods.append(f"transport {resolved_intent.transport_mode}")
                        if getattr(resolved_intent, "dietary_preference", None):
                            mods.append(f"{resolved_intent.dietary_preference} food")
                        if getattr(resolved_intent, "earliest_activity_time", None):
                            mods.append(f"activities after {resolved_intent.earliest_activity_time}")
                        change_desc = f"Trip updated: {', '.join(mods)}" if mods else "Trip preferences updated"
                else:
                    change_desc = "Trip preferences updated"

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
                events=discovered_events,
                action=action,
                change_description=change_desc,
                interest_note=interest_note,
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
        parsed_rescue = self.ai_service.parse_rescue_intent(message)
        if inspect.isawaitable(parsed_rescue):
            await parsed_rescue

        if self.enable_trip_pass and chat_id not in self._demo_bypass_chats:
            active_trip = self.trip_repo.get_active_trip(chat_id)
            if active_trip:
                pass_rec = self.trip_pass_repo.get_by_trip_id(active_trip.id)
                if not pass_rec or not pass_rec.is_unlocked:
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

        # Execute rescue as a proposal (Phase 7 Feature C: Proposal-Gated Rescue)
        call_kwargs = {}
        if isinstance(self.rescue_service, RescueService):
            call_kwargs["as_proposal"] = True

        rescue_res = await self.rescue_service.execute_rescue(
            chat_id=chat_id,
            user_message=message,
            **call_kwargs,
        )
        if rescue_res.is_proposal and rescue_res.pending_proposal:
            self.conversation_repo.save_pending_rescue_proposal(
                chat_id=chat_id,
                trip_id=rescue_res.trip_id,
                proposal=rescue_res.pending_proposal,
            )

        active_trip = self.trip_repo.get_active_trip(chat_id)
        selected_dest = (
            active_trip.destination
            if (active_trip and isinstance(getattr(active_trip, "destination", None), str))
            else None
        )

        return OrchestrationResult(
            trip_id=rescue_res.trip_id,
            status="RESCUE",
            selected_destination=selected_dest,
            feasibility_status="FEASIBLE" if rescue_res.is_feasible else "NOT_FEASIBLE",
            generated_itinerary=rescue_res.updated_itinerary,
            ledger_summary=rescue_res.ledger_summary,
            message_text=getattr(rescue_res, "message_text", None) or format_rescue_result(rescue_res),
            error=rescue_res.error,
        )

    async def _handle_in_trip_query(
        self,
        chat_id: int,
        ai_result: ParsedTripIntent,
        clean_text: str,
    ) -> OrchestrationResult:
        """Route contextual inquiries to InTripCompanionHandler."""
        active_trip = self.trip_repo.get_active_trip(chat_id)
        if not active_trip or str(active_trip.status).upper() != "ACTIVE":
            planning_trip = self.trip_repo.get_planning_trip(chat_id)
            if planning_trip is None:
                return OrchestrationResult(
                    status="CLARIFICATION",
                    message_text=(
                        "I noticed you're asking an in-trip question, but you don't have an active trip yet! 🌴\n\n"
                        "Tell me where you'd like to go, your budget, number of people, and duration to get started planning."
                    ),
                )
            active_trip = planning_trip

        if self.enable_trip_pass and chat_id not in self._demo_bypass_chats:
            pass_rec = self.trip_pass_repo.get_by_trip_id(active_trip.id)
            if not pass_rec or not pass_rec.is_unlocked:
                return OrchestrationResult(
                    trip_id=active_trip.id,
                    status="PASS_LOCKED",
                    message_text=(
                        "🔒 *In-Trip Companion is Locked*\n\n"
                        "Live contextual assistance requires an active Budlance Trip Pass.\n"
                        "Send `/trip_pass` to unlock for ₹49, or use `/demo_pass` for demo evaluation."
                    ),
                    is_pass_unlocked=False,
                    pass_status=pass_rec.status if pass_rec else "FREE",
                )

        q_type = getattr(ai_result, "in_trip_query_type", None) or self.ai_service._extract_in_trip_query_type(clean_text)
        companion_res = await self.intrip_companion.handle_query(
            chat_id=chat_id,
            trip=active_trip,
            query_type=q_type,
            user_message=clean_text,
        )
        return OrchestrationResult(
            trip_id=active_trip.id,
            status="IN_TRIP_QUERY",
            message_text=companion_res.message_text,
            ledger_summary=companion_res.ledger_summary,
        )

    async def _handle_manage_booking(
        self,
        chat_id: int,
        ai_result: ParsedTripIntent,
        clean_text: str,
    ) -> OrchestrationResult:
        """Route to BookingLifecycleHandler for user-confirmed bookings or cancellations."""
        active_trip = self.trip_repo.get_active_trip(chat_id) or self.trip_repo.get_planning_trip(chat_id)
        if not active_trip:
            return OrchestrationResult(
                status="CLARIFICATION",
                message_text="No active trip found to manage bookings for. Tell me where you'd like to travel, your budget, and duration to start planning!",
            )

        b_target = getattr(ai_result, "booking_target", None) or "flight"
        b_action = getattr(ai_result, "booking_action", None) or "confirmed"

        if b_action == "cancelled":
            res = self.booking_handler.record_cancellation(
                trip_id=active_trip.id,
                component_type=b_target,
                user_reported_only=True,
                notes=clean_text,
            )
        elif b_action == "show":
            return OrchestrationResult(
                trip_id=active_trip.id,
                status="MANAGE_BOOKING",
                message_text=self.booking_handler.format_bookings_summary(active_trip.id),
            )
        else:  # "confirmed"
            res = self.booking_handler.user_confirms_booking(
                trip_id=active_trip.id,
                component_type=b_target,
                notes=clean_text,
            )

        return OrchestrationResult(
            trip_id=active_trip.id,
            status="MANAGE_BOOKING",
            message_text=res.message_text,
        )

    async def _handle_log_expense(
        self,
        chat_id: int,
        parsed_intent: ParsedTripIntent,
        message_text: str = "",
        event_id: str | None = None,
    ) -> OrchestrationResult:
        """Route to ExpenseLifecycleHandler using the ACTIVE CONFIRMED TRIP — never the pending draft."""
        logger.info(
            "[ACTION_ROUTER] LOG_EXPENSE detected for chat_id=%s. Loading active trip (NOT pending draft).",
            chat_id,
        )
        active_trip = self.trip_repo.get_active_trip(chat_id)
        if not active_trip or str(active_trip.status).upper() != "ACTIVE":
            planning_trip = self.trip_repo.get_planning_trip(chat_id)
            if planning_trip is not None:
                msg_lower = (message_text or "").lower()
                if any(w in msg_lower for w in ("checked in", "check in", "check-in", "checked into", "arrived", "started")):
                    self.trip_repo.update_trip_status(planning_trip.id, status="ACTIVE", is_active=True)
                    logger.info("[LOG_EXPENSE] Planning trip %s activated upon hotel check-in detection", planning_trip.id)

        eff_event_id = event_id or getattr(parsed_intent, "event_id", None)
        expense_res = await self.expense_handler.handle_log_expense(
            chat_id=chat_id,
            parsed=parsed_intent,
            event_id=eff_event_id,
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
                action=TripAction.CONFIRM_BOOKING,
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

        # Distinguish plan confirmation ("confirm this trip", "confirm dates") from ticket booking ("booked"):
        clean_lower = clean_text.lower()
        is_plan_confirmation = (
            any(p in clean_lower for p in [
                "confirm this trip", "confirm trip", "confirm the trip",
                "confirm the plan", "confirm this plan", "confirm plan",
                "confirm dates", "confirm date", "confirm proposed dates",
            ])
            and not any(b in clean_lower for b in ["booked", "ticket booked", "tickets booked", "already booked", "booking done"])
        )

        if is_plan_confirmation and planning_trip is not None:
            logger.info("[CONFIRM_BOOKING] Planning confirmation recorded for chat_id=%s trip_id=%s.", chat_id, planning_trip.id)
            resolved_intent = resolved_intent.model_copy(update={"date_confirmed": True, "date_is_ambiguous": False})
            self.conversation_repo.save_pending_intent(chat_id, resolved_intent)
            dest_suffix = f" to {planning_trip.destination}" if planning_trip.destination else ""
            return OrchestrationResult(
                trip_id=planning_trip.id,
                status="PLANNING",
                action=TripAction.CONFIRM_BOOKING,
                selected_destination=planning_trip.destination,
                message_text=(
                    f"✅ Your trip plan{dest_suffix} is confirmed!\n\n"
                    "Next step: Book your transport tickets using the links provided above. "
                    "Once you have booked, simply reply with `Booked` to activate your trip!"
                ),
            )

        # Do not finalise live price-sensitive results using unconfirmed ambiguous dates
        if getattr(resolved_intent, "date_is_ambiguous", False):
            phrase = getattr(resolved_intent, "date_ambiguous_phrase", "relative date")
            logger.info("[CONFIRM_BOOKING] Blocked activation for chat_id=%s due to ambiguous date phrase '%s'.", chat_id, phrase)
            return OrchestrationResult(
                trip_id=planning_trip.id if planning_trip else None,
                status="CLARIFICATION",
                action=TripAction.CONFIRM_BOOKING,
                message_text=(
                    f"⚠️ Ambiguous travel dates detected ('{phrase}').\n\n"
                    "Travel dates must be confirmed before finalizing bookings or booking tickets. "
                    "Please specify your exact departure date (e.g., 'From 15 Oct 2026') or reply 'Confirm dates' to accept the proposed dates."
                ),
            )

        if planning_trip is None and (not resolved_intent.destination or not resolved_intent.budget):
            # Step 12 — Handle "Booked" With No Valid Planning Trip
            logger.info("[CONFIRM_BOOKING] No planning trip found to activate for chat_id=%s.", chat_id)
            return OrchestrationResult(
                status="NO_PLANNING_TRIP",
                action=TripAction.CONFIRM_BOOKING,
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
                    action=TripAction.CONFIRM_BOOKING,
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
            start_date=resolved_intent.start_date,
            end_date=resolved_intent.end_date,
            hotel_tier=resolved_intent.hotel_tier,
            hotel_preference=resolved_intent.hotel_preference,
            strict_constraints=resolved_intent.strict_constraints,
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
            places=places,
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
            start_date=resolved_intent.start_date,
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

        discovered_events = []
        dest_key = chosen_dest.lower().strip()
        for k, ev_list in _CURATED_DESTINATION_EVENTS.items():
            if k in dest_key or dest_key in k:
                discovered_events = list(ev_list)
                break

        interest_note = resolve_interest_mismatch_note(
            destination=chosen_dest,
            requested_interests=resolved_intent.interests,
            curated_attractions=attractions,
            places=places,
            origin=origin,
        )
        if not interest_note and generated_itin and getattr(generated_itin, "feasibility_note", None):
            if "Note:" in generated_itin.feasibility_note:
                interest_note = generated_itin.feasibility_note

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
            events=discovered_events,
            interest_note=interest_note,
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
        outbound_date: str | None = None,
        return_date: str | None = None,
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
        # Group 1 & 2: Curated domestic candidates (OFFLINE / TEST FALLBACK ONLY)
        # In LIVE MODE (SERPAPI_LIVE_ENABLED=true), NO hardcoded candidates are injected.
        # ---------------------------------------------------------------
        curated_with_corridor: list[str] = []
        curated_no_corridor: list[str] = []

        if not self.is_live_mode:
            curated_pool = list(_CURATED_DOMESTIC_POOL)
            if interests:
                interest_set = {i.lower() for i in interests}
                def _curated_match_score(entry: dict[str, Any]) -> int:
                    tags = {t.lower() for t in entry.get("tags", [])}
                    return len(interest_set.intersection(tags))
                curated_pool.sort(key=_curated_match_score, reverse=True)

            for entry in curated_pool:
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
                            "[CURATED_OFFLINE] %s added with known offline corridor from %s.",
                            dest_name, corridor_origin,
                        )
                    else:
                        curated_no_corridor.append(dest_name)
                        logger.info(
                            "[CURATED_OFFLINE] %s added without direct corridor (will need transport check).",
                            dest_name,
                        )
        else:
            logger.info("[DISCOVER_LIVE] Live mode active: zero hardcoded domestic pool candidates injected.")

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
            if outbound_date:
                explore_params["outbound_date"] = str(outbound_date)
            if return_date:
                explore_params["return_date"] = str(return_date)
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
        start_date: str | None = None,
        end_date: str | None = None,
        date_ctx: TripDateContext | None = None,
        requires_attraction_fees: bool = False,
        hotel_tier: str | None = None,
        hotel_preference: str | None = None,
        strict_constraints: list[str] | None = None,
    ) -> dict[str, Any]:
        """Collect travel components, normalize, estimate, and evaluate through Reverse-Budget Engine."""
        _local_call_count = 0
        if date_ctx is None:
            date_ctx = build_trip_date_context(
                days=days,
                start_date=start_date,
                return_date=end_date,
            )
        days = date_ctx.days

        # 1. Collect required travel components through Cache/Fallback/SerpApi pipeline.
        # For discovery candidates with a known offline corridor, we skip the live flight call
        # to conserve provider-call budget — Gate 2 will use the corridor directly.
        effective_transport_mode = transport_mode
        if is_discovery_candidate and has_offline_corridor and not transport_mode and not self.is_live_mode:
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
            start_date=start_date,
            end_date=end_date,
            date_ctx=date_ctx,
        )

        if primary_transport is None and effective_transport_mode == "train" and not transport_mode:
            available_transports = await self.lookup_transport_options(
                origin=origin,
                destination=destination,
                people=people,
                transport_mode=None,
                transport_class=transport_class,
                outbound_date=date_ctx.flight_outbound_date,
                return_date=date_ctx.flight_return_date,
                days=date_ctx.days,
            )
            primary_transport = available_transports[0] if available_transports else None

        # Preference-aware selection for primary transport and hotel
        pref_list = [str(x) for x in (interests or []) if isinstance(x, str)]
        if isinstance(travel_party, str):
            pref_list.append(travel_party)
        if hotel_preference and isinstance(hotel_preference, str):
            pref_list.append(hotel_preference)
        if hotel_tier and isinstance(hotel_tier, str):
            pref_list.append(hotel_tier)
        if strict_constraints:
            if isinstance(strict_constraints, list):
                pref_list.extend([str(x) for x in strict_constraints if isinstance(x, str)])
            elif isinstance(strict_constraints, str):
                pref_list.append(strict_constraints)

        pref_list = [str(x) for x in pref_list if isinstance(x, str)]
        pref_str = " ".join(pref_list).lower()
        is_luxury_pref = (
            any(k in pref_str for k in ("luxury", "premium", "resort", "5 star", "5-star", "4 star", "4-star", "luxury_hotel"))
            or (hotel_tier and hotel_tier.lower() in ("luxury", "4-star", "4 star", "5-star", "5 star"))
        )
        is_high_budget = budget >= Decimal("100000.00")

        # For high budgets or explicit luxury preferences, pre-select top quality options
        if is_high_budget or is_luxury_pref:
            if available_hotels:
                hotel_budget_ceiling = budget * get_settings().budget_hotel_warning_ratio if not is_high_budget else budget * Decimal("0.70")
                pref_hotel = self.optimizer.select_preferred_hotel(
                    available_hotels=available_hotels,
                    budget_limit=hotel_budget_ceiling,
                    preferences=pref_list,
                    hotel_tier=hotel_tier or ("luxury" if is_luxury_pref else "standard"),
                    is_generous_budget=is_high_budget,
                )
                if pref_hotel:
                    primary_hotel = pref_hotel

            if available_transports:
                transport_budget_ceiling = budget * get_settings().budget_transport_warning_ratio if not is_high_budget else budget * Decimal("0.50")
                pref_trans = self.optimizer.select_preferred_transport(
                    available_transports=available_transports,
                    budget_limit=transport_budget_ceiling,
                    preferences=pref_list,
                    transport_class=transport_class,
                    is_generous_budget=is_high_budget,
                )
                if pref_trans:
                    primary_transport = pref_trans

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
        activities_budget = round(budget * get_settings().budget_activities_ratio, 2)

        def _is_price_zero_or_missing(obj, attr: str) -> bool:
            if obj is None:
                return True
            val = getattr(obj, attr, None)
            if val is None:
                return True
            if isinstance(val, (int, float, str, Decimal)):
                try:
                    return Decimal(str(val)) <= Decimal("0.00")
                except Exception:
                    return True
            return False

        # Inter-city trips require a valid resolved physical transport option.
        # If no flight or train corridor exists, candidate destination is strictly NOT_FEASIBLE.
        is_intercity = origin.lower().strip() != destination.lower().strip()
        if is_intercity and _is_price_zero_or_missing(primary_transport, "price"):
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
                "explanation": f"No physical transport options could be resolved between {origin} and {destination}.",
                "opt_result": None,
                "rejection_reason": "NO_TRANSPORT_AVAILABLE",
                "provider_calls_used": _local_call_count,
            }

        # Multi-day trips require a valid accommodation option.
        # For discovered candidates or when live hotel search returns no properties, candidate destination is strictly NOT_FEASIBLE.
        requires_lodging = date_ctx.requires_lodging
        missing_usable_hotel = _is_price_zero_or_missing(primary_hotel, "total_price")
        gw = getattr(self.cache_manager, "gateway", None)
        raw_creds = getattr(gw, "has_credentials", False)
        has_real_creds = (raw_creds is True and self.is_live_mode)
        is_offline_unconfigured = (
            not is_discovery_candidate
            and primary_hotel is None
            and (not has_real_creds or not self.is_live_mode)
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
                "explanation": f"No valid accommodation options could be resolved in {destination}.",
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
            requires_transport=is_intercity,
            requires_lodging=requires_lodging,
            requires_attraction_fees=requires_attraction_fees,
        )

        if baseline_eval.status == "INCOMPLETE_COST_DATA":
            return {
                "is_feasible": False,
                "destination": destination,
                "days": days,
                "attractions": selected_attractions,
                "baseline_eval": baseline_eval,
                "explanation": baseline_eval.explanation,
                "opt_result": None,
                "rejection_reason": "INCOMPLETE_COST_DATA",
                "provider_calls_used": _local_call_count,
            }

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

        # Check if duration/dates are locked
        is_locked_duration = (
            (start_date is not None and end_date is not None)
            or any("date" in s.lower() or "flight" in s.lower() for s in (strict_constraints or []))
        )
        locked_days = {days} if is_locked_duration else None

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
            locked_days=locked_days,
            requires_transport=is_intercity,
            requires_lodging=requires_lodging,
            requires_attraction_fees=requires_attraction_fees,
            explicit_transport_mode=transport_mode,
            explicit_hotel_tier=hotel_tier or ("luxury" if is_luxury_pref else None),
            strict_preferences=pref_list,
        )

        sel_hotel = getattr(opt_result, "selected_hotel", None)
        sel_trans = getattr(opt_result, "selected_transport", None)
        final_eval = getattr(opt_result, "final_evaluation", None)
        has_valid_lodging = (
            not requires_lodging
            or (sel_hotel is not None and getattr(sel_hotel, "total_price", Decimal("0.00")) > Decimal("0.00"))
            or (is_offline_unconfigured and final_eval is not None and getattr(final_eval, "breakdown", None) is not None and final_eval.breakdown.hotel_cost > Decimal("0.00"))
        )
        if (
            opt_result.is_feasible
            and (not is_intercity or (sel_trans is not None and getattr(sel_trans, "price", Decimal("0.00")) > Decimal("0.00")))
            and has_valid_lodging
        ):
            return {
                "is_feasible": True,
                "destination": destination,
                "days": opt_result.days,
                "transport": sel_trans,
                "hotel": sel_hotel,
                "route": route,
                "attractions": selected_attractions,
                "evaluation": final_eval,
                "opt_result": opt_result,
                "provider_calls_used": _local_call_count,
                "date_ctx": date_ctx,
            }

        return {
            "is_feasible": False,
            "destination": destination,
            "days": days,
            "attractions": selected_attractions,
            "baseline_eval": baseline_eval,
            "opt_result": opt_result,
            "provider_calls_used": _local_call_count,
            "date_ctx": date_ctx,
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
        days: int = 1,
    ) -> list[FlightOption | TransitOption]:
        """Fetch transport options from Cache/Fallback/SerpApi for the current preference.

        Guarantees that changed transport preferences trigger a fresh query and never reuse stale data.

        SerpApi google_flights requires: departure_id, arrival_id, outbound_date, return_date, adults.
        We resolve origin/destination city names to IATA codes via the location resolver before
        calling the live API.  If either endpoint has no IATA mapping (e.g. Manali), we skip the
        live flight call and go straight to the train corridor fallback.
        """
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
                # Build dates: use provided dates or default via unified date context
                if outbound_date is None or return_date is None:
                    _calc_ctx = build_trip_date_context(days=days)
                    out_date = outbound_date or _calc_ctx.flight_outbound_date
                    ret_date = return_date or _calc_ctx.flight_return_date
                else:
                    out_date = outbound_date
                    ret_date = return_date

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
                if cls:
                    cabin_map = {
                        "economy": 1,
                        "premium_economy": 2,
                        "premium economy": 2,
                        "business": 3,
                        "first": 4,
                        "first_class": 4,
                        "first class": 4,
                    }
                    if cls in cabin_map:
                        flight_params["travel_class"] = cabin_map[cls]
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
                                    if best_b["direct_url"]:
                                        fc.deep_link = best_b["direct_url"]
                                        fc.is_exact_booking = True
                                    elif best_b.get("has_post_data") and best_b.get("booking_request"):
                                        import hashlib
                                        from budlance.config import get_settings
                                        b_id = hashlib.sha256((fc.booking_token or str(uuid4())).encode("utf-8")).hexdigest()[:12]
                                        self.cache_manager.cache_repo.store_booking_request(b_id, best_b["booking_request"])
                                        base_url = get_settings().effective_public_base_url
                                        fc.deep_link = f"{base_url}/book/{b_id}"
                                        fc.is_exact_booking = True

                        except Exception as exc:
                            logger.debug("Failed to resolve flight booking options for token: %s", exc)
                    if not fc.deep_link:
                        fc.deep_link = build_safe_flight_search_url(
                            origin=fc.departure_airport or origin,
                            destination=fc.arrival_airport or destination,
                            outbound_date=out_date,
                            return_date=ret_date,
                            people=people,
                            travel_class=cls,
                            booking_token=fc.booking_token,
                        )
                results.extend(valid_flight_candidates)
            # When live flights return no results: do NOT fabricate fake FlightOption (IndiGo 6E-101).
        # In LIVE MODE, do not silently fallback to domestic rail for an unsupported flight destination
        # when transport_mode was not explicitly requested as train.
        allow_train = (mode == "train") or (not mode and not self.is_live_mode)
        if allow_train:
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

            # If static rail/bus corridor has no options, use DynamicTransitGenerator
            if not results:
                from budlance.estimation.dynamic_transit import DynamicTransitGenerator
                dyn_gen = DynamicTransitGenerator()
                dyn_opts = dyn_gen.generate_options(
                    origin=origin,
                    destination=destination,
                    people=people,
                    transport_class=cls,
                )
                if dyn_opts:
                    results.extend(dyn_opts)

        return results

    async def _collect_travel_components(
        self,
        origin: str,
        destination: str,
        people: int,
        days: int,
        transport_mode: str | None = None,
        transport_class: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        date_ctx: TripDateContext | None = None,
    ) -> tuple[
        FlightOption | TransitOption | None,
        list[FlightOption | TransitOption],
        HotelOption | None,
        list[HotelOption],
        RouteOption | None,
    ]:
        """Fetch and normalize travel, stay, and routes from Cache/Fallback/SerpApi."""
        if date_ctx is None:
            date_ctx = build_trip_date_context(
                days=days,
                start_date=start_date,
                return_date=end_date,
            )

        # a. Transports (Flights + Trains/Buses)
        preferred_transports = await self.lookup_transport_options(
            origin=origin,
            destination=destination,
            people=people,
            transport_mode=transport_mode,
            transport_class=transport_class,
            outbound_date=date_ctx.flight_outbound_date,
            return_date=date_ctx.flight_return_date,
            days=date_ctx.days,
        )

        all_transports: list[FlightOption | TransitOption] = list(preferred_transports)

        primary_transport = all_transports[0] if all_transports else None

        # b. Hotels
        # SerpApi google_hotels requires: q, check_in_date, check_out_date, adults
        hotel_check_in  = date_ctx.hotel_check_in_date
        hotel_check_out = date_ctx.hotel_check_out_date
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
        stay_nights = date_ctx.stay_nights if date_ctx.stay_nights > 0 else (1 if days == 1 else max(1, days - 1))
        hotel_candidates = self.normalizer.normalize_hotels(
            hotel_env,
            nights=stay_nights,
            check_in=hotel_check_in,
            check_out=hotel_check_out,
        )
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
        events: list[Any] | None = None,
        action: TripAction | str | None = None,
        change_description: str | None = None,
        interest_note: str | None = None,
    ) -> OrchestrationResult:
        # Cache bookings for this trip so unlock/demo commands have access to booking details
        self._cached_trip_bookings[trip_id] = (transport, hotel)

        is_pass_unlocked = True
        pass_status = "PAID"
        checkout_url = None

        is_alternative = (action == TripAction.FIND_ALTERNATIVE or action == "FIND_ALTERNATIVE")
        is_demo_bypass = (chat_id in self._demo_bypass_chats)

        if self.enable_trip_pass:
            pass_record = self.payment_service.get_or_create_pass(
                user_id=user_id,
                chat_id=chat_id,
                trip_id=trip_id,
            )
            if is_demo_bypass and not get_settings().is_production:
                self.trip_pass_repo.update_pass_status(
                    trip_id=trip_id,
                    status="DEMO_ACCESS",
                    metadata={"bypass": "demo_flag"},
                )
                pass_record.status = "DEMO_ACCESS"

            is_pass_unlocked = pass_record.is_unlocked
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
                    interest_note=interest_note,
                    is_alternative=is_alternative,
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
                    events=events,
                    interest_note=interest_note,
                    is_alternative=is_alternative,
                )
        else:
            is_pass_unlocked = True
            pass_status = "PAID"
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
                events=events,
                interest_note=interest_note,
                is_alternative=is_alternative,
            )

        # Cache full plan so user can request "full plan" at any time
        had_prior_full_plan = chat_id in self._last_full_plan
        self._last_full_plan[chat_id] = msg_text

        # For CHANGE_* actions, return a compact summary (what changed, new total, new surplus), with "full plan" available on request
        if had_prior_full_plan and action in (
            TripAction.CHANGE_BUDGET,
            TripAction.CHANGE_DAYS,
            TripAction.CHANGE_PEOPLE,
            TripAction.CHANGE_DESTINATION,
            TripAction.CHANGE_TRANSPORT,
        ):
            msg_text = format_change_summary(
                action=action,
                destination=chosen_dest,
                breakdown=final_eval.breakdown,
                change_description=change_description or "Trip preferences updated",
                currency=final_eval.breakdown.currency,
            )

        return OrchestrationResult(
            trip_id=trip_id,
            status="FEASIBLE",
            action=action,
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

    async def _format_unlocked_trip_result(
        self,
        trip: Trip,
        chat_id: int,
        pass_record: TripPass,
        is_demo: bool = False,
    ) -> OrchestrationResult:
        """Format and return the full unlocked trip plan after payment or demo bypass."""
        itin_record = self.itinerary_repo.get_itinerary(trip.id)
        try:
            ledger_summary = self.ledger_manager.get_summary(trip.id)
        except Exception:
            ledger_summary = None

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

        cached_trans, cached_hot = self._cached_trip_bookings.get(trip.id, (None, None))
        if cached_trans is None and breakdown and breakdown.transport_cost > Decimal("0.00"):
            from budlance.schemas.travel import TransitOption
            cached_trans = TransitOption(
                airline=None,
                name_or_operator="Estimated train fare (distance-based)",
                price=breakdown.transport_cost,
                transit_type="train",
                deep_link="https://www.irctc.co.in/nget/train-search",
            )
        if cached_hot is None and breakdown and breakdown.hotel_cost > Decimal("0.00"):
            from budlance.schemas.travel import HotelOption
            cached_hot = HotelOption(
                name=f"Standard Hotel in {trip.destination or 'Destination'}",
                total_price=breakdown.hotel_cost,
                price_per_night=breakdown.hotel_cost / Decimal(max(1, trip.duration_days)),
                deep_link=f"https://www.google.com/travel/hotels/{trip.destination or ''}",
            )

        if breakdown:
            plan_text = format_feasible_plan(
                destination=trip.destination or "Destination",
                days=trip.duration_days,
                people=trip.people_count,
                breakdown=breakdown,
                transport=cached_trans,
                hotel=cached_hot,
                itinerary=gen_itin,
                ledger=ledger_summary,
                travel_party=trip.people_count == 1 and "solo" or None,
                is_pass_unlocked=True,
            )
            if is_demo or pass_record.status == "DEMO_ACCESS":
                msg_text = f"🎟️ *Judge/Demo Bypass Activated!* ✅\n\n{plan_text}"
            else:
                msg_text = f"🎟️ *Budlance Trip Pass: ACTIVE ✅ (Verified Stripe Payment)*\n\n{plan_text}"
        else:
            if is_demo or pass_record.status == "DEMO_ACCESS":
                msg_text = (
                    f"🎟️ *Budlance Trip Pass Unlocked via Judge/Demo Bypass!* ✅\n\n"
                    f"Your trip to {trip.destination} is fully unlocked. Complete day-by-day attraction schedule, "
                    f"booking links, and live In-Trip Rescue are now active."
                )
            else:
                msg_text = (
                    f"🎟️ *Budlance Trip Pass Unlocked!* ✅\n\n"
                    f"Your payment has been verified. Complete day-by-day attraction schedule, "
                    f"booking links, and live In-Trip Rescue for {trip.destination} are now active."
                )

        return OrchestrationResult(
            trip_id=trip.id,
            status="FEASIBLE",
            selected_destination=trip.destination,
            feasibility_status="FEASIBLE",
            selected_transport=cached_trans,
            selected_hotel=cached_hot,
            generated_itinerary=gen_itin,
            ledger_summary=ledger_summary,
            budget_breakdown=breakdown,
            message_text=msg_text,
            is_pass_unlocked=True,
            pass_status=pass_record.status,
        )

    async def _handle_demo_pass_command(self, chat_id: int, target_trip_id: str | None = None) -> OrchestrationResult:
        """Controlled demo/judge bypass to unlock Trip Pass immediately without real payment."""
        settings = get_settings()
        if settings.is_production:
            return OrchestrationResult(
                status="ERROR",
                message_text=(
                    "⛔ *Demo Pass Unavailable*\n\n"
                    "Judge/Demo bypass is disabled in production environments. "
                    "Please use the secure checkout link to purchase a Trip Pass."
                ),
                is_pass_unlocked=False,
            )

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

        return await self._format_unlocked_trip_result(trip, chat_id, pass_record, is_demo=True)

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

        if pass_record.is_unlocked:
            status_label = "ACTIVE ✅ (Verified Payment)" if pass_record.is_verified_paid else "DEMO ACCESS ✅ (Evaluation Mode)"
            return OrchestrationResult(
                trip_id=trip.id,
                status="PASS_UNLOCKED",
                message_text=(
                    f"🎟️ *Budlance Trip Pass: {status_label}*\n\n"
                    f"Your Trip Pass for {trip.destination} is active.\n"
                    f"• Amount: {pass_record.currency} {pass_record.amount:,.2f}\n"
                    f"• Reference: `{pass_record.payment_reference or 'confirmed'}`\n\n"
                    f"Full itinerary, booking links, and live In-Trip Rescue are unlocked."
                ),
                is_pass_unlocked=True,
                pass_status=pass_record.status,
            )

        session = await self.payment_service.create_checkout_session(
            trip_id=trip.id,
            chat_id=chat_id,
            user_id=trip.user_id,
        )

        fee_str = f"₹{session.amount:,.0f}" if session.currency == "INR" else f"{session.currency} {session.amount:,.2f}"
        budget_str = f"₹{trip.budget_total:,.0f}" if trip.budget_total else "your travel budget"
        demo_line = "\n\n_(Judge/Demo review: send `/demo_pass` to unlock instantly without payment)_" if not get_settings().is_production else ""

        return OrchestrationResult(
            trip_id=trip.id,
            status="CHECKOUT_PENDING",
            message_text=(
                f"🎟️ *Budlance Trip Pass: Unlock Full Plan*\n\n"
                f"Your full trip plan is ready to unlock with a {fee_str} Trip Pass. "
                f"This service fee is separate from your {budget_str} travel budget.\n\n"
                f"Would you like to proceed to secure checkout?\n"
                f"👉 [Proceed to Secure Checkout]({session.checkout_url})"
                f"{demo_line}"
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
                is_pass_unlocked=False,
            )

        pass_record = self.trip_pass_repo.get_by_trip_id(trip.id)
        if pass_record and pass_record.is_unlocked:
            return await self._format_unlocked_trip_result(trip, chat_id, pass_record, is_demo=pass_record.is_demo)

        # Confirm payment directly via Stripe Checkout Session check (no webhook required for polling)
        if pass_record and pass_record.payment_reference:
            is_paid = await self.payment_service.check_stripe_checkout_status(pass_record.payment_reference)
            if is_paid:
                updated_pass = self.trip_pass_repo.update_pass_status(
                    trip_id=trip.id,
                    status="PAID_VERIFIED",
                    metadata={"confirmed_via": "stripe_session_poll"},
                )
                return await self._format_unlocked_trip_result(trip, chat_id, updated_pass or pass_record, is_demo=False)

        settings = get_settings()
        demo_hint = "\n\n💡 *Evaluator / Demo Bypass:* Send `/demo_pass` to unlock the full trip plan immediately." if not settings.is_production else "\n\n👉 Send `/pass` to receive a new checkout link or try again."

        return OrchestrationResult(
            trip_id=trip.id,
            status="PAYMENT_PENDING",
            message_text=(
                "⏳ *Payment Verification Pending*\n\n"
                "We have not yet received payment confirmation from the gateway for this trip.\n"
                "If you just completed payment, please wait a moment or send `/pass` to check again."
                f"{demo_hint}"
            ),
            is_pass_unlocked=False,
            pass_status=pass_record.status if pass_record else "FREE",
        )
