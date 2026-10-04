"""Hotel normalizer converting Google Hotels responses to HotelOption models."""

import logging
import re
from decimal import Decimal
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
    """Parse Google Hotels data envelope into a list of normalized HotelOption models.

    Strict validation rules:
    - Valid property: Must be a dict and have a non-empty name.
    - Valid positive price: Must have strictly positive (> 0) price_per_night and total_price.
    - Missing / invalid / zero / negative price -> reject property.
    """
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
        if not name or not str(name).strip():
            continue

        # Extract price per night
        rate_info = prop.get("rate_per_night")
        raw_night_price = None
        if isinstance(rate_info, dict):
            raw_night_price = (
                rate_info.get("extracted_lowest")
                if rate_info.get("extracted_lowest") is not None
                else rate_info.get("lowest")
            )
        elif isinstance(rate_info, (int, float, str, Decimal)):
            raw_night_price = rate_info

        if raw_night_price is None:
            raw_night_price = (
                prop.get("price")
                if prop.get("price") is not None
                else prop.get("extracted_price")
            )

        # Extract total rate
        total_info = prop.get("total_rate")
        raw_total_price = None
        if isinstance(total_info, dict):
            raw_total_price = (
                total_info.get("extracted_lowest")
                if total_info.get("extracted_lowest") is not None
                else total_info.get("lowest")
            )
        elif isinstance(total_info, (int, float, str, Decimal)):
            raw_total_price = total_info

        if raw_total_price is None:
            raw_total_price = prop.get("extracted_total_price")

        # Missing price check: if both price fields are completely absent -> reject property
        if raw_night_price is None and raw_total_price is None:
            logger.debug("Rejecting hotel '%s': missing price.", name)
            continue

        # Validate night price if provided
        price_per_night: Decimal | None = None
        night_currency: str = "INR"
        if raw_night_price is not None:
            price_per_night, night_currency = parse_price_and_currency(raw_night_price)
            # missing/invalid/zero/negative price -> reject property
            if price_per_night <= Decimal("0.00"):
                logger.debug(
                    "Rejecting hotel '%s': invalid or non-positive night price (%s).",
                    name,
                    raw_night_price,
                )
                continue

        # Validate total price if provided
        total_price: Decimal | None = None
        total_currency: str = "INR"
        if raw_total_price is not None:
            total_price, total_currency = parse_price_and_currency(raw_total_price)
            # missing/invalid/zero/negative price -> reject property
            if total_price <= Decimal("0.00"):
                logger.debug(
                    "Rejecting hotel '%s': invalid or non-positive total price (%s).",
                    name,
                    raw_total_price,
                )
                continue

        # Infer complementary price if only one was provided
        if price_per_night is None and total_price is not None:
            price_per_night = total_price
            currency = total_currency
        elif total_price is None and price_per_night is not None:
            total_price = price_per_night
            currency = night_currency
        else:
            currency = night_currency or total_currency or "INR"

        # Final verification: both prices must be strictly positive Decimals
        if (
            price_per_night is None
            or total_price is None
            or price_per_night <= Decimal("0.00")
            or total_price <= Decimal("0.00")
        ):
            logger.debug(
                "Rejecting hotel '%s': missing, invalid, or zero price (night=%s, total=%s).",
                name,
                price_per_night,
                total_price,
            )
            continue

        hotel_class = _extract_hotel_class(prop.get("hotel_class"))
        address = prop.get("address") or prop.get("location")
        rating = prop.get("overall_rating") or prop.get("rating")
        review_count = prop.get("reviews")
        link = prop.get("link")

        option = HotelOption(
            name=str(name).strip(),
            hotel_class=hotel_class,
            address=str(address).strip() if address else None,
            price_per_night=price_per_night,
            total_price=total_price,
            currency=currency,
            rating=float(rating) if rating is not None else None,
            review_count=int(review_count) if review_count is not None else None,
            deep_link=str(link).strip() if link else None,
            source=envelope.source,
            is_fallback=envelope.is_fallback,
        )
        results.append(option)

    return results
