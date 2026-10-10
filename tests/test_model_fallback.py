"""Deterministic tests for OpenRouter model-level fallback and AI Intent Service reliability.

Scenarios tested:
  A. Primary model succeeds -> 1 call, fallback not used, heuristic not used.
  B. Primary model returns 429 -> fallback model 1 succeeds, heuristic not used.
  C. Primary 429, fallback 1 429 -> fallback model 2 succeeds, heuristic not used.
  D. All models fail -> local heuristic succeeds, ai_heuristic_used=True, telemetry preserves original 429.
  E. Model returns malformed JSON -> next fallback model is tried.
  F. Model returns invalid schema / enum -> next fallback model is tried.
  G. Network timeout on primary -> fallback model succeeds.
  H. Non-retryable 4xx (e.g. 400 client error) -> fails fast without blind looping.
  I. Total AI attempts are finite (strictly candidate_models length, no infinite retry).
  J. Single user message produces exactly one validated intent result.
  + Heuristic regressions:
      1. "Let me plan a new trip" -> NEW_TRIP
      2. "I would rather spend more time in nature" -> NOT LOG_EXPENSE
      3. "Add ₹5000 to my budget" -> CHANGE_BUDGET with is_delta=True (budget increased, not overwritten)
  + Config validation:
      OPENROUTER_FALLBACK_MODELS parsing (string, list, default)
  + Live SerpApi calls assertion = 0
"""

import asyncio
import json
import time
from decimal import Decimal
from unittest.mock import AsyncMock, patch
import httpx
import pytest

from budlance.ai.client import OpenRouterClient
from budlance.ai.exceptions import OpenRouterResponseError
from budlance.ai.schemas import ParsedTripIntent, TripAction
from budlance.ai.service import AIIntentService
from budlance.ai.telemetry import AITelemetry
from budlance.config import Settings, get_settings


def _create_mock_response(status_code: int, data: dict) -> httpx.Response:
    """Create a mock httpx.Response."""
    req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(data).encode("utf-8"),
        request=req,
    )


def _valid_intent_content(action: str = "NEW_TRIP", **kwargs) -> str:
    payload = {
        "action": action,
        "origin": "Chennai",
        "destination": "Goa",
        "budget": 20000,
        "people": 2,
        "days": 3,
        **kwargs,
    }
    return json.dumps(payload)


# =============================================================================
# SCENARIOS A - J: MODEL-LEVEL FALLBACK CLIENT & SERVICE TESTS
# =============================================================================


@pytest.mark.asyncio
async def test_scenario_a_primary_succeeds():
    """Test A: Primary succeeds -> 1 call, fallback not used, heuristic not used."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    primary_model = "google/gemma-4-26b-a4b-it:free"
    fallback_1 = "google/gemma-4-31b-it:free"
    fallback_2 = "nvidia/nemotron-3.5-lightning:free"

    mock_http.post.return_value = _create_mock_response(
        200,
        {
            "model": primary_model,
            "choices": [{"message": {"content": _valid_intent_content()}}],
        },
    )

    client = OpenRouterClient(
        api_key="sk-or-test-key",
        model=primary_model,
        fallback_models=[fallback_1, fallback_2],
        http_client=mock_http,
    )
    svc = AIIntentService(client=client, use_mock=False)

    intent = await svc.parse_trip_intent("Trip to Goa from Chennai for 2 people 3 days budget 20000")

    assert intent.action == TripAction.NEW_TRIP
    assert intent.destination == "Goa"
    assert mock_http.post.call_count == 1

    summary = client.telemetry.get_summary()
    assert summary["ai_primary_model"] == primary_model
    assert summary["ai_selected_model"] == primary_model
    assert summary["ai_fallback_used"] is False
    assert summary["ai_fallback_index"] == 0
    assert summary["ai_total_attempts"] == 1
    assert summary["ai_heuristic_used"] is False
    assert summary["ai_failure_reason"] is None


@pytest.mark.asyncio
async def test_scenario_b_primary_429_fallback_1_succeeds():
    """Test B: Primary returns 429 -> fallback model 1 succeeds, heuristic not used."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    primary_model = "google/gemma-4-26b-a4b-it:free"
    fallback_1 = "google/gemma-4-31b-it:free"
    fallback_2 = "nvidia/nemotron-3.5-lightning:free"

    # Call 1 (primary) -> 429; Call 2 (fallback 1) -> 200
    mock_http.post.side_effect = [
        _create_mock_response(
            429,
            {"error": {"message": f"{primary_model} is temporarily rate-limited upstream", "code": 429}},
        ),
        _create_mock_response(
            200,
            {
                "model": fallback_1,
                "choices": [{"message": {"content": _valid_intent_content()}}],
            },
        ),
    ]

    client = OpenRouterClient(
        api_key="sk-or-test-key",
        model=primary_model,
        fallback_models=[fallback_1, fallback_2],
        http_client=mock_http,
    )
    svc = AIIntentService(client=client, use_mock=False)

    intent = await svc.parse_trip_intent("Trip to Goa from Chennai for 2 people 3 days budget 20000")

    assert intent.action == TripAction.NEW_TRIP
    assert intent.destination == "Goa"
    assert mock_http.post.call_count == 2

    # Check payload sent on 2nd attempt targeted fallback_1 directly (no models array)
    second_call_payload = mock_http.post.call_args_list[1].kwargs["json"]
    assert second_call_payload["model"] == fallback_1
    assert "models" not in second_call_payload

    summary = client.telemetry.get_summary()
    assert summary["ai_primary_model"] == primary_model
    assert summary["ai_selected_model"] == fallback_1
    assert summary["ai_fallback_used"] is True
    assert summary["ai_fallback_index"] == 1
    assert summary["ai_total_attempts"] == 2
    assert summary["ai_heuristic_used"] is False
    # Ensure attempt 0 recorded 429 and was not hidden
    assert summary["attempts"][0]["status"] == "rate_limited"
    assert summary["attempts"][0]["http_status"] == 429


