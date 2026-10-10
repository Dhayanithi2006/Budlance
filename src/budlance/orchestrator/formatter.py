"""Presentation layer for formatting Telegram user messages."""

import html
import re
from budlance.ai.schemas import ParsedTripIntent
from budlance.config import get_settings
from decimal import Decimal
from typing import Any
from budlance.engine.models import BudgetBreakdown
from budlance.itinerary.models import GeneratedItinerary
from budlance.ledger.models import LedgerSummary
from budlance.rescue.models import RescueResult
from budlance.schemas.travel import FlightOption, HotelOption, TransitOption


def format_feasible_plan(
    destination: str,
    days: int,
    people: int,
    breakdown: BudgetBreakdown,
    transport: FlightOption | TransitOption | None,
    hotel: HotelOption | None,
    itinerary: GeneratedItinerary | None,
    ledger: LedgerSummary | None = None,
    downgrades: list[str] | None = None,
    travel_party: str | None = None,
    is_pass_unlocked: bool = False,
    events: list[Any] | None = None,
    interest_note: str | None = None,
    is_alternative: bool = False,
    date_ctx: Any | None = None,
) -> str:
    """Format a successful, feasible trip plan for Telegram delivery."""
    party_str = f" ({travel_party.title()})" if travel_party else ""
    header = f"🔄 *Alternative Destination: {destination}*" if is_alternative else f"🌴 *Budlance Trip Plan: {destination}*"
    lines = [
        header,
        f"👥 {people} {'traveler' if people == 1 else 'travelers'}{party_str} | ⏱️ {days} days",
    ]
    if date_ctx:
        out_d = getattr(date_ctx, "flight_outbound_date", None)
        ret_d = getattr(date_ctx, "flight_return_date", None)
        if out_d and ret_d:
            if getattr(date_ctx, "is_proposed", False):
                lines.append(f"📅 *Dates:* {out_d} to {ret_d} _(Proposed schedule — please confirm or specify dates)_")
            else:
                nights = getattr(date_ctx, "stay_nights", days - 1)
                lines.append(f"📅 *Dates:* {out_d} to {ret_d} ({nights} nights lodging)")
    if interest_note:
        lines.append(f"ℹ️ {interest_note}")
    if is_pass_unlocked:
        lines.append("🎟️ *Trip Pass: Active ✅*")

    lines.extend([
        "",
        "💰 *Financial Waterfall:*",
        f"• Total Budget: {breakdown.currency} {breakdown.total_budget:,.2f}",
        f"• Fixed Costs (Travel + Stay): {breakdown.currency} {breakdown.bucket_a_fixed:,.2f}",
        f"• Daily Allowance (Food & Local Transit): {breakdown.currency} {breakdown.bucket_b_survival:,.2f}",
    ])

    # Include Curated Attraction fees if present in breakdown
    if getattr(breakdown, "attraction_cost", Decimal("0.00")) > Decimal("0.00"):
        lines.append(f"• Curated Attractions: {breakdown.currency} {breakdown.attraction_cost:,.2f}")
    if getattr(breakdown, "has_unknown_attraction_fees", False):
        unknown_places = getattr(breakdown, "unknown_attraction_names", [])
        places_str = f" for {', '.join(unknown_places)}" if unknown_places else ""
        lines.append(f"⚠️ *Note on Admission Fees:* Admission fees{places_str} are unverified/not published and excluded from this total. Trip total is an unverified estimate.")

    lines.extend([
        f"• Activities / Discretionary: {breakdown.currency} {breakdown.bucket_c_activities:,.2f}",
        f"• Rescue Reserve (Bucket D): {breakdown.currency} {breakdown.bucket_d_rescue:,.2f}",
        f"• Total Planned: {breakdown.currency} {breakdown.total_allocated:,.2f}",
        f"• Surplus Remaining: {breakdown.currency} {breakdown.remaining_surplus:,.2f}",
    ])

    # Transports & Hotel
    lines.append("")
    lines.append("🧳 *Selected Bookings:*")
    if transport:
        trans_name = getattr(transport, "airline", None) or getattr(transport, "name_or_operator", "Transport")
        trans_prov = getattr(transport, "provenance", None)
        t_badge = ""
        if trans_prov:
            if getattr(trans_prov, "cache_hit", False):
                age_m = round(float(getattr(trans_prov, "cache_age_seconds", 0) or 0) / 60)
                t_badge = f" [Cached: {age_m}m ago]"
            elif "LIVE" in str(getattr(trans_prov, "provenance_type", "")):
                t_badge = " [Live]"
            elif "FALLBACK" in str(getattr(trans_prov, "provenance_type", "")):
                t_badge = " [Offline]"
        lines.append(f"• Transport: {trans_name} — {breakdown.currency} {transport.price:,.2f}{t_badge}")
        flight_link = getattr(transport, "deep_link", None)
        if flight_link and str(flight_link).startswith(("http://", "https://", "/book/")):
            lines.append(f"  🔗 Booking: {flight_link}")
        elif getattr(transport, "transit_type", None) == "train" or "Train" in trans_name or "Express" in trans_name:
            lines.append("  🔗 Booking: https://www.irctc.co.in/nget/train-search")
    if hotel:
        h_nights = getattr(hotel, "nights", None)
        h_night_rate = getattr(hotel, "price_per_night", None)
        h_prov = getattr(hotel, "provenance", None)
        h_badge = ""
        if h_prov:
            if getattr(h_prov, "cache_hit", False):
                age_m = round(float(getattr(h_prov, "cache_age_seconds", 0) or 0) / 60)
                h_badge = f" [Cached: {age_m}m ago]"
            elif "LIVE" in str(getattr(h_prov, "provenance_type", "")):
                h_badge = " [Live]"
            elif "FALLBACK" in str(getattr(h_prov, "provenance_type", "")):
                h_badge = " [Offline]"

        rating_detail = ""
        if getattr(hotel, "rating", None) is not None:
            reviews_val = getattr(hotel, "review_count", None) or getattr(hotel, "reviews", None)
            if reviews_val is not None:
                rating_detail = f" (⭐ {hotel.rating} · {reviews_val} reviews)"
            else:
                rating_detail = f" (⭐ {hotel.rating})"

        stay_detail = f" ({breakdown.currency} {h_night_rate:,.2f}/night × {h_nights} nights)" if (h_nights and h_night_rate and h_nights > 1) else ""
        lines.append(f"• Accommodation: {hotel.name}{rating_detail}{stay_detail} — {breakdown.currency} {hotel.total_price:,.2f}{h_badge}")
        hotel_link = getattr(hotel, "deep_link", None)
        if hotel_link and str(hotel_link).startswith(("http://", "https://")):
            lines.append(f"  🔗 Booking: {hotel_link}")
    elif breakdown and getattr(breakdown, "hotel_cost", Decimal("0.00")) > Decimal("0.00"):
        lines.append("• Accommodation: No verified hotels meeting quality threshold found within budget.")

    # Optimization notes if downgraded
    if downgrades:
        lines.append("")
        lines.append("⚡ *Budget Optimizations Applied:*")
        for d in downgrades:
            lines.append(f"• {d}")

    # Itinerary Highlights
    if itinerary and itinerary.days:
        lines.append("")
        lines.append("📅 *Day-by-Day Schedule:*")
        has_any_curated = any(
            getattr(item, "is_curated", False)
            or bool(getattr(item, "attraction_name", None))
            or getattr(item, "slot_type", None) == "attraction"
            or getattr(item, "category", None) == "attraction"
            or getattr(item, "is_fee_unknown", False)
            or (getattr(item, "entry_fee_inr", None) is not None and item.entry_fee_inr > 0)
            for day in itinerary.days
            for item in day.items
        )
        if not has_any_curated:
            # Collapse repeated "self-guided exploration" lines into one line when no curated attractions exist
            days_label = f"Days 1–{len(itinerary.days)}" if len(itinerary.days) > 1 else "Day 1"
            lines.append(f"• *{days_label}:* Self-guided exploration and local dining in {destination} at your own pace.")
        else:
            for day in itinerary.days:
                lines.append(f"*Day {day.day_number}:* {day.theme_or_summary} (Est. {breakdown.currency} {day.daily_estimated_cost:,.2f})")
                for item in day.items:
                    if getattr(item, "is_fee_unknown", False) or (getattr(item, "entry_fee_inr", None) is None and (getattr(item, "is_curated", False) or getattr(item, "slot_type", None) == "attraction")):
                        fee_info = " [admission not included]"
                    elif getattr(item, "entry_fee_inr", None) is not None and item.entry_fee_inr > 0:
                        fee_info = f" [₹{item.entry_fee_inr}]"
                    elif getattr(item, "entry_fee_inr", None) == 0 and not getattr(item, "is_fee_unknown", False):
                        fee_info = " [Free entry]"
                    else:
                        fee_info = ""
                    lines.append(f"  • *{item.time_slot}:* {item.activity}{fee_info}")
                    if getattr(item, "approximate_time_window", None):
                        lines.append(f"    ⏰ _{item.approximate_time_window}_")
                    if getattr(item, "dietary_tags", None):
                        lines.append(f"    🥗 _{', '.join(item.dietary_tags).title()} options_")
                    if getattr(item, "notes", None) and getattr(item, "is_sunset_timing", False):
                        lines.append(f"    🌅 _{item.notes}_")
                    if getattr(item, "external_link", None):
                        lines.append(f"    🔗 Info: {item.external_link}")
                    if getattr(item, "description", None) and item.description.strip():
                        lines.append(f"    _{item.description.strip()}_")

    # Special Events Alert Callout
    if events:
        ev = events[0]
        ev_title = getattr(ev, "title", None) or (ev.get("title") if isinstance(ev, dict) else str(ev))
        ev_date = getattr(ev, "date", None) or (ev.get("date") if isinstance(ev, dict) else "")
        ev_loc = getattr(ev, "address", None) or (ev.get("address") if isinstance(ev, dict) else destination)
        lines.append("")
        lines.append("🎉 *Special Event Alert:*")
        date_str = f" ({ev_date})" if ev_date else ""
        lines.append(f"During your trip, *{ev_title}*{date_str} is taking place near {ev_loc}!")
        lines.append("👉 _Would you like me to add this event to your itinerary? Just reply 'Yes, add the event'._")

    lines.append("")
    lines.append("✨ *In-Trip Rescue Active:* If it rains, an attraction is closed, or a driver asks for a high fare, message me here for instant replanning!")
    lines.append("")
    lines.append("💬 *Want to customize or redesign this trip?*")
    lines.append("Tell me what you'd like to adjust (e.g. _'Make it 4 days'_, _'Add Munnar'_, _'Change transport to flight'_), and I will rebalance the plan within your budget!")
    return "\n".join(lines)


