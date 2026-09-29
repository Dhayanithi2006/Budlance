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

    if not isinstance(data, dict):
        return results

    # Support single corridor object or container with "corridors" array
    corridors = data.get("corridors") if "corridors" in data else [data]
    if not isinstance(corridors, list):
        return results

    is_train = envelope.engine in ("trains", "train_corridors") or "classes" in str(data)

    for c in corridors:
        if not isinstance(c, dict):
            continue

        origin = c.get("origin", "Origin")
        destination = c.get("destination", "Destination")
        distance_km = float(c.get("distance_km", 0.0))
        duration_hours = float(c.get("duration_hours", 0.0))

        if is_train:
            name = c.get("train_name", "Indian Railways Express")
            classes = c.get("classes") or {}
            if isinstance(classes, dict) and classes:
                for cls_name, fare in classes.items():
                    results.append(
                        TransitOption(
                            transit_type="train",
                            origin=origin,
                            destination=destination,
                            name_or_operator=name,
                            distance_km=distance_km,
                            duration_hours=duration_hours,
                            price=Decimal(str(fare)),
                            class_or_type=cls_name,
                            source=DataSource.FALLBACK,
                            is_fallback=True,
                        )
                    )
            else:
                # Default single train fare if classes not broken down
                fare = c.get("fare_inr", 500)
                results.append(
                    TransitOption(
                        transit_type="train",
                        origin=origin,
                        destination=destination,
                        name_or_operator=name,
                        distance_km=distance_km,
                        duration_hours=duration_hours,
                        price=Decimal(str(fare)),
                        class_or_type="SL",
                        source=DataSource.FALLBACK,
                        is_fallback=True,
                    )
                )
        else:
            # Bus corridor
            operator = c.get("bus_type", "Intercity Express Bus")
            fare = c.get("fare_inr", 700)
            results.append(
                TransitOption(
                    transit_type="bus",
                    origin=origin,
                    destination=destination,
                    name_or_operator=operator,
                    distance_km=distance_km,
                    duration_hours=duration_hours,
                    price=Decimal(str(fare)),
                    class_or_type=c.get("bus_type"),
                    source=DataSource.FALLBACK,
                    is_fallback=True,
                )
            )

    return results