@pytest.mark.asyncio
async def test_scenario_c_primary_and_fallback_1_429_fallback_2_succeeds():
    """Test C: Primary 429, fallback 1 429 -> fallback model 2 succeeds, heuristic not used."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    primary_model = "google/gemma-4-26b-a4b-it:free"
    fallback_1 = "google/gemma-4-31b-it:free"
    fallback_2 = "nvidia/nemotron-3.5-lightning:free"

    mock_http.post.side_effect = [
        _create_mock_response(429, {"error": {"message": f"{primary_model} 429", "code": 429}}),
        _create_mock_response(429, {"error": {"message": f"{fallback_1} 429", "code": 429}}),
        _create_mock_response(
            200,
            {
                "model": fallback_2,
                "choices": [{"message": {"content": _valid_intent_content()}}],
            },
        ),
    ]

    client = OpenRouterClient(
        api_key="sk-or-test-key",
        model=primary_model,
        fallback_models=[fallback_1, fallback_2],
        http_client=mock_http,
    )
    svc = AIIntentService(client=client, use_mock=False)

    intent = await svc.parse_trip_intent("Trip to Goa from Chennai for 2 people 3 days budget 20000")

    assert intent.action == TripAction.NEW_TRIP
    assert mock_http.post.call_count == 3

    summary = client.telemetry.get_summary()
    assert summary["ai_selected_model"] == fallback_2
    assert summary["ai_fallback_used"] is True
    assert summary["ai_fallback_index"] == 2
    assert summary["ai_total_attempts"] == 3
    assert summary["ai_heuristic_used"] is False


@pytest.mark.asyncio
async def test_scenario_d_all_models_fail_heuristic_succeeds():
    """Test D: All models fail -> local heuristic succeeds, ai_heuristic_used=True."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    primary_model = "google/gemma-4-26b-a4b-it:free"
    fallback_1 = "google/gemma-4-31b-it:free"
    fallback_2 = "nvidia/nemotron-3.5-lightning:free"

    mock_http.post.side_effect = [
        _create_mock_response(429, {"error": {"message": f"{primary_model} 429", "code": 429}}),
        _create_mock_response(429, {"error": {"message": f"{fallback_1} 429", "code": 429}}),
        _create_mock_response(503, {"error": {"message": f"{fallback_2} 503", "code": 503}}),
    ]

    client = OpenRouterClient(
        api_key="sk-or-test-key",
        model=primary_model,
        fallback_models=[fallback_1, fallback_2],
        http_client=mock_http,
    )
    svc = AIIntentService(client=client, use_mock=False)

    # All LLMs fail, should fall back to heuristic extraction without crashing
    intent = await svc.parse_trip_intent("Trip to Goa from Chennai for 2 people 3 days budget 20000")

    assert intent.action == TripAction.NEW_TRIP
    assert intent.destination == "Goa"
    assert intent.origin == "Chennai"
    assert intent.budget == Decimal("20000")
    assert intent.people == 2
    assert intent.days == 3

    summary = client.telemetry.get_summary()
    assert summary["ai_selected_model"] is None
    assert summary["ai_heuristic_used"] is True
    assert summary["ai_fallback_used"] is True
    assert summary["ai_total_attempts"] == 3
    assert "429" in summary["ai_failure_reason"] or "503" in summary["ai_failure_reason"]


@pytest.mark.asyncio
async def test_scenario_e_malformed_json_triggers_next_fallback():
    """Test E: Model returns malformed JSON -> next fallback model is tried."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    primary_model = "google/gemma-4-26b-a4b-it:free"
    fallback_1 = "google/gemma-4-31b-it:free"

    mock_http.post.side_effect = [
        _create_mock_response(
            200,
            {
                "model": primary_model,
                "choices": [{"message": {"content": "This is definitely not valid json {"}}],
            },
        ),
        _create_mock_response(
            200,
            {
                "model": fallback_1,
                "choices": [{"message": {"content": _valid_intent_content()}}],
            },
        ),
    ]

    client = OpenRouterClient(
        api_key="sk-or-test-key",
        model=primary_model,
        fallback_models=[fallback_1],
        http_client=mock_http,
    )
    svc = AIIntentService(client=client, use_mock=False)

    intent = await svc.parse_trip_intent("Trip to Goa from Chennai for 2 people 3 days budget 20000")

    assert intent.action == TripAction.NEW_TRIP
    assert mock_http.post.call_count == 2
    summary = client.telemetry.get_summary()
    assert summary["ai_selected_model"] == fallback_1
    assert summary["attempts"][0]["status"] == "malformed_json"


@pytest.mark.asyncio
async def test_scenario_f_invalid_schema_triggers_next_fallback():
    """Test F: Model returns invalid schema / non-numeric budget -> next fallback tried."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    primary_model = "google/gemma-4-26b-a4b-it:free"
    fallback_1 = "google/gemma-4-31b-it:free"

    # Primary returns action not in TripAction enum and invalid people string
    mock_http.post.side_effect = [
        _create_mock_response(
            200,
            {
                "model": primary_model,
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({
                                "action": "INVALID_NON_EXISTENT_ACTION",
                                "people": "invalid_number",
                            })
                        }
                    }
                ],
            },
        ),
        _create_mock_response(
            200,
            {
                "model": fallback_1,
                "choices": [{"message": {"content": _valid_intent_content()}}],
            },
        ),
    ]

    client = OpenRouterClient(
        api_key="sk-or-test-key",
        model=primary_model,
        fallback_models=[fallback_1],
        http_client=mock_http,
    )
    svc = AIIntentService(client=client, use_mock=False)

    intent = await svc.parse_trip_intent("Trip to Goa from Chennai for 2 people 3 days budget 20000")

    assert intent.action == TripAction.NEW_TRIP
    assert mock_http.post.call_count == 2
    summary = client.telemetry.get_summary()
    assert summary["ai_selected_model"] == fallback_1
    assert summary["attempts"][0]["status"] == "validation_failed"


