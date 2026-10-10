"""Regression tests for live provider migration and hardcoded travel data elimination.

Verifies:
- Live mode never injects _CURATED_DOMESTIC_POOL into candidate discovery.
- Offline mode preserves _CURATED_DOMESTIC_POOL for zero-network test suite execution.
- Hotel pricing correctly computes nights * rate_per_night when total_rate is absent.
- Attraction entry fee preserves unknown state (entry_fee_inr=None, is_fee_unknown=True).
- Formatter never displays unknown attraction fee as ₹0.
- Events normalizer safely parses Google Search events_results without fabricating events.
- Live mode forbids silent domestic rail fallback for unsupported flight routes.
- Location query helpers produce correct queries for food and events.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
import pytest

from budlance.attractions.models import Attraction
from budlance.attractions.selector import AttractionSelector
from budlance.normalization.events import normalize_events
from budlance.normalization.hotels import normalize_hotels
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.schemas.travel import EventOption
from budlance.serpapi.location import (
    resolve_events_query,
    resolve_food_query,
    resolve_iata,
)
from budlance.serpapi.models import DataSource, TravelDataEnvelope


# =============================================================================
# 1. Candidate Destination Pool Gating (Live vs. Offline)
# =============================================================================

@pytest.mark.asyncio
async def test_live_mode_excludes_curated_domestic_pool():
    """In LIVE mode, _CURATED_DOMESTIC_POOL must NOT be injected into candidates."""
    orch = BudlanceOrchestrator()
    # Mock gateway with is_live_mode=True
    orch.cache_manager.gateway = MagicMock()
    orch.cache_manager.gateway.is_live_mode = True

    # Empty travel explore results
    orch.cache_manager.get_travel_data = AsyncMock(
        return_value=TravelDataEnvelope(
            source=DataSource.LIVE,
            engine="google_travel_explore",
            query_hash="hash_explore",
            data={"destinations": []},
        )
    )

    candidates, has_curated = await orch._discover_destinations(
        origin="Chennai",
        interests=["beach"],
        budget=Decimal("50000"),
    )

    # In live mode with 0 live destinations returned, candidates must be empty
    assert candidates == []
    assert has_curated is False


@pytest.mark.asyncio
async def test_offline_mode_preserves_curated_domestic_pool():
    """In OFFLINE mode (tests/sandbox), _CURATED_DOMESTIC_POOL must provide offline fallbacks."""
    orch = BudlanceOrchestrator()
    # Mock gateway with is_live_mode=False
    orch.cache_manager.gateway = MagicMock()
    orch.cache_manager.gateway.is_live_mode = False
    orch.cache_manager.gateway.has_credentials = False

    # Empty travel explore results
    orch.cache_manager.get_travel_data = AsyncMock(
        return_value=TravelDataEnvelope(
            source=DataSource.FALLBACK,
            engine="google_travel_explore",
            query_hash="hash_explore",
            data={"destinations": []},
            is_fallback=True,
        )
    )

    candidates, has_curated = await orch._discover_destinations(
        origin="Chennai",
        interests=["beach"],
        budget=Decimal("50000"),
    )

    # In offline mode, curated pool supplies offline candidates (e.g. Goa)
    assert len(candidates) > 0
    assert "Goa" in candidates
    assert has_curated is True


# =============================================================================
# 2. Hotel Pricing Semantics: Nights Multiplier
# =============================================================================

def test_hotel_normalization_multiplies_rate_by_nights():
    """When total_rate is absent but rate_per_night exists, total_price = price_per_night * nights."""
    envelope = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_hotels",
        query_hash="hash_hotel",
        data={
            "properties": [
                {
                    "name": "Ocean View Resort",
                    "rate_per_night": {"extracted_lowest": 4500},
                    "overall_rating": 4.5,
                }
            ]
        },
    )

    # 4 nights stay
    hotels = normalize_hotels(envelope, nights=4)
    assert len(hotels) == 1
    assert hotels[0].price_per_night == Decimal("4500")
    assert hotels[0].total_price == Decimal("18000")  # 4500 * 4


def test_hotel_normalization_uses_total_rate_when_present():
    """When total_rate is explicitly provided by the provider, it takes precedence."""
    envelope = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_hotels",
        query_hash="hash_hotel",
        data={
            "properties": [
                {
                    "name": "Luxury Palace",
                    "rate_per_night": {"extracted_lowest": 5000},
                    "total_rate": {"extracted_lowest": 14000},  # e.g. discounted 3-night package
                }
            ]
        },
    )

    hotels = normalize_hotels(envelope, nights=3)
    assert len(hotels) == 1
    assert hotels[0].price_per_night == Decimal("5000")
    assert hotels[0].total_price == Decimal("14000")


# =============================================================================
# 3. Attractions & Preserving Unknown Entry Fee
# =============================================================================

def test_attraction_selector_converts_live_places_and_preserves_unknown_fee():
    """AttractionSelector converts live provider places and marks unknown admission fee as None."""
    selector = AttractionSelector()
    live_places = [
        {
            "name": "Calangute Beach Promenade",
            "category": "beach",
            "address": "Calangute, Goa",
            "price_level": None,  # Unknown
        },
        {
            "name": "Fort Aguada",
            "category": "historical_landmark",
            "address": "Sinquerim, Goa",
            "price_level": "free",  # Explicitly free
        },
    ]

    attractions = selector.select_for_itinerary(
        destination="Goa",
        travel_party="friends",
        interests=["beach"],
        days=2,
        places=live_places,
    )

    assert len(attractions) >= 2
    # First place: fee unknown
    beach_attr = next(a for a in attractions if a.name == "Calangute Beach Promenade")
    assert beach_attr.entry_fee_inr is None
    assert beach_attr.is_fee_unknown is True
    assert beach_attr.source == "LIVE_PROVIDER"

    # Second place: explicitly free
    fort_attr = next(a for a in attractions if a.name == "Fort Aguada")
    assert fort_attr.entry_fee_inr == 0
    assert fort_attr.is_fee_unknown is False
    assert fort_attr.source == "LIVE_PROVIDER"


def test_formatter_does_not_display_unknown_fee_as_zero():
    """Feasible plan formatter must not display unknown admission as '₹0'."""
    from budlance.engine.models import BudgetBreakdown
    from budlance.itinerary.models import GeneratedItinerary, ItineraryDay, ItineraryItem
    from budlance.orchestrator.formatter import format_feasible_plan

    item_unknown = ItineraryItem(
        time_slot="Morning",
        activity="Visit Anjuna Flea Market",
        category="attraction",
        planned_cost=Decimal("0.00"),
        entry_fee_inr=None,
        is_fee_unknown=True,
    )

    item_paid = ItineraryItem(
        time_slot="Afternoon",
        activity="Entry to Heritage Museum",
        category="attraction",
        planned_cost=Decimal("250.00"),
        entry_fee_inr=250,
        is_fee_unknown=False,
    )

    from uuid import uuid4
    itin = GeneratedItinerary(
        trip_id=uuid4(),
        destination="Goa",
        days_count=2,
        total_budget=Decimal("50000.00"),
        days=[
            ItineraryDay(
                day_number=1,
                theme_or_summary="Coastal Exploration",
                items=[item_unknown, item_paid],
                daily_estimated_cost=Decimal("250.00"),
            )
        ],
        total_estimated_cost=Decimal("250.00"),
    )

    breakdown = BudgetBreakdown(
        total_budget=Decimal("50000.00"),
        bucket_a_fixed=Decimal("20000.00"),
        bucket_b_survival=Decimal("15000.00"),
        bucket_c_activities=Decimal("5000.00"),
        bucket_d_rescue=Decimal("5000.00"),
        transport_cost=Decimal("10000.00"),
        hotel_cost=Decimal("10000.00"),
        food_cost=Decimal("10000.00"),
        local_transit_cost=Decimal("5000.00"),
        attraction_cost=Decimal("250.00"),
        total_allocated=Decimal("40250.00"),
        remaining_surplus=Decimal("9750.00"),
    )

    formatted = format_feasible_plan(
        destination="Goa",
        days=2,
        people=2,
        breakdown=breakdown,
        transport=None,
        hotel=None,
        itinerary=itin,
        ledger=None,
    )

    # The unknown fee must NOT be rendered as "₹0"
    assert "₹0" not in formatted
    # Must be rendered as admission not included
    assert "[admission not included]" in formatted
    assert "[₹250]" in formatted


# =============================================================================
# 4. Live Events Normalization
# =============================================================================

def test_normalize_events_parses_serpapi_events_results():
    """Event normalizer correctly maps Google Search events_results."""
    envelope = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google",
        query_hash="hash_events",
        data={
            "events_results": [
                {
                    "title": "Sunburn Festival Goa 2026",
                    "date": {"start_date": "Dec 27", "when": "Dec 27 - 29"},
                    "address": ["Vagator Beach, Goa"],
                    "link": "https://example.com/sunburn",
                    "description": "Annual electronic dance music festival.",
                    "ticket_info": [{"source": "BookMyShow", "link": "https://example.com/tickets"}],
                }
            ]
        },
    )

    events = normalize_events(envelope)
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, EventOption)
    assert ev.name == "Sunburn Festival Goa 2026"
    assert ev.date_str == "Dec 27 - 29"
    assert ev.address == "Vagator Beach, Goa"
    assert ev.link == "https://example.com/sunburn"
    assert ev.source == DataSource.LIVE
    assert ev.is_fallback is False


def test_normalize_events_empty_results_does_not_fabricate():
    """When no events are found, return empty list without fabricating synthetic events."""
    envelope = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google",
        query_hash="hash_events_empty",
        data={"events_results": []},
    )

    events = normalize_events(envelope)
    assert events == []


# =============================================================================
# 5. Live Mode Safe Transport Gating (No Silent Rail Fallback)
# =============================================================================

@pytest.mark.asyncio
async def test_live_mode_forbids_rail_fallback_for_unsupported_flight_route():
    """In LIVE mode, an unsupported airport must NOT fall back to domestic rail unless requested."""
    orch = BudlanceOrchestrator()
    orch.cache_manager.gateway = MagicMock()
    orch.cache_manager.gateway.is_live_mode = True

    # User did not specify mode (default flight exploration), but no IATA code exists for destination
    # e.g., destination 'Ooty' has no IATA code
    transports = await orch.lookup_transport_options(
        origin="Chennai",
        destination="Ooty",
        people=2,
        transport_mode=None,  # default flight exploration
    )

    # In LIVE mode: no flights available and train fallback is forbidden for unrequested mode
    assert transports == []


@pytest.mark.asyncio
async def test_offline_mode_permits_rail_fallback_for_tests():
    """In OFFLINE mode, domestic rail fallback remains available for existing test suites."""
    orch = BudlanceOrchestrator()
    orch.cache_manager.gateway = MagicMock()
    orch.cache_manager.gateway.is_live_mode = False

    transports = await orch.lookup_transport_options(
        origin="Chennai",
        destination="Ooty",
        people=2,
        transport_mode=None,
    )

    # In offline mode, train/bus corridor is found
    assert len(transports) > 0
    assert any(t.transit_type in ("train", "bus") for t in transports)


# =============================================================================
# 6. Food & Event Query Resolution
# =============================================================================

def test_resolve_food_and_event_queries():
    """Verify live search query strings for food and seasonal events."""
    food_q1 = resolve_food_query("Goa", interest="seafood")
    assert "seafood in Goa" in food_q1

    food_q2 = resolve_food_query("Jaipur", interest="local cuisine")
    assert "local cuisine in Jaipur" in food_q2

    food_q_default = resolve_food_query("Kochi")
    assert food_q_default == "local food restaurants in Kochi"

    events_q1 = resolve_events_query("Goa")
    assert events_q1 == "events in Goa"

    events_q2 = resolve_events_query("Delhi", date_or_season="December")
    assert events_q2 == "events in Delhi December"

    # IATA resolution
    assert resolve_iata("BOM") == "BOM"
    assert resolve_iata("Mumbai") == "BOM"
    assert resolve_iata("NonExistentCity12345") is None
