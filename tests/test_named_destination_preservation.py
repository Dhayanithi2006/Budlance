"""Unit and integration tests for Item 1: Named destination preservation.

Requirement:
Named destination is never swapped. If the message names a place (even unrecognized),
store it as requested_destination and skip discovery.
Infeasible -> say so with numbers, then ask before offering alternatives (labeled as alternatives).
Test: "explore america, 20000, 2 days".
"""

from decimal import Decimal
import pytest
from unittest.mock import AsyncMock, MagicMock

from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.ai.service import AIIntentService
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.orchestrator.formatter import format_infeasible_plan, format_free_summary, format_feasible_plan
from budlance.engine.models import BudgetBreakdown


def test_intent_parsing_explore_america():
    """Verify 'explore america, 20000, 2 days' captures America and requested_destination."""
    svc = AIIntentService(use_mock=True)
    intent = svc._mock_parse_trip_intent("explore america, 20000, 2 days")

    assert intent.destination == "America"
    assert intent.requested_destination == "America"
    assert intent.budget == Decimal("20000")
    assert intent.days == 2
    assert intent.needs_destination_discovery is False


def test_intent_parsing_various_named_places():
    """Verify other unrecognized or arbitrary destinations are stored as requested_destination."""
    svc = AIIntentService(use_mock=True)

    intent1 = svc._mock_parse_trip_intent("trip to iceland, 50000, 5 days")
    assert intent1.destination == "Iceland"
    assert intent1.requested_destination == "Iceland"
    assert intent1.needs_destination_discovery is False

    intent2 = svc._mock_parse_trip_intent("explore south korea, 80000, 4 days")
    assert intent2.destination == "South Korea"
    assert intent2.requested_destination == "South Korea"
    assert intent2.needs_destination_discovery is False


@pytest.mark.asyncio
async def test_explore_america_orchestrator_infeasible_with_numbers_and_question():
    """Test 'explore america, 20000, 2 days' via orchestrator:
    - Never swapped (destination remains America)
    - Skips discovery
    - Infeasible -> reports numbers (budget)
    - Asks before offering alternatives
    """
    orch = BudlanceOrchestrator(ai_service=AIIntentService(use_mock=True))
    res = await orch.handle_user_message(
        telegram_user_id=101,
        chat_id=909090,
        message="explore america, 20000, 2 days",
    )

    assert res.status == "NOT_FEASIBLE"
    assert res.selected_destination == "America"
    # Destination must NEVER be swapped
    assert "pondicherry" not in res.message_text.lower()
    assert "goa" not in res.message_text.lower()

    # Numbers must be stated
    assert "20,000" in res.message_text or "20000" in res.message_text

    # Must ask before offering alternatives
    assert "alternative" in res.message_text.lower()
    assert "?" in res.message_text


def test_format_infeasible_plan_numbers_and_alternatives_prompt():
    """Test formatter output includes numbers and asks about alternatives."""
    res = format_infeasible_plan(
        destination="America",
        budget=Decimal("20000.00"),
        deficit=Decimal("5000.00"),
        explanation="Flight costs exceed allocated transport budget.",
        currency="INR",
    )
    assert "America" in res
    assert "20,000.00" in res
    assert "5,000.00" in res
    assert "alternative" in res.lower()
    assert "?" in res


def test_alternative_destination_labeled_as_alternative():
    """Test that when an alternative is presented, it is explicitly labeled as alternative."""
    breakdown = BudgetBreakdown(
        total_budget=Decimal("20000.00"),
        total_allocated=Decimal("20000.00"),
        bucket_a_fixed=Decimal("8000.00"),
        bucket_b_survival=Decimal("4000.00"),
        bucket_c_activities=Decimal("4000.00"),
        bucket_d_rescue=Decimal("4000.00"),
        remaining_surplus=Decimal("0.00"),
        transport_cost=Decimal("4000.00"),
        hotel_cost=Decimal("4000.00"),
        food_cost=Decimal("3000.00"),
        local_transit_cost=Decimal("1000.00"),
        currency="INR",
    )
    res_free = format_free_summary(
        destination="Pondicherry",
        days=2,
        people=1,
        breakdown=breakdown,
        is_alternative=True,
    )
    assert "Alternative Destination: Pondicherry" in res_free

    res_full = format_feasible_plan(
        destination="Pondicherry",
        days=2,
        people=1,
        breakdown=breakdown,
        transport=None,
        hotel=None,
        itinerary=None,
        ledger=None,
        is_alternative=True,
    )
    assert "Alternative Destination: Pondicherry" in res_full
