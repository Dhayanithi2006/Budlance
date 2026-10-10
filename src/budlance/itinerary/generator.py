"""Itinerary Generator constructing day-by-day travel schedules from FEASIBLE budget results."""

import logging
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from budlance.attractions.models import Attraction
from budlance.attractions.selector import AttractionSelector
from budlance.db.models import Itinerary as ItineraryRecord, utc_now
from budlance.db.repositories.itinerary_repo import ItineraryRepository
from budlance.engine.models import BudgetEvaluationResult
from budlance.itinerary.models import GeneratedItinerary, ItineraryDay, ItineraryItem
from budlance.itinerary.solar import get_sunset_window
from budlance.schemas.travel import FlightOption, HotelOption, PlaceOption, RouteOption, TransitOption
from budlance.serpapi.models import DataSource

logger = logging.getLogger(__name__)


class ItineraryGenerator:
    """Constructs day-by-day itineraries strictly downstream of the Reverse-Budget Engine.

    Financial Boundary:
    The Itinerary Generator NEVER calculates trip feasibility or alters budget totals.
    It formats and schedules the already-approved components and allocations.
    """

    def __init__(
        self,
        itinerary_repo: ItineraryRepository | None = None,
        attraction_selector: AttractionSelector | None = None,
    ) -> None:
        self.itinerary_repo = itinerary_repo or ItineraryRepository()
        self.attraction_selector = attraction_selector or AttractionSelector()

    def generate(
        self,
        trip_id: UUID,
        destination: str,
        evaluation: BudgetEvaluationResult,
        days: int,
        transport: FlightOption | TransitOption | None = None,
        hotel: HotelOption | None = None,
        places: list[PlaceOption] | None = None,
        route: RouteOption | None = None,
        travel_party: str | None = None,
        interests: list[str] | None = None,
        attractions: list[Attraction] | None = None,
        start_date: str | None = None,
        events: list[Any] | None = None,
        dietary_preference: str | None = None,
        schedule_pace: str | None = None,
        earliest_activity_time: str | None = None,
        arrival_time: str | None = None,
        departure_time: str | None = None,
        special_activity_request: str | None = None,
    ) -> GeneratedItinerary:
        """Generate a complete day-by-day itinerary if the evaluation is FEASIBLE.

        Two distinct modes:
        - MODE A: Curated data exists (e.g. Gujarat). Allocates real verified attractions.
        - MODE B: No curated data exists. Allocates structured free time without fake landmarks.
        """
        # 1. Guard against infeasible input
        if not evaluation.is_feasible:
            logger.info("Cannot generate itinerary for infeasible budget evaluation.")
            return GeneratedItinerary(
                trip_id=trip_id,
                destination=destination,
                days_count=0,
                days=[],
                is_feasible=False,
                total_budget=evaluation.breakdown.total_budget,
                total_planned_cost=Decimal("0.00"),
                feasibility_note=evaluation.explanation,
            )

        days_count = max(1, days)
        calc_end_date = None
        d_start_obj = None
        if start_date:
            from datetime import datetime as dt, timedelta
            d_start_obj = dt.strptime(start_date, "%Y-%m-%d").date()
            calc_end_date = (d_start_obj + timedelta(days=max(0, days_count - 1))).strftime("%Y-%m-%d")

        breakdown = evaluation.breakdown
        daily_food_budget = round(breakdown.food_cost / Decimal(days_count), 2)
        daily_transit_budget = round(breakdown.local_transit_cost / Decimal(days_count), 2)

        # 1. Determine available attractions (Mode A vs Mode B)
        if attractions is not None:
            available_attractions = list(attractions)
        else:
            available_attractions = list(
                self.attraction_selector.select_for_itinerary(
                    destination=destination,
                    travel_party=travel_party,
                    interests=interests,
                    days=days,
                )
            )

        # Live Places Precedence: When live places from Google Maps / local search exist and are non-fallback,
        # they take precedence over static catalog when attractions is not explicitly provided.
        has_real_live_places = bool(places and any(not getattr(p, "is_fallback", False) for p in places))
        if has_real_live_places and attractions is None:
            return self._generate_legacy_places_itinerary(
                trip_id=trip_id,
                destination=destination,
                evaluation=evaluation,
                days_count=days_count,
                transport=transport,
                hotel=hotel,
                places=places,
            )

        itinerary_days: list[ItineraryDay] = []
        honest_note: str | None = None

        if available_attractions:
            # =================================================================
            # MODE A: Curated data exists (e.g. Gujarat) or Live Provider Places
            # =================================================================
            active_interests = [i.strip().lower() for i in (interests or []) if i and i.strip()]
            has_interest_filter = len(active_interests) > 0

            def _matches_interest(attr: Attraction) -> bool:
                cat = attr.category.lower()
                name = attr.name.lower()
                desc = attr.description.lower()
                for int_k in active_interests:
                    if int_k in cat or cat in int_k or int_k in name or int_k in desc:
                        return True
                    if (int_k in ("heritage", "culture", "history") and 
                        any(syn in cat or syn in desc for syn in ("heritage", "culture", "history", "historic"))):
                        return True
                return False

            matching_pool = [a for a in available_attractions if _matches_interest(a)] if has_interest_filter else list(available_attractions)
            honest_note = None

            if has_interest_filter and len(matching_pool) == 0:
                honest_note = f"Note: {destination} has limited verified activities matching '{', '.join(interests or [])}'. We kept your schedule light without off-tone filler."
                pool_to_use = list(available_attractions[:days_count])
            else:
                pool_to_use = list(available_attractions)

            # Cluster attractions by region
            by_region: dict[str, list[Attraction]] = {}
            regions_order: list[str] = []
            for a in pool_to_use:
                r = getattr(a, "region", None) or destination
                if r not in by_region:
                    by_region[r] = []
                    regions_order.append(r)
                by_region[r].append(a)

            # Prioritize regions that have multiple attractions so each day clusters within its region
            regions_order.sort(key=lambda r: len(by_region[r]), reverse=True)

            used_regions: set[str] = set()
            for d in range(1, days_count + 1):
                day_items: list[ItineraryItem] = []
                day_date = (d_start_obj + timedelta(days=d - 1)).strftime("%Y-%m-%d") if d_start_obj else None

                # Select ONE region for this day (prefer unused regions that have items)
                available_unused = [r for r in regions_order if r not in used_regions and by_region.get(r)]
                if available_unused:
                    day_region = available_unused[0]
                else:
                    candidates_with_items = [r for r in regions_order if by_region.get(r)]
                    if candidates_with_items:
                        day_region = candidates_with_items[0]
                    elif regions_order:
                        day_region = regions_order[(d - 1) % len(regions_order)]
                    else:
                        day_region = destination

                used_regions.add(day_region)
                region_pool = by_region.get(day_region, [])

                # -------------------------------------------------------------
                # 1. MORNING SLOT
                # -------------------------------------------------------------
                if d == 1 and arrival_time is not None:
                    trans_name = getattr(transport, "airline", None) or getattr(transport, "name_or_operator", "Onward Transport") if transport else "Gateway Transport"
                    arr_t = arrival_time
                    hotel_part = f" and transfer to {hotel.name}" if hotel else ""
                    day_items.append(
                        ItineraryItem(
                            time_slot="Morning",
                            start_time=arr_t,
                            end_time="12:30 PM",
                            approximate_time_window=f"{arr_t} – 12:30 PM",
                            activity=f"Arrival via {trans_name}{hotel_part}",
                            place_name=hotel.name if hotel else destination,
                            category="accommodation" if hotel else "transport",
                            planned_cost=Decimal("0.00"),
                            source=transport.source if transport else DataSource.ESTIMATED,
                            notes=f"Land at {arr_t}, gateway transfer and hotel check-in",
                            description=f"Arrive in {destination}, complete gateway transfer and check in to accommodation.",
                            external_link=getattr(transport, "deep_link", None) or (getattr(hotel, "deep_link", None) if hotel else None),
                            is_approximate_schedule=True,
                            duration_minutes=150,
                            travel_time_to_next_minutes=30,
                            dietary_tags=["vegetarian"] if dietary_preference == "vegetarian" else [],
                        )
                    )
                elif d == days_count and days_count > 1 and not region_pool:
                    day_items.append(
                        ItineraryItem(
                            time_slot="Morning",
                            start_time="08:30 AM",
                            end_time="10:30 AM",
                            approximate_time_window="08:30 AM – 10:30 AM",
                            activity="Morning exploration and breakfast",
                            place_name=destination,
                            category="food",
                            planned_cost=daily_food_budget,
                            source=DataSource.ESTIMATED,
                            description=f"Morning exploration and local breakfast in {destination}.",
                            dietary_tags=["vegetarian"] if dietary_preference == "vegetarian" else [],
                            duration_minutes=120,
                            travel_time_to_next_minutes=30,
                        )
                    )
                else:
                    start_m_time = earliest_activity_time or "09:00 AM"
                    end_m_time = "11:30 AM"
                    if region_pool:
                        attr1 = region_pool.pop(0)
                        day_items.append(
                            ItineraryItem(
                                time_slot="Morning",
                                start_time=start_m_time,
                                end_time=end_m_time,
                                approximate_time_window=f"{start_m_time} – {end_m_time}",
                                activity=f"Visit {attr1.name}",
                                place_name=attr1.name,
                                category=getattr(attr1, "category", None) or "attraction",
                                planned_cost=Decimal(str(attr1.entry_fee_inr or 0)),
                                source=DataSource.LIVE if getattr(attr1, "is_fee_unknown", False) else DataSource.ESTIMATED,
                                is_curated=getattr(attr1, "entry_fee_inr", None) is not None and not getattr(attr1, "is_fee_unknown", False),
                                attraction_name=attr1.name,
                                opening_hours=getattr(attr1, "opening_hours", None),
                                entry_fee_inr=getattr(attr1, "entry_fee_inr", None),
                                is_fee_unknown=getattr(attr1, "is_fee_unknown", False),
                                description=getattr(attr1, "description", "") or f"Visit historical highlights at {attr1.name}.",
                                slot_type="attraction",
                                suggestion=f"Best visited in the {getattr(attr1, 'best_time_of_day', 'morning')}. Duration: ~{getattr(attr1, 'typical_time_hours', 2)}h.",
                                region=day_region,
                                duration_minutes=int(float(getattr(attr1, "typical_time_hours", 2.0) or 2.0) * 60),
                                travel_time_to_next_minutes=30,
                                admission_fee_status="unknown" if getattr(attr1, "is_fee_unknown", False) else ("free" if getattr(attr1, "entry_fee_inr", 0) == 0 else "verified"),
                                interest_suitability=f"Selected for {travel_party or 'traveler'} exploring {attr1.category}.",
                            )
                        )
                    else:
                        day_items.append(
                            ItineraryItem(
                                time_slot="Morning",
                                start_time=start_m_time,
                                end_time=end_m_time,
                                approximate_time_window=f"{start_m_time} – {end_m_time}",
                                activity="Morning at leisure",
                                place_name=destination,
                                category="free_time",
                                planned_cost=Decimal("0.00"),
                                source=DataSource.ESTIMATED,
                                is_curated=False,
                                attraction_name=None,
                                opening_hours=None,
                                entry_fee_inr=0,
                                description=f"Morning at leisure in {day_region}.",
                                slot_type="free_time",
                                suggestion="Explore local cafes, markets, and neighborhoods at your own pace",
                                region=day_region,
                                duration_minutes=150,
                            )
                        )

                # -------------------------------------------------------------
                # 2. AFTERNOON SLOT
                # -------------------------------------------------------------
                matching_event = None
                if events and day_date:
                    for ev in events:
                        ev_start = str(getattr(ev, "start_date", "") or getattr(ev, "date_str", "") or "")
                        if day_date in ev_start or ev_start in day_date:
                            matching_event = ev
                            break

                if has_interest_filter and len(matching_pool) == 0:
                    day_items.append(
                        ItineraryItem(
                            time_slot="Afternoon",
                            start_time="02:00 PM",
                            end_time="04:30 PM",
                            approximate_time_window="02:00 PM – 04:30 PM",
                            activity="Free time for self-guided exploration",
                            place_name=destination,
                            category="free_time",
                            planned_cost=Decimal("0.00"),
                            source=DataSource.ESTIMATED,
                            is_curated=False,
                            attraction_name=None,
                            opening_hours=None,
                            entry_fee_inr=0,
                            description=f"Self-guided exploration of {day_region}.",
                            slot_type="free_time",
                            suggestion="Explore local cafes, markets, and neighborhoods at your own pace",
                            region=day_region,
                            duration_minutes=150,
                        )
                    )
                elif d == days_count and days_count > 1 and hotel:
                    day_items.append(
                        ItineraryItem(
                            time_slot="Afternoon",
                            start_time="11:30 AM",
                            end_time="02:00 PM",
                            approximate_time_window="11:30 AM – 02:00 PM",
                            activity=f"Check out from {hotel.name} and transit to departure hub",
                            place_name=hotel.name,
                            category="accommodation",
                            planned_cost=Decimal("0.00"),
                            source=hotel.source,
                            description=f"Check out from {hotel.name} and transfer toward departure gateway.",
                            duration_minutes=150,
                            external_link=getattr(hotel, "deep_link", None),
                        )
                    )
                elif region_pool:
                    attr2 = region_pool.pop(0)
                    start_a_time = "02:00 PM" if not earliest_activity_time else ("02:00 PM" if "AM" in earliest_activity_time else earliest_activity_time)
                    end_a_time = "04:30 PM"
                    day_items.append(
                        ItineraryItem(
                            time_slot="Afternoon",
                            start_time=start_a_time,
                            end_time=end_a_time,
                            approximate_time_window=f"{start_a_time} – {end_a_time}",
                            activity=f"Explore {attr2.name}",
                            place_name=attr2.name,
                            category=getattr(attr2, "category", None) or "attraction",
                            planned_cost=Decimal(str(attr2.entry_fee_inr or 0)),
                            source=DataSource.LIVE if getattr(attr2, "is_fee_unknown", False) else DataSource.ESTIMATED,
                            is_curated=getattr(attr2, "entry_fee_inr", None) is not None and not getattr(attr2, "is_fee_unknown", False),
                            attraction_name=attr2.name,
                            opening_hours=getattr(attr2, "opening_hours", None),
                            entry_fee_inr=getattr(attr2, "entry_fee_inr", None),
                            is_fee_unknown=getattr(attr2, "is_fee_unknown", False),
                            description=getattr(attr2, "description", "") or f"Explore cultural sights at {attr2.name}.",
                            slot_type="attraction",
                            suggestion=f"Best visited in the {getattr(attr2, 'best_time_of_day', 'afternoon')}. Duration: ~{getattr(attr2, 'typical_time_hours', 2)}h.",
                            region=day_region,
                            duration_minutes=int(float(getattr(attr2, "typical_time_hours", 2.0) or 2.0) * 60),
                            travel_time_to_next_minutes=30,
                            admission_fee_status="unknown" if getattr(attr2, "is_fee_unknown", False) else ("free" if getattr(attr2, "entry_fee_inr", 0) == 0 else "verified"),
                            dietary_tags=["vegetarian"] if dietary_preference == "vegetarian" else [],
                        )
                    )
                else:
                    day_items.append(
                        ItineraryItem(
                            time_slot="Afternoon",
                            start_time="02:00 PM",
                            end_time="04:30 PM",
                            approximate_time_window="02:00 PM – 04:30 PM",
                            activity="Free time for self-guided exploration",
                            place_name=destination,
                            category="free_time",
                            planned_cost=Decimal("0.00"),
                            source=DataSource.ESTIMATED,
                            is_curated=False,
                            attraction_name=None,
                            opening_hours=None,
                            entry_fee_inr=0,
                            description=f"Self-guided exploration of {day_region}.",
                            slot_type="free_time",
                            suggestion="Explore local cafes, markets, and neighborhoods at your own pace",
                            region=day_region,
                            duration_minutes=150,
                            dietary_tags=["vegetarian"] if dietary_preference == "vegetarian" else [],
                        )
                    )

                # -------------------------------------------------------------
                # 3. EVENING SLOT
                # -------------------------------------------------------------
                is_sunset_req = special_activity_request == "sunset at a beach" or (
                    interests and any("beach" in str(i).lower() for i in interests) and (d == 1 or d == 2)
                )
                beach_attr_idx = next(
                    (i for i, a in enumerate(region_pool) if "beach" in (getattr(a, "category", "") or "").lower() or "beach" in a.name.lower()),
                    None,
                )
                evening_idx = next(
                    (i for i, a in enumerate(region_pool) if getattr(a, "best_time_of_day", "").lower() == "evening"),
                    None,
                )

                if d == days_count and days_count > 1 and transport:
                    t_ret_name = getattr(transport, "airline", None) or getattr(transport, "name_or_operator", "Return Transport") if transport else "Return Transport"
                    ret_start = departure_time or "03:30 PM"
                    day_items.append(
                        ItineraryItem(
                            time_slot="Evening",
                            start_time=ret_start,
                            end_time="06:30 PM",
                            approximate_time_window=f"{ret_start} onwards",
                            activity=f"Return journey via {t_ret_name}",
                            place_name=destination,
                            category="transport",
                            planned_cost=Decimal("0.00"),
                            source=transport.source if transport else DataSource.ESTIMATED,
                            notes="Return transport timing verified",
                            description=f"Safe travels on your return journey from {destination}.",
                            external_link=getattr(transport, "deep_link", None),
                            duration_minutes=180,
                        )
                    )
                elif is_sunset_req and beach_attr_idx is not None:
                    beach_attr = region_pool.pop(beach_attr_idx)
                    sunset_info = get_sunset_window(
                        date_val=day_date,
                        latitude=getattr(beach_attr, "latitude", None),
                        longitude=getattr(beach_attr, "longitude", None),
                        destination=destination,
                    )
                    day_items.append(
                        ItineraryItem(
                            time_slot="Evening",
                            start_time=sunset_info["start_time"],
                            end_time=sunset_info["end_time"],
                            approximate_time_window=sunset_info["approximate_time_window"],
                            activity=f"Sunset viewing at {beach_attr.name}",
                            place_name=beach_attr.name,
                            category="beach",
                            planned_cost=Decimal(str(beach_attr.entry_fee_inr or 0)),
                            source=DataSource.LIVE if getattr(beach_attr, "is_fee_unknown", False) else DataSource.ESTIMATED,
                            is_curated=True,
                            attraction_name=beach_attr.name,
                            opening_hours=getattr(beach_attr, "opening_hours", None),
                            entry_fee_inr=getattr(beach_attr, "entry_fee_inr", None),
                            is_fee_unknown=getattr(beach_attr, "is_fee_unknown", False),
                            description=beach_attr.description or f"Spectacular sunset views over coastal waters at {beach_attr.name}.",
                            slot_type="attraction",
                            region=day_region,
                            is_sunset_timing=True,
                            notes=sunset_info["notes"],
                            admission_fee_status="free" if (beach_attr.entry_fee_inr or 0) == 0 else "verified",
                            duration_minutes=120,
                            dietary_tags=["vegetarian"] if dietary_preference == "vegetarian" else [],
                        )
                    )
                elif matching_event:
                    ev_title = (
                        getattr(matching_event, "name", None)
                        or getattr(matching_event, "title", None)
                        or (matching_event.get("name") if isinstance(matching_event, dict) else (matching_event.get("title") if isinstance(matching_event, dict) else str(matching_event)))
                    )
                    ev_venue = getattr(matching_event, "venue", None) or (matching_event.get("venue") if isinstance(matching_event, dict) else destination)
                    ev_desc = getattr(matching_event, "description", None) or (matching_event.get("description") if isinstance(matching_event, dict) else f"Verified event in {destination}.")
                    ev_link = getattr(matching_event, "link", None) or (matching_event.get("link") if isinstance(matching_event, dict) else None)
                    ev_source = getattr(matching_event, "source", None) or DataSource.LIVE
                    day_items.append(
                        ItineraryItem(
                            time_slot="Evening",
                            start_time="06:30 PM",
                            end_time="09:00 PM",
                            approximate_time_window="06:30 PM – 09:00 PM",
                            activity=f"Event: {ev_title}",
                            place_name=ev_venue,
                            category="attraction",
                            planned_cost=Decimal("0.00"),
                            source=ev_source,
                            is_curated=True,
                            attraction_name=ev_title,
                            description=ev_desc,
                            external_link=ev_link,
                            slot_type="attraction",
                            region=day_region,
                            notes=getattr(matching_event, "ticket_info", None) if not isinstance(matching_event, dict) else matching_event.get("ticket_info", "Admission details per event organizer"),
                            duration_minutes=150,
                            dietary_tags=["vegetarian"] if dietary_preference == "vegetarian" else [],
                        )
                    )
                elif evening_idx is not None and not (has_interest_filter and len(matching_pool) == 0):
                    attr_eve = region_pool.pop(evening_idx)
                    day_items.append(
                        ItineraryItem(
                            time_slot="Evening",
                            start_time="05:30 PM",
                            end_time="07:15 PM",
                            approximate_time_window="05:30 PM – 07:15 PM",
                            activity=f"Evening visit to {attr_eve.name}",
                            place_name=attr_eve.name,
                            category=getattr(attr_eve, "category", None) or "attraction",
                            planned_cost=Decimal(str(attr_eve.entry_fee_inr or 0)),
                            source=DataSource.LIVE if getattr(attr_eve, "is_fee_unknown", False) else DataSource.ESTIMATED,
                            is_curated=getattr(attr_eve, "entry_fee_inr", None) is not None and not getattr(attr_eve, "is_fee_unknown", False),
                            attraction_name=attr_eve.name,
                            opening_hours=getattr(attr_eve, "opening_hours", None),
                            entry_fee_inr=getattr(attr_eve, "entry_fee_inr", None),
                            is_fee_unknown=getattr(attr_eve, "is_fee_unknown", False),
                            description=getattr(attr_eve, "description", "") or f"Evening visit to {attr_eve.name}.",
                            slot_type="attraction",
                            suggestion=f"Best visited in the evening. Duration: ~{getattr(attr_eve, 'typical_time_hours', 2)}h.",
                            region=day_region,
                            admission_fee_status="unknown" if getattr(attr_eve, "is_fee_unknown", False) else ("free" if getattr(attr_eve, "entry_fee_inr", 0) == 0 else "verified"),
                            duration_minutes=105,
                            dietary_tags=["vegetarian"] if dietary_preference == "vegetarian" else [],
                        )
                    )
                else:
                    eve_desc = "Vegetarian dinner and relaxed evening" if dietary_preference == "vegetarian" else "Local dining and relaxation"
                    eve_tags = ["vegetarian"] if dietary_preference == "vegetarian" else []
                    day_items.append(
                        ItineraryItem(
                            time_slot="Evening",
                            start_time="07:00 PM",
                            end_time="09:00 PM",
                            approximate_time_window="07:00 PM – 09:00 PM",
                            activity=f"{eve_desc} in {day_region}",
                            place_name=destination,
                            category="food",
                            planned_cost=daily_food_budget,
                            source=DataSource.ESTIMATED,
                            is_curated=False,
                            attraction_name=None,
                            opening_hours=None,
                            entry_fee_inr=0,
                            description=f"Local dining and relaxation in {day_region}.",
                            slot_type="free_time",
                            suggestion="Sample regional culinary specialties and unwind",
                            region=day_region,
                            dietary_tags=eve_tags,
                            duration_minutes=120,
                        )
                    )

                # Day summary
                curated_names = [it.attraction_name for it in day_items if it.is_curated and it.attraction_name]
                summary = f"Day {d}: " + " & ".join(curated_names) if curated_names else f"Day {d}: Self-guided exploration of {destination}"
                food_allowance = daily_food_budget if not any(it.category == "food" and it.planned_cost > 0 for it in day_items) else Decimal("0.00")
                day_cost = sum(item.planned_cost for item in day_items) + food_allowance

                itinerary_days.append(
                    ItineraryDay(
                        day_number=d,
                        date_str=day_date,
                        theme_or_summary=summary,
                        region=day_region,
                        items=day_items,
                        daily_estimated_cost=round(day_cost, 2),
                    )
                )

        else:
            # =================================================================
            # MODE B: NO curated data exists (structured free time)
            # ZERO fake landmark strings
            # =================================================================
            for d in range(1, days_count + 1):
                day_items = [
                    ItineraryItem(
                        time_slot="Morning",
                        activity="Arrival & check-in / Morning at leisure" if d == 1 else "Morning at leisure",
                        place_name=destination,
                        category="free_time",
                        planned_cost=Decimal("0.00"),
                        source=DataSource.ESTIMATED,
                        is_curated=False,
                        attraction_name=None,
                        opening_hours=None,
                        entry_fee_inr=0,
                        description=(
                            f"Arrival in {destination} and morning at leisure."
                            if d == 1
                            else f"Morning at leisure for relaxed breakfast in {destination}."
                        ),
                        slot_type="free_time",
                        suggestion="Explore local cafes, markets, and neighborhoods at your own pace",
                    ),
                    ItineraryItem(
                        time_slot="Afternoon",
                        activity="Free time for self-guided exploration",
                        place_name=destination,
                        category="free_time",
                        planned_cost=Decimal("0.00"),
                        source=DataSource.ESTIMATED,
                        is_curated=False,
                        attraction_name=None,
                        opening_hours=None,
                        entry_fee_inr=0,
                        description=f"Self-guided exploration of {destination}.",
                        slot_type="free_time",
                        suggestion="Explore local cafes, markets, and neighborhoods at your own pace",
                    ),
                    ItineraryItem(
                        time_slot="Evening",
                        activity="Local dining and relaxation",
                        place_name=destination,
                        category="food",
                        planned_cost=daily_food_budget,
                        source=DataSource.ESTIMATED,
                        is_curated=False,
                        attraction_name=None,
                        opening_hours=None,
                        entry_fee_inr=0,
                        description=f"Local dining and relaxation in {destination}.",
                        slot_type="free_time",
                        suggestion="Explore local cafes, markets, and neighborhoods at your own pace",
                    ),
                ]
                day_cost = sum(item.planned_cost for item in day_items)
                day_date = (d_start_obj + timedelta(days=d - 1)).strftime("%Y-%m-%d") if d_start_obj else None
                itinerary_days.append(
                    ItineraryDay(
                        day_number=d,
                        date_str=day_date,
                        theme_or_summary=f"Day {d}: Self-guided exploration of {destination}",
                        items=day_items,
                        daily_estimated_cost=round(day_cost, 2),
                    )
                )

        total_itinerary_cost = sum(day.daily_estimated_cost for day in itinerary_days)

        generated = GeneratedItinerary(
            trip_id=trip_id,
            destination=destination,
            days_count=days_count,
            days=itinerary_days,
            is_feasible=True,
            total_budget=breakdown.total_budget,
            total_planned_cost=total_itinerary_cost,
            feasibility_note=honest_note or evaluation.explanation,
            start_date=start_date,
            end_date=calc_end_date,
        )

        # Persist in database repository
        db_record = ItineraryRecord(
            id=uuid4(),
            trip_id=trip_id,
            days=[day.model_dump(mode="json") for day in itinerary_days],
            is_feasible=True,
            feasibility_note=evaluation.explanation,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.itinerary_repo.save_itinerary(db_record)

        return generated

    def _generate_legacy_places_itinerary(
        self,
        trip_id: UUID,
        destination: str,
        evaluation: BudgetEvaluationResult,
        days_count: int,
        transport: FlightOption | TransitOption | None,
        hotel: HotelOption | None,
        places: list[PlaceOption] | None,
    ) -> GeneratedItinerary:
        """Legacy helper for tests passing explicit PlaceOption lists."""
        breakdown = evaluation.breakdown
        daily_food_budget = round(breakdown.food_cost / Decimal(days_count), 2)
        daily_transit_budget = round(breakdown.local_transit_cost / Decimal(days_count), 2)
        daily_activities_budget = round(breakdown.bucket_c_activities / Decimal(days_count), 2)

        available_places = [p for p in (places or []) if not getattr(p, "is_fallback", False)]
        place_idx = 0

        hotel_name = hotel.name if hotel else "Hotel / Guesthouse"
        hotel_night_cost = hotel.price_per_night if (hotel and hotel.price_per_night) else (
            round(breakdown.hotel_cost / Decimal(days_count), 2)
        )
        hotel_source = hotel.source if hotel else DataSource.ESTIMATED

        itinerary_days: list[ItineraryDay] = []

        for d in range(1, days_count + 1):
            day_items: list[ItineraryItem] = []

            if d == 1:
                trans_name = getattr(transport, "airline", None) or getattr(transport, "name_or_operator", "Onward Transport")
                trans_cost = transport.price if transport else Decimal("0.00")
                trans_src = transport.source if transport else DataSource.ESTIMATED
                day_items.append(
                    ItineraryItem(
                        time_slot="Morning",
                        activity=f"Onward travel to {destination} via {trans_name}",
                        place_name=destination,
                        category="transport",
                        planned_cost=trans_cost,
                        source=trans_src,
                        notes=f"Arrival in {destination}",
                    )
                )
                day_items.append(
                    ItineraryItem(
                        time_slot="Afternoon",
                        activity=f"Check-in at {hotel_name} and welcome lunch",
                        place_name=hotel_name,
                        category="accommodation",
                        planned_cost=hotel_night_cost,
                        source=hotel_source,
                    )
                )
                first_place = available_places[place_idx] if place_idx < len(available_places) else None
                if first_place:
                    place_idx += 1
                    day_items.append(
                        ItineraryItem(
                            time_slot="Evening",
                            activity=f"Explore {first_place.name} ({first_place.category or 'sightseeing'})",
                            place_name=first_place.name,
                            category=first_place.category or "attraction",
                            planned_cost=daily_activities_budget,
                            source=first_place.source,
                        )
                    )
                else:
                    day_items.append(
                        ItineraryItem(
                            time_slot="Evening",
                            activity=f"Leisure walk and local dinner in {destination}",
                            place_name=destination,
                            category="food",
                            planned_cost=daily_food_budget,
                            source=DataSource.ESTIMATED,
                        )
                    )

            elif d < days_count:
                morning_place = available_places[place_idx] if place_idx < len(available_places) else None
                if morning_place:
                    place_idx += 1

                day_items.append(
                    ItineraryItem(
                        time_slot="Morning",
                        activity=f"Morning visit to {morning_place.name}" if morning_place else "Morning sightseeing & breakfast",
                        place_name=morning_place.name if morning_place else destination,
                        category=morning_place.category or "attraction" if morning_place else "attraction",
                        planned_cost=daily_transit_budget,
                        source=morning_place.source if morning_place else DataSource.ESTIMATED,
                    )
                )

                afternoon_place = available_places[place_idx] if place_idx < len(available_places) else None
                if afternoon_place:
                    place_idx += 1

                day_items.append(
                    ItineraryItem(
                        time_slot="Afternoon",
                        activity=f"Afternoon exploration at {afternoon_place.name}" if afternoon_place else "Local lunch & exploration",
                        place_name=afternoon_place.name if afternoon_place else hotel_name,
                        category=afternoon_place.category or "food" if afternoon_place else "food",
                        planned_cost=daily_food_budget,
                        source=afternoon_place.source if afternoon_place else DataSource.ESTIMATED,
                    )
                )

                day_items.append(
                    ItineraryItem(
                        time_slot="Evening",
                        activity=f"Relaxed evening dinner near {hotel_name}",
                        place_name=hotel_name,
                        category="accommodation",
                        planned_cost=hotel_night_cost,
                        source=hotel_source,
                    )
                )

            else:
                day_items.append(
                    ItineraryItem(
                        time_slot="Morning",
                        activity="Morning breakfast and souvenir shopping",
                        place_name=destination,
                        category="food",
                        planned_cost=daily_food_budget,
                        source=DataSource.ESTIMATED,
                    )
                )
                day_items.append(
                    ItineraryItem(
                        time_slot="Afternoon",
                        activity=f"Check out from {hotel_name} and transit to departure hub",
                        place_name=hotel_name,
                        category="local_transit",
                        planned_cost=daily_transit_budget,
                        source=DataSource.ESTIMATED,
                    )
                )
                trans_name = getattr(transport, "airline", None) or getattr(transport, "name_or_operator", "Return Transport")
                day_items.append(
                    ItineraryItem(
                        time_slot="Evening",
                        activity=f"Return journey via {trans_name}",
                        place_name=destination,
                        category="transport",
                        planned_cost=Decimal("0.00"),
                        source=transport.source if transport else DataSource.ESTIMATED,
                        notes="Safe travels home!",
                    )
                )

            day_cost = sum(item.planned_cost for item in day_items)
            itinerary_days.append(
                ItineraryDay(
                    day_number=d,
                    theme_or_summary=f"Day {d} in {destination}",
                    items=day_items,
                    daily_estimated_cost=round(day_cost, 2),
                )
            )

        total_itinerary_cost = sum(day.daily_estimated_cost for day in itinerary_days)
        generated = GeneratedItinerary(
            trip_id=trip_id,
            destination=destination,
            days_count=days_count,
            days=itinerary_days,
            is_feasible=True,
            total_budget=breakdown.total_budget,
            total_planned_cost=total_itinerary_cost,
            feasibility_note=evaluation.explanation,
        )

        db_record = ItineraryRecord(
            id=uuid4(),
            trip_id=trip_id,
            days=[day.model_dump(mode="json") for day in itinerary_days],
            is_feasible=True,
            feasibility_note=evaluation.explanation,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.itinerary_repo.save_itinerary(db_record)
        return generated

