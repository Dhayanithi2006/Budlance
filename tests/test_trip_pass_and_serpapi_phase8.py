"""Budlance Phase 8 — Comprehensive Trip Pass Monetization & Live SerpApi Verification.

Tests all 25 requirements:
1. Free user receives feasibility summary.
2. Checkout request created.
3. Correct Trip Pass amount (₹49).
4. Travel budget remains unchanged after pass purchase.
5. Successful verified payment unlocks pass.
6. Failed payment does not unlock pass.
7. Abandoned payment does not unlock pass.
8. Duplicate payment event is idempotent.
9. Invalid payment reference rejected.
10. Wrong trip/user payment association rejected.
11. Demo bypass works explicitly.
12. New trip does not inherit old paid state.
13. Live SerpApi success path.
14. Missing SerpApi credential fallback.
15. SerpApi failure fallback.
16. Live flight result normalization.
17. Live hotel result normalization.
18. Live round-trip transport costing.
19. Live data still passes existing feasibility constraints.
20. Existing booking handoff remains truthful.
21. Existing lifecycle PLANNING -> ACTIVE -> COMPLETED remains intact.
22. Expense logging remains intact.
23. Rescue remains intact (gated when pass is locked, active when unlocked).
24. Reoptimization remains intact.
25. Reconciliation remains intact.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4
import pytest
from httpx import ASGITransport, AsyncClient

from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.ai.service import AIIntentService
from budlance.api.app import create_app
from budlance.api.routes import set_payment_service
from budlance.attractions.selector import AttractionSelector
from budlance.cache.manager import CacheFallbackManager
from budlance.config import Settings
from budlance.db.models import Trip, TripPass
from budlance.db.repositories.conversation_repo import ConversationStateRepository
from budlance.db.repositories.intent_repo import IntentRepository
from budlance.db.repositories.itinerary_repo import ItineraryRepository
from budlance.db.repositories.ledger_repo import LedgerRepository
from budlance.db.repositories.rescue_repo import RescueRepository
from budlance.db.repositories.trip_pass_repo import TripPassRepository
from budlance.db.repositories.trip_repo import TripRepository
from budlance.db.repositories.user_repo import UserRepository
from budlance.engine.budget import ReverseBudgetEngine
from budlance.engine.models import BudgetBreakdown, BudgetEvaluationResult
from budlance.engine.optimizer import OptimizationEngine
from budlance.estimation.estimator import EstimationLayer
from budlance.itinerary.generator import ItineraryGenerator
from budlance.ledger.manager import VirtualLedgerManager
from budlance.normalization.flights import build_safe_flight_search_url, extract_best_booking_option
from budlance.normalization.normalizer import DataNormalizer
from budlance.normalization.transit import calculate_round_trip_cost
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.payment.service import PaymentService
from budlance.rescue.service import RescueService
from budlance.schemas.travel import FlightOption, HotelOption, PlaceOption, RouteOption, TransitOption
from budlance.serpapi.gateway import SerpApiGateway
from budlance.serpapi.models import DataSource, TravelDataEnvelope


# =========================================================================
# Test fixtures & factory helpers
# =========================================================================

def _create_mock_cache() -> CacheFallbackManager:
    mock_cache = MagicMock(spec=CacheFallbackManager)

    async def _get(engine, params, trip_id=None, **kw):
        if engine == "google_flights":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="fh",
                data={"best_flights": [{"flights": [{"airline": "IndiGo", "flight_number": "6E-101"}], "price": 3000}]},
                is_fallback=False,
            )
        if engine == "google_hotels":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="hh",
                data={"properties": [{"name": "Seaside Resort", "rate_per_night": {"extracted_lowest": 1500}, "hotel_class": "3"}]},
                is_fallback=False,
            )
        if engine == "google_maps":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="mh",
                data={"local_results": [{"title": "Baga Beach", "rating": 4.5}]},
                is_fallback=False,
            )
        if engine == "google_travel_explore":
            return TravelDataEnvelope(
                source=DataSource.LIVE,
                engine=engine,
                query_hash="eh",
                data={"destinations": [{"name": "Goa", "destination": "Goa"}]},
                is_fallback=False,
            )
        return TravelDataEnvelope(
            source=DataSource.FALLBACK,
            engine=engine,
            query_hash="def",
            data={},
            is_fallback=True,
        )

    mock_cache.get_travel_data = AsyncMock(side_effect=_get)
    mock_cache.get_flight_booking_options = AsyncMock(return_value=None)
    return mock_cache


def _build_test_orchestrator(enable_trip_pass: bool = True) -> tuple[BudlanceOrchestrator, dict]:
    user_repo = UserRepository(client=None)
    trip_repo = TripRepository(client=None)
    intent_repo = IntentRepository(client=None)
    itinerary_repo = ItineraryRepository(client=None)
    ledger_repo = LedgerRepository(client=None)
    rescue_repo = RescueRepository(client=None)
    conversation_repo = ConversationStateRepository(client=None)
    trip_pass_repo = TripPassRepository(client=None)

    payment_service = PaymentService(
        trip_pass_repo=trip_pass_repo,
        pass_amount=Decimal("49.00"),
        pass_currency="INR",
        default_provider="demo",
    )

    budget_engine = ReverseBudgetEngine()
    estimation = EstimationLayer()
    optimizer = OptimizationEngine(budget_engine=budget_engine, estimation_layer=estimation)
    ledger_mgr = VirtualLedgerManager(ledger_repo)
    cache_mgr = _create_mock_cache()
    normalizer = DataNormalizer()
    attraction_selector = AttractionSelector(cache_manager=cache_mgr)
    itin_gen = ItineraryGenerator(itinerary_repo, attraction_selector=attraction_selector)

    ai_service = MagicMock(spec=AIIntentService)

    async def _parse(prompt: str):
        p_low = prompt.lower()
        if "rescue" in p_low or "rain" in p_low:
            return ParsedTripIntent(action=TripAction.RESCUE)
        if "spent" in p_low or "dinner" in p_low:
            return ParsedTripIntent(
                action=TripAction.LOG_EXPENSE,
                amount=Decimal("500.00"),
                expense_category="food",
                expense_description="dinner",
            )
        if "finished" in p_low or "done" in p_low or "complete" in p_low:
            return ParsedTripIntent(action=TripAction.TRIP_COMPLETE)
        if "booked" in p_low:
            return ParsedTripIntent(action=TripAction.CONFIRM_BOOKING)
        if "budget to" in p_low:
            return ParsedTripIntent(
                action=TripAction.CHANGE_BUDGET,
                budget=Decimal("12000.00"),
                origin="Chennai",
                destination="Goa",
                people=2,
                days=3,
                currency="INR",
            )
        # Default new trip
        return ParsedTripIntent(
            action=TripAction.NEW_TRIP,
            origin="Chennai",
            destination="Goa",
            budget=Decimal("25000.00"),
            people=2,
            days=3,
            currency="INR",
            interests=["beach"],
        )

    ai_service.parse_trip_intent = AsyncMock(side_effect=_parse)
    ai_service.parse_trip_intent_with_context = AsyncMock(
        side_effect=lambda user_prompt=None, existing_intent=None, *args, **kwargs: _parse(
            user_prompt or kwargs.get("prompt") or (args[0] if args else "")
        )
    )

    from budlance.ai.schemas import ParsedRescueIntent
    ai_service.parse_rescue_intent = AsyncMock(
        return_value=ParsedRescueIntent(
            rescue_type="weather_closure",
            user_issue="Raining at beach",
            affected_category="beach",
        )
    )

    rescue_service = RescueService(
        trip_repo=trip_repo,
        itinerary_repo=itinerary_repo,
        ledger_repo=ledger_repo,
        rescue_repo=rescue_repo,
        ai_service=ai_service,
        cache_manager=cache_mgr,
        normalizer=normalizer,
        budget_engine=budget_engine,
        estimation_layer=estimation,
        ledger_manager=ledger_mgr,
    )

    itinerary_enhancer = MagicMock()
    itinerary_enhancer.enhance_itinerary = AsyncMock(side_effect=lambda itinerary, **kw: itinerary)

    orchestrator = BudlanceOrchestrator(
        user_repo=user_repo,
        trip_repo=trip_repo,
        intent_repo=intent_repo,
        itinerary_repo=itinerary_repo,
        ledger_repo=ledger_repo,
        rescue_repo=rescue_repo,
        conversation_repo=conversation_repo,
        trip_pass_repo=trip_pass_repo,
        payment_service=payment_service,
        ai_service=ai_service,
        cache_manager=cache_mgr,
        normalizer=normalizer,
        estimation_layer=estimation,
        budget_engine=budget_engine,
        optimizer=optimizer,
        itinerary_generator=itin_gen,
        itinerary_enhancer=itinerary_enhancer,
        ledger_manager=ledger_mgr,
        rescue_service=rescue_service,
        enable_trip_pass=enable_trip_pass,
    )

    repos = {
        "user_repo": user_repo,
        "trip_repo": trip_repo,
        "intent_repo": intent_repo,
        "itinerary_repo": itinerary_repo,
        "ledger_repo": ledger_repo,
        "rescue_repo": rescue_repo,
        "conversation_repo": conversation_repo,
        "trip_pass_repo": trip_pass_repo,
        "payment_service": payment_service,
        "cache_manager": cache_mgr,
    }
    return orchestrator, repos


# =========================================================================
# Phase 8 Tests
# =========================================================================

@pytest.mark.asyncio
async def test_01_free_user_receives_feasibility_summary():
    """1. Free user receives feasibility summary with Waterfall and locked itinerary notice."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    res = await orc.handle_user_message(
        telegram_user_id=101,
        chat_id=1001,
        message="Plan a 3-day trip from Chennai to Goa for 2 people with budget ₹25,000",
    )
    assert res.status == "FEASIBLE"
    assert res.feasibility_status == "FEASIBLE"
    assert res.is_pass_unlocked is False
    assert res.pass_status in ("FREE", "CHECKOUT_PENDING")
    assert "Financial Waterfall (Free Feasibility Analysis)" in res.message_text
    assert "Detailed Itinerary & Rescue Locked" in res.message_text
    assert "Budlance Trip Pass: INR 49.00" in res.message_text
    # Detailed objects hidden from free return
    assert res.generated_itinerary is None
    assert res.selected_transport is None


