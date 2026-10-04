"""Tests for curated attraction data files and schema compliance."""

import json
from pathlib import Path
import pytest

ATTRACTION_DIR = Path("data/attractions")
VALID_CATEGORIES = {"culture", "architecture", "nature", "relaxation", "family_fun", "heritage"}
VALID_PARTIES = {"solo", "couple", "friends", "family", "relatives"}
VALID_TIMES = {"morning", "afternoon", "evening"}


def test_gujarat_attractions_file_exists():
    """Verify data/attractions/gujarat.json exists."""
    path = ATTRACTION_DIR / "gujarat.json"
    assert path.is_file(), f"Expected {path} to exist"


def test_gujarat_attractions_valid_schema():
    """Verify all attractions in gujarat.json satisfy the required schema."""
    path = ATTRACTION_DIR / "gujarat.json"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert isinstance(data, list)
    assert 8 <= len(data) <= 10, f"Expected 8 to 10 attractions, found {len(data)}"

    names = set()
    for item in data:
        # Field presence
        assert "name" in item and item["name"].strip()
        assert "category" in item
        assert "suitable_for" in item
        assert "typical_time_hours" in item
        assert "opening_hours" in item
        assert "entry_fee_inr" in item
        assert "description" in item and item["description"].strip()
        assert "location" in item and item["location"].strip()
        assert "best_time_of_day" in item

        # No duplicate names
        name = item["name"].strip()
        assert name not in names, f"Duplicate attraction name: {name}"
        names.add(name)

        # Field types and validations
        assert item["category"] in VALID_CATEGORIES, f"Invalid category {item['category']} in {name}"
        assert isinstance(item["suitable_for"], list) and len(item["suitable_for"]) > 0
        for party in item["suitable_for"]:
            assert party in VALID_PARTIES, f"Invalid party '{party}' in {name}"

        assert isinstance(item["typical_time_hours"], (int, float)) and item["typical_time_hours"] > 0
        assert isinstance(item["entry_fee_inr"], int) and item["entry_fee_inr"] >= 0
        assert item["best_time_of_day"] in VALID_TIMES, f"Invalid time of day in {name}"

        # No placeholders
        desc = item["description"].lower()
        assert "placeholder" not in desc
        assert "todo" not in desc
        assert "tbd" not in desc
        assert len(desc) >= 30, f"Description too short for {name}"
