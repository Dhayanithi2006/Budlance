"""Flight normalizer converting Google Flights raw responses to FlightOption models."""

import logging
from typing import Any
from budlance.normalization.utils import parse_price_and_currency
from budlance.schemas.travel import FlightOption
from budlance.serpapi.models import TravelDataEnvelope

logger = logging.getLogger(__name__)


def normalize_flights(envelope: TravelDataEnvelope) -> list[FlightOption]:
    """Parse Google Flights data envelope into a list of normalized FlightOption models."""
    results: list[FlightOption] = []
    data = envelope.data

    if not isinstance(data, dict):
        logger.warning("Flight data is not a valid dictionary envelope.")
        return results

    # Best flights and other flights are standard SerpApi Google Flights blocks
    flight_groups = []
    if isinstance(data.get("best_flights"), list):
        flight_groups.extend(data["best_flights"])
    if isinstance(data.get("other_flights"), list):
        flight_groups.extend(data["other_flights"])

    for item in flight_groups:
        if not isinstance(item, dict):
            continue

        raw_price = item.get("price")
        price, currency = parse_price_and_currency(raw_price)

        # Flights array inside group represents individual legs/segments
        legs = item.get("flights") or []
        first_leg = legs[0] if isinstance(legs, list) and legs else {}
        last_leg = legs[-1] if isinstance(legs, list) and legs else {}

        airline = first_leg.get("airline") or item.get("airline")
        flight_number = first_leg.get("flight_number")

        dep_info = first_leg.get("departure_airport") or {}
        arr_info = last_leg.get("arrival_airport") or {}

        dep_airport = dep_info.get("id") or dep_info.get("name")
        arr_airport = arr_info.get("id") or arr_info.get("name")
        dep_time = dep_info.get("time")
        arr_time = arr_info.get("time")

        duration_mins = item.get("total_duration") or first_leg.get("duration")
        stops = max(0, len(legs) - 1) if isinstance(legs, list) else 0
        deep_link = item.get("booking_token") or item.get("link")

        option = FlightOption(
            airline=airline,
            flight_number=flight_number,
            departure_airport=dep_airport,
            arrival_airport=arr_airport,
            departure_time=dep_time,
            arrival_time=arr_time,
            price=price,
            currency=currency,
            duration_minutes=int(duration_mins) if duration_mins is not None else None,
            stops=stops,
            deep_link=str(deep_link) if deep_link else None,
            source=envelope.source,
            is_fallback=envelope.is_fallback,
        )
        results.append(option)

    return results
