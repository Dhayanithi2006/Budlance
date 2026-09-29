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
) -> str:
    """Format a successful, feasible trip plan for Telegram delivery."""
    lines = [
        f"🌴 *Budlance Trip Plan: {destination}*",
        f"👥 {people} {'traveler' if people == 1 else 'travelers'} | ⏱️ {days} days",
        "",
        "💰 *Financial Waterfall:*",
        f"• Total Budget: {breakdown.currency} {breakdown.total_budget:,.2f}",
        f"• Fixed Costs (Travel + Stay): {breakdown.currency} {breakdown.bucket_a_fixed:,.2f}",
        f"• Daily Allowance (Food & Local Transit): {breakdown.currency} {breakdown.bucket_b_survival:,.2f}",
        f"• Activities / Discretionary: {breakdown.currency} {breakdown.bucket_c_activities:,.2f}",
        f"• Rescue Reserve (Bucket D): {breakdown.currency} {breakdown.bucket_d_rescue:,.2f}",
        f"• Total Planned: {breakdown.currency} {breakdown.total_allocated:,.2f}",
        f"• Surplus Remaining: {breakdown.currency} {breakdown.remaining_surplus:,.2f}",
    ]

    # Transports & Hotel
    lines.append("")
    lines.append("🧳 *Selected Bookings:*")
    if transport:
        trans_name = getattr(transport, "airline", None) or getattr(transport, "name_or_operator", "Transport")
        source_val = transport.source.value.upper() if hasattr(transport.source, "value") else str(transport.source).upper()
        lines.append(f"• Transport: {trans_name} — {breakdown.currency} {transport.price:,.2f} `[{source_val}]`")
    if hotel:
        hotel_source = hotel.source.value.upper() if hasattr(hotel.source, "value") else str(hotel.source).upper()
        lines.append(f"• Accommodation: {hotel.name} — {breakdown.currency} {hotel.total_price:,.2f} `[{hotel_source}]`")

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
                prov = f"`[{item.source.value.upper()}]`" if hasattr(item.source, "value") else f"`[{item.source}]`"
                lines.append(f"  • {item.time_slot}: {item.activity} {prov}")

    lines.append("")
    lines.append("✨ *In-Trip Rescue Active:* If it rains, an attraction is closed, or a driver asks for a high fare, message me here for instant replanning!")
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


def format_clarification(missing_fields: list[str]) -> str:
    """Format a helpful clarification request for missing inputs."""
    field_labels = {
        "budget": "Total budget (e.g. ₹20,000)",
        "people": "Number of travelers (e.g. 2 people or solo)",
        "days": "Duration in days (e.g. 3 days)",
        "origin": "Departure city (e.g. from Mumbai)",
    }
    lines = [
        "🤔 *I need a few more details to plan your trip within budget:*",
        "",
    ]
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
            alt_prov = rescue_res.selected_alternative.source.value.upper() if (rescue_res.selected_alternative and hasattr(rescue_res.selected_alternative.source, "value")) else "CACHED"
            return (
                f"🌦️ *Rescue Mode: Alternative Found!*\n\n"
                f"Due to: _{rescue_res.user_issue}_\n"
                f"✅ Selected Alternative: *{alt_name}* `[{alt_prov}]`\n"
                f"💰 Budget Impact: +₹{rescue_res.budget_impact:,.2f} (Covered by Rescue Reserve)\n\n"
                f"📝 *Updated Itinerary Saved:* The affected activity has been replaced and your ledger remains balanced."
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
                f"• Quoted Price: ₹{fg.reported_price:,.2f} `[USER_REPORTED]`\n"
                f"• Estimated Fair Fare: ₹{fg.estimated_fare:,.2f} `[ESTIMATED]` (₹{fg.rate_per_km}/km for ~{fg.distance_km} km)\n"
                f"• Status: *{fg.status.replace('_', ' ').upper()}*\n\n"
                f"ℹ️ {fg.advisory_notes}\n\n"
                f"📝 Recorded reported expenditure in your Virtual Ledger."
            )
        return f"🚕 {rescue_res.resolution_summary}"

    return (
        f"ℹ️ *Budlance Rescue Assistant*\n\n"
        f"{rescue_res.resolution_summary}"
    )
