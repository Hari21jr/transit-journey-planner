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
    assert "same stop" in capsys.readouterr().err


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
