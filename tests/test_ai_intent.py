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
from budlance.ai.schemas import ParsedRescueIntent, ParsedTripIntent
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
