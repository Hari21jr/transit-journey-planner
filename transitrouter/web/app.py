"""Web front end for the journey planner.

The feed is parsed once at startup and held in memory. Two million stop
times take nine seconds to read; doing that per request would make the
page unusable, and the data is a static snapshot anyway.

Flask is an optional dependency — the router itself has none, and that is
worth keeping true. Install with ``pip install -e ".[web]"``.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from flask import Flask, jsonify, render_template, request

from ..gtfs.loader import format_time, load_feed, parse_date, parse_time
from ..routing.journey import Journey
from ..routing.places import build_places, resolve, search_places
from ..routing.raptor import plan
from ..routing.timetable import build_timetable
from ..routing.transfers import build_transfers
from .geocode import DEFAULT_ACCESS_WALK_M, Geocoder, StopIndex


def _window_label(start: date | None, end: date | None) -> str:
    """"4 Sep – 5 Oct 2026", or one date, or nothing."""
    if start is None or end is None:
        return ""
    if start.year == end.year:
        return f"{start.day} {start:%b} – {end.day} {end:%b} {end.year}"
    return f"{start.day} {start:%b} {start.year} – {end.day} {end:%b} {end.year}"


@dataclass
class Endpoint:
    """Where a journey starts or ends, and how to reach the network from it.

    A station and a street address are the same shape to the router: a set
    of stops with a walking cost attached. The difference is only that a
    station's platforms cost nothing to reach, because you are stood on one.
    """

    name: str
    lat: float
    lon: float
    stops: dict[str, int] = field(default_factory=dict)
    is_address: bool = False


def create_app(feed_path: str, max_walk: float = 400.0,
               access_walk: float = DEFAULT_ACCESS_WALK_M,
               geocoder=None) -> Flask:
    """Build the app. ``geocoder`` is injectable so the tests can supply a
    stub: a suite that reaches OpenStreetMap is slow, flaky, and rude."""
    app = Flask(__name__)

    started = time.perf_counter()
    feed = load_feed(feed_path)
    places = build_places(feed)
    transfers = build_transfers(feed, max_walk_m=max_walk)
    stop_index = StopIndex(feed, cell_m=access_walk)
    if geocoder is None:
        geocoder = Geocoder(feed)
    load_seconds = time.perf_counter() - started
    # Counted once. It is only a display figure, and walking every trip on
    # each request would be silly for something that cannot change.
    stop_time_count = sum(len(t.stop_ids) for t in feed.trips.values())

    # Timetables are per service date and cost ~0.4s to build, so they are
    # cached rather than rebuilt on every query.
    timetables: dict[date, object] = {}

    def timetable_for(day: date):
        if day not in timetables:
            tt = build_timetable(feed, on_date=day)
            tt.transfers = transfers
            timetables[day] = tt
        return timetables[day]

    window_start, window_end = feed.service_window

    def default_date() -> date:
        today = datetime.now(tz=UTC).astimezone().date()
        if feed.covers(today):
            return today
        # An expired snapshot would otherwise show an empty planner with no
        # explanation. Land on a day the feed actually covers.
        return window_start or today

    # ---------------------------------------------------------------- #
    # Resolving what the user typed
    # ---------------------------------------------------------------- #

    def as_endpoint(token: str) -> Endpoint | None:
        """A place id, stop id, or stop name — nothing that needs the network."""
        place = resolve(feed, places, token)
        if place is None:
            return None
        return Endpoint(name=place.name, lat=place.lat, lon=place.lon,
                        stops=dict.fromkeys(place.stop_ids, 0))

    def as_address(token: str) -> Endpoint | None:
        """Geocode, then find what can be walked to from there."""
        located = geocoder.lookup(token)
        if located is None:
            return None
        nearby = stop_index.walkable_from(located.lat, located.lon,
                                          radius_m=access_walk)
        if not nearby:
            return None
        return Endpoint(name=located.name, lat=located.lat, lon=located.lon,
                        stops=nearby, is_address=True)

    # ---------------------------------------------------------------- #
    # Turning results into JSON
    # ---------------------------------------------------------------- #

    def stop_point(stop_id: str) -> dict | None:
        stop = feed.stops.get(stop_id)
        if stop is None or math.isnan(stop.lat) or math.isnan(stop.lon):
            return None
        return {"id": stop.id, "name": stop.name, "lat": stop.lat, "lon": stop.lon}

    def stop_name(stop_id: str) -> str:
        stop = feed.stops.get(stop_id)
        return stop.name if stop else stop_id

    def route_style(trip_id: str) -> dict:
        """The agency's own colour and mode for the route a trip runs on."""
        trip = feed.trips.get(trip_id)
        route = feed.routes.get(trip.route_id) if trip else None
        if route is None:
            return {"color": "", "text_color": "", "mode": "bus"}
        return {"color": route.color, "text_color": route.text_color,
                "mode": route.mode}

    def leg_to_json(leg) -> dict:
        # Ride legs carry every stop they pass so the map can follow the
        # route; walking legs are just the two endpoints.
        ids = leg.stop_ids or [leg.from_stop, leg.to_stop]
        points = [p for p in (stop_point(s) for s in ids) if p]
        return {
            "kind": leg.kind,
            "from": stop_name(leg.from_stop),
            "to": stop_name(leg.to_stop),
            "_depart": leg.depart,
            "_arrive": leg.arrive,
            "route": leg.route_label,
            "headsign": leg.headsign,
            "stops": leg.intermediate_stops,
            "points": points,
            **(route_style(leg.trip_id) if leg.kind == "ride"
               else {"color": "", "text_color": "", "mode": "walk"}),
        }

    def walk_leg(from_point: dict, to_point: dict, depart: int, seconds: int) -> dict:
        return {
            "kind": "walk",
            "from": from_point["name"],
            "to": to_point["name"],
            "_depart": depart,
            "_arrive": depart + seconds,
            "route": "", "headsign": "", "stops": 0,
            "color": "", "text_color": "", "mode": "walk",
            "points": [from_point, to_point],
        }

    def merge_walks(legs: list[dict]) -> list[dict]:
        """Collapse back-to-back walking legs into one.

        The walk from an address to its nearest stop and the router's own
        footpath from that stop to a better one are two legs internally,
        but they are one continuous walk to a person: "walk 6 min to Stop
        3006, walk 2 min to Stop 3084" is a route nobody would follow.
        Only genuinely contiguous walks are merged — if there is a gap you
        really are waiting somewhere, and that should stay visible.
        """
        merged: list[dict] = []
        for leg in legs:
            previous = merged[-1] if merged else None
            if (previous and previous["kind"] == "walk" and leg["kind"] == "walk"
                    and previous["_arrive"] == leg["_depart"]):
                previous["to"] = leg["to"]
                previous["_arrive"] = leg["_arrive"]
                previous["points"] = [previous["points"][0], leg["points"][-1]]
            else:
                merged.append(leg)
        return merged

    def finish_leg(leg: dict) -> dict:
        """Swap the working second counts for what the page shows.

        Both a formatted string and the raw seconds go out: the page lets
        the reader switch between 24- and 12-hour clocks, and re-deriving
        that from "17:30" would mean parsing it back again.
        """
        depart, arrive = leg.pop("_depart"), leg.pop("_arrive")
        leg["depart"] = format_time(depart)[:5]
        leg["arrive"] = format_time(arrive)[:5]
        leg["depart_seconds"] = depart
        leg["arrive_seconds"] = arrive
        leg["minutes"] = max(1, (arrive - depart) // 60)
        return leg

    def journey_to_json(journey: Journey, query_time: int,
                        origin: Endpoint, destination: Endpoint) -> dict:
        legs = [leg_to_json(leg) for leg in journey.legs]
        extra_walk = 0
        depart, arrive = journey.depart, journey.arrive

        # The router's answer starts at a stop and ends at a stop. When
        # either end is an address, the walk to and from the door is part
        # of the journey and has to be shown and counted — otherwise a
        # 12-minute walk to the bus quietly disappears from the total.
        if origin.is_address:
            access = origin.stops.get(journey.legs[0].from_stop, 0)
            if access:
                start = {"id": "", "name": origin.name,
                         "lat": origin.lat, "lon": origin.lon}
                board = stop_point(journey.legs[0].from_stop)
                if board:
                    # Leaving as late as possible: nobody wants to be told
                    # to stand at a bus stop for twenty minutes.
                    depart = journey.depart - access
                    legs.insert(0, walk_leg(start, board, depart, access))
                    extra_walk += access

        if destination.is_address:
            egress = destination.stops.get(journey.legs[-1].to_stop, 0)
            if egress:
                alight = stop_point(journey.legs[-1].to_stop)
                end = {"id": "", "name": destination.name,
                       "lat": destination.lat, "lon": destination.lon}
                if alight:
                    legs.append(walk_leg(alight, end, journey.arrive, egress))
                    arrive += egress
                    extra_walk += egress

        legs = [finish_leg(leg) for leg in merge_walks(legs)]
        return {
            "depart": format_time(depart)[:5],
            "arrive": format_time(arrive)[:5],
            "depart_seconds": depart,
            "arrive_seconds": arrive,
            "total_minutes": max(0, arrive - query_time) // 60,
            "wait_minutes": max(0, depart - query_time) // 60,
            "transfers": journey.transfers,
            "walk_minutes": (journey.walking_seconds + extra_walk) // 60,
            "legs": legs,
        }

    # ------------------------------------------------------------------ #

    @app.route("/")
    def index():
        return render_template(
            "index.html",
            feed_name=feed_path.split("/")[-1].split("\\")[-1],
            stats={
                "stops": len(feed.stops),
                "places": len(places),
                "routes": len(feed.routes),
                "trips": len(feed.trips),
                "stop_times": stop_time_count,
                "load_seconds": round(load_seconds, 1),
            },
            window={
                "start": window_start.isoformat() if window_start else "",
                "end": window_end.isoformat() if window_end else "",
                "label": _window_label(window_start, window_end),
                # A snapshot from two years ago is not a bug, but a page that
                # quietly opens on a 2024 date looks like one. Saying which
                # it is turns a confusing date into a stated fact.
                "current": feed.covers(datetime.now(tz=UTC).astimezone().date()),
            },
            default_date=default_date().isoformat(),
        )

    @app.route("/healthz")
    def healthz():
        """Liveness check for the host, and the endpoint an uptime pinger
        hits to stop a free instance falling asleep. Deliberately cheap —
        the feed is already parsed by the time this can be reached."""
        return jsonify({
            "ok": True,
            "stops": len(feed.stops),
            "stop_times": stop_time_count,
            "service": [window_start.isoformat() if window_start else None,
                        window_end.isoformat() if window_end else None],
            "load_seconds": round(load_seconds, 1),
        })

    @app.route("/api/places")
    def api_places():
        """Autocomplete over stop names only.

        Addresses are deliberately not suggested here: geocoding fires on
        every keystroke, and Nominatim's terms allow one request a second.
        Addresses are resolved when the journey is planned instead.
        """
        query = request.args.get("q", "")
        found = search_places(places, query, limit=8)
        return jsonify([
            {"id": p.id, "name": p.name, "stops": p.platform_count}
            for p in found
        ])

    @app.route("/api/plan")
    def api_plan():
        origin_token = request.args.get("from", "").strip()
        dest_token = request.args.get("to", "").strip()
        at = request.args.get("at", "08:00")
        on = parse_date(request.args.get("date", "").replace("-", "")) or default_date()
        max_rounds = min(8, max(1, int(request.args.get("max_rounds", 5))))

        if not origin_token or not dest_token:
            return jsonify({"error": "Enter both a start and a destination."}), 400

        resolved: list[Endpoint] = []
        for token in (origin_token, dest_token):
            # Stop names first: they are local, instant, and what most
            # queries are. Only fall through to the geocoder when the feed
            # has nothing that could be meant.
            found = as_endpoint(token)
            if found is None:
                matches = search_places(places, token, limit=6)
                if matches:
                    return jsonify({
                        "error": f"“{token}” matches {len(matches)} places. Pick one:",
                        "suggestions": [{"id": p.id, "name": p.name} for p in matches],
                    }), 409
                found = as_address(token)
            if found is None:
                return jsonify({
                    "error": (f"Could not find “{token}” — not a stop in this "
                              f"feed, and not an address we could place near "
                              f"any stop."),
                }), 404
            resolved.append(found)

        origin, destination = resolved

        if origin.stops.keys() == destination.stops.keys():
            return jsonify({"error": "Start and destination are the same place."}), 400

        if not feed.covers(on):
            return jsonify({
                "error": (f"This feed has no service on {on}. It covers "
                          f"{window_start} to {window_end} — GTFS feeds are "
                          f"dated snapshots."),
            }), 400

        depart = parse_time(at)
        started_at = time.perf_counter()
        result = plan(timetable_for(on), origin.stops, destination.stops,
                      depart, max_rounds=max_rounds)
        elapsed_ms = (time.perf_counter() - started_at) * 1000

        if not result.journeys:
            return jsonify({
                "error": (f"No journey found from {origin.name} to "
                          f"{destination.name} after {at}. The last vehicle "
                          f"may have gone, or they may not be connected today."),
            }), 404

        return jsonify({
            "origin": {"name": origin.name, "lat": origin.lat, "lon": origin.lon,
                       "address": origin.is_address},
            "destination": {"name": destination.name, "lat": destination.lat,
                            "lon": destination.lon, "address": destination.is_address},
            "solved_ms": round(elapsed_ms),
            "stops_reached": result.stops_reached,
            "rounds": result.rounds_used,
            "journeys": [journey_to_json(j, depart, origin, destination)
                         for j in result.journeys],
        })

    return app
