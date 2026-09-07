"""Walking transfers between nearby stops.

Most feeds declare only a handful of transfers, usually inside stations. But
real journeys often involve crossing a street to a stop on the other side,
which the feed never mentions. Without generated footpaths the router will
happily send you nine stops out of your way to use a connection the agency
happened to document.

Candidate pairs come from a grid index rather than comparing all stops to
all stops: OC Transpo has around 6,000 stops, so the naive approach is 36
million distance calculations, and a larger city is far worse.
"""

from __future__ import annotations

import math
from collections import defaultdict

from ..gtfs.loader import Feed

EARTH_RADIUS_M = 6_371_000.0
# Comfortable walking speed. Deliberately conservative: a router that
# assumes an Olympic pace produces itineraries people miss.
WALK_SPEED_MS = 1.33
DEFAULT_MAX_WALK_M = 400.0
# Time cost of getting off one vehicle and onto another at the same stop.
MIN_TRANSFER_SECONDS = 60

# Straight-line distance is not walking distance. You follow streets and
# cross at corners, so the route you actually walk is longer than the crow
# flies — by about a third in a normal street grid, and much more where a
# river, railway or highway forces a detour. Without this correction every
# walking leg is quietly optimistic, which is the failure mode that makes
# someone miss a bus. 1.3 is the usual figure from the routing literature
# and roughly what a street-network router returns for short urban trips.
STREET_DETOUR_FACTOR = 1.3


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def walk_seconds(metres: float) -> int:
    """Seconds to walk a straight-line distance of ``metres``.

    The detour factor is applied here rather than at the call sites so that
    every walking leg — transfers, and the walk from an address — is costed
    the same way.
    """
    return max(MIN_TRANSFER_SECONDS,
               int(metres * STREET_DETOUR_FACTOR / WALK_SPEED_MS))


def build_transfers(
    feed: Feed,
    max_walk_m: float = DEFAULT_MAX_WALK_M,
) -> dict[str, list[tuple[str, int]]]:
    """Generate symmetric walking transfers, merged with the feed's own.

    Where the agency declares a transfer time, that wins: they know their
    own stations better than a straight-line distance does.
    """
    # Grid cells sized so that any pair within max_walk_m falls in the same
    # cell or an adjacent one. One degree of latitude is ~111km everywhere;
    # longitude shrinks with latitude, so use the feed's own latitude.
    if not feed.stops:
        return {}

    mean_lat = sum(s.lat for s in feed.stops.values()) / len(feed.stops)
    lat_cell = max_walk_m / 111_320.0
    lon_cell = max_walk_m / (111_320.0 * max(math.cos(math.radians(mean_lat)), 0.01))

    grid: dict[tuple[int, int], list[str]] = defaultdict(list)
    for stop in feed.stops.values():
        if math.isnan(stop.lat) or math.isnan(stop.lon):
            continue
        grid[(int(stop.lat / lat_cell), int(stop.lon / lon_cell))].append(stop.id)

    transfers: dict[str, dict[str, int]] = defaultdict(dict)

    for (gy, gx), ids in grid.items():
        # Compare this cell against itself and its eight neighbours.
        neighbours: list[str] = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                neighbours.extend(grid.get((gy + dy, gx + dx), ()))

        for a_id in ids:
            a = feed.stops[a_id]
            for b_id in neighbours:
                if a_id == b_id:
                    continue
                b = feed.stops[b_id]
                distance = haversine(a.lat, a.lon, b.lat, b.lon)
                if distance <= max_walk_m:
                    transfers[a_id][b_id] = walk_seconds(distance)

    # Agency-declared transfers override generated ones.
    for (a_id, b_id), seconds in feed.declared_transfers.items():
        transfers[a_id][b_id] = max(seconds, MIN_TRANSFER_SECONDS)

    return {stop: sorted(dests.items(), key=lambda kv: kv[1])
            for stop, dests in transfers.items()}
