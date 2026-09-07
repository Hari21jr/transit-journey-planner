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
import unicodedata
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
# A trailing letter, or an explicitly labelled platform.
_LETTER_OR_LABELLED = re.compile(
    r"""\s+(?:
          [A-Z]                                              # HURDMAN A
        | (?:PLATFORM|PLATFORME|BAY|QUAI|STAND)\s*\d{1,2}    # BLAIR PLATFORM 3
    )$""",
    re.IGNORECASE | re.VERBOSE,
)

# A bare trailing number, which is far more dangerous — see below.
_BARE_NUMBER = re.compile(r"\s+\d{1,2}$")

MIN_STEM_CHARS = 4
# A bare number is only a platform designator when what precedes it is
# substantial. "TUNNEY'S PASTURE 2" is a platform; "Stop 12" is a stop's
# whole name, and stripping it collapses every numbered stop in the feed
# into one place. Requiring a longer, multi-word stem separates the two.
MIN_BARE_NUMBER_STEM_CHARS = 10


def normalise_name(name: str) -> str:
    """Strip a trailing platform designator, if removing it leaves a name.

    Deliberately conservative in both directions. "RIDEAU / FRIEL" and
    "RIDEAU / NELSON" keep their distinct names and stay separate places —
    they are different corners, not platforms of one station.
    """
    name = name.strip()

    stripped = _LETTER_OR_LABELLED.sub("", name)
    if stripped != name and len(stripped) >= MIN_STEM_CHARS:
        return stripped.lower()

    stripped = _BARE_NUMBER.sub("", name)
    if (stripped != name
            and len(stripped) >= MIN_BARE_NUMBER_STEM_CHARS
            and " " in stripped.strip()):
        return stripped.lower()

    return name.lower()


_APOSTROPHE = re.compile(r"['’ʼ]")
_NON_WORD = re.compile(r"[^0-9a-z]+")


def fold(text: str) -> str:
    """Reduce a name to the letters and digits someone would actually type.

    Transit stop names are full of punctuation nobody reproduces from
    memory: ``RIDEAU / CHARLOTTE``, ``ST-LAURENT``, ``TUNNEY'S PASTURE``,
    ``PLACE D'ORLÉANS``. Matching those literally means the search only
    works for people who already know the exact name, which is the
    opposite of what a search box is for. Accents go too — few people
    switch keyboard layouts to look up a bus stop.
    """
    decomposed = unicodedata.normalize("NFKD", text.lower())
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    # Apostrophes are deleted rather than turned into a space, because the
    # way people type "TUNNEY'S PASTURE" is "tunneys pasture". Splitting on
    # them instead would make the folded name "tunney s pasture", which that
    # query does not match.
    return _NON_WORD.sub(" ", _APOSTROPHE.sub("", ascii_only)).strip()


@dataclass
class Place:
    """One named location, backed by one or more GTFS stops."""

    id: str
    name: str
    stop_ids: list[str] = field(default_factory=list)
    lat: float = 0.0
    lon: float = 0.0
    # Precomputed at build time: search runs over every place on each
    # keystroke, and folding 4,000 names per request is wasted work.
    search_key: str = ""

    def key(self) -> str:
        """Folded name, computed on demand for hand-built Place objects."""
        return self.search_key or fold(self.name)

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
            search_key=fold(name),
            stop_ids=sorted(stop_ids),
            lat=sum(s.lat for s in stops) / len(stops),
            lon=sum(s.lon for s in stops) / len(stops),
        )
    return places


def search_places(places: dict[str, Place], query: str, limit: int = 25) -> list[Place]:
    """Find places by name, best match first.

    Ranked exact > prefix > substring > all words present. Without that
    ordering, searching a real feed for "Bank" returns forty stops on Bank
    Street before the station actually called Bank.

    The last tier is what makes the box usable: "rideau charlotte" should
    find ``RIDEAU / CHARLOTTE`` even though that is not a substring of it,
    and word order should not matter, because nobody remembers which side
    of the slash a cross-street was on.
    """
    needle = fold(query)
    if not needle:
        return []
    words = needle.split()

    exact, prefix, contains, all_words = [], [], [], []
    for place in places.values():
        key = place.key()
        if key == needle:
            exact.append(place)
        elif key.startswith(needle):
            prefix.append(place)
        elif needle in key:
            contains.append(place)
        elif len(words) > 1 and all(w in key for w in words):
            all_words.append(place)

    for bucket in (prefix, contains, all_words):
        bucket.sort(key=lambda p: (len(p.name), p.name))
    return (exact + prefix + contains + all_words)[:limit]


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
    # An exact name match wins even when other places contain the string:
    # "Bank" should not be ambiguous just because forty stops sit on Bank
    # Street. Compared folded, so "st laurent" resolves "ST-LAURENT".
    needle = fold(token)
    exact = [p for p in matches if p.key() == needle]
    if len(exact) == 1:
        return exact[0]
    return None
