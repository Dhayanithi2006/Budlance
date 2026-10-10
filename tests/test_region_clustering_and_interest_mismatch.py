"""Tests for Item 3: Interest mismatch handling and one region per day regional clustering."""

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
import pytest

from budlance.ai.service import AIIntentService
from budlance.attractions.models import Attraction
from budlance.attractions.selector import AttractionSelector
from budlance.estimation.estimator import EstimationLayer
from budlance.engine.budget import ReverseBudgetEngine
from budlance.itinerary.generator import ItineraryGenerator
from budlance.orchestrator.orchestrator import BudlanceOrchestrator


def test_all_curated_json_attractions_have_region():
    """All attractions in all curated JSON files must have a non-empty region field."""
    attractions_dir = Path("data/attractions")
    assert attractions_dir.exists(), "data/attractions directory must exist"

    json_files = list(attractions_dir.glob("*.json"))
    assert len(json_files) >= 7, f"Expected at least 7 JSON files, found {len(json_files)}"

    for jf in json_files:
        with open(jf, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert isinstance(data, list), f"{jf.name} must be a JSON array"
        assert len(data) > 0, f"{jf.name} must not be empty"
        for item in data:
            assert "region" in item, f"Missing 'region' field in {jf.name} for attraction {item.get('name')}"
            assert isinstance(item["region"], str) and item["region"].strip(), (
                f"Empty 'region' in {jf.name} for attraction {item.get('name')}"
            )


def test_one_region_per_day_clustering():
    """ItineraryGenerator must schedule attractions so each day only contains items from one region."""
    generator = ItineraryGenerator()
    budget_engine = ReverseBudgetEngine()
    estimation = EstimationLayer()

    dest = "Pondicherry"
    days = 2
    food_est = estimation.estimate_food(people=2, days=days)
    transit_est = estimation.estimate_local_transit_daily(days=days, people=2)
    eval_res = budget_engine.evaluate(
        total_budget=Decimal("25000"),
        people=2,
        days=days,
        transport=None,
        hotel=None,
        food_estimate=food_est,
        local_transit_estimate=transit_est,
    )
    assert eval_res.is_feasible

    itin = generator.generate(
        trip_id=uuid4(),
        destination=dest,
        evaluation=eval_res,
        days=days,
    )
    assert itin.is_feasible
    assert len(itin.days) == days

    day_regions = []
    for day in itin.days:
        assert day.region is not None, f"Day {day.day_number} must have an assigned region"
        day_regions.append(day.region)
        # All items on this day must have the same region as the day
        for item in day.items:
            if item.region:
                assert item.region == day.region, (
                    f"Item {item.activity} has region '{item.region}', expected day region '{day.region}'"
                )

    # Days should visit different regions when multiple regions are available
    if len(set(day_regions)) > 1:
        assert day_regions[0] != day_regions[1]


def test_interest_mismatch_schedules_fewer_items_and_honest_line():
    """When requested interests have no match, schedule fewer items (no off-tone filler) and provide an honest note."""
    generator = ItineraryGenerator()
    budget_engine = ReverseBudgetEngine()
    estimation = EstimationLayer()

    dest = "Agra"
    days = 2
    food_est = estimation.estimate_food(people=2, days=days)
    transit_est = estimation.estimate_local_transit_daily(days=days, people=2)
    eval_res = budget_engine.evaluate(
        total_budget=Decimal("20000"),
        people=2,
        days=days,
        transport=None,
        hotel=None,
        food_estimate=food_est,
        local_transit_estimate=transit_est,
    )

    # User asks for "scuba diving" in Agra (complete mismatch)
    itin = generator.generate(
        trip_id=uuid4(),
        destination=dest,
        evaluation=eval_res,
        days=days,
        interests=["scuba diving"],
    )

    # Must provide honest line in feasibility_note
    assert itin.feasibility_note is not None
    assert "limited" in itin.feasibility_note.lower() or "no matching" in itin.feasibility_note.lower()
    assert "filler" in itin.feasibility_note.lower() or "light" in itin.feasibility_note.lower()

    # Must schedule fewer items: Afternoon slots should NOT be packed with off-tone filler
    for day in itin.days:
        afternoon_item = next((it for it in day.items if it.time_slot.lower() == "afternoon"), None)
        assert afternoon_item is not None
        # Afternoon is free time, not an unrelated padded attraction
        assert afternoon_item.category == "free_time"
        assert afternoon_item.slot_type == "free_time"


@pytest.mark.asyncio
async def test_orchestrator_interest_mismatch_flow():
    """End-to-end orchestrator flow with interest mismatch displays the honest note."""
    orch = BudlanceOrchestrator(ai_service=AIIntentService(use_mock=True))
    res = await orch.handle_user_message(
        telegram_user_id=8899,
        chat_id=776655,
        message="Plan a trip from Delhi to Agra for 2 people, 2 days, budget 20000 with scuba diving",
    )
    assert res.status == "FEASIBLE"
    assert "Note:" in res.message_text
    # Honest explanation present
    assert "scuba" in res.message_text.lower()
