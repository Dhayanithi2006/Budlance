"""In-Trip AI travel companion handling contextual inquiries during active travel.

Supported requests:
- "What is planned for today?"
- "What is the next activity?"
- "Find somewhere good to eat near my current location."
- "Show me the route to the next place."
- "How much of my budget is left?"
- "Can I afford another activity?"
- "I will be travelling to the airport in two hours. Help me plan."

Boundary Rules:
1. Uses the confirmed active trip, itinerary, and virtual ledger.
2. Never hallucinates live GPS, background tracking, or real-time traffic;
   explicitly states the limitation when location-based queries are asked.
3. Safe itinerary proposals: a proposal is NOT an applied change.
"""

from decimal import Decimal
import logging
import re
from typing import Any
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field

from budlance.cache.manager import CacheFallbackManager
from budlance.db.models import Itinerary, Trip
from budlance.db.repositories.intent_repo import IntentRepository
from budlance.db.repositories.itinerary_repo import ItineraryRepository
from budlance.db.repositories.trip_repo import TripRepository
from budlance.itinerary.models import ItineraryDay, ItineraryItem
from budlance.ledger.manager import VirtualLedgerManager
from budlance.ledger.models import LedgerSummary
from budlance.normalization.normalizer import DataNormalizer

logger = logging.getLogger(__name__)


def format_currency_amount(amount: Decimal) -> str:
    """Format decimal amount cleanly."""
    if amount == int(amount):
        return f"{int(amount):,}"
    return f"{amount:,.2f}"


class CompanionResponse(BaseModel):
    """Result of an in-trip companion query."""
    model_config = ConfigDict(from_attributes=True)

    trip_id: UUID
    query_type: str
    message_text: str
    ledger_summary: LedgerSummary | None = None


