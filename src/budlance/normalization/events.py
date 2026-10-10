"""Event normalizer converting Google Search events_results to EventOption models."""

import datetime as dt
import logging
import re
from typing import Any
from budlance.schemas.travel import EventOption
from budlance.serpapi.models import DataSource, TravelDataEnvelope

logger = logging.getLogger(__name__)


def _parse_approx_date(date_val: Any) -> dt.date | None:
    if not date_val:
        return None
    if isinstance(date_val, dt.date):
        return date_val
    s = str(date_val).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(s[:10], fmt).date()
        except Exception:
            pass
    return None


def filter_events_overlapping_dates(
    events: list[EventOption],
    trip_start: str | dt.date | None,
    trip_end: str | dt.date | None,
) -> list[EventOption]:
    """Filter discovered events to only those that overlap with the confirmed trip dates."""
    if not trip_start or not trip_end or not events:
        return events

    start = _parse_approx_date(trip_start)
    end = _parse_approx_date(trip_end)
    if not start or not end:
        return events

    trip_days: list[dt.date] = []
    curr = start
    while curr <= end:
        trip_days.append(curr)
        curr += dt.timedelta(days=1)

    matching_events: list[EventOption] = []
    for ev in events:
        ev_start = _parse_approx_date(ev.start_date)
        ev_end = _parse_approx_date(ev.end_date) or ev_start
        if ev_start and ev_end:
            if max(start, ev_start) <= min(end, ev_end):
                matching_events.append(ev)
                continue
            else:
                continue

        if ev.date_str:
            d_lower = ev.date_str.lower()
            matched_day = False
            for td in trip_days:
                month_abbr = td.strftime("%b").lower()
                month_full = td.strftime("%B").lower()
                day_num = str(td.day)
                day_pad = f"{td.day:02d}"

                if (month_abbr in d_lower or month_full in d_lower) and (
                    re.search(rf"\b0?{day_num}\b", d_lower) or day_pad in d_lower
                ):
                    matched_day = True
                    break
            if matched_day:
                matching_events.append(ev)
                continue

        # If date is completely absent or cannot be verified against the dates, do not show as verified
        logger.debug("[EVENTS] Event '%s' date '%s' does not overlap trip [%s to %s]", ev.name, ev.date_str, trip_start, trip_end)

    return matching_events


def normalize_events(envelope: TravelDataEnvelope) -> list[EventOption]:
    """Parse SerpApi Google Search envelope containing events_results into EventOption models."""
    results: list[EventOption] = []
    data = envelope.data

    if not isinstance(data, dict):
        logger.warning("Event data is not a valid dictionary envelope.")
        return results

    raw_events = data.get("events_results") or []
    if not isinstance(raw_events, list):
        return results

    is_verified = (envelope.source in (DataSource.LIVE, "LIVE_PROVIDER")) and not envelope.is_fallback

    for item in raw_events:
        if not isinstance(item, dict):
            continue

        title = item.get("title")
        if not title or not str(title).strip():
            continue

        date_info = item.get("date") or {}
        date_str = None
        start_date = None
        end_date = None
        if isinstance(date_info, dict):
            date_str = date_info.get("when") or date_info.get("start_date")
            start_date = date_info.get("start_date")
            end_date = date_info.get("end_date")
        elif isinstance(date_info, str):
            date_str = date_info

        venue_info = item.get("venue") or {}
        venue_name = None
        if isinstance(venue_info, dict):
            venue_name = venue_info.get("name")
        elif isinstance(venue_info, str):
            venue_name = venue_info

        address_info = item.get("address")
        address_str = (
            ", ".join(address_info)
            if isinstance(address_info, list)
            else (str(address_info) if address_info else None)
        )

        ticket_info = item.get("ticket_info")
        ticket_str = str(ticket_info) if ticket_info else None

        option = EventOption(
            name=str(title).strip(),
            date_str=str(date_str).strip() if date_str else None,
            start_date=str(start_date).strip() if start_date else None,
            end_date=str(end_date).strip() if end_date else None,
            venue=str(venue_name).strip() if venue_name else None,
            address=address_str,
            link=item.get("link"),
            description=item.get("description"),
            ticket_info=ticket_str,
            is_verified=is_verified,
            source=envelope.source,
            is_fallback=envelope.is_fallback,
            retrieval_timestamp=envelope.provenance.retrieval_timestamp if envelope.provenance else envelope.created_at,
            provenance=envelope.provenance,
        )
        results.append(option)

    return results

