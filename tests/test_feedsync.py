"""Tests for startup feed refresh.

Nothing here touches the network: the download is stubbed, because a test
suite that reaches OC Transpo is slow, flaky and rude.
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date
from pathlib import Path

import pytest

from transitrouter.web import feedsync


def _make_feed(path: Path, end: str) -> Path:
    """Minimal GTFS zip carrying only the dates feedsync reads."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["service_id", "monday", "start_date", "end_date"])
    writer.writerow(["WK", "1", "20260101", end])
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("calendar.txt", buf.getvalue())
    return path


def test_reads_end_date(tmp_path):
    feed = _make_feed(tmp_path / "feed.zip", "20261231")
    assert feedsync.feed_end_date(feed) == date(2026, 12, 31)


def test_calendar_dates_extends_the_window(tmp_path):
    path = tmp_path / "feed.zip"
    _make_feed(path, "20260601")
    with zipfile.ZipFile(path, "a") as zf:
        zf.writestr("calendar_dates.txt",
                    "service_id,date,exception_type\nWK,20260915,1\n")
    assert feedsync.feed_end_date(path) == date(2026, 9, 15)


def test_unreadable_zip_reads_as_none(tmp_path):
    bad = tmp_path / "feed.zip"
    bad.write_bytes(b"not a zip")
    assert feedsync.feed_end_date(bad) is None


def test_current_feed_is_left_alone(tmp_path, monkeypatch):
    feed = _make_feed(tmp_path / "feed.zip", "20261231")
    before = feed.read_bytes()

    def explode(*_a, **_k):
        raise AssertionError("should not download a feed that is still valid")

    monkeypatch.setattr(feedsync, "_download", explode)
    out = feedsync.ensure_current_feed(feed, "http://example.invalid",
                                       today=date(2026, 12, 1))
    assert out == feed
    assert feed.read_bytes() == before


def test_expired_feed_is_replaced(tmp_path, monkeypatch):
    feed = _make_feed(tmp_path / "feed.zip", "20261025")

    def fake_download(url, dest):
        _make_feed(Path(dest), "20270131")

    monkeypatch.setattr(feedsync, "_download", fake_download)
    feedsync.ensure_current_feed(feed, "http://example.invalid",
                                 today=date(2026, 11, 7))
    assert feedsync.feed_end_date(feed) == date(2027, 1, 31)


def test_missing_feed_is_fetched(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "feed.zip"

    def fake_download(url, dest):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        _make_feed(Path(dest), "20270131")

    monkeypatch.setattr(feedsync, "_download", fake_download)
    feedsync.ensure_current_feed(target, "http://example.invalid",
                                 today=date(2026, 11, 7))
    assert target.exists()


def test_failed_refresh_keeps_the_stale_feed(tmp_path, monkeypatch):
    """An expired feed still serves the UI and stop search. Refusing to
    start would be the worse failure."""
    feed = _make_feed(tmp_path / "feed.zip", "20261025")

    def fail(*_a, **_k):
        raise OSError("network down")

    monkeypatch.setattr(feedsync, "_download", fail)
    out = feedsync.ensure_current_feed(feed, "http://example.invalid",
                                       today=date(2026, 11, 7))
    assert out == feed
    assert feedsync.feed_end_date(feed) == date(2026, 10, 25)


def test_failed_refresh_with_no_feed_raises(tmp_path, monkeypatch):
    def fail(*_a, **_k):
        raise OSError("network down")

    monkeypatch.setattr(feedsync, "_download", fail)
    with pytest.raises(OSError):
        feedsync.ensure_current_feed(tmp_path / "missing.zip",
                                     "http://example.invalid",
                                     today=date(2026, 11, 7))
