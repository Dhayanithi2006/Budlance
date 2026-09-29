"""Route normalizer converting Google Maps Directions results to RouteOption models."""

import logging
from typing import Any
from budlance.schemas.travel import RouteOption
from budlance.serpapi.models import TravelDataEnvelope

logger = logging.getLogger(__name__)


def normalize_routes(envelope: TravelDataEnvelope) -> list[RouteOption]:
    """Parse Google Maps Directions envelope into a list of normalized RouteOption models."""
    results: list[RouteOption] = []
    data = envelope.data

    if not isinstance(data, dict):
        logger.warning("Directions data is not a valid dictionary envelope.")
        return results

    routes = data.get("routes") or data.get("directions") or []
    if not isinstance(routes, list):
        return results

    for r in routes:
        if not isinstance(r, dict):
            continue

        legs = r.get("legs") or []
        first_leg = legs[0] if isinstance(legs, list) and legs else {}

        origin = first_leg.get("start_address") or r.get("start_address") or "Origin"
        destination = first_leg.get("end_address") or r.get("end_address") or "Destination"

        # Extract distance in meters and convert to km
        dist_info = first_leg.get("distance") or r.get("distance") or {}
        if isinstance(dist_info, dict):
            dist_val = dist_info.get("value", 0)  # meters
            distance_km = round(dist_val / 1000.0, 2)
        elif isinstance(dist_info, (int, float)):
            distance_km = round(float(dist_info) / 1000.0, 2)
        else:
            distance_km = 0.0

        # Extract duration in seconds and convert to minutes
        dur_info = first_leg.get("duration") or r.get("duration") or {}
        if isinstance(dur_info, dict):
            dur_val = dur_info.get("value", 0)  # seconds
            duration_mins = round(dur_val / 60)
        elif isinstance(dur_info, (int, float)):
            duration_mins = round(float(dur_info) / 60)
        else:
            duration_mins = 0

        summary = r.get("summary") or first_leg.get("summary")

        option = RouteOption(
            origin=str(origin),
            destination=str(destination),
            distance_km=distance_km,
            duration_minutes=duration_mins,
            summary=str(summary) if summary else None,
            source=envelope.source,
            is_fallback=envelope.is_fallback,
        )
        results.append(option)

    return results
