"""Tests for centralized configuration module."""

from budlance.config import Settings


def test_settings_default_instantiation(monkeypatch):
    """Verify that settings instantiate without error when external credentials are empty."""
    # Ensure environment variables are cleared for isolated testing
    for var in [
        "TELEGRAM_BOT_TOKEN",
        "OPENROUTER_API_KEY",
        "OPENROUTER_FALLBACK_API_KEY",
        "OPENROUTER_MODEL",
        "SERPAPI_API_KEY",
        "SERPAPI_FALLBACK_API_KEY",
        "SUPABASE_URL",
        "SUPABASE_KEY",
        "DATABASE_URL",
        "APP_ENV",
        "LOG_LEVEL",
        "WEBHOOK_URL",
        "PORT",
    ]:
        monkeypatch.delenv(var, raising=False)

    settings = Settings(_env_file=None)

    assert settings.app_env == "development"
    assert settings.log_level == "INFO"
    assert settings.port == 8000
    assert settings.webhook_url == ""
    assert settings.openrouter_model == "anthropic/claude-3.5-sonnet"
    assert settings.openrouter_fallback_api_key == ""
    assert settings.serpapi_fallback_api_key == ""
    assert settings.has_telegram_token is False
    assert settings.has_openrouter_credentials is False
    assert settings.has_serpapi_credentials is False
    assert settings.has_supabase_credentials is False
    assert settings.is_production is False


