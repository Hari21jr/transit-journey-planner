"""Group stops into places a person would actually name.

In a real feed, "Hurdman Station" is not one stop. It is a parent station
plus a dozen numbered platforms, each with its own id, and often a pair of
on-street stops either side of the road as well. Someone asking to travel
"from Hurdman" does not know or care which platform their bus leaves from —
and routing from the wrong one produces a journey that starts with a
pointless walk, or no journey at all.

So queries are answered over a *place*: the set of stops that share a
parent station, or failing that, share a name and sit close together.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from ..gtfs.loader import Feed
from .transfers import haversine

# Stops with the same name further apart than this are different places —
# "Bank / Somerset" and "Bank / Gladstone" are not one location, and many
# feeds reuse a street name across an entire corridor.
SAME_NAME_RADIUS_M = 250.0

# Platform designators to strip before grouping. Many agencies — OC Transpo
# among them — never populate parent_station and instead encode the platform
# in the name: HURDMAN A, HURDMAN B, HURDMAN C are one station in every sense
# a traveller cares about. Without this, asking to leave "from Hurdman" is
# ambiguous between seven places that are all the same place.
_PLATFORM_SUFFIX = re.compile(
    r"""\s+(?:
          [A-Z]                      # HURDMAN A
        | (?:PLATFORM|PLATFORME|BAY|QUAI|STOP|STAND)\s*\d+   # ... PLATFORM 3
        | \d{1,2}                    # TUNNEY'S PASTURE 2
    )$""",
    re.IGNORECASE | re.VERBOSE,
)


def normalise_name(name: str) -> str:
    """Strip a trailing platform designator, if removing it leaves a name.

    Deliberately conservative: only one suffix is removed, and only when at
    least four characters survive. "RIDEAU / FRIEL" and "RIDEAU / NELSON"
    keep their distinct names and stay separate places, which is correct —
    they are different corners, not platforms of one station.
    """
    stripped = _PLATFORM_SUFFIX.sub("", name.strip())
    return (stripped if len(stripped) >= 4 else name.strip()).lower()


@dataclass
class Place:
    """One named location, backed by one or more GTFS stops."""

    id: str
    name: str
    stop_ids: list[str] = field(default_factory=list)
    lat: float = 0.0
    lon: float = 0.0

    @property
    def platform_count(self) -> int:
        return len(self.stop_ids)

    def __repr__(self) -> str:
        return f"Place({self.name!r}, {self.platform_count} stops)"


def build_places(feed: Feed) -> dict[str, Place]:
    """Collapse the feed's stops into places, keyed by place id."""
    groups: dict[str, list[str]] = defaultdict(list)

    # Pass 1: anything with a parent station belongs to that station.
    # Stations themselves are skipped — they are labels, not boarding points,
    # and letting one through would create a second place with the station's
    # own name competing against the group of its platforms.
    unparented: list[str] = []
    for stop in feed.stops.values():
        if not stop.is_boardable:
            continue
        if stop.parent and stop.parent in feed.stops:
            groups[stop.parent].append(stop.id)
        else:
            # No parent, or a parent referenced but never defined. Either
            # way the stop stands on its own until name clustering runs.
            unparented.append(stop.id)

    # Pass 2: cluster the rest by name, splitting clusters that are spread
    # too far apart to be one location.
    by_name: dict[str, list[str]] = defaultdict(list)
    for stop_id in unparented:
        by_name[normalise_name(feed.stops[stop_id].name)].append(stop_id)

    for name_key, stop_ids in by_name.items():
        if len(stop_ids) == 1:
            # setdefault, not assignment: a stop id can collide with a
            # station id already used as a group key in pass 1.
            groups.setdefault(stop_ids[0], stop_ids)
            continue

        remaining = list(stop_ids)
        cluster_index = 0
        while remaining:
            seed_id = remaining.pop(0)
            seed = feed.stops[seed_id]
            cluster = [seed_id]
            still: list[str] = []
            for other_id in remaining:
                other = feed.stops[other_id]
                if haversine(seed.lat, seed.lon, other.lat, other.lon) <= SAME_NAME_RADIUS_M:
                    cluster.append(other_id)
                else:
                    still.append(other_id)
            remaining = still
            key = seed_id if cluster_index == 0 else f"{name_key}#{cluster_index}"
            if key in groups:
                key = f"{name_key}#{cluster_index}@{seed_id}"
            groups[key] = cluster
            cluster_index += 1

    places: dict[str, Place] = {}
    for place_id, stop_ids in groups.items():
        if not stop_ids:
            continue
        stops = [feed.stops[s] for s in stop_ids if s in feed.stops]
        if not stops:
            continue
        # A parent station carries the name people use. Otherwise, when the
        # members only agree after stripping platform letters, use the shared
        # stem — calling the group "HURDMAN A" would be actively misleading
        # when it also contains B through E.
        parent = feed.stops.get(place_id)
        if parent is not None and not parent.is_boardable:
            name = parent.name
        else:
            distinct = {s.name for s in stops}
            if len(distinct) == 1:
                name = stops[0].name
            else:
                stem = normalise_name(stops[0].name)
                name = stem.upper() if stops[0].name.isupper() else stem.title()
        places[place_id] = Place(
            id=place_id,
            name=name,
            stop_ids=sorted(stop_ids),
            lat=sum(s.lat for s in stops) / len(stops),
            lon=sum(s.lon for s in stops) / len(stops),
        )
    return places


def search_places(places: dict[str, Place], query: str, limit: int = 25) -> list[Place]:
    """Find places by name fragment, best match first.

    An exact name beats a prefix, which beats a substring. Without that
    ordering, searching a real feed for "Bank" returns forty stops on Bank
    Street before the station actually called Bank.
    """
    needle = query.strip().lower()
    if not needle:
        return []

    exact, prefix, contains = [], [], []
    for place in places.values():
        name = place.name.lower()
        if name == needle:
            exact.append(place)
        elif name.startswith(needle):
            prefix.append(place)
        elif needle in name:
            contains.append(place)

    for bucket in (prefix, contains):
        bucket.sort(key=lambda p: (len(p.name), p.name))
    return (exact + prefix + contains)[:limit]


def resolve(feed: Feed, places: dict[str, Place], token: str) -> Place | None:
    """Turn user input into a single place, or None if it is not decidable.

    Accepts a place id, a raw stop id, or an unambiguous name.
    """
    token = token.strip()
    if token in places:
        return places[token]

    # A raw stop id: return whichever place contains it.
    if token in feed.stops:
        for place in places.values():
            if token in place.stop_ids:
                return place
        stop = feed.stops[token]
        return Place(id=token, name=stop.name, stop_ids=[token],
                     lat=stop.lat, lon=stop.lon)

    matches = search_places(places, token)
    if len(matches) == 1:
        return matches[0]
    # An exact name match wins even when other places contain the string.
    exact = [p for p in matches if p.name.lower() == token.lower()]
    if len(exact) == 1:
        return exact[0]
    return None
