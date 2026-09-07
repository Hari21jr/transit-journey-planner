"""Correctness tests for the router, against a feed with known answers."""

from __future__ import annotations

import pytest

from transitrouter.gtfs.loader import format_time, parse_time
from transitrouter.routing.raptor import plan
from transitrouter.routing.timetable import build_timetable
from transitrouter.routing.transfers import haversine, walk_seconds

EIGHT_AM = parse_time("08:00")


# --------------------------------------------------------------------- #
# Time handling

@pytest.mark.parametrize("text,seconds", [
    ("00:00:00", 0), ("08:30:00", 30600), ("23:59:59", 86399),
    ("24:00:00", 86400), ("25:10:00", 90600), ("8:05", 29100),
])
def test_parse_time(text, seconds):
    assert parse_time(text) == seconds


def test_times_past_midnight_survive_a_round_trip():
    """A 25:10 trip is the 1:10am service of the previous service day.

    Normalising it to 01:10 would make it sort before the evening trips and
    silently corrupt every overnight journey.
    """
    assert format_time(parse_time("25:10:00")) == "25:10:00"


def test_blank_time_is_marked_missing():
    assert parse_time("") == -1
    assert format_time(-1) == "--:--"


# --------------------------------------------------------------------- #
# Loading

def test_feed_loads_expected_shape(feed):
    assert len(feed.stops) == 7
    assert len(feed.routes) == 3
    # 6 route-1 + 6 route-2 + 1 route-3 + 1 weekend
    assert len(feed.trips) == 14
    assert feed.stops["C"].name == "Charlie Hub"


def test_service_days_parsed(feed):
    assert feed.service_days["WEEKDAY"] == {0, 1, 2, 3, 4}
    assert feed.service_days["WEEKEND"] == {5, 6}


def test_weekday_filter_excludes_weekend_trips(feed):
    monday = build_timetable(feed, weekday=0)
    saturday = build_timetable(feed, weekday=5)
    assert sum(len(r) for r in monday.routes) == 13
    assert sum(len(r) for r in saturday.routes) == 1


def test_trips_are_regrouped_by_stop_sequence(feed):
    """Route 1 and route 3 both start at A but serve different stops.

    They must never end up in the same routing pattern, or the router will
    board a route-3 vehicle expecting it to stop at B.
    """
    tt = build_timetable(feed, weekday=0)
    patterns = {r.stops for r in tt.routes}
    assert ("A", "B", "C") in patterns
    assert ("A", "E") in patterns
    assert ("C", "D", "E") in patterns


def test_trips_within_a_pattern_are_sorted_by_departure(feed):
    tt = build_timetable(feed, weekday=0)
    for route in tt.routes:
        firsts = [d[0] for d in route.departures]
        assert firsts == sorted(firsts)


# --------------------------------------------------------------------- #
# Transfers

def test_haversine_matches_known_distance():
    # Roughly 40 m between the two Charlie stops.
    d = haversine(45.4300, -75.6800, 45.4302, -75.6805)
    assert 30 < d < 60


def test_walk_time_has_a_floor():
    """Even a zero-metre transfer takes time — you still have to get off."""
    assert walk_seconds(0) == 60
    assert walk_seconds(400) > 60


def test_nearby_stops_get_footpaths(timetable):
    assert any(dest == "C2" for dest, _ in timetable.transfers.get("C", []))


def test_distant_stops_get_no_footpath(timetable):
    assert not any(dest == "Z" for dest, _ in timetable.transfers.get("A", []))


# --------------------------------------------------------------------- #
# Routing — the answers here are worked out by hand from conftest.py

def test_direct_journey_no_transfer(timetable):
    result = plan(timetable, "A", "C", EIGHT_AM)
    assert result.journeys
    best = result.best
    assert best.transfers == 0
    assert best.arrive == parse_time("08:12")   # A 08:00 -> C 08:12
    assert [leg.kind for leg in best.legs] == ["ride"]