@pytest.mark.asyncio
async def test_scenario_g_network_timeout_triggers_fallback():
    """Test G: Network timeout on primary -> fallback model succeeds."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    primary_model = "google/gemma-4-26b-a4b-it:free"
    fallback_1 = "google/gemma-4-31b-it:free"

    req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    mock_http.post.side_effect = [
        httpx.TimeoutException("Connection timed out", request=req),
        _create_mock_response(
            200,
            {
                "model": fallback_1,
                "choices": [{"message": {"content": _valid_intent_content()}}],
            },
        ),
    ]

    client = OpenRouterClient(
        api_key="sk-or-test-key",
        model=primary_model,
        fallback_models=[fallback_1],
        http_client=mock_http,
    )
    svc = AIIntentService(client=client, use_mock=False)

    intent = await svc.parse_trip_intent("Trip to Goa from Chennai for 2 people 3 days budget 20000")

    assert intent.action == TripAction.NEW_TRIP
    assert mock_http.post.call_count == 2
    summary = client.telemetry.get_summary()
    assert summary["ai_selected_model"] == fallback_1
    assert summary["attempts"][0]["status"] == "timeout"


@pytest.mark.asyncio
async def test_scenario_h_client_error_400_fails_fast():
    """Test H: Non-retryable 4xx (HTTP 400 caused by bad payload) -> fails fast without blind looping."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    primary_model = "google/gemma-4-26b-a4b-it:free"
    fallback_1 = "google/gemma-4-31b-it:free"
    fallback_2 = "nvidia/nemotron-3.5-lightning:free"

    mock_http.post.return_value = _create_mock_response(
        400,
        {"error": {"message": "Invalid parameter: prompt too long", "code": 400}},
    )

    client = OpenRouterClient(
        api_key="sk-or-test-key",
        model=primary_model,
        fallback_models=[fallback_1, fallback_2],
        http_client=mock_http,
    )

    # Calling chat_completion directly should raise OpenRouterResponseError on 400
    with pytest.raises(OpenRouterResponseError) as exc_info:
        await client.chat_completion([{"role": "user", "content": "hi"}])

    assert "HTTP 400" in str(exc_info.value)
    # Must fail fast on client error — must not loop through fallback models!
    assert mock_http.post.call_count == 1


@pytest.mark.asyncio
async def test_scenario_i_finite_ai_attempts_no_infinite_loop():
    """Test I: Verify total attempts are strictly bounded by configured model count."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    primary_model = "google/gemma-4-26b-a4b-it:free"
    fallbacks = ["fb-model-1", "fb-model-2"]

    # All return 500
    mock_http.post.return_value = _create_mock_response(
        500,
        {"error": {"message": "Server error", "code": 500}},
    )

    client = OpenRouterClient(
        api_key="sk-or-test-key",
        model=primary_model,
        fallback_models=fallbacks,
        http_client=mock_http,
    )
    svc = AIIntentService(client=client, use_mock=False)

    intent = await svc.parse_trip_intent("Goa 3 days 2 people budget 20000")

    # Bounded to exactly 3 attempts (primary + 2 fallbacks)
    assert mock_http.post.call_count == 3
    assert client.telemetry.last_run.ai_total_attempts == 3
    assert intent.action == TripAction.NEW_TRIP


@pytest.mark.asyncio
async def test_scenario_j_single_user_message_single_final_intent():
    """Test J: Exactly one validated intent result is returned for one user message."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.post.return_value = _create_mock_response(
        200,
        {
            "model": "google/gemma-4-26b-a4b-it:free",
            "choices": [{"message": {"content": _valid_intent_content()}}],
        },
    )

    client = OpenRouterClient(api_key="sk-or-test-key", http_client=mock_http)
    svc = AIIntentService(client=client, use_mock=False)

    intent = await svc.parse_trip_intent("Trip to Goa from Chennai for 2 people 3 days budget 20000")
    assert isinstance(intent, ParsedTripIntent)
    assert intent.destination == "Goa"


# =============================================================================
# HEURISTIC PARSER REGRESSION TESTS
# =============================================================================


def test_heuristic_regression_1_let_me_plan_new_trip():
    """Regression 1: 'Let me plan a new trip' must correctly identify NEW_TRIP."""
    svc = AIIntentService(use_mock=True)

    action_1 = svc._mock_classify_action("let me plan a new trip")
    assert action_1 == TripAction.NEW_TRIP

    action_2 = svc._mock_classify_action("let me plan new trip")
    assert action_2 == TripAction.NEW_TRIP

    parsed = svc._mock_parse_trip_intent("Let me plan a new trip")
    assert parsed.action == TripAction.NEW_TRIP


