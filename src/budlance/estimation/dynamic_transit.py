"""Dynamic intercity transit generator for arbitrary origin-to-destination pairs.

Resolves distance dynamically using coordinates/Haversine formula or route data,
and computes realistic Indian Railways and intercity bus fares based on distance slabs.
Guarantees that Budlance can plan trips between ANY two cities without hardcoded corridor files.
"""

from decimal import Decimal
import logging
import math
from typing import Any
from uuid import uuid4

from budlance.cache.fallback import FallbackDataProvider
from budlance.schemas.travel import TransitOption
from budlance.serpapi.models import DataSource

logger = logging.getLogger(__name__)

# Approximate coordinates (lat, lon) for major Indian cities and tourist hubs
# Used for fast, offline, zero-token distance estimation when Google Maps Directions is unconfigured.
_CITY_COORDINATES: dict[str, tuple[float, float]] = {
    # South India
    "chennai": (13.0827, 80.2707),
    "madras": (13.0827, 80.2707),
    "bangalore": (12.9716, 77.5946),
    "bengaluru": (12.9716, 77.5946),
    "madurai": (9.9252, 78.1198),
    "coimbatore": (11.0168, 76.9558),
    "trichy": (10.7905, 78.7047),
    "tiruchirappalli": (10.7905, 78.7047),
    "salem": (11.6643, 78.1460),
    "tirunelveli": (8.7139, 77.7567),
    "kanyakumari": (8.0883, 77.5385),
    "pondicherry": (11.9416, 79.8083),
    "puducherry": (11.9416, 79.8083),
    "ooty": (11.4102, 76.6950),
    "udagamandalam": (11.4102, 76.6950),
    "kodaikanal": (10.2381, 77.4892),
    "hosur": (12.7409, 77.8253),
    "kochi": (9.9312, 76.2673),
    "cochin": (9.9312, 76.2673),
    "ernakulam": (9.9816, 76.2999),
    "trivandrum": (8.5241, 76.9366),
    "thiruvananthapuram": (8.5241, 76.9366),
    "munnar": (10.0889, 77.0595),
    "alleppey": (9.4981, 76.3388),
    "alappuzha": (9.4981, 76.3388),
    "wayanad": (11.6854, 76.1320),
    "kozhikode": (11.2588, 75.7804),
    "calicut": (11.2588, 75.7804),
    "mysore": (12.2958, 76.6394),
    "mysuru": (12.2958, 76.6394),
    "coorg": (12.3375, 75.8069),
    "madikeri": (12.4244, 75.7382),
    "mangalore": (12.9141, 74.8560),
    "hampi": (15.3350, 76.4600),
    "hyderabad": (17.3850, 78.4867),
    "secunderabad": (17.4399, 78.4983),
    "visakhapatnam": (17.6868, 83.2185),
    "vizag": (17.6868, 83.2185),
    "vijayawada": (16.5062, 80.6480),
    "tirupati": (13.6288, 79.4192),

    # West India
    "mumbai": (19.0760, 72.8777),
    "bombay": (19.0760, 72.8777),
    "pune": (18.5204, 73.8567),
    "nagpur": (21.1458, 79.0882),
    "nashik": (19.9975, 73.7898),
    "aurangabad": (19.8762, 75.3433),
    "chhatrapati sambhajinagar": (19.8762, 75.3433),
    "shirdi": (19.7667, 74.4764),
    "mahabaleshwar": (17.9237, 73.6586),
    "lonavala": (18.7557, 73.4091),
    "goa": (15.2993, 74.1240),
    "panaji": (15.4909, 73.8278),
    "margao": (15.2832, 73.9862),
    "ahmedabad": (23.0225, 72.5714),
    "surat": (21.1702, 72.8311),
    "vadodara": (22.3072, 73.1812),
    "rajkot": (22.3039, 70.8022),
    "kutch": (23.7337, 69.8597),
    "bhuj": (23.2420, 69.6669),
    "gujarat": (23.0225, 72.5714),
    "kerala": (9.9312, 76.2673),

    # North India
    "delhi": (28.7041, 77.1025),
    "new delhi": (28.6139, 77.2090),
    "noida": (28.5355, 77.3910),
    "gurgaon": (28.4595, 77.0266),
    "gurugram": (28.4595, 77.0266),
    "agra": (27.1767, 78.0081),
    "jaipur": (26.9124, 75.7873),
    "udaipur": (24.5854, 73.7125),
    "jodhpur": (26.2389, 73.0243),
    "jaisalmer": (26.9157, 70.9083),
    "pushkar": (26.4897, 74.5511),
    "lucknow": (26.8467, 80.9462),
    "varanasi": (25.3176, 82.9739),
    "kanpur": (26.4499, 80.3319),
    "prayagraj": (25.4358, 81.8463),
    "allahabad": (25.4358, 81.8463),
    "amritsar": (31.6340, 74.8723),
    "chandigarh": (30.7333, 76.7794),
    "shimla": (31.1048, 77.1734),
    "manali": (32.2432, 77.1892),
    "dharamshala": (32.2190, 76.3234),
    "rishikesh": (30.0869, 78.2676),
    "haridwar": (29.9457, 78.1642),
    "dehradun": (30.3165, 78.0322),
    "nainital": (29.3919, 79.4542),
    "mussoorie": (30.4598, 78.0644),

    # East & North-East India
    "kolkata": (22.5726, 88.3639),
    "calcutta": (22.5726, 88.3639),
    "darjeeling": (27.0410, 88.2663),
    "siliguri": (26.7271, 88.3953),
    "patna": (25.5941, 85.1376),
    "gaya": (24.7914, 85.0002),
    "bhubaneswar": (20.2961, 85.8245),
    "puri": (19.8135, 85.8312),
    "cuttack": (20.4625, 85.8828),
    "ranchi": (23.3441, 85.3096),
    "guwahati": (26.1445, 91.7362),
    "shillong": (25.5788, 91.8933),
    "gangtok": (27.3389, 88.6065),

    # Central India
    "bhopal": (23.2599, 77.4126),
    "indore": (22.7196, 75.8577),
    "ujjain": (23.1765, 75.7885),
    "gwalior": (26.2183, 78.1828),
    "jabalpur": (23.1815, 79.9864),
    "raipur": (21.2514, 81.6296),

    # Island & High Altitude destinations
    "port blair": (11.6234, 92.7265),
    "andaman": (11.6234, 92.7265),
    "leh": (34.1526, 77.5771),
    "ladakh": (34.1526, 77.5771),
}

