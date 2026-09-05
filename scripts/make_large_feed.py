"""Generate a synthetic GTFS feed at the scale of a real city.

The router needs to be shown working on something the size of an actual
agency, not just the seven-stop test network. This builds a grid city with
a configurable number of stops, routes and trips so load time and query
time can be measured without depending on any particular agency's feed.

    python scripts/make_large_feed.py out/ --stops 6000 --routes 220

Defaults produce roughly OC Transpo's shape: ~6,000 stops, ~220 routes,
~50,000 trips, ~1.5M stop times.
"""

from __future__ import annotations

import argparse
import csv
import math
import random
from pathlib import Path

# Ottawa-ish bounding box, so distances and walking transfers are realistic.
LAT0, LAT1 = 45.25, 45.52
LON0, LON1 = -75.92, -75.55


def _write(directory: Path, name: str, header: list[str], rows) -> int:
    count = 0
    with (directory / name).open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def generate(
    out: Path,
    n_stops: int = 6000,
    n_routes: int = 220,
    trips_per_route: int = 220,
    seed: int = 7,
) -> dict[str, int]:
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)

    # ---- stops on a jittered grid ------------------------------------- #
    side = int(math.sqrt(n_stops)) + 1
    stops: list[tuple[str, float, float]] = []
    for i in range(n_stops):
        gy, gx = divmod(i, side)
        lat = LAT0 + (LAT1 - LAT0) * (gy / side) + rng.uniform(-0.001, 0.001)
        lon = LON0 + (LON1 - LON0) * (gx / side) + rng.uniform(-0.001, 0.001)
        stops.append((f"S{i}", lat, lon))

    _write(out, "stops.txt", ["stop_id", "stop_name", "stop_lat", "stop_lon"],
           ((sid, f"Stop {sid[1:]}", f"{lat:.6f}", f"{lon:.6f}")
            for sid, lat, lon in stops))

    _write(out, "routes.txt",
           ["route_id", "route_short_name", "route_long_name", "route_type"],
           ((f"R{r}", str(r), f"Route {r}", 3) for r in range(n_routes)))

    _write(out, "calendar.txt",
           ["service_id", "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday", "start_date", "end_date"],
           [("WEEKDAY", 1, 1, 1, 1, 1, 0, 0, "20260101", "20271231")])

    # ---- route patterns ----------------------------------------------- #
    # Routes run along grid rows and columns rather than wandering randomly.
    # Random walks across a 6,000-stop grid almost never intersect, which
    # produces a network where most origin/destination pairs are genuinely
    # unreachable — that measures the generator, not the router. A grid of
    # crossing lines means any pair is reachable within a couple of changes,
    # which is also how real bus networks are laid out.
    patterns: list[list[int]] = []
    for r in range(n_routes):
        length = rng.randint(15, 40)
        if r % 2 == 0:
            # West-east along one row, from a random column.
            row = rng.randrange(side)
            col = rng.randrange(max(1, side - length))
            indices = [row * side + (col + i) for i in range(length)]
        else:
            # North-south along one column.
            col = rng.randrange(side)
            row = rng.randrange(max(1, side - length))
            indices = [(row + i) * side + col for i in range(length)]
        pattern = [i for i in indices if 0 <= i < n_stops]
        if len(pattern) >= 2:
            patterns.append(pattern)

    def trip_rows():
        for r, pattern in enumerate(patterns):
            # Service from 05:00 to 01:00 next day.
            headway = max(4, int(1200 / max(1, trips_per_route / 20)))
            for t in range(trips_per_route):
                start_s = 5 * 3600 + t * headway * 60 // 4
                tid = f"R{r}-{t}"
                yield tid, f"R{r}", "WEEKDAY", f"Stop {pattern[-1]}"
                # stop times emitted separately below via the same seed order
                _stop_time_buffer.append((tid, pattern, start_s))

    _stop_time_buffer: list[tuple[str, list[int], int]] = []
    n_trips = _write(out, "trips.txt",
                     ["trip_id", "route_id", "service_id", "trip_headsign"],
                     trip_rows())

    def stop_time_rows():
        for tid, pattern, start_s in _stop_time_buffer:
            t = start_s
            for seq, stop_index in enumerate(pattern):
                h, rem = divmod(t, 3600)
                m, s = divmod(rem, 60)
                clock = f"{h:02d}:{m:02d}:{s:02d}"
                yield tid, clock, clock, f"S{stop_index}", seq
                t += rng.randint(60, 180)

    n_stop_times = _write(
        out, "stop_times.txt",
        ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
        stop_time_rows())

    return {"stops": n_stops, "routes": n_routes,
            "trips": n_trips, "stop_times": n_stop_times}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("out", type=Path)
    p.add_argument("--stops", type=int, default=6000)
    p.add_argument("--routes", type=int, default=220)
    p.add_argument("--trips-per-route", type=int, default=220)
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args()

    stats = generate(args.out, args.stops, args.routes,
                     args.trips_per_route, args.seed)
    print(f"wrote {args.out}: " + ", ".join(f"{v:,} {k}" for k, v in stats.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
