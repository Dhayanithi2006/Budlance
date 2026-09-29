"""Offline deterministic tests for Phase 10 — Rescue / Replanning Mode.

Verifies all 21 architectural test requirements:
1. Active trip successfully loaded.
2. No active trip handled safely.
3. Weather rescue intent routed correctly.
4. Closure rescue intent routed correctly.
5. Price dispute routed correctly.
6. Unknown rescue request does not modify trip.
7. Weather/closure branch uses existing Cache/Fallback pipeline.
8. Rescue branch does not call SerpApi directly.
9. Alternative place is normalized correctly.
10. Alternative remains correctly labeled LIVE/CACHED/FALLBACK.
11. Estimated replacement cost remains ESTIMATED.
12. User-reported price remains USER_REPORTED.
13. Feasible replacement updates itinerary.
14. Feasible replacement updates ledger correctly.
15. Infeasible replacement is rejected.
16. Original itinerary remains intact after failed rescue.
17. Rescue event is persisted.
18. Existing reported spending is preserved.
19. Price-dispute calculation does not overwrite the user's reported amount.
20. Reverse-Budget Engine remains the final feasibility authority.
21. Existing Phase 1–9 tests continue to pass.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
import pytest

from budlance.ai.service import AIIntentService
from budlance.cache.manager import CacheFallbackManager
from budlance.db.models import BudgetAllocation, Itinerary, LedgerEntry, Trip, utc_now
from budlance.db.repositories.itinerary_repo import ItineraryRepository
from budlance.db.repositories.ledger_repo import LedgerRepository
from budlance.db.repositories.rescue_repo import RescueRepository
from budlance.db.repositories.trip_repo import TripRepository
from budlance.engine.budget import ReverseBudgetEngine
from budlance.estimation.estimator import EstimationLayer
from budlance.itinerary.models import GeneratedItinerary, ItineraryDay, ItineraryItem
from budlance.ledger.manager import VirtualLedgerManager
from budlance.normalization.normalizer import DataNormalizer
from budlance.rescue.service import RescueService
from budlance.schemas.travel import PlaceOption
from budlance.serpapi.models import DataSource, TravelDataEnvelope


@pytest.fixture
def repos():
    """In-memory repositories for deterministic offline testing."""
    return {
        "trip_repo": TripRepository(client=None),
        "itinerary_repo": ItineraryRepository(client=None),
        "ledger_repo": LedgerRepository(client=None),
        "rescue_repo": RescueRepository(client=None),
    }


@pytest.fixture
def active_trip_fixture(repos):
    """Create an active trip with baseline itinerary and ledger allocations."""
    user_id = uuid4()
    chat_id = 998877

    trip = repos["trip_repo"].create_trip(
        user_id=user_id,
        telegram_chat_id=chat_id,
        budget_total=Decimal("20000.00"),
        destination="Goa",
        origin="Mumbai",
        currency="INR",
        people_count=2,
        duration_days=3,
        is_active=True,
    )

    # Baseline Itinerary
    day1_items = [
        ItineraryItem(
            time_slot="Morning",
            activity="Visit Calangute Beach",
            place_name="Calangute Beach",
            category="beach",
            planned_cost=Decimal("0.00"),
            source=DataSource.LIVE,
        ),
        ItineraryItem(
            time_slot="Afternoon",
            activity="Lunch at Beach Shack",
            place_name="Beach Shack",
            category="food",
            planned_cost=Decimal("600.00"),
            source=DataSource.ESTIMATED,
        ),
    ]
    day1 = ItineraryDay(
        day_number=1,
        theme_or_summary="Arrival & Beach Day",
        items=day1_items,
        daily_estimated_cost=Decimal("600.00"),
    )
    itin_record = Itinerary(
        id=uuid4(),
        trip_id=trip.id,
        days=[day1.model_dump(mode="json")],
        is_feasible=True,
        feasibility_note="Initial feasible itinerary",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    repos["itinerary_repo"].save_itinerary(itin_record)

    # Baseline Budget Allocations (Total = 20,000)
    alloc = BudgetAllocation(
        id=uuid4(),
        trip_id=trip.id,
        transport_allocated=Decimal("6000.00"),
        stay_allocated=Decimal("8000.00"),
        food_allocated=Decimal("3000.00"),
        activities_discretionary=Decimal("1000.00"),
        rescue_fund_allocated=Decimal("2000.00"),
        total_budget=Decimal("20000.00"),
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    repos["ledger_repo"].save_budget_allocation(alloc)

    # Initial ledger entries
    repos["ledger_repo"].add_ledger_entry(
        LedgerEntry(
            id=uuid4(),
            trip_id=trip.id,
            category="activities",
            description="Activities Fund",
            allocated_amount=Decimal("1000.00"),
            planned_amount=Decimal("1000.00"),
            spent_amount=Decimal("0.00"),
            remaining_amount=Decimal("1000.00"),
            source="estimated",
            created_at=utc_now(),
        )
    )
    repos["ledger_repo"].add_ledger_entry(
        LedgerEntry(
            id=uuid4(),
            trip_id=trip.id,
            category="daily_survival",
            description="Local Transit Allowance",
            allocated_amount=Decimal("1000.00"),
            planned_amount=Decimal("1000.00"),
            spent_amount=Decimal("0.00"),
            remaining_amount=Decimal("1000.00"),
            source="estimated",
            created_at=utc_now(),
        )
    )

    return trip


@pytest.fixture
def mock_cache_manager():
    """Mock CacheFallbackManager returning deterministic Maps envelopes."""
    mgr = MagicMock(spec=CacheFallbackManager)

    def _make_envelope(source=DataSource.LIVE):
        return TravelDataEnvelope(
            source=source,
            engine="google_maps",
            query_hash="mock_maps_hash",
            data={
                "local_results": [
                    {
                        "title": "Goa Science Centre & Planetarium",
                        "type": "Museum",
                        "address": "Miramar, Panaji, Goa",
                        "rating": 4.5,
                        "reviews": 1200,
                    },
                    {
                        "title": "Houses of Goa Museum",
                        "type": "Museum",
                        "address": "Salvador do Mundo, Goa",
                        "rating": 4.6,
                        "reviews": 850,
                    },
                ]
            },
            is_fallback=(source == DataSource.FALLBACK),
        )

    mgr.get_travel_data = AsyncMock(return_value=_make_envelope(DataSource.LIVE))
    return mgr


# ============================================================================
# Tests
# ============================================================================

@pytest.mark.asyncio
async def test_active_trip_successfully_loaded(repos, active_trip_fixture, mock_cache_manager):
    """1. Active trip successfully loaded."""
    service = RescueService(
        trip_repo=repos["trip_repo"],
        itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"],
        rescue_repo=repos["rescue_repo"],
        cache_manager=mock_cache_manager,
    )

    result = await service.execute_rescue(
        chat_id=active_trip_fixture.telegram_chat_id,
        user_message="It is raining at Calangute Beach",
    )

    assert result.trip_id == active_trip_fixture.id
    assert result.success is True
    assert result.rescue_type == "weather_closure"


@pytest.mark.asyncio
async def test_no_active_trip_handled_safely(repos, mock_cache_manager):
    """2. No active trip handled safely without calling external services."""
    service = RescueService(
        trip_repo=repos["trip_repo"],
        itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"],
        rescue_repo=repos["rescue_repo"],
        cache_manager=mock_cache_manager,
    )

    result = await service.execute_rescue(
        chat_id=123456789,  # Unregistered chat
        user_message="It's pouring rain outside",
    )

    assert result.trip_id is None
    assert result.success is False
    assert result.error == "NO_ACTIVE_TRIP"
    mock_cache_manager.get_travel_data.assert_not_called()


@pytest.mark.asyncio
async def test_weather_rescue_intent_routed_correctly(repos, active_trip_fixture, mock_cache_manager):
    """3. Weather rescue intent routed correctly to weather_closure."""
    service = RescueService(
        trip_repo=repos["trip_repo"],
        itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"],
        rescue_repo=repos["rescue_repo"],
        cache_manager=mock_cache_manager,
    )

    result = await service.execute_rescue(
        chat_id=active_trip_fixture.telegram_chat_id,
        user_message="It's storming and heavy rain here",
    )

    assert result.rescue_type == "weather_closure"
    assert result.success is True


@pytest.mark.asyncio
async def test_closure_rescue_intent_routed_correctly(repos, active_trip_fixture, mock_cache_manager):
    """4. Closure rescue intent routed correctly to weather_closure."""
    service = RescueService(
        trip_repo=repos["trip_repo"],
        itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"],
        rescue_repo=repos["rescue_repo"],
        cache_manager=mock_cache_manager,
    )

    result = await service.execute_rescue(
        chat_id=active_trip_fixture.telegram_chat_id,
        user_message="The monument is closed today for renovation",
    )

    assert result.rescue_type == "weather_closure"
    assert result.success is True


@pytest.mark.asyncio
async def test_price_dispute_routed_correctly(repos, active_trip_fixture, mock_cache_manager):
    """5. Price dispute routed correctly to price_dispute branch."""
    service = RescueService(
        trip_repo=repos["trip_repo"],
        itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"],
        rescue_repo=repos["rescue_repo"],
        cache_manager=mock_cache_manager,
    )

    result = await service.execute_rescue(
        chat_id=active_trip_fixture.telegram_chat_id,
        user_message="The auto driver is asking ₹500",
    )

    assert result.rescue_type == "price_dispute"
    assert result.success is True
    assert result.fare_guidance is not None
    assert result.fare_guidance.reported_price == Decimal("500")
    # Verify no SerpApi / cache query was made for price dispute
    mock_cache_manager.get_travel_data.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_rescue_request_does_not_modify_trip(repos, active_trip_fixture, mock_cache_manager):
    """6. Unknown rescue request does not modify trip, itinerary, or ledger."""
    service = RescueService(
        trip_repo=repos["trip_repo"],
        itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"],
        rescue_repo=repos["rescue_repo"],
        cache_manager=mock_cache_manager,
    )

    itin_before = repos["itinerary_repo"].get_itinerary(active_trip_fixture.id)
    ledger_entries_before = len(repos["ledger_repo"].get_ledger_entries(active_trip_fixture.id))

    result = await service.execute_rescue(
        chat_id=active_trip_fixture.telegram_chat_id,
        user_message="I would like some ice cream and chocolate",
    )

    assert result.rescue_type == "unknown"
    assert result.success is False

    # Itinerary and ledger unchanged
    itin_after = repos["itinerary_repo"].get_itinerary(active_trip_fixture.id)
    assert itin_after.days == itin_before.days
    assert len(repos["ledger_repo"].get_ledger_entries(active_trip_fixture.id)) == ledger_entries_before
    mock_cache_manager.get_travel_data.assert_not_called()


@pytest.mark.asyncio
async def test_weather_closure_uses_cache_fallback_pipeline(repos, active_trip_fixture, mock_cache_manager):
    """7. Weather/closure branch uses existing Cache/Fallback pipeline."""
    service = RescueService(
        trip_repo=repos["trip_repo"],
        itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"],
        rescue_repo=repos["rescue_repo"],
        cache_manager=mock_cache_manager,
    )

    await service.execute_rescue(
        chat_id=active_trip_fixture.telegram_chat_id,
        user_message="It's raining heavily",
    )

    # Verified CacheFallbackManager.get_travel_data was called with google_maps
    mock_cache_manager.get_travel_data.assert_called_once()
    call_args = mock_cache_manager.get_travel_data.call_args[1]
    assert call_args["engine"] == "google_maps"
    assert call_args["trip_id"] == active_trip_fixture.id


@pytest.mark.asyncio
async def test_rescue_branch_does_not_call_serpapi_directly(repos, active_trip_fixture, mock_cache_manager):
    """8. Rescue branch does not call SerpApi directly."""
    service = RescueService(
        trip_repo=repos["trip_repo"],
        itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"],
        rescue_repo=repos["rescue_repo"],
        cache_manager=mock_cache_manager,
    )

    # Ensure RescueService has no SerpApiGateway attribute and only uses CacheFallbackManager
    assert not hasattr(service, "gateway")
    assert not hasattr(service, "serpapi_gateway")

    await service.execute_rescue(
        chat_id=active_trip_fixture.telegram_chat_id,
        user_message="The beach is closed due to storm",
    )

    # Verifies only CacheFallbackManager was engaged
    mock_cache_manager.get_travel_data.assert_called_once()


@pytest.mark.asyncio
async def test_alternative_place_normalized_correctly(repos, active_trip_fixture, mock_cache_manager):
    """9. Alternative place is normalized through existing DataNormalizer."""
    service = RescueService(
        trip_repo=repos["trip_repo"],
        itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"],
        rescue_repo=repos["rescue_repo"],
        cache_manager=mock_cache_manager,
    )

    result = await service.execute_rescue(
        chat_id=active_trip_fixture.telegram_chat_id,
        user_message="Raining at beach",
    )

    assert result.selected_alternative is not None
    assert isinstance(result.selected_alternative, PlaceOption)
    assert result.selected_alternative.name in [
        "Goa Science Centre & Planetarium",
        "Houses of Goa Museum",
    ]


@pytest.mark.asyncio
async def test_alternative_source_provenance_preservation(repos, active_trip_fixture):
    """10. Alternative remains correctly labeled LIVE / CACHED / FALLBACK."""
    # Test with CACHED envelope
    cached_manager = MagicMock(spec=CacheFallbackManager)
    cached_manager.get_travel_data = AsyncMock(
        return_value=TravelDataEnvelope(
            source=DataSource.CACHED,
            engine="google_maps",
            query_hash="cached_hash",
            data={
                "local_results": [
                    {"title": "Goa Chitra Museum", "type": "Museum", "rating": 4.7}
                ]
            },
            is_fallback=False,
        )
    )

    service = RescueService(
        trip_repo=repos["trip_repo"],
        itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"],
        rescue_repo=repos["rescue_repo"],
        cache_manager=cached_manager,
    )

    result = await service.execute_rescue(
        chat_id=active_trip_fixture.telegram_chat_id,
        user_message="Raining heavily",
    )

    assert result.selected_alternative.source == DataSource.CACHED
    # Also check the updated itinerary item retains CACHED
    day1_items = result.updated_itinerary.days[0].items
    replaced_item = next(i for i in day1_items if i.place_name == "Goa Chitra Museum")
    assert replaced_item.source == DataSource.CACHED


@pytest.mark.asyncio
async def test_estimated_replacement_cost_remains_estimated(repos, active_trip_fixture, mock_cache_manager):
    """11. Estimated replacement cost in ledger remains labeled estimated."""
    service = RescueService(
        trip_repo=repos["trip_repo"],
        itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"],
        rescue_repo=repos["rescue_repo"],
        cache_manager=mock_cache_manager,
    )

    result = await service.execute_rescue(
        chat_id=active_trip_fixture.telegram_chat_id,
        user_message="Bad weather outside",
    )

    assert result.success is True
    entries = repos["ledger_repo"].get_ledger_entries(active_trip_fixture.id)
    rescue_entries = [e for e in entries if "Rescue replacement" in e.description]
    assert len(rescue_entries) > 0
    assert rescue_entries[-1].source == "estimated"


@pytest.mark.asyncio
async def test_user_reported_price_remains_user_reported(repos, active_trip_fixture):
    """12. User-reported price in dispute remains labeled user_reported."""
    service = RescueService(
        trip_repo=repos["trip_repo"],
        itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"],
        rescue_repo=repos["rescue_repo"],
    )

    result = await service.execute_rescue(
        chat_id=active_trip_fixture.telegram_chat_id,
        user_message="The auto driver is asking ₹450",
    )

    assert result.success is True
    entries = repos["ledger_repo"].get_ledger_entries(active_trip_fixture.id)
    reported_entry = next(e for e in entries if e.spent_amount == Decimal("450"))
    assert reported_entry.source == "user_reported"


@pytest.mark.asyncio
async def test_feasible_replacement_updates_itinerary(repos, active_trip_fixture, mock_cache_manager):
    """13. Feasible replacement updates the itinerary item in repository."""
    service = RescueService(
        trip_repo=repos["trip_repo"],
        itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"],
        rescue_repo=repos["rescue_repo"],
        cache_manager=mock_cache_manager,
    )

    result = await service.execute_rescue(
        chat_id=active_trip_fixture.telegram_chat_id,
        user_message="It's raining at Calangute Beach",
    )

    assert result.success is True
    saved_itin = repos["itinerary_repo"].get_itinerary(active_trip_fixture.id)
    items = saved_itin.days[0]["items"]
    # Calangute Beach replaced with indoor museum
    place_names = [i["place_name"] for i in items]
    assert "Calangute Beach" not in place_names
    assert any("Museum" in name or "Science" in name for name in place_names)


@pytest.mark.asyncio
async def test_feasible_replacement_updates_ledger_correctly(repos, active_trip_fixture, mock_cache_manager):
    """14. Feasible replacement updates ledger planned figures."""
    service = RescueService(
        trip_repo=repos["trip_repo"],
        itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"],
        rescue_repo=repos["rescue_repo"],
        cache_manager=mock_cache_manager,
    )

    result = await service.execute_rescue(
        chat_id=active_trip_fixture.telegram_chat_id,
        user_message="Rain at beach",
    )

    assert result.ledger_summary is not None
    assert result.ledger_summary.total_budget == Decimal("20000.00")
    # Invariant holds
    assert result.ledger_summary.total_allocated <= Decimal("20000.00")


@pytest.mark.asyncio
async def test_infeasible_replacement_is_rejected(repos, active_trip_fixture):
    """15. Infeasible replacement exceeding rescue fund is rejected."""
    # Mock candidate place with massive cost that exceeds the 2,000 rescue reserve
    expensive_manager = MagicMock(spec=CacheFallbackManager)
    expensive_manager.get_travel_data = AsyncMock(
        return_value=TravelDataEnvelope(
            source=DataSource.LIVE,
            engine="google_maps",
            query_hash="expensive_hash",
            data={
                "local_results": [
                    {"title": "Luxury Yacht Charter", "type": "Luxury Tour", "rating": 5.0}
                ]
            },
            is_fallback=False,
        )
    )

    # Custom normalizer that tags high estimated_cost
    mock_normalizer = MagicMock(spec=DataNormalizer)
    mock_normalizer.normalize_places.return_value = [
        PlaceOption(
            name="Luxury Yacht Charter",
            category="Luxury Tour",
            rating=5.0,
            estimated_cost=Decimal("5000.00"),  # Exceeds the 2,000 rescue fund!
            source=DataSource.LIVE,
        )
    ]

    service = RescueService(
        trip_repo=repos["trip_repo"],
        itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"],
        rescue_repo=repos["rescue_repo"],
        cache_manager=expensive_manager,
        normalizer=mock_normalizer,
    )

    result = await service.execute_rescue(
        chat_id=active_trip_fixture.telegram_chat_id,
        user_message="Raining at beach",
    )

    assert result.is_feasible is False
    assert result.success is False
    assert "exceeds" in result.resolution_summary.lower()
    assert result.updated_itinerary is None


@pytest.mark.asyncio
async def test_original_itinerary_intact_after_failed_rescue(repos, active_trip_fixture):
    """16. Original itinerary remains intact after failed rescue."""
    expensive_manager = MagicMock(spec=CacheFallbackManager)
    expensive_manager.get_travel_data = AsyncMock(
        return_value=TravelDataEnvelope(
            source=DataSource.LIVE,
            engine="google_maps",
            query_hash="expensive_hash",
            data={"local_results": [{"title": "Super Expensive Club", "rating": 5.0}]},
            is_fallback=False,
        )
    )
    mock_normalizer = MagicMock(spec=DataNormalizer)
    mock_normalizer.normalize_places.return_value = [
        PlaceOption(
            name="Super Expensive Club",
            estimated_cost=Decimal("10000.00"),
            source=DataSource.LIVE,
        )
    ]

    service = RescueService(
        trip_repo=repos["trip_repo"],
        itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"],
        rescue_repo=repos["rescue_repo"],
        cache_manager=expensive_manager,
        normalizer=mock_normalizer,
    )

    itin_before = repos["itinerary_repo"].get_itinerary(active_trip_fixture.id)
    before_places = [i["place_name"] for i in itin_before.days[0]["items"]]

    result = await service.execute_rescue(
        chat_id=active_trip_fixture.telegram_chat_id,
        user_message="Rain at beach",
    )

    assert result.success is False
    itin_after = repos["itinerary_repo"].get_itinerary(active_trip_fixture.id)
    after_places = [i["place_name"] for i in itin_after.days[0]["items"]]

    assert before_places == after_places
    assert "Calangute Beach" in after_places


@pytest.mark.asyncio
async def test_rescue_event_persisted(repos, active_trip_fixture, mock_cache_manager):
    """17. Rescue event is persisted into RescueRepository."""
    service = RescueService(
        trip_repo=repos["trip_repo"],
        itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"],
        rescue_repo=repos["rescue_repo"],
        cache_manager=mock_cache_manager,
    )

    await service.execute_rescue(
        chat_id=active_trip_fixture.telegram_chat_id,
        user_message="The monument is closed today",
    )

    events = repos["rescue_repo"].get_rescue_events(active_trip_fixture.id)
    assert len(events) == 1
    event = events[0]
    assert event.rescue_type == "weather_closure"
    assert "closed" in event.user_message.lower()
    assert event.trip_id == active_trip_fixture.id


@pytest.mark.asyncio
async def test_existing_reported_spending_preserved(repos, active_trip_fixture, mock_cache_manager):
    """18. Existing reported spending is preserved after rescue."""
    # First record some user spending
    ledger_mgr = VirtualLedgerManager(repos["ledger_repo"])
    ledger_mgr.record_spending(
        trip_id=active_trip_fixture.id,
        category="daily_survival",
        amount=Decimal("350.00"),
        description="Breakfast cafe",
        source="user_reported",
    )

    service = RescueService(
        trip_repo=repos["trip_repo"],
        itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"],
        rescue_repo=repos["rescue_repo"],
        cache_manager=mock_cache_manager,
        ledger_manager=ledger_mgr,
    )

    # Perform weather rescue
    await service.execute_rescue(
        chat_id=active_trip_fixture.telegram_chat_id,
        user_message="It's raining outside",
    )

    # Verify original spending entry of 350 is intact
    summary = ledger_mgr.get_summary(active_trip_fixture.id)
    assert summary.total_spent >= Decimal("350.00")
    matching = [e for e in summary.entries if e.description == "Breakfast cafe"]
    assert len(matching) == 1
    assert matching[0].spent_amount == Decimal("350.00")
    assert matching[0].source == "user_reported"


@pytest.mark.asyncio
async def test_price_dispute_does_not_overwrite_user_reported_amount(repos, active_trip_fixture):
    """19. Price dispute does not overwrite user's reported amount with estimated."""
    service = RescueService(
        trip_repo=repos["trip_repo"],
        itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"],
        rescue_repo=repos["rescue_repo"],
    )

    result = await service.execute_rescue(
        chat_id=active_trip_fixture.telegram_chat_id,
        user_message="The auto driver is asking ₹750",
    )

    assert result.fare_guidance.reported_price == Decimal("750")
    # Verify the estimated fare is calculated independently
    assert result.fare_guidance.estimated_fare < Decimal("750")

    # Verify ledger recorded exactly 750, not estimated fare
    entries = repos["ledger_repo"].get_ledger_entries(active_trip_fixture.id)
    fare_entry = next(e for e in entries if "Reported auto fare" in e.description)
    assert fare_entry.spent_amount == Decimal("750.00")
    assert fare_entry.source == "user_reported"


@pytest.mark.asyncio
async def test_reverse_budget_engine_authoritative_decision(repos, active_trip_fixture):
    """20. Reverse-Budget Engine remains the final feasibility authority."""
    budget_engine = ReverseBudgetEngine()
    alloc = repos["ledger_repo"].get_budget_allocation(active_trip_fixture.id)

    # Cost delta within rescue reserve (2,000)
    feasible_eval = budget_engine.evaluate_rescue(
        total_budget=Decimal("20000.00"),
        current_allocations=alloc,
        cost_delta=Decimal("500.00"),
    )
    assert feasible_eval.is_feasible is True
    assert feasible_eval.status == "FEASIBLE"

    # Cost delta exceeding rescue reserve (2,000)
    infeasible_eval = budget_engine.evaluate_rescue(
        total_budget=Decimal("20000.00"),
        current_allocations=alloc,
        cost_delta=Decimal("3500.00"),
    )
    assert infeasible_eval.is_feasible is False
    assert infeasible_eval.status == "NOT_FEASIBLE"
    assert infeasible_eval.deficit == Decimal("1500.00")
