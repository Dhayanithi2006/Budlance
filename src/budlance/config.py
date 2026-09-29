"""Centralized settings configuration for Budlance using pydantic-settings."""

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

    # AI Gateway (OpenRouter)
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    openrouter_model: str = Field(
        default="anthropic/claude-3.5-sonnet", alias="OPENROUTER_MODEL"
    )

    # Live Travel Search (SerpApi)
    serpapi_api_key: str = Field(default="", alias="SERPAPI_API_KEY")

    # Database & Cache (Supabase PostgreSQL)
    supabase_url: str = Field(default="", alias="SUPABASE_URL")
    supabase_key: str = Field(default="", alias="SUPABASE_KEY")
    database_url: str = Field(default="", alias="DATABASE_URL")

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
            "has_telegram_token": self.has_telegram_token,
            "has_openrouter_credentials": self.has_openrouter_credentials,
            "has_serpapi_credentials": self.has_serpapi_credentials,
            "has_supabase_credentials": self.has_supabase_credentials,
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
            f"has_serpapi={self.has_serpapi_credentials}, "
            f"has_supabase={self.has_supabase_credentials})"
        )


@lru_cache
def get_settings() -> Settings:
    """Cached singleton instance of Settings."""
    return Settings()
