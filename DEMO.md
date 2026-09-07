# Demo walkthrough

Everything this tool does, in the order worth showing someone. Each step says
what to run, what you should see, and what it proves — the last part being the
one that matters if somebody asks you about it.

Uses OC Transpo's Ottawa feed throughout. Substitute your own path:

```powershell
$FEED = "..\mdb-738-202407010000.zip"
```

The feed's schedule covers summer 2024, so every routing command below passes
`--date 2024-07-15` (a Monday inside that window).

---

## 0. Prove it runs

```powershell
pytest
```

**Expect:** `118 passed` in under two seconds.

**Proves:** the algorithm is verified against a seven-stop network small
enough that every optimal journey was worked out by hand. Correctness tests,
not smoke tests — including that the Pareto set contains no dominated journey,
and that a route you must wait for isn't reported as faster than one you can
board now.

---

## 1. Read a real feed

```powershell
journey info $FEED
```

**Expect:**

```
loaded 5,791 stops, 204 routes, 54,358 trips, 2,014,198 stop times in 9.4s
built 891 patterns, 4,070 places, 53,018 footpaths in 0.4s

stops         5,791
gtfs routes   204
trips         54,358
stop times    2,014,198
services      37
valid         2024-06-27 to 2024-08-24  (expired)
date overrides     198   (holiday and one-off service changes)

patterns      891   (trips regrouped by identical stop sequence)
footpaths     53,018   (walking transfers within 400m)
places        4,070   (1,578 group more than one stop)
```

**Proves four things at once:**

- **Two million rows in 9 seconds** — the loader streams and converts to
  primitives rather than building an object per stop-time.
- **204 routes became 891 patterns.** A GTFS route isn't a RAPTOR route; trips
  that short-turn or skip stops must be separated or the router boards you
  onto vehicles that don't stop where it thinks. This number is that step
  working.
- **198 date overrides** — holiday exceptions parsed, not ignored.
- **`(expired)`** — the tool knows the feed is a dated snapshot and says so,
  rather than mysteriously finding no journeys.

---

## 2. Find a place

```powershell
journey stops $FEED --search "hurdman"
```

**Expect:**

```
ID              NAME                                  STOPS  LAT, LON
AF910           HURDMAN                                   5  45.41211, -75.66620
AF995           HURDMAN O-TRAIN EAST / EST                1  45.41238, -75.66380
AF990           HURDMAN O-TRAIN WEST / OUEST              1  45.41232, -75.66480
```

**Proves:** OC Transpo publishes five separate stops named `HURDMAN A` through
`HURDMAN E` and sets no `parent_station`. Those five are one station to
anybody travelling. The `5` in the STOPS column is them grouped.

Contrast with a name that must *not* group:

```powershell
journey stops $FEED --search "rideau /"
```

Every corner — `RIDEAU / FRIEL`, `RIDEAU / NELSON`, `RIDEAU / CHARLOTTE` —
stays its own place, usually with 2 stops (the two sides of the street).
Over-merging here would produce confidently wrong journeys.

---

## 3. Plan a journey

```powershell
journey plan $FEED --from "Hurdman" --to "Rideau" --at 17:30 --date 2024-07-15
```

**Expect:**

```
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

**This is the whole project in one screen.** Point at four things:

1. **Two answers, both optimal.** RAPTOR is multi-criteria by construction —
   it returns the Pareto set over arrival time and transfer count instead of
   collapsing them with an arbitrary transfer penalty. One gets you there 25
   minutes sooner; the other needs no changes.
2. **`32ms`** across a network of 5,791 stops and 54,358 trips.
3. **Walking legs mid-journey.** The transfer at Lees is a 4-minute walk
   between two stops the agency never declared as connected. Those footpaths
   are generated, not read.
4. **`52 min total, 28 min wait`.** The direct bus is a 24-minute ride, but
   you'd stand at the stop for 28 minutes first. Reporting in-vehicle time
   alone would make the slower option look faster.

### Constrain it

```powershell
journey plan $FEED --from "Hurdman" --to "Rideau" --at 17:30 --date 2024-07-15 --max-rounds 1
```

Only the no-transfer journey survives — transfers are a loop bound in the
search, not a filter applied to results afterwards.

```powershell
journey plan $FEED --from "Hurdman" --to "Rideau" --at 17:30 --date 2024-07-15 --max-walk 100
```

Restricts walking to 100m. Fewer footpaths exist, so the itinerary changes —
usually to something slower with fewer walking legs.

---

## 4. Show it handles being used wrong

Worth demonstrating deliberately. Most student projects fall over here.

```powershell
journey plan $FEED --from "Bank" --to "Rideau" --date 2024-07-15
```
Lists the candidate places instead of silently picking one. Guessing between
forty stops on Bank Street would produce a plausible-looking wrong answer.

```powershell
journey plan $FEED --from "Hurdman" --to "Atlantis" --date 2024-07-15
```
`no stop matches 'Atlantis'`

```powershell
journey plan $FEED --from "Hurdman" --to "Rideau" --date 2026-01-15
```
```
This feed has no service on 2026-01-15. It covers 2024-06-27 to 2024-08-24
— GTFS feeds are dated snapshots.
Try:  --date 2024-06-27   or download a current feed.
```
The failure that looks most like a broken router, explained instead.

```powershell
journey plan $FEED --from "Hurdman" --to "Rideau" --date "next tuesday"
journey info missing-file.zip
```
Both exit with a one-line message and a non-zero code. No traceback.

---

## 5. Measure it

```powershell
journey benchmark $FEED --queries 200 --date 2024-07-15
```

**Expect:**

```
200 random queries departing 08:00:00
  solved      192/200 (96%)
  mean        50.4 ms
  median      52.2 ms
  p95         81.5 ms
  slowest     176.5 ms
