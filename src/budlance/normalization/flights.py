"""Flight normalizer converting Google Flights raw responses to FlightOption models."""

import logging
import urllib.parse
from decimal import Decimal
from typing import Any
from budlance.normalization.utils import parse_price_and_currency
from budlance.schemas.travel import FlightOption
from budlance.serpapi.models import TravelDataEnvelope

logger = logging.getLogger(__name__)

INVALID_ROUTE_TERMS = {
    "from", "to", "none", "null", "undefined", "empty",
    "flight", "flights", "train", "trains", "bus", "buses",
    "destination", "origin", "somewhere", "anywhere", "place", "places",
}


def is_valid_route_endpoint(term: str | None) -> bool:
    """Validate that a route origin or destination is not None, empty, or a stopword placeholder."""
    if not term or not isinstance(term, str):
        return False
    cleaned = term.strip().lower()
    if not cleaned or cleaned in INVALID_ROUTE_TERMS:
        return False
    return True


def validate_flight_route(origin: str | None, destination: str | None) -> bool:
    """Validate route endpoints are non-empty, distinct, and not invalid stopwords."""
    if not is_valid_route_endpoint(origin) or not is_valid_route_endpoint(destination):
        return False
    if str(origin).strip().lower() == str(destination).strip().lower():
        return False
    return True


def build_safe_flight_search_url(
    origin: str | None,
    destination: str | None,
    outbound_date: str | None = None,
    return_date: str | None = None,
    people: int = 1,
    travel_class: str | None = None,
    booking_token: str | None = None,
) -> str:
    """Build a valid, URL-encoded round-trip Google Flights booking/search link.

    Guarantees:
    - Never generates malformed queries like 'Flights to From from Chennai'.
    - If route is invalid (missing or stopword like 'From' or 'None'), returns safe generic portal.
    - If a booking_token is provided:
        - If already an http(s) URL, returns it directly.
        - Otherwise, attaches booking_token parameter.
    - Otherwise uses IATA codes and real dates in format:
        q="Flights to {dest_iata} from {orig_iata} on {outbound_date} through {return_date}"
    - Never leaves unfilled placeholder tokens (e.g. {X}, {DATE}, None, null).
    """
    if not validate_flight_route(origin, destination):
        return "https://www.google.com/travel/flights"

    # 1. Use booking_token if already present
    if booking_token and str(booking_token).strip():
        tok = str(booking_token).strip()
        if tok.startswith(("http://", "https://")):
            return tok
        return f"https://www.google.com/travel/flights?booking_token={urllib.parse.quote_plus(tok)}"

    from budlance.serpapi.location import resolve_iata

    clean_origin = str(origin).strip()
    clean_destination = str(destination).strip()

    orig_iata = resolve_iata(clean_origin)
    if not orig_iata:
        orig_iata = clean_origin.upper() if (len(clean_origin) == 3 and clean_origin.isalpha()) else clean_origin.title()

    dest_iata = resolve_iata(clean_destination)
    if not dest_iata:
        dest_iata = clean_destination.upper() if (len(clean_destination) == 3 and clean_destination.isalpha()) else clean_destination.title()

    # Form: q="Flights to X from Y on DATE through DATE"
    if outbound_date and return_date:
        query_str = f"Flights to {dest_iata} from {orig_iata} on {outbound_date} through {return_date}"
    elif outbound_date:
        query_str = f"Flights to {dest_iata} from {orig_iata} on {outbound_date}"
    else:
        query_str = f"Flights to {dest_iata} from {orig_iata}"

    encoded_query = urllib.parse.quote_plus(query_str)
    return f"https://www.google.com/travel/flights?q={encoded_query}"


def extract_best_booking_option(booking_data: dict[str, Any]) -> dict[str, Any] | None:
    """Extract primary booking option from SerpApi Google Flights booking options payload.

    Distinguishes GET deep links from POST data booking requests.
    """
    if not isinstance(booking_data, dict):
        return None

    options = booking_data.get("booking_options")
    if not isinstance(options, list) or not options:
        return None

    first_opt = options[0]
    if not isinstance(first_opt, dict):
        return None

    seller = first_opt.get("book_with") or first_opt.get("marketed_by") or first_opt.get("seller")
    raw_price = first_opt.get("price")
    price, currency = parse_price_and_currency(raw_price)

    booking_req = first_opt.get("booking_request")
    direct_url: str | None = None
    has_post_data = False

    if isinstance(booking_req, dict):
        url = booking_req.get("url")
        post_data = booking_req.get("post_data")
        if post_data:
            has_post_data = True
            direct_url = None  # Do NOT convert POST data into a fake GET URL
        elif url and str(url).startswith(("http://", "https://")):
            direct_url = str(url)
    elif isinstance(booking_req, str) and booking_req.startswith(("http://", "https://")):
        direct_url = booking_req

    return {
        "seller": str(seller) if seller else None,
        "price": price,
        "currency": currency,
        "direct_url": direct_url,
        "booking_request": booking_req if isinstance(booking_req, dict) else None,
        "has_post_data": has_post_data,
    }