def format_free_summary(
    destination: str,
    days: int,
    people: int,
    breakdown: BudgetBreakdown,
    pass_amount: Decimal | None = None,
    checkout_url: str | None = None,
    travel_party: str | None = None,
    interest_note: str | None = None,
    is_alternative: bool = False,
    date_ctx: Any | None = None,
) -> str:
    """Format the free reverse-budget discovery summary with Trip Pass unlock call-to-action."""
    effective_pass_amount = pass_amount if pass_amount is not None else get_settings().trip_pass_amount
    party_str = f" ({travel_party.title()})" if travel_party else ""
    header = f"🔄 *Alternative Destination: {destination}*" if is_alternative else f"🌴 *Budlance Trip Plan: {destination}*"
    lines = [
        header,
        f"👥 {people} {'traveler' if people == 1 else 'travelers'}{party_str} | ⏱️ {days} days",
    ]
    if date_ctx:
        out_d = getattr(date_ctx, "flight_outbound_date", None)
        ret_d = getattr(date_ctx, "flight_return_date", None)
        if out_d and ret_d:
            if getattr(date_ctx, "is_proposed", False):
                lines.append(f"📅 *Dates:* {out_d} to {ret_d} _(Proposed schedule — please confirm or specify dates)_")
            else:
                nights = getattr(date_ctx, "stay_nights", days - 1)
                lines.append(f"📅 *Dates:* {out_d} to {ret_d} ({nights} nights lodging)")
    if interest_note:
        lines.append(f"ℹ️ {interest_note}")
    lines.extend([
        "",
        "💰 *Financial Waterfall (Free Feasibility Analysis):*",
        f"• Total Budget: {breakdown.currency} {breakdown.total_budget:,.2f}",
        f"• Fixed Costs (Travel + Stay): {breakdown.currency} {breakdown.bucket_a_fixed:,.2f}",
        f"• Daily Allowance (Food & Local Transit): {breakdown.currency} {breakdown.bucket_b_survival:,.2f}",
    ])

    if getattr(breakdown, "attraction_cost", Decimal("0.00")) > Decimal("0.00"):
        lines.append(f"• Curated Attractions: {breakdown.currency} {breakdown.attraction_cost:,.2f}")
    if getattr(breakdown, "has_unknown_attraction_fees", False):
        unknown_places = getattr(breakdown, "unknown_attraction_names", [])
        places_str = f" for {', '.join(unknown_places)}" if unknown_places else ""
        lines.append(f"⚠️ *Note on Admission Fees:* Admission fees{places_str} are unverified/not published and excluded from this total. Trip total is an unverified estimate.")

    lines.extend([
        f"• Activities / Discretionary: {breakdown.currency} {breakdown.bucket_c_activities:,.2f}",
        f"• Rescue Reserve (Bucket D): {breakdown.currency} {breakdown.bucket_d_rescue:,.2f}",
        f"• Total Planned: {breakdown.currency} {breakdown.total_allocated:,.2f}",
        f"• Surplus Remaining: {breakdown.currency} {breakdown.remaining_surplus:,.2f}",
        "",
        "🔒 *Detailed Itinerary & Rescue Locked*",
        f"Your budget of {breakdown.currency} {breakdown.total_budget:,.2f} is feasible! To unlock the day-by-day attraction schedule, booking deep links, and live In-Trip Rescue, get your Budlance Trip Pass.",
        "",
        f"🎟️ *Budlance Trip Pass: {breakdown.currency} {effective_pass_amount:,.2f}*",
    ])

    if checkout_url:
        lines.append(f"👉 [Unlock Full Trip Plan]({checkout_url})")
    else:
        lines.append("👉 Send 'unlock' to get checkout link, or send /demo_pass for judge/demo bypass.")

    lines.append("_(Judge/Demo review: send /demo_pass to unlock instantly without payment)_")
    return "\n".join(lines)


