"""Tests for the web front end."""

from __future__ import annotations

import pytest

flask = pytest.importorskip("flask", reason="web extra not installed")

from transitrouter.web.app import create_app  # noqa: E402
from transitrouter.web.geocode import Location  # noqa: E402


class StubGeocoder:
    """Stands in for OpenStreetMap. A suite that hits the live geocoder is
    slow, flaky, offline-hostile, and an abuse of a free service."""

    def __init__(self, known: dict[str, tuple[float, float]]):
        self.known = {k.lower(): v for k, v in known.items()}
        self.calls: list[str] = []

    def lookup(self, query: str):
        self.calls.append(query)
        found = self.known.get(" ".join(query.lower().split()))
        if found is None:
            return None
        return Location(name=query, lat=found[0], lon=found[1])


# 12 Bravo Lane sits ~50m from stop B; Far Farm is nowhere near anything.
ADDRESSES = {
    "12 Bravo Lane": (45.4253, -75.6903),
    "Far Farm Road": (46.9000, -74.0000),
}


@pytest.fixture
def geocoder():
    return StubGeocoder(ADDRESSES)


@pytest.fixture
def client(sample_feed_dir, geocoder):
    app = create_app(str(sample_feed_dir), geocoder=geocoder)
    app.config["TESTING"] = True
    return app.test_client()


