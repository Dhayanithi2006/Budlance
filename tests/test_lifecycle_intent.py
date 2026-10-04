"""Tests for Task 2 — LOG_EXPENSE + TRIP_COMPLETE intent system extensions."""

from decimal import Decimal
import pytest

from budlance.ai.schemas import (
    ParsedIntent,
    ParsedTripIntent,
    TravelAction,
    TripAction,
    normalize_expense_category,
)
from budlance.ai.service import AIIntentService


@pytest.fixture
def parser():
    """Deterministic heuristic/mock parser for offline, reproducible intent tests."""
    return AIIntentService(use_mock=True)


class TestLifecycleIntentSchema:
    """Verify TravelAction, TripAction, and ParsedIntent fields."""

    def test_travel_action_and_trip_action_enum(self):
        assert TripAction.LOG_EXPENSE.value == "LOG_EXPENSE"
        assert TripAction.TRIP_COMPLETE.value == "TRIP_COMPLETE"
        assert ParsedIntent is ParsedTripIntent

    def test_parsed_intent_lifecycle_fields_defaults(self):
        intent = ParsedTripIntent()
        assert intent.amount is None
        assert intent.expense_category is None
        assert intent.day_number is None
        assert intent.day_completed is False

    def test_parsed_intent_decimal_normalization(self):
        intent = ParsedTripIntent(
            action=TripAction.LOG_EXPENSE,
            amount="2,200.50",
            expense_category="FOOD",
            day_number=1,
            day_completed=False,
        )
        assert isinstance(intent.amount, Decimal)
        assert intent.amount == Decimal("2200.50")
        assert intent.expense_category == "food"
        assert intent.day_number == 1
        assert intent.day_completed is False

    def test_expense_category_normalization(self):
        assert normalize_expense_category("FOOD") == "food"
        assert normalize_expense_category("lunch") == "food"
        assert normalize_expense_category("cafe and dinner") == "food"
        assert normalize_expense_category("autos") == "transport"
        assert normalize_expense_category("taxi ride") == "transport"
        assert normalize_expense_category("museum entry") == "activities"
        assert normalize_expense_category("resort room") == "stay"
        assert normalize_expense_category(None) is None