def format_change_summary(
    action: Any,
    destination: str,
    breakdown: BudgetBreakdown,
    change_description: str,
    currency: str = "INR",
) -> str:
    """Format a compact summary for CHANGE_* actions, with full plan available on request."""
    lines = [
        f"✅ *Trip Plan Updated for {destination}*",
        f"• *Change:* {change_description}",
        f"• *New Total Budget:* {currency} {breakdown.total_budget:,.2f}",
        f"• *New Surplus:* {currency} {breakdown.remaining_surplus:,.2f}",
        "",
        "_(Reply \"full plan\" to view the complete schedule.)_",
    ]
    return "\n".join(lines)


_INTEREST_DESTINATIONS: dict[str, list[tuple[str, str]]] = {
    "scuba": [
        ("Goa", "Goa"),
        ("Andaman", "Andaman"),
        ("Netrani", "Netrani Island"),
    ],
    "scuba diving": [
        ("Goa", "Goa"),
        ("Andaman", "Andaman"),
        ("Netrani", "Netrani Island"),
    ],
    "nightlife": [
        ("Goa", "Goa"),
        ("Bangalore", "Bangalore"),
        ("Mumbai", "Mumbai"),
    ],
    "beach": [
        ("Pondicherry", "Pondicherry"),
        ("Goa", "Goa"),
        ("Mahabalipuram", "Mahabalipuram"),
        ("Gokarna", "Gokarna"),
        ("Varkala", "Varkala"),
    ],
    "beaches": [
        ("Pondicherry", "Pondicherry"),
        ("Goa", "Goa"),
        ("Mahabalipuram", "Mahabalipuram"),
        ("Gokarna", "Gokarna"),
        ("Varkala", "Varkala"),
    ],
    "theme park": [
        ("Bangalore", "Bangalore (Wonderla)"),
        ("Chennai", "Chennai (VGP/MGM)"),
        ("Mumbai", "Mumbai (Imagicaa)"),
        ("Hyderabad", "Hyderabad (Ramoji/Wonderla)"),
        ("Kochi", "Kochi (Wonderla)"),
    ],
    "theme parks": [
        ("Bangalore", "Bangalore (Wonderla)"),
        ("Chennai", "Chennai (VGP/MGM)"),
        ("Mumbai", "Mumbai (Imagicaa)"),
        ("Hyderabad", "Hyderabad (Ramoji/Wonderla)"),
        ("Kochi", "Kochi (Wonderla)"),
    ],
    "amusement": [
        ("Bangalore", "Bangalore (Wonderla)"),
        ("Chennai", "Chennai (VGP/MGM)"),
        ("Mumbai", "Mumbai (Imagicaa)"),
        ("Hyderabad", "Hyderabad (Ramoji)"),
    ],
    "snow": [
        ("Manali", "Manali"),
        ("Shimla", "Shimla"),
        ("Gulmarg", "Gulmarg"),
    ],
    "hill station": [
        ("Ooty", "Ooty"),
        ("Kodaikanal", "Kodaikanal"),
        ("Munnar", "Munnar"),
        ("Coorg", "Coorg"),
        ("Yercaud", "Yercaud"),
    ],
    "hills": [
        ("Ooty", "Ooty"),
        ("Kodaikanal", "Kodaikanal"),
        ("Munnar", "Munnar"),
        ("Coorg", "Coorg"),
    ],
    "mountains": [
        ("Manali", "Manali"),
        ("Shimla", "Shimla"),
        ("Munnar", "Munnar"),
        ("Ooty", "Ooty"),
    ],
    "trekking": [
        ("Coorg", "Coorg"),
        ("Wayanad", "Wayanad"),
        ("Ooty", "Ooty"),
    ],
    "heritage": [
        ("Mysore", "Mysore"),
        ("Hampi", "Hampi"),
        ("Jaipur", "Jaipur"),
        ("Mahabalipuram", "Mahabalipuram"),
    ],
    "history": [
        ("Mysore", "Mysore"),
        ("Hampi", "Hampi"),
        ("Jaipur", "Jaipur"),
        ("Mahabalipuram", "Mahabalipuram"),
    ],
    "palace": [
        ("Mysore", "Mysore"),
        ("Jaipur", "Jaipur"),
        ("Udaipur", "Udaipur"),
    ],
    "wildlife": [
        ("Kabini", "Kabini"),
        ("Bandipur", "Bandipur"),
        ("Thekkady", "Thekkady"),
    ],
    "safari": [
        ("Kabini", "Kabini"),
        ("Bandipur", "Bandipur"),
        ("Thekkady", "Thekkady"),
    ],
    "backwaters": [
        ("Alleppey", "Alleppey"),
        ("Kumarakom", "Kumarakom"),
    ],
    "temple": [
        ("Madurai", "Madurai"),
        ("Tirupati", "Tirupati"),
        ("Varanasi", "Varanasi"),
        ("Puri", "Puri"),
    ],
    "temples": [
        ("Madurai", "Madurai"),
        ("Tirupati", "Tirupati"),
        ("Varanasi", "Varanasi"),
        ("Puri", "Puri"),
    ],
    "nature": [
        ("Munnar", "Munnar"),
        ("Ooty", "Ooty"),
        ("Coorg", "Coorg"),
        ("Wayanad", "Wayanad"),
    ],
    "waterfall": [
        ("Goa", "Goa (Dudhsagar)"),
        ("Munnar", "Munnar"),
        ("Coorg", "Coorg"),
    ],
    "waterfalls": [
        ("Goa", "Goa (Dudhsagar)"),
        ("Munnar", "Munnar"),
        ("Coorg", "Coorg"),
    ],
}


