"""A hand-built GTFS feed with known correct answers.

Testing a router against a real feed proves nothing: you cannot assert on an
itinerary you had to trust the router to compute. This feed is small enough
that every optimal journey can be worked out on paper, which is what makes
the assertions meaningful.

Network (times in minutes past 08:00):

    A ──route 1──> B ──route 1──> C          departs A every 10 min
    C ──route 2──> D ──route 2──> E          departs C every 10 min
    A ─────────route 3─────────────> E       departs A hourly, slow
    C  ~40m walk~  C2                        C2 is served by route 2 as well

So A→E has two sensible answers: change at C (faster, one transfer) or ride
route 3 all the way (slower, no transfer). A correct Pareto router returns
both.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

STOPS = [
    # id,  name,          lat,      lon
    ("A", "Alpha Station", 45.4200, -75.7000),
    ("B", "Bravo Stop", 45.4250, -75.6900),
    ("C", "Charlie Hub", 45.4300, -75.6800),
    ("C2", "Charlie Hub West", 45.4302, -75.6805),  # ~40m from C
    ("D", "Delta Stop", 45.4350, -75.6700),
    ("E", "Echo Terminal", 45.4400, -75.6600),
    ("Z", "Zulu Isolated", 45.5000, -75.5000),  # unreachable on purpose
]

ROUTES = [
    ("R1", "1", "Alpha to Charlie"),
    ("R2", "2", "Charlie to Echo"),
    ("R3", "3", "Alpha to Echo direct"),
]


def _hhmmss(minutes_past_8: int) -> str:
    total = 8 * 3600 + minutes_past_8 * 60
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _write(path: Path, name: str, header: list[str], rows: list[list]) -> None:
    with (path / name).open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def build_sample_feed(directory: Path) -> Path:
    """Write the network above as a GTFS directory."""
    directory.mkdir(parents=True, exist_ok=True)

    _write(directory, "stops.txt", ["stop_id", "stop_name", "stop_lat", "stop_lon"],
           [[s[0], s[1], s[2], s[3]] for s in STOPS])

    _write(directory, "routes.txt",
           ["route_id", "route_short_name", "route_long_name", "route_type"],
           [[r[0], r[1], r[2], 3] for r in ROUTES])

    _write(directory, "calendar.txt",
           ["service_id", "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday", "start_date", "end_date"],
           [["WEEKDAY", 1, 1, 1, 1, 1, 0, 0, "20260101", "20271231"],
            ["WEEKEND", 0, 0, 0, 0, 0, 1, 1, "20260101", "20271231"]])

    trips: list[list] = []
    stop_times: list[list] = []

    # Route 1: A(+0) -> B(+5) -> C(+12), every 10 minutes for an hour.
    for i in range(6):
        offset = i * 10
        tid = f"R1-{i}"
        trips.append([tid, "R1", "WEEKDAY", "Charlie Hub"])
        for seq, (stop, delta) in enumerate([("A", 0), ("B", 5), ("C", 12)]):
            t = _hhmmss(offset + delta)
            stop_times.append([tid, t, t, stop, seq])

    # Route 2: C(+0) -> D(+6) -> E(+14), every 10 minutes, offset by 3
    # so a change at C actually requires a short wait.
    for i in range(6):
        offset = i * 10 + 3
        tid = f"R2-{i}"
        trips.append([tid, "R2", "WEEKDAY", "Echo Terminal"])
        for seq, (stop, delta) in enumerate([("C", 0), ("D", 6), ("E", 14)]):
            t = _hhmmss(offset + delta)
            stop_times.append([tid, t, t, stop, seq])

    # Route 3: A -> E direct but slow (55 min), once at 08:00.
    trips.append(["R3-0", "R3", "WEEKDAY", "Echo Terminal express"])
    for seq, (stop, delta) in enumerate([("A", 0), ("E", 55)]):
        t = _hhmmss(delta)
        stop_times.append(["R3-0", t, t, stop, seq])

    # A weekend-only trip, so weekday filtering can be tested.
    trips.append(["R1-SAT", "R1", "WEEKEND", "Charlie Hub"])
    for seq, (stop, delta) in enumerate([("A", 0), ("B", 5), ("C", 12)]):
        t = _hhmmss(delta)
        stop_times.append(["R1-SAT", t, t, stop, seq])

    _write(directory, "trips.txt",
           ["trip_id", "route_id", "service_id", "trip_headsign"], trips)
    _write(directory, "stop_times.txt",
           ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
           stop_times)

    return directory


@pytest.fixture
def sample_feed_dir(tmp_path) -> Path:
    return build_sample_feed(tmp_path / "feed")


@pytest.fixture
def feed(sample_feed_dir):
    from transitrouter.gtfs.loader import load_feed
    return load_feed(sample_feed_dir)


@pytest.fixture
def timetable(feed):
    from transitrouter.routing.timetable import build_timetable
    from transitrouter.routing.transfers import build_transfers

    tt = build_timetable(feed, weekday=0)  # Monday
    tt.transfers = build_transfers(feed, max_walk_m=400)
    return tt
