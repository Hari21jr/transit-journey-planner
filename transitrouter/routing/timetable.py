"""Reshape a GTFS feed into the structures RAPTOR needs.

The important idea here is that a GTFS ``route`` is not a RAPTOR route.
GTFS route 95 might contain trips that skip stops, short-turn, or run a
different pattern on weekends. RAPTOR requires that every trip on a route
visits exactly the same stops in the same order, so trips are regrouped by
their actual stop sequence. One GTFS route commonly becomes a dozen
RAPTOR routes.

Getting this wrong produces a router that silently returns bad journeys —
it will happily board a trip at a stop that trip does not serve.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ..gtfs.loader import Feed


@dataclass
class TimetableRoute:
    """Trips sharing an identical stop sequence, sorted by departure time."""

    stops: tuple[str, ...]
    gtfs_route_id: str
    trip_ids: list[str] = field(default_factory=list)
    # Indexed [trip][position in stops]
    arrivals: list[list[int]] = field(default_factory=list)
    departures: list[list[int]] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.trip_ids)

    def earliest_trip(self, stop_index: int, after: int) -> int | None:
        """Index of the first trip departing ``stop_index`` at or after ``after``.

        Trips are sorted by departure time at the first stop. Because every
        trip on a RAPTOR route serves the same stops in order and vehicles
        are assumed not to overtake one another, that ordering also holds at
        every later stop — which is what makes this binary search valid.
        """
        lo, hi = 0, len(self.departures)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.departures[mid][stop_index] < after:
                lo = mid + 1
            else:
                hi = mid
        return lo if lo < len(self.departures) else None


@dataclass
class Timetable:
    """RAPTOR-ready view of a feed, optionally filtered to one weekday."""

    routes: list[TimetableRoute] = field(default_factory=list)
    # stop_id -> [(route index, index of that stop within the route)]
    stop_routes: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    # stop_id -> [(stop_id, walk seconds)]
    transfers: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    feed: Feed | None = None

    @property
    def stats(self) -> dict[str, int]:
        return {
            "routes": len(self.routes),
            "trips": sum(len(r) for r in self.routes),
            "stops": len(self.stop_routes),
            "transfers": sum(len(v) for v in self.transfers.values()),
        }


def build_timetable(feed: Feed, weekday: int | None = None) -> Timetable:
    """Group trips into RAPTOR routes.

    ``weekday`` is 0=Monday..6=Sunday. When given, only trips whose service
    runs that day are included; when omitted, every trip is. Feeds without a
    ``calendar.txt`` are treated as running every day, since filtering them
    to nothing would be worse than being slightly permissive.
    """
    patterns: dict[tuple[str, tuple[str, ...]], TimetableRoute] = {}

    for trip in feed.trips.values():
        if weekday is not None and feed.service_days:
            days = feed.service_days.get(trip.service_id)
            if days is not None and weekday not in days:
                continue

        key = (trip.route_id, tuple(trip.stop_ids))
        route = patterns.get(key)
        if route is None:
            route = TimetableRoute(stops=tuple(trip.stop_ids),
                                   gtfs_route_id=trip.route_id)
            patterns[key] = route
        route.trip_ids.append(trip.id)
        route.arrivals.append(trip.arrivals)
        route.departures.append(trip.departures)

    timetable = Timetable(feed=feed)
    stop_routes: dict[str, list[tuple[int, int]]] = defaultdict(list)

    for route in patterns.values():
        # Sort trips by departure from the first stop so earliest_trip can
        # binary search.
        order = sorted(range(len(route.trip_ids)),
                       key=lambda i: route.departures[i][0])
        route.trip_ids = [route.trip_ids[i] for i in order]
        route.arrivals = [route.arrivals[i] for i in order]
        route.departures = [route.departures[i] for i in order]

        index = len(timetable.routes)
        timetable.routes.append(route)
        for position, stop_id in enumerate(route.stops):
            stop_routes[stop_id].append((index, position))

    timetable.stop_routes = dict(stop_routes)
    return timetable