def test_journey_requiring_a_transfer(timetable):
    """A→E via C: route 1 at 08:00 arrives C 08:12, route 2 departs 08:13."""
    result = plan(timetable, "A", "E", EIGHT_AM)
    assert result.journeys
    fastest = result.best
    assert fastest.arrive == parse_time("08:27")   # C 08:13 -> E 08:27
    assert fastest.transfers == 1


def test_pareto_returns_the_slower_direct_option_too(timetable):
    """Route 3 is slower but needs no transfer. Both answers are valid."""
    result = plan(timetable, "A", "E", EIGHT_AM)
    transfer_counts = {j.transfers for j in result.journeys}
    assert 1 in transfer_counts, "should find the fast one-transfer journey"
    assert 0 in transfer_counts, "should also find the slow direct journey"

    direct = next(j for j in result.journeys if j.transfers == 0)
    assert direct.arrive == parse_time("08:55")
    assert direct.arrive > result.best.arrive


def test_journeys_are_pareto_optimal(timetable):
    """No returned journey may be beaten on both arrival time and transfers."""
    result = plan(timetable, "A", "E", EIGHT_AM)
    for a in result.journeys:
        for b in result.journeys:
            if a is b:
                continue
            assert not (b.arrive <= a.arrive and b.transfers < a.transfers), (
                "a dominated journey survived the Pareto filter"
            )


def test_later_departure_gives_a_later_trip(timetable):
    """Leaving at 08:05 should catch the 08:10, not time-travel to the 08:00."""
    result = plan(timetable, "A", "C", parse_time("08:05"))
    assert result.best.depart >= parse_time("08:05")
    assert result.best.arrive == parse_time("08:22")


def test_unreachable_stop_returns_nothing(timetable):
    result = plan(timetable, "A", "Z", EIGHT_AM)
    assert result.journeys == []


def test_no_journey_after_the_last_departure(timetable):
    result = plan(timetable, "A", "C", parse_time("23:00"))
    assert result.journeys == []


def test_walking_transfer_is_used_when_it_helps(timetable):
    """C2 is only reachable on foot from C, so any journey there must walk."""
    result = plan(timetable, "A", "C2", EIGHT_AM)
    assert result.journeys
    assert any(leg.kind == "walk" for leg in result.best.legs)


def test_transfer_limit_is_respected(timetable):
    """max_rounds=1 means one vehicle, so the transfer journey is excluded."""
    result = plan(timetable, "A", "E", EIGHT_AM, max_rounds=1)
    assert all(j.transfers == 0 for j in result.journeys)


def test_legs_are_contiguous_and_ordered(timetable):
    """Each leg must start where the previous ended, and not before it."""
    result = plan(timetable, "A", "E", EIGHT_AM)
    for journey in result.journeys:
        for prev, nxt in zip(journey.legs, journey.legs[1:], strict=False):
            assert prev.to_stop == nxt.from_stop
            assert nxt.depart >= prev.arrive


def test_journey_starts_at_origin_and_ends_at_destination(timetable):
    result = plan(timetable, "A", "E", EIGHT_AM)
    for journey in result.journeys:
        assert journey.legs[0].from_stop == "A"
        assert journey.legs[-1].to_stop == "E"


def test_summary_and_description_render(timetable, feed):
    journey = plan(timetable, "A", "E", EIGHT_AM).best
    assert "min" in journey.summary()
    lines = journey.describe(feed)
    assert any("Echo Terminal" in line for line in lines)


def test_duration_is_measured_from_when_you_asked(timetable):
    """A ride you must wait 30 minutes for is not faster than one you catch now.

    Reporting in-vehicle time alone makes the waiting option look better than
    the one that actually gets you there first.
    """
    result = plan(timetable, "A", "E", EIGHT_AM)
    direct = next(j for j in result.journeys if j.transfers == 0)

    # Route 3 leaves A at 08:00 and takes 55 minutes; the transfer journey
    # arrives at 08:27. Door-to-door must reflect the real arrival.
    assert direct.door_to_door(EIGHT_AM) == direct.arrive - EIGHT_AM
    assert result.best.door_to_door(EIGHT_AM) < direct.door_to_door(EIGHT_AM)