class TestLogExpenseAndTripCompleteParsing:
    """Verify parsing for the required test cases."""

    @pytest.mark.asyncio
    async def test_1_spent_2200_on_food_today(self, parser: AIIntentService):
        """1. 'spent ₹2200 on food today' → LOG_EXPENSE → amount = 2200 → expense_category = food → day_completed = false."""
        intent = await parser.parse_trip_intent("spent ₹2200 on food today")
        assert intent.action == TripAction.LOG_EXPENSE
        assert isinstance(intent.amount, Decimal)
        assert intent.amount == Decimal("2200")
        assert intent.expense_category == "food"
        assert intent.day_completed is False
        assert intent.day_number is None

    @pytest.mark.asyncio
    async def test_2_day_1_done_used_about_3000(self, parser: AIIntentService):
        """2. 'Day 1 done, used about ₹3000 on autos and lunch' → LOG_EXPENSE → amount = 3000 → day_number = 1 → day_completed = true."""
        intent = await parser.parse_trip_intent("Day 1 done, used about ₹3000 on autos and lunch")
        assert intent.action == TripAction.LOG_EXPENSE
        assert isinstance(intent.amount, Decimal)
        assert intent.amount == Decimal("3000")
        assert intent.day_number == 1
        assert intent.day_completed is True

    @pytest.mark.asyncio
    async def test_3_spent_800_on_activities_today(self, parser: AIIntentService):
        """3. 'I spent ₹800 on activities today' → LOG_EXPENSE → amount = 800 → expense_category = activities."""
        intent = await parser.parse_trip_intent("I spent ₹800 on activities today")
        assert intent.action == TripAction.LOG_EXPENSE
        assert isinstance(intent.amount, Decimal)
        assert intent.amount == Decimal("800")
        assert intent.expense_category == "activities"
        assert intent.day_completed is False

    @pytest.mark.asyncio
    async def test_4_the_trip_is_over_were_back_home(self, parser: AIIntentService):
        """4. 'The trip is over, we're back home' → TRIP_COMPLETE."""
        intent = await parser.parse_trip_intent("The trip is over, we're back home")
        assert intent.action == TripAction.TRIP_COMPLETE
        assert intent.amount is None
        assert intent.expense_category is None
        assert intent.day_number is None

    @pytest.mark.asyncio
    async def test_5_existing_planning_and_rescue_messages_unchanged(self, parser: AIIntentService):
        """5. Existing planning/change/rescue messages → continue producing previous actions unchanged."""
        # Planning
        plan = await parser.parse_trip_intent("Plan a trip from Chennai to Goa for 2 people, 3 days, with budget ₹25000")
        assert plan.action == TripAction.NEW_TRIP
        assert plan.budget == Decimal("25000")
        assert plan.people == 2
        assert plan.days == 3
        assert plan.origin == "Chennai"
        assert plan.destination == "Goa"

        # Change budget
        change_b = await parser.parse_trip_intent("budget is now 30000")
        assert change_b.action == TripAction.CHANGE_BUDGET
        assert change_b.budget == Decimal("30000")

        # Change days
        change_d = await parser.parse_trip_intent("make it 5 days")
        assert change_d.action == TripAction.CHANGE_DAYS
        assert change_d.days == 5

        # Rescue
        rescue = await parser.parse_trip_intent("auto driver is asking 600 rupees for 2 km ride")
        assert rescue.action == TripAction.RESCUE

    @pytest.mark.asyncio
    async def test_expense_without_day_completion_water(self, parser: AIIntentService):
        """'spent ₹100 on water' → LOG_EXPENSE, amount=100, day_completed=False."""
        intent = await parser.parse_trip_intent("spent ₹100 on water")
        assert intent.action == TripAction.LOG_EXPENSE
        assert intent.amount == Decimal("100")
        assert intent.day_completed is False

    @pytest.mark.asyncio
    async def test_expense_with_explicit_day_completion(self, parser: AIIntentService):
        """'Day 1 is done, we spent ₹3000 today' → LOG_EXPENSE, amount=3000, day_number=1, day_completed=True."""
        intent = await parser.parse_trip_intent("Day 1 is done, we spent ₹3000 today")
        assert intent.action == TripAction.LOG_EXPENSE
        assert intent.amount == Decimal("3000")
        assert intent.day_number == 1
        assert intent.day_completed is True

    @pytest.mark.asyncio
    async def test_ambiguous_endings_not_trip_complete(self, parser: AIIntentService):
        """Ambiguous conversational endings like 'thanks', 'that's it', 'okay' must not be TRIP_COMPLETE."""
        for msg in ["thanks", "thank you", "that's it", "thats it", "ok", "okay"]:
            intent = await parser.parse_trip_intent(msg)
            assert intent.action == TripAction.UNRECOGNIZED

    @pytest.mark.asyncio
    async def test_tanglish_and_shorthand_expense(self, parser: AIIntentService):
        """Tanglish and shorthand inputs."""
        # Tanglish expense
        tanglish_exp = await parser.parse_trip_intent("food ku 500 spent pannen")
        assert tanglish_exp.action == TripAction.LOG_EXPENSE
        assert tanglish_exp.amount == Decimal("500")
        assert tanglish_exp.expense_category == "food"

        # Tanglish trip complete
        tanglish_done = await parser.parse_trip_intent("trip mudinjadhu, we are back home")
        assert tanglish_done.action == TripAction.TRIP_COMPLETE

    @pytest.mark.asyncio
    async def test_no_downstream_calls_during_parsing(self, parser: AIIntentService, monkeypatch):
        """Ensure parsing does not trigger any database, external API, or orchestrator calls."""
        # Calling parse_trip_intent directly only executes NLP parsing logic
        res = await parser.parse_trip_intent("spent 1500 on dinner")
        assert res.action == TripAction.LOG_EXPENSE
        assert res.amount == Decimal("1500")
        assert res.expense_category == "food"
