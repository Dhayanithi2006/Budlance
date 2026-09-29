"""Tests for centralized configuration module."""

from budlance.config import Settings


def test_settings_default_instantiation(monkeypatch):
    """Verify that settings instantiate without error when external credentials are empty."""
    # Ensure environment variables are cleared for isolated testing
    for var in [
        "TELEGRAM_BOT_TOKEN",
        "OPENROUTER_API_KEY",
        "OPENROUTER_MODEL",
        "SERPAPI_API_KEY",
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
    assert settings.has_telegram_token is False
    assert settings.has_openrouter_credentials is False
    assert settings.has_serpapi_credentials is False
    assert settings.has_supabase_credentials is False
    assert settings.is_production is False


def test_settings_populated(monkeypatch):
    """Verify that all 11 environment variables are correctly loaded and mapped."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token_123")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-testkey")
    monkeypatch.setenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct")
    monkeypatch.setenv("SERPAPI_API_KEY", "serp_secret_key")
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
    assert settings.openrouter_model == "meta-llama/llama-3.3-70b-instruct"
    assert settings.serpapi_api_key == "serp_secret_key"
    assert settings.supabase_url == "https://xyz.supabase.co"
    assert settings.supabase_key == "sb_secret_key"
    assert settings.database_url == "postgresql://test:test@localhost:5432/db"
    assert settings.app_env == "production"
    assert settings.log_level == "DEBUG"
    assert settings.webhook_url == "https://api.budlance.com/webhook"
    assert settings.port == 9000

    assert settings.has_telegram_token is True
    assert settings.has_openrouter_credentials is True
    assert settings.has_serpapi_credentials is True
    assert settings.has_supabase_credentials is True
    assert settings.is_production is True


def test_secret_masking(monkeypatch):
    """Verify that sensitive values are not exposed in repr or safe_dict."""
    fake_secret = "super_sensitive_api_secret_999"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", fake_secret)
    monkeypatch.setenv("OPENROUTER_API_KEY", fake_secret)
    monkeypatch.setenv("SERPAPI_API_KEY", fake_secret)
    monkeypatch.setenv("SUPABASE_KEY", fake_secret)

    settings = Settings(_env_file=None)
    repr_str = repr(settings)
    safe_info = settings.safe_dict()

    # Neither repr nor safe_dict should contain the sensitive secret string
    assert fake_secret not in repr_str
    assert fake_secret not in str(safe_info)
    assert safe_info["has_telegram_token"] is True
    assert safe_info["has_openrouter_credentials"] is True
    assert safe_info["has_serpapi_credentials"] is True
