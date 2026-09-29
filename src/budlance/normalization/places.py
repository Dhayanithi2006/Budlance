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

    places = data.get("local_results") or []
    if not isinstance(places, list):
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

        option = PlaceOption(
            name=str(name),
            category=str(category) if category else None,
            address=str(address) if address else None,
            rating=float(rating) if rating is not None else None,
            review_count=int(reviews) if reviews is not None else None,
            price_level=str(price_level) if price_level else None,
            estimated_cost=Decimal("0.00"),
            source=envelope.source,
            is_fallback=envelope.is_fallback,
        )
        results.append(option)

    return results
