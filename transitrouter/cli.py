"""Command-line journey planner.

    journey info      FEED.zip
    journey stops     FEED.zip --search "rideau"
    journey plan      FEED.zip --from 3009 --to 8767 --at 17:30
    journey benchmark FEED.zip --queries 200
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import time
from datetime import date

from .gtfs.loader import GtfsError, format_time, load_feed, parse_time
from .routing.raptor import plan
from .routing.timetable import build_timetable
from .routing.transfers import build_transfers

DIM, BOLD, GREEN, YELLOW, RESET = (
    "\033[90m", "\033[1m", "\033[92m", "\033[93m", "\033[0m"
)


def _prepare(args, weekday: int | None = None):
    """Load a feed and build the routing structures, with timings."""
    t0 = time.perf_counter()
    feed = load_feed(args.feed, progress=None if args.quiet else _tick)
    t1 = time.perf_counter()
    timetable = build_timetable(feed, weekday=weekday)
    timetable.transfers = build_transfers(feed, max_walk_m=args.max_walk)
    t2 = time.perf_counter()

    if not args.quiet:
        print(f"  loaded {feed.summary()} in {t1 - t0:.2f}s", file=sys.stderr)
        stats = timetable.stats
        print(f"  built {stats['routes']:,} routing patterns, "
              f"{stats['transfers']:,} footpaths in {t2 - t1:.2f}s\n",
              file=sys.stderr)
    return feed, timetable


def _tick(message: str) -> None:
    print(f"  … {message}", end="\r", file=sys.stderr)


def _resolve_stop(feed, token: str) -> str | None:
    """Accept a stop id, or a unique name fragment."""
    if token in feed.stops:
        return token
    lowered = token.lower()
    matches = [s for s in feed.stops.values() if lowered in s.name.lower()]
    if len(matches) == 1:
        return matches[0].id
    if not matches:
        print(f"no stop matches {token!r}", file=sys.stderr)
    else:
        print(f"{token!r} is ambiguous ({len(matches)} matches). "
              f"Use `journey stops` to find the id:", file=sys.stderr)
        for stop in matches[:8]:
            print(f"    {stop.id:<12} {stop.name}", file=sys.stderr)
    return None


# --------------------------------------------------------------------- #

def cmd_info(args) -> int:
    feed, timetable = _prepare(args)
    stats = timetable.stats
    print(f"{BOLD}Feed{RESET}          {args.feed}")
    print(f"stops         {len(feed.stops):,}")
    print(f"gtfs routes   {len(feed.routes):,}")
    print(f"trips         {len(feed.trips):,}")
    print(f"stop times    {sum(len(t.stop_ids) for t in feed.trips.values()):,}")
    print(f"services      {len(feed.service_days):,}")
    print()
    print(f"{BOLD}Routing structures{RESET}")
    print(f"patterns      {stats['routes']:,}   "
          f"{DIM}(trips regrouped by identical stop sequence){RESET}")
    print(f"footpaths     {stats['transfers']:,}   "
          f"{DIM}(walking transfers within {args.max_walk:.0f}m){RESET}")
    return 0


def cmd_stops(args) -> int:
    args.quiet = True
    feed = load_feed(args.feed)
    needle = args.search.lower()
    matches = [s for s in feed.stops.values() if needle in s.name.lower()]
    if not matches:
        print("no matches")
        return 1
    matches.sort(key=lambda s: s.name)
    print(f"{'ID':<14}{'NAME':<44}LAT, LON")
    for stop in matches[: args.limit]:
        print(f"{stop.id:<14}{stop.name[:43]:<44}{stop.lat:.5f}, {stop.lon:.5f}")
    if len(matches) > args.limit:
        print(f"{DIM}… and {len(matches) - args.limit} more{RESET}")
    return 0


def cmd_plan(args) -> int:
    weekday = args.weekday if args.weekday is not None else date.today().weekday()
    feed, timetable = _prepare(args, weekday=weekday)

    origin = _resolve_stop(feed, args.origin)
    destination = _resolve_stop(feed, args.destination)
    if not origin or not destination:
        return 1
    if origin == destination:
        print("origin and destination are the same stop", file=sys.stderr)
        return 1

    depart = parse_time(args.at)
    t0 = time.perf_counter()
    result = plan(timetable, origin, destination, depart, max_rounds=args.max_rounds)
    elapsed = (time.perf_counter() - t0) * 1000

    print(f"{BOLD}{feed.stops[origin].name} → {feed.stops[destination].name}{RESET}")
    print(f"{DIM}departing after {format_time(depart)} · "
          f"solved in {elapsed:.0f}ms · {result.stops_reached:,} stops reached "
          f"in {result.rounds_used} rounds{RESET}\n")

    if not result.journeys:
        print("No journey found. The stops may not be connected on this "
              "service day, or the last vehicle may have gone.")
        return 1

    for i, journey in enumerate(result.journeys):
        tag = f"{GREEN}fastest{RESET}" if i == 0 else f"{YELLOW}fewer changes{RESET}"
        print(f"  [{tag}] {journey.summary()}")
        for line in journey.describe(feed):
            print(f"    {line}")
        print()
    return 0


def cmd_benchmark(args) -> int:
    """Time random queries — the number that goes on a resume."""
    weekday = args.weekday if args.weekday is not None else date.today().weekday()
    feed, timetable = _prepare(args, weekday=weekday)

    routable = [s for s in timetable.stop_routes if timetable.stop_routes[s]]
    if len(routable) < 2:
        print("feed has too few connected stops to benchmark", file=sys.stderr)
        return 1

    rng = random.Random(args.seed)
    depart = parse_time(args.at)
    times: list[float] = []
    solved = 0

    for _ in range(args.queries):
        a, b = rng.sample(routable, 2)
        t0 = time.perf_counter()
        result = plan(timetable, a, b, depart, max_rounds=args.max_rounds)
        times.append((time.perf_counter() - t0) * 1000)
        solved += bool(result.journeys)

    times.sort()
    print(f"{BOLD}{args.queries} random queries{RESET} departing {format_time(depart)}")
    print(f"  solved      {solved}/{args.queries} "
          f"({solved / args.queries:.0%})")
    print(f"  mean        {statistics.mean(times):.1f} ms")
    print(f"  median      {statistics.median(times):.1f} ms")
    print(f"  p95         {times[int(len(times) * 0.95)]:.1f} ms")
    print(f"  slowest     {times[-1]:.1f} ms")
    return 0


# --------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="journey",
        description="Transit journey planner over any GTFS feed",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("feed", help="path to a GTFS .zip or unzipped directory")
        sp.add_argument("--max-walk", type=float, default=400.0,
                        help="metres of walking allowed between stops")
        sp.add_argument("--quiet", action="store_true")
        return sp

    common(sub.add_parser("info", help="feed and routing statistics")
           ).set_defaults(func=cmd_info)

    s = common(sub.add_parser("stops", help="find stop ids by name"))
    s.add_argument("--search", required=True)
    s.add_argument("--limit", type=int, default=25)
    s.set_defaults(func=cmd_stops)

    pl = common(sub.add_parser("plan", help="plan a journey"))
    pl.add_argument("--from", dest="origin", required=True)
    pl.add_argument("--to", dest="destination", required=True)
    pl.add_argument("--at", default="08:00", help="departure time, HH:MM")
    pl.add_argument("--weekday", type=int, choices=range(7),
                    help="0=Monday .. 6=Sunday (default: today)")
    pl.add_argument("--max-rounds", type=int, default=5,
                    help="max vehicles boarded (transfers + 1)")
    pl.set_defaults(func=cmd_plan)

    b = common(sub.add_parser("benchmark", help="time random queries"))
    b.add_argument("--queries", type=int, default=100)
    b.add_argument("--at", default="08:00")
    b.add_argument("--weekday", type=int, choices=range(7))
    b.add_argument("--max-rounds", type=int, default=5)
    b.add_argument("--seed", type=int, default=42)
    b.set_defaults(func=cmd_benchmark)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except GtfsError as exc:
        print(f"feed error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