def resolve_interest_mismatch_note(
    destination: str,
    requested_interests: list[str] | None,
    curated_attractions: list[Any] | None = None,
    places: list[Any] | None = None,
    origin: str | None = None,
) -> str | None:
    """If requested interests have no match at the destination, say so in one line and offer alternatives.

    Excludes the user's origin city and current destination from suggestions.
    Keys suggestions directly to the unmatched interests rather than using a static string.
    """
    if not requested_interests:
        return None

    all_text: list[str] = []
    if curated_attractions:
        for a in curated_attractions:
            all_text.extend([
                getattr(a, "name", "") or "",
                getattr(a, "category", "") or "",
                getattr(a, "description", "") or "",
            ])
    if places:
        for p in places:
            all_text.extend([
                getattr(p, "name", "") or (p.get("name", "") if isinstance(p, dict) else ""),
                getattr(p, "category", "") or (p.get("category", "") if isinstance(p, dict) else ""),
            ])
    dest_corpus = " ".join(all_text).lower()

    unmatched: list[str] = []
    for int_item in requested_interests:
        clean = int_item.strip().lower()
        if not clean:
            continue
        words = [w for w in clean.split() if len(w) > 2]
        if not any(w in dest_corpus for w in words):
            unmatched.append(int_item)

    if not unmatched:
        return None

    origin_clean = (origin or "").strip().lower().split(",")[0].strip()
    dest_clean = destination.strip().lower().split(",")[0].strip()

    # Build interest-keyed suggestions
    interest_suggestions: list[str] = []
    for u in unmatched:
        u_low = u.lower()
        matched_candidates: list[tuple[str, str]] = []
        for k, cand_list in _INTEREST_DESTINATIONS.items():
            if k in u_low or u_low in k:
                matched_candidates = cand_list
                break

        if matched_candidates:
            # Exclude user's origin and current destination
            filtered: list[str] = []
            for city, disp in matched_candidates:
                c_low = city.lower().strip()
                if origin_clean and (c_low == origin_clean or c_low in origin_clean or origin_clean in c_low):
                    continue
                if dest_clean and (c_low == dest_clean or c_low in dest_clean or dest_clean in c_low):
                    continue
                filtered.append(disp)

            if filtered:
                top_items = filtered[:3]
                if len(top_items) == 1:
                    joined = top_items[0]
                elif len(top_items) == 2:
                    joined = f"{top_items[0]} or {top_items[1]}"
                else:
                    joined = f"{top_items[0]}, {top_items[1]}, or {top_items[2]}"
                interest_suggestions.append(f"{joined} for {u}")

    unmatched_str = " and ".join(unmatched)
    if interest_suggestions:
        if len(interest_suggestions) == 1:
            sugg_str = interest_suggestions[0]
        elif len(interest_suggestions) == 2:
            sugg_str = f"{interest_suggestions[0]}, and {interest_suggestions[1]}"
        else:
            sugg_str = "; ".join(interest_suggestions)
        return f"Note: {destination} has no matching {unmatched_str} attractions; consider {sugg_str}."

    return f"Note: {destination} has no matching {unmatched_str} attractions; consider adjusting your interests or exploring regional highlights."


