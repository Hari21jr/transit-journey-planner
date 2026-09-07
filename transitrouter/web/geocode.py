"""Turn a street address into coordinates, and coordinates into stops.

A GTFS feed knows where the buses stop. It does not know where anyone
lives, so "47 Huntcliff Place" is unfindable in it no matter how good the
name matching is — the string simply is not in the data. Answering that
query needs a second source.

OpenStreetMap's Nominatim is used because it is free, needs no key, and
covers the whole world. Its terms ask for an identifying User-Agent and no
more than one request a second, both of which are honoured below. Results
are cached, since the same address gets typed repeatedly while someone
tries different departure times.

This is the only part of the project that reaches the network, and it is
deliberately failure-tolerant: if Nominatim is slow, down, or blocked, the
planner falls back to stop-name search rather than breaking.
"""

from __future__ import annotations

import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass

from ..gtfs.loader import Feed
from ..routing.transfers import haversine, walk_seconds

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = (
    "transitrouter/1.0 (GTFS journey planner; "
    "https://github.com/Hari21jr/transit-journey-planner)"
)
REQUEST_TIMEOUT = 6.0
MIN_REQUEST_INTERVAL = 1.0  # Nominatim's published rate limit.
CACHE_SIZE = 512

# How far someone will walk to reach a bus from a front door. Longer than a
# transfer between two stops, because there is no alternative — you cannot
# be dropped closer to your own house.
DEFAULT_ACCESS_WALK_M = 800.0
# Beyond a handful of boarding options the extra stops are all worse and
# only cost search time.
MAX_ACCESS_STOPS = 8


@dataclass
class Location:
    """A geocoded point, and how it should be labelled in an itinerary."""

    name: str
    lat: float
    lon: float


class Geocoder:
    """Address lookup, bounded to the feed's own area and rate-limited."""

    def __init__(self, feed: Feed, user_agent: str = USER_AGENT):
        self.user_agent = user_agent
        self._cache: OrderedDict[str, Location | None] = OrderedDict()
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._viewbox = _feed_bounds(feed)

    def lookup(self, query: str) -> Location | None:
        """Geocode ``query``, or return None if it cannot be placed.

        Results are confined to the feed's bounding box. Without that,
        "Bank Street" in an Ottawa feed resolves to London, and the planner
        confidently reports that no bus goes there.
        """
        key = " ".join(query.lower().split())
        if not key or self._viewbox is None:
            return None

        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]

        found = self._request(query)

        with self._lock:
            self._cache[key] = found
            self._cache.move_to_end(key)
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        return found

    def _request(self, query: str) -> Location | None:
        south, west, north, east = self._viewbox
        params = urllib.parse.urlencode({
            "q": query,
            "format": "jsonv2",
            "limit": "1",
            "addressdetails": "0",
            # bounded=1 makes the viewbox a hard filter rather than a hint.
            "viewbox": f"{west},{north},{east},{south}",
            "bounded": "1",
        })
        request = urllib.request.Request(
            f"{NOMINATIM_URL}?{params}",
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
        )

        with self._lock:
            wait = MIN_REQUEST_INTERVAL - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            # A geocoder that is down should degrade the planner, not break
            # it: stop-name search still works.
            return None

        if not payload:
            return None
        first = payload[0]
        try:
            return Location(
                name=_short_name(first.get("display_name", query)),
                lat=float(first["lat"]),
                lon=float(first["lon"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _short_name(display_name: str) -> str:
    """Nominatim returns the full postal hierarchy down to the country.

    "47 Huntcliff Place, Barrhaven, Nepean, Ottawa, Eastern Ontario,
    Ontario, K2J 3Y6, Canada" is accurate and useless as a label. The first
    two components are the part a person recognises.
    """
    parts = [p.strip() for p in display_name.split(",") if p.strip()]
    return ", ".join(parts[:2]) if parts else display_name


def _feed_bounds(feed: Feed) -> tuple[float, float, float, float] | None:
    """South, west, north, east of the feed's stops, padded by ~15km."""
    lats = [s.lat for s in feed.stops.values() if not math.isnan(s.lat)]
    lons = [s.lon for s in feed.stops.values() if not math.isnan(s.lon)]
    if not lats or not lons:
        return None
    pad = 0.15
    return (min(lats) - pad, min(lons) - pad, max(lats) + pad, max(lons) + pad)


class StopIndex:
    """Grid index over stop positions, for "what can I walk to from here".

    The same trick as the transfer builder: scanning 6,000 stops per query
    is affordable once, but this runs on the origin and the destination of
    every request.
    """

    def __init__(self, feed: Feed, cell_m: float = DEFAULT_ACCESS_WALK_M):
        self.feed = feed
        self.cell_m = cell_m
        lats = [s.lat for s in feed.stops.values() if not math.isnan(s.lat)]
        mean_lat = sum(lats) / len(lats) if lats else 45.0
        self._lat_cell = cell_m / 111_320.0
        self._lon_cell = cell_m / (111_320.0 * max(math.cos(math.radians(mean_lat)), 0.01))
        self._grid: dict[tuple[int, int], list[str]] = {}
        for stop in feed.stops.values():
            if math.isnan(stop.lat) or math.isnan(stop.lon) or not stop.is_boardable:
                continue
            cell = (int(stop.lat / self._lat_cell), int(stop.lon / self._lon_cell))
            self._grid.setdefault(cell, []).append(stop.id)

    def walkable_from(
        self,
        lat: float,
        lon: float,
        radius_m: float = DEFAULT_ACCESS_WALK_M,
        limit: int = MAX_ACCESS_STOPS,
    ) -> dict[str, int]:
        """Nearest boardable stops, mapped to the seconds spent walking."""
        gy, gx = int(lat / self._lat_cell), int(lon / self._lon_cell)
        candidates: list[str] = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                candidates.extend(self._grid.get((gy + dy, gx + dx), ()))

        scored: list[tuple[float, str]] = []
        for stop_id in candidates:
            stop = self.feed.stops[stop_id]
            distance = haversine(lat, lon, stop.lat, stop.lon)
            if distance <= radius_m:
                scored.append((distance, stop_id))

        scored.sort()
        return {stop_id: walk_seconds(distance)
                for distance, stop_id in scored[:limit]}
