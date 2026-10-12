"""Location resolution: map Budlance city names to SerpApi location identifiers.

SerpApi Travel Explore and Google Flights require departure_id / arrival_id.
These are typically IATA airport codes (e.g. 'MAA' for Chennai).

Google Hotels and Google Maps accept city-name queries via the 'q' parameter,
so no special resolution is needed for those engines.

Responsibility:
  city_name (str) --> SerpApi departure_id / arrival_id (str)

Design decisions:
  - Only covers cities that appear as origins/destinations in the existing
    project's known travel corridors and regional catalog.
  - Does NOT attempt geocoding or external lookup (offline-safe).
  - Returns None when no IATA code is known, so callers can decide
    whether to call the live API (hotels/maps q-param still works) or
    skip the live call and rely on fallback.
  - No business logic -- pure lookup only.
"""

import logging

logger = logging.getLogger(__name__)

# -------------------------------------------------------------------------
# IATA airport-code mapping for cities referenced in Budlance
# (origins from user inputs + destinations from regional catalog)
# -------------------------------------------------------------------------
_CITY_TO_IATA: dict[str, str] = {
    # South India
    "chennai":      "MAA",
    "madras":       "MAA",
    "bengaluru":    "BLR",
    "bangalore":    "BLR",
    "hyderabad":    "HYD",
    "kochi":        "COK",
    "cochin":       "COK",
    "coimbatore":   "CJB",
    "trivandrum":   "TRV",
    "thiruvananthapuram": "TRV",
    "mangalore":    "IXE",
    "vizag":        "VTZ",
    "visakhapatnam": "VTZ",
    # West India
    "mumbai":       "BOM",
    "bombay":       "BOM",
    "pune":         "PNQ",
    "goa":          "GOI",
    "ahmedabad":    "AMD",
    "surat":        "STV",
    # North India
    "delhi":        "DEL",
    "new delhi":    "DEL",
    "jaipur":       "JAI",
    "lucknow":      "LKO",
    "varanasi":     "VNS",
    "amritsar":     "ATQ",
    "chandigarh":   "IXC",
    "agra":         "AGR",
    # East India
    "kolkata":      "CCU",
    "calcutta":     "CCU",
    "bhubaneswar":  "BBI",
    "guwahati":     "GAU",
    "patna":        "PAT",
    # Central India
    "bhopal":       "BHO",
    "nagpur":       "NAG",
    "raipur":       "RPR",
    "indore":       "IDR",
    # Hill stations / tourist destinations
    "manali":       None,
    "shimla":       None,
    "ooty":         None,
    "coorg":        None,
    "mysuru":       "MYQ",
    "mysore":       "MYQ",
    "udaipur":      "UDR",
    "jodhpur":      "JDH",
    "kota":         None,
    "rishikesh":    None,
    "mussoorie":    None,
    "kerala":       "COK",
    # International popular from India
    "singapore":    "SIN",
    "dubai":        "DXB",
    "bangkok":      "BKK",
    "london":       "LHR",
    "new york":     "JFK",
}

# -------------------------------------------------------------------------
# No-airport transfer routing: destinations reachable by flight to nearest
# airport + onward road/taxi transfer.  Transfer times are approximate and
# labeled clearly in the preview so users are never misled.
# -------------------------------------------------------------------------
_NO_AIRPORT_TRANSFER: dict[str, dict] = {
    "munnar":    {"nearest_city": "Kochi",      "iata": "COK", "transfer_km": 130, "transfer_hours": 3.5, "mode": "taxi"},
    "ooty":      {"nearest_city": "Coimbatore", "iata": "CJB", "transfer_km": 86,  "transfer_hours": 2.5, "mode": "taxi"},
    "coorg":     {"nearest_city": "Mangalore",  "iata": "IXE", "transfer_km": 160, "transfer_hours": 4.0, "mode": "taxi"},
    "kodaikanal":{"nearest_city": "Madurai",    "iata": "IXM", "transfer_km": 120, "transfer_hours": 3.0, "mode": "taxi"},
    "manali":    {"nearest_city": "Chandigarh", "iata": "IXC", "transfer_km": 310, "transfer_hours": 9.0, "mode": "bus/taxi"},
    "shimla":    {"nearest_city": "Chandigarh", "iata": "IXC", "transfer_km": 120, "transfer_hours": 3.5, "mode": "taxi"},
    "rishikesh": {"nearest_city": "Dehradun",   "iata": "DED", "transfer_km": 43,  "transfer_hours": 1.5, "mode": "taxi"},
    "mussoorie": {"nearest_city": "Dehradun",   "iata": "DED", "transfer_km": 35,  "transfer_hours": 1.0, "mode": "taxi"},
    "wayanad":   {"nearest_city": "Calicut",    "iata": "CCJ", "transfer_km": 75,  "transfer_hours": 2.0, "mode": "taxi"},
    "thekkady":  {"nearest_city": "Madurai",    "iata": "IXM", "transfer_km": 140, "transfer_hours": 3.5, "mode": "taxi"},
    "alleppey":  {"nearest_city": "Kochi",      "iata": "COK", "transfer_km": 85,  "transfer_hours": 1.5, "mode": "taxi"},
    "yercaud":   {"nearest_city": "Salem",      "iata": "SXV", "transfer_km": 30,  "transfer_hours": 1.0, "mode": "taxi"},
    "hampi":     {"nearest_city": "Hubli",      "iata": "HBX", "transfer_km": 160, "transfer_hours": 3.5, "mode": "bus/taxi"},
    "gokarna":   {"nearest_city": "Goa",        "iata": "GOI", "transfer_km": 120, "transfer_hours": 3.0, "mode": "taxi"},
    "varkala":   {"nearest_city": "Trivandrum", "iata": "TRV", "transfer_km": 55,  "transfer_hours": 1.5, "mode": "taxi"},
    "kabini":    {"nearest_city": "Mysore",     "iata": "MYQ", "transfer_km": 80,  "transfer_hours": 2.0, "mode": "taxi"},
}


