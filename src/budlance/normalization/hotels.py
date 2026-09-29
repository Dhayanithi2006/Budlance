"""Hotel normalizer converting Google Hotels responses to HotelOption models."""

import logging
import re
from typing import Any
from budlance.normalization.utils import parse_price_and_currency
from budlance.schemas.travel import HotelOption
from budlance.serpapi.models import TravelDataEnvelope

logger = logging.getLogger(__name__)


def _extract_hotel_class(raw_class: Any) -> int | None:
    if raw_class is None:
        return None
    if isinstance(raw_class, int):
        return raw_class
    match = re.search(r"(\d+)", str(raw_class))
    return int(match.group(1)) if match else None


def normalize_hotels(envelope: TravelDataEnvelope) -> list[HotelOption]:
    """Parse Google Hotels data envelope into a list of normalized HotelOption models."""
    results: list[HotelOption] = []
    data = envelope.data

    if not isinstance(data, dict):
        logger.warning("Hotel data is not a valid dictionary envelope.")
        return results

    properties = data.get("properties") or []
    if not isinstance(properties, list):
        return results

    for prop in properties:
        if not isinstance(prop, dict):
            continue

        name = prop.get("name")
        if not name:
            continue

        # Extract price per night and total rate
        rate_info = prop.get("rate_per_night") or {}
        raw_night_price = (
            rate_info.get("extracted_lowest")
            or rate_info.get("lowest")
            or prop.get("price")
        )
        price_per_night, currency = parse_price_and_currency(raw_night_price)

        total_info = prop.get("total_rate") or {}
        raw_total_price = (
            total_info.get("extracted_lowest")
            or total_info.get("lowest")
            or raw_night_price
        )
        total_price, _ = parse_price_and_currency(raw_total_price)

        hotel_class = _extract_hotel_class(prop.get("hotel_class"))
        address = prop.get("address") or prop.get("location")
        rating = prop.get("overall_rating") or prop.get("rating")
        review_count = prop.get("reviews")
        link = prop.get("link")

        option = HotelOption(
            name=str(name),
            hotel_class=hotel_class,
            address=str(address) if address else None,
            price_per_night=price_per_night,
            total_price=total_price,
            currency=currency,
            rating=float(rating) if rating is not None else None,
            review_count=int(review_count) if review_count is not None else None,
            deep_link=str(link) if link else None,
            source=envelope.source,
            is_fallback=envelope.is_fallback,
        )
        results.append(option)

    return results
