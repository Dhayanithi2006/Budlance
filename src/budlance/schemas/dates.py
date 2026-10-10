"""Centralized trip duration, date resolution, and hotel stay night semantics."""

from datetime import date, datetime, timedelta
from pydantic import BaseModel, ConfigDict, Field


def parse_date(value: str | date) -> date:
    """Parse string in YYYY-MM-DD or return date object."""
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    raise ValueError(f"Invalid date value: {value!r}. Expected YYYY-MM-DD string or date object.")


def calculate_stay_nights(check_in: str | date, check_out: str | date) -> int:
    """Calculate the exact number of nights between check_in and check_out.

    Mathematical invariants:
    - check_out must be strictly after check_in.
    - Same-day checkout is invalid (raises ValueError).
    - check_out before check_in is invalid (raises ValueError).
    - Returns (check_out - check_in).days as an integer >= 1.
    """
    d_in = parse_date(check_in)
    d_out = parse_date(check_out)

    diff = (d_out - d_in).days
    if diff < 0:
        raise ValueError(
            f"Invalid checkout date: check_out ({d_out}) cannot be before check_in ({d_in})."
        )
    if diff == 0:
        raise ValueError(
            f"Invalid same-day checkout: check_in ({d_in}) and check_out ({d_out}) are the same date. "
            "Hotel stays require at least 1 night."
        )
    return diff


class TripDateContext(BaseModel):
    """Authoritative single source of truth for trip duration and date semantics."""
    model_config = ConfigDict(from_attributes=True)

    days: int = Field(ge=1, description="Total trip duration in calendar days.")
    start_date: date = Field(description="Trip start / Day 1 departure date.")
    end_date: date = Field(description="Trip end / Day N return date.")

    # Flight dates (ISO string YYYY-MM-DD)
    flight_outbound_date: str = Field(description="Departure flight date (Day 1).")
    flight_return_date: str = Field(description="Return flight date (Day N).")

    # Hotel dates (ISO string YYYY-MM-DD)
    hotel_check_in_date: str = Field(description="Hotel check-in date.")
    hotel_check_out_date: str = Field(description="Hotel check-out date.")

    # Stay nights
    stay_nights: int = Field(ge=0, description="Exact number of hotel nights.")
    requires_lodging: bool = Field(description="Whether trip requires overnight lodging (days > 1).")
    is_proposed: bool = Field(default=False, description="Whether dates are proposed rather than explicitly confirmed.")
    date_confirmed: bool = Field(default=True, description="Whether travel dates have been explicitly confirmed.")


def build_trip_date_context(
    days: int,
    start_date: date | str | None = None,
    outbound_date: str | None = None,
    return_date: str | None = None,
    default_lead_days: int = 30,
    is_confirmed: bool | None = None,
) -> TripDateContext:
    """Build a mathematically consistent TripDateContext for an N-day trip.

    Consistency rules:
    1. Days: Must be at least 1 calendar day.
    2. Outbound / Return dates:
       - If outbound_date is provided, start_date is parsed from it.
       - If not provided, start_date defaults to (today + default_lead_days).
       - If return_date is provided:
           end_date is parsed from it.
           days = max(1, (end_date - start_date).days + 1)
       - If return_date is not provided:
           end_date = start_date + timedelta(days=max(0, days - 1))
    3. Hotel dates:
       - For multi-day trips (days > 1):
           hotel_check_in = start_date
           hotel_check_out = end_date (Day N, when travelers check out and fly home)
           stay_nights = (hotel_check_out - hotel_check_in).days = days - 1
           requires_lodging = True
       - For 1-day trips (days == 1):
           requires_lodging = False
           stay_nights = 0 (or 1 if a room is explicitly queried)
           hotel_check_in = start_date
           hotel_check_out = start_date + timedelta(days=1)
    """
    days_val = max(1, days)
    today = date.today()
    has_explicit_start = False

    if outbound_date and isinstance(outbound_date, (str, date)):
        try:
            d_start = parse_date(outbound_date)
            has_explicit_start = True
        except (ValueError, TypeError):
            d_start = today + timedelta(days=default_lead_days)
    elif start_date and isinstance(start_date, (str, date)):
        try:
            d_start = parse_date(start_date)
            has_explicit_start = True
        except (ValueError, TypeError):
            d_start = today + timedelta(days=default_lead_days)
    else:
        d_start = today + timedelta(days=default_lead_days)

    if return_date and isinstance(return_date, (str, date)):
        try:
            d_end = parse_date(return_date)
            if d_end < d_start:
                raise ValueError(
                    f"Invalid return date: return_date ({d_end}) cannot be before outbound_date ({d_start})."
                )
            # Update days_val to match explicit date interval
            days_val = max(1, (d_end - d_start).days + 1)
        except (ValueError, TypeError) as exc:
            if "cannot be before" in str(exc):
                raise
            d_end = d_start + timedelta(days=max(0, days_val - 1))
    else:
        d_end = d_start + timedelta(days=max(0, days_val - 1))

    flight_out = d_start.strftime("%Y-%m-%d")
    flight_ret = d_end.strftime("%Y-%m-%d")

    if days_val > 1:
        hotel_in = flight_out
        hotel_out = flight_ret
        stay_nights = (d_end - d_start).days
        requires_lodging = True
    else:
        # 1-day day-trip: departure and return on the same day
        hotel_in = flight_out
        hotel_out = (d_start + timedelta(days=1)).strftime("%Y-%m-%d")
        stay_nights = 0
        requires_lodging = False

    is_prop = not has_explicit_start
    confirmed_val = is_confirmed if is_confirmed is not None else has_explicit_start

    return TripDateContext(
        days=days_val,
        start_date=d_start,
        end_date=d_end,
        flight_outbound_date=flight_out,
        flight_return_date=flight_ret,
        hotel_check_in_date=hotel_in,
        hotel_check_out_date=hotel_out,
        stay_nights=stay_nights,
        requires_lodging=requires_lodging,
        is_proposed=is_prop,
        date_confirmed=confirmed_val,
    )
