"""Attraction selector component for scoring, filtering, and diversifying curated attractions."""

import json
import logging
from pathlib import Path
from typing import Any

from budlance.attractions.models import Attraction

logger = logging.getLogger(__name__)

# Map common city names to regional/state attraction files
DESTINATION_ALIASES: dict[str, str] = {
    "ahmedabad": "gujarat",
    "gandhinagar": "gujarat",
    "patan": "gujarat",
    "mehsana": "gujarat",
}


class AttractionSelector:
    """Selects, filters, and ranks curated attractions for destination itineraries."""

    def __init__(
        self,
        attractions_dir: Path | str | None = None,
        cache_manager: Any | None = None,
    ) -> None:
        self.cache_manager = cache_manager
        if attractions_dir is not None:
            self.attractions_dir = Path(attractions_dir)
        else:
            # Default to repo_root/data/attractions or relative data/attractions
            repo_root = Path(__file__).resolve().parents[3]
            candidate = repo_root / "data" / "attractions"
            if candidate.is_dir():
                self.attractions_dir = candidate
            else:
                self.attractions_dir = Path("data/attractions")

    def _find_data_file(self, destination: str) -> Path | None:
        """Find the attraction JSON file for a given destination."""
        dest_clean = destination.strip().lower()
        if not dest_clean:
            return None

        # 1. Direct file match: e.g. "gujarat.json"
        direct = self.attractions_dir / f"{dest_clean}.json"
        if direct.is_file():
            return direct

        # 2. Alias match: e.g. "ahmedabad" -> "gujarat.json"
        alias = DESTINATION_ALIASES.get(dest_clean)
        if alias:
            alias_path = self.attractions_dir / f"{alias}.json"
            if alias_path.is_file():
                return alias_path

        return None

    def _load_attractions(self, destination: str) -> list[Attraction]:
        """Load curated attractions for a destination. Returns empty list if not found."""
        file_path = self._find_data_file(destination)
        if file_path:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    raw_list = json.load(f)
                if not isinstance(raw_list, list):
                    logger.warning("Attraction file %s is not a list", file_path)
                    return []
                return [Attraction.model_validate(item) for item in raw_list]
            except Exception as exc:
                logger.warning("Failed to load attractions from %s: %s", file_path, exc)
                return []

        # If no curated file exists, enforce SerpApi credentials guard via CacheFallbackManager
        if self.cache_manager is not None:
            gateway = getattr(self.cache_manager, "gateway", None)
            if gateway and getattr(gateway, "has_credentials", False):
                logger.info(
                    "[ATTRACTIONS] Curated file absent for '%s'. Live SerpApi discovery branch available.",
                    destination,
                )
            else:
                logger.info(
                    "[ATTRACTIONS] Curated file absent for '%s' and SerpApi has_credentials=False; returning empty list (Mode B).",
                    destination,
                )
        else:
            logger.info("No curated attraction data found for destination '%s'", destination)

        return []

    def select_for_itinerary(
        self,
        destination: str,
        travel_party: str | None,
        interests: list[str] | None = None,
        days: int = 1,
    ) -> list[Attraction]:
        """Filter, score, and select up to (days * 2) attractions for an itinerary.

        Rules:
        - Returns [] for unknown destinations without error.
        - Filters by travel_party when specified.
        - Scores interest matches (+2) and party suitability (+1).
        - Balances best_time_of_day diversity (morning, afternoon, evening).
        - Limits output to max(1, days * 2) attractions.
        """
        all_attractions = self._load_attractions(destination)
        if not all_attractions:
            return []

        active_interests = interests or []
        party_clean = travel_party.strip().lower() if travel_party else None

        # 1. Filter by travel_party
        candidates: list[Attraction] = []
        for attr in all_attractions:
            if party_clean is not None:
                suitable_lower = [s.strip().lower() for s in attr.suitable_for]
                if party_clean not in suitable_lower:
                    continue
            candidates.append(attr)

        if not candidates:
            return []

        # 2. Score remaining attractions
        scored: list[tuple[float, Attraction]] = []
        for attr in candidates:
            score = 0.0
            cat_lower = attr.category.strip().lower()

            # +2 points if category matches one of the user's interests (case-insensitive substring)
            for interest in active_interests:
                int_lower = interest.strip().lower()
                if int_lower and (int_lower in cat_lower or cat_lower in int_lower):
                    score += 2.0
                    break

            # +1 point if suitable for party
            if party_clean is not None:
                suitable_lower = [s.strip().lower() for s in attr.suitable_for]
                if party_clean in suitable_lower:
                    score += 1.0

            scored.append((score, attr))

        # Sort candidate pool by score descending
        scored.sort(key=lambda x: x[0], reverse=True)

        # 3. Limit count to days * 2 with best_time_of_day diversity
        max_count = max(1, days * 2)
        return self._select_with_diversity(scored, max_count)

    def _select_with_diversity(
        self,
        scored: list[tuple[float, Attraction]],
        max_count: int,
    ) -> list[Attraction]:
        """Select up to max_count attractions preserving best_time_of_day diversity."""
        if len(scored) <= max_count:
            # If total candidates <= max_count, return all sorted by score
            return [attr for _, attr in scored]

        by_time: dict[str, list[Attraction]] = {
            "morning": [],
            "afternoon": [],
            "evening": [],
        }
        for _, attr in scored:
            tod = attr.best_time_of_day.strip().lower()
            if tod in by_time:
                by_time[tod].append(attr)
            else:
                by_time.setdefault(tod, []).append(attr)

        selected: list[Attraction] = []
        time_cycle = ["morning", "afternoon", "evening"]

        # Round-robin across morning, afternoon, evening to pick highest-scoring candidate
        while len(selected) < max_count:
            picked_any = False
            for tod in time_cycle:
                if by_time.get(tod) and len(selected) < max_count:
                    selected.append(by_time[tod].pop(0))
                    picked_any = True
            if not picked_any:
                break

        return selected