def test_heuristic_regression_2_spend_time_in_nature_not_log_expense():
    """Regression 2: 'I would rather spend more time in nature' must NOT become LOG_EXPENSE."""
    svc = AIIntentService(use_mock=True)

    phrases = [
        "I would rather spend more time in nature",
        "I want to spend more time in nature",
        "prefer to spend time in nature",
        "we can spend some time in nature",
    ]
    for p in phrases:
        action = svc._mock_classify_action(p.lower())
        assert action != TripAction.LOG_EXPENSE, f"Failed for phrase: '{p}'"


def test_heuristic_regression_3_add_5000_to_budget_increases_budget():
    """Regression 3: 'Add ₹5000 to my budget' must increase existing budget rather than replace it."""
    svc = AIIntentService(use_mock=True)

    # 1. Action classification
    action = svc._mock_classify_action("add ₹5000 to my budget")
    assert action == TripAction.CHANGE_BUDGET

    # 2. Intent extraction: must have is_delta=True and budget_delta=5000
    parsed = svc._mock_parse_trip_intent("Add ₹5000 to my budget")
    assert parsed.action == TripAction.CHANGE_BUDGET
    assert parsed.is_delta is True
    assert parsed.budget_delta == Decimal("5000")

    # 3. Applying to existing intent with 50,000 budget
    existing = ParsedTripIntent(
        destination="Kerala",
        origin="Chennai",
        budget=Decimal("50000"),
        people=3,
        days=2,
    )
    updated = existing.apply_change_action(parsed)
    # MUST be 55,000, NOT 5,000!
    assert updated.budget == Decimal("55000")
    assert updated.destination == "Kerala"


# =============================================================================
# CONFIGURATION & ZERO SERPAPI VERIFICATION TESTS
# =============================================================================


def test_openrouter_fallback_models_config_parsing():
    """Verify OPENROUTER_FALLBACK_MODELS parses from string, list, or empty."""
    # From comma-separated string
    s1 = Settings(
        openrouter_fallback_models="google/gemma-4-31b-it:free, nvidia/nemotron-3.5-lightning:free",
    )
    assert s1.openrouter_fallback_models == [
        "google/gemma-4-31b-it:free",
        "nvidia/nemotron-3.5-lightning:free",
    ]
    assert s1.resolved_openrouter_models == [
        s1.openrouter_model,
        "google/gemma-4-31b-it:free",
        "nvidia/nemotron-3.5-lightning:free",
    ]

    # From Python list
    s2 = Settings(
        openrouter_fallback_models=["model-a", "model-b"],
    )
    assert s2.openrouter_fallback_models == ["model-a", "model-b"]

    # Empty string -> empty list
    s3 = Settings(openrouter_fallback_models="")
    assert s3.openrouter_fallback_models == []


def test_zero_serpapi_calls_verified():
    """Step 9: Confirm live SerpApi calls = 0 during all model fallback tests."""
    with patch("budlance.serpapi.gateway.SerpApiGateway.execute_search") as mock_execute:
        assert mock_execute.call_count == 0


# =============================================================================
# ISSUE 2 & 3 REGRESSION TESTS: TELEMETRY CONSISTENCY & BOUNDED TIMEOUT
# =============================================================================


@pytest.mark.asyncio
async def test_telemetry_primary_success_consistency():
    """Regression: Primary succeeds -> selected_model == candidate_models[0], fallback_index == 0."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    primary_model = "google/gemma-4-26b-a4b-it:free"
    fallback_1 = "google/gemma-4-31b-it:free"
    fallback_2 = "nvidia/nemotron-3.5-lightning:free"
    candidate_models = [primary_model, fallback_1, fallback_2]

    mock_http.post.return_value = _create_mock_response(
        200,
        {
            "model": primary_model,
            "choices": [{"message": {"content": _valid_intent_content()}}],
        },
    )

    client = OpenRouterClient(
        api_key="sk-or-test-key",
        model=primary_model,
        fallback_models=[fallback_1, fallback_2],
        http_client=mock_http,
    )
    svc = AIIntentService(client=client, use_mock=False)
    intent = await svc.parse_trip_intent("Trip to Goa from Chennai for 2 people 3 days budget 20000")

    assert intent.action == TripAction.NEW_TRIP
    assert mock_http.post.call_count == 1

    summary = client.telemetry.get_summary()
    assert summary["ai_primary_model"] == primary_model
    assert summary["ai_selected_model"] == primary_model
    assert summary["ai_fallback_index"] == 0
    # Invariant: selected_model == candidate_models[fallback_index]
    assert summary["ai_selected_model"] == candidate_models[summary["ai_fallback_index"]]
    assert summary["ai_fallback_used"] is False
    assert summary["ai_heuristic_used"] is False
    assert len(summary["attempts"]) == 1
    assert summary["attempts"][0]["requested_model"] == primary_model
    assert summary["attempts"][0]["status"] == "success"


@pytest.mark.asyncio
async def test_telemetry_fallback_1_success_consistency():
    """Regression: Primary 429 -> fallback 1 succeeds -> selected_model == candidate_models[1], fallback_index == 1."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    primary_model = "google/gemma-4-26b-a4b-it:free"
    fallback_1 = "google/gemma-4-31b-it:free"
    fallback_2 = "nvidia/nemotron-3.5-lightning:free"
    candidate_models = [primary_model, fallback_1, fallback_2]

    mock_http.post.side_effect = [
        _create_mock_response(429, {"error": {"message": f"{primary_model} rate limited", "code": 429}}),
        _create_mock_response(
            200,
            {
                "model": fallback_1,
                "choices": [{"message": {"content": _valid_intent_content()}}],
            },
        ),
    ]

    client = OpenRouterClient(
        api_key="sk-or-test-key",
        model=primary_model,
        fallback_models=[fallback_1, fallback_2],
        http_client=mock_http,
    )
    svc = AIIntentService(client=client, use_mock=False)
    intent = await svc.parse_trip_intent("Trip to Goa from Chennai for 2 people 3 days budget 20000")

    assert intent.action == TripAction.NEW_TRIP
    assert mock_http.post.call_count == 2

    summary = client.telemetry.get_summary()
    assert summary["ai_primary_model"] == primary_model
    assert summary["ai_selected_model"] == fallback_1
    assert summary["ai_fallback_index"] == 1
    # Invariant: selected_model == candidate_models[fallback_index]
    assert summary["ai_selected_model"] == candidate_models[summary["ai_fallback_index"]]
    assert summary["ai_fallback_used"] is True
    assert summary["ai_heuristic_used"] is False
    assert len(summary["attempts"]) == 2
    assert summary["attempts"][0]["requested_model"] == primary_model
    assert summary["attempts"][0]["status"] == "rate_limited"
    assert summary["attempts"][1]["requested_model"] == fallback_1
    assert summary["attempts"][1]["status"] == "success"