```

**Proves:** the performance claim is reproducible by anyone who clones the
repo, on their own hardware, against a public feed. Not a number in a README.

---

## 6. Works on any agency

Nothing here is Ottawa-specific. Download any feed from
[mobilitydatabase.org](https://mobilitydatabase.org) — Toronto, Vancouver,
Montréal — and every command above works unchanged.

Or with no feed at all:

```powershell
python scripts/make_large_feed.py C:\Users\you\feed
journey benchmark C:\Users\you\feed --queries 200 --weekday 0
```

---

## 7. The web planner

```powershell
pip install -e ".[web]"
journey serve $FEED
```

```
journey planner → http://127.0.0.1:8000
 * Running on http://127.0.0.1:8000
```

The feed loads once at startup — the same ~9 seconds the CLI spends — and is
then held in memory, so queries answer in milliseconds. Open the address in a
browser and you get:

- **Autocomplete** on both boxes, backed by `/api/places`. Debounced, because
  a request per keystroke fires a dozen for one word.
- **Two itineraries** side by side, tagged *fastest* and *fewest changes*,
  each broken into legs with route numbers, walk times and the arrival.
- **The journey drawn on a map.** Every stop the vehicle calls at is plotted,
  so the line follows the road; walking legs are dashed. Click the other
  itinerary and the map redraws.
- **`Solved in 6 ms · 608 stops reached in 5 rounds`** under the form, so the
  algorithm is visible rather than hidden behind the UI.
- **A 24h / 12h switch**, which re-renders from the raw seconds the API
  already sends rather than re-querying.
- **A header that says whether the schedule is current**, so an archived
  snapshot reads as a stated fact rather than a page stuck in the past.

Things worth trying, because they're the parts that usually break:

| Try | What should happen |
|---|---|
| Type a street address, e.g. `47 Huntcliff Place` | it geocodes, walks you to the nearest stops, and the walk from the door is the first leg |
| Type `Rideau` and submit | 409 — the name is ambiguous, so it lists the candidates as buttons; clicking one re-plans |
| Type `Atlantis` | 404 — "No stop matches" |
| Set the date outside the feed window | 400 explaining the feed is a dated snapshot, with the range it covers |
| Set **Most vehicles** to 1 | only zero-transfer itineraries come back |
| Hit the swap arrows | endpoints reverse and it re-plans |
| Block `unpkg.com` in devtools and reload | the map says it's unavailable; the itineraries still render |
| Block `nominatim.openstreetmap.org` and search an address | address lookup fails cleanly; stop names keep working |

**What this proves:** the router isn't a script that only its author can run.
Someone who has never heard of GTFS can use it, and the API underneath
(`/api/places`, `/api/plan`) is the shape you'd build a real front end on.

---

## The two-minute version

If someone gives you two minutes, run exactly three commands:

```powershell
journey info $FEED
journey plan $FEED --from "Hurdman" --to "Rideau" --at 17:30 --date 2024-07-15
journey benchmark $FEED --queries 200 --date 2024-07-15
```

Two million rows read, two optimal itineraries across Ottawa in 32ms, and 200
queries measured to prove it wasn't a fluke.

If they have a screen in front of them instead, `journey serve $FEED` and plan
Hurdman → Rideau in the browser.
