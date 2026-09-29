"""Supabase PostgreSQL client management for Budlance.

Reads credentials safely via centralized settings. Does not expose secrets or
raise fatal errors when external credentials are empty in local/test mode.
"""

import logging
from functools import lru_cache
from typing import Any
from supabase import Client, create_client
from budlance.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache
def get_supabase_client() -> Client | None:
    """Initialize and return the Supabase client instance.

    Returns None if SUPABASE_URL or SUPABASE_KEY are not configured,
    allowing offline development and tests to proceed without crashing.
    """
    settings = get_settings()

    if not settings.has_supabase_credentials:
        logger.info("Supabase credentials unconfigured; running database layer in offline/mock mode.")
        return None

    try:
        client: Client = create_client(settings.supabase_url, settings.supabase_key)
        logger.info("Supabase client initialized successfully.")
        return client
    except Exception as exc:
        logger.warning("Failed to initialize Supabase client (%s: %s).", type(exc).__name__, exc)
        return None


def is_database_connected() -> bool:
    """Check if the Supabase client is configured and available."""
    client = get_supabase_client()
    return client is not None
