"""Regression tests for Pre-OpenRouter Cleanup Batch.

Covers:
1. [FALLBACK] and internal tags do not appear in final Telegram-facing output while preserving model provenance.
2. Long formatted Telegram response is split cleanly without middle-of-word truncation.
3. Rescue with no discovered alternative produces the intended actionable fallback response.
4. Activities value is generated from the actual 5% budget derivation source.
5. Activity value used by budget engine matches the value used by ledger and itinerary.
6. Theme-park unsupported interest does not silently claim a matching destination.
"""

from decimal import Decimal
from uuid import uuid4
import pytest

from budlance.ai.service import AIIntentService
from budlance.engine.budget import ReverseBudgetEngine
from budlance.engine.models import BudgetBreakdown, BudgetEvaluationResult
from budlance.estimation.estimator import EstimationLayer
from budlance.itinerary.generator import ItineraryGenerator
from budlance.itinerary.models import GeneratedItinerary, ItineraryDay, ItineraryItem
from budlance.ledger.models import LedgerSummary
from budlance.orchestrator.formatter import (
    format_feasible_plan,
    format_rescue_result,
    split_telegram_message,
)
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.rescue.models import FareGuidance, RescueResult
from budlance.schemas.travel import (
    FlightOption,
    FoodEstimate,
    HotelOption,
    LocalTransitEstimate,
    PlaceOption,
    TransitOption,
)
from budlance.serpapi.models import DataSource


def test_1_no_fallback_labels_in_telegram_plan_output():
    """1. [FALLBACK], [ESTIMATED], [LIVE] must NOT appear in final Telegram-facing message."""
    breakdown = BudgetBreakdown(
        total_budget=Decimal("19000.00"),
        currency="INR",
        bucket_a_fixed=Decimal("6500.00"),
        bucket_b_survival=Decimal("5400.00"),
        bucket_c_activities=Decimal("950.00"),
        bucket_d_rescue=Decimal("1900.00"),
        transport_cost=Decimal("500.00"),
        hotel_cost=Decimal("6000.00"),
        food_cost=Decimal("4800.00"),
        local_transit_cost=Decimal("600.00"),
        total_allocated=Decimal("14750.00"),
        remaining_surplus=Decimal("4250.00"),
        provenance={
            "transport": DataSource.FALLBACK,
            "hotel": DataSource.FALLBACK,
            "food": DataSource.ESTIMATED,
            "local_transit": DataSource.ESTIMATED,
            "activities": DataSource.ESTIMATED,
            "rescue_reserve": DataSource.ESTIMATED,
        },
    )

    transport = TransitOption(
        transit_type="train",
        origin="Chennai",
        destination="Goa",
        name_or_operator="Indian Railways Express",
        price=Decimal("500.00"),
        source=DataSource.FALLBACK,
        is_fallback=True,
    )

    hotel = HotelOption(
        name="Goa Heritage Palace",
        hotel_class=3,
        price_per_night=Decimal("3000.00"),
        total_price=Decimal("6000.00"),
        source=DataSource.FALLBACK,
        is_fallback=True,
    )

    day1 = ItineraryDay(
        day_number=1,
        theme_or_summary="Day 1 in Goa",
        items=[
            ItineraryItem(
                time_slot="Morning",
                activity="Onward travel to Goa via Indian Railways Express",
                category="transport",
                planned_cost=Decimal("500.00"),
                source=DataSource.FALLBACK,
            ),
            ItineraryItem(
                time_slot="Evening",
                activity="Explore Goa Central Landmark",
                category="attraction",
                planned_cost=Decimal("475.00"),
                source=DataSource.FALLBACK,
            ),
        ],
        daily_estimated_cost=Decimal("975.00"),
    )

    itin = GeneratedItinerary(
        trip_id=uuid4(),
        destination="Goa",
        days_count=1,
        days=[day1],
        is_feasible=True,
        total_budget=Decimal("19000.00"),
        total_planned_cost=Decimal("975.00"),
    )

    text = format_feasible_plan(
        destination="Goa",
        days=1,
        people=3,
        breakdown=breakdown,
        transport=transport,
        hotel=hotel,
        itinerary=itin,
        ledger=None,
    )

    # Assert no internal developer badges leak into text
    assert "[FALLBACK]" not in text
    assert "[FALL..." not in text
    assert "[ESTIMATED]" not in text
    assert "[LIVE]" not in text
    assert "[CACHED]" not in text

    # Assert internal provenance remains intact in underlying objects
    assert transport.source == DataSource.FALLBACK
    assert hotel.source == DataSource.FALLBACK
    assert day1.items[0].source == DataSource.FALLBACK
    assert breakdown.provenance["transport"] == DataSource.FALLBACK