def test_index_renders_with_feed_stats(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Transit Journey Planner" in body
    assert "stop times" in body


def test_index_defaults_to_a_date_the_feed_covers(client):
    """An expired snapshot must not open on a day with no service."""
    body = client.get("/").data.decode()
    assert 'value="2026-01-01"' in body or 'value="20' in body
    assert 'min="2026-01-01"' in body
    assert 'max="2027-12-31"' in body


def test_places_autocomplete(client):
    data = client.get("/api/places?q=charlie").get_json()
    assert any("Charlie" in p["name"] for p in data)


def test_places_autocomplete_is_empty_for_a_blank_query(client):
    assert client.get("/api/places?q=").get_json() == []


def test_plan_returns_journeys(client):
    data = client.get("/api/plan?from=A&to=E&at=08:00&date=2026-01-05").get_json()
    assert data["journeys"], "expected at least one itinerary"
    best = data["journeys"][0]
    assert best["depart"] == "08:00"
    assert best["transfers"] == 1
    assert best["legs"][0]["kind"] == "ride"


def test_plan_includes_map_points_for_every_leg(client):
    """The map draws polylines from these; a leg without them is invisible."""
    data = client.get("/api/plan?from=A&to=E&at=08:00&date=2026-01-05").get_json()
    for journey in data["journeys"]:
        for leg in journey["legs"]:
            assert len(leg["points"]) >= 2
            assert all("lat" in p and "lon" in p for p in leg["points"])


def test_ride_legs_carry_intermediate_stops(client):
    """A straight line from board to alight would cut across the city."""
    data = client.get("/api/plan?from=A&to=C&at=08:00&date=2026-01-05").get_json()
    ride = next(leg for leg in data["journeys"][0]["legs"] if leg["kind"] == "ride")
    assert [p["id"] for p in ride["points"]] == ["A", "B", "C"]


def test_plan_reports_a_wait(client):
    data = client.get("/api/plan?from=A&to=E&at=08:00&date=2026-01-05").get_json()
    direct = next(j for j in data["journeys"] if j["transfers"] == 0)
    assert direct["total_minutes"] == 55


def test_ambiguous_name_returns_suggestions(client):
    resp = client.get("/api/plan?from=Alpha&to=Charlie&at=08:00&date=2026-01-05")
    assert resp.status_code == 409
    data = resp.get_json()
    assert data["suggestions"]
    assert any("Charlie" in s["name"] for s in data["suggestions"])


def test_unknown_place_is_a_404(client):
    resp = client.get("/api/plan?from=Alpha&to=Atlantis&at=08:00&date=2026-01-05")
    assert resp.status_code == 404
    assert "Could not find" in resp.get_json()["error"]


# --- planning from a street address ------------------------------------ #

def test_an_address_is_geocoded_and_routed_from(client):
    """A house number is not in any GTFS feed, so this is the only path
    by which "plan from where I live" can work at all."""
    data = client.get(
        "/api/plan?from=12 Bravo Lane&to=Echo Terminal&at=08:00&date=2026-01-05"
    ).get_json()
    assert data["origin"]["address"] is True
    assert data["journeys"]


def test_the_walk_from_the_door_is_shown_and_counted(client):
    """Silently dropping the access walk understates every journey."""
    data = client.get(
        "/api/plan?from=12 Bravo Lane&to=Echo Terminal&at=08:00&date=2026-01-05"
    ).get_json()
    first = data["journeys"][0]["legs"][0]
    assert first["kind"] == "walk"
    assert first["from"] == "12 Bravo Lane"
    assert data["journeys"][0]["walk_minutes"] >= first["minutes"]


def test_the_journey_starts_when_you_leave_the_house(client):
    """The departure shown has to be when you set off, not when the bus
    goes — otherwise you leave on time and miss it."""
    data = client.get(
        "/api/plan?from=12 Bravo Lane&to=Echo Terminal&at=08:00&date=2026-01-05"
    ).get_json()
    journey = data["journeys"][0]
    walk, ride = journey["legs"][0], journey["legs"][1]
    assert journey["depart"] == walk["depart"]
    assert walk["arrive"] <= ride["depart"]


def test_stop_names_never_reach_the_geocoder(client, geocoder):
    """Geocoding a name the feed already knows would be a needless network
    round trip on the commonest query there is."""
    client.get("/api/plan?from=Alpha&to=Echo Terminal&at=08:00&date=2026-01-05")
    assert geocoder.calls == []


def test_an_address_with_no_stops_near_it_is_a_404(client):
    resp = client.get(
        "/api/plan?from=Far Farm Road&to=Echo Terminal&at=08:00&date=2026-01-05"
    )
    assert resp.status_code == 404


def test_the_planner_survives_the_geocoder_being_down(sample_feed_dir):
    """Nominatim going down should cost the address feature, not the app."""
    class Dead:
        def lookup(self, query):
            raise TimeoutError("nominatim is unreachable")

    app = create_app(str(sample_feed_dir), geocoder=Dead())
    app.config["TESTING"] = True
    resp = app.test_client().get(
        "/api/plan?from=Alpha&to=Echo Terminal&at=08:00&date=2026-01-05"
    )
    assert resp.status_code == 200, "a stop-name query must not touch it"


def test_missing_endpoints_are_rejected(client):
    resp = client.get("/api/plan?from=&to=&at=08:00")
    assert resp.status_code == 400


def test_identical_endpoints_are_rejected(client):
    resp = client.get("/api/plan?from=A&to=A&at=08:00&date=2026-01-05")
    assert resp.status_code == 400
    assert "same place" in resp.get_json()["error"]


def test_date_outside_the_feed_window_explains_itself(client):
    resp = client.get("/api/plan?from=A&to=E&at=08:00&date=2030-01-07")
    assert resp.status_code == 400
    error = resp.get_json()["error"]
    assert "no service on 2030-01-07" in error
    assert "2026-01-01" in error


def test_unreachable_destination_is_a_404(client):
    resp = client.get("/api/plan?from=A&to=Z&at=08:00&date=2026-01-05")
    assert resp.status_code == 404
    assert "No journey found" in resp.get_json()["error"]


def test_transfer_limit_is_honoured(client):
    data = client.get(
        "/api/plan?from=A&to=E&at=08:00&date=2026-01-05&max_rounds=1"
    ).get_json()
    assert all(j["transfers"] == 0 for j in data["journeys"])


def test_back_to_back_walks_are_shown_as_one(client):
    """The walk from the door and the router's own footpath off that stop
    are one continuous walk to a person, not two instructions."""
    data = client.get(
        "/api/plan?from=12 Bravo Lane&to=Echo Terminal&at=08:00&date=2026-01-05"
    ).get_json()
    for journey in data["journeys"]:
        kinds = [leg["kind"] for leg in journey["legs"]]
        pairs = list(zip(kinds, kinds[1:], strict=False))
        assert ("walk", "walk") not in pairs, f"consecutive walking legs in {kinds}"


def test_a_merged_walk_still_ends_where_the_bus_is(client):
    data = client.get(
        "/api/plan?from=12 Bravo Lane&to=Echo Terminal&at=08:00&date=2026-01-05"
    ).get_json()
    journey = data["journeys"][0]
    walk = journey["legs"][0]
    boarding = next(leg for leg in journey["legs"] if leg["kind"] == "ride")
    assert walk["to"] == boarding["from"]
    assert walk["points"][-1]["name"] == boarding["from"]


# --- presentation the page depends on ---------------------------------- #

def test_every_time_ships_as_seconds_too(client):
    """The 12/24-hour switch re-renders from these; parsing "17:30" back
    out of the string would be silly and would lose times past midnight."""
    data = client.get("/api/plan?from=A&to=E&at=08:00&date=2026-01-05").get_json()
    for journey in data["journeys"]:
        assert isinstance(journey["depart_seconds"], int)
        assert isinstance(journey["arrive_seconds"], int)
        for leg in journey["legs"]:
            assert isinstance(leg["depart_seconds"], int)
            assert leg["arrive_seconds"] >= leg["depart_seconds"]


def test_ride_legs_carry_the_agency_colour_and_mode(client):
    data = client.get("/api/plan?from=A&to=E&at=08:00&date=2026-01-05").get_json()
    ride = next(leg for leg in data["journeys"][0]["legs"] if leg["kind"] == "ride")
    assert ride["mode"] in {"bus", "rail", "metro", "tram", "ferry"}
    assert "color" in ride


def test_the_page_says_whether_the_schedule_is_current(client):
    """A snapshot from two years ago is not a bug, but a page that quietly
    opens on a 2024 date looks like one."""
    body = client.get("/").data.decode()
    assert "Schedule current" in body or "Schedule snapshot" in body
