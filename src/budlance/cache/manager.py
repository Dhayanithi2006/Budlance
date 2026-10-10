"""Cache & Fallback Manager enforcing the frozen data resolution hierarchy."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from typing import Any
from uuid import UUID, uuid4

import time
from budlance.cache.fallback import FallbackDataProvider
from budlance.db.models import SearchCache, utc_now
from budlance.db.repositories.cache_repo import CacheRepository
from budlance.db.repositories.usage_repo import UsageRepository
from budlance.serpapi.gateway import SerpApiGateway
from budlance.serpapi.models import DataProvenance, DataSource, ProvenanceType, TravelDataEnvelope

logger = logging.getLogger(__name__)

ENGINE_TTLS: dict[str, int] = {
    "google_flights": 12,       # 12 hours for volatile flight pricing
    "google_hotels": 24,        # 24 hours for daily hotel room pricing
    "google_travel_explore": 48,# 48 hours for destination explore feeds
    "google_maps": 168,         # 7 days for local places
    "google_maps_directions": 168,
    "google": 24,               # 24 hours for live events
}


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
        default_ttl_hours: int = 168,
    ) -> None:
        self.gateway = gateway or SerpApiGateway()
        self.cache_repo = cache_repo or CacheRepository()
        self.usage_repo = usage_repo or UsageRepository()
        self.fallback = fallback_provider or FallbackDataProvider()
        self.default_ttl_hours = default_ttl_hours
        self.default_ttl = timedelta(hours=default_ttl_hours)

    async def get_travel_data(
        self,
        engine: str,
        params: dict[str, Any],
        trip_id: UUID | None = None,
        check_fallback_first: bool = False,
        bypass_cache: bool = False,
    ) -> TravelDataEnvelope:
        """Resolve travel data following: Cache Check -> Fallback Check -> Live SerpApi.

        Resolution order:
        1. Cache Check: If recent unexpired cached result exists (and bypass_cache=False), return CACHED envelope with provenance.
        2. Fallback Check: If query matches known static fallback (e.g. train/bus corridors).
        3. Live SerpApi: Call gateway, record usage, update cache with TTL, return LIVE envelope with provenance.
        """
        query_hash = compute_query_hash(engine, params)

        # 1. Cache Check
        if not bypass_cache:
            cached_entry = self.cache_repo.get_cached_search(query_hash)
            if cached_entry:
                age_sec = (datetime.now(timezone.utc) - cached_entry.created_at).total_seconds()
                logger.info(
                    "[CACHE] engine=%s resolution=CACHE_HIT hash=%s age_sec=%.1f",
                    engine,
                    query_hash[:8],
                    age_sec,
                )
                self.usage_repo.record_api_call(trip_id=trip_id, engine=engine, cached=True)
                provenance = DataProvenance(
                    provenance_type=ProvenanceType.CACHED_PROVIDER_RESULT,
                    provider="serpapi",
                    engine=engine,
                    retrieval_timestamp=cached_entry.created_at,
                    cache_hit=True,
                    cache_age_seconds=max(0.0, round(age_sec, 1)),
                    query_params=params,
                    price_scope="quote",
                    currency=params.get("currency", "INR"),
                    http_status=200,
                    latency_sec=0.0,
                )
                return TravelDataEnvelope(
                    source=DataSource.CACHED,
                    engine=engine,
                    query_hash=query_hash,
                    data=cached_entry.response_data,
                    is_fallback=False,
                    status="success",
                    created_at=cached_entry.created_at,
                    provenance=provenance,
                )

        # 2. Transit Fallback (trains and buses are offline catalogs only, never sent to SerpApi)
        origin = params.get("origin") or params.get("from")
        destination = params.get("destination") or params.get("to")

        now_ts = utc_now()
        if engine in ("trains", "train_corridors"):
            train_data = self.fallback.get_train_corridor(str(origin), str(destination)) if origin and destination else None
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
                    created_at=now_ts,
                    provenance=DataProvenance(
                        provenance_type=ProvenanceType.OFFLINE_FALLBACK,
                        provider="offline_catalog",
                        engine=engine,
                        retrieval_timestamp=now_ts,
                        cache_hit=False,
                        query_params=params,
                        price_scope="offline_table",
                    ),
                )
            logger.info(
                "[CACHE] engine=%s resolution=NO_CORRIDOR_DATA origin=%s destination=%s outcome=empty_envelope_returned",
                engine,
                origin,
                destination,
            )
            return TravelDataEnvelope(
                source=DataSource.FALLBACK,
                engine=engine,
                query_hash=query_hash,
                data={},
                is_fallback=True,
                status="empty",
                created_at=now_ts,
                provenance=DataProvenance(
                    provenance_type=ProvenanceType.UNKNOWN,
                    provider="offline_catalog",
                    engine=engine,
                    retrieval_timestamp=now_ts,
                    cache_hit=False,
                    query_params=params,
                ),
            )
        elif engine in ("buses", "bus_corridors"):
            bus_data = self.fallback.get_bus_corridor(str(origin), str(destination)) if origin and destination else None
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
                    created_at=now_ts,
                    provenance=DataProvenance(
                        provenance_type=ProvenanceType.OFFLINE_FALLBACK,
                        provider="offline_catalog",
                        engine=engine,
                        retrieval_timestamp=now_ts,
                        cache_hit=False,
                        query_params=params,
                        price_scope="offline_table",
                    ),
                )
            logger.info(
                "[CACHE] engine=%s resolution=NO_CORRIDOR_DATA origin=%s destination=%s outcome=empty_envelope_returned",
                engine,
                origin,
                destination,
            )
            return TravelDataEnvelope(
                source=DataSource.FALLBACK,
                engine=engine,
                query_hash=query_hash,
                data={},
                is_fallback=True,
                status="empty",
                created_at=now_ts,
                provenance=DataProvenance(
                    provenance_type=ProvenanceType.UNKNOWN,
                    provider="offline_catalog",
                    engine=engine,
                    retrieval_timestamp=now_ts,
                    cache_hit=False,
                    query_params=params,
                ),
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
                        created_at=now_ts,
                        provenance=DataProvenance(
                            provenance_type=ProvenanceType.OFFLINE_FALLBACK,
                            provider="offline_catalog",
                            engine=engine,
                            retrieval_timestamp=now_ts,
                            cache_hit=False,
                            query_params=params,
                            price_scope="offline_table",
                        ),
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
                created_at=now_ts,
                provenance=DataProvenance(
                    provenance_type=ProvenanceType.UNKNOWN,
                    provider="unconfigured",
                    engine=engine,
                    retrieval_timestamp=now_ts,
                    cache_hit=False,
                    query_params=params,
                ),
            )

        # 3. Live SerpApi Call
        logger.info(
            "[CACHE] engine=%s resolution=LIVE_CALL hash=%s",
            engine,
            query_hash[:8],
        )
        t0 = time.perf_counter()
        now_ts = utc_now()
        try:
            live_data = await self.gateway.execute_search(engine, params)
            latency_sec = time.perf_counter() - t0
        except Exception as exc:
            latency_sec = time.perf_counter() - t0
            # Cross-pollination guard: only use transit corridor fallback for transit engines.
            # A failed google_flights call must NEVER return train corridor data.
            _is_transit_engine = engine in ("trains", "train_corridors", "buses", "bus_corridors")
            if _is_transit_engine and origin and destination:
                fb = self.fallback.get_train_corridor(str(origin), str(destination))
                if fb:
                    logger.info(
                        "[CACHE] engine=%s resolution=FALLBACK_AFTER_LIVE_ERROR error=%s",
                        engine,
                        type(exc).__name__,
                    )
                    fb_provenance = DataProvenance(
                        provenance_type=ProvenanceType.OFFLINE_FALLBACK,
                        provider="offline_catalog",
                        engine=engine,
                        retrieval_timestamp=now_ts,
                        cache_hit=False,
                        cache_age_seconds=None,
                        query_params=params,
                        price_scope="offline_table",
                        currency=params.get("currency", "INR"),
                        http_status=None,
                        latency_sec=round(latency_sec, 3),
                    )
                    return TravelDataEnvelope(
                        source=DataSource.FALLBACK,
                        engine=engine,
                        query_hash=query_hash,
                        data=fb,
                        is_fallback=True,
                        status="success",
                        created_at=now_ts,
                        provenance=fb_provenance,
                    )
            # For non-transit engines (flights, hotels, maps) or when no corridor exists:
            # return an empty envelope — the caller's own fallback will handle it.
            logger.warning(
                "[CACHE] engine=%s resolution=LIVE_CALL_FAILED outcome=empty_envelope error=%s: %s",
                engine,
                type(exc).__name__,
                exc,
            )
            err_provenance = DataProvenance(
                provenance_type=ProvenanceType.UNKNOWN,
                provider="serpapi",
                engine=engine,
                retrieval_timestamp=now_ts,
                cache_hit=False,
                cache_age_seconds=None,
                query_params=params,
                price_scope=None,
                currency=params.get("currency", "INR"),
                http_status=getattr(exc, "status_code", 500) if hasattr(exc, "status_code") else 500,
                latency_sec=round(latency_sec, 3),
            )
            return TravelDataEnvelope(
                source=DataSource.FALLBACK,
                engine=engine,
                query_hash=query_hash,
                data={},
                is_fallback=True,
                status="error",
                created_at=now_ts,
                provenance=err_provenance,
            )

        # Record live API call
        self.usage_repo.record_api_call(trip_id=trip_id, engine=engine, cached=False)

        # Store in Supabase cache with engine-specific TTL
        ttl_hours = ENGINE_TTLS.get(engine, self.default_ttl_hours)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
        cache_record = SearchCache(
            id=uuid4(),
            query_hash=query_hash,
            engine=engine,
            params_json=params,
            response_data=live_data,
            expires_at=expires_at,
            created_at=now_ts,
        )
        self.cache_repo.set_cached_search(cache_record)

        live_provenance = DataProvenance(
            provenance_type=ProvenanceType.LIVE_PROVIDER,
            provider="serpapi",
            engine=engine,
            retrieval_timestamp=now_ts,
            cache_hit=False,
            cache_age_seconds=None,
            query_params=params,
            price_scope="quote",
            currency=params.get("currency", "INR"),
            http_status=200,
            latency_sec=round(latency_sec, 3),
        )

        return TravelDataEnvelope(
            source=DataSource.LIVE,
            engine=engine,
            query_hash=query_hash,
            data=live_data,
            is_fallback=False,
            status="success",
            created_at=now_ts,
            provenance=live_provenance,
        )

    async def get_flight_booking_options(
        self,
        booking_token: str,
        trip_id: UUID | None = None,
        bypass_cache: bool = False,
    ) -> TravelDataEnvelope:
        """Resolve flight booking options for a given booking_token via Cache or Live SerpApi."""
        return await self.get_travel_data(
            engine="google_flights",
            params={"booking_token": booking_token},
            trip_id=trip_id,
            bypass_cache=bypass_cache,
        )
