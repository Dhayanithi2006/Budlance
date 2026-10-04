"""Centralized settings configuration for Budlance using pydantic-settings."""

from decimal import Decimal
from functools import lru_cache
from typing import Any
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central application settings loaded from environment or .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Telegram Bot
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")

    # AI Gateway (OpenRouter or Google Gemini)
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    openrouter_model: str = Field(
        default="anthropic/claude-3.5-sonnet", alias="OPENROUTER_MODEL"
    )
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-flash-latest", alias="GEMINI_MODEL")

    # Live Travel Search (SerpApi)
    serpapi_api_key: str = Field(default="", alias="SERPAPI_API_KEY")
    serpapi_live_enabled: bool = Field(default=True, alias="SERPAPI_LIVE_ENABLED")

    # Database & Cache (Supabase PostgreSQL)
    supabase_url: str = Field(default="", alias="SUPABASE_URL")
    supabase_key: str = Field(default="", alias="SUPABASE_KEY")
    database_url: str = Field(default="", alias="DATABASE_URL")

    # Trip Pass Monetization & Payments
    enable_trip_pass: bool = Field(default=False, alias="ENABLE_TRIP_PASS")
    trip_pass_amount: Decimal = Field(default=Decimal("49.00"), alias="TRIP_PASS_AMOUNT")
    trip_pass_currency: str = Field(default="INR", alias="TRIP_PASS_CURRENCY")

    # Planning Heuristics (explicitly marked offline planning defaults, not live market quotes)
    food_budget_rates: dict[str, int] = Field(
        default_factory=lambda: {"budget": 400, "standard": 800, "comfort": 1500},
        alias="FOOD_BUDGET_RATES",
    )
    transit_rates: dict[str, int] = Field(
        default_factory=lambda: {"auto_per_km": 15, "cab_per_km": 22, "metro_bus_daily_pass": 100},
        alias="TRANSIT_RATES",
    )
    rescue_reserve_percent: Decimal = Field(
        default=Decimal("0.10"),
        alias="RESCUE_RESERVE_PERCENT",
    )
    maps_search_radius_meters: int = Field(default=25000, alias="MAPS_SEARCH_RADIUS_METERS")
    maps_zoom_level: int = Field(default=14, alias="MAPS_ZOOM_LEVEL")
    openrouter_fallback_model: str = Field(
        default="meta-llama/llama-3.3-70b-instruct:free",
        alias="OPENROUTER_FALLBACK_MODEL",
    )

    # Destination-evaluation quota guards (applied even when live mode is enabled)
    max_live_candidates_per_request: int = Field(
        default=6, alias="MAX_LIVE_CANDIDATES_PER_REQUEST"
    )
    max_live_provider_calls_per_request: int = Field(
        default=10, alias="MAX_LIVE_PROVIDER_CALLS_PER_REQUEST"
    )
    destination_evaluation_timeout_seconds: float = Field(
        default=20.0, alias="DESTINATION_EVALUATION_TIMEOUT_SECONDS"
    )

    # Payment Gateways (Razorpay primary India rail; Stripe sandbox/test rail)
    razorpay_key_id: str = Field(default="", alias="RAZORPAY_KEY_ID")
    razorpay_key_secret: str = Field(default="", alias="RAZORPAY_KEY_SECRET")
    stripe_api_key: str = Field(default="", alias="STRIPE_API_KEY")
    stripe_webhook_secret: str = Field(default="", alias="STRIPE_WEBHOOK_SECRET")

    # App & Server Settings
    app_env: str = Field(default="development", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    webhook_url: str = Field(default="", alias="WEBHOOK_URL")
    port: int = Field(default=8000, alias="PORT")

    @property
    def has_telegram_token(self) -> bool:
        """Check if a non-empty Telegram bot token is present."""
        return bool(self.telegram_bot_token and self.telegram_bot_token.strip())

    @property
    def has_openrouter_credentials(self) -> bool:
        """Check if OpenRouter API credentials are configured."""
        return bool(self.openrouter_api_key and self.openrouter_api_key.strip())

    @property
    def has_gemini_credentials(self) -> bool:
        """Check if Gemini API credentials are configured."""
        return bool(self.gemini_api_key and self.gemini_api_key.strip())

    @property
    def has_ai_credentials(self) -> bool:
        """Check if any AI gateway credentials (OpenRouter or Gemini) are configured."""
        return self.has_openrouter_credentials or self.has_gemini_credentials

    @property
    def has_serpapi_credentials(self) -> bool:
        """Check if SerpApi credentials are configured."""
        return bool(self.serpapi_api_key and self.serpapi_api_key.strip())

    @property
    def has_supabase_credentials(self) -> bool:
        """Check if Supabase credentials are configured."""
        return bool(
            self.supabase_url and self.supabase_url.strip() and
            self.supabase_key and self.supabase_key.strip()
        )

    @property
    def has_razorpay_credentials(self) -> bool:
        """Check if Razorpay API keys are configured."""
        return bool(self.razorpay_key_id and self.razorpay_key_id.strip() and self.razorpay_key_secret and self.razorpay_key_secret.strip())

    @property
    def has_stripe_credentials(self) -> bool:
        """Check if Stripe API key is configured."""
        return bool(self.stripe_api_key and self.stripe_api_key.strip())

    @property
    def is_production(self) -> bool:
        """Check if running in production mode."""
        return self.app_env.strip().lower() == "production"

    def safe_dict(self) -> dict[str, Any]:
        """Return non-sensitive configuration parameters for logging and inspection."""
        return {
            "app_env": self.app_env,
            "log_level": self.log_level,
            "port": self.port,
            "webhook_url": self.webhook_url or None,
            "openrouter_model": self.openrouter_model,
            "gemini_model": self.gemini_model,
            "has_telegram_token": self.has_telegram_token,
            "has_openrouter_credentials": self.has_openrouter_credentials,
            "has_gemini_credentials": self.has_gemini_credentials,
            "has_ai_credentials": self.has_ai_credentials,
            "has_serpapi_credentials": self.has_serpapi_credentials,
            "serpapi_live_enabled": self.serpapi_live_enabled,
            "has_supabase_credentials": self.has_supabase_credentials,
            "enable_trip_pass": self.enable_trip_pass,
            "trip_pass_amount": float(self.trip_pass_amount),
            "trip_pass_currency": self.trip_pass_currency,
            "has_razorpay_credentials": self.has_razorpay_credentials,
            "has_stripe_credentials": self.has_stripe_credentials,
        }

    def __repr__(self) -> str:
        """Ensure sensitive tokens and keys are never printed in debug representations."""
        return (
            f"Settings("
            f"app_env='{self.app_env}', "
            f"log_level='{self.log_level}', "
            f"port={self.port}, "
            f"has_telegram={self.has_telegram_token}, "
            f"has_openrouter={self.has_openrouter_credentials}, "
            f"has_gemini={self.has_gemini_credentials}, "
            f"has_serpapi={self.has_serpapi_credentials}, "
            f"has_supabase={self.has_supabase_credentials})"
        )


@lru_cache
def get_settings() -> Settings:
    """Cached singleton instance of Settings."""
    return Settings()