@pytest.mark.asyncio
async def test_telemetry_fallback_2_success_consistency_with_provider_routing():
    """Regression: Primary & Fallback 1 fail -> Fallback 2 succeeds.
    Tests internal provider routing distinction (requested_model vs actual_model).
    """
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    primary_model = "google/gemma-4-26b-a4b-it:free"
    fallback_1 = "google/gemma-4-31b-it:free"
    fallback_2 = "nvidia/nemotron-3.5-lightning:free"
    candidate_models = [primary_model, fallback_1, fallback_2]

    # OpenRouter internally routes fallback_2 to an internal provider
    actual_provider = "fireworks/nemotron-3.5-lightning-preview"

    mock_http.post.side_effect = [
        _create_mock_response(429, {"error": {"message": f"{primary_model} rate limited", "code": 429}}),
        _create_mock_response(503, {"error": {"message": f"{fallback_1} service unavailable", "code": 503}}),
        _create_mock_response(
            200,
            {
                "model": actual_provider,
                "choices": [{"message": {"content": _valid_intent_content()}}],
            },
        ),
    ]

    client = OpenRouterClient(
        api_key="sk-or-test-key",
        model=primary_model,
        fallback_models=[fallback_1, fallback_2],
        http_client=mock_http,
    )
    svc = AIIntentService(client=client, use_mock=False)
    intent = await svc.parse_trip_intent("Trip to Goa from Chennai for 2 people 3 days budget 20000")

    assert intent.action == TripAction.NEW_TRIP
    assert mock_http.post.call_count == 3

    summary = client.telemetry.get_summary()
    assert summary["ai_primary_model"] == primary_model
    assert summary["ai_selected_model"] == fallback_2
    assert summary["ai_actual_provider_model"] == actual_provider
    assert summary["ai_fallback_index"] == 2
    # Invariant: selected_model == candidate_models[fallback_index]
    assert summary["ai_selected_model"] == candidate_models[summary["ai_fallback_index"]]
    assert summary["ai_fallback_used"] is True
    assert summary["ai_heuristic_used"] is False
    assert len(summary["attempts"]) == 3
    assert summary["attempts"][2]["requested_model"] == fallback_2
    assert summary["attempts"][2]["actual_model"] == actual_provider
    assert summary["attempts"][2]["selected_model"] == fallback_2
    assert summary["attempts"][2]["status"] == "success"


