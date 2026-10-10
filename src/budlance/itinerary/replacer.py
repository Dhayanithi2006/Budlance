"""Surgical itinerary activity replacement engine.

Enables replacing a specific unavailable or closed attraction on Day X
with a verified nearby alternative matching the user's category (e.g. nature spot)
while strictly preserving all other days, flights, hotels, and budget allocations.
"""

from decimal import Decimal
import logging
from typing import Any
from budlance.attractions.models import Attraction
from budlance.itinerary.models import GeneratedItinerary, ItineraryDay, ItineraryItem
from budlance.schemas.travel import PlaceOption, RouteOption
from budlance.serpapi.models import DataSource

logger = logging.getLogger(__name__)


def replace_itinerary_item(
    itinerary: GeneratedItinerary,
    target_name_or_cat: str,
    replacement_category: str = "nature",
    day_number: int | None = None,
    available_attractions: list[Attraction] | None = None,
    available_places: list[PlaceOption] | None = None,
    destination: str = "Goa",
    replacement_item: Attraction | PlaceOption | None = None,
    route_option: RouteOption | None = None,
) -> tuple[GeneratedItinerary, ItineraryItem | None, ItineraryItem | None]:
    """Surgically replace an item in the itinerary.

    Returns:
        (updated_itinerary, old_item, new_item)
    """
    target_clean = (target_name_or_cat or "").strip().lower()
    rep_cat_clean = (replacement_category or "nature").strip().lower()

    # 1. Identify target day and target item
    found_day: ItineraryDay | None = None
    found_item_idx: int | None = None
    old_item: ItineraryItem | None = None

    for day in itinerary.days:
        if day_number is not None and day.day_number != day_number:
            continue
        for idx, item in enumerate(day.items):
            act_low = (item.activity or "").lower()
            place_low = (item.place_name or "").lower()
            attr_low = (item.attraction_name or "").lower()
            cat_low = (item.category or "").lower()
            slot_low = (item.time_slot or "").lower()

            if (
                target_clean in act_low
                or target_clean in place_low
                or target_clean in attr_low
                or target_clean in cat_low
                or target_clean in slot_low
                or (target_clean == "museum" and any(k in act_low or k in place_low for k in ("museum", "gallery", "exhibit")))
                or (target_clean in ("activity", "attraction") and (item.slot_type == "attraction" or item.attraction_name))
            ):
                found_day = day
                found_item_idx = idx
                old_item = item
                break
        if found_day:
            break

    if not found_day or found_item_idx is None or old_item is None:
        logger.warning(
            "Could not find matching item for target '%s' on day %s in itinerary.",
            target_name_or_cat,
            day_number,
        )
        return itinerary, None, None

    # 2. Gather names of already-scheduled attractions across the itinerary to avoid duplicates
    used_names: set[str] = set()
    for d in itinerary.days:
        for it in d.items:
            if it.place_name:
                used_names.add(it.place_name.strip().lower())
            if it.attraction_name:
                used_names.add(it.attraction_name.strip().lower())

    # 3. Search replacement candidates
    candidate_attr: Attraction | None = None
    candidate_place: PlaceOption | None = None

    if replacement_item is not None:
        if isinstance(replacement_item, Attraction):
            candidate_attr = replacement_item
        elif isinstance(replacement_item, PlaceOption):
            candidate_place = replacement_item

    # Check curated attractions pool
    if not candidate_attr and not candidate_place and available_attractions:
        for attr in available_attractions:
            attr_name_low = attr.name.strip().lower()
            if attr_name_low in used_names:
                continue
            cat = (attr.category or "").lower()
            desc = (attr.description or "").lower()
            if (
                rep_cat_clean in attr_name_low
                or attr_name_low in rep_cat_clean
                or rep_cat_clean in cat
                or cat in rep_cat_clean
                or (rep_cat_clean in ("nature", "nature spot") and any(syn in cat or syn in desc for syn in ("nature", "waterfall", "beach", "sanctuary", "wildlife", "forest", "scenic", "park")))
            ):
                candidate_attr = attr
                break

    # Check places pool if no curated attraction candidate found
    if not candidate_attr and not candidate_place and available_places:
        for p in available_places:
            p_name_low = p.name.strip().lower()
            if p_name_low in used_names:
                continue
            p_cat = (p.category or "").lower()
            p_desc = (p.description or "").lower()
            if (
                rep_cat_clean in p_name_low
                or p_name_low in rep_cat_clean
                or rep_cat_clean in p_cat
                or p_cat in rep_cat_clean
                or (rep_cat_clean in ("nature", "nature spot") and any(syn in p_cat or syn in p_desc for syn in ("nature", "park", "garden", "beach", "waterfall", "wildlife", "lake")))
            ):
                candidate_place = p
                break

    # If still not found, search attractions catalog for destination
    if not candidate_attr and not candidate_place:
        from budlance.attractions.selector import AttractionSelector
        selector = AttractionSelector()
        all_dest_attrs = selector._load_attractions(destination)
        for attr in all_dest_attrs:
            attr_name_low = attr.name.strip().lower()
            if attr_name_low in used_names:
                continue
            cat = (attr.category or "").lower()
            desc = (attr.description or "").lower()
            if (
                rep_cat_clean in attr_name_low
                or attr_name_low in rep_cat_clean
                or rep_cat_clean in cat
                or cat in rep_cat_clean
                or (rep_cat_clean in ("nature", "nature spot") and any(syn in cat or syn in desc for syn in ("nature", "waterfall", "beach", "sanctuary", "wildlife", "forest", "scenic", "park")))
            ):
                candidate_attr = attr
                break

    # 4. Construct replacement ItineraryItem
    if candidate_attr:
        fee_inr = candidate_attr.entry_fee_inr
        is_unknown = getattr(candidate_attr, "is_fee_unknown", False) or (fee_inr is None)
        fee_status = "unknown" if is_unknown else ("free" if (fee_inr or 0) == 0 else "verified")
        time_slot = old_item.time_slot
        time_win = old_item.approximate_time_window or ("02:00 PM – 04:30 PM" if time_slot == "Afternoon" else "11:00 AM – 01:30 PM")
        diff_region = bool(candidate_attr.region and old_item.region and candidate_attr.region.lower().strip() != old_item.region.lower().strip())
        if route_option and route_option.duration_minutes > 0:
            travel_time = route_option.duration_minutes
            src_lbl = getattr(route_option.source, "value", str(route_option.source))
            route_notes = f"Transfer: ~{route_option.distance_km:.1f} km ({travel_time} min). Route source: {src_lbl}."
        else:
            travel_time = 45 if diff_region else 30
            est_dist = 15.0 if diff_region else 5.0
            route_notes = f"Estimated transfer: ~{est_dist:g} km (~{travel_time} min) [CONFIG_ESTIMATE: unverified transfer heuristic, local traffic and route may vary]."

        new_item = ItineraryItem(
            time_slot=time_slot,
            activity=f"Visit {candidate_attr.name}",
            place_name=candidate_attr.name,
            attraction_name=candidate_attr.name,
            category=candidate_attr.category or "nature",
            planned_cost=Decimal(str(fee_inr)) if not is_unknown and fee_inr is not None else Decimal("0.00"),
            source=DataSource.UNKNOWN if is_unknown else (DataSource.LIVE if getattr(candidate_attr, "source", None) in (DataSource.LIVE, "LIVE") else DataSource.ESTIMATED),
            is_curated=True,
            opening_hours=candidate_attr.opening_hours,
            entry_fee_inr=fee_inr if not is_unknown else None,
            is_fee_unknown=is_unknown,
            description=candidate_attr.description or f"Scenic nature spot in {destination}.",
            slot_type="attraction",
            region=candidate_attr.region or found_day.region,
            start_time=old_item.start_time,
            end_time=old_item.end_time,
            approximate_time_window=time_win,
            duration_minutes=int(float(candidate_attr.typical_time_hours or 2.0) * 60),
            travel_time_to_next_minutes=travel_time,
            admission_fee_status=fee_status,
            interest_suitability=f"Replaced closed {target_name_or_cat} with nearby {candidate_attr.name} ({candidate_attr.category}).",
            notes=route_notes,
        )
    elif candidate_place:
        fee_inr = candidate_place.entry_fee_inr
        is_unknown = getattr(candidate_place, "is_fee_unknown", False) or (fee_inr is None)
        fee_status = "unknown" if is_unknown else ("free" if (fee_inr or 0) == 0 else "verified")
        time_slot = old_item.time_slot
        time_win = old_item.approximate_time_window or ("02:00 PM – 04:30 PM" if time_slot == "Afternoon" else "11:00 AM – 01:30 PM")
        diff_region = bool(getattr(candidate_place, "region", None) and old_item.region and str(candidate_place.region).lower().strip() != str(old_item.region).lower().strip())
        if route_option and route_option.duration_minutes > 0:
            travel_time = route_option.duration_minutes
            src_lbl = getattr(route_option.source, "value", str(route_option.source))
            route_notes = f"Transfer: ~{route_option.distance_km:.1f} km ({travel_time} min). Route source: {src_lbl}."
        else:
            travel_time = 45 if diff_region else 30
            est_dist = 15.0 if diff_region else 5.0
            route_notes = f"Estimated transfer: ~{est_dist:g} km (~{travel_time} min) [CONFIG_ESTIMATE: unverified transfer heuristic, local traffic and route may vary]."

        new_item = ItineraryItem(
            time_slot=time_slot,
            activity=f"Explore {candidate_place.name}",
            place_name=candidate_place.name,
            attraction_name=candidate_place.name,
            category=candidate_place.category or "nature",
            planned_cost=Decimal(str(fee_inr)) if not is_unknown and fee_inr is not None else Decimal("0.00"),
            source=DataSource.UNKNOWN if is_unknown else candidate_place.source,
            is_curated=False,
            opening_hours=candidate_place.opening_hours,
            entry_fee_inr=int(fee_inr) if (fee_inr is not None and not is_unknown) else None,
            is_fee_unknown=is_unknown,
            description=candidate_place.description or f"Scenic nature spot in {destination}.",
            slot_type="attraction",
            region=found_day.region,
            start_time=old_item.start_time,
            end_time=old_item.end_time,
            approximate_time_window=time_win,
            duration_minutes=int(float(candidate_place.typical_time_hours or 2.0) * 60),
            travel_time_to_next_minutes=travel_time,
            admission_fee_status=fee_status,
            external_link=candidate_place.link,
            interest_suitability=f"Replaced closed {target_name_or_cat} with nearby {candidate_place.name}.",
            notes=route_notes,
        )
    else:
        logger.warning("No replacement found for category '%s'", replacement_category)
        return itinerary, old_item, None

    # 5. Swap item in place
    found_day.items[found_item_idx] = new_item

    # Recompute daily estimated cost
    daily_food_allowance = Decimal("0.00")
    if not any(it.category == "food" for it in found_day.items):
        daily_food_allowance = Decimal("500.00")
    found_day.daily_estimated_cost = round(
        sum(item.planned_cost for item in found_day.items) + daily_food_allowance,
        2,
    )

    # Recompute total planned cost
    itinerary.total_planned_cost = round(
        sum(d.daily_estimated_cost for d in itinerary.days),
        2,
    )

    # Update summary of the day
    curated_names = [it.attraction_name for it in found_day.items if it.attraction_name]
    if curated_names:
        found_day.theme_or_summary = f"Day {found_day.day_number}: " + " & ".join(curated_names)

    return itinerary, old_item, new_item
