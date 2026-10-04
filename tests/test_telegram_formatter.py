"""Tests for Telegram Formatter Integration (Task 11).

Verifies presentation of travel party, real attraction costs, slot entry fees,
and enhanced descriptions in Telegram markdown output.
"""

from decimal import Decimal
from uuid import uuid4
import pytest

from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.engine.models import BudgetBreakdown
from budlance.itinerary.models import GeneratedItinerary, ItineraryDay, ItineraryItem
from budlance.orchestrator.formatter import format_clarification, format_feasible_plan
from budlance.serpapi.models import DataSource


def test_format_feasible_plan_displays_travel_party():
    """Verify that travel_party appears in header when provided."""
    breakdown = BudgetBreakdown(
        total_budget=Decimal("50000.00"),
        bucket_a_fixed=Decimal("20000.00"),
        bucket_b_survival=Decimal("10000.00"),
        bucket_c_activities=Decimal("2500.00"),
        bucket_d_rescue=Decimal("5000.00"),
        transport_cost=Decimal("12000.00"),
        hotel_cost=Decimal("8000.00"),
        food_cost=Decimal("8000.00"),
        local_transit_cost=Decimal("2000.00"),
        attraction_cost=Decimal("200.00"),
        total_allocated=Decimal("37700.00"),
        remaining_surplus=Decimal("12300.00"),
        currency="INR",
    )

    msg = format_feasible_plan(
        destination="Gujarat",
        days=2,
        people=2,
        breakdown=breakdown,
        transport=None,
        hotel=None,
        itinerary=None,
        ledger=None,
        travel_party="couple",
    )

    assert "(Couple)" in msg
    assert "• Curated Attractions: INR 200.00" in msg


def test_format_feasible_plan_omits_travel_party_when_none():
    """When travel_party is None, format cleanly without parenthetical badge."""
    breakdown = BudgetBreakdown(
        total_budget=Decimal("50000.00"),
        bucket_a_fixed=Decimal("20000.00"),
        bucket_b_survival=Decimal("10000.00"),
        bucket_c_activities=Decimal("2500.00"),
        bucket_d_rescue=Decimal("5000.00"),
        transport_cost=Decimal("12000.00"),
        hotel_cost=Decimal("8000.00"),
        food_cost=Decimal("8000.00"),
        local_transit_cost=Decimal("2000.00"),
        attraction_cost=Decimal("0.00"),
        total_allocated=Decimal("37500.00"),
        remaining_surplus=Decimal("12500.00"),
        currency="INR",
    )

    msg = format_feasible_plan(
        destination="Goa",
        days=3,
        people=2,
        breakdown=breakdown,
        transport=None,
        hotel=None,
        itinerary=None,
        ledger=None,
        travel_party=None,
    )

    assert "👥 2 travelers | ⏱️ 3 days" in msg
    assert "Curated Attractions" not in msg


def test_format_feasible_plan_displays_slot_descriptions_and_entry_fees():
    """Verify that slot entry fees and italicized descriptions are rendered."""
    breakdown = BudgetBreakdown(
        total_budget=Decimal("50000.00"),
        bucket_a_fixed=Decimal("20000.00"),
        bucket_b_survival=Decimal("10000.00"),
        bucket_c_activities=Decimal("2500.00"),
        bucket_d_rescue=Decimal("5000.00"),
        transport_cost=Decimal("12000.00"),
        hotel_cost=Decimal("8000.00"),
        food_cost=Decimal("8000.00"),
        local_transit_cost=Decimal("2000.00"),
        attraction_cost=Decimal("200.00"),
        total_allocated=Decimal("37700.00"),
        remaining_surplus=Decimal("12300.00"),
        currency="INR",
    )

    item1 = ItineraryItem(
        time_slot="Morning",
        activity="Visit Sabarmati Ashram",
        place_name="Sabarmati Ashram",
        category="heritage",
        planned_cost=Decimal("0.00"),
        source=DataSource.ESTIMATED,
        is_curated=True,
        attraction_name="Sabarmati Ashram",
        entry_fee_inr=0,
        description="A serene and tranquil historical refuge by the Sabarmati river, offering peaceful walks and historic reflections.",
        slot_type="attraction",
    )
    item2 = ItineraryItem(
        time_slot="Afternoon",
        activity="Explore Rani ki Vav",
        place_name="Rani ki Vav",
        category="heritage",
        planned_cost=Decimal("50.00"),
        source=DataSource.ESTIMATED,
        is_curated=True,
        attraction_name="Rani ki Vav",
        entry_fee_inr=50,
        description="A breathtaking subterranean stepwell filled with ornate sculptures, perfect for appreciating grand historical architecture together.",
        slot_type="attraction",
    )

    day1 = ItineraryDay(
        day_number=1,
        theme_or_summary="Day 1: Sabarmati Ashram & Rani ki Vav",
        items=[item1, item2],
        daily_estimated_cost=Decimal("50.00"),
    )

    itinerary = GeneratedItinerary(
        trip_id=uuid4(),
        destination="Gujarat",
        days_count=1,
        days=[day1],
        is_feasible=True,
        total_budget=Decimal("50000.00"),
        total_planned_cost=Decimal("50.00"),
    )

    msg = format_feasible_plan(
        destination="Gujarat",
        days=1,
        people=2,
        breakdown=breakdown,
        transport=None,
        hotel=None,
        itinerary=itinerary,
        ledger=None,
        travel_party="couple",
    )

    assert "• *Morning:* Visit Sabarmati Ashram" in msg
    assert "_A serene and tranquil historical refuge" in msg
    assert "• *Afternoon:* Explore Rani ki Vav [₹50]" in msg
    assert "_A breathtaking subterranean stepwell" in msg


def test_format_clarification_includes_travel_party():
    """Verify that clarification summaries include travel_party when present."""
    intent = ParsedTripIntent(
        action=TripAction.NEW_TRIP,
        budget=Decimal("50000.00"),
        people=2,
        origin="Chennai",
        destination="Gujarat",
        travel_party="couple",
        interests=["heritage"],
    )

    clarification = format_clarification(missing_fields=["days"], known_context=intent)
    assert "2 travelers (couple)" in clarification
    assert "Chennai → Gujarat" in clarification
    assert "How many days would you like to stay?" in clarification
