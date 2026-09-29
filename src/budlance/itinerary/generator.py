"""Itinerary Generator constructing day-by-day travel schedules from FEASIBLE budget results."""

import logging
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

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

    def __init__(self, itinerary_repo: ItineraryRepository | None = None) -> None:
        self.itinerary_repo = itinerary_repo or ItineraryRepository()

    def generate(
        self,
        trip_id: UUID,
        destination: str,
        evaluation: BudgetEvaluationResult,
        days: int,
        transport: FlightOption | TransitOption | None,
        hotel: HotelOption | None,
        places: list[PlaceOption] | None = None,
        route: RouteOption | None = None,
    ) -> GeneratedItinerary:
        """Generate a complete day-by-day itinerary if the evaluation is FEASIBLE."""
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
        daily_activities_budget = round(breakdown.bucket_c_activities / Decimal(days_count), 2)

        available_places = list(places) if places else []
        place_idx = 0

        hotel_name = hotel.name if hotel else "Hotel / Guesthouse"
        hotel_night_cost = hotel.price_per_night if (hotel and hotel.price_per_night) else (
            round(breakdown.hotel_cost / Decimal(days_count), 2)
        )
        hotel_source = hotel.source if hotel else DataSource.ESTIMATED

        itinerary_days: list[ItineraryDay] = []

        for d in range(1, days_count + 1):
            day_items: list[ItineraryItem] = []

            # =================================================================
            # Day 1: Onward Journey, Check-in, Evening Place
            # =================================================================
            if d == 1:
                # Morning: Onward journey
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

                # Afternoon: Hotel check-in and lunch
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

                # Evening: First attraction / beach
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

            # =================================================================
            # Intermediate Days: Sightseeing, Culture, Meals
            # =================================================================
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

            # =================================================================
            # Final Day: Check-out and Return Journey
            # =================================================================
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
                        planned_cost=Decimal("0.00"),  # Already accounted in total transport cost
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

        # 2. Persist in database repository
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
