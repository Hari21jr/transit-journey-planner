# Runtime image for the hosted web planner.
#
# The feed is downloaded at build time rather than committed: it is ~55MB
# of CSV that changes every few weeks, and a repository is the wrong place
# for either the size or the churn. Rebuilding the image is how the
# deployment gets a current schedule.

FROM python:3.12-slim

# Fail loudly and early rather than serving a half-built image.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

WORKDIR /app

# OC Transpo's current GTFS feed, via the Mobility Database. Override at
# build time for a different agency:
#   docker build --build-arg FEED_URL=https://.../toronto.zip .
ARG FEED_URL=https://files.mobilitydatabase.org/mdb-2154/mdb-2154-202609050023/mdb-2154-202609050023.zip

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Dependencies first, so a code change does not re-download the feed or
# reinstall the world.
COPY pyproject.toml README.md ./
COPY transitrouter ./transitrouter
RUN pip install --no-cache-dir ".[web]" gunicorn

RUN mkdir -p /data \
 && curl -fsSL --retry 3 "$FEED_URL" -o /data/feed.zip \
 && test -s /data/feed.zip

ENV FEED_PATH=/data/feed.zip \
    PYTHONUNBUFFERED=1 \
    PORT=8000

# One worker, several threads. Each worker holds its own copy of the feed —
# around 160MB for Ottawa — so a second one would not fit in a small
# container. Queries are milliseconds of CPU, so threads are the right way
# to handle concurrency here.
#
# The long timeout covers startup: parsing a few million stop times takes
# around ten seconds, and gunicorn's default 30s worker timeout is close
# enough to that to be worth raising.
CMD exec gunicorn transitrouter.web.wsgi:app \
    --bind "0.0.0.0:${PORT}" \
    --workers 1 --threads 8 --worker-class gthread \
    --timeout 120 --graceful-timeout 30 \
    --access-logfile - --error-logfile -
