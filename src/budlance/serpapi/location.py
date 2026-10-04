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
    return result


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