class InTripCompanionHandler:
    """Contextual companion answering live travel questions using persisted trip data."""

    def __init__(
        self,
        trip_repo: TripRepository | None = None,
        itinerary_repo: ItineraryRepository | None = None,
        intent_repo: IntentRepository | None = None,
        ledger_manager: VirtualLedgerManager | None = None,
        cache_manager: CacheFallbackManager | None = None,
        normalizer: DataNormalizer | None = None,
    ) -> None:
        self.trip_repo = trip_repo or TripRepository()
        self.itinerary_repo = itinerary_repo or ItineraryRepository()
        self.intent_repo = intent_repo or IntentRepository()
        self.ledger_manager = ledger_manager or VirtualLedgerManager()
        self.cache_manager = cache_manager or CacheFallbackManager()
        self.normalizer = normalizer or DataNormalizer()

    async def handle_query(
        self,
        chat_id: int,
        trip: Trip,
        query_type: str,
        user_message: str = "",
    ) -> CompanionResponse:
        """Dispatch query to dedicated companion handler."""
        q = (query_type or "general").lower()
        if q == "today":
            return self._handle_today(trip)
        if q == "next":
            return self._handle_next_activity(trip)
        if q == "budget":
            return self._handle_budget_query(trip, user_message)
        if q == "food":
            return await self._handle_food_query(trip, user_message)
        if q == "route":
            return self._handle_route_query(trip)
        if q == "transport_transit":
            return self._handle_transit_query(trip, user_message)
        if q == "movie":
            return await self._handle_movie_query(trip, user_message)

        return self._handle_general(trip)

    def _handle_today(self, trip: Trip) -> CompanionResponse:
        """Answer 'What is planned for today?' using current active day in itinerary with dynamic date synchronization."""
        itin_record = self.itinerary_repo.get_itinerary(trip.id)
        current_day_num = getattr(trip, "current_day", 1) or 1

        if not itin_record or not itin_record.days:
            return CompanionResponse(
                trip_id=trip.id,
                query_type="today",
                message_text=f"Your trip to {trip.destination} is active (Day {current_day_num} of {trip.duration_days}), but no detailed schedule is recorded.",
            )

        days = [ItineraryDay.model_validate(d) for d in itin_record.days]

        # Dynamic date synchronization
        import datetime
        from datetime import timezone, timedelta
        ist = timezone(timedelta(hours=5, minutes=30))
        today_date = datetime.datetime.now(ist).date()
        today_iso = today_date.strftime("%Y-%m-%d")

        # 1. Match exact date_str on an itinerary day
        matched_day = next((d for d in days if getattr(d, "date_str", None) == today_iso), None)
        if matched_day:
            current_day_num = matched_day.day_number
        else:
            intent = (
                self.intent_repo.get_trip_intent(trip.id)
                if hasattr(self, "intent_repo") and self.intent_repo and hasattr(self.intent_repo, "get_trip_intent")
                else (self.intent_repo.get_intent_by_trip(trip.id) if hasattr(self, "intent_repo") and self.intent_repo and hasattr(self.intent_repo, "get_intent_by_trip") else None)
            )
            start_date_val = getattr(intent, "start_date", None) if intent else None
            if not start_date_val and hasattr(trip, "start_date"):
                start_date_val = getattr(trip, "start_date", None)
            if start_date_val:
                try:
                    if isinstance(start_date_val, str):
                        s_dt = datetime.datetime.strptime(start_date_val.strip(), "%Y-%m-%d").date()
                    elif isinstance(start_date_val, datetime.date):
                        s_dt = start_date_val
                    else:
                        s_dt = None
                    if s_dt:
                        day_offset = (today_date - s_dt).days + 1
                        if 1 <= day_offset <= trip.duration_days:
                            current_day_num = day_offset
                except Exception:
                    pass

        day_schedule = next((d for d in days if d.day_number == current_day_num), None)
        date_label = f" ({day_schedule.date_str})" if day_schedule and getattr(day_schedule, "date_str", None) else ""

        if not day_schedule or not day_schedule.items:
            return CompanionResponse(
                trip_id=trip.id,
                query_type="today",
                message_text=f"🗓️ *Day {current_day_num}{date_label} in {trip.destination}:* Free exploration day. No fixed scheduled activities.",
            )

        lines = [f"🗓️ *Plan for Day {current_day_num}{date_label} in {trip.destination}:*"]
        for it in day_schedule.items:
            time_str = f"[{it.time_slot}] " if it.time_slot else ""
            cost_str = f" (Est. ₹{format_currency_amount(it.planned_cost)})" if it.planned_cost > 0 else ""
            lines.append(f"• {time_str}*{it.place_name}*: {it.activity}{cost_str}")

        hotel_rec = getattr(day_schedule, "hotel_recommendation", None)
        if hotel_rec:
            lines.append(f"\n🏨 *Stay:* {hotel_rec}")

        return CompanionResponse(
            trip_id=trip.id,
            query_type="today",
            message_text="\n".join(lines),
        )

    def _handle_next_activity(self, trip: Trip) -> CompanionResponse:
        """Answer 'What is the next activity?'."""
        itin_record = self.itinerary_repo.get_itinerary(trip.id)
        if not itin_record or not itin_record.days:
            return CompanionResponse(
                trip_id=trip.id,
                query_type="next",
                message_text="No scheduled upcoming activities found.",
            )

        days = [ItineraryDay.model_validate(d) for d in itin_record.days]
        current_day = next((d for d in days if d.day_number == trip.current_day), None)
        if not current_day or not current_day.items:
            return CompanionResponse(
                trip_id=trip.id,
                query_type="next",
                message_text=f"No further scheduled activities for Day {trip.current_day}. Enjoy your free time in {trip.destination}!",
            )

        # Select first activity or upcoming activity
        next_item = current_day.items[0]
        cost_str = f" (Est. ₹{format_currency_amount(next_item.planned_cost)})" if next_item.planned_cost > 0 else " (Free admission)"
        msg = (
            f"📍 *Next Scheduled Activity on Day {trip.current_day}:*\n\n"
            f"• *{next_item.place_name}*\n"
            f"• Activity: {next_item.activity}\n"
            f"• Timing: {next_item.time_slot or 'Scheduled today'}\n"
            f"• Cost: {cost_str}"
        )
        return CompanionResponse(
            trip_id=trip.id,
            query_type="next",
            message_text=msg,
        )

    def _handle_budget_query(self, trip: Trip, user_message: str) -> CompanionResponse:
        """Answer budget checks, remaining funds, and affordability queries."""
        summary = self.ledger_manager.get_summary(trip.id)

        # Check if user asked whether they can afford a specific amount e.g. "Can I afford ₹1,200?"
        amt_match = re.search(r"(?:₹|rs\.?|inr)?\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)", user_message.lower())
        asked_amt = None
        if amt_match:
            try:
                candidate = Decimal(amt_match.group(1).replace(",", ""))
                if candidate > Decimal("10"):  # reasonable activity price
                    asked_amt = candidate
            except Exception:
                pass

        unspent = summary.unspent_balance
        reserve = summary.unspent_reserve

        lines = [
            f"💰 *Budget Status for {trip.destination}:*",
            f"• *Total Budget:* ₹{format_currency_amount(trip.budget_total)}",
            f"• *Total Spent So Far:* ₹{format_currency_amount(summary.total_spent)}",
            f"• *Remaining Unspent Balance:* ₹{format_currency_amount(unspent)}",
            f"• *Emergency Reserve Available:* ₹{format_currency_amount(reserve)}",
        ]

        if asked_amt is not None:
            formatted_asked = format_currency_amount(asked_amt)
            if unspent >= asked_amt:
                lines.append(f"\n✅ *Yes!* You can comfortably afford this activity (₹{formatted_asked}). You have ₹{format_currency_amount(unspent)} remaining.")
            elif (unspent + reserve) >= asked_amt:
                lines.append(f"\n⚠️ *Caution:* The activity costs ₹{formatted_asked}, but your general balance has only ₹{format_currency_amount(unspent)}. Covering it would dip into your emergency reserve (₹{format_currency_amount(reserve)} available).")
            else:
                lines.append(f"\n❌ *Budget Alert:* The activity costs ₹{formatted_asked}, but your total remaining funds (including reserve) are only ₹{format_currency_amount(unspent + reserve)}.")

        return CompanionResponse(
            trip_id=trip.id,
            query_type="budget",
            message_text="\n".join(lines),
            ledger_summary=summary,
        )

    async def _handle_food_query(self, trip: Trip, user_message: str) -> CompanionResponse:
        """Answer 'Find somewhere good to eat near my location'."""
        dest = trip.destination or "Destination"
        env = await self.cache_manager.get_travel_data(
            engine="google_maps",
            params={
                "q": f"best restaurants cafes in {dest}",
                "location": dest,
                "type": "search",
            },
            trip_id=trip.id,
        )
        places = self.normalizer.normalize_places(env) or []
        food_places = [p for p in places if p.category in ("restaurant", "cafe", "food") or any(w in p.name.lower() for w in ("cafe", "restaurant", "kitchen", "diner", "bistro"))]

        selected = food_places[:3] if food_places else places[:3]

        lines = [
            f"🍽️ *Dining Suggestions in {dest}:*",
        ]
        if selected:
            for p in selected:
                rating_str = f" ⭐ {p.rating}" if p.rating else ""
                lines.append(f"• *{p.name}*{rating_str} — {p.category or 'Local cuisine'}")
        else:
            lines.append(f"• Try local cafes and beachside shacks in {dest}.")

        lines.append(
            "\nℹ️ *Note:* Budlance operates without background GPS tracking. "
            "Suggestions are based on top-rated dining spots in your trip destination."
        )

        return CompanionResponse(
            trip_id=trip.id,
            query_type="food",
            message_text="\n".join(lines),
        )

    def _handle_route_query(self, trip: Trip) -> CompanionResponse:
        """Answer route questions, disclosing lack of real-time GPS traffic."""
        dest = trip.destination or "Destination"
        msg = (
            f"🗺️ *Route & Local Transit Guidance for {dest}:*\n\n"
            f"• *Recommended transit:* Auto-rickshaw, app cab, or local bus.\n"
            f"• *Estimated transit time:* 20–35 minutes between central attractions.\n\n"
            f"ℹ️ *Note:* Budlance is a travel planner and does not provide live turn-by-turn GPS or real-time traffic updates. "
            f"We recommend using Google Maps or a local transit app for real-time turn directions."
        )
        return CompanionResponse(
            trip_id=trip.id,
            query_type="route",
            message_text=msg,
        )

    def _handle_transit_query(self, trip: Trip, user_message: str) -> CompanionResponse:
        """Answer airport / transfer planning requests."""
        dest = trip.destination or "Destination"
        msg = (
            f"✈️ *Transfer Guidance for {dest} Airport:*\n\n"
            f"• *Departure timing:* Aim to leave for the airport at least 2.5–3 hours before your scheduled flight.\n"
            f"• *Transit options:* Pre-paid airport taxi or verified cab ride.\n"
            f"• *Budget estimate:* Typically ₹500–₹900 depending on pickup distance.\n\n"
            f"Safe travels on your journey!"
        )
    async def _handle_movie_query(self, trip: Trip, user_message: str) -> CompanionResponse:
        """Answer movie night / showtimes lookup with real local cinemas or honest fallback."""
        dest = trip.destination or "Destination"
        clean_dest = dest.split(",")[0].strip()

        cinemas = []
        try:
            envelope = await self.cache_manager.get_travel_data(
                engine="google_maps",
                params={
                    "q": f"cinemas movie theatres in {clean_dest}",
                    "location": clean_dest,
                },
                trip_id=trip.id,
            )
            candidates = self.normalizer.normalize_places(envelope)
            cinemas = [
                c for c in candidates
                if c.name and any(kw in c.name.lower() or kw in (c.category or "").lower() for kw in ("cinema", "theatre", "theater", "movies", "multiplex", "talkies", "film"))
            ]
        except Exception as exc:
            logger.debug("[COMPANION] Error searching cinemas for %s: %s", clean_dest, exc)

        dest_slug = re.sub(r"[^a-zA-Z0-9]+", "-", clean_dest.lower()).strip("-")
        bms_url = f"https://in.bookmyshow.com/explore/movies-{dest_slug}"

        if cinemas:
            lines = [f"🎬 *Movie Night in {clean_dest}:*"]
            lines.append("Here are local cinema options discovered nearby:\n")
            for c in cinemas[:3]:
                rating_str = f" (⭐ {c.rating:.1f})" if getattr(c, "rating", None) else ""
                loc_str = getattr(c, "address", None) or clean_dest
                lines.append(f"• *{c.name}*{rating_str} — {loc_str}")
            lines.append(f"\n🎟️ *Check live showtimes & book tickets:* {bms_url}")
            return CompanionResponse(
                trip_id=trip.id,
                query_type="movie",
                message_text="\n".join(lines),
            )

        # Honest fallback (e.g. hill stations like Munnar without active commercial multiplexes)
        lines = [
            f"🎬 *Movie Night in {clean_dest}:*",
            f"No commercial movie theatres or multiplex showtimes found in {clean_dest}.",
            f"",
            f"💡 *Local Recommendation:* Enjoy an evening bonfire, acoustic music, or stargazing session at your resort, or relax with a streaming movie over hotel Wi-Fi!",
            f"🎟️ *Regional Showtimes:* If you are heading towards a major hub, check Kerala/regional showtimes on BookMyShow: https://in.bookmyshow.com/explore/movies-kochi",
        ]
        return CompanionResponse(
            trip_id=trip.id,
            query_type="movie",
            message_text="\n".join(lines),
        )

    def _handle_general(self, trip: Trip) -> CompanionResponse:
        """General active trip status summary."""
        msg = (
            f"✈️ *Active Trip to {trip.destination}:*\n\n"
            f"• Day: Day {trip.current_day} of {trip.duration_days}\n"
            f"• Budget: ₹{format_currency_amount(trip.budget_total)}\n\n"
            f"You can ask me:\n"
            f"• `What is planned for today?`\n"
            f"• `What is the next activity?`\n"
            f"• `Find somewhere good to eat`\n"
            f"• `How much budget is left?`\n"
            f"• `Spent ₹500 on dinner` to record an expense."
        )
        return CompanionResponse(
            trip_id=trip.id,
            query_type="general",
            message_text=msg,
        )