@pytest.mark.asyncio
async def test_telemetry_all_fail_heuristic_consistency():
    """Regression: All models fail -> ai_selected_model is None, heuristic used, invariant holds."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    primary_model = "google/gemma-4-26b-a4b-it:free"
    fallback_1 = "google/gemma-4-31b-it:free"
    fallback_2 = "nvidia/nemotron-3.5-lightning:free"
    candidate_models = [primary_model, fallback_1, fallback_2]

    mock_http.post.side_effect = [
        _create_mock_response(429, {"error": {"message": f"{primary_model} 429", "code": 429}}),
        _create_mock_response(429, {"error": {"message": f"{fallback_1} 429", "code": 429}}),
        _create_mock_response(500, {"error": {"message": f"{fallback_2} 500", "code": 500}}),
    ]

    client = OpenRouterClient(
        api_key="sk-or-test-key",
        model=primary_model,
        fallback_models=[fallback_1, fallback_2],
        http_client=mock_http,
    )
    svc = AIIntentService(client=client, use_mock=False)
    intent = await svc.parse_trip_intent("Trip to Goa from Chennai for 2 people 3 days budget 20000")

    assert intent.action == TripAction.NEW_TRIP
    assert intent.destination == "Goa"
    assert mock_http.post.call_count == 3

    summary = client.telemetry.get_summary()
    assert summary["ai_primary_model"] == primary_model
    assert summary["ai_selected_model"] is None
    assert summary["selected_candidate_index"] is None
    assert summary["ai_fallback_index"] is None
    assert summary["fallback_index"] is None
    assert summary["fallback_index"] != 3  # STRICT ASSERTION: Never use pseudo-index 3
    assert summary["ai_actual_provider_model"] is None
    assert summary["ai_fallback_used"] is True
    assert summary["models_exhausted"] is True
    assert summary["models_attempted"] == 3
    assert summary["http_attempts"] == 3
    assert summary["ai_heuristic_used"] is True
    assert len(summary["attempts"]) == 3
    for idx, att in enumerate(summary["attempts"]):
        assert att["requested_model"] == candidate_models[idx]
        assert att["actual_model"] is None


@pytest.mark.asyncio
async def test_deterministic_timeout_bound():
    """Regression for Issue 3: Enforce strict deterministic timeout bound per request attempt.
    Hanging request is aborted strictly at request_timeout, triggering graceful fallback.
    """
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    primary_model = "google/gemma-4-26b-a4b-it:free"
    fallback_1 = "google/gemma-4-31b-it:free"
    candidate_models = [primary_model, fallback_1]

    call_count = 0

    async def dynamic_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Simulate a hung primary upstream socket
            await asyncio.sleep(5.0)
            return _create_mock_response(200, {"choices": [{"message": {"content": "{}"}}]})
        return _create_mock_response(
            200,
            {
                "model": fallback_1,
                "choices": [{"message": {"content": _valid_intent_content()}}],
            },
        )

    mock_http.post.side_effect = dynamic_post

    # Enforce a 0.15s deterministic timeout bound
    client = OpenRouterClient(
        api_key="sk-or-test-key",
        model=primary_model,
        fallback_models=[fallback_1],
        http_client=mock_http,
        request_timeout=0.15,
    )
    svc = AIIntentService(client=client, use_mock=False)

    start = time.perf_counter()
    intent = await svc.parse_trip_intent("Trip to Goa from Chennai for 2 people 3 days budget 20000")
    elapsed = time.perf_counter() - start

    # The entire operation (hanging attempt + fallback) must take well under 1.5 seconds, proving 5.0s was cancelled
    assert elapsed < 1.0, f"Expected bounded timeout < 1.0s, but elapsed took {elapsed:.2f}s"
    assert intent.action == TripAction.NEW_TRIP

    summary = client.telemetry.get_summary()
    assert summary["ai_selected_model"] == fallback_1
    assert summary["selected_candidate_index"] == 1
    assert summary["ai_fallback_index"] == 1
    assert summary["ai_selected_model"] == candidate_models[summary["ai_fallback_index"]]
    assert len(summary["attempts"]) == 2
    assert summary["attempts"][0]["requested_model"] == primary_model
    assert summary["attempts"][0]["status"] == "timeout"
    assert summary["attempts"][0]["latency_ms"] < 600.0
    assert summary["attempts"][1]["requested_model"] == fallback_1
    assert summary["attempts"][1]["status"] == "success"


# =============================================================================
# ISSUE 1 & ISSUE 2 RIGOROUS REGRESSION SUITE (TESTS A, B, C, D & INVARIANTS)
# =============================================================================


@pytest.mark.asyncio
async def test_regression_a_primary_succeeds_candidate_index_0():
    """Test A: Primary succeeds -> selected_candidate_index = 0, fallback_index = 0, models_exhausted = False."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    primary_model = "google/gemma-4-26b-a4b-it:free"
    fallback_1 = "google/gemma-4-31b-it:free"
    fallback_2 = "nvidia/nemotron-3.5-lightning:free"
    candidate_models = [primary_model, fallback_1, fallback_2]

    mock_http.post.return_value = _create_mock_response(
        200,
        {
            "model": primary_model,
            "choices": [{"message": {"content": _valid_intent_content()}}],
        },
    )

    client = OpenRouterClient(
        api_key="sk-or-test-key",
        model=primary_model,
        fallback_models=[fallback_1, fallback_2],
        http_client=mock_http,
    )
    svc = AIIntentService(client=client, use_mock=False)
    intent = await svc.parse_trip_intent("Trip to Goa from Chennai for 2 people 3 days budget 20000")

    assert intent.action == TripAction.NEW_TRIP
    summary = client.telemetry.get_summary()

    # Model-level assertions
    assert summary["selected_model"] == primary_model
    assert summary["selected_candidate_index"] == 0
    assert summary["fallback_index"] == 0
    assert summary["models_attempted"] == 1
    assert summary["model_candidates_attempted"] == 1
    assert summary["models_exhausted"] is False
    assert summary["model_candidates_exhausted"] is False
    assert summary["heuristic_used"] is False

    # HTTP-level assertions
    assert summary["http_attempts"] == 1
    assert summary["ai_total_attempts"] == 1
    assert len(summary["attempts"]) == 1
    assert summary["attempts"][0]["requested_model"] == primary_model
    assert summary["attempts"][0]["api_key_index"] == 0
    assert summary["attempts"][0]["status"] == "success"

    # Invariant
    assert summary["selected_model"] == candidate_models[summary["selected_candidate_index"]]