def resolve_iata(city: str) -> str | None:
    """Resolve a city/region name to a SerpApi-compatible IATA airport code.

    Returns:
        IATA code string (e.g. ''MAA'') when known, or None when no airport
        mapping exists (e.g. hill stations reachable only by train/road).

    The returned value is safe to pass as ``departure_id`` or ``arrival_id``
    to SerpApi google_travel_explore and google_flights engines.
    """
    if not city or not isinstance(city, str):
        return None
    key = city.strip().lower()
    result = _CITY_TO_IATA.get(key)
    if result is None and key not in _CITY_TO_IATA:
        logger.debug("[LOCATION] No IATA mapping for city=%r. Live flight search will be skipped.", city)
    # If the input is already a 3-letter IATA code known in the mapping values:
    if len(key) == 3 and key.upper() in [v for v in _CITY_TO_IATA.values() if v]:
        return key.upper()
    return result


def resolve_nearest_airport_with_transfer(destination: str) -> dict | None:
    """Return nearest airport info for destinations that have no direct airport.

    Used to route flights via the nearest city and add a labeled transfer leg
    (taxi/bus) in the preview so the user is never shown a misleading direct-flight
    option to a no-airport destination.

    Returns:
        dict with keys: nearest_city, iata, transfer_km, transfer_hours, mode
        or None if the destination has its own airport (or is unknown).
    """
    if not destination or not isinstance(destination, str):
        return None
    key = destination.strip().lower()
    info = _NO_AIRPORT_TRANSFER.get(key)
    if info:
        logger.info(
            "[LOCATION] %r has no direct airport. Routing via %s (%s) + %s transfer (~%.1f hrs).",
            destination, info["nearest_city"], info["iata"], info["mode"], info["transfer_hours"],
        )
    return info


def resolve_hotel_query(city: str) -> str:
    """Build a Google Hotels ''q'' parameter value for a destination city.

    Returns a canonical query string like ''Hotels in Goa''.
    """
    clean = city.strip().title() if city else "Unknown"
    return f"Hotels in {clean}"


def resolve_places_query(city: str, interest: str | None = None) -> str:
    """Build a Google Maps 'q' parameter value for attraction discovery.

    Returns a canonical query string like 'places attractions in Goa',
    or interest-specific queries like 'theme parks in Goa' or 'local food in Goa'.
    """
    clean = city.strip().title() if city else "Unknown"
    if interest and str(interest).strip():
        int_clean = str(interest).strip().lower()
        if int_clean in ("theme park", "theme parks", "amusement park", "amusement parks"):
            return f"theme parks in {clean}"
        return f"{int_clean} in {clean}"
    return f"places attractions in {clean}"


def resolve_food_query(city: str, interest: str | None = None) -> str:
    """Build a Google Maps 'q' parameter value for live food and restaurant discovery.

    Uses user food interest or default local cuisine query without hardcoding restaurant names.
    """
    clean = city.strip().title() if city else "Unknown"
    if interest and str(interest).strip():
        int_clean = str(interest).strip().lower()
        if any(w in int_clean for w in ("food", "cuisine", "restaurant", "seafood", "dining", "cafe")):
            return f"{int_clean} in {clean}"
        return f"{int_clean} food in {clean}"
    return f"local food restaurants in {clean}"


def resolve_events_query(city: str, date_or_season: str | None = None) -> str:
    """Build a Google Search 'q' parameter value for live events discovery using events_results."""
    clean = city.strip().title() if city else "Unknown"
    if date_or_season and str(date_or_season).strip():
        return f"events in {clean} {str(date_or_season).strip()}"
    return f"events in {clean}"

