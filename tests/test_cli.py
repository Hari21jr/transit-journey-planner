"""Tests for the command-line interface and the loader's edge cases."""

from __future__ import annotations

import zipfile

import pytest

from transitrouter.cli import build_parser, main
from transitrouter.gtfs.loader import GtfsError, load_feed


@pytest.fixture
def feed_zip(sample_feed_dir, tmp_path):
    """The same sample network, zipped — the shape agencies actually ship."""
    target = tmp_path / "feed.zip"
    with zipfile.ZipFile(target, "w") as zf:
        for f in sample_feed_dir.iterdir():
            zf.write(f, f.name)
    return target


# --------------------------------------------------------------------- #
# Loader edge cases

def test_loads_from_a_zip(feed_zip):
    feed = load_feed(feed_zip)
    assert len(feed.stops) == 7


def test_loads_from_a_zip_with_a_containing_folder(sample_feed_dir, tmp_path):
    """Feeds are often zipped with a wrapping directory. Handle both."""
    target = tmp_path / "nested.zip"
    with zipfile.ZipFile(target, "w") as zf:
        for f in sample_feed_dir.iterdir():
            zf.write(f, f"gtfs/{f.name}")
    assert len(load_feed(target).stops) == 7


def test_missing_feed_raises(tmp_path):
    with pytest.raises(GtfsError, match="no such feed"):
        load_feed(tmp_path / "absent.zip")


def test_incomplete_feed_names_what_is_missing(tmp_path):
    (tmp_path / "stops.txt").write_text("stop_id,stop_name,stop_lat,stop_lon\n")
    with pytest.raises(GtfsError, match="missing required files"):
        load_feed(tmp_path)


def test_untimed_stops_are_dropped(sample_feed_dir):
    """A stop_time with no arrival or departure cannot be routed through."""
    path = sample_feed_dir / "stop_times.txt"
    path.write_text(path.read_text() + "R1-0,,,B,99\n")
    feed = load_feed(sample_feed_dir)
    assert len(feed.trips["R1-0"].stop_ids) == 3


def test_stop_times_for_unknown_stops_are_ignored(sample_feed_dir):
    path = sample_feed_dir / "stop_times.txt"
    path.write_text(path.read_text() + "R1-0,09:00:00,09:00:00,NOPE,98\n")
    feed = load_feed(sample_feed_dir)
    assert "NOPE" not in feed.trips["R1-0"].stop_ids


# --------------------------------------------------------------------- #
# CLI

def test_parser_requires_a_subcommand():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_info_reports_feed_shape(feed_zip, capsys):
    assert main(["info", str(feed_zip), "--quiet"]) == 0
    out = capsys.readouterr().out
    assert "stops         7" in out
    assert "patterns" in out


def test_stops_search_finds_by_name(feed_zip, capsys):
    assert main(["stops", str(feed_zip), "--search", "charlie"]) == 0
    out = capsys.readouterr().out
    assert "Charlie Hub" in out
    assert "Charlie Hub West" in out


def test_stops_search_reports_no_matches(feed_zip, capsys):
    assert main(["stops", str(feed_zip), "--search", "nowhere"]) == 1
    assert "no matches" in capsys.readouterr().out