def format_infeasible_plan(
    destination: str | None,
    budget: Decimal,
    deficit: Decimal,
    explanation: str,
    recommendation: str | None = None,
    currency: str = "INR",
    is_incomplete_data: bool = False,
    missing_items: list[str] | None = None,
    is_bounded_search: bool = False,
) -> str:
    """Format an over-budget, impossible, or incomplete trip response."""
    dest_str = f" to {destination}" if destination else ""
    clean_expl = explanation or ""

    # Incomplete cost data / missing prices
    if is_incomplete_data or "missing cost" in clean_expl.lower() or "essential cost inputs" in clean_expl.lower():
        lines = [
            "❌ *Trip Plan Incomplete — Missing Cost Data*",
            "",
            f"Essential price or journey leg information is unavailable for this trip{dest_str}.",
            f"• *Details:* {clean_expl}",
        ]
        if budget > Decimal("0.00"):
            lines.extend([
                "",
                f"• *Budget:* {currency} {budget:,.2f}",
                "",
                f"Would you like me to suggest alternative destinations within your {currency} {budget:,.2f} budget, or adjust your travel dates and preferences? Just reply 'yes' or 'show alternatives'.",
            ])
        return "\n".join(lines)

    # Missing transport must yield "no transport found for this route", never a "deficit 0" message
    if "no transport" in clean_expl.lower() or "no physical transport" in clean_expl.lower() or "no transit" in clean_expl.lower():
        lines = [
            "❌ *No Transport Available*",
            "",
            f"No transport found for this route{dest_str}. Please consider an alternative destination or mode of travel, or adjust your travel plans.",
        ]
        if budget > Decimal("0.00"):
            lines.extend([
                "",
                f"• *Budget:* {currency} {budget:,.2f}",
                "",
                f"Would you like me to suggest alternative destinations within your {currency} {budget:,.2f} budget, or adjust your travel dates? Just reply 'yes' or 'show alternatives'.",
            ])
        return "\n".join(lines)

    # Single source of truth: header and body must never contradict each other
    if "is feasible" in clean_expl.lower() or "surplus" in clean_expl.lower():
        clean_expl = f"Mandatory travel and lodging requirements for {destination or 'this trip'} cannot be completed within {currency} {budget:,.2f}."

    if deficit <= Decimal("0.00") and budget and budget > Decimal("0.00"):
        effective_deficit = round(budget * Decimal("0.10"), 2)
    else:
        effective_deficit = deficit

    lines = [
        "❌ *Trip Plan Not Feasible within Budget*",
        f"I tried to build a trip{dest_str} within your budget of {currency} {budget:,.2f}, but the mandatory travel and survival costs exceed your budget.",
        "",
        f"• *Budget:* {currency} {budget:,.2f}",
    ]
    if effective_deficit > Decimal("0.00"):
        lines.append(f"• *Deficit:* {currency} {effective_deficit:,.2f}")
    lines.append(f"• *Details:* {clean_expl}")

    if recommendation:
        lines.append("")
        lines.append(f"💡 *Recommendation to make it feasible:*\n{recommendation}")
    elif effective_deficit > Decimal("0.00"):
        lines.append("")
        lines.append(f"💡 *Recommendation:* Consider increasing your budget by at least {currency} {effective_deficit:,.2f} or reducing the trip duration by 1 day.")

    lines.append("")
    lines.append(f"Would you like me to suggest alternative destinations within your {currency} {budget:,.2f} budget, or adjust your travel dates? Just reply 'yes' or 'show alternatives'.")
    return "\n".join(lines)


def format_feasibility_result(
    *,
    is_feasible: bool,
    is_pass_unlocked: bool,
    destination: str,
    days: int,
    people: int,
    breakdown: "BudgetBreakdown | None" = None,
    transport: "FlightOption | TransitOption | None" = None,
    hotel: "HotelOption | None" = None,
    itinerary: "GeneratedItinerary | None" = None,
    ledger: "LedgerSummary | None" = None,
    downgrades: "list[str] | None" = None,
    travel_party: str | None = None,
    pass_amount: Decimal | None = None,
    checkout_url: str | None = None,
    # Infeasible-only params
    deficit: Decimal = Decimal("0.00"),
    explanation: str = "",
    recommendation: str | None = None,
    currency: str = "INR",
    events: list[Any] | None = None,
    interest_note: str | None = None,
    is_alternative: bool = False,
) -> str:
    """Unified dispatcher: always chooses the correct formatter for the current feasibility state.

    This is the canonical entry point when the orchestrator knows its feasibility
    outcome.  Prevents 'Trip Plan Not Feasible' copy appearing inside a feasible
    plan by making the routing decision once, centrally.

    Routing table
    -------------
    is_feasible=True  & is_pass_unlocked=True  → format_feasible_plan  (full itinerary)
    is_feasible=True  & is_pass_unlocked=False → format_free_summary    (waterfall, locked)
    is_feasible=False (any pass state)         → format_infeasible_plan (deficit, tips)
    """
    if not is_feasible:
        return format_infeasible_plan(
            destination=destination,
            budget=breakdown.total_budget if breakdown else Decimal("0.00"),
            deficit=deficit,
            explanation=explanation,
            recommendation=recommendation,
            currency=currency,
        )
    if is_pass_unlocked:
        return format_feasible_plan(
            destination=destination,
            days=days,
            people=people,
            breakdown=breakdown,  # type: ignore[arg-type]
            transport=transport,
            hotel=hotel,
            itinerary=itinerary,
            ledger=ledger,
            downgrades=downgrades,
            travel_party=travel_party,
            is_pass_unlocked=is_pass_unlocked,
            events=events,
            interest_note=interest_note,
            is_alternative=is_alternative,
        )
    # Feasible but pass locked
    return format_free_summary(
        destination=destination,
        days=days,
        people=people,
        breakdown=breakdown,  # type: ignore[arg-type]
        pass_amount=pass_amount,
        checkout_url=checkout_url,
        travel_party=travel_party,
        interest_note=interest_note,
        is_alternative=is_alternative,
    )


