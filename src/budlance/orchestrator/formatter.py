"""Presentation layer for formatting Telegram user messages."""

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
    ledger: LedgerSummary | None,
    downgrades: list[str] | None = None,
    travel_party: str | None = None,
    is_pass_unlocked: bool = False,
) -> str:
    """Format a successful, feasible trip plan for Telegram delivery."""
    party_str = f" ({travel_party.title()})" if travel_party else ""
    lines = [
        f"🌴 *Budlance Trip Plan: {destination}*",
        f"👥 {people} {'traveler' if people == 1 else 'travelers'}{party_str} | ⏱️ {days} days",
    ]
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
        lines.append(f"• Transport: {trans_name} — {breakdown.currency} {transport.price:,.2f}")
    if hotel:
        lines.append(f"• Accommodation: {hotel.name} — {breakdown.currency} {hotel.total_price:,.2f}")

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
        for day in itinerary.days:
            lines.append(f"*Day {day.day_number}:* {day.theme_or_summary} (Est. {breakdown.currency} {day.daily_estimated_cost:,.2f})")
            for item in day.items:
                fee_info = f" [₹{item.entry_fee_inr}]" if getattr(item, "entry_fee_inr", 0) and item.entry_fee_inr > 0 else ""
                lines.append(f"  • *{item.time_slot}:* {item.activity}{fee_info}")
                if getattr(item, "description", None) and item.description.strip():
                    lines.append(f"    _{item.description.strip()}_")

    lines.append("")
    lines.append("✨ *In-Trip Rescue Active:* If it rains, an attraction is closed, or a driver asks for a high fare, message me here for instant replanning!")
    return "\n".join(lines)


def format_free_summary(
    destination: str,
    days: int,
    people: int,
    breakdown: BudgetBreakdown,
    pass_amount: Decimal = Decimal("49.00"),
    checkout_url: str | None = None,
    travel_party: str | None = None,
) -> str:
    """Format the free reverse-budget discovery summary with Trip Pass unlock call-to-action."""
    party_str = f" ({travel_party.title()})" if travel_party else ""
    lines = [
        f"🌴 *Budlance Trip Plan: {destination}*",
        f"👥 {people} {'traveler' if people == 1 else 'travelers'}{party_str} | ⏱️ {days} days",
        "",
        "💰 *Financial Waterfall (Free Feasibility Analysis):*",
        f"• Total Budget: {breakdown.currency} {breakdown.total_budget:,.2f}",
        f"• Fixed Costs (Travel + Stay): {breakdown.currency} {breakdown.bucket_a_fixed:,.2f}",
        f"• Daily Allowance (Food & Local Transit): {breakdown.currency} {breakdown.bucket_b_survival:,.2f}",
    ]

    if getattr(breakdown, "attraction_cost", Decimal("0.00")) > Decimal("0.00"):
        lines.append(f"• Curated Attractions: {breakdown.currency} {breakdown.attraction_cost:,.2f}")

    lines.extend([
        f"• Activities / Discretionary: {breakdown.currency} {breakdown.bucket_c_activities:,.2f}",
        f"• Rescue Reserve (Bucket D): {breakdown.currency} {breakdown.bucket_d_rescue:,.2f}",
        f"• Total Planned: {breakdown.currency} {breakdown.total_allocated:,.2f}",
        f"• Surplus Remaining: {breakdown.currency} {breakdown.remaining_surplus:,.2f}",
        "",
        "🔒 *Detailed Itinerary & Rescue Locked*",
        f"Your budget of {breakdown.currency} {breakdown.total_budget:,.2f} is feasible! To unlock the day-by-day attraction schedule, booking deep links, and live In-Trip Rescue, get your Budlance Trip Pass.",
        "",
        f"🎟️ *Budlance Trip Pass: {breakdown.currency} {pass_amount:,.2f}*",
    ])

    if checkout_url:
        lines.append(f"👉 [Unlock Full Trip Plan]({checkout_url})")
    else:
        lines.append("👉 Send 'unlock' to get checkout link, or send /demo_pass for judge/demo bypass.")

    lines.append("_(Judge/Demo review: send /demo_pass to unlock instantly without payment)_")
    return "\n".join(lines)


def format_infeasible_plan(
    destination: str | None,
    budget: Decimal,
    deficit: Decimal,
    explanation: str,
    recommendation: str | None = None,
    currency: str = "INR",
) -> str:
    """Format an over-budget / impossible trip response."""
    dest_str = f" to {destination}" if destination else ""
    lines = [
        "❌ *Trip Plan Not Feasible within Budget*",
        f"I tried to build a trip{dest_str} within your budget of {currency} {budget:,.2f}, but the mandatory travel and survival costs exceed your budget.",
        "",
        f"• *Deficit:* {currency} {deficit:,.2f}",
        f"• *Details:* {explanation}",
    ]
    if recommendation:
        lines.append("")
        lines.append(f"💡 *Recommendation to make it feasible:*\n{recommendation}")
    else:
        lines.append("")
        lines.append(f"💡 *Recommendation:* Consider increasing your budget by at least {currency} {deficit:,.2f} or reducing the trip duration by 1 day.")

    return "\n".join(lines)


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
            return (
                f"🚕 *Advisory Transit Fare Guidance:*\n\n"
                f"• Quoted Price: ₹{fg.reported_price:,.2f}\n"
                f"• Estimated Fair Fare: ₹{fg.estimated_fare:,.2f} (~₹{fg.rate_per_km}/km for ~{fg.distance_km} km)\n"
                f"• Status: *{fg.status.replace('_', ' ').upper()}*\n\n"
                f"ℹ️ {fg.advisory_notes}\n\n"
                f"📝 Recorded reported expenditure in your Virtual Ledger."
            )
        return f"🚕 {rescue_res.resolution_summary}"

    return (
        f"ℹ️ *Budlance Rescue Assistant*\n\n"
        f"{rescue_res.resolution_summary}"
    )


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
