"""Place normalizer converting Google Local/Maps results to PlaceOption models."""

import logging
from decimal import Decimal
from typing import Any
from budlance.schemas.travel import PlaceOption
from budlance.serpapi.models import TravelDataEnvelope

logger = logging.getLogger(__name__)


def normalize_places(envelope: TravelDataEnvelope) -> list[PlaceOption]:
    """Parse Google Maps/Local places envelope into a list of normalized PlaceOption models."""
    results: list[PlaceOption] = []
    data = envelope.data

    if not isinstance(data, dict):
        logger.warning("Place data is not a valid dictionary envelope.")
        return results

    raw_places = data.get("local_results") or data.get("place_results") or data.get("organic_results") or []
    if isinstance(raw_places, dict):
        places = [raw_places]
    elif isinstance(raw_places, list):
        places = raw_places
    else:
        return results

    for item in places:
        if not isinstance(item, dict):
            continue

        name = item.get("title") or item.get("name")
        if not name:
            continue

        category = item.get("type")
        if not category and isinstance(item.get("types"), list) and item["types"]:
            category = item["types"][0]

        address = item.get("address")
        rating = item.get("rating")
        reviews = item.get("reviews")
        price_level = item.get("price")

        place_id = item.get("place_id") or item.get("data_id")
        coords = item.get("gps_coordinates") if isinstance(item.get("gps_coordinates"), dict) else {}
        lat = coords.get("latitude") if isinstance(coords.get("latitude"), (int, float)) else None
        lng = coords.get("longitude") if isinstance(coords.get("longitude"), (int, float)) else None
        hours = item.get("operating_hours") or item.get("hours") or item.get("open_state")
        link = item.get("link") or item.get("website")

        option = PlaceOption(
            name=str(name),
            category=str(category) if category else None,
            address=str(address) if address else None,
            rating=float(rating) if rating is not None else None,
            review_count=int(reviews) if reviews is not None else None,
            price_level=str(price_level) if price_level else None,
            estimated_cost=Decimal("0.00"),
            place_id=str(place_id) if place_id else None,
            latitude=float(lat) if lat is not None else None,
            longitude=float(lng) if lng is not None else None,
            opening_hours=str(hours) if hours else None,
            link=str(link) if link else None,
            source=envelope.source,
            is_fallback=envelope.is_fallback,
            retrieval_timestamp=envelope.provenance.retrieval_timestamp if envelope.provenance else envelope.created_at,
            provenance=envelope.provenance,
        )
        results.append(option)

    return results