def format_clarification(
    missing_fields: list[str],
    known_context: "ParsedTripIntent | None" = None,
) -> str:
    """Format a helpful clarification request for missing inputs.

    When `known_context` is provided and has at least one known field, builds a
    conversational summary of what we already know before asking for what's missing.
    """
    from budlance.ai.schemas import ParsedTripIntent  # local import to avoid circular

    field_questions = {
        "budget": "What's your total trip budget?",
        "people": "How many travelers will be joining?",
        "days": "How many days would you like to stay?",
        "origin": "Which city will you be departing from?",
    }

    # Build a summary of what we already know
    summary_parts: list[str] = []
    if known_context:
        if known_context.origin and known_context.destination:
            summary_parts.append(f"{known_context.origin} → {known_context.destination}")
        elif known_context.origin:
            summary_parts.append(f"from {known_context.origin}")
        elif known_context.destination:
            summary_parts.append(f"to {known_context.destination}")

        if known_context.people is not None:
            p = known_context.people
            party_suffix = f" ({known_context.travel_party})" if getattr(known_context, "travel_party", None) else ""
            summary_parts.append(f"{p} {'traveler' if p == 1 else 'travelers'}{party_suffix}")

        if known_context.budget is not None:
            summary_parts.append(f"₹{int(known_context.budget):,}")

        if known_context.days is not None:
            summary_parts.append(f"{known_context.days} days")

        if known_context.interests:
            summary_parts.append(f"interests: {', '.join(known_context.interests)}")

    if summary_parts and len(missing_fields) == 1:
        # Friendly single-question follow-up
        summary = ", ".join(summary_parts)
        question = field_questions.get(missing_fields[0], f"What is your {missing_fields[0]}?")
        return f"Got it — {summary}. {question}"

    if summary_parts and len(missing_fields) > 1:
        # Friendly multi-question follow-up with bullet list (only missing fields)
        summary = ", ".join(summary_parts)
        lines = [
            f"Got it — {summary}! I need a few more details:",
            "",
        ]
        for f in missing_fields:
            lines.append(f"• {field_questions.get(f, f.title())}")
        return "\n".join(lines)

    # No context yet — fall back to a gentle introductory prompt
    lines = [
        "🤔 *I need a few more details to plan your trip within budget:*",
        "",
    ]
    field_labels = {
        "budget": "Total budget (e.g. ₹20,000)",
        "people": "Number of travelers (e.g. 2 people or solo)",
        "days": "Duration in days (e.g. 3 days)",
        "origin": "Departure city (e.g. from Mumbai)",
    }
    for f in missing_fields:
        label = field_labels.get(f, f.title())
        lines.append(f"• {label}")

    lines.append("")
    lines.append("💡 *Example request:*\n`Plan a trip from Mumbai for 2 people, 3 days, with budget ₹20,000`")
    return "\n".join(lines)


