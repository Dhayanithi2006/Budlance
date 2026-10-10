"""Tests for OpenRouter AI Intent Layer and structured schemas."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
import httpx
import pytest

from budlance.ai.client import OpenRouterClient
from budlance.ai.exceptions import (
    OpenRouterAuthError,
    OpenRouterNetworkError,
    OpenRouterResponseError,
    OpenRouterValidationError,
)
from budlance.ai.schemas import ParsedRescueIntent, ParsedTripIntent, TripAction
from budlance.ai.service import AIIntentService
from budlance.db.repositories.intent_repo import IntentRepository


# ============================================================================
# 1. Complete Travel Intent Tests
# ============================================================================
@pytest.mark.asyncio
async def test_complete_travel_intent_parsing():
    """Verify parsing a fully-specified user travel request."""
    service = AIIntentService(use_mock=True)
    prompt = "I have ₹25,000 for 5 days with 3 people for Kerala. We like beaches and food, traveling with family."
    intent = await service.parse_trip_intent(prompt)

    assert intent.budget == Decimal("25000")
    assert intent.currency == "INR"
    assert intent.days == 5
    assert intent.people == 3
    assert intent.destination == "Kerala"
    assert "beach" in intent.interests or "beaches" in intent.interests
    assert intent.traveler_type == "family"
    assert intent.is_plannable is True
    assert intent.needs_destination_discovery is False
    assert len(intent.missing_fields) == 0


# ============================================================================
# 2. Destination Omitted (Reverse-Budget Discovery)
# ============================================================================
@pytest.mark.asyncio
async def test_destination_omitted_for_discovery():
    """Verify that omitting destination triggers reverse-budget discovery mode."""
    service = AIIntentService(use_mock=True)
    prompt = "We have ₹15,000 for 4 days for 2 people. Show me where we can go, we love nature."
    intent = await service.parse_trip_intent(prompt)

    assert intent.budget == Decimal("15000")
    assert intent.days == 4
    assert intent.people == 2
    assert intent.destination is None
    assert intent.needs_destination_discovery is True
    assert intent.is_plannable is True


# ============================================================================
# 3. Incomplete Request
# ============================================================================
@pytest.mark.asyncio
async def test_incomplete_travel_request():
    """Verify incomplete requests explicitly expose missing fields instead of inventing numbers."""
    service = AIIntentService(use_mock=True)
    prompt = "I want to visit Goa with friends."
    intent = await service.parse_trip_intent(prompt)

    assert intent.destination == "Goa"
    assert intent.budget is None
    assert intent.days is None
    assert intent.is_plannable is False
    assert "budget" in intent.missing_fields
    assert "days" in intent.missing_fields


# ============================================================================
# 4. Multilingual Inputs
# ============================================================================
@pytest.mark.asyncio
async def test_multilingual_input_tamil():
    """Verify parsing multilingual Tamil travel prompt."""
    service = AIIntentService(use_mock=True)
    prompt = "15000 ரூபாய் 5 நாட்கள் 3 பேர் கேரளா செல்ல வேண்டும் (Kerala)"
    intent = await service.parse_trip_intent(prompt)

    assert intent.budget == Decimal("15000")
    assert intent.days == 5
    assert intent.people == 3
    assert intent.destination == "Kerala"


@pytest.mark.asyncio
async def test_multilingual_input_hindi():
    """Verify parsing Hindi/Hinglish travel prompt."""
    service = AIIntentService(use_mock=True)
    prompt = "₹20000 budget hai, 4 din, 2 log, Goa jana hai"
    intent = await service.parse_trip_intent(prompt)

    assert intent.budget == Decimal("20000")
    assert intent.days == 4
    assert intent.people == 2
    assert intent.destination == "Goa"


# ============================================================================
# 5. Rescue Intent Tests
# ============================================================================
@pytest.mark.asyncio
async def test_rescue_weather_closure():
    """Verify weather/closure distress message classification."""
    service = AIIntentService(use_mock=True)
    message = "It is heavily raining outside and the fort is closed!"
    rescue = await service.parse_rescue_intent(message)

    assert rescue.rescue_type == "weather_closure"
    assert "rain" in rescue.user_issue.lower() or "weather" in rescue.user_issue.lower()
    assert rescue.reported_price is None


@pytest.mark.asyncio
async def test_rescue_price_dispute():
    """Verify fare/price dispute extraction with reported price."""
    service = AIIntentService(use_mock=True)
    message = "The auto driver is asking 500 rupees for a 2 km ride!"
    rescue = await service.parse_rescue_intent(message)

    assert rescue.rescue_type == "price_dispute"
    assert rescue.reported_price == Decimal("500")
    assert rescue.service_type == "auto"


@pytest.mark.asyncio
async def test_rescue_unknown_message():
    """Verify non-rescue chat is classified as unknown."""
    service = AIIntentService(use_mock=True)
    message = "Can you recommend a song for the beach?"
    rescue = await service.parse_rescue_intent(message)

    assert rescue.rescue_type == "unknown"


# ============================================================================
# 6. Database Persistence Integration
# ============================================================================
def test_to_trip_intent_record_and_save():
    """Verify valid parsed intent converts cleanly to TripIntent and persists in repo."""
    intent = ParsedTripIntent(
        budget=Decimal("30000.00"),
        currency="INR",
        people=2,
        days=4,
        destination="Ooty",
        interests=["nature", "tea"],
    )
    trip_id = uuid4()
    record = intent.to_trip_intent_record(trip_id=trip_id, raw_prompt="Trip to Ooty")

    assert record.trip_id == trip_id
    assert record.budget == Decimal("30000.00")

    repo = IntentRepository(client=None)
    saved = repo.save_trip_intent(record)
    assert saved.destination == "Ooty"

    fetched = repo.get_trip_intent(trip_id)
    assert fetched is not None
    assert fetched.budget == Decimal("30000.00")


# ============================================================================
# 7. Error Handling & Client Mock Tests
# ============================================================================
@pytest.mark.asyncio
async def test_client_missing_credentials():
    """Verify client raises OpenRouterAuthError when credentials are missing."""
    client = OpenRouterClient(api_key="")
    with pytest.raises(OpenRouterAuthError):
        await client.chat_completion([{"role": "user", "content": "hello"}])


@pytest.mark.asyncio
async def test_client_network_timeout():
    """Verify network timeouts are translated into OpenRouterNetworkError."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.post = AsyncMock(side_effect=httpx.ReadTimeout("Read timed out"))

    client = OpenRouterClient(api_key="test_key", http_client=mock_http)
    with pytest.raises(OpenRouterNetworkError):
        await client.chat_completion([{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_client_invalid_json_response():
    """Verify non-JSON model response raises OpenRouterResponseError."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "This is definitely not valid json"}}]
    }

    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.post = AsyncMock(return_value=mock_resp)

    client = OpenRouterClient(api_key="test_key", http_client=mock_http)
    with pytest.raises(OpenRouterResponseError):
        await client.chat_completion([{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_service_validation_error_on_bad_schema():
    """Verify that a schema mismatch raises OpenRouterValidationError."""
    mock_client = MagicMock(spec=OpenRouterClient)
    mock_client.has_credentials = True
    # Return invalid budget type (e.g. a list instead of a number)
    mock_client.chat_completion = AsyncMock(return_value={"budget": "not_a_valid_number"})

    service = AIIntentService(client=mock_client, use_mock=False)
    with pytest.raises(OpenRouterValidationError):
        await service.parse_trip_intent("Visit Kerala")


@pytest.mark.asyncio
async def test_gemini_client_missing_credentials():
    """Verify GeminiClient raises OpenRouterAuthError when credentials are missing."""
    from budlance.ai.client import GeminiClient

    client = GeminiClient(api_key="")
    assert client.has_credentials is False
    with pytest.raises(OpenRouterAuthError):
        await client.chat_completion([{"role": "user", "content": "hello"}])


@pytest.mark.asyncio
async def test_gemini_client_mock_completion():
    """Verify GeminiClient parses valid candidate JSON."""
    from budlance.ai.client import GeminiClient

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": '{"budget": 15000, "days": 3, "destination": "Goa"}'}]
                }
            }
        ]
    }
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.post = AsyncMock(return_value=mock_resp)

    client = GeminiClient(api_key="fake_key", http_client=mock_http)
    assert client.has_credentials is True
    res = await client.chat_completion([{"role": "user", "content": "test"}])
    assert res["destination"] == "Goa"
    assert res["budget"] == 15000


def test_ai_intent_service_selects_openrouter(monkeypatch):
    """Verify AIIntentService selects OpenRouterClient when OpenRouter credentials exist (Gemini disabled)."""
    from budlance.ai.client import OpenRouterClient
    from budlance.config import get_settings
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-fake-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemma-4-26b-a4b-it:free")
    get_settings.cache_clear()

    service = AIIntentService()
    assert isinstance(service.client, OpenRouterClient)
    assert service.use_mock is False
    assert service.client.model == "google/gemma-4-26b-a4b-it:free"


# ============================================================================
# 8. Tanglish + Mock Fallback Regression Tests (Task 10)
# ============================================================================

def test_mock_exact_tanglish_extraction():
    """Exact Tanglish sentence must parse all 4 required fields correctly."""
    service = AIIntentService(use_mock=True)
    result = service._mock_parse_trip_intent(
        "Enakku 15000 budget irukku, 2 peru, 3 days Chennai la irundhu hill station poganum."
    )
    assert result.budget == pytest.approx(15000, rel=1e-3)
    assert result.people == 2
    assert result.days == 3
    assert result.origin == "Chennai"
    assert result.destination is None
    assert "hill station" in result.interests


def test_mock_tanglish_variation_20k_bangalore():
    """Tanglish with 20k shorthand, 3 peru, Bangalore origin, hill station interest."""
    service = AIIntentService(use_mock=True)
    result = service._mock_parse_trip_intent(
        "Enakku 20k budget, 4 days, 3 peru, Bangalore la irundhu hill station poganum."
    )
    assert result.budget == pytest.approx(20000, rel=1e-3)
    assert result.people == 3
    assert result.days == 4
    assert result.origin == "Bangalore"
    assert result.destination is None
    assert "hill station" in result.interests


def test_mock_tanglish_beach_trip():
    """Tanglish beach variation: '2 peru' + 'Chennai la irundhu' + beach interest."""
    service = AIIntentService(use_mock=True)
    result = service._mock_parse_trip_intent(
        "Naan 15000 budget la 2 peru 3 days Chennai la irundhu beach trip poganum."
    )
    assert result.people == 2
    assert result.origin == "Chennai"
    assert result.destination is None
    assert "beach" in result.interests


def test_mock_origin_not_misclassified_as_destination():
    """Origin city must NEVER appear as destination in the heuristic parser."""
    service = AIIntentService(use_mock=True)
    for prompt in [
        "Enakku 15000 budget irukku, 2 peru, 3 days Chennai la irundhu hill station poganum.",
        "Plan a trip from Chennai for 2 people, 3 days, with a budget of 15000.",
        "2 peru, 3 days, Chennai la irundhu trip venum, budget 18000.",
    ]:
        result = service._mock_parse_trip_intent(prompt)
        assert result.origin == "Chennai", f"Expected origin=Chennai for: {prompt!r}"
        assert result.destination != "Chennai", f"Chennai must not be destination for: {prompt!r}"


def test_mock_hill_station_interest_extracted():
    """'hill station' must appear in interests when mentioned in any form."""
    service = AIIntentService(use_mock=True)
    result = service._mock_parse_trip_intent(
        "15000 budget, 2 peru, 3 days Chennai la irundhu hill station poganum."
    )
    assert "hill station" in result.interests


def test_mock_english_origin_extraction():
    """Standard English 'from <city>' pattern must set origin, not destination."""
    service = AIIntentService(use_mock=True)
    result = service._mock_parse_trip_intent(
        "Plan a trip from Chennai for 2 people, 3 days, with a budget of ₹15,000."
    )
    assert result.budget == pytest.approx(15000, rel=1e-3)
    assert result.people == 2
    assert result.days == 3
    assert result.origin == "Chennai"
    assert result.destination != "Chennai"


@pytest.mark.asyncio
async def test_openrouter_success_returns_valid_intent():
    """OpenRouter mock success → produces correct TripIntent without falling back."""
    mock_client = MagicMock(spec=OpenRouterClient)
    mock_client.has_credentials = True
    mock_client.chat_completion = AsyncMock(return_value={
        "budget": 15000,
        "currency": "INR",
        "people": 2,
        "days": 3,
        "origin": "Chennai",
        "destination": None,
        "interests": ["hill station"],
        "traveler_type": None,
    })
    service = AIIntentService(client=mock_client, use_mock=False)
    result = await service.parse_trip_intent("Enakku 15000 budget irukku, 2 peru, 3 days Chennai la irundhu hill station poganum.")
    assert result.people == 2
    assert result.origin == "Chennai"
    assert result.destination is None
    assert "hill station" in result.interests
    assert result.is_plannable is True


@pytest.mark.asyncio
async def test_openrouter_failure_logs_and_falls_back_to_mock():
    """OpenRouter network failure must fall back to heuristic gracefully (no exception to caller)."""
    from budlance.ai.exceptions import OpenRouterResponseError

    mock_client = MagicMock(spec=OpenRouterClient)
    mock_client.has_credentials = True
    mock_client.chat_completion = AsyncMock(
        side_effect=OpenRouterResponseError("HTTP 429: rate limited")
    )
    service = AIIntentService(client=mock_client, use_mock=False)

    # Fallback should be transparent — no exception raised to the caller
    result = await service.parse_trip_intent(
        "Enakku 15000 budget irukku, 2 peru, 3 days Chennai la irundhu hill station poganum."
    )
    # Heuristic extracts budget=15000, people=2, days=3, origin=Chennai
    assert result.budget is not None
    assert result.people == 2
    assert result.origin == "Chennai"


@pytest.mark.asyncio
async def test_invalid_openrouter_json_does_not_produce_silent_fabricated_intent():
    """Invalid model JSON must raise OpenRouterValidationError, not produce a fabricated intent silently."""
    mock_client = MagicMock(spec=OpenRouterClient)
    mock_client.has_credentials = True
    # Return data that fails Pydantic validation (budget is a list, which is invalid)
    mock_client.chat_completion = AsyncMock(return_value={"budget": [1, 2, 3]})

    service = AIIntentService(client=mock_client, use_mock=False)
    with pytest.raises(OpenRouterValidationError):
        await service.parse_trip_intent("test")


def test_mock_indian_lakh_and_crore_budget_extraction():
    """Verify Indian currency notation (5,00,000, 1,50,000, 5 lakh, 5.5 lakhs, 1 crore) in fallback parser."""
    service = AIIntentService(use_mock=True)

    intent1 = service._mock_parse_trip_intent("Rs 5,00,000 for solo trip from Chennai for 7 days")
    assert intent1.budget == Decimal("500000")
    assert intent1.action == TripAction.NEW_TRIP

    intent2 = service._mock_parse_trip_intent("budget 1,50,000 for 5 days to Goa")
    assert intent2.budget == Decimal("150000")

    intent3 = service._mock_parse_trip_intent("5 lakhs budget for 3 days from Mumbai")
    assert intent3.budget == Decimal("500000")

    intent4 = service._mock_parse_trip_intent("3.5 lac budget for 2 people")
    assert intent4.budget == Decimal("350000")


def test_mock_trip_planning_spend_time_does_not_trigger_log_expense():
    """Colloquial 'spend more time in nature' during trip planning must NOT be misclassified as LOG_EXPENSE."""
    service = AIIntentService(use_mock=True)
    msg = (
        "I have around Rs 5,00,000 for a solo trip. I don't have a destination fixed yet. "
        "I really want somewhere with calm nature, cool climate, very fresh air. "
        "I would rather spend more time in nature, local food and quiet places. "
        "I'm starting from Chennai. I can travel for around 7 to 10 days."
    )
    intent = service._mock_parse_trip_intent(msg)
    assert intent.action == TripAction.NEW_TRIP
    assert intent.budget == Decimal("500000")
    assert intent.origin == "Chennai"
    assert intent.people == 1
    # Duration selection rule selects conservative lower-bound (7 days) for ranges like "7 to 10 days"
    assert intent.days == 7
    assert "nature" in intent.interests


@pytest.mark.asyncio
async def test_openrouter_failover_to_fallback_on_auth_error():
    """chat_completion must failover to fallback key when primary key returns 401."""
    mock_http = MagicMock(spec=httpx.AsyncClient)

    resp_primary = MagicMock()
    resp_primary.status_code = 401
    resp_primary.text = '{"error": "Invalid API key"}'

    resp_fallback = MagicMock()
    resp_fallback.status_code = 200
    resp_fallback.json.return_value = {
        "choices": [{"message": {"content": '{"budget": 20000, "days": 3, "destination": "Goa"}'}}]
    }

    mock_http.post = AsyncMock(side_effect=[resp_primary, resp_fallback])

    client = OpenRouterClient(
        api_key="bad_primary_key",
        fallback_api_key="good_fallback_key",
        http_client=mock_http,
    )
    assert client.has_credentials is True

    result = await client.chat_completion([{"role": "user", "content": "test"}])
    assert result["destination"] == "Goa"
    assert mock_http.post.call_count == 2
    assert mock_http.post.call_args_list[0].kwargs["headers"]["Authorization"] == "Bearer bad_primary_key"
    assert mock_http.post.call_args_list[1].kwargs["headers"]["Authorization"] == "Bearer good_fallback_key"


@pytest.mark.asyncio
async def test_openrouter_failover_to_fallback_on_rate_limit():
    """chat_completion must failover to fallback key when primary key hits 429."""
    mock_http = MagicMock(spec=httpx.AsyncClient)

    resp_primary = MagicMock()
    resp_primary.status_code = 429
    resp_primary.text = '{"error": {"code": 429, "message": "Rate limit reached"}}'

    resp_fallback = MagicMock()
    resp_fallback.status_code = 200
    resp_fallback.json.return_value = {
        "choices": [{"message": {"content": '{"budget": 50000, "days": 5, "destination": "Ooty"}'}}]
    }

    mock_http.post = AsyncMock(side_effect=[resp_primary, resp_fallback])

    client = OpenRouterClient(
        api_key="rate_limited_primary",
        fallback_api_key="active_fallback_key",
        http_client=mock_http,
    )

    result = await client.chat_completion([{"role": "user", "content": "plan trip"}])
    assert result["destination"] == "Ooty"
    assert mock_http.post.call_count == 2
    assert mock_http.post.call_args_list[1].kwargs["headers"]["Authorization"] == "Bearer active_fallback_key"


