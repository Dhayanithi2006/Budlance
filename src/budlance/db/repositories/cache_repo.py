"""Cache repository for reusing SerpApi search results stored in Supabase."""

from typing import Any
from datetime import datetime, timezone
from supabase import Client
from budlance.db.client import get_supabase_client
from budlance.db.models import SearchCache


class CacheRepository:
    """Data access repository for SerpApi query caching."""

    _booking_relay_store: dict[str, SearchCache] = {}

    def __init__(self, client: Client | None = None) -> None:
        self._client = client or get_supabase_client()
        self._memory_store: dict[str, SearchCache] = {}


    def get_cached_search(self, query_hash: str) -> SearchCache | None:
        """Fetch cached search result if it exists and has not expired."""
        now = datetime.now(timezone.utc)

        if self._client:
            res = (
                self._client.table("search_cache")
                .select("*")
                .eq("query_hash", query_hash)
                .gt("expires_at", now.isoformat())
                .limit(1)
                .execute()
            )
            if res.data:
                return SearchCache.model_validate(res.data[0])
            return None

        cached = self._memory_store.get(query_hash)
        if cached and cached.expires_at > now:
            return cached
        return None

    def set_cached_search(self, record: SearchCache) -> SearchCache:
        """Store or update a cached SerpApi search result."""
        if self._client:
            payload = {
                "query_hash": record.query_hash,
                "engine": record.engine,
                "params_json": record.params_json,
                "response_data": record.response_data,
                "expires_at": record.expires_at.isoformat(),
                "created_at": record.created_at.isoformat(),
            }
            res = (
                self._client.table("search_cache")
                .upsert(payload, on_conflict="query_hash")
                .execute()
            )
            if res.data:
                return SearchCache.model_validate(res.data[0])
            return record

        self._memory_store[record.query_hash] = record
        return record

    def store_booking_request(
        self,
        booking_id: str,
        booking_request: dict[str, Any],
        ttl_seconds: int = 3600,
    ) -> str:
        """Store flight booking request (url + post_data) with short expiry."""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        record = SearchCache(
            query_hash=f"book:{booking_id}",
            engine="booking_relay",
            params_json={"booking_id": booking_id},
            response_data=booking_request,
            expires_at=now + timedelta(seconds=ttl_seconds),
            created_at=now,
        )
        if self._client:
            try:
                self.set_cached_search(record)
            except Exception:
                pass
        CacheRepository._booking_relay_store[booking_id] = record
        self._memory_store[f"book:{booking_id}"] = record
        return booking_id

    def get_booking_request(self, booking_id: str) -> dict[str, Any] | None:
        """Retrieve stored flight booking request if not expired."""
        now = datetime.now(timezone.utc)
        cached = self.get_cached_search(f"book:{booking_id}")
        if not cached:
            cached = CacheRepository._booking_relay_store.get(booking_id)
        if cached and cached.expires_at > now and isinstance(cached.response_data, dict):
            return cached.response_data
        return None