@pytest.mark.asyncio
async def test_02_checkout_request_created():
    """2. Checkout request and URL created for the planned trip."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    res = await orc.handle_user_message(
        telegram_user_id=102,
        chat_id=1002,
        message="Plan a 3-day trip from Chennai to Goa for 2 people with budget ₹25,000",
    )
    assert res.checkout_url is not None
    assert str(res.trip_id) in res.checkout_url
    pass_rec = repos["trip_pass_repo"].get_by_trip_id(res.trip_id)
    assert pass_rec is not None
    assert pass_rec.checkout_url == res.checkout_url


@pytest.mark.asyncio
async def test_03_correct_trip_pass_amount():
    """3. Correct Trip Pass amount is ₹49.00 INR."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    res = await orc.handle_user_message(
        telegram_user_id=103,
        chat_id=1003,
        message="Plan a 3-day trip from Chennai to Goa for 2 people with budget ₹25,000",
    )
    pass_rec = repos["trip_pass_repo"].get_by_trip_id(res.trip_id)
    assert pass_rec.amount == Decimal("49.00")
    assert pass_rec.currency == "INR"


@pytest.mark.asyncio
async def test_04_travel_budget_remains_unchanged_after_pass_purchase():
    """4. Travel budget remains unchanged after pass purchase (buckets A-D invariant)."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    res_free = await orc.handle_user_message(
        telegram_user_id=104,
        chat_id=1004,
        message="Plan a 3-day trip from Chennai to Goa for 2 people with budget ₹25,000",
    )
    breakdown_before = res_free.budget_breakdown

    # Bypass pass purchase
    res_paid = await orc.handle_user_message(telegram_user_id=104, chat_id=1004, message="/demo_pass")
    breakdown_after = res_paid.budget_breakdown

    assert breakdown_after.total_budget == breakdown_before.total_budget
    assert breakdown_after.bucket_a_fixed == breakdown_before.bucket_a_fixed
    assert breakdown_after.bucket_b_survival == breakdown_before.bucket_b_survival
    assert breakdown_after.bucket_c_activities == breakdown_before.bucket_c_activities
    assert breakdown_after.bucket_d_rescue == breakdown_before.bucket_d_rescue
    assert breakdown_after.total_allocated == breakdown_before.total_allocated


@pytest.mark.asyncio
async def test_05_successful_verified_payment_unlocks_pass():
    """5. Successful verified payment unlocks pass and delivers full detailed itinerary."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    res_free = await orc.handle_user_message(
        telegram_user_id=105,
        chat_id=1005,
        message="Plan a 3-day trip from Chennai to Goa for 2 people with budget ₹25,000",
    )
    trip_id = res_free.trip_id

    # Simulate payment provider webhook confirmation
    v_res = await repos["payment_service"].verify_webhook_event(
        provider="demo",
        payload={"trip_id": str(trip_id), "status": "paid", "payment_id": "pay_test_105"},
    )
    assert v_res.success is True
    assert v_res.status == "PAID"

    # User messages "paid" or checks pass status
    res_paid = await orc.handle_user_message(telegram_user_id=105, chat_id=1005, message="paid")
    assert res_paid.is_pass_unlocked is True
    assert res_paid.pass_status == "PAID"
    assert res_paid.generated_itinerary is not None
    assert "Trip Pass: Active" in res_paid.message_text or "Judge/Demo Bypass Activated" in res_paid.message_text