def test_plan_produces_an_itinerary(feed_zip, capsys):
    code = main(["plan", str(feed_zip), "--from", "A", "--to", "E",
                 "--at", "08:00", "--weekday", "0", "--quiet"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Alpha Station" in out and "Echo Terminal" in out
    assert "transfer" in out


def test_plan_accepts_a_stop_name_instead_of_an_id(feed_zip, capsys):
    code = main(["plan", str(feed_zip), "--from", "Alpha", "--to", "Echo",
                 "--at", "08:00", "--weekday", "0", "--quiet"])
    assert code == 0
    assert "Echo Terminal" in capsys.readouterr().out


def test_plan_rejects_an_ambiguous_name(feed_zip, capsys):
    """'Charlie' matches two stops — guessing one would be worse than asking."""
    code = main(["plan", str(feed_zip), "--from", "Alpha", "--to", "Charlie",
                 "--at", "08:00", "--weekday", "0", "--quiet"])
    assert code == 1
    assert "ambiguous" in capsys.readouterr().err


def test_plan_rejects_an_unknown_stop(feed_zip, capsys):
    code = main(["plan", str(feed_zip), "--from", "Alpha", "--to", "Atlantis",
                 "--at", "08:00", "--weekday", "0", "--quiet"])
    assert code == 1
    assert "no stop matches" in capsys.readouterr().err


def test_plan_rejects_identical_endpoints(feed_zip, capsys):
    code = main(["plan", str(feed_zip), "--from", "A", "--to", "A",
                 "--at", "08:00", "--quiet"])
    assert code == 1
    assert "same place" in capsys.readouterr().err


def test_plan_reports_when_nothing_is_reachable(feed_zip, capsys):
    code = main(["plan", str(feed_zip), "--from", "A", "--to", "Z",
                 "--at", "08:00", "--weekday", "0", "--quiet"])
    assert code == 1
    assert "No journey found" in capsys.readouterr().out


def test_bad_feed_path_exits_cleanly(tmp_path, capsys):
    """A missing feed should be an error message, not a traceback."""
    code = main(["info", str(tmp_path / "gone.zip"), "--quiet"])
    assert code == 2
    assert "feed error" in capsys.readouterr().err


def test_benchmark_runs(feed_zip, capsys):
    code = main(["benchmark", str(feed_zip), "--queries", "5",
                 "--weekday", "0", "--quiet"])
    assert code == 0
    out = capsys.readouterr().out
    assert "median" in out and "p95" in out


# --------------------------------------------------------------------- #
# Station grouping and calendar exceptions

def test_places_group_platforms_under_a_parent_station(tmp_path):
    """A station's platforms must resolve as one place, not compete as many."""
    from transitrouter.gtfs.loader import load_feed
    from transitrouter.routing.places import build_places, resolve

    d = tmp_path / "feed"
    d.mkdir()
    (d / "stops.txt").write_text(
        "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
        "HUR,Hurdman Station,45.4120,-75.6650,1,\n"
        "HUR1,Hurdman Platform 1,45.4121,-75.6651,0,HUR\n"
        "HUR2,Hurdman Platform 2,45.4122,-75.6652,0,HUR\n"
        "HUR3,Hurdman Platform 3,45.4123,-75.6653,0,HUR\n"
    )
    (d / "routes.txt").write_text("route_id,route_short_name,route_long_name,route_type\nR,1,One,3\n")
    (d / "trips.txt").write_text("trip_id,route_id,service_id\nT,R,S\n")
    (d / "stop_times.txt").write_text(
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T,08:00:00,08:00:00,HUR1,0\nT,08:10:00,08:10:00,HUR2,1\n"
    )

    feed = load_feed(d)
    places = build_places(feed)
    hurdman = resolve(feed, places, "Hurdman Station")
    assert hurdman is not None
    assert hurdman.platform_count == 3
    assert set(hurdman.stop_ids) == {"HUR1", "HUR2", "HUR3"}


def test_a_raw_stop_id_resolves_to_its_place(tmp_path, sample_feed_dir):
    from transitrouter.gtfs.loader import load_feed
    from transitrouter.routing.places import build_places, resolve

    feed = load_feed(sample_feed_dir)
    places = build_places(feed)
    assert resolve(feed, places, "A").stop_ids == ["A"]


def test_same_name_stops_far_apart_stay_separate(tmp_path):
    """Feeds reuse street names down a whole corridor. Distance splits them."""
    from transitrouter.gtfs.loader import load_feed
    from transitrouter.routing.places import build_places

    d = tmp_path / "feed"
    d.mkdir()
    (d / "stops.txt").write_text(
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "N1,Bank,45.4000,-75.6900\n"
        "N2,Bank,45.4001,-75.6901\n"       # ~14 m away — same place
        "F1,Bank,45.4300,-75.6900\n"       # ~3.3 km away — different place
    )
    (d / "routes.txt").write_text("route_id,route_short_name,route_long_name,route_type\nR,1,One,3\n")
    (d / "trips.txt").write_text("trip_id,route_id,service_id\nT,R,S\n")
    (d / "stop_times.txt").write_text(
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T,08:00:00,08:00:00,N1,0\nT,08:10:00,08:10:00,F1,1\n"
    )

    places = build_places(load_feed(d))
    sizes = sorted(p.platform_count for p in places.values())
    assert sizes == [1, 2]


def test_calendar_dates_removes_service_on_a_holiday(sample_feed_dir):
    """A statutory holiday cancels the weekday service. Honour it."""
    from datetime import date

    from transitrouter.gtfs.loader import load_feed
    from transitrouter.routing.timetable import build_timetable

    (sample_feed_dir / "calendar_dates.txt").write_text(
        "service_id,date,exception_type\nWEEKDAY,20260707,2\n"
    )
    feed = load_feed(sample_feed_dir)

    normal = build_timetable(feed, on_date=date(2026, 7, 6))    # Monday
    holiday = build_timetable(feed, on_date=date(2026, 7, 7))   # Tuesday, cancelled
    assert sum(len(r) for r in normal.routes) == 13
    assert sum(len(r) for r in holiday.routes) == 0


def test_calendar_dates_adds_service_on_an_extra_day(sample_feed_dir):
    from datetime import date

    from transitrouter.gtfs.loader import load_feed
    from transitrouter.routing.timetable import build_timetable

    (sample_feed_dir / "calendar_dates.txt").write_text(
        "service_id,date,exception_type\nWEEKDAY,20260711,1\n"
    )
    feed = load_feed(sample_feed_dir)
    saturday = build_timetable(feed, on_date=date(2026, 7, 11))
    # The weekday trips are added on top of the normal weekend one.
    assert sum(len(r) for r in saturday.routes) == 14


def test_service_dates_respect_the_feed_validity_window(sample_feed_dir):
    """A date outside start_date..end_date runs nothing."""
    from datetime import date

    from transitrouter.gtfs.loader import load_feed
    from transitrouter.routing.timetable import build_timetable

    feed = load_feed(sample_feed_dir)
    expired = build_timetable(feed, on_date=date(2030, 1, 7))
    assert sum(len(r) for r in expired.routes) == 0


def test_plan_accepts_an_explicit_date(feed_zip, capsys):
    code = main(["plan", str(feed_zip), "--from", "A", "--to", "E",
                 "--at", "08:00", "--date", "2026-07-06", "--quiet"])
    assert code == 0
    assert "2026-07-06" in capsys.readouterr().out


def test_plan_rejects_an_unreadable_date(feed_zip, capsys):
    code = main(["plan", str(feed_zip), "--from", "A", "--to", "E",
                 "--date", "next tuesday", "--quiet"])
    assert code == 2
    assert "unreadable date" in capsys.readouterr().err


def test_info_reports_the_feed_validity_window(feed_zip, capsys):
    assert main(["info", str(feed_zip), "--quiet"]) == 0
    out = capsys.readouterr().out
    assert "valid         2026-01-01 to 2027-12-31" in out


def test_expired_feed_explains_itself_instead_of_saying_no_route(feed_zip, capsys):
    """A dated snapshot returning nothing looks like a broken router.

    The sample feed's calendar ends in 2027, so routing in 2030 must say the
    feed has expired and name the window, not shrug.
    """
    code = main(["plan", str(feed_zip), "--from", "A", "--to", "E",
                 "--at", "08:00", "--date", "2030-01-07", "--quiet"])
    assert code == 1
    out = capsys.readouterr().out
    assert "no service on 2030-01-07" in out
    assert "2026-01-01" in out and "2027-12-31" in out


# --------------------------------------------------------------------- #
# Platform-suffix grouping, modelled on OC Transpo's real naming

def _write_feed(d, stops_csv):
    d.mkdir(parents=True, exist_ok=True)
    (d / "stops.txt").write_text(stops_csv)
    (d / "routes.txt").write_text(
        "route_id,route_short_name,route_long_name,route_type\nR,1,One,3\n")
    (d / "trips.txt").write_text("trip_id,route_id,service_id\nT,R,S\n")
    first = stops_csv.splitlines()[1].split(",")[0]
    second = stops_csv.splitlines()[2].split(",")[0]
    (d / "stop_times.txt").write_text(
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        f"T,08:00:00,08:00:00,{first},0\nT,08:10:00,08:10:00,{second},1\n")
    return d


def test_lettered_platforms_group_into_one_station(tmp_path):
    """OC Transpo names platforms HURDMAN A..E and sets no parent_station.

    All five are the same station to anyone travelling. Left ungrouped,
    'from Hurdman' is ambiguous between places that are not actually distinct.
    """
    from transitrouter.gtfs.loader import load_feed
    from transitrouter.routing.places import build_places, resolve

    d = _write_feed(tmp_path / "feed",
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "AF910,HURDMAN A,45.41219,-75.66504\n"
        "AF920,HURDMAN B,45.41214,-75.66556\n"
        "AF930,HURDMAN C,45.41211,-75.66632\n"
        "AF940,HURDMAN D,45.41208,-75.66689\n"
        "AF950,HURDMAN E,45.41204,-75.66721\n")

    feed = load_feed(d)
    places = build_places(feed)
    hurdman = resolve(feed, places, "Hurdman")
    assert hurdman is not None, "Hurdman should resolve to a single place"
    assert hurdman.platform_count == 5
    assert "A" not in hurdman.name.split()[-1:], "should not be named after one platform"


def test_different_corners_of_one_street_stay_separate(tmp_path):
    """RIDEAU / FRIEL and RIDEAU / NELSON are different places, not platforms.

    This is the failure mode in the other direction: over-eager grouping
    would merge an entire street into one stop.
    """
    from transitrouter.gtfs.loader import load_feed
    from transitrouter.routing.places import build_places

    d = _write_feed(tmp_path / "feed",
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "CD080,RIDEAU / FRIEL,45.42900,-75.66900\n"
        "CD100,RIDEAU / NELSON,45.42800,-75.67300\n"
        "CD040,RIDEAU / AUGUSTA,45.42850,-75.67100\n")

    places = build_places(load_feed(d))
    assert len(places) == 3


def test_platform_stripping_is_conservative():
    """Stripping must never reduce a name to something meaningless."""
    from transitrouter.routing.places import normalise_name

    assert normalise_name("HURDMAN A") == "hurdman"
    assert normalise_name("TUNNEY'S PASTURE 2") == "tunney's pasture"
    assert normalise_name("BLAIR PLATFORM 3") == "blair"
    assert normalise_name("RIDEAU / FRIEL") == "rideau / friel"
    # Too short to strip — "MAIN" must not become "M".
    assert normalise_name("MAIN") == "main"


def test_far_apart_lettered_stops_are_not_merged(tmp_path):
    """Sharing a stem is not enough; they must also be close together."""
    from transitrouter.gtfs.loader import load_feed
    from transitrouter.routing.places import build_places

    d = _write_feed(tmp_path / "feed",
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "X1,BANK A,45.40000,-75.69000\n"
        "X2,BANK B,45.43000,-75.69000\n")   # ~3.3 km apart

    places = build_places(load_feed(d))
    assert len(places) == 2


def test_a_bare_number_is_only_stripped_from_a_substantial_name():
    """'Stop 12' is a whole name; 'TUNNEY'S PASTURE 2' is a platform.

    Stripping bare numbers indiscriminately collapses every numbered stop in
    a feed into a single place — which is exactly what happened before this
    guard existed.
    """
    from transitrouter.routing.places import normalise_name

    assert normalise_name("Stop 12") == "stop 12"
    assert normalise_name("Bay 7") == "bay 7"
    assert normalise_name("TUNNEY'S PASTURE 2") == "tunney's pasture"
    assert normalise_name("BLAIR PLATFORM 3") == "blair"
    assert normalise_name("HURDMAN A") == "hurdman"


def test_numbered_stops_stay_separate_places(tmp_path):
    from transitrouter.gtfs.loader import load_feed
    from transitrouter.routing.places import build_places

    d = _write_feed(tmp_path / "feed",
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "S10,Stop 10,45.40000,-75.69000\n"
        "S11,Stop 11,45.40010,-75.69010\n"
        "S12,Stop 12,45.40020,-75.69020\n")

    places = build_places(load_feed(d))
    assert len(places) == 3, "numbered stops must not collapse into one place"