def test_wait_is_reported_in_the_summary(timetable):
    """Departing at 08:05 means waiting for the 08:10, and saying so."""
    result = plan(timetable, "A", "C", parse_time("08:05"))
    journey = result.best
    assert journey.wait_before(parse_time("08:05")) == 5 * 60
    assert "5 min wait" in journey.summary(parse_time("08:05"))


def test_no_wait_is_not_mentioned(timetable):
    journey = plan(timetable, "A", "C", EIGHT_AM).best
    assert "wait" not in journey.summary(EIGHT_AM)


def test_walking_time_allows_for_following_streets():
    """Straight-line distance underestimates a walk: you cross at corners.
    A 400m hop is not a 5-minute walk at 4.8km/h, it is closer to 6½."""
    from transitrouter.routing.transfers import WALK_SPEED_MS, walk_seconds

    straight_line = 400 / WALK_SPEED_MS
    assert walk_seconds(400) > straight_line
    assert 350 < walk_seconds(400) < 420


def test_a_very_short_hop_still_costs_the_transfer_floor():
    """Two stops on opposite kerbs are 20m apart; getting off one bus and
    onto another is never instant, whatever the distance says."""
    from transitrouter.routing.transfers import MIN_TRANSFER_SECONDS, walk_seconds

    assert walk_seconds(5) == MIN_TRANSFER_SECONDS


# --- how stop times are stored ----------------------------------------- #

def test_stop_ids_are_shared_not_copied(sample_feed_dir):
    """A feed row parses a fresh string per stop-time. Keeping those means
    one string object per row instead of one per stop — on Ottawa's feed,
    2.7 million objects instead of 5,800, and a few hundred MB."""
    from transitrouter.gtfs.loader import load_feed

    feed = load_feed(str(sample_feed_dir))
    for trip in feed.trips.values():
        for stop_id in trip.stop_ids:
            assert stop_id is feed.stops[stop_id].id


def test_times_are_stored_in_a_typed_array(sample_feed_dir):
    """A list of ints costs 36 bytes an entry; a signed 32-bit array costs
    four. That ratio is what decides whether a real feed fits in memory."""
    from array import array

    from transitrouter.gtfs.loader import load_feed

    feed = load_feed(str(sample_feed_dir))
    trip = next(iter(feed.trips.values()))
    assert isinstance(trip.arrivals, array)
    assert trip.arrivals.typecode == "i"
    assert isinstance(trip.departures, array)


def test_stop_times_out_of_sequence_are_reordered(tmp_path):
    """GTFS does not promise stop_times.txt is sorted. Rows are appended in
    file order for speed, so the out-of-order case has to be caught."""
    import csv

    from transitrouter.gtfs.loader import load_feed

    def write(name, header, rows):
        with (tmp_path / name).open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(rows)

    write("stops.txt", ["stop_id", "stop_name", "stop_lat", "stop_lon"],
          [["A", "Alpha", 45.42, -75.70], ["B", "Bravo", 45.43, -75.69],
           ["C", "Charlie", 45.44, -75.68]])
    write("routes.txt", ["route_id", "route_short_name", "route_type"],
          [["R1", "1", "3"]])
    write("trips.txt", ["route_id", "service_id", "trip_id"], [["R1", "S1", "T1"]])
    write("calendar.txt",
          ["service_id", "monday", "tuesday", "wednesday", "thursday",
           "friday", "saturday", "sunday", "start_date", "end_date"],
          [["S1", 1, 1, 1, 1, 1, 1, 1, "20260101", "20271231"]])
    # Deliberately shuffled: sequence 3, then 1, then 2.
    write("stop_times.txt",
          ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
          [["T1", "08:20:00", "08:20:00", "C", 3],
           ["T1", "08:00:00", "08:00:00", "A", 1],
           ["T1", "08:10:00", "08:10:00", "B", 2]])

    trip = load_feed(str(tmp_path)).trips["T1"]
    assert trip.stop_ids == ["A", "B", "C"]
    assert list(trip.arrivals) == [28800, 29400, 30000]
    assert list(trip.departures) == [28800, 29400, 30000]
