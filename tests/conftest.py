"""Pytest configuration and global fixtures for Budlance test suite."""

import pytest
from budlance.config import get_settings
from budlance.db.client import get_supabase_client


@pytest.fixture(autouse=True)
def isolate_unit_test_environment(monkeypatch):
    """Ensure unit tests run in offline/isolated mode by default.

    Prevents unit tests from attempting live network/database calls or
    violating foreign key constraints with dummy test UUIDs.
    """
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setenv("SUPABASE_KEY", "")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("SERPAPI_LIVE_ENABLED", "false")
    monkeypatch.setenv("SERPAPI_API_KEY", "")
    monkeypatch.setenv("SERPAPI_FALLBACK_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_FALLBACK_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    get_settings.cache_clear()
    get_supabase_client.cache_clear()
    yield
    get_settings.cache_clear()
    get_supabase_client.cache_clear()
