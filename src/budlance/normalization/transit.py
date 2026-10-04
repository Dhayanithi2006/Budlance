"""Transit normalizer converting static train/bus fallback datasets to TransitOption models."""

import logging
from decimal import Decimal
from typing import Any
from budlance.schemas.travel import TransitOption
from budlance.serpapi.models import DataSource, TravelDataEnvelope

logger = logging.getLogger(__name__)


def normalize_transit_fallback(envelope: TravelDataEnvelope) -> list[TransitOption]:
    """Parse static train or bus corridor fallback envelopes into normalized TransitOption models."""
    results: list[TransitOption] = []
    data = envelope.data

    if not isinstance(data, dict) or not data:
        return results

    # Support single corridor object or container with "corridors" array
    corridors = data.get("corridors") if "corridors" in data else [data]
    if not isinstance(corridors, list):
        return results

    is_train = envelope.engine in ("trains", "train_corridors") or "classes" in str(data)

    for c in corridors:
        if not isinstance(c, dict) or not c:
            continue

        origin = c.get("origin")
        destination = c.get("destination")
        if not origin or not destination:
            continue

        distance_km = float(c.get("distance_km", 0.0))
        duration_hours = float(c.get("duration_hours", 0.0))

        if is_train:
            name = c.get("train_name", "Indian Railways Express")
            classes = c.get("classes") or {}
            if isinstance(classes, dict) and classes:
                for cls_name, fare in classes.items():
                    if fare is None:
                        continue
                    try:
                        fare_dec = Decimal(str(fare))
                    except Exception:
                        continue
                    if fare_dec <= Decimal("0.00"):
                        continue
                    results.append(
                        TransitOption(
                            transit_type="train",
                            origin=origin,
                            destination=destination,
                            name_or_operator=name,
                            distance_km=distance_km,
                            duration_hours=duration_hours,
                            price=fare_dec,
                            class_or_type=cls_name,
                            source=DataSource.FALLBACK,
                            is_fallback=True,
                        )
                    )
            else:
                # Require explicit positive fare_inr; do NOT fabricate default fare
                raw_fare = c.get("fare_inr")
                if raw_fare is not None:
                    try:
                        fare_dec = Decimal(str(raw_fare))
                    except Exception:
                        fare_dec = Decimal("0.00")
                    if fare_dec > Decimal("0.00"):
                        results.append(
                            TransitOption(
                                transit_type="train",
                                origin=origin,
                                destination=destination,
                                name_or_operator=name,
                                distance_km=distance_km,
                                duration_hours=duration_hours,
                                price=fare_dec,
                                class_or_type="SL",
                                source=DataSource.FALLBACK,
                                is_fallback=True,
                            )
                        )
        else:
            # Bus corridor: require explicit positive fare_inr; do NOT fabricate default fare
            operator = c.get("bus_type", "Intercity Express Bus")
            raw_fare = c.get("fare_inr")
            if raw_fare is not None:
                try:
                    fare_dec = Decimal(str(raw_fare))
                except Exception:
                    fare_dec = Decimal("0.00")
                if fare_dec > Decimal("0.00"):
                    results.append(
                        TransitOption(
                            transit_type="bus",
                            origin=origin,
                            destination=destination,
                            name_or_operator=operator,
                            distance_km=distance_km,
                            duration_hours=duration_hours,
                            price=fare_dec,
                            class_or_type=c.get("bus_type"),
                            source=DataSource.FALLBACK,
                            is_fallback=True,
                        )
                    )

    return results


def calculate_round_trip_cost(outbound_fare: Decimal, return_fare: Decimal, people: int) -> Decimal:
    """Calculate complete round-trip transport cost for N travelers.

    Formula: (outbound fare + return fare) * number of travelers.
    Uses Decimal arithmetic for exact currency calculation.
    """
    count = Decimal(max(1, people))
    return (outbound_fare + return_fare) * count


def build_round_trip_transit_options(
    outbound_options: list[TransitOption],
    return_options: list[TransitOption],
    people: int,
) -> list[TransitOption]:
    """Combine outbound and return transit options into round-trip TransitOption models.

    For each outbound class/type, matches the corresponding return class/type.
    If matching return class is found:
        round_trip_price = (outbound_fare + return_fare) * people
    If no matching return class is found:
        round_trip_price = (outbound_fare + outbound_fare) * people (assuming identical reverse fare)
    """
    results: list[TransitOption] = []

    # Map return options by normalized class/type for quick matching
    return_by_class: dict[str, TransitOption] = {}
    for ret in return_options:
        cls_key = (ret.class_or_type or "").strip().lower()
        if cls_key and cls_key not in return_by_class:
            return_by_class[cls_key] = ret

    for out in outbound_options:
        cls_key = (out.class_or_type or "").strip().lower()
        ret_opt = return_by_class.get(cls_key)

        outbound_fare = out.price
        return_fare = ret_opt.price if ret_opt is not None else outbound_fare

        if outbound_fare <= Decimal("0.00") or return_fare <= Decimal("0.00"):
            continue

        total_price = calculate_round_trip_cost(outbound_fare, return_fare, people)
        if total_price <= Decimal("0.00"):
            continue

        round_trip_opt = out.model_copy(update={"price": total_price})
        results.append(round_trip_opt)

    return results