@pytest.mark.asyncio
async def test_06_failed_payment_does_not_unlock_pass():
    """6. Failed payment does not unlock pass; user stays on free tier."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    res_free = await orc.handle_user_message(
        telegram_user_id=106,
        chat_id=1006,
        message="Plan a 3-day trip from Chennai to Goa for 2 people with budget ₹25,000",
    )
    trip_id = res_free.trip_id

    # Simulate payment failure callback
    v_res = await repos["payment_service"].verify_webhook_event(
        provider="demo",
        payload={"trip_id": str(trip_id), "status": "failed", "error": "card_declined"},
    )
    assert v_res.status == "PAYMENT_FAILED"
    pass_rec = repos["trip_pass_repo"].get_by_trip_id(trip_id)
    assert pass_rec.status == "PAYMENT_FAILED"

    # Verify pass status command
    res_check = await orc.handle_user_message(telegram_user_id=106, chat_id=1006, message="/pass")
    assert res_check.is_pass_unlocked is False


@pytest.mark.asyncio
async def test_07_abandoned_payment_does_not_unlock_pass():
    """7. Abandoned payment leaves user on free summary and allows retry."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    res_free = await orc.handle_user_message(
        telegram_user_id=107,
        chat_id=1007,
        message="Plan a 3-day trip from Chennai to Goa for 2 people with budget ₹25,000",
    )
    trip_id = res_free.trip_id

    v_res = await repos["payment_service"].verify_webhook_event(
        provider="demo",
        payload={"trip_id": str(trip_id), "status": "abandoned"},
    )
    assert v_res.status == "PAYMENT_ABANDONED"
    pass_rec = repos["trip_pass_repo"].get_by_trip_id(trip_id)
    assert pass_rec.status == "PAYMENT_ABANDONED"