def normalize_flight_booking_options(envelope: TravelDataEnvelope) -> list[dict[str, Any]]:
    """Parse Google Flights booking options envelope into structured booking options."""
    if not isinstance(envelope.data, dict):
        return []
    raw_opts = envelope.data.get("booking_options") or []
    results: list[dict[str, Any]] = []
    for opt in raw_opts:
        if not isinstance(opt, dict):
            continue
        extracted = extract_best_booking_option({"booking_options": [opt]})
        if extracted:
            results.append(extracted)
    return results


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
    if isinstance(data.get("flights"), list):
        flight_groups.extend(data["flights"])


    for item in flight_groups:
        if not isinstance(item, dict):
            continue

        raw_price = item.get("price")
        if raw_price is None:
            logger.debug("Rejecting flight item: missing price field.")
            continue

        price, currency = parse_price_and_currency(raw_price)
        if price <= Decimal("0.00"):
            logger.debug("Rejecting flight item: invalid, zero, or non-positive price (%s).", raw_price)
            continue

        # Flights array inside group represents individual legs/segments
        legs = item.get("flights") or []
        first_leg = legs[0] if isinstance(legs, list) and legs else {}
        last_leg = legs[-1] if isinstance(legs, list) and legs else {}

        airline = first_leg.get("airline") or item.get("airline")
        flight_number = first_leg.get("flight_number")

        dep_info = first_leg.get("departure_airport") or {}
        arr_info = last_leg.get("arrival_airport") or {}

        first_dep_airport = dep_info.get("id") or dep_info.get("name")
        last_arr_airport = arr_info.get("id") or arr_info.get("name")

        # Determine if legs contain a round trip (returning to the origin airport)
        is_round_trip_legs = (
            len(legs) > 1
            and first_dep_airport
            and last_arr_airport
            and str(first_dep_airport).strip().lower() == str(last_arr_airport).strip().lower()
        )

        if is_round_trip_legs:
            # Outbound leg arrives at destination
            outbound_arr_info = first_leg.get("arrival_airport") or {}
            dep_airport = first_dep_airport
            arr_airport = outbound_arr_info.get("id") or outbound_arr_info.get("name")
            dep_time = dep_info.get("time")
            arr_time = outbound_arr_info.get("time")

            # Return leg information
            ret_flight_no = last_leg.get("flight_number")
            ret_dep_time = last_leg.get("departure_airport", {}).get("time")
            ret_arr_time = arr_info.get("time")
            stops = max(0, len(legs) - 2)
        else:
            dep_airport = first_dep_airport
            arr_airport = last_arr_airport
            dep_time = dep_info.get("time")
            arr_time = arr_info.get("time")
            ret_flight_no = None
            ret_dep_time = None
            ret_arr_time = None
            stops = max(0, len(legs) - 1) if isinstance(legs, list) else 0

        duration_mins = item.get("total_duration") or first_leg.get("duration")

        # Booking token and booking option handling
        raw_token = item.get("booking_token")
        raw_link = item.get("link")
        booking_token = str(raw_token) if raw_token else None

        seller = item.get("seller") or item.get("airline") or first_leg.get("airline")
        booking_request = None
        is_exact_booking = False
        deep_link = None

        # Check if booking_options is embedded directly in item
        if "booking_options" in item:
            opt = extract_best_booking_option(item)
            if opt:
                seller = opt["seller"] or seller
                booking_request = opt["booking_request"]
                if opt["direct_url"]:
                    deep_link = opt["direct_url"]
                    is_exact_booking = True
                elif opt.get("has_post_data") and opt.get("booking_request"):
                    import hashlib
                    from budlance.config import get_settings
                    from budlance.db.repositories.cache_repo import CacheRepository
                    token_src = raw_token or raw_link or flight_number or airline
                    b_id = hashlib.sha256(str(token_src).encode("utf-8")).hexdigest()[:12]
                    CacheRepository().store_booking_request(b_id, opt["booking_request"])
                    base_url = get_settings().effective_public_base_url
                    deep_link = f"{base_url}/book/{b_id}"
                    is_exact_booking = True


        # Fallback to direct link or token (if token is a valid URL as used in mock fixtures)
        if not deep_link:
            if raw_link and str(raw_link).startswith(("http://", "https://")):
                deep_link = str(raw_link)
            elif raw_token and str(raw_token).startswith(("http://", "https://")):
                deep_link = str(raw_token)

        outbound_date = item.get("outbound_date")
        return_date = item.get("return_date")

        option = FlightOption(
            airline=airline,
            flight_number=flight_number,
            departure_airport=dep_airport,
            arrival_airport=arr_airport,
            departure_time=dep_time,
            arrival_time=arr_time,
            price=price,
            currency=currency,
            price_scope="quote",
            duration_minutes=int(duration_mins) if duration_mins is not None else None,
            stops=stops,
            deep_link=deep_link,
            seller=str(seller) if seller else None,
            booking_token=booking_token,
            booking_request=booking_request,
            outbound_date=str(outbound_date) if outbound_date else None,
            return_date=str(return_date) if return_date else None,
            is_exact_booking=is_exact_booking,
            return_flight_number=ret_flight_no,
            return_departure_time=ret_dep_time,
            return_arrival_time=ret_arr_time,
            source=envelope.source,
            is_fallback=envelope.is_fallback,
            retrieval_timestamp=envelope.provenance.retrieval_timestamp if envelope.provenance else envelope.created_at,
            provenance=envelope.provenance,
        )
        results.append(option)

    return results
