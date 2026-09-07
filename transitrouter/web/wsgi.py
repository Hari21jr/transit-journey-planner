"""WSGI entry point for a hosted deployment.

``journey serve`` runs Flask's development server, which is right for a
laptop and wrong for anything public. In a container a WSGI server such as
gunicorn imports ``app`` from here instead.

The feed path comes from the environment because a container has no command
line to pass it on:

    FEED_PATH=/data/feed.zip gunicorn transitrouter.web.wsgi:app

Loading happens at import, before the port is bound, so the service is not
reachable until it can actually answer. Parsing a few million stop times
takes seconds, and a host that starts sending traffic mid-parse would get
errors rather than a slow first response.
"""

from __future__ import annotations

import os

from .app import create_app

FEED_PATH = os.environ.get("FEED_PATH", "feed.zip")
MAX_WALK = float(os.environ.get("MAX_WALK_M", "400"))
ACCESS_WALK = float(os.environ.get("ACCESS_WALK_M", "800"))

app = create_app(FEED_PATH, max_walk=MAX_WALK, access_walk=ACCESS_WALK)