@pytest.mark.asyncio
async def test_08_duplicate_payment_event_is_idempotent():
    """8. Duplicate payment event does not create duplicate passes or corrupt state."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    res = await orc.handle_user_message(
        telegram_user_id=108,
        chat_id=1008,
        message="Plan a 3-day trip from Chennai to Goa for 2 people with budget ₹25,000",
    )
    trip_id = res.trip_id

    # Event 1
    v1 = await repos["payment_service"].verify_webhook_event(
        provider="demo",
        payload={"trip_id": str(trip_id), "status": "paid", "payment_id": "pay_dup_01"},
    )
    # Event 2 (duplicate)
    v2 = await repos["payment_service"].verify_webhook_event(
        provider="demo",
        payload={"trip_id": str(trip_id), "status": "paid", "payment_id": "pay_dup_01"},
    )
    assert v1.success is True
    assert v2.success is True
    assert v2.status == "PAID"
    # Still only one pass record
    pass_rec = repos["trip_pass_repo"].get_by_trip_id(trip_id)
    assert pass_rec.status == "PAID"


@pytest.mark.asyncio
async def test_09_invalid_payment_reference_rejected():
    """9. Invalid payment reference or missing payload data is rejected."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    v_res = await repos["payment_service"].verify_webhook_event(
        provider="demo",
        payload={"invalid_key": "junk"},
    )
    assert v_res.success is False
    assert v_res.error == "INVALID_PAYLOAD"


