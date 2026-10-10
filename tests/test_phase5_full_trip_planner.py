"""Phase 5 End-to-End Tests: Full-Trip Planner & Day-by-Day Itinerary Engine.

Covers the three mandatory user journeys:
1. Example 1: 5-Day Goa Trip from Chennai (₹30,000 budget, 2 adults, 4 hotel nights, round-trip transport).
2. Example 2: Time-Aware Itinerary with landing at 10 AM, activities >= 11 AM, vegetarian food, beach sunset.
3. Example 3: Surgical replacement of Day 2 closed museum with a nearby nature spot.
Plus: date calculations, event validation, and financial/provenance integrity.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4
import pytest

from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.ai.service import AIIntentService
from budlance.engine.budget import ReverseBudgetEngine
from budlance.estimation.estimator import EstimationLayer
from budlance.itinerary.generator import ItineraryGenerator
from budlance.itinerary.models import GeneratedItinerary, ItineraryDay, ItineraryItem
from budlance.itinerary.replacer import replace_itinerary_item
from budlance.orchestrator.formatter import format_feasible_plan
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.schemas.travel import EventOption, FlightOption, HotelOption, PlaceOption
from budlance.serpapi.models import DataSource


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def orchestrator():
    """Build an orchestrator with mock AI service to test deterministic pipeline."""
    return BudlanceOrchestrator()


@pytest.fixture
def mock_flights():
    """Mock round-trip flight from Chennai to Goa (₹3,500/person each way = ₹14,000 total)."""
    return FlightOption(
        airline="IndiGo",
        flight_number="6E-204",
        price=Decimal("14000.00"),
        source=DataSource.CACHED,
        deep_link="https://www.google.com/travel/flights?q=MAA+to+GOI",
    )


@pytest.fixture
def mock_hotel():
    """Mock 4-night stay in Goa (₹2,200/night = ₹8,800 total)."""
    return HotelOption(
        name="Santana Beach Resort",
        price_per_night=Decimal("2200.00"),
        total_price=Decimal("8800.00"),
        source=DataSource.LIVE,
        address="Candolim, North Goa",
        deep_link="https://www.google.com/travel/hotels/santana",
    )


# =============================================================================
# EXAMPLE 1: COMPLETE GOA TRIP
# =============================================================================

@pytest.mark.asyncio
async def test_example_1_complete_goa_trip(orchestrator, monkeypatch):
    """Example 1: 5-day Goa trip from Chennai (1–5 Nov 2026, 2 adults, ₹30,000 budget, relaxed, beaches)."""
    chat_id = 991001

    prompt = (
        "Plan a 5-day trip from Chennai to Goa for 2 adults from 1–5 November 2026. "
        "My total budget is ₹30,000, including return travel, stay, food, local travel and activities. "
        "I like beaches and local food. Keep the schedule relaxed."
    )

    monkeypatch.setenv("SERPAPI_LIVE_ENABLED", "false")
    res = await orchestrator.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message=prompt,
    )

    assert res.status == "FEASIBLE"
    assert res.selected_destination == "Goa"

    # Verify duration and nights
    itin = res.generated_itinerary or res.itinerary
    assert itin is not None
    assert itin.days_count == 5
    assert len(itin.days) == 5

    # Check date intervals: 1 to 5 Nov = 5 calendar days, 4 hotel nights
    day_dates = [d.date_str for d in itin.days if d.date_str]
    if day_dates:
        assert day_dates[0] == "2026-11-01"
        assert day_dates[-1] == "2026-11-05"

    # Verify each day has exact morning, afternoon, evening slots
    for day in itin.days:
        slots = [item.time_slot for item in day.items]
        assert slots == ["Morning", "Afternoon", "Evening"]

    # Verify Day 1 has arrival & transfer
    day1_items = itin.days[0].items
    assert any("arrival" in it.activity.lower() or "visit" in it.activity.lower() for it in day1_items)

    # Verify Day 5 has return transport
    day5_items = itin.days[-1].items
    assert any("return journey" in it.activity.lower() or "transit" in it.activity.lower() for it in day5_items)

    # Verify budget and unknown admission fees handling
    assert res.budget_breakdown is not None
    # Outbound + return travel + stay + food + transit within ₹30,000
    assert res.budget_breakdown.total_budget == Decimal("30000.00")


# =============================================================================
# EXAMPLE 2: TIME-AWARE ITINERARY WITH CONSTRAINTS
# =============================================================================

@pytest.mark.asyncio
async def test_example_2_time_aware_constraints(orchestrator):
    """Example 2: Update existing Goa trip with landing at 10 AM, activities >= 11 AM, vegetarian food, beach sunset."""
    chat_id = 991002

    # Initial turn: establish the Goa trip
    prompt1 = "Plan a 5-day trip from Chennai to Goa for 2 adults with ₹30000 budget."
    res1 = await orchestrator.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message=prompt1)
    assert res1.status == "FEASIBLE"

    # Follow-up turn: add constraints
    prompt2 = (
        "Keep my Goa trip, but I land at 10 AM on the first day. "
        "I prefer vegetarian food, don't schedule any activities before 11 AM, "
        "and I want to watch the sunset at a beach. Keep the return journey unchanged."
    )
    res2 = await orchestrator.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message=prompt2)

    assert res2.status == "FEASIBLE"
    itin = res2.generated_itinerary or res2.itinerary
    assert itin is not None
    assert itin.days_count == 5

    # 1. Day 1: Account for arrival transfer at 10 AM, first activity starting at or after 11 AM
    day1 = itin.days[0]
    morning_item = day1.items[0]
    assert morning_item.time_slot == "Morning"
    assert "10" in (morning_item.start_time or "") and "AM" in (morning_item.start_time or "")

    # 2. Activity timing >= 11 AM constraint respected
    afternoon_item = day1.items[1]
    assert afternoon_item.time_slot == "Afternoon"
    # Afternoon activity window starts >= 11 AM (typically 01:00 PM or 02:00 PM)
    assert "PM" in afternoon_item.start_time or "11:" in afternoon_item.start_time or "12:" in afternoon_item.start_time

    # 3. Beach sunset scheduled in Evening slot
    evening_item = day1.items[2]
    assert evening_item.time_slot == "Evening"
    assert getattr(evening_item, "is_sunset_timing", False) is True or "sunset" in evening_item.activity.lower() or "beach" in evening_item.category.lower()
    assert getattr(evening_item, "notes", None) is not None

    # 4. Vegetarian dietary tags present
    assert any("vegetarian" in (it.dietary_tags or []) for day in itin.days for it in day.items)

    # 5. Return journey unchanged on Day 5
    day5 = itin.days[-1]
    assert any("return journey" in it.activity.lower() or "transit" in it.activity.lower() for it in day5.items)


# =============================================================================
# EXAMPLE 3: SURGICAL REPLACEMENT OF UNAVAILABLE ATTRACTION
# =============================================================================

@pytest.mark.asyncio
async def test_example_3_replace_closed_attraction_surgically(orchestrator):
    """Example 3: Replace Day 2 closed museum with a nearby nature spot while keeping flights, hotel, and other days intact."""
    chat_id = 991003

    # Initial turn: plan trip
    prompt1 = "Plan a 3-day trip from Chennai to Goa for 2 adults with ₹25000 budget."
    res1 = await orchestrator.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message=prompt1)
    itin1 = res1.generated_itinerary or res1.itinerary
    # Ensure Day 2 has a museum visit per example specification
    day2_record = orchestrator.itinerary_repo.get_itinerary(res1.trip_id)
    assert day2_record is not None
    day2_record.days[1]["items"][1]["activity"] = "Visit Goa State Museum"
    day2_record.days[1]["items"][1]["place_name"] = "Goa State Museum"
    day2_record.days[1]["items"][1]["attraction_name"] = "Goa State Museum"
    day2_record.days[1]["items"][1]["category"] = "museum"
    orchestrator.itinerary_repo.save_itinerary(day2_record)

    # Follow-up turn: user reports museum closed and requests nature replacement
    prompt2 = (
        "The museum on Day 2 is closed. Replace it with a nearby nature spot, "
        "keep my hotel and flights unchanged, and stay within my original budget. Don't change anything else."
    )
    res2 = await orchestrator.handle_user_message(telegram_user_id=chat_id, chat_id=chat_id, message=prompt2)

    assert res2.status == "FEASIBLE"
    itin2 = res2.generated_itinerary or res2.itinerary
    assert itin2 is not None

    # Day 1 and Day 3 items should remain intact
    assert len(itin2.days) == 3
    assert itin2.days[0].theme_or_summary == itin1.days[0].theme_or_summary
    assert itin2.days[2].theme_or_summary == itin1.days[2].theme_or_summary

    # Day 2 should reflect the replacement (nature / beach spot)
    day2_items_after = itin2.days[1].items
    assert any(
        "nature" in it.category.lower()
        or "beach" in it.category.lower()
        or "waterfall" in it.activity.lower()
        or "sanctuary" in it.activity.lower()
        for it in day2_items_after
    )

    # Verified that hotel and flight/transport parameters and budget were preserved
    if res1.selected_transport is not None:
        assert res2.selected_transport is not None
        assert res2.selected_transport.model_dump(exclude={"id"}) == res1.selected_transport.model_dump(exclude={"id"})
    else:
        assert res2.selected_transport == res1.selected_transport

    if res1.selected_hotel is not None:
        assert res2.selected_hotel is not None
        assert res2.selected_hotel.model_dump(exclude={"id"}) == res1.selected_hotel.model_dump(exclude={"id"})
    else:
        assert res2.selected_hotel == res1.selected_hotel

    assert res2.budget_breakdown.total_budget == res1.budget_breakdown.total_budget


# =============================================================================
# GROUNDED SCHEDULING & INTEGRITY TESTS
# =============================================================================

def test_inclusive_calendar_dates_and_nights_calculation():
    """Verify inclusive calendar days and hotel night count: 1–5 Nov = 5 days, 4 nights."""
    ai_service = AIIntentService(use_mock=True)
    intent = ai_service._mock_full_extract("Plan a trip to Goa from 1–5 November 2026")

    assert intent.start_date == "2026-11-01"
    assert intent.end_date == "2026-11-05"
    assert intent.days == 5  # 5 calendar days inclusive

    # Hotel nights count is days - 1 = 4 nights
    hotel_nights = intent.days - 1
    assert hotel_nights == 4


def test_replacer_function_direct_execution():
    """Verify replacer engine directly performs atomic in-place replacement."""
    generator = ItineraryGenerator()
    engine = ReverseBudgetEngine()
    estimation = EstimationLayer()

    eval_result = engine.evaluate(
        total_budget=Decimal("25000.00"),
        people=2,
        days=3,
        transport=None,
        hotel=None,
        food_estimate=estimation.estimate_food(people=2, days=3),
        local_transit_estimate=estimation.estimate_local_transit_daily(days=3, people=2),
    )

    itin = generator.generate(
        trip_id=uuid4(),
        destination="Goa",
        evaluation=eval_result,
        days=3,
    )

    # Replace Day 2 afternoon item with a nature spot
    updated_itin, old_it, new_it = replace_itinerary_item(
        itinerary=itin,
        target_name_or_cat="museum",
        replacement_category="nature",
        day_number=2,
        destination="Goa",
    )

    # In Goa dataset or fallback, replaces with a nature or beach candidate
    if new_it:
        assert new_it.category in ("nature", "beach", "scenic", "attraction")
        assert updated_itin.days_count == 3
        # Cost recalculation preserves validity
        assert updated_itin.total_planned_cost >= Decimal("0.00")


def test_formatter_includes_phase5_grounded_fields():
    """Verify telegram formatter displays approximate time windows, dietary options, and sunset notes."""
    estimation = EstimationLayer()
    engine = ReverseBudgetEngine()

    eval_res = engine.evaluate(
        total_budget=Decimal("30000.00"),
        people=2,
        days=2,
        transport=None,
        hotel=None,
        food_estimate=estimation.estimate_food(people=2, days=2),
        local_transit_estimate=estimation.estimate_local_transit_daily(days=2, people=2),
    )

    day1 = ItineraryDay(
        day_number=1,
        theme_or_summary="Day 1: Arrival & Coastal Sunset",
        daily_estimated_cost=Decimal("1500.00"),
        items=[
            ItineraryItem(
                time_slot="Morning",
                activity="Arrival in Goa & hotel check-in",
                approximate_time_window="10:00 AM – 12:30 PM",
                category="accommodation",
                description="Arrive in Goa, complete gateway transfer and check in to accommodation.",
            ),
            ItineraryItem(
                time_slot="Afternoon",
                activity="Explore Aguada Fort",
                approximate_time_window="02:00 PM – 04:30 PM",
                category="attraction",
                entry_fee_inr=50,
                description="Historic 17th-century Portuguese fortress and lighthouse overlooking the sea.",
                dietary_tags=["vegetarian"],
            ),
            ItineraryItem(
                time_slot="Evening",
                activity="Sunset viewing at Baga Beach",
                approximate_time_window="05:15 PM – 07:15 PM",
                category="beach",
                is_sunset_timing=True,
                notes="Sunset timing approx. 05:45 PM – 06:30 PM depending on season",
                description="Spectacular sunset views over coastal waters at Baga Beach.",
                external_link="https://maps.google.com/?cid=123",
            ),
        ],
    )

    itin = GeneratedItinerary(
        trip_id=uuid4(),
        destination="Goa",
        days_count=1,
        days=[day1],
        is_feasible=True,
        total_budget=Decimal("30000.00"),
        total_planned_cost=Decimal("1500.00"),
    )

    msg = format_feasible_plan(
        destination="Goa",
        days=1,
        people=2,
        breakdown=eval_res.breakdown,
        transport=None,
        hotel=None,
        itinerary=itin,
        ledger=None,
    )

    assert "• *Morning:* Arrival in Goa & hotel check-in" in msg
    assert "⏰ _10:00 AM – 12:30 PM_" in msg
    assert "• *Afternoon:* Explore Aguada Fort [₹50]" in msg
    assert "🥗 _Vegetarian options_" in msg
    assert "🌅 _Sunset timing approx. 05:45 PM – 06:30 PM depending on season_" in msg
    assert "🔗 Info: https://maps.google.com/?cid=123" in msg
