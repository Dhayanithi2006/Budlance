"""Unified DataNormalizer dispatching external envelopes to appropriate normalizers."""

import logging
from typing import Any
from budlance.normalization.events import normalize_events
from budlance.normalization.flights import normalize_flights
from budlance.normalization.hotels import normalize_hotels
from budlance.normalization.places import normalize_places
from budlance.normalization.routes import normalize_routes
from budlance.normalization.transit import normalize_transit_fallback
from budlance.serpapi.models import TravelDataEnvelope

logger = logging.getLogger(__name__)


class DataNormalizer:
    """Unified service for normalizing external data envelopes into domain travel options."""

    @staticmethod
    def normalize_flights(envelope: TravelDataEnvelope):
        return normalize_flights(envelope)

    @staticmethod
    def normalize_hotels(
        envelope: TravelDataEnvelope,
        nights: int = 1,
        check_in: str | None = None,
        check_out: str | None = None,
    ):
        return normalize_hotels(envelope, nights=nights, check_in=check_in, check_out=check_out)

    @staticmethod
    def normalize_places(envelope: TravelDataEnvelope):
        return normalize_places(envelope)

    @staticmethod
    def normalize_routes(envelope: TravelDataEnvelope):
        return normalize_routes(envelope)

    @staticmethod
    def normalize_transit(envelope: TravelDataEnvelope):
        return normalize_transit_fallback(envelope)

    @staticmethod
    def normalize_events(envelope: TravelDataEnvelope):
        return normalize_events(envelope)

    def normalize(self, envelope: TravelDataEnvelope) -> list[Any]:
        """Automatically route envelope to the appropriate normalizer based on engine and source."""
        engine = envelope.engine.lower()

        if "flight" in engine:
            return self.normalize_flights(envelope)
        if "hotel" in engine:
            return self.normalize_hotels(envelope)
        if "event" in engine or (engine == "google" and "events_results" in envelope.data):
            return self.normalize_events(envelope)
        if "map" in engine and "direction" in engine:
            return self.normalize_routes(envelope)
        if "map" in engine or "local" in engine:
            return self.normalize_places(envelope)
        if "train" in engine or "bus" in engine or envelope.is_fallback:
            return self.normalize_transit(envelope)

        logger.warning("No dedicated normalizer for engine=%s; returning empty list.", engine)
        return []
