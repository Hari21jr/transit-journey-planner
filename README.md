# Transit Journey Planner

Plans public transit journeys over any GTFS feed using RAPTOR, returning
several itineraries that trade travel time against number of transfers.
No routing library — the algorithm is the project.

[![CI](https://github.com/Hari21jr/transit-journey-planner/actions/workflows/ci.yml/badge.svg)](https://github.com/Hari21jr/transit-journey-planner/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Coverage](https://img.shields.io/badge/coverage-91%25-brightgreen)
![Dependencies](https://img.shields.io/badge/runtime%20deps-none-brightgreen)

```
$ journey plan octranspo.zip --from "Hurdman" --to "Rideau" --at 17:30

HURDMAN → RIDEAU
2024-07-15, departing after 17:30:00 · solved in 32ms · 2,995 stops reached in 5 rounds

  [fastest] 17:30:00 → 17:57:24  (27 min total, 1 transfer, 8 min walking)
    17:30:00  walk 1 min to HURDMAN O-TRAIN WEST / OUEST
    17:35:00  route 1 to Tunney's Pasture from HURDMAN O-TRAIN WEST (1 stops)
    17:38:00  arrive LEES O-TRAIN WEST / OUEST
    17:38:00  walk 4 min to LEES / BRUNSWICK
    17:44:00  route 16 to Tunney's Pasture from LEES / BRUNSWICK (14 stops)
    17:54:00  arrive MACKENZIE KING 2A
    17:54:00  walk 3 min to RIDEAU C

  [fewer changes] 17:58:00 → 18:22:15  (52 min total, 28 min wait, 0 transfers)
    17:58:00  route 9 to Rideau from HURDMAN E (27 stops)
    18:21:00  arrive DALHOUSIE / RIDEAU (D)
    18:21:00  walk 1 min to RIDEAU A
```

Real output on OC Transpo's Ottawa feed. Both journeys are optimal: one gets
you there 25 minutes sooner, the other needs no changes at all.

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
  25 minutes faster, the other has one fewer change.
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

**Stations are one place, not seven.** Someone asking to travel *from Hurdman*
neither knows nor cares which platform their bus leaves from. GTFS has a
`parent_station` field for exactly this — and OC Transpo, like many agencies,
leaves it empty and encodes the platform in the name instead: `HURDMAN A`
through `HURDMAN E`, five separate stops with five separate names.

So places are built three ways in order: by parent station where one exists,
then by name with a trailing platform designator stripped (`HURDMAN A` →
`HURDMAN`, `BLAIR PLATFORM 3` → `BLAIR`), and finally by proximity — stops
sharing a stem but 3km apart are not one station. On Ottawa's feed that turns
5,791 stops into 4,111 places, 1,578 of which group more than one stop.

The stripping is deliberately conservative, because the opposite failure is
worse: `RIDEAU / FRIEL` and `RIDEAU / NELSON` are different corners, and
merging a whole street into one stop would produce confidently wrong journeys.
Every platform of a resolved place is seeded into the search at once, so the
router picks whichever turns out best.

**Holidays are honoured.** `calendar.txt` gives a weekly pattern, but agencies
lean heavily on `calendar_dates.txt` to override it: a statutory holiday
typically removes the weekday service and adds a Sunday one. Reading only the
weekly pattern gets those days wrong in both directions. Some feeds skip the
weekly calendar entirely and list every operating day as an exception, so both
paths have to work.

**Walking transfers are generated, not just read.** Most feeds declare only a
handful of transfers, usually inside stations, and never mention the stop
across the street. Footpaths are generated for stop pairs within 400m using a
grid index — comparing all stops to all stops would be 36 million distance
calculations for a 6,000-stop feed. Agency-declared transfer times override
generated ones, since they know their own stations.

**Walking is not a transfer.** A journey that walks between two stops still
counts as one vehicle, which matters for the Pareto comparison.

**Time is measured from when you asked, not from when the bus moves.** The
direct route above is a 24-minute ride — but it leaves in 28 minutes, so it
delivers you 25 minutes later than the option with a change in it. Reporting
in-vehicle duration alone makes the waiting journey look like the fast one.

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
journey plan      FEED   --from PLACE --to PLACE [--at HH:MM]
                         [--date YYYY-MM-DD] [--weekday 0-6] [--max-rounds N]
journey benchmark FEED   [--queries N] [--at HH:MM] [--seed N]
```

`--from` and `--to` take a place id, a raw stop id, or an unambiguous name. An
ambiguous name lists the candidates rather than guessing which one you meant.

`--date` routes for a real calendar day, honouring holiday exceptions.
`--weekday` is the blunter fallback that ignores them.

## Performance

On OC Transpo's published feed for Ottawa, single-threaded:

| | |
|---|---|
| Feed | 5,791 stops · 204 routes · 54,358 trips · **2,014,198 stop times** |
| Parse | 9.4 s |
| Build routing structures | 0.5 s |
| | 891 routing patterns · 4,111 places · 53,018 footpaths |

200 random origin/destination pairs on that feed, departing 08:00, no caching
between queries:

| | |
|---|---|
| Solved | 192 / 200 (96%) |
| Query, mean | **50.4 ms** |
| Query, median | 52.2 ms |
| Query, p95 | 81.5 ms |
| Slowest | 176.5 ms |

Reproduce with `journey benchmark FEED --queries 200 --date YYYY-MM-DD`.

## Layout

```
transitrouter/
├── gtfs/
│   └── loader.py       streaming GTFS parse; times as seconds since midnight
├── routing/
│   ├── timetable.py    regroup trips into RAPTOR routes by stop sequence
│   ├── places.py       group platforms into the places people name
│   ├── transfers.py    generated walking footpaths via a grid index
│   ├── raptor.py       the round-based search and journey reconstruction
│   └── journey.py      itinerary types and formatting
└── cli.py
```

## Testing

```bash
pip install -r requirements-dev.txt
pytest --cov=transitrouter    # 64 tests, 91% coverage
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
- **No real-time data.** Everything is scheduled time; delays are invisible.
- **Straight-line walking.** Footpaths ignore rivers, railways and buildings,
  so a 400m transfer across the Rideau Canal will be proposed where no bridge
  exists.
- **No fare or accessibility filtering.**

## Licence

MIT
