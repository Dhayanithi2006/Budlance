"""Cache & Fallback Manager enforcing the frozen data resolution hierarchy."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from typing import Any
from uuid import UUID, uuid4

from budlance.cache.fallback import FallbackDataProvider
from budlance.db.models import SearchCache, utc_now
from budlance.db.repositories.cache_repo import CacheRepository
from budlance.db.repositories.usage_repo import UsageRepository
from budlance.serpapi.gateway import SerpApiGateway
from budlance.serpapi.models import DataSource, TravelDataEnvelope

logger = logging.getLogger(__name__)


def compute_query_hash(engine: str, params: dict[str, Any]) -> str:
    """Generate deterministic SHA-256 hash for engine and query params."""
    normalized_params = json.dumps(params, sort_keys=True, default=str)
    raw = f"{engine}:{normalized_params}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class CacheFallbackManager:
    """Orchestrates Cache -> Fallback -> Live SerpApi resolution."""

    def __init__(
        self,
        gateway: SerpApiGateway | None = None,
        cache_repo: CacheRepository | None = None,
        usage_repo: UsageRepository | None = None,
        fallback_provider: FallbackDataProvider | None = None,
        default_ttl_hours: int = 6,
    ) -> None:
        self.gateway = gateway or SerpApiGateway()
        self.cache_repo = cache_repo or CacheRepository()
        self.usage_repo = usage_repo or UsageRepository()
        self.fallback = fallback_provider or FallbackDataProvider()
        self.default_ttl = timedelta(hours=default_ttl_hours)

    async def get_travel_data(
        self,
        engine: str,
        params: dict[str, Any],
        trip_id: UUID | None = None,
        check_fallback_first: bool = False,
    ) -> TravelDataEnvelope:
        """Resolve travel data following: Cache Check -> Fallback Check -> Live SerpApi.

        Resolution order:
        1. Cache Check: If recent unexpired cached result exists, return CACHED envelope.
        2. Fallback Check: If query matches known static fallback (e.g. train/bus corridors).
        3. Live SerpApi: Call gateway, record usage, update cache with TTL, return LIVE envelope.
        """
        query_hash = compute_query_hash(engine, params)

        # 1. Cache Check
        cached_entry = self.cache_repo.get_cached_search(query_hash)
        if cached_entry:
            logger.info(
                "[CACHE] engine=%s resolution=CACHE_HIT hash=%s",
                engine,
                query_hash[:8],
            )
            self.usage_repo.record_api_call(trip_id=trip_id, engine=engine, cached=True)
            return TravelDataEnvelope(
                source=DataSource.CACHED,
                engine=engine,
                query_hash=query_hash,
                data=cached_entry.response_data,
                is_fallback=False,
                status="success",
            )

        # 2. Fallback Check (for corridor transit or when explicitly requested / offline)
        origin = params.get("origin") or params.get("from")
        destination = params.get("destination") or params.get("to")

        if origin and destination:
            if engine in ("trains", "train_corridors"):
                train_data = self.fallback.get_train_corridor(str(origin), str(destination))
                if train_data:
                    logger.info(
                        "[CACHE] engine=%s resolution=FALLBACK_CORRIDOR corridor_type=train origin=%s destination=%s",
                        engine,
                        origin,
                        destination,
                    )
                    return TravelDataEnvelope(
                        source=DataSource.FALLBACK,
                        engine=engine,
                        query_hash=query_hash,
                        data=train_data,
                        is_fallback=True,
                        status="success",
                    )
            elif engine in ("buses", "bus_corridors"):
                bus_data = self.fallback.get_bus_corridor(str(origin), str(destination))
                if bus_data:
                    logger.info(
                        "[CACHE] engine=%s resolution=FALLBACK_CORRIDOR corridor_type=bus origin=%s destination=%s",
                        engine,
                        origin,
                        destination,
                    )
                    return TravelDataEnvelope(
                        source=DataSource.FALLBACK,
                        engine=engine,
                        query_hash=query_hash,
                        data=bus_data,
                        is_fallback=True,
                        status="success",
                    )

        # If gateway does not have live credentials, try corridor fallback or return empty envelope
        if not self.gateway.has_credentials:
            if origin and destination:
                fallback_res = (
                    self.fallback.get_train_corridor(str(origin), str(destination))
                    or self.fallback.get_bus_corridor(str(origin), str(destination))
                )
                if fallback_res:
                    logger.info(
                        "[CACHE] engine=%s resolution=FALLBACK_CORRIDOR_NO_CREDS origin=%s destination=%s",
                        engine,
                        origin,
                        destination,
                    )
                    return TravelDataEnvelope(
                        source=DataSource.FALLBACK,
                        engine=engine,
                        query_hash=query_hash,
                        data=fallback_res,
                        is_fallback=True,
                        status="success",
                    )
            # No corridor data available — return empty envelope so callers use their own fallbacks
            logger.info(
                "[CACHE] engine=%s resolution=UNCONFIGURED outcome=empty_envelope_returned",
                engine,
            )
            return TravelDataEnvelope(
                source=DataSource.FALLBACK,
                engine=engine,
                query_hash=query_hash,
                data={},
                is_fallback=True,
                status="unconfigured",
            )

        # 3. Live SerpApi Call
        logger.info(
            "[CACHE] engine=%s resolution=LIVE_CALL hash=%s",
            engine,
            query_hash[:8],
        )
        try:
            live_data = await self.gateway.execute_search(engine, params)
        except Exception as exc:
            # If live call fails, try corridor fallback; otherwise return empty envelope for caller fallbacks
            if origin and destination:
                fb = self.fallback.get_train_corridor(str(origin), str(destination))
                if fb:
                    logger.info(
                        "[CACHE] engine=%s resolution=FALLBACK_AFTER_LIVE_ERROR error=%s",
                        engine,
                        type(exc).__name__,
                    )
                    return TravelDataEnvelope(
                        source=DataSource.FALLBACK,
                        engine=engine,
                        query_hash=query_hash,
                        data=fb,
                        is_fallback=True,
                        status="success",
                    )
            logger.warning(
                "[CACHE] engine=%s resolution=LIVE_CALL_FAILED outcome=empty_envelope error=%s: %s",
                engine,
                type(exc).__name__,
                exc,
            )
            return TravelDataEnvelope(
                source=DataSource.FALLBACK,
                engine=engine,
                query_hash=query_hash,
                data={},
                is_fallback=True,
                status="error",
            )

        # Record live API call
        self.usage_repo.record_api_call(trip_id=trip_id, engine=engine, cached=False)

        # Store in Supabase cache with TTL
        expires_at = datetime.now(timezone.utc) + self.default_ttl
        cache_record = SearchCache(
            id=uuid4(),
            query_hash=query_hash,
            engine=engine,
            params_json=params,
            response_data=live_data,
            expires_at=expires_at,
            created_at=utc_now(),
        )
        self.cache_repo.set_cached_search(cache_record)

        return TravelDataEnvelope(
            source=DataSource.LIVE,
            engine=engine,
            query_hash=query_hash,
            data=live_data,
            is_fallback=False,
            status="success",
        )