def test_settings_populated(monkeypatch):
    """Verify that all environment variables are correctly loaded and mapped."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token_123")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-testkey")
    monkeypatch.setenv("OPENROUTER_FALLBACK_API_KEY", "sk-or-v1-fallbackkey")
    monkeypatch.setenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct")
    monkeypatch.setenv("SERPAPI_API_KEY", "serp_secret_key")
    monkeypatch.setenv("SERPAPI_FALLBACK_API_KEY", "serp_fallback_key")
    monkeypatch.setenv("SUPABASE_URL", "https://xyz.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "sb_secret_key")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/db")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("WEBHOOK_URL", "https://api.budlance.com/webhook")
    monkeypatch.setenv("PORT", "9000")

    settings = Settings(_env_file=None)

    assert settings.telegram_bot_token == "test_token_123"
    assert settings.openrouter_api_key == "sk-or-v1-testkey"
    assert settings.openrouter_fallback_api_key == "sk-or-v1-fallbackkey"
    assert settings.openrouter_model == "meta-llama/llama-3.3-70b-instruct"
    assert settings.serpapi_api_key == "serp_secret_key"
    assert settings.serpapi_fallback_api_key == "serp_fallback_key"
    assert settings.supabase_url == "https://xyz.supabase.co"
    assert settings.supabase_key == "sb_secret_key"
    assert settings.database_url == "postgresql://test:test@localhost:5432/db"
    assert settings.app_env == "production"
    assert settings.log_level == "DEBUG"
    assert settings.webhook_url == "https://api.budlance.com/webhook"
    assert settings.port == 9000

    assert settings.has_telegram_token is True
    assert settings.has_openrouter_credentials is True
    assert settings.has_openrouter_fallback is True
    assert settings.has_serpapi_credentials is True
    assert settings.has_serpapi_fallback is True
    assert settings.has_supabase_credentials is True
    assert settings.is_production is True


def test_secret_masking(monkeypatch):
    """Verify that sensitive values are not exposed in repr or safe_dict."""
    fake_secret = "super_sensitive_api_secret_999"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", fake_secret)
    monkeypatch.setenv("OPENROUTER_API_KEY", fake_secret)
    monkeypatch.setenv("OPENROUTER_FALLBACK_API_KEY", fake_secret)
    monkeypatch.setenv("SERPAPI_API_KEY", fake_secret)
    monkeypatch.setenv("SERPAPI_FALLBACK_API_KEY", fake_secret)
    monkeypatch.setenv("SUPABASE_KEY", fake_secret)
    monkeypatch.setenv("STRIPE_API_KEY", fake_secret)

    settings = Settings(_env_file=None)
    repr_str = repr(settings)
    safe_info = settings.safe_dict()

    # Neither repr nor safe_dict should contain the sensitive secret string
    assert fake_secret not in repr_str
    assert fake_secret not in str(safe_info)
    assert safe_info["has_telegram_token"] is True
    assert safe_info["has_openrouter_credentials"] is True
    assert safe_info["has_openrouter_fallback"] is True
    assert safe_info["has_serpapi_credentials"] is True
    assert safe_info["has_serpapi_fallback"] is True
    assert safe_info["has_stripe_credentials"] is True


def test_business_constants_defaults():
    """Verify default values and Decimal types of all 8 centralized business constants."""
    from decimal import Decimal
    settings = Settings(_env_file=None)

    assert settings.trip_pass_amount == Decimal("49.00")
    assert settings.reoptimization_material_threshold == Decimal("500.00")
    assert settings.budget_transport_warning_ratio == Decimal("0.35")
    assert settings.budget_hotel_warning_ratio == Decimal("0.40")
    assert settings.budget_survival_warning_ratio == Decimal("0.30")
    assert settings.budget_attractions_warning_ratio == Decimal("0.15")
    assert settings.budget_activities_ratio == Decimal("0.05")
    assert settings.budget_fallback_deficit_ratio == Decimal("0.20")


def test_business_constants_env_overrides(monkeypatch):
    """Verify that all 8 business constants support environment-variable overrides with Decimal parsing."""
    from decimal import Decimal

    monkeypatch.setenv("TRIP_PASS_AMOUNT", "99.00")
    monkeypatch.setenv("REOPTIMIZATION_MATERIAL_THRESHOLD", "750.00")
    monkeypatch.setenv("BUDGET_TRANSPORT_WARNING_RATIO", "0.38")
    monkeypatch.setenv("BUDGET_HOTEL_WARNING_RATIO", "0.45")
    monkeypatch.setenv("BUDGET_SURVIVAL_WARNING_RATIO", "0.25")
    monkeypatch.setenv("BUDGET_ATTRACTIONS_WARNING_RATIO", "0.18")
    monkeypatch.setenv("BUDGET_ACTIVITIES_RATIO", "0.08")
    monkeypatch.setenv("BUDGET_FALLBACK_DEFICIT_RATIO", "0.22")

    settings = Settings(_env_file=None)

    assert settings.trip_pass_amount == Decimal("99.00")
    assert settings.reoptimization_material_threshold == Decimal("750.00")
    assert settings.budget_transport_warning_ratio == Decimal("0.38")
    assert settings.budget_hotel_warning_ratio == Decimal("0.45")
    assert settings.budget_survival_warning_ratio == Decimal("0.25")
    assert settings.budget_attractions_warning_ratio == Decimal("0.18")
    assert settings.budget_activities_ratio == Decimal("0.08")
    assert settings.budget_fallback_deficit_ratio == Decimal("0.22")


def test_trip_pass_amount_propagation_across_layers(monkeypatch):
    """Verify TRIP_PASS_AMOUNT=99.00 dynamically propagates to model, repo, and formatter."""
    from decimal import Decimal
    from uuid import uuid4
    from budlance.config import get_settings
    from budlance.db.models import TripPass
    from budlance.db.repositories.trip_pass_repo import TripPassRepository
    from budlance.orchestrator.formatter import format_free_summary
    from budlance.engine.models import BudgetBreakdown

    monkeypatch.setenv("TRIP_PASS_AMOUNT", "99.00")
    get_settings.cache_clear()

    # 1. Pydantic Model default factory
    pass_obj = TripPass(trip_id=uuid4(), telegram_user_id=1, telegram_chat_id=1)
    assert pass_obj.amount == Decimal("99.00")

    # 2. Repository runtime resolution
    repo = TripPassRepository()
    repo._client = None  # in-memory path
    created = repo.create_pass(uuid4(), 1, 1)
    assert created.amount == Decimal("99.00")

    # 3. Formatter presentation
    breakdown = BudgetBreakdown(
        total_budget=Decimal("10000"),
        bucket_a_fixed=Decimal("5000"),
        bucket_b_survival=Decimal("3000"),
        bucket_c_activities=Decimal("1000"),
        bucket_d_rescue=Decimal("1000"),
        transport_cost=Decimal("2000"),
        hotel_cost=Decimal("3000"),
        food_cost=Decimal("2000"),
        local_transit_cost=Decimal("1000"),
        total_allocated=Decimal("10000"),
        remaining_surplus=Decimal("0"),
    )
    msg = format_free_summary("Goa", 3, 2, breakdown)
    assert "🎟️ *Budlance Trip Pass: INR 99.00*" in msg

    get_settings.cache_clear()


def test_budget_warning_ratios_propagation(monkeypatch):
    """Verify budget warning ratios dynamically affect ReverseBudgetEngine contributor diagnostics."""
    from decimal import Decimal
    from budlance.config import get_settings
    from budlance.engine.budget import ReverseBudgetEngine
    from budlance.schemas.travel import FlightOption, FoodEstimate, HotelOption, LocalTransitEstimate

    # Set threshold low so even a small cost triggers warning
    monkeypatch.setenv("BUDGET_TRANSPORT_WARNING_RATIO", "0.10")
    get_settings.cache_clear()

    engine = ReverseBudgetEngine()
    # Total budget 10,000, transport 1,500 (> 10% = 1,000)
    # Mandatory costs = transport (1500) + hotel (9000) + food (1000) = 11,500 > 10,000 (infeasible)
    res = engine.evaluate(
        total_budget=Decimal("10000"),
        people=1,
        days=2,
        transport=FlightOption(price=Decimal("1500"), airline="TestAir"),
        hotel=HotelOption(total_price=Decimal("9000"), name="TestHotel"),
        food_estimate=FoodEstimate(tier="standard", daily_cost_per_person=Decimal("500"), total_cost=Decimal("1000"), people=1, days=2),
        local_transit_estimate=LocalTransitEstimate(mode="metro_bus", total_cost=Decimal("0")),
    )

    assert any("Transport" in c for c in res.major_cost_contributors)
    get_settings.cache_clear()


def test_reoptimizer_material_threshold_propagation(monkeypatch):
    """Verify REOPTIMIZATION_MATERIAL_THRESHOLD dynamically controls the reoptimization gate."""
    from decimal import Decimal
    from unittest.mock import MagicMock
    from budlance.config import get_settings
    from budlance.db.models import Trip
    from budlance.lifecycle.reoptimizer import is_meaningful_reoptimization_trigger

    # Set threshold to 100.00
    monkeypatch.setenv("REOPTIMIZATION_MATERIAL_THRESHOLD", "100.00")
    get_settings.cache_clear()

    mock_trip = MagicMock(spec=Trip)
    mock_trip.id = MagicMock()
    mock_trip.budget_total = Decimal("10000")
    mock_repo = MagicMock()

    # 150.00 >= 100.00 -> warrants re-optimization trigger evaluation
    result_large = is_meaningful_reoptimization_trigger(
        trip=mock_trip,
        ledger_repo=mock_repo,
        recent_expense_amount=Decimal("150.00"),
    )
    assert result_large is True

    get_settings.cache_clear()