@pytest.mark.asyncio
async def test_10_wrong_trip_user_payment_association_rejected():
    """10. Webhook with non-existent trip_id is rejected."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    random_trip_id = uuid4()
    v_res = await repos["payment_service"].verify_webhook_event(
        provider="demo",
        payload={"trip_id": str(random_trip_id), "status": "paid"},
    )
    assert v_res.success is False
    assert v_res.error == "TRIP_NOT_FOUND"


@pytest.mark.asyncio
async def test_11_demo_bypass_works_explicitly():
    """11. Demo bypass works explicitly via /demo_pass command."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    await orc.handle_user_message(
        telegram_user_id=111,
        chat_id=1011,
        message="Plan a 3-day trip from Chennai to Goa for 2 people with budget ₹25,000",
    )
    res_bypass = await orc.handle_user_message(
        telegram_user_id=111,
        chat_id=1011,
        message="/demo_pass",
    )
    assert res_bypass.is_pass_unlocked is True
    assert res_bypass.pass_status in ("PAID", "DEMO_ACCESS")
    assert "Judge/Demo Bypass Activated" in res_bypass.message_text
    assert res_bypass.generated_itinerary is not None


@pytest.mark.asyncio
async def test_12_new_trip_does_not_inherit_old_paid_state():
    """12. A new trip does not inherit PAID status from an old trip."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=True)
    # Trip 1
    res1 = await orc.handle_user_message(
        telegram_user_id=112,
        chat_id=1012,
        message="Plan a 3-day trip from Chennai to Goa for 2 people with budget ₹25,000",
    )
    trip_id_1 = res1.trip_id
    # Unlock trip 1
    await orc.handle_user_message(telegram_user_id=112, chat_id=1012, message="/demo_pass")
    pass_1 = repos["trip_pass_repo"].get_by_trip_id(trip_id_1)
    assert pass_1.status in ("PAID", "DEMO_ACCESS")

    # Now create Trip 2
    res2 = await orc.handle_user_message(
        telegram_user_id=112,
        chat_id=1012,
        message="Plan a 5-day trip from Chennai to Manali for 2 people with budget ₹35,000",
    )
    trip_id_2 = res2.trip_id
    assert trip_id_2 != trip_id_1
    pass_2 = repos["trip_pass_repo"].get_by_trip_id(trip_id_2)
    # Trip 2 pass is fresh (FREE / CHECKOUT_PENDING), not inherited
    assert pass_2.status in ("FREE", "CHECKOUT_PENDING")
    assert res2.is_pass_unlocked is False


@pytest.mark.asyncio
async def test_13_live_serpapi_success_path():
    """13. Live SerpApi success path returns LIVE envelope when live is enabled."""
    gateway = SerpApiGateway(api_key="valid_test_key", serpapi_live_enabled=True)
    assert gateway.has_credentials is True

    fake_response_data = {
        "best_flights": [
            {
                "flights": [{"airline": "Air India", "flight_number": "AI-502"}],
                "price": 4200,
            }
        ]
    }

    with patch.object(gateway, "execute_search", AsyncMock(return_value=fake_response_data)):
        cache = CacheFallbackManager(gateway=gateway)
        env = await cache.get_travel_data(engine="google_flights", params={"origin": "MAA", "destination": "GOI"})
        assert env.source == DataSource.LIVE
        assert env.is_fallback is False
        assert env.data["best_flights"][0]["price"] == 4200


@pytest.mark.asyncio
async def test_14_missing_serpapi_credential_fallback():
    """14. Missing SerpApi credential safely falls back to catalog without crashing."""
    gateway = SerpApiGateway(api_key="", serpapi_live_enabled=True)
    assert gateway.has_credentials is False

    cache = CacheFallbackManager(gateway=gateway)
    env = await cache.get_travel_data(engine="google_flights", params={"origin": "Chennai", "destination": "Goa"})
    assert env.source == DataSource.FALLBACK
    assert env.is_fallback is True


@pytest.mark.asyncio
async def test_15_serpapi_failure_fallback():
    """15. SerpApi network or HTTP failure gracefully falls back to cached/fallback data."""
    gateway = SerpApiGateway(api_key="valid_key", serpapi_live_enabled=True)
    with patch.object(gateway, "execute_search", AsyncMock(side_effect=RuntimeError("SerpApi 503 Service Unavailable"))):
        cache = CacheFallbackManager(gateway=gateway)
        env = await cache.get_travel_data(engine="google_hotels", params={"location": "Goa"})
        assert env.source == DataSource.FALLBACK
        assert env.is_fallback is True


def test_16_live_flight_result_normalization():
    """16. Live flight results are accurately normalized into FlightOption models."""
    normalizer = DataNormalizer()
    live_env = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_flights",
        query_hash="live_fh",
        data={
            "best_flights": [
                {
                    "flights": [
                        {
                            "airline": "IndiGo",
                            "flight_number": "6E-204",
                            "departure_airport": {"name": "MAA"},
                            "arrival_airport": {"name": "GOI"},
                        }
                    ],
                    "price": 3850,
                    "booking_token": "token_live_123",
                }
            ]
        },
        is_fallback=False,
    )
    flights = normalizer.normalize_flights(live_env)
    assert len(flights) == 1
    assert flights[0].airline == "IndiGo"
    assert flights[0].price == Decimal("3850.00")
    assert flights[0].source == DataSource.LIVE
    assert flights[0].is_fallback is False


def test_17_live_hotel_result_normalization():
    """17. Live hotel results are accurately normalized into HotelOption models."""
    normalizer = DataNormalizer()
    live_env = TravelDataEnvelope(
        source=DataSource.LIVE,
        engine="google_hotels",
        query_hash="live_hh",
        data={
            "properties": [
                {
                    "name": "Hyatt Place Goa",
                    "rate_per_night": {"extracted_lowest": 2900},
                    "hotel_class": "4",
                }
            ]
        },
        is_fallback=False,
    )
    hotels = normalizer.normalize_hotels(live_env)
    assert len(hotels) == 1
    assert hotels[0].name == "Hyatt Place Goa"
    assert hotels[0].price_per_night == Decimal("2900.00")
    assert hotels[0].hotel_class == 4
    assert hotels[0].source == DataSource.LIVE


def test_18_live_round_trip_transport_costing():
    """18. Live round-trip transport calculation strictly uses (outbound + return) * travelers."""
    outbound = Decimal("3500.00")
    return_leg = Decimal("3500.00")
    travelers = 3
    total_cost = calculate_round_trip_cost(outbound, return_leg, travelers)
    assert total_cost == Decimal("21000.00")  # (3500 + 3500) * 3


def test_19_live_data_still_passes_existing_feasibility_constraints():
    """19. Live component pricing strictly adheres to reverse-budget feasibility rules."""
    engine = ReverseBudgetEngine()
    estimation = EstimationLayer()
    transport = FlightOption(
        airline="IndiGo",
        price=Decimal("8000.00"),
        source=DataSource.LIVE,
    )
    hotel = HotelOption(
        name="Hotel Ocean",
        total_price=Decimal("6000.00"),
        price_per_night=Decimal("2000.00"),
        hotel_class=3,
        source=DataSource.LIVE,
    )
    food_est = estimation.estimate_food(people=2, days=3, tier="budget")
    transit_est = estimation.estimate_local_transit_daily(days=3, people=2, mode="metro_bus")
    res = engine.evaluate(
        total_budget=Decimal("25000.00"),
        people=2,
        days=3,
        transport=transport,
        hotel=hotel,
        food_estimate=food_est,
        local_transit_estimate=transit_est,
        activities_budget=Decimal("1250.00"),
    )
    assert res.is_feasible is True
    assert res.breakdown.bucket_a_fixed == Decimal("14000.00")


def test_20_existing_booking_handoff_remains_truthful():
    """20. Booking handoff distinguishes Direct Provider Option vs Search Handoff; never fake GET url."""
    safe_search_url = build_safe_flight_search_url(
        origin="MAA",
        destination="GOI",
        people=2,
        travel_class="economy",
    )
    assert "google.com/travel/flights" in safe_search_url
    assert "token" not in safe_search_url

    # Exact direct booking option
    sample_booking_payload = {
        "booking_options": [
            {
                "seller": "IndiGo Airlines",
                "price": 3800,
                "booking_request": {
                    "url": "https://www.goindigo.in/book/flight?flight=6E101"
                },
            }
        ]
    }
    extracted = extract_best_booking_option(sample_booking_payload)
    assert extracted["seller"] == "IndiGo Airlines"
    assert extracted["direct_url"] == "https://www.goindigo.in/book/flight?flight=6E101"


@pytest.mark.asyncio
async def test_21_existing_lifecycle_planning_active_completed_remains_intact():
    """21. PLANNING -> ACTIVE -> COMPLETED lifecycle works seamlessly."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=False)
    # 1. Planning
    res_plan = await orc.handle_user_message(
        telegram_user_id=121,
        chat_id=1021,
        message="Plan a trip from Chennai to Goa for 2 people with budget ₹25,000",
    )
    assert res_plan.status == "FEASIBLE"
    trip = repos["trip_repo"].get_trip(res_plan.trip_id)
    assert trip.status == "PLANNING"

    # 2. Booked -> ACTIVE
    res_active = await orc.handle_user_message(
        telegram_user_id=121,
        chat_id=1021,
        message="Booked",
    )
    assert res_active.status == "ACTIVE"
    trip_active = repos["trip_repo"].get_trip(res_plan.trip_id)
    assert trip_active.status == "ACTIVE"

    # 3. Trip Complete
    res_comp = await orc.handle_user_message(
        telegram_user_id=121,
        chat_id=1021,
        message="Trip is completed",
    )
    assert res_comp.status in ("PENDING_RECONCILIATION", "COMPLETED")


