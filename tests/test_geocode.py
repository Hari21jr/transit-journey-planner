"""Tests for address lookup and the walk from a point to the network."""

from __future__ import annotations

import pytest

pytest.importorskip("flask", reason="web extra not installed")

from transitrouter.gtfs.loader import load_feed  # noqa: E402
from transitrouter.web.geocode import (  # noqa: E402
    Geocoder,
    StopIndex,
    _feed_bounds,
    _short_name,
)


@pytest.fixture
def feed(sample_feed_dir):
    return load_feed(str(sample_feed_dir))


def test_a_display_name_is_cut_down_to_what_a_person_recognises():
    """Nominatim returns the whole postal hierarchy down to the country."""
    full = ("47 Huntcliff Place, Barrhaven, Nepean, Ottawa, "
            "Eastern Ontario, Ontario, K2J 3Y6, Canada")
    assert _short_name(full) == "47 Huntcliff Place, Barrhaven"


def test_a_display_name_with_one_part_survives():
    assert _short_name("Hurdman") == "Hurdman"


def test_the_search_area_is_the_feed_plus_a_margin(feed):
    """Unbounded, "Bank Street" in an Ottawa feed resolves to London and the
    planner then reports, correctly and uselessly, that no bus goes there."""
    south, west, north, east = _feed_bounds(feed)
    assert south < 45.42 < north
    assert west < -75.70 < east
    # Padded, so an address just outside the last stop still resolves.
    assert north - south > 0.2


def test_bounds_are_none_for_a_feed_with_no_positions(feed):
    feed.stops.clear()
    assert _feed_bounds(feed) is None


def test_walkable_stops_come_back_with_their_walking_times(feed):
    index = StopIndex(feed)
    # A point a few metres from Charlie Hub.
    found = index.walkable_from(45.4300, -75.6801, radius_m=800)
    assert "C" in found
    assert all(seconds >= 60 for seconds in found.values()), \
        "a 60s floor stops the router proposing impossible dashes"


def test_nothing_is_walkable_from_the_middle_of_nowhere(feed):
    index = StopIndex(feed)
    assert index.walkable_from(46.9, -74.0, radius_m=800) == {}


def test_the_walk_list_is_capped(feed):
    index = StopIndex(feed)
    found = index.walkable_from(45.4300, -75.6801, radius_m=100_000, limit=2)
    assert len(found) == 2


def test_stations_are_not_offered_as_boarding_points(tmp_path, feed):
    """A parent station is a label, not a place a bus stops."""
    index = StopIndex(feed)
    for stop_id in index.walkable_from(45.4300, -75.6801, radius_m=5_000):
        assert feed.stops[stop_id].is_boardable


def test_a_geocoder_lookup_is_cached(feed, monkeypatch):
    """The same address gets retyped while trying different times."""
    geocoder = Geocoder(feed)
    calls: list[str] = []

    def fake(query):
        calls.append(query)
        return None

    monkeypatch.setattr(geocoder, "_request", fake)
    geocoder.lookup("47 Huntcliff Place")
    geocoder.lookup("47  huntcliff  place")   # same address, typed differently
    assert calls == ["47 Huntcliff Place"]


def test_a_blank_query_never_reaches_the_network(feed, monkeypatch):
    geocoder = Geocoder(feed)
    monkeypatch.setattr(geocoder, "_request",
                        lambda q: pytest.fail("should not be called"))
    assert geocoder.lookup("   ") is None
