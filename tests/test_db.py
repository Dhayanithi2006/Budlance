"""Tests for Supabase PostgreSQL persistence models and repositories."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
import pytest
from pydantic import ValidationError

from budlance.db.client import get_supabase_client, is_database_connected
from budlance.db.models import (
    User,
    Trip,
    TripIntent,
    TripOption,
    FlightOption,
    HotelOption,
    PlaceOption,
    Itinerary,
    BudgetAllocation,
    LedgerEntry,
    PlanAttempt,
    RescueEvent,
    SearchCache,
    ApiUsage,
)
from budlance.db.repositories import (
    UserRepository,
    TripRepository,
    IntentRepository,
    ItineraryRepository,
    LedgerRepository,
    AttemptRepository,
    RescueRepository,
    CacheRepository,
    UsageRepository,
)


def test_supabase_client_unconfigured(monkeypatch):
    """Verify client handles missing credentials gracefully."""
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setenv("SUPABASE_KEY", "")
    get_supabase_client.cache_clear()

    client = get_supabase_client()
    assert client is None
    assert is_database_connected() is False


def test_all_14_domain_models_instantiation():
    """Verify that all 14 frozen schema entities instantiate and validate properly."""
    uid = uuid4()
    trip_id = uuid4()
    option_id = uuid4()
    now = datetime.now(timezone.utc)

    # 1. User
    user = User(id=uid, telegram_user_id=12345678, username="traveler1", first_name="Alice")
    assert user.telegram_user_id == 12345678

    # 2. Trip
    trip = Trip(
        id=trip_id,
        user_id=uid,
        telegram_chat_id=12345678,
        destination="Goa",
        origin="Mumbai",
        budget_total=Decimal("25000.00"),
        people_count=2,
        duration_days=4,
    )
    assert trip.is_active is True
    assert trip.status == "planning"

    # 3. TripIntent
    intent = TripIntent(
        trip_id=trip_id,
        budget=Decimal("25000.00"),
        people=2,
        days=4,
        destination="Goa",
        interests=["beaches", "seafood"],
    )
    assert "beaches" in intent.interests

    # 4. TripOption
    option = TripOption(id=option_id, trip_id=trip_id, total_estimated_cost=Decimal("22000.00"))
    assert option.option_tier == "standard"

    # 5. FlightOption
    flight = FlightOption(
        trip_option_id=option_id,
        airline="IndiGo",
        flight_number="6E-101",
        price=Decimal("4500.00"),
    )
    assert flight.airline == "IndiGo"

    # 6. HotelOption
    hotel = HotelOption(
        trip_option_id=option_id,
        name="Seaside Resort",
        hotel_class=3,
        price_per_night=Decimal("2500.00"),
        total_price=Decimal("7500.00"),
    )
    assert hotel.hotel_class == 3

    # 7. PlaceOption
    place = PlaceOption(
        trip_option_id=option_id,
        name="Baga Beach",
        category="beach",
        estimated_cost=Decimal("0.00"),
    )
    assert place.category == "beach"

    # 8. Itinerary
    itinerary = Itinerary(
        trip_id=trip_id,
        days=[{"day": 1, "activity": "Arrival and beach sunset"}],
        is_feasible=True,
    )
    assert len(itinerary.days) == 1

    # 9. BudgetAllocation
    alloc = BudgetAllocation(
        trip_id=trip_id,
        transport_allocated=Decimal("9000.00"),
        stay_allocated=Decimal("7500.00"),
        food_allocated=Decimal("4000.00"),
        activities_discretionary=Decimal("2500.00"),
        rescue_fund_allocated=Decimal("2000.00"),
        total_budget=Decimal("25000.00"),
    )
    assert alloc.total_budget == Decimal("25000.00")

    # 10. LedgerEntry
    ledger = LedgerEntry(
        trip_id=trip_id,
        category="fixed_booking",
        description="Hotel booking",
        allocated_amount=Decimal("7500.00"),
        planned_amount=Decimal("7200.00"),
        source="live",
    )
    assert ledger.source == "live"

    # 11. PlanAttempt
    attempt = PlanAttempt(
        trip_id=trip_id,
        attempt_number=1,
        downgrade_type="hotel_tier_down",
        was_feasible=True,
        cost_calculated=Decimal("24500.00"),
    )
    assert attempt.attempt_number == 1

    # 12. RescueEvent
    rescue = RescueEvent(
        trip_id=trip_id,
        rescue_type="weather_closure",
        user_message="It's pouring rain outside",
        resolution_summary="Swapped beach morning for indoor museum tour",
    )
    assert rescue.rescue_type == "weather_closure"

    # 13. SearchCache
    cache = SearchCache(
        query_hash="hash_12345",
        engine="google_flights",
        params_json={"origin": "BOM", "destination": "GOI"},
        response_data={"flights": []},
        expires_at=now + timedelta(hours=6),
    )
    assert cache.engine == "google_flights"

    # 14. ApiUsage
    usage = ApiUsage(trip_id=trip_id, engine="google_hotels", call_count=1)
    assert usage.call_count == 1


def test_plan_attempt_validation_range():
    """Verify attempt_number must be between 1 and 4 per frozen optimization specification."""
    trip_id = uuid4()
    # Valid attempt
    attempt = PlanAttempt(
        trip_id=trip_id,
        attempt_number=4,
        downgrade_type="trim_discretionary_b",
        was_feasible=False,
        cost_calculated=Decimal("30000.00"),
    )
    assert attempt.attempt_number == 4

    # Invalid attempt > 4
    with pytest.raises(ValidationError):
        PlanAttempt(
            trip_id=trip_id,
            attempt_number=5,
            downgrade_type="trim_discretionary_b",
            was_feasible=False,
            cost_calculated=Decimal("30000.00"),
        )


def test_user_and_trip_repository_flow():
    """Verify User -> Trip association and active trip retrieval for Rescue Mode."""
    user_repo = UserRepository(client=None)
    trip_repo = TripRepository(client=None)

    # 1. Create or get user
    user = user_repo.get_or_create_user(telegram_user_id=987654321, username="test_bot_user", first_name="Bob")
    assert user.telegram_user_id == 987654321

    # Idempotent retrieval
    same_user = user_repo.get_or_create_user(telegram_user_id=987654321)
    assert same_user.id == user.id

    # 2. Create first active trip
    trip1 = trip_repo.create_trip(
        user_id=user.id,
        telegram_chat_id=987654321,
        budget_total=Decimal("20000.00"),
        destination="Kerala",
        duration_days=5,
        is_active=True,
    )
    assert trip1.is_active is True

    # Active trip should be trip1
    active_trip = trip_repo.get_active_trip(telegram_chat_id=987654321)
    assert active_trip is not None
    assert active_trip.id == trip1.id

    # 3. Create second active trip (replaces first as active)
    trip2 = trip_repo.create_trip(
        user_id=user.id,
        telegram_chat_id=987654321,
        budget_total=Decimal("15000.00"),
        destination="Ooty",
        duration_days=3,
        is_active=True,
    )

    # First trip must be deactivated; second trip is now the active trip
    active_trip = trip_repo.get_active_trip(telegram_chat_id=987654321)
    assert active_trip is not None
    assert active_trip.id == trip2.id

    old_trip = trip_repo.get_trip(trip1.id)
    assert old_trip is not None
    assert old_trip.is_active is False


def test_ledger_and_attempts_repositories():
    """Verify ledger allocations, line items, and plan attempts persistence."""
    trip_id = uuid4()
    ledger_repo = LedgerRepository(client=None)
    attempt_repo = AttemptRepository(client=None)

    # Budget Allocation
    allocation = BudgetAllocation(
        trip_id=trip_id,
        transport_allocated=Decimal("6000.00"),
        stay_allocated=Decimal("5000.00"),
        food_allocated=Decimal("3000.00"),
        rescue_fund_allocated=Decimal("1000.00"),
        total_budget=Decimal("15000.00"),
    )
    saved_alloc = ledger_repo.save_budget_allocation(allocation)
    fetched_alloc = ledger_repo.get_budget_allocation(trip_id)
    assert fetched_alloc is not None
    assert fetched_alloc.transport_allocated == Decimal("6000.00")

    # Ledger entry
    entry = LedgerEntry(
        trip_id=trip_id,
        category="daily_survival",
        description="Local auto fare estimate",
        allocated_amount=Decimal("500.00"),
        source="estimated",
    )
    ledger_repo.add_ledger_entry(entry)
    entries = ledger_repo.get_ledger_entries(trip_id)
    assert len(entries) == 1
    assert entries[0].category == "daily_survival"

    # Plan attempts
    attempt1 = PlanAttempt(
        trip_id=trip_id,
        attempt_number=1,
        downgrade_type="hotel_tier_down",
        was_feasible=False,
        cost_calculated=Decimal("18000.00"),
    )
    attempt_repo.record_plan_attempt(attempt1)
    attempts = attempt_repo.get_plan_attempts(trip_id)
    assert len(attempts) == 1
    assert attempts[0].downgrade_type == "hotel_tier_down"


def test_cache_and_usage_repositories():
    """Verify search cache and API usage tracking repositories."""
    cache_repo = CacheRepository(client=None)
    usage_repo = UsageRepository(client=None)
    trip_id = uuid4()

    # Cache hit & expiration
    cache_record = SearchCache(
        query_hash="hash_flights_blr_goa",
        engine="google_flights",
        params_json={"from": "BLR", "to": "GOI"},
        response_data={"options": ["6E-202"]},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    cache_repo.set_cached_search(cache_record)
    cached = cache_repo.get_cached_search("hash_flights_blr_goa")
    assert cached is not None
    assert cached.engine == "google_flights"

    # Expired cache lookup
    expired_record = SearchCache(
        query_hash="hash_expired",
        engine="google_hotels",
        params_json={},
        response_data={},
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    cache_repo.set_cached_search(expired_record)
    assert cache_repo.get_cached_search("hash_expired") is None

    # Usage tracking
    usage_repo.record_api_call(trip_id, "google_flights", cached=False)
    usage_repo.record_api_call(trip_id, "google_flights", cached=True)
    records = usage_repo.get_trip_usage(trip_id)
    assert len(records) == 2
    assert records[0].call_count == 1
    assert records[1].cached_count == 1