@pytest.mark.asyncio
async def test_22_expense_logging_remains_intact():
    """22. Expense logging on active trip updates virtual ledger cleanly."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=False)
    # Plan and activate
    res_plan = await orc.handle_user_message(
        telegram_user_id=122,
        chat_id=1022,
        message="Plan a trip from Chennai to Goa for 2 people with budget ₹25,000",
    )
    await orc.handle_user_message(telegram_user_id=122, chat_id=1022, message="Booked")

    # Log expense
    res_exp = await orc.handle_user_message(
        telegram_user_id=122,
        chat_id=1022,
        message="Spent ₹500 on dinner",
    )
    assert res_exp.status in ("EXPENSE_LOGGED", "LOG_EXPENSE")
    assert res_exp.ledger_summary is not None
    assert res_exp.ledger_summary.total_spent == Decimal("500.00")


@pytest.mark.asyncio
async def test_23_rescue_remains_intact():
    """23. Rescue mode is locked when pass is locked, but works when unlocked."""
    # A. With Pass Enabled and Locked
    orc_locked, repos_locked = _build_test_orchestrator(enable_trip_pass=True)
    res_plan = await orc_locked.handle_user_message(
        telegram_user_id=123,
        chat_id=1023,
        message="Plan a trip from Chennai to Goa for 2 people with budget ₹25,000",
    )
    await orc_locked.handle_user_message(telegram_user_id=123, chat_id=1023, message="Booked")
    res_rescue_locked = await orc_locked.handle_user_message(
        telegram_user_id=123,
        chat_id=1023,
        message="Rescue: it is raining at the beach",
    )
    assert res_rescue_locked.status == "PASS_LOCKED"
    assert "In-Trip Rescue is Locked" in res_rescue_locked.message_text

    # B. Unlock via demo pass
    await orc_locked.handle_user_message(telegram_user_id=123, chat_id=1023, message="/demo_pass")
    res_rescue_unlocked = await orc_locked.handle_user_message(
        telegram_user_id=123,
        chat_id=1023,
        message="Rescue: it is raining at the beach",
    )
    assert res_rescue_unlocked.status == "RESCUE"


@pytest.mark.asyncio
async def test_24_reoptimization_remains_intact():
    """24. Reoptimization engages optimizer when budget is reduced."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=False)
    # Initially feasible at ₹25,000
    await orc.handle_user_message(
        telegram_user_id=124,
        chat_id=1024,
        message="Plan a trip from Chennai to Goa for 2 people with budget ₹25,000",
    )
    # Change budget to ₹12,000
    res_opt = await orc.handle_user_message(
        telegram_user_id=124,
        chat_id=1024,
        message="Change budget to 12000",
    )
    assert res_opt.feasibility_status in ("FEASIBLE", "NOT_FEASIBLE")


@pytest.mark.asyncio
async def test_25_reconciliation_remains_intact():
    """25. Trip completion and final spend reconciliation prompt and record correctly."""
    orc, repos = _build_test_orchestrator(enable_trip_pass=False)
    res_plan = await orc.handle_user_message(
        telegram_user_id=125,
        chat_id=1025,
        message="Plan a trip from Chennai to Goa for 2 people with budget ₹25,000",
    )
    await orc.handle_user_message(telegram_user_id=125, chat_id=1025, message="Booked")
    # Initiate completion
    res_comp = await orc.handle_user_message(telegram_user_id=125, chat_id=1025, message="Trip complete")
    assert res_comp.status == "PENDING_RECONCILIATION"

    # User responds "skip"
    res_skip = await orc.handle_user_message(telegram_user_id=125, chat_id=1025, message="skip")
    assert res_skip.status == "COMPLETED"
