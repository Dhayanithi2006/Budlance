"""Cache repository for reusing SerpApi search results stored in Supabase."""

from datetime import datetime, timezone
from supabase import Client
from budlance.db.client import get_supabase_client
from budlance.db.models import SearchCache


class CacheRepository:
    """Data access repository for SerpApi query caching."""

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
            res = (
                self._client.table("search_cache")
                .upsert({
                    "id": str(record.id),
                    "query_hash": record.query_hash,
                    "engine": record.engine,
                    "params_json": record.params_json,
                    "response_data": record.response_data,
                    "expires_at": record.expires_at.isoformat(),
                    "created_at": record.created_at.isoformat(),
                })
                .execute()
            )
            return SearchCache.model_validate(res.data[0])

        self._memory_store[record.query_hash] = record
        return record