# Explicit railhead list of Indian cities and hubs with direct broad-gauge rail connectivity
RAILHEAD_CITIES: set[str] = {
    # South India
    "chennai", "madras", "bangalore", "bengaluru", "madurai", "coimbatore",
    "trichy", "tiruchirappalli", "salem", "tirunelveli", "kanyakumari",
    "pondicherry", "puducherry", "hosur", "kochi", "cochin", "ernakulam",
    "trivandrum", "thiruvananthapuram", "alleppey", "alappuzha", "kozhikode",
    "calicut", "mysore", "mysuru", "mangalore", "hampi", "hyderabad",
    "secunderabad", "visakhapatnam", "vizag", "vijayawada", "tirupati",
    # West India
    "mumbai", "bombay", "pune", "nagpur", "nashik", "aurangabad",
    "chhatrapati sambhajinagar", "shirdi", "lonavala", "goa", "panaji",
    "margao", "ahmedabad", "surat", "vadodara", "rajkot", "kutch", "bhuj",
    "gujarat", "kerala",
    # North India
    "delhi", "new delhi", "noida", "gurgaon", "gurugram", "agra", "jaipur",
    "udaipur", "jodhpur", "jaisalmer", "pushkar", "lucknow", "varanasi",
    "kanpur", "prayagraj", "allahabad", "amritsar", "chandigarh", "haridwar",
    "dehradun",
    # East & Central India
    "kolkata", "calcutta", "siliguri", "patna", "gaya", "bhubaneswar", "puri",
    "cuttack", "ranchi", "guwahati", "bhopal", "indore", "ujjain", "gwalior",
    "jabalpur", "raipur",
}

# Destinations without a direct broad-gauge railhead that connect via nearest railhead + road
NON_RAILHEAD_GATEWAYS: dict[str, str] = {
    "munnar": "Ernakulam/Aluva",
    "ooty": "Mettupalayam/Coimbatore",
    "udagamandalam": "Mettupalayam/Coimbatore",
    "kodaikanal": "Kodai Road",
    "wayanad": "Kozhikode",
    "coorg": "Mysore",
    "madikeri": "Mysore",
    "mahabaleshwar": "Pune",
    "manali": "Chandigarh",
    "dharamshala": "Pathankot",
    "nainital": "Kathgodam",
    "mussoorie": "Dehradun",
    "shillong": "Guwahati",
    "gangtok": "Siliguri",
    "shimla": "Kalka",
    "darjeeling": "Siliguri",
}


def is_railhead(city: str) -> bool:
    """Check if a city is on the explicit railhead list."""
    clean = city.strip().lower()
    if clean in RAILHEAD_CITIES:
        return True
    for rc in RAILHEAD_CITIES:
        if rc == clean:
            return True
        if len(rc) >= 4 and rc in clean:
            return True
        if len(clean) >= 4 and clean in rc:
            return True
        if rc in ("goa", "pune", "agra", "puri", "gaya") and (f" {rc}" in f" {clean} " or f"{rc} " in f" {clean} "):
            return True
    return False