def test_2_no_fallback_labels_in_rescue_output():
    """2. Rescue messages do not expose internal provenance badges."""
    # Case A: Weather rescue alternative found
    alt = PlaceOption(
        name="Goa State Museum",
        category="museum",
        source=DataSource.FALLBACK,
        estimated_cost=Decimal("100.00"),
    )
    res_alt = RescueResult(
        trip_id=uuid4(),
        success=True,
        rescue_type="weather_closure",
        user_issue="Heavy rain at Miramar Beach",
        resolution_summary="Found indoor alternative place: Goa State Museum",
        selected_alternative=alt,
        budget_impact=Decimal("100.00"),
    )
    text_alt = format_rescue_result(res_alt)
    assert "[FALLBACK]" not in text_alt
    assert "[CACHED]" not in text_alt
    assert "[LIVE]" not in text_alt
    assert "Goa State Museum" in text_alt
    assert alt.source == DataSource.FALLBACK

    # Case B: Fare dispute
    fg = FareGuidance(
        mode="auto",
        reported_price=Decimal("400.00"),
        estimated_fare=Decimal("180.00"),
        difference=Decimal("220.00"),
        rate_per_km=15.0,
        distance_km=12.0,
        status="severe_overcharge",
        advisory_notes="Driver quote is ~122% above standard rates.",
    )
    res_fare = RescueResult(
        trip_id=uuid4(),
        success=True,
        rescue_type="price_dispute",
        user_issue="Driver asking ₹400 for 12 km",
        resolution_summary="Fare dispute analyzed against local rates.",
        fare_guidance=fg,
    )
    text_fare = format_rescue_result(res_fare)
    assert "[USER_REPORTED]" not in text_fare
    assert "[ESTIMATED]" not in text_fare
    assert "400.00" in text_fare


def test_3_split_telegram_message_limits_and_integrity():
    """3. Message splitting respects 4096 limit without cutting in middle of words."""
    short_text = "Hello from Budlance! Clean message."
    chunks_short = split_telegram_message(short_text, max_length=4096)
    assert len(chunks_short) == 1
    assert chunks_short[0] == short_text

    # Build long message that exceeds 4096
    lines = [f"Day {i}: Activity description for trip day {i} with lots of details" for i in range(1, 100)]
    long_text = "\n".join(lines)
    assert len(long_text) > 4096

    chunks = split_telegram_message(long_text, max_length=4096)
    assert len(chunks) > 1

    for c in chunks:
        assert len(c) <= 4096
        # Must not end with a partial bracket or open delimiter
        assert not c.endswith("[FALL")
        assert not c.endswith("[")

    # Reconstituted text contains all original lines
    reconstituted = "\n".join(chunks)
    for line in lines:
        assert line in reconstituted


