"""Static fallback data provider for train/bus corridors and rate tables."""

from decimal import Decimal
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FALLBACK_DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "fallback"


class FallbackDataProvider:
    """Manages reading and matching static fallback JSON datasets."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or FALLBACK_DATA_DIR

    def _load_json(self, filename: str) -> dict[str, Any]:
        file_path = self.data_dir / filename
        if not file_path.exists():
            logger.warning("Fallback file not found: %s", file_path)
            return {}
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.error("Failed to read fallback file %s: %s", file_path, exc)
            return {}

    def get_train_corridor(self, origin: str, destination: str) -> dict[str, Any] | None:
        """Find matching static train corridor between two cities, preferring exact direction."""
        data = self._load_json("train_corridors.json")
        orig_clean = origin.strip().lower()
        dest_clean = destination.strip().lower()
        corridors = data.get("corridors", [])

        # Pass 1: exact directional match
        for c in corridors:
            c_orig = c.get("origin", "").lower()
            c_dest = c.get("destination", "").lower()
            if c_orig == orig_clean and c_dest == dest_clean:
                return {**c, "is_fallback": True}

        # Pass 2: reverse route fallback
        for c in corridors:
            c_orig = c.get("origin", "").lower()
            c_dest = c.get("destination", "").lower()
            if c_orig == dest_clean and c_dest == orig_clean:
                return {**c, "is_fallback": True}
        return None

    def get_bus_corridor(self, origin: str, destination: str) -> dict[str, Any] | None:
        """Find matching static bus corridor between two cities, preferring exact direction."""
        data = self._load_json("bus_corridors.json")
        orig_clean = origin.strip().lower()
        dest_clean = destination.strip().lower()
        corridors = data.get("corridors", [])

        # Pass 1: exact directional match
        for c in corridors:
            c_orig = c.get("origin", "").lower()
            c_dest = c.get("destination", "").lower()
            if c_orig == orig_clean and c_dest == dest_clean:
                return {**c, "is_fallback": True}

        # Pass 2: reverse route fallback
        for c in corridors:
            c_orig = c.get("origin", "").lower()
            c_dest = c.get("destination", "").lower()
            if c_orig == dest_clean and c_dest == orig_clean:
                return {**c, "is_fallback": True}
        return None

    def get_rate_tables(self) -> dict[str, Any]:
        """Fetch local cost estimation rate tables."""
        return self._load_json("rate_tables.json")

    def get_offline_train_fare_estimate(self, travel_class: str | None = None) -> Decimal:
        """Fetch offline train fare estimate for unmapped routes from rate tables.

        Classified strictly as OFFLINE / FALLBACK estimation.
        """
        rates = self.get_rate_tables().get("offline_train_fare_estimates_inr", {})
        cls_clean = (travel_class or "").lower().replace("-", "").replace(" ", "")
        if cls_clean in rates:
            return Decimal(str(rates[cls_clean]))
        default_val = rates.get("default", 1500)
        return Decimal(str(default_val))

    def get_offline_lodging_estimate(self, tier: str = "standard") -> Decimal:
        """Fetch offline lodging estimate per room per night from rate tables.

        Classified strictly as OFFLINE / FALLBACK planning rate.
        """
        rates = self.get_rate_tables().get("offline_lodging_estimates_per_night_inr", {})
        norm = (tier or "standard").lower()
        if norm in rates:
            return Decimal(str(rates[norm]))
        default_val = rates.get("standard", 2000)
        return Decimal(str(default_val))