# Destinations with no rail connectivity
NO_RAIL_DESTINATIONS: set[str] = {
    "leh", "ladakh", "kargil",
    "port blair", "andaman", "nicobar", "havelock", "neil island",
    "lakshadweep", "kavaratti", "agatti",
}

# Island destinations separated by sea from mainland India (no overland/surface transit)
ISLAND_DESTINATIONS: set[str] = {
    "port blair", "andaman", "nicobar", "havelock", "neil island",
    "lakshadweep", "kavaratti", "agatti",
}


def compute_haversine_distance_km(coord1: tuple[float, float], coord2: tuple[float, float]) -> float:
    """Calculate the great-circle distance between two points in km."""
    lat1, lon1 = math.radians(coord1[0]), math.radians(coord1[1])
    lat2, lon2 = math.radians(coord2[0]), math.radians(coord2[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    # Earth radius in km (~6371) multiplied by road/rail curvature factor (~1.25)
    return round(6371.0 * c * 1.25, 1)


class DynamicTransitGenerator:
    """Generates realistic intercity rail and bus travel options for any origin-destination pair."""

    def __init__(self, fallback_provider: FallbackDataProvider | None = None) -> None:
        self.fallback = fallback_provider or FallbackDataProvider()

    def estimate_distance_km(self, origin: str, destination: str) -> float | None:
        """Estimate road/rail travel distance between two cities.

        Returns None if either origin or destination coordinates cannot be resolved,
        or if surface travel is physically impossible (e.g. mainland to island).
        """
        o_clean = origin.strip().lower()
        d_clean = destination.strip().lower()

        if o_clean == d_clean:
            return 15.0

        # Island routes have no overland surface transit connectivity with mainland India
        is_o_island = any(isl in o_clean for isl in ISLAND_DESTINATIONS)
        is_d_island = any(isl in d_clean for isl in ISLAND_DESTINATIONS)
        if (is_o_island and not is_d_island) or (is_d_island and not is_o_island):
            logger.info("Surface transit impossible between mainland and island: %r to %r", origin, destination)
            return None

        coord1 = _CITY_COORDINATES.get(o_clean)
        coord2 = _CITY_COORDINATES.get(d_clean)

        if coord1 and coord2:
            return compute_haversine_distance_km(coord1, coord2)

        # Partial matching (e.g., "North Goa" -> "goa", "Fort Kochi" -> "kochi")
        for k, v in _CITY_COORDINATES.items():
            if not coord1 and (k in o_clean or o_clean in k):
                coord1 = v
            if not coord2 and (k in d_clean or d_clean in k):
                coord2 = v

        if coord1 and coord2:
            return compute_haversine_distance_km(coord1, coord2)

        # Unknown route: do NOT guess 450 km silently
        return None

    def generate_options(
        self,
        origin: str,
        destination: str,
        people: int = 1,
        transport_class: str | None = None,
        preferred_mode: str | None = None,
    ) -> list[TransitOption]:
        """Generate realistic round-trip Train and Bus TransitOptions for any origin and destination."""
        people_count = max(1, people)
        distance_km = self.estimate_distance_km(origin, destination)
        if distance_km is None:
            logger.info(
                "Dynamic transit cannot resolve route between %r and %r (unknown route).",
                origin, destination,
            )
            return []
        orig_title = origin.strip().title()
        dest_title = destination.strip().title()
        o_clean = origin.strip().lower()
        d_clean = destination.strip().lower()

        # Check if train connectivity exists
        is_no_rail = (
            any(nr in o_clean for nr in NO_RAIL_DESTINATIONS)
            or any(nr in d_clean for nr in NO_RAIL_DESTINATIONS)
        )

        has_orig_rail = is_railhead(o_clean)
        has_dest_rail = is_railhead(d_clean)
        both_on_railhead = has_orig_rail and has_dest_rail

        # If not both on explicit railhead list, determine if connection is "nearest railhead + road"
        has_gateway = (
            any(gw in o_clean or o_clean in gw for gw in NON_RAILHEAD_GATEWAYS)
            or any(gw in d_clean or d_clean in gw for gw in NON_RAILHEAD_GATEWAYS)
        )

        # Estimated one-way duration: avg 55 km/h for train + 1 hr buffer
        train_duration_hours = round(max(2.0, (distance_km / 55.0) + 0.5), 1)
        bus_duration_hours = round(max(2.5, (distance_km / 48.0) + 1.0), 1)

        # Standard Indian Railways fare distance slabs (per person, one way)
        fare_sl = round(max(Decimal("220.00"), Decimal(str(distance_km)) * Decimal("0.45") + Decimal("150.00")), 2)
        fare_3a = round(max(Decimal("650.00"), Decimal(str(distance_km)) * Decimal("1.15") + Decimal("300.00")), 2)
        fare_2a = round(max(Decimal("950.00"), Decimal(str(distance_km)) * Decimal("1.65") + Decimal("450.00")), 2)
        fare_1a = round(max(Decimal("1600.00"), Decimal(str(distance_km)) * Decimal("2.80") + Decimal("700.00")), 2)
        fare_bus = round(max(Decimal("450.00"), Decimal(str(distance_km)) * Decimal("1.40") + Decimal("200.00")), 2)

        # Round-trip total for the entire party (2 one-way trips * people)
        multiplier = Decimal(str(people_count * 2))

        train_options: list[TransitOption] = []
        bus_options: list[TransitOption] = []

        # Only emit train option for cities on explicit railhead list;
        # otherwise label "nearest railhead + road" or emit bus only.
        if not is_no_rail and (both_on_railhead or has_gateway):
            train_label = (
                "Estimated train fare (distance-based)"
                if both_on_railhead
                else "Estimated train fare (nearest railhead + road)"
            )

            # 1. 3AC Train
            train_options.append(
                TransitOption(
                    id=uuid4(),
                    transit_type="train",
                    origin=orig_title,
                    destination=dest_title,
                    name_or_operator=train_label,
                    distance_km=distance_km,
                    duration_hours=train_duration_hours,
                    price=round(fare_3a * multiplier, 2),
                    currency="INR",
                    class_or_type="3AC",
                    source=DataSource.ESTIMATED,
                    is_fallback=True,
                )
            )

            # 2. Sleeper Train
            train_options.append(
                TransitOption(
                    id=uuid4(),
                    transit_type="train",
                    origin=orig_title,
                    destination=dest_title,
                    name_or_operator=train_label,
                    distance_km=distance_km,
                    duration_hours=train_duration_hours,
                    price=round(fare_sl * multiplier, 2),
                    currency="INR",
                    class_or_type="Sleeper",
                    source=DataSource.ESTIMATED,
                    is_fallback=True,
                )
            )

            # 3. 2AC Train
            train_options.append(
                TransitOption(
                    id=uuid4(),
                    transit_type="train",
                    origin=orig_title,
                    destination=dest_title,
                    name_or_operator=train_label,
                    distance_km=distance_km,
                    duration_hours=train_duration_hours,
                    price=round(fare_2a * multiplier, 2),
                    currency="INR",
                    class_or_type="2AC",
                    source=DataSource.ESTIMATED,
                    is_fallback=True,
                )
            )

            # 4. 1AC Train
            train_options.append(
                TransitOption(
                    id=uuid4(),
                    transit_type="train",
                    origin=orig_title,
                    destination=dest_title,
                    name_or_operator=train_label,
                    distance_km=distance_km,
                    duration_hours=train_duration_hours,
                    price=round(fare_1a * multiplier, 2),
                    currency="INR",
                    class_or_type="1AC",
                    source=DataSource.ESTIMATED,
                    is_fallback=True,
                )
            )

        # Intercity Bus (only for feasible overland bus distances <= 1500 km or non-mountain routes)
        if distance_km <= 1500.0 or not is_no_rail:
            bus_options.append(
                TransitOption(
                    id=uuid4(),
                    transit_type="bus",
                    origin=orig_title,
                    destination=dest_title,
                    name_or_operator="Estimated bus fare (distance-based)",
                    distance_km=distance_km,
                    duration_hours=bus_duration_hours,
                    price=round(fare_bus * multiplier, 2),
                    currency="INR",
                    class_or_type="AC Sleeper",
                    source=DataSource.ESTIMATED,
                    is_fallback=True,
                )
            )

        # Under ~350 km (allowing up to 380 km for Haversine curvature factor), show bus alongside train
        if distance_km <= 380.0 and bus_options and train_options:
            options = [train_options[0], bus_options[0]] + train_options[1:]
        else:
            options = train_options + bus_options

        # Filter or prioritize based on requested class or mode
        cls_req = (transport_class or "").lower().replace("-", "").replace(" ", "")
        if cls_req:
            matching = [opt for opt in options if cls_req in (opt.class_or_type or "").lower().replace("-", "").replace(" ", "")]
            if matching:
                remaining = [opt for opt in options if opt not in matching]
                if distance_km <= 380.0 and bus_options and bus_options[0] not in matching:
                    rem_non_bus = [opt for opt in remaining if opt.transit_type != "bus"]
                    return matching + [bus_options[0]] + rem_non_bus
                return matching + remaining

        if preferred_mode == "bus":
            bus_opts = [opt for opt in options if opt.transit_type == "bus"]
            train_opts = [opt for opt in options if opt.transit_type == "train"]
            return bus_opts + train_opts

        return options
