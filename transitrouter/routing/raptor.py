"""RAPTOR — Round-bAsed Public Transit Optimized Router.

Dijkstra is the reflex for shortest paths, but it fits transit badly. A
transit network is not a static graph: the cost of an edge depends on when
you arrive at it, so the graph has to be expanded over time, and it grows
enormous. RAPTOR sidesteps that by not building a graph at all.

Instead it works in rounds, where round k is "the best you can do using at
most k vehicles". Round 1 considers every route reachable from the origin.
Round 2 considers every route reachable from anywhere round 1 got you. And
so on. Two useful properties fall out:

  * It is naturally multi-criteria. The result is a set of journeys that
    trade arrival time against number of transfers, rather than one answer
    from an arbitrary transfer penalty.
  * Round count bounds transfers directly, so "at most 2 transfers" is a
    loop bound rather than a filter applied afterwards.

Reference: Delling, Pajor & Werneck, "Round-Based Public Transit Routing"
(Microsoft Research, 2012).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .journey import Journey, Leg
from .timetable import Timetable

INF = 2**31


@dataclass(frozen=True)
class _Board:
    """How a stop was reached: by riding, or on foot."""

    kind: str            # "ride" | "walk"
    from_stop: str
    route_index: int = -1
    trip_index: int = -1
    board_position: int = -1
    alight_position: int = -1
    walk_seconds: int = 0


@dataclass
class RaptorResult:
    """Every Pareto-optimal journey found, best arrival first."""

    journeys: list[Journey]
    rounds_used: int
    stops_reached: int

    @property
    def best(self) -> Journey | None:
        return self.journeys[0] if self.journeys else None


Endpoint = str | Iterable[str] | Mapping[str, int]


def _endpoint(value: Endpoint) -> dict[str, int]:
    """Normalise an endpoint to ``{stop_id: walk_seconds}``.

    A bare stop id or a collection of them costs nothing to reach or leave —
    that is the station case, where the traveller is already on the platform.
    A mapping carries a per-stop walk, which is what an address needs: the
    stop at the end of the street and the one four blocks away are not
    reachable at the same moment, and seeding both at the query time would
    invent journeys that cannot be caught.
    """
    if isinstance(value, str):
        return {value: 0} if value else {}
    if isinstance(value, Mapping):
        return {s: max(0, int(w)) for s, w in value.items() if s}
    return {s: 0 for s in value if s}


def plan(
    timetable: Timetable,
    origin: Endpoint,
    destination: Endpoint,
    departure_time: int,
    max_rounds: int = 5,
    max_initial_walk: bool = True,
) -> RaptorResult:
    """Find journeys from ``origin`` to ``destination`` departing no earlier
    than ``departure_time`` (seconds since midnight).

    Both endpoints accept a single stop id, a collection of them, or a
    mapping of stop id to the seconds spent walking to or from it. A named
    place such as a station is a set of platform stops the traveller does
    not choose between, so all of them are seeded at once and the search
    picks whichever turns out best; an address is the same idea with a
    walking cost attached to each.

    Returns one journey per transfer count, so the caller can choose between
    "fastest" and "fewest changes" rather than being handed a single answer.
    """
    routes = timetable.routes
    stop_routes = timetable.stop_routes
    footpaths = timetable.transfers

    origins = _endpoint(origin)
    targets = _endpoint(destination)

    if not origins or not targets or origins.keys() & targets.keys():
        return RaptorResult([], 0, 0)
    if not any(o in stop_routes or o in footpaths for o in origins):
        return RaptorResult([], 0, 0)

    # best[p]: earliest known arrival at p by any number of trips.
    best: dict[str, int] = {s: departure_time + w for s, w in origins.items()}
    # labels[k][p]: earliest arrival at p using at most k trips.
    labels: list[dict[str, int]] = [dict(best)]
    parents: list[dict[str, _Board]] = [{}]

    marked: set[str] = set(origins)

    # The best *door* arrival at the destination — the arrival at a target
    # stop plus the walk from it — cached rather than recomputed. This is
    # read once per stop scanned, millions of times on a real feed, so a
    # min() over the target set here is measurably slower than maintaining
    # it. Pruning against the door arrival stays correct because every
    # egress walk is non-negative.
    target_bound = INF

    # Walking from the origin before boarding anything is allowed, and is
    # often the difference between a sensible journey and a silly one.
    if max_initial_walk:
        for start, access in origins.items():
            for near, seconds in footpaths.get(start, ()):
                arrival = departure_time + access + seconds
                if arrival < best.get(near, INF):
                    best[near] = arrival
                    if near in targets:
                        target_bound = min(target_bound, arrival + targets[near])
                    labels[0][near] = arrival
                    parents[0][near] = _Board("walk", start, walk_seconds=seconds)
                    marked.add(near)

    journeys: list[Journey] = []
    rounds_used = 0

    for k in range(1, max_rounds + 1):
        labels.append({})
        parents.append({})
        rounds_used = k

        # --- collect the routes worth scanning this round -------------- #
        # For each route, remember the earliest position at which we can
        # board it. Scanning from further along would miss nothing but
        # would also gain nothing.
        queue: dict[int, int] = {}
        for stop in marked:
            for route_index, position in stop_routes.get(stop, ()):
                current = queue.get(route_index)
                if current is None or position < current:
                    queue[route_index] = position
        marked = set()

        # --- scan each route once -------------------------------------- #
        for route_index, start_position in queue.items():
            route = routes[route_index]
            trip_index: int | None = None
            board_position = -1

            for position in range(start_position, len(route.stops)):
                stop = route.stops[position]

                if trip_index is not None:
                    arrival = route.arrivals[trip_index][position]
                    # Pruning: an arrival no better than what we already know
                    # for this stop, or than the best arrival at the target,
                    # cannot be part of an optimal journey.
                    if arrival < min(best.get(stop, INF), target_bound):
                        best[stop] = arrival
                        if stop in targets:
                            target_bound = min(target_bound, arrival + targets[stop])
                        labels[k][stop] = arrival
                        parents[k][stop] = _Board(
                            "ride", route.stops[board_position],
                            route_index=route_index, trip_index=trip_index,
                            board_position=board_position, alight_position=position,
                        )
                        marked.add(stop)

                # Can an earlier trip be caught here? This is the step that
                # makes RAPTOR correct on routes where a later-departing trip
                # overtakes nothing but reaches this stop sooner.
                previous = labels[k - 1].get(stop)
                if previous is not None and (
                    trip_index is None
                    or previous <= route.departures[trip_index][position]
                ):
                    candidate = route.earliest_trip(position, previous)
                    if candidate is not None and candidate != trip_index:
                        trip_index = candidate
                        board_position = position

        # --- relax footpaths ------------------------------------------- #
        # One walk per round: walking twice in a row is never better than
        # walking once, given transfers are already shortest-path distances.
        for stop in list(marked):
            base = labels[k].get(stop)
            if base is None:
                continue
            for near, seconds in footpaths.get(stop, ()):
                arrival = base + seconds
                if arrival < best.get(near, INF):
                    best[near] = arrival
                    if near in targets:
                        target_bound = min(target_bound, arrival + targets[near])
                    labels[k][near] = arrival
                    parents[k][near] = _Board("walk", stop, walk_seconds=seconds)
                    marked.add(near)

        # Record the best journey achievable with exactly this many rounds,
        # arriving at whichever target stop is reached earliest.
        reached = [t for t in targets if t in labels[k]]
        if reached:
            # Whichever target stop gets you to the door soonest, not
            # whichever the vehicle reaches soonest — a stop reached two
            # minutes earlier but five minutes further to walk is worse.
            arrival_stop = min(reached, key=lambda t: labels[k][t] + targets[t])
            journey = _reconstruct(timetable, labels, parents, k,
                                   set(origins), arrival_stop)
            if journey and journey.legs:
                journeys.append(journey)

        if not marked:
            break

    # Keep only Pareto-optimal results: a journey with more transfers has to
    # actually arrive earlier to earn its place.
    journeys.sort(key=lambda j: (j.arrive, j.transfers))
    pareto: list[Journey] = []
    fewest = INF
    for journey in journeys:
        if journey.transfers < fewest:
            pareto.append(journey)
            fewest = journey.transfers

    return RaptorResult(pareto, rounds_used, len(best))


def _reconstruct(
    timetable: Timetable,
    labels: list[dict[str, int]],
    parents: list[dict[str, _Board]],
    round_index: int,
    origins: set[str],
    destination: str,
) -> Journey | None:
    """Walk the parent pointers backwards from the destination."""
    feed = timetable.feed
    legs: list[Leg] = []
    stop = destination
    k = round_index
    guard = 0

    while stop not in origins:
        guard += 1
        if guard > 200:  # a cycle here would mean a bug, not a long journey
            return None

        board = parents[k].get(stop)
        while board is None and k > 0:
            k -= 1
            board = parents[k].get(stop)
        if board is None:
            return None

        arrive = labels[k].get(stop)
        if arrive is None:
            arrive = min((labels[i][stop] for i in range(len(labels))
                          if stop in labels[i]), default=None)
            if arrive is None:
                return None

        if board.kind == "walk":
            legs.append(Leg(
                kind="walk", from_stop=board.from_stop, to_stop=stop,
                depart=arrive - board.walk_seconds, arrive=arrive,
            ))
            stop = board.from_stop
            # A walk does not consume a round.
        else:
            route = timetable.routes[board.route_index]
            trip_id = route.trip_ids[board.trip_index]
            trip = feed.trips.get(trip_id) if feed else None
            gtfs_route = feed.routes.get(route.gtfs_route_id) if feed else None
            legs.append(Leg(
                kind="ride",
                from_stop=board.from_stop, to_stop=stop,
                depart=route.departures[board.trip_index][board.board_position],
                arrive=route.arrivals[board.trip_index][board.alight_position],
                route_label=gtfs_route.label if gtfs_route else route.gtfs_route_id,
                headsign=trip.headsign if trip else "",
                trip_id=trip_id,
                intermediate_stops=board.alight_position - board.board_position,
                stop_ids=list(route.stops[board.board_position:
                                          board.alight_position + 1]),
            ))
            stop = board.from_stop
            k -= 1
            if k < 0:
                return None

    legs.reverse()
    return Journey(legs=legs)
