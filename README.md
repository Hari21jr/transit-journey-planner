# Transit Journey Planner

Plans public transit journeys over any GTFS feed using RAPTOR, returning
several itineraries that trade travel time against number of transfers.
No routing library — the algorithm is the project.

[![CI](https://github.com/Hari21jr/transit-journey-planner/actions/workflows/ci.yml/badge.svg)](https://github.com/Hari21jr/transit-journey-planner/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Coverage](https://img.shields.io/badge/coverage-92%25-brightgreen)
![Dependencies](https://img.shields.io/badge/runtime%20deps-none-brightgreen)

```
$ journey plan feed.zip --from S2929 --to S5195 --at 08:00

Stop 2929 → Stop 5195
departing after 08:00:00 · solved in 5ms · 608 stops reached in 5 rounds

  [fastest] 08:03:26 → 09:19:21  (75 min, 2 transfers, 4 min walking)
    08:03:26  route 157 to Stop 4645 from Stop 2929 (1 stops)
    08:06:02  arrive Stop 3007
    08:06:02  walk 4 min to Stop 3084
    08:14:29  route 136 to Stop 3107 from Stop 3084 (5 stops)
    08:22:04  arrive Stop 3089
    08:27:57  route 9 to Stop 5507 from Stop 3089 (27 stops)
    09:19:21  arrive Stop 5195

  [fewer changes] 08:00:00 → 09:45:53  (105 min, 1 transfer, 8 min walking)
    08:00:00  walk 4 min to Stop 2928
    08:17:25  route 133 to Stop 5112 from Stop 2928 (28 stops)
    09:14:27  arrive Stop 5112
    09:14:27  walk 4 min to Stop 5190
    09:36:35  route 70 to Stop 5198 from Stop 5190 (5 stops)
    09:45:53  arrive Stop 5195
```

## Why RAPTOR and not Dijkstra

Dijkstra is the reflex for shortest paths, and it fits transit badly. The
cost of "take this bus" depends entirely on when you arrive at the stop, so
the graph has to be expanded over time — one node per stop per departure —
and it becomes enormous.

RAPTOR doesn't build a graph at all. It works in rounds, where round *k* is
"the best you can do using at most *k* vehicles". Round 1 scans every route
reachable from the origin; round 2 scans every route reachable from anywhere
round 1 reached. Two things fall out of that:

- **It is naturally multi-criteria.** The answer is a set of journeys trading
  arrival time against transfers, rather than a single result produced by an
  arbitrary transfer penalty. Both itineraries above are optimal — one is
  30 minutes faster, the other has one fewer change.
- **Transfers are a loop bound, not a filter.** "At most two changes" is
  `max_rounds=3`, enforced during the search instead of by discarding results
  afterwards.

Reference: Delling, Pajor & Werneck, *Round-Based Public Transit Routing*
(Microsoft Research, 2012).

## The part that quietly breaks implementations

**A GTFS route is not a RAPTOR route.** RAPTOR requires every trip on a route
to visit exactly the same stops in the same order — that's what makes its
binary search for "the next trip I can catch" valid. GTFS routes don't work
that way: route 95 might have trips that short-turn, skip stops, or run a
different pattern on Sundays.

So trips are regrouped by their actual stop sequence before routing. One GTFS
route commonly becomes a dozen routing patterns. Skip this and the router
still runs, still returns journeys, and quietly boards you onto vehicles that
don't stop where it thinks they do.

## Other things worth knowing

**Times past midnight are preserved.** GTFS deliberately allows `25:10:00`,
meaning 1:10am belonging to the previous service day. Normalising that to
`01:10` would sort it before the evening trips and corrupt every overnight
journey. Times are stored as seconds since midnight and never wrapped.

**Walking transfers are generated, not just read.** Most feeds declare only a
handful of transfers, usually inside stations, and never mention the stop
across the street. Footpaths are generated for stop pairs within 400m using a
grid index — comparing all stops to all stops would be 36 million distance
calculations for a 6,000-stop feed. Agency-declared transfer times override
generated ones, since they know their own stations.

**Walking is not a transfer.** A journey that walks between two stops still
counts as one vehicle, which matters for the Pareto comparison.

## Quick start

```bash
git clone https://github.com/Hari21jr/transit-journey-planner.git
cd transit-journey-planner

python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux

pip install -e .
```

Then point it at a GTFS feed — a `.zip` or an unzipped directory. Ottawa's is
at [OC Transpo's developer portal](https://www.octranspo.com/en/plan-your-trip/travel-tools/developers/);
almost every agency publishes one, and hundreds are indexed at the
[Mobility Database](https://mobilitydatabase.org).

```bash
journey info  feed.zip
journey stops feed.zip --search "rideau"
journey plan  feed.zip --from 3009 --to 8767 --at 17:30
```

No feed to hand? Generate a synthetic city:

```bash
python scripts/make_large_feed.py /tmp/feed
journey plan /tmp/feed --from S2929 --to S5195 --at 08:00
```

## Commands

```
journey info      FEED   [--max-walk M]
journey stops     FEED   --search TEXT [--limit N]
journey plan      FEED   --from STOP --to STOP [--at HH:MM]
                         [--weekday 0-6] [--max-rounds N]
journey benchmark FEED   [--queries N] [--at HH:MM] [--seed N]
```

`--from` and `--to` take a stop id or an unambiguous name fragment. An
ambiguous name lists the candidates rather than guessing.

## Performance

Measured on the generated 6,000-stop feed (`scripts/make_large_feed.py`),
single-threaded, no caching between queries:

| | |
|---|---|
| Feed | 6,000 stops · 220 routes · 48,400 trips · **1,400,960 stop times** |
| Parse | 6.8 s |
| Build routing structures | 0.4 s (220 patterns, 14,438 footpaths) |
| Query, mean | **6.8 ms** |
| Query, median | 4.7 ms |
| Query, p95 | 20.6 ms |

Reproduce with `journey benchmark /tmp/feed --queries 200`.

## Layout

```
transitrouter/
├── gtfs/
│   └── loader.py       streaming GTFS parse; times as seconds since midnight
├── routing/
│   ├── timetable.py    regroup trips into RAPTOR routes by stop sequence
│   ├── transfers.py    generated walking footpaths via a grid index
│   ├── raptor.py       the round-based search and journey reconstruction
│   └── journey.py      itinerary types and formatting
└── cli.py
```

## Testing

```bash
pip install -r requirements-dev.txt
pytest --cov=transitrouter    # 47 tests, 92% coverage
ruff check transitrouter tests
```

The router is tested against a seven-stop network small enough that every
optimal journey can be worked out by hand (`tests/conftest.py` documents it).
Testing a router against a real feed proves nothing — you cannot assert on an
itinerary you needed the router to compute. The sample network deliberately
contains a fast journey with one transfer and a slow one with none, so Pareto
optimality is checked rather than assumed.

## Known limitations

- **Departure-time queries only.** No "arrive by 09:00" search, which needs
  the algorithm run backwards.
- **`calendar_dates.txt` is not applied**, so holiday exceptions and
  single-day services are missed. Regular weekly service is handled.
- **No real-time data.** Everything is scheduled time; delays are invisible.
- **Straight-line walking.** Footpaths ignore rivers, railways and buildings,
  so a 400m transfer across the Rideau Canal will be proposed where no bridge
  exists.
- **No fare or accessibility filtering.**

## Licence

MIT