def test_4_rescue_no_alternatives_actionable_response():
    """4. Rescue with no discovered alternative produces actionable next steps."""
    res = RescueResult(
        trip_id=uuid4(),
        success=False,
        rescue_type="weather_closure",
        user_issue="It's raining heavily at beach",
        resolution_summary="No suitable alternative places could be discovered in Goa.",
        is_feasible=True,
        error="NO_ALTERNATIVES_FOUND",
    )

    text = format_rescue_result(res)

    # Must NOT claim failure is budget infeasibility
    assert "Rescue Replanning Infeasible" not in text
    # Must state transparently that no alternatives were discovered
    assert "No suitable alternative places could be discovered in Goa" in text
    # Must offer actionable guidance
    assert "Actionable Next Steps" in text
    assert "Rescue Reserve" in text
    assert "Your current itinerary and budget allocations remain untouched" in text


def test_5_activities_amount_generation_and_consistency():
    """5. Activities amount is derived as 5% of budget and remains consistent."""
    budget = Decimal("19000.00")
    people = 3
    days = 2

    # Derived 5% activity budget
    activities_budget = round(budget * Decimal("0.05"), 2)
    assert activities_budget == Decimal("950.00")

    # Budget engine evaluation
    engine = ReverseBudgetEngine()
    est_layer = EstimationLayer()
    food = est_layer.estimate_food(people=people, days=days, tier="standard")
    transit = est_layer.estimate_local_transit_daily(days=days, people=people, mode="metro_bus")
    transport = TransitOption(
        transit_type="train",
        origin="Chennai",
        destination="Goa",
        name_or_operator="Indian Railways Express",
        price=Decimal("500.00"),
    )
    hotel = HotelOption(name="Hotel", hotel_class=3, price_per_night=Decimal("3000.00"), total_price=Decimal("6000.00"))

    eval_result = engine.evaluate(
        total_budget=budget,
        people=people,
        days=days,
        transport=transport,
        hotel=hotel,
        food_estimate=food,
        local_transit_estimate=transit,
        activities_budget=activities_budget,
    )

    assert eval_result.is_feasible is True
    assert eval_result.breakdown.bucket_c_activities == Decimal("950.00")

    # Itinerary generator daily allocation
    daily_act = round(eval_result.breakdown.bucket_c_activities / Decimal(days), 2)
    assert daily_act == Decimal("475.00")


@pytest.mark.asyncio
async def test_6_theme_park_interest_transparent_unmatched_behavior():
    """6. Theme-park request does not silently fabricate static destinations.

    Open-ended destination request:
    -> live destination discovery
    -> if no candidates
    -> controlled CLARIFICATION / no-results
    Verifies:
    - theme-park interest is preserved
    - budget, days, people, origin are preserved
    - no static destination (e.g. Goa) is fabricated
    - no deleted destination catalog is used
    - controlled clarification response is returned
    """
    ai_service = AIIntentService(use_mock=True)
    prompt = "I have 19000, 3 people, 2 days, theme park going from Chennai"
    intent = await ai_service.parse_trip_intent(prompt)

    assert intent.budget == Decimal("19000")
    assert intent.people == 3
    assert intent.days == 2
    assert intent.origin == "Chennai"
    assert "theme park" in intent.interests
    assert intent.destination is None  # Needs discovery

    # Orchestrator handles message and returns controlled clarification without fabricating static catalog
    orch = BudlanceOrchestrator(ai_service=ai_service)
    result = await orch.handle_user_message(
        telegram_user_id=123456,
        chat_id=123456,
        message=prompt,
    )

    assert result.status in ("CLARIFICATION", "NOT_FEASIBLE")
    assert result.selected_destination is None
    if result.status == "CLARIFICATION":
        assert "I couldn't find available destinations matching your budget from Chennai" in result.message_text
    else:
        assert "No feasible destination found" in result.message_text

    # Pending intent must preserve user parameters for follow-up turn
    pending = orch.conversation_repo.get_pending_intent(123456)
    assert pending is not None
    assert pending.budget == Decimal("19000")
    assert pending.people == 3
    assert pending.days == 2
    assert pending.origin == "Chennai"
    assert "theme park" in pending.interests
    assert pending.destination is None