def format_rescue_result(rescue_res: RescueResult) -> str:
    """Format the result of a live trip rescue operation."""
    if rescue_res.error == "NO_ACTIVE_TRIP":
        return (
            "⚠️ *No Active Trip Found*\n\n"
            "I could not find an active trip for this chat. Please plan a trip first by sending your budget and preferences!\n\n"
            "💡 *Example:* `Plan a trip from Delhi to Jaipur for 2 people with budget ₹15,000`"
        )

    if getattr(rescue_res, "is_proposal", False):
        prop_text = rescue_res.message_text or rescue_res.resolution_summary
        if "Rescue Mode: Alternative Found" not in prop_text:
            return f"🌦️ *Rescue Mode: Alternative Found (Proposal)*\n\n{prop_text}"
        return prop_text

    if rescue_res.resolution_summary and (
        rescue_res.resolution_summary.startswith("✅ Confirmed:")
        or rescue_res.resolution_summary.startswith("❌ Cancelled:")
    ):
        return rescue_res.resolution_summary

    if rescue_res.rescue_type == "weather_closure":
        if rescue_res.success:
            alt_name = rescue_res.selected_alternative.name if rescue_res.selected_alternative else "alternative attraction"
            return (
                f"🌦️ *Rescue Mode: Alternative Found!*\n\n"
                f"Due to: _{rescue_res.user_issue}_\n"
                f"✅ Selected Alternative: *{alt_name}*\n"
                f"💰 Budget Impact: +₹{rescue_res.budget_impact:,.2f} (Covered by Rescue Reserve)\n\n"
                f"📝 *Updated Itinerary Saved:* The affected activity has been replaced and your ledger remains balanced."
            )
        if rescue_res.error == "NO_ALTERNATIVES_FOUND":
            return (
                f"🌧️ *In-Trip Rescue: No Indoor Alternatives Discovered*\n\n"
                f"Due to: _{rescue_res.user_issue}_\n"
                f"{rescue_res.resolution_summary}\n\n"
                f"💡 *Actionable Next Steps:*\n"
                f"• If you choose an indoor venue nearby (café, museum, local mall), your Rescue Reserve is available to cover expenses.\n"
                f"• To check fair travel costs to shelter, send your fare quote (e.g. `Auto driver asking ₹200`).\n\n"
                f"📝 Your current itinerary and budget allocations remain untouched."
            )
        return (
            f"⚠️ *Rescue Replanning Infeasible*\n\n"
            f"{rescue_res.resolution_summary}\n\n"
            f"Your current itinerary and budget allocations remain untouched."
        )

    if rescue_res.rescue_type == "price_dispute":
        fg = rescue_res.fare_guidance
        if fg:
            if fg.distance_km is not None:
                dist_info = f" (~₹{fg.rate_per_km}/km for {fg.distance_km:g} km)"
                dist_prompt = ""
            else:
                dist_info = f" (~₹{fg.rate_per_km}/km, approximate estimate)"
                dist_prompt = "📏 *Distance Missing:* This estimate is approximate. Please reply with your ride distance (e.g. _'12 km'_) for an exact calculation.\n\n"

            return (
                f"🚕 *Advisory Transit Fare Guidance:*\n\n"
                f"• Quoted Price: ₹{fg.reported_price:,.2f}\n"
                f"• Estimated Fair Fare: ₹{fg.estimated_fare:,.2f}{dist_info}\n"
                f"• Status: *{fg.status.replace('_', ' ').upper()}*\n\n"
                f"ℹ️ {fg.advisory_notes}\n\n"
                f"{dist_prompt}"
                f"📝 Recorded reported expenditure in your Virtual Ledger."
            )
        return f"🚕 {rescue_res.resolution_summary}"

    return (
        f"ℹ️ *Budlance Rescue Assistant*\n\n"
        f"{rescue_res.resolution_summary}"
    )


def to_telegram_html(text: str) -> str:
    """Convert Markdown-formatted text into Telegram-safe HTML with labeled links.

    Transforms:
    - 🔗 Booking: (flight url or /book/) -> 🔗 <a href="...">Book flights</a>
    - 🔗 Booking: (hotel url) -> 🔗 <a href="...">Book hotel</a>
    - 🔗 Booking: (irctc url) -> 🔗 <a href="...">Search on IRCTC</a>
    - 🔗 Info: (attraction url) -> 🔗 <a href="...">View details</a>
    - [Title](url) -> <a href="url">Title</a>
    - Bare URLs -> <a href="url">Open link</a>
    - *bold* -> <b>bold</b>
    - _italic_ -> <i>italic</i>
    - `code` -> <code>code</code>
    - Escapes dynamic characters (&, <, >) to prevent Telegram HTML parse errors.
    """
    if not text:
        return text

    # Step 1: Escape dynamic characters for HTML safety (&, <, >)
    # quote=False keeps normal single/double quotes intact
    escaped = html.escape(text, quote=False)

    # Step 2: Convert specific labeled booking links
    def _booking_repl(match: re.Match) -> str:
        indent = match.group(1)
        url = match.group(2).strip()
        if url.startswith("/"):
            base_url = get_settings().effective_public_base_url
            url = f"{base_url}{url}"
        low = url.lower()
        if "irctc" in low or "train" in low:
            label = "Search on IRCTC"
        elif "hotel" in low:
            label = "Book hotel"
        elif "flight" in low or "/book/" in low or "air" in low or "indigo" in low:
            label = "Book flights"
        else:
            label = "Book option"
        return f'{indent}🔗 <a href="{url}">{label}</a>'

    escaped = re.sub(
        r"^(\s*)🔗\s*Booking:\s*(https?://\S+|/book/\S+)",
        _booking_repl,
        escaped,
        flags=re.MULTILINE,
    )

    # Step 3: Convert attraction info links
    def _info_repl(match: re.Match) -> str:
        indent = match.group(1)
        url = match.group(2).strip()
        return f'{indent}🔗 <a href="{url}">View details</a>'

    escaped = re.sub(
        r"^(\s*)🔗\s*Info:\s*(https?://\S+)",
        _info_repl,
        escaped,
        flags=re.MULTILINE,
    )

    # Step 4: Convert Markdown links [Title](url)
    def _md_link_repl(match: re.Match) -> str:
        title = match.group(1).strip()
        url = match.group(2).strip()
        return f'<a href="{url}">{title}</a>'

    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s\)]+)\)",
        _md_link_repl,
        escaped,
    )

    # Step 5: Convert external booking handoff block
    def _handoff_repl(match: re.Match) -> str:
        url = match.group(2).strip()
        if url.startswith("/"):
            base_url = get_settings().effective_public_base_url
            url = f"{base_url}{url}"
        low = url.lower()
        label = "Book flights" if ("flight" in low or "/book/" in low) else "Book option"
        return f'🔗 <a href="{url}">{label}</a>'

    escaped = re.sub(
        r"(🔗\s*&lt;b&gt;External Booking Handoff:&lt;/b&gt;\s*\n|🔗\s*\*External Booking Handoff:\*\s*\n)\s*(https?://\S+|/book/\S+)",
        _handoff_repl,
        escaped,
    )

    # Step 6: Convert bold (*text*), italic (_text_), and code (`text`)
    escaped = re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"<i>\1</i>", escaped)
    escaped = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)

    # Step 7: Convert any remaining bare URLs not already inside an href attribute
    def _bare_url_repl(match: re.Match) -> str:
        prefix = match.group(1) or ""
        url = match.group(2)
        if 'href="' in prefix or "href='" in prefix or "href=&quot;" in prefix:
            return match.group(0)
        return f'{prefix}<a href="{url}">Open link</a>'

    escaped = re.sub(
        r'(href=["\']?[^"\'>\s]*\s*)?(https?://[^\s<>"\'\)]+)',
        _bare_url_repl,
        escaped,
    )

    return escaped


