"""Read a GTFS feed into memory.

A real feed is bigger than it looks. OC Transpo's ``stop_times.txt`` is a few
million rows; Toronto's is larger. Two consequences shape this module:

* Rows are streamed with the stdlib ``csv`` reader and turned into plain
  integers immediately. Keeping a dict per stop-time would cost hundreds of
  bytes per row and gigabytes overall.
* Times become seconds since midnight. GTFS deliberately allows hours past
  24 — a trip departing 25:10:00 is the 1:10am service belonging to the
  *previous* service day — so wall-clock parsing would corrupt overnight
  routes.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# GTFS files this router actually reads. Anything else in the zip is ignored.
REQUIRED = ("stops.txt", "routes.txt", "trips.txt", "stop_times.txt")
OPTIONAL = ("calendar.txt", "calendar_dates.txt", "transfers.txt")


class GtfsError(Exception):
    """Raised when a feed is missing files or is structurally unusable."""


def parse_time(value: str) -> int:
    """``"25:10:00"`` -> 90600. Returns -1 for a blank time.

    Blank arrival/departure times are legal in GTFS: they mark a stop the
    vehicle passes without a scheduled time, to be interpolated. This router
    drops those stops rather than guessing.
    """
    value = value.strip()
    if not value:
        return -1
    try:
        parts = value.split(":")
        if len(parts) == 2:  # some feeds omit seconds
            h, m, s = int(parts[0]), int(parts[1]), 0
        else:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
    except (ValueError, IndexError) as exc:
        raise GtfsError(f"unparseable time {value!r}") from exc
    return h * 3600 + m * 60 + s


def parse_date(value: str) -> date | None:
    """GTFS dates are ``YYYYMMDD``. Returns None for a blank or malformed one."""
    value = value.strip()
    if len(value) != 8 or not value.isdigit():
        return None
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    except ValueError:
        return None


def format_time(seconds: int) -> str:
    """Inverse of :func:`parse_time`, keeping hours past 24 visible."""
    if seconds < 0:
        return "--:--"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


@dataclass
class Stop:
    id: str
    name: str
    lat: float
    lon: float
    parent: str = ""
    # GTFS location_type: 0 is a boarding point, 1 is a parent station, and
    # 2-4 are entrances, generic nodes and boarding areas. Only 0 can be
    # ridden from, and conflating a station with its platforms produces a
    # place that swallows its own members.
    location_type: int = 0

    @property
    def is_boardable(self) -> bool:
        return self.location_type == 0

    def __repr__(self) -> str:  # keeps test failures readable
        return f"Stop({self.id}, {self.name!r})"


@dataclass
class Route:
    id: str
    short_name: str
    long_name: str
    type: int = 3  # 3 = bus, per the GTFS spec

    @property
    def label(self) -> str:
        return self.short_name or self.long_name or self.id


@dataclass
class Trip:
    id: str
    route_id: str
    service_id: str
    headsign: str = ""
    # Parallel arrays, in stop sequence order. Kept as lists of primitives
    # rather than objects because there are millions of these.
    stop_ids: list[str] = field(default_factory=list)
    arrivals: list[int] = field(default_factory=list)
    departures: list[int] = field(default_factory=list)

    @property
    def start_time(self) -> int:
        return self.departures[0] if self.departures else -1


@dataclass
class Service:
    """When a service runs: a weekly pattern plus per-date exceptions.

    ``calendar.txt`` gives the pattern; ``calendar_dates.txt`` overrides it
    on specific days. Agencies lean on the exceptions heavily — statutory
    holidays typically remove the weekday service and add a Sunday one — so
    a router that reads only the weekly pattern gets holidays wrong in both
    directions. Some feeds skip ``calendar.txt`` entirely and express every
    single service day as an addition.
    """

    id: str
    weekdays: set[int] = field(default_factory=set)  # 0=Monday
    start: date | None = None
    end: date | None = None
    added: set[date] = field(default_factory=set)
    removed: set[date] = field(default_factory=set)

    def runs_on(self, day: date) -> bool:
        if day in self.removed:
            return False
        if day in self.added:
            return True
        if self.start and day < self.start:
            return False
        if self.end and day > self.end:
            return False
        return day.weekday() in self.weekdays


@dataclass
class Feed:
    """A parsed GTFS feed."""

    stops: dict[str, Stop] = field(default_factory=dict)
    routes: dict[str, Route] = field(default_factory=dict)
    trips: dict[str, Trip] = field(default_factory=dict)
    services: dict[str, Service] = field(default_factory=dict)
    # Explicit transfers declared by the agency, (from, to) -> seconds
    declared_transfers: dict[tuple[str, str], int] = field(default_factory=dict)

    @property
    def service_days(self) -> dict[str, set[int]]:
        """Weekly patterns only. Kept for callers that ignore exceptions."""
        return {sid: s.weekdays for sid, s in self.services.items()}

    def runs_on(self, service_id: str, day: date) -> bool:
        """Whether a service operates on a given date.

        An unknown service_id is treated as running. Feeds sometimes
        reference services they never define, and dropping those trips
        silently would be worse than including them.
        """
        service = self.services.get(service_id)
        return True if service is None else service.runs_on(day)

    @property
    def service_window(self) -> tuple[date | None, date | None]:
        """The first and last date any service in this feed operates.

        Feeds are snapshots with an expiry — usually a few weeks or months.
        Routing for a date outside the window correctly returns nothing,
        which looks exactly like a broken router unless the range is
        reported, so callers can say what went wrong.
        """
        starts = [s.start for s in self.services.values() if s.start]
        ends = [s.end for s in self.services.values() if s.end]
        for service in self.services.values():
            starts.extend(service.added)
            ends.extend(service.added)
        return (min(starts) if starts else None, max(ends) if ends else None)

    def covers(self, day: date) -> bool:
        start, end = self.service_window
        if start and day < start:
            return False
        return not (end and day > end)

    def summary(self) -> str:
        stop_times = sum(len(t.stop_ids) for t in self.trips.values())
        return (f"{len(self.stops):,} stops, {len(self.routes):,} routes, "
                f"{len(self.trips):,} trips, {stop_times:,} stop times")


# --------------------------------------------------------------------- #


def _open_source(path: str | Path):
    """Yield ``(filename, text_stream)`` for each file in a zip or directory."""
    path = Path(path)
    if path.is_dir():
        def reader(name: str):
            f = path / name
            return f.open("r", encoding="utf-8-sig", newline="") if f.exists() else None
        return reader, {p.name for p in path.iterdir()}

    if not path.exists():
        raise GtfsError(f"no such feed: {path}")

    archive = zipfile.ZipFile(path)
    # Feeds are sometimes zipped with a containing folder.
    names = {Path(n).name: n for n in archive.namelist() if not n.endswith("/")}

    def reader(name: str):
        actual = names.get(name)
        if actual is None:
            return None
        return io.TextIOWrapper(archive.open(actual), encoding="utf-8-sig", newline="")

    return reader, set(names)


def _rows(stream) -> list[dict[str, str]]:
    return list(csv.DictReader(stream))


def load_feed(path: str | Path, progress=None) -> Feed:
    """Load a GTFS feed from a ``.zip`` or an unzipped directory."""
    reader, present = _open_source(path)

    missing = [f for f in REQUIRED if f not in present]
    if missing:
        raise GtfsError(f"feed is missing required files: {', '.join(missing)}")

    feed = Feed()

    def note(msg: str) -> None:
        if progress:
            progress(msg)

    # ---- stops -------------------------------------------------------- #
    note("reading stops")
    with reader("stops.txt") as fh:
        for row in csv.DictReader(fh):
            try:
                lat = float(row.get("stop_lat") or "nan")
                lon = float(row.get("stop_lon") or "nan")
            except ValueError:
                continue  # a stop with no position cannot be walked to
            sid = row["stop_id"].strip()
            try:
                location_type = int((row.get("location_type") or "0").strip() or 0)
            except ValueError:
                location_type = 0
            feed.stops[sid] = Stop(
                id=sid,
                name=(row.get("stop_name") or sid).strip(),
                lat=lat, lon=lon,
                parent=(row.get("parent_station") or "").strip(),
                location_type=location_type,
            )

    # ---- routes ------------------------------------------------------- #
    note("reading routes")
    with reader("routes.txt") as fh:
        for row in csv.DictReader(fh):
            rid = row["route_id"].strip()
            feed.routes[rid] = Route(
                id=rid,
                short_name=(row.get("route_short_name") or "").strip(),
                long_name=(row.get("route_long_name") or "").strip(),
                type=int(row.get("route_type") or 3),
            )

    # ---- trips -------------------------------------------------------- #
    note("reading trips")
    with reader("trips.txt") as fh:
        for row in csv.DictReader(fh):
            tid = row["trip_id"].strip()
            feed.trips[tid] = Trip(
                id=tid,
                route_id=row["route_id"].strip(),
                service_id=(row.get("service_id") or "").strip(),
                headsign=(row.get("trip_headsign") or "").strip(),
            )

    # ---- stop times (the big one) ------------------------------------- #
    note("reading stop times")
    # Collected per trip first, then sorted by stop_sequence. Feeds are
    # usually already ordered, but the spec does not promise it.
    pending: dict[str, list[tuple[int, str, int, int]]] = defaultdict(list)
    with reader("stop_times.txt") as fh:
        for row in csv.DictReader(fh):
            tid = row["trip_id"].strip()
            if tid not in feed.trips:
                continue  # stop_time for a trip we dropped
            arr = parse_time(row.get("arrival_time", ""))
            dep = parse_time(row.get("departure_time", ""))
            if arr < 0 and dep < 0:
                continue  # untimed stop
            if arr < 0:
                arr = dep
            if dep < 0:
                dep = arr
            sid = row["stop_id"].strip()
            if sid not in feed.stops:
                continue
            seq = int(row.get("stop_sequence") or 0)
            pending[tid].append((seq, sid, arr, dep))

    for tid, entries in pending.items():
        entries.sort(key=lambda e: e[0])
        trip = feed.trips[tid]
        trip.stop_ids = [e[1] for e in entries]
        trip.arrivals = [e[2] for e in entries]
        trip.departures = [e[3] for e in entries]

    # A trip with fewer than two timed stops cannot be ridden.
    feed.trips = {tid: t for tid, t in feed.trips.items() if len(t.stop_ids) >= 2}

    # ---- calendar ----------------------------------------------------- #
    note("reading calendar")
    if "calendar.txt" in present:
        days = ("monday", "tuesday", "wednesday", "thursday",
                "friday", "saturday", "sunday")
        with reader("calendar.txt") as fh:
            for row in csv.DictReader(fh):
                sid = row["service_id"].strip()
                feed.services[sid] = Service(
                    id=sid,
                    weekdays={i for i, d in enumerate(days)
                              if (row.get(d) or "0").strip() == "1"},
                    start=parse_date(row.get("start_date", "")),
                    end=parse_date(row.get("end_date", "")),
                )

    if "calendar_dates.txt" in present:
        note("reading calendar exceptions")
        with reader("calendar_dates.txt") as fh:
            for row in csv.DictReader(fh):
                sid = row["service_id"].strip()
                day = parse_date(row.get("date", ""))
                if day is None:
                    continue
                service = feed.services.get(sid)
                if service is None:
                    # A calendar_dates-only feed: no weekly pattern exists,
                    # every operating day is listed as an addition.
                    service = feed.services[sid] = Service(id=sid)
                # exception_type 1 = added, 2 = removed
                if (row.get("exception_type") or "1").strip() == "2":
                    service.removed.add(day)
                else:
                    service.added.add(day)

    # ---- declared transfers ------------------------------------------- #
    if "transfers.txt" in present:
        note("reading transfers")
        with reader("transfers.txt") as fh:
            for row in csv.DictReader(fh):
                a = (row.get("from_stop_id") or "").strip()
                b = (row.get("to_stop_id") or "").strip()
                if not a or not b or a not in feed.stops or b not in feed.stops:
                    continue
                # transfer_type 3 means "transfer not possible" — skip it.
                if (row.get("transfer_type") or "0").strip() == "3":
                    continue
                seconds = int(row.get("min_transfer_time") or 0)
                feed.declared_transfers[(a, b)] = seconds

    note("done")
    return feed
