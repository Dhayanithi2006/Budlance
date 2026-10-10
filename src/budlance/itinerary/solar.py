"""Astronomical solar calculation engine for date- and location-aware sunset scheduling.

Computes solar sunset times using standard NOAA Solar Calculator algorithms
without external network calls, and provides truthful approximate time windows
for itinerary scheduling.
"""

from datetime import date, datetime, time, timedelta
import math
from typing import Any
from budlance.serpapi.models import DataSource

# Known destination centroid coordinates (latitude, longitude in decimal degrees)
DESTINATION_CENTROIDS: dict[str, tuple[float, float]] = {
    "goa": (15.2993, 74.1240),
    "candolim": (15.5173, 73.7627),
    "calangute": (15.5439, 73.7553),
    "baga": (15.5553, 73.7516),
    "panaji": (15.4909, 73.8278),
    "north goa": (15.5439, 73.7553),
    "south goa": (15.2000, 73.9500),
    "mumbai": (19.0760, 72.8777),
    "chennai": (13.0827, 80.2707),
    "bangalore": (12.9716, 77.5946),
    "bengaluru": (12.9716, 77.5946),
    "delhi": (28.6139, 77.2090),
    "new delhi": (28.6139, 77.2090),
    "jaipur": (26.9124, 75.7873),
    "kochi": (9.9312, 76.2673),
    "kerala": (10.8505, 76.2711),
    "pondicherry": (11.9416, 79.8083),
    "puducherry": (11.9416, 79.8083),
    "agra": (27.1767, 78.0081),
    "madurai": (9.9252, 78.1198),
    "gujarat": (23.0225, 72.5714),
    "ahmedabad": (23.0225, 72.5714),
}


def calculate_solar_sunset(
    date_val: date | str,
    latitude: float,
    longitude: float,
) -> time | None:
    """Calculate solar sunset time in Indian Standard Time (IST, UTC+5:30).

    Uses standard NOAA solar position equations:
    - Accounts for day of year, equation of time, and solar declination.
    - Uses standard 90.8333° atmospheric refraction solar zenith.

    Returns:
        datetime.time in IST or None if polar day/night or invalid parameters.
    """
    if isinstance(date_val, str):
        try:
            d_obj = date.fromisoformat(date_val.strip())
        except Exception:
            return None
    elif isinstance(date_val, date):
        d_obj = date_val
    else:
        return None

    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return None

    day_of_year = d_obj.timetuple().tm_yday
    # Fractional year in radians
    gamma = 2.0 * math.pi / 365.25 * (day_of_year - 1)

    # Equation of time in minutes
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )

    # Solar declination angle in radians
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )

    lat_rad = math.radians(latitude)
    # Standard solar zenith for sunset including atmospheric refraction (90° 50')
    zenith_rad = math.radians(90.8333)

    denom = math.cos(lat_rad) * math.cos(decl)
    if abs(denom) < 1e-9:
        return None

    cos_hour_angle = (math.cos(zenith_rad) - math.sin(lat_rad) * math.sin(decl)) / denom

    # Check for polar day / night
    if cos_hour_angle > 1.0 or cos_hour_angle < -1.0:
        return None

    hour_angle_deg = math.degrees(math.acos(cos_hour_angle))

    # Solar noon in UTC minutes
    solar_noon_utc_min = 720.0 - (4.0 * longitude) - eqtime

    # Sunset in UTC minutes
    sunset_utc_min = solar_noon_utc_min + (4.0 * hour_angle_deg)

    # Convert to IST (+5:30 = +330 minutes)
    sunset_ist_min = sunset_utc_min + 330.0

    # Wrap within 24 hours
    sunset_ist_min = sunset_ist_min % 1440.0

    hour = int(sunset_ist_min // 60)
    minute = int(round(sunset_ist_min % 60))
    if minute >= 60:
        minute = 0
        hour = (hour + 1) % 24

    return time(hour=hour, minute=minute)


def resolve_coordinates(
    latitude: float | None = None,
    longitude: float | None = None,
    destination: str | None = None,
) -> tuple[float | None, float | None]:
    """Resolve latitude and longitude from explicit values or known destination centroid."""
    if latitude is not None and longitude is not None:
        try:
            return float(latitude), float(longitude)
        except (ValueError, TypeError):
            pass

    if destination:
        dest_clean = destination.strip().lower()
        for k, coords in DESTINATION_CENTROIDS.items():
            if k in dest_clean or dest_clean in k:
                return coords

    return None, None


def get_sunset_window(
    date_val: date | str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    destination: str | None = None,
) -> dict[str, Any]:
    """Construct grounded sunset schedule window and truthful disclosure notes.

    Returns dict containing:
        - start_time: str (e.g. "05:30 PM")
        - end_time: str (e.g. "06:45 PM")
        - approximate_time_window: str (e.g. "05:30 PM – 06:45 PM")
        - notes: str (truthful explanation of solar estimate and approximate nature)
        - is_approximate: bool
        - calculated_sunset_time: str | None
        - source: DataSource
    """
    eff_lat, eff_lon = resolve_coordinates(latitude, longitude, destination)
    calculated_time: time | None = None

    if date_val and eff_lat is not None and eff_lon is not None:
        calculated_time = calculate_solar_sunset(date_val, eff_lat, eff_lon)

    if calculated_time is not None:
        # Golden hour starts ~35-40 minutes before sunset
        dt_sunset = datetime.combine(date.today(), calculated_time)
        dt_start = dt_sunset - timedelta(minutes=40)
        dt_end = dt_sunset + timedelta(minutes=35)

        start_str = dt_start.strftime("%I:%M %p")
        end_str = dt_end.strftime("%I:%M %p")
        sunset_str = calculated_time.strftime("%I:%M %p")

        d_label = f" for {date_val}" if date_val else ""
        dest_label = f" in {destination}" if destination else ""
        notes = (
            f"Estimated sunset ~{sunset_str} IST based on solar position{d_label}{dest_label}. "
            "Arrive 30–40 minutes before sunset for golden hour. Timing is approximate; verify locally."
        )

        return {
            "start_time": start_str,
            "end_time": end_str,
            "approximate_time_window": f"{start_str} – {end_str}",
            "notes": notes,
            "is_approximate": True,
            "calculated_sunset_time": sunset_str,
            "source": DataSource.ESTIMATED,
        }

    # Fallback when coordinates or exact calendar date are unavailable
    fallback_start = "05:15 PM"
    fallback_end = "06:45 PM"
    notes = (
        "Sunset timing (~05:30 PM – 06:15 PM) is approximate; exact solar coordinates unavailable. "
        "Verify local sunset time upon arrival."
    )

    return {
        "start_time": fallback_start,
        "end_time": fallback_end,
        "approximate_time_window": f"{fallback_start} – {fallback_end}",
        "notes": notes,
        "is_approximate": True,
        "calculated_sunset_time": None,
        "source": DataSource.ESTIMATED,
    }