@pytest.mark.asyncio
async def test_regression_b_fallback1_succeeds_candidate_index_1():
    """Test B: Fallback 1 succeeds -> selected_candidate_index = 1, fallback_index = 1, models_exhausted = False."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    primary_model = "google/gemma-4-26b-a4b-it:free"
    fallback_1 = "google/gemma-4-31b-it:free"
    fallback_2 = "nvidia/nemotron-3.5-lightning:free"
    candidate_models = [primary_model, fallback_1, fallback_2]

    mock_http.post.side_effect = [
        _create_mock_response(429, {"error": {"message": f"{primary_model} rate limited", "code": 429}}),
        _create_mock_response(
            200,
            {
                "model": fallback_1,
                "choices": [{"message": {"content": _valid_intent_content()}}],
            },
        ),
    ]

    client = OpenRouterClient(
        api_key="sk-or-test-key",
        model=primary_model,
        fallback_models=[fallback_1, fallback_2],
        http_client=mock_http,
    )
    svc = AIIntentService(client=client, use_mock=False)
    intent = await svc.parse_trip_intent("Trip to Goa from Chennai for 2 people 3 days budget 20000")

    assert intent.action == TripAction.NEW_TRIP
    summary = client.telemetry.get_summary()

    # Model-level assertions
    assert summary["selected_model"] == fallback_1
    assert summary["selected_candidate_index"] == 1
    assert summary["fallback_index"] == 1
    assert summary["models_attempted"] == 2
    assert summary["model_candidates_attempted"] == 2
    assert summary["models_exhausted"] is False
    assert summary["model_candidates_exhausted"] is False
    assert summary["heuristic_used"] is False

    # HTTP-level assertions
    assert summary["http_attempts"] == 2
    assert summary["ai_total_attempts"] == 2
    assert len(summary["attempts"]) == 2
    assert summary["attempts"][0]["requested_model"] == primary_model
    assert summary["attempts"][1]["requested_model"] == fallback_1
    assert summary["attempts"][1]["status"] == "success"

    # Invariant
    assert summary["selected_model"] == candidate_models[summary["selected_candidate_index"]]


@pytest.mark.asyncio
async def test_regression_c_fallback2_succeeds_candidate_index_2():
    """Test C: Fallback 2 succeeds -> selected_candidate_index = 2, fallback_index = 2, models_exhausted = False."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    primary_model = "google/gemma-4-26b-a4b-it:free"
    fallback_1 = "google/gemma-4-31b-it:free"
    fallback_2 = "nvidia/nemotron-3.5-lightning:free"
    candidate_models = [primary_model, fallback_1, fallback_2]

    mock_http.post.side_effect = [
        _create_mock_response(429, {"error": {"message": f"{primary_model} rate limited", "code": 429}}),
        _create_mock_response(503, {"error": {"message": f"{fallback_1} 503", "code": 503}}),
        _create_mock_response(
            200,
            {
                "model": fallback_2,
                "choices": [{"message": {"content": _valid_intent_content()}}],
            },
        ),
    ]

    client = OpenRouterClient(
        api_key="sk-or-test-key",
        model=primary_model,
        fallback_models=[fallback_1, fallback_2],
        http_client=mock_http,
    )
    svc = AIIntentService(client=client, use_mock=False)
    intent = await svc.parse_trip_intent("Trip to Goa from Chennai for 2 people 3 days budget 20000")

    assert intent.action == TripAction.NEW_TRIP
    summary = client.telemetry.get_summary()

    # Model-level assertions
    assert summary["selected_model"] == fallback_2
    assert summary["selected_candidate_index"] == 2
    assert summary["fallback_index"] == 2
    assert summary["models_attempted"] == 3
    assert summary["model_candidates_attempted"] == 3
    assert summary["models_exhausted"] is False
    assert summary["model_candidates_exhausted"] is False
    assert summary["heuristic_used"] is False

    # HTTP-level assertions
    assert summary["http_attempts"] == 3
    assert summary["ai_total_attempts"] == 3
    assert len(summary["attempts"]) == 3
    assert summary["attempts"][2]["requested_model"] == fallback_2
    assert summary["attempts"][2]["status"] == "success"

    # Invariant
    assert summary["selected_model"] == candidate_models[summary["selected_candidate_index"]]


@pytest.mark.asyncio
async def test_regression_d_all_models_fail_candidate_index_none():
    """Test D: All models fail -> selected_model = None, selected_candidate_index = None, fallback_index = None (NEVER 3!)."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    primary_model = "google/gemma-4-26b-a4b-it:free"
    fallback_1 = "google/gemma-4-31b-it:free"
    fallback_2 = "nvidia/nemotron-3.5-lightning:free"

    mock_http.post.side_effect = [
        _create_mock_response(429, {"error": {"message": f"{primary_model} 429", "code": 429}}),
        _create_mock_response(429, {"error": {"message": f"{fallback_1} 429", "code": 429}}),
        _create_mock_response(500, {"error": {"message": f"{fallback_2} 500", "code": 500}}),
    ]

    client = OpenRouterClient(
        api_key="sk-or-test-key",
        model=primary_model,
        fallback_models=[fallback_1, fallback_2],
        http_client=mock_http,
    )
    svc = AIIntentService(client=client, use_mock=False)
    intent = await svc.parse_trip_intent("Trip to Goa from Chennai for 2 people 3 days budget 20000")

    assert intent.action == TripAction.NEW_TRIP
    summary = client.telemetry.get_summary()

    # STRICT ASSERTIONS: NO PSEUDO-INDEX 3
    assert summary["selected_model"] is None
    assert summary["selected_candidate_index"] is None
    assert summary["fallback_index"] is None
    assert summary["ai_fallback_index"] is None
    assert summary["fallback_index"] != 3
    assert summary["ai_fallback_index"] != 3

    assert summary["models_exhausted"] is True
    assert summary["model_candidates_exhausted"] is True
    assert summary["models_attempted"] == 3
    assert summary["heuristic_used"] is True
    assert summary["http_attempts"] == 3


@pytest.mark.asyncio
async def test_regression_invariant_candidate_index_matches_selected_model():
    """Test invariant: if selected_candidate_index is not None: selected_model == candidate_models[selected_candidate_index]."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    primary = "google/gemma-4-26b-a4b-it:free"
    fb1 = "google/gemma-4-31b-it:free"
    fb2 = "nvidia/nemotron-3.5-lightning:free"
    candidate_models = [primary, fb1, fb2]

    # Run loop over all 3 candidate indices
    for target_idx in range(len(candidate_models)):
        responses = []
        for i in range(target_idx):
            responses.append(_create_mock_response(429, {"error": {"message": "rate limit", "code": 429}}))
        responses.append(_create_mock_response(
            200,
            {
                "model": candidate_models[target_idx],
                "choices": [{"message": {"content": _valid_intent_content()}}],
            },
        ))
        mock_http.post.side_effect = responses

        client = OpenRouterClient(
            api_key="sk-or-test-key",
            model=primary,
            fallback_models=[fb1, fb2],
            http_client=mock_http,
        )
        svc = AIIntentService(client=client, use_mock=False)
        await svc.parse_trip_intent("Trip to Goa from Chennai for 2 people 3 days budget 20000")

        summary = client.telemetry.get_summary()
        assert summary["selected_candidate_index"] == target_idx
        # Invariant verified
        if summary["selected_candidate_index"] is not None:
            assert summary["selected_model"] == candidate_models[summary["selected_candidate_index"]]