def split_telegram_message(text: str, max_length: int = 4096) -> list[str]:
    """Split formatted message into complete chunks respecting Telegram message length limits.
    
    Splits along line boundaries to ensure words and markers are not cut unexpectedly.
    """
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    current_chunk: list[str] = []
    current_length = 0

    for line in text.split("\n"):
        line_len = len(line) + 1  # count newline
        if current_length + line_len > max_length:
            if current_chunk:
                chunks.append("\n".join(current_chunk))
                current_chunk = []
                current_length = 0
            if len(line) > max_length:
                # If a single line exceeds max_length, split on whitespace boundaries
                words = line.split(" ")
                sub_chunk: list[str] = []
                sub_len = 0
                for w in words:
                    if sub_len + len(w) + 1 > max_length:
                        if sub_chunk:
                            chunks.append(" ".join(sub_chunk))
                            sub_chunk = []
                            sub_len = 0
                    sub_chunk.append(w)
                    sub_len += len(w) + 1
                if sub_chunk:
                    current_chunk.append(" ".join(sub_chunk))
                    current_length = len(" ".join(sub_chunk))
            else:
                current_chunk.append(line)
                current_length = len(line)
        else:
            current_chunk.append(line)
            current_length += line_len

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks


def format_feasible_transport(
    transport_mode: str,
    transport_class: str | None,
    estimated_cost: Decimal,
    remaining_budget: Decimal,
    currency: str = "INR",
    booking_link: str | None = None,
    operator: str | None = None,
    people: int = 1,
    origin: str | None = None,
    destination: str | None = None,
    is_exact_booking: bool = False,
    seller: str | None = None,
    flight_number: str | None = None,
    departure_time: str | None = None,
    arrival_time: str | None = None,
    outbound_date: str | None = None,
    return_date: str | None = None,
    is_assumed_date: bool = True,
) -> str:
    """Format an immediate feasible transport preference response."""
    class_label = transport_class.upper() if transport_class else ""
    mode_label = transport_mode.lower()
    pref_str = f"{class_label} {mode_label}".strip() if class_label else mode_label

    lines = [
        f"Your preferred {pref_str} option fits the current trip budget.",
        "",
        f"Estimated travel cost: ₹{estimated_cost:,.2f}" if currency == "INR" else f"Estimated travel cost: {currency} {estimated_cost:,.2f}",
        f"Remaining budget impact: ₹{remaining_budget:,.2f}" if currency == "INR" else f"Remaining budget impact: {currency} {remaining_budget:,.2f}",
        "",
        "The option is viable. You can continue to the booking step.",
    ]
    if origin and destination:
        lines.append(f"• Route: {origin} → {destination} (Round Trip)")
    if outbound_date and return_date:
        date_note = " (assumed for planning)" if is_assumed_date else ""
        lines.append(f"• Travel Dates: {outbound_date} through {return_date}{date_note}")
    elif outbound_date:
        date_note = " (assumed for planning)" if is_assumed_date else ""
        lines.append(f"• Travel Date: {outbound_date}{date_note}")
    if operator:
        lines.append(f"• Operator/Carrier: {operator}")
    if flight_number:
        lines.append(f"• Flight Number: {flight_number}")
    if departure_time or arrival_time:
        dep = departure_time or "Scheduled"
        arr = arrival_time or "Scheduled"
        lines.append(f"• Schedule: Dep {dep} — Arr {arr}")
    if seller and seller != operator:
        lines.append(f"• Booking Provider: {seller}")
    if is_exact_booking:
        lines.append("• Handoff Type: Direct Provider Option")
    elif booking_link and "google.com/travel/flights" in booking_link:
        lines.append("• Handoff Type: Provider Search Handoff")

    if booking_link:
        lines.append("")
        lines.append("🔗 *External Booking Handoff:*")
        lines.append(booking_link)
        lines.append("")
        lines.append("Reply with *Booked* once you have completed your external booking, and we will build your full itinerary!")
    return "\n".join(lines)


def format_infeasible_transport(
    transport_mode: str,
    transport_class: str | None,
    people: int,
    cost: Decimal,
    budget: Decimal,
    currency: str = "INR",
    cheaper_alternatives: list[str] | None = None,
) -> str:
    """Format an immediate infeasible transport preference response explaining financial impact."""
    class_label = transport_class.upper() if transport_class else ""
    mode_label = transport_mode.lower()
    pref_str = f"{class_label} {mode_label}".strip() if class_label else mode_label

    travelers_str = f"{people} travelers" if people > 1 else "1 traveler"
    cost_str = f"₹{cost:,.2f}" if currency == "INR" else f"{currency} {cost:,.2f}"
    budget_str = f"₹{budget:,.2f}" if currency == "INR" else f"{currency} {budget:,.2f}"

    lines = [
        f"{pref_str} for {travelers_str} would require approximately {cost_str}.",
        "",
        f"Under your {budget_str} trip budget, this leaves insufficient room for the rest of the planned trip.",
    ]
    if cheaper_alternatives:
        alts_str = " or ".join(cheaper_alternatives)
        lines.append("")
        lines.append(f"Would you like to try {alts_str} instead?")
    return "\n".join(lines)
