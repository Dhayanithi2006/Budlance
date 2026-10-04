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
        # they are authoritative and MUST be used before falling back to the curated static catalog.
        has_real_live_places = bool(places and any(not getattr(p, "is_fallback", False) for p in places))
        if has_real_live_places:
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

        if available_attractions:
            # =================================================================
            # MODE A: Curated data exists (e.g. Gujarat)
            # =================================================================
            pool = list(available_attractions)
            for d in range(1, days_count + 1):
                day_items: list[ItineraryItem] = []

                # Morning slot: real attraction 1
                if pool:
                    attr1 = pool.pop(0)
                    day_items.append(
                        ItineraryItem(
                            time_slot="Morning",
                            activity=f"Visit {attr1.name}",
                            place_name=attr1.name,
                            category=attr1.category,
                            planned_cost=Decimal(str(attr1.entry_fee_inr)),
                            source=DataSource.ESTIMATED,
                            is_curated=True,
                            attraction_name=attr1.name,
                            opening_hours=attr1.opening_hours,
                            entry_fee_inr=attr1.entry_fee_inr,
                            description=attr1.description,
                            slot_type="attraction",
                            suggestion=f"Best visited in the {attr1.best_time_of_day}. Duration: ~{attr1.typical_time_hours}h.",
                        )
                    )
                else:
                    day_items.append(
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
                            description=f"Morning at leisure in {destination}.",
                            slot_type="free_time",
                            suggestion="Explore local cafes, markets, and neighborhoods at your own pace",
                        )
                    )

                # Afternoon slot: real attraction 2
                if pool:
                    attr2 = pool.pop(0)
                    day_items.append(
                        ItineraryItem(
                            time_slot="Afternoon",
                            activity=f"Explore {attr2.name}",
                            place_name=attr2.name,
                            category=attr2.category,
                            planned_cost=Decimal(str(attr2.entry_fee_inr)),
                            source=DataSource.ESTIMATED,
                            is_curated=True,
                            attraction_name=attr2.name,
                            opening_hours=attr2.opening_hours,
                            entry_fee_inr=attr2.entry_fee_inr,
                            description=attr2.description,
                            slot_type="attraction",
                            suggestion=f"Best visited in the {attr2.best_time_of_day}. Duration: ~{attr2.typical_time_hours}h.",
                        )
                    )
                else:
                    day_items.append(
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
                        )
                    )

                # Evening slot: evening attraction if available, else dining/relaxation
                evening_idx = next(
                    (i for i, a in enumerate(pool) if a.best_time_of_day.lower() == "evening"),
                    None,
                )
                if evening_idx is not None:
                    attr_eve = pool.pop(evening_idx)
                    day_items.append(
                        ItineraryItem(
                            time_slot="Evening",
                            activity=f"Evening visit to {attr_eve.name}",
                            place_name=attr_eve.name,
                            category=attr_eve.category,
                            planned_cost=Decimal(str(attr_eve.entry_fee_inr)),
                            source=DataSource.ESTIMATED,
                            is_curated=True,
                            attraction_name=attr_eve.name,
                            opening_hours=attr_eve.opening_hours,
                            entry_fee_inr=attr_eve.entry_fee_inr,
                            description=attr_eve.description,
                            slot_type="attraction",
                            suggestion=f"Best visited in the evening. Duration: ~{attr_eve.typical_time_hours}h.",
                        )
                    )
                else:
                    day_items.append(
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
                            suggestion="Sample regional culinary specialties and unwind",
                        )
                    )

                # Day summary
                curated_names = [it.attraction_name for it in day_items if it.is_curated and it.attraction_name]
                summary = f"Day {d}: " + " & ".join(curated_names) if curated_names else f"Day {d}: Self-guided exploration of {destination}"
                day_cost = sum(item.planned_cost for item in day_items)

                itinerary_days.append(
                    ItineraryDay(
                        day_number=d,
                        theme_or_summary=summary,
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
                itinerary_days.append(
                    ItineraryDay(
                        day_number=d,
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
            feasibility_note=evaluation.explanation,
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