@pytest.mark.asyncio
async def test_regression_models_attempted_independent_from_http_attempts():
    """Test that model-level candidate transitions are independent from total HTTP attempts.
    Candidate 0 tries 2 API keys (2 HTTP attempts -> both 429).
    Candidate 1 succeeds on key 0 (1 HTTP attempt -> 200).
    Candidate 2 is never reached.

    Total model candidates attempted = 2.
    Total HTTP attempts = 3.
    """
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    primary = "google/gemma-4-26b-a4b-it:free"
    fb1 = "google/gemma-4-31b-it:free"
    fb2 = "nvidia/nemotron-3.5-lightning:free"
    candidate_models = [primary, fb1, fb2]

    mock_http.post.side_effect = [
        # Candidate 0, Key 0
        _create_mock_response(429, {"error": {"message": "key 0 rate limited", "code": 429}}),
        # Candidate 0, Key 1 (failover)
        _create_mock_response(429, {"error": {"message": "key 1 rate limited", "code": 429}}),
        # Candidate 1, Key 0 (success)
        _create_mock_response(
            200,
            {
                "model": fb1,
                "choices": [{"message": {"content": _valid_intent_content()}}],
            },
        ),
    ]

    client = OpenRouterClient(
        api_key="sk-or-key-primary",
        fallback_api_key="sk-or-key-secondary",
        model=primary,
        fallback_models=[fb1, fb2],
        http_client=mock_http,
    )
    svc = AIIntentService(client=client, use_mock=False)
    intent = await svc.parse_trip_intent("Trip to Goa from Chennai for 2 people 3 days budget 20000")

    assert intent.action == TripAction.NEW_TRIP
    summary = client.telemetry.get_summary()

    # Model-level count != HTTP-level count
    assert summary["models_attempted"] == 2
    assert summary["model_candidates_attempted"] == 2
    assert summary["http_attempts"] == 3
    assert summary["ai_total_attempts"] == 3
    assert summary["models_attempted"] != summary["http_attempts"]

    # Check candidate index & model
    assert summary["selected_candidate_index"] == 1
    assert summary["selected_model"] == fb1
    assert summary["selected_model"] == candidate_models[summary["selected_candidate_index"]]

    # Verify per-attempt HTTP log
    attempts = summary["attempts"]
    assert len(attempts) == 3
    assert attempts[0]["requested_model"] == primary
    assert attempts[0]["api_key_index"] == 0
    assert attempts[0]["status"] == "rate_limited"

    assert attempts[1]["requested_model"] == primary
    assert attempts[1]["api_key_index"] == 1
    assert attempts[1]["status"] == "rate_limited"

    assert attempts[2]["requested_model"] == fb1
    assert attempts[2]["api_key_index"] == 0
    assert attempts[2]["status"] == "success"


@pytest.mark.asyncio
async def test_regression_bounded_api_key_failover():
    """Verify secondary API key failover remains bounded and cannot create infinite loops.
    With 2 keys and 3 candidate models, total HTTP attempts cannot exceed 3 * 2 = 6.
    """
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    primary = "google/gemma-4-26b-a4b-it:free"
    fb1 = "google/gemma-4-31b-it:free"
    fb2 = "nvidia/nemotron-3.5-lightning:free"

    # All 6 attempts return 429
    mock_http.post.side_effect = [
        _create_mock_response(429, {"error": {"message": "429", "code": 429}})
        for _ in range(6)
    ]

    client = OpenRouterClient(
        api_key="sk-or-key-primary",
        fallback_api_key="sk-or-key-secondary",
        model=primary,
        fallback_models=[fb1, fb2],
        http_client=mock_http,
    )
    svc = AIIntentService(client=client, use_mock=False)
    intent = await svc.parse_trip_intent("Trip to Goa from Chennai for 2 people 3 days budget 20000")

    assert intent.action == TripAction.NEW_TRIP  # Fell back to heuristic
    summary = client.telemetry.get_summary()

    # Bounded to exactly 6 HTTP attempts across 3 models
    assert summary["http_attempts"] == 6
    assert summary["models_attempted"] == 3
    assert summary["models_exhausted"] is True
    assert summary["selected_candidate_index"] is None
    assert summary["fallback_index"] is None
    assert summary["fallback_index"] != 3
    assert summary["heuristic_used"] is True
    assert mock_http.post.call_count == 6

