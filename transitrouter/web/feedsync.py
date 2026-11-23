"""Keep the deployed feed current.

A GTFS feed is a dated snapshot. OC Transpo's covers roughly a month, and
once the end of that window passes every journey request fails with "this
feed has no service on <today>" — the service is up, but useless.

The feed used to arrive at image build time, which meant the deployment
only refreshed when something happened to trigger a rebuild. Nothing did,
and the planner served an expired schedule for two weeks.

This module moves the decision to startup: look at the feed on disk, and
if it is missing or its service window has already ended, fetch a current
one. The check is deliberately cheap — it reads the date columns out of
the zip rather than parsing the feed — so a warm start pays almost
nothing for it.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import tempfile
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

# Always serves the current export rather than a dated snapshot.
DEFAULT_FEED_URL = (
    "https://oct-gtfs-emasagcnfmcgeham.z01.azurefd.net/public-access/GTFSExport.zip"
)

_DOWNLOAD_TIMEOUT_S = 120


def _parse_gtfs_date(raw: str) -> date | None:
    raw = (raw or "").strip()
    if len(raw) != 8 or not raw.isdigit():
        return None
    try:
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    except ValueError:
        return None


def feed_end_date(path: Path) -> date | None:
    """Last date the feed claims to cover, or None if it cannot be read.

    Reads calendar.txt and calendar_dates.txt only. A corrupt or
    unreadable zip returns None, which callers treat as "replace it".
    """
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            latest: date | None = None

            for member, column in (("calendar.txt", "end_date"),
                                   ("calendar_dates.txt", "date")):
                if member not in names:
                    continue
                with zf.open(member) as fh:
                    reader = csv.DictReader(io.TextIOWrapper(fh, "utf-8-sig"))
                    for row in reader:
                        parsed = _parse_gtfs_date(row.get(column, ""))
                        if parsed and (latest is None or parsed > latest):
                            latest = parsed
            return latest
    except (OSError, zipfile.BadZipFile, KeyError, UnicodeDecodeError):
        return None


def _download(url: str, dest: Path) -> None:
    """Fetch to a temporary file first, then move into place.

    A half-written feed is worse than a stale one: the stale feed at least
    answers requests inside its window, while a truncated zip fails to
    parse and the service will not start at all.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), suffix=".partial")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with (
            urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_S) as resp,
            tmp.open("wb") as out,
        ):
            while chunk := resp.read(1 << 20):
                out.write(chunk)
        if tmp.stat().st_size == 0:
            raise OSError("downloaded feed was empty")
        if feed_end_date(tmp) is None:
            raise OSError("downloaded file is not a readable GTFS zip")
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


def ensure_current_feed(path: str | Path, url: str | None = None,
                        today: date | None = None) -> Path:
    """Return a feed path whose service window has not already ended.

    Downloads only when the feed is missing, unreadable, or expired. If a
    download fails and some feed is already on disk, that one is kept and
    the error is logged: an expired feed still serves the UI, map and stop
    search, which is a better failure than refusing to start.
    """
    path = Path(path)
    url = url or os.environ.get("FEED_URL") or DEFAULT_FEED_URL
    today = today or date.today()

    end = feed_end_date(path) if path.exists() else None

    if end is not None and end >= today:
        log.info("feed at %s covers through %s, keeping it", path, end)
        return path

    reason = "missing" if end is None else f"expired on {end}"
    log.warning("feed %s (%s); downloading from %s", path, reason, url)

    try:
        _download(url, path)
    except Exception as exc:  # noqa: BLE001 - startup must not hard-fail
        if path.exists():
            log.error("feed refresh failed (%s); continuing with the "
                      "existing feed", exc)
            return path
        raise

    log.info("feed refreshed; now covers through %s", feed_end_date(path))
    return path
