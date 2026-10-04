"""Tests for AttractionSelector component."""

import pytest
from budlance.attractions.models import Attraction
from budlance.attractions.selector import AttractionSelector


@pytest.fixture
def selector():
    return AttractionSelector()


def test_loads_gujarat_attractions(selector):
    """Loads attractions for 'Gujarat' and alias 'Ahmedabad'."""
    attractions_gujarat = selector.select_for_itinerary("gujarat", travel_party=None, days=5)
    assert len(attractions_gujarat) == 9
    assert all(isinstance(a, Attraction) for a in attractions_gujarat)

    attractions_ahmedabad = selector.select_for_itinerary("Ahmedabad", travel_party=None, days=5)
    assert len(attractions_ahmedabad) == 9


def test_filters_by_party_family_excludes_calico(selector):
    """'family' party filters out Calico Museum of Textiles."""
    selected = selector.select_for_itinerary(
        "gujarat",
        travel_party="family",
        days=5,
    )
    names = [a.name for a in selected]
    assert "Calico Museum of Textiles" not in names
    # Verify included attractions are all suitable for family
    for a in selected:
        assert "family" in a.suitable_for


def test_none_travel_party_retains_all_attractions(selector):
    """None travel_party keeps all candidates without filtering."""
    selected = selector.select_for_itinerary(
        "gujarat",
        travel_party=None,
        days=5,
    )
    names = [a.name for a in selected]
    assert "Calico Museum of Textiles" in names
    assert len(selected) == 9


def test_unknown_destination_returns_empty_list_without_raising(selector):
    """Unknown or unconfigured destination safely returns [] without crashing."""
    selected = selector.select_for_itinerary(
        "UnknownCity999",
        travel_party="couple",
        days=3,
    )
    assert selected == []


def test_scores_interest_matches_higher(selector):
    """Attractions matching user interests receive higher rank."""
    # Interest in "architecture"
    selected = selector.select_for_itinerary(
        "gujarat",
        travel_party=None,
        interests=["architecture"],
        days=1,  # limit to 2 attractions
    )
    assert len(selected) == 2
    # At least one architectural attraction should be selected in top 2
    categories = [a.category for a in selected]
    assert "architecture" in categories


def test_limits_count_to_days_times_two(selector):
    """Limits number of attractions to days * 2."""
    selected_1_day = selector.select_for_itinerary("gujarat", travel_party=None, days=1)
    assert len(selected_1_day) == 2

    selected_2_days = selector.select_for_itinerary("gujarat", travel_party=None, days=2)
    assert len(selected_2_days) == 4

    selected_3_days = selector.select_for_itinerary("gujarat", travel_party=None, days=3)
    assert len(selected_3_days) == 6


def test_preserves_time_of_day_diversity(selector):
    """Selected attractions provide morning, afternoon, and evening balance."""
    selected = selector.select_for_itinerary("gujarat", travel_party=None, days=2)
    times = {a.best_time_of_day for a in selected}
    # With 4 attractions in Gujarat, we should have multiple times represented
    assert "morning" in times
    assert "afternoon" in times
    assert "evening" in times
