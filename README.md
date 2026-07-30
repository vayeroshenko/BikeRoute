# idf-commute-planner

This repository currently implements Milestones 0–3: configuration,
secret-safe fixture capture, the authenticated PRIM API discovery CLI, a
resilient shared PRIM client, normalized domain models, and provider adapters
for Navitia journeys, Geovelo routes, bulk disruptions, and optional SIRI Stop
Monitoring. It also implements explicit outbound bike-to-station orchestration,
hard/soft bicycle thresholds, disruption-aware ranking, an all-transit
baseline, terminal output, and local SQLite bicycle-location state.
Outbound/return route confirmation and exact-station return planning are also
implemented, together with a local graphical interface. Round-trip ranking is
intentionally deferred.

## Setup

Python 3.12 or newer is required.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
cp config.example.yaml config.yaml
```

Subscribe in PRIM to Navitia generic access v2, traffic/disruptions, Geovelo,
and optionally Stop Monitoring. Put the token in `.env` or export
`PRIM_API_KEY`. Put private coordinates and candidate stations in the ignored
`config.yaml`.

## Safe discovery probe

```bash
idf-commute probe all --config config.yaml
```

Individual probes are available with `navitia`, `disruptions`, `geovelo`, and
`stop-monitoring`. Use `identifiers` to resolve RER/bus line queries even
before a candidate station is configured. `--dry-run` validates configuration
and prints only redacted request shapes. Live captures go to the ignored
`tests/fixtures/live/` directory by default.

The program never logs the API token or request query strings. Before a
fixture is written, configured home/work coordinate values, authorization
values, token-like fields, and sensitive URL parameters are recursively
redacted. Review a capture before moving it into a tracked fixture directory.

See [docs/api-findings.md](docs/api-findings.md) for what has and has not been
confirmed against PRIM and [docs/milestone-2.md](docs/milestone-2.md) for the
normalized adapter boundary. See [docs/milestone-3.md](docs/milestone-3.md)
for outbound planner behavior and current live findings.

## Outbound planning

```bash
idf-commute plan outbound \
  --config config.yaml \
  --depart-at 2026-07-30T08:00:00+02:00 \
  --max-bike-minutes 25 \
  --max-walking-minutes 20 \
  --max-walking-leg-minutes 10 \
  --bike-station best \
  --max-results 10
```

The command never relaxes the configured hard bicycle limit. Rejected routes
show their measured duration and reason. `--max-bike-minutes` changes the hard
limit for one run without editing `config.yaml`; for a persistent change, set
`bicycle.max_bike_minutes` in the ignored local configuration.

`--max-walking-minutes` applies a hard limit to total walking across an
itinerary, including station access, walking transfers, and the final walk.
It overrides `walking.max_minutes` from `config.yaml` for one run. The default
configured limit is 30 minutes. The comparison table displays each route's
walking total, and rejected routes report the shortest returned walking time.

`--max-walking-leg-minutes` separately limits every individual walking leg;
it overrides `walking.max_leg_minutes`, which defaults to 20 minutes. This can
reject a long station-access walk even when the itinerary's total walking limit
is more permissive. Output displays walking as `total / longest leg`.

The comparison requests multiple Navitia journeys per origin, keeps distinct
transit line sequences, and reserves up to 60% of the result list for
all-transit alternatives so bike-route variants cannot hide options such as a
direct bus, replacement bus, or metro chain. `--max-results` accepts 1–20 and
defaults to 10. Each ranked result includes its full leg sequence, stop names,
waits, transfers, realtime/base-schedule status, bicycle metrics, and cleaned
disruption notices.

Interchangeable services of the same transport type between the same two stops
are grouped into one itinerary (for example, `Bus 4602 / 4621 / 4622`). The
displayed times and score come from the fastest member of the group.

Use `--bike-station best` to discover RER B stations, prefilter them to a
generous bicycle radius, and let the full Geovelo + transit score select the
best endpoint. Pass a station name or stop-area ID instead to evaluate exactly
one endpoint, for example `--bike-station Bourg-la-Reine`. If the option is
omitted, the planner retains the configured `candidate_stations` behavior.
The automatic line is configured with `bicycle.target_line_id` and
`bicycle.target_line_label`; both default to RER B.

To limit `best` without treating the branched RER B as one linear sequence,
configure any number of graph ranges:

```yaml
bicycle:
  best_station_ranges:
    - {start: Laplace, end: Bourg-la-Reine}
    - {start: Bourg-la-Reine, end: Sceaux}
```

The planner takes the union of these paths before route planning. Overlapping
stations such as Bourg-la-Reine are queried only once. The same selection can
be overridden for one run with repeatable options:

```bash
idf-commute plan outbound --config config.yaml \
  --depart-at 2026-07-30T08:00:00+02:00 --bike-station best \
  --bike-station-range "Laplace..Bourg-la-Reine" \
  --bike-station-range "Bourg-la-Reine..Sceaux"
```

Ranges can also cross the junction directly, such as `Sceaux..Antony`. The
normal maximum-bike-time radius is still applied after the ranges are merged.

Select a scoring profile per run with `--score-mode`:

- `balanced` preserves the original all-factor ranking.
- `fastest` ranks by door-to-door time only.
- `fewest-transfers` strongly penalizes transfers.
- `easy-ride` emphasizes bicycle duration and cycling comfort.
- `reliable` emphasizes disruptions, schedule-only data, and transfers.

Set the default with `scoring.mode` in `config.yaml`. Individual non-negative
multipliers under `scoring.weights` override the selected preset:

```yaml
scoring:
  mode: reliable
  weights:
    transfers: 2
    disruptions: 4
    freshness: 3
```

Available weight keys are `door_to_door`, `bike_penalty`, `transfers`,
`disruptions`, `freshness`, `connection_margin`, and `cycling_comfort`. The CLI
prints the active profile and every multiplier used in each score.

## Bicycle location state

The planner stores bicycle location in the configured local SQLite database:

```yaml
state:
  sqlite_path: ./data/commute.sqlite3
```

Relative paths are resolved from the directory containing `config.yaml`. State
is not changed merely by calculating a route. Inspect or explicitly correct it
without making any network request:

```bash
idf-commute bike status --config config.yaml
idf-commute bike set-home --config config.yaml
idf-commute bike set-station stop_area:IDFM:70033 \
  --name "Bourg-la-Reine" --config config.yaml
idf-commute bike set-unknown --config config.yaml
```

An absent database means `unknown`; it does not silently assume the bicycle is
at home. When the saved location is `unknown` or a station, outbound planning
suppresses bike-to-station candidates and their API requests, while still
showing all-transit options. After physically checking that the bicycle is
home, `bike set-home` enables those outbound candidates. The `data/` directory
remains ignored by Git.

Planning remains read-only unless `--confirm-rank` is supplied. To record that
you selected a bike route from the displayed comparison, first ensure the
bicycle is recorded at home, then confirm its rank:

```bash
idf-commute bike set-home --config config.yaml
idf-commute plan outbound --config config.yaml \
  --depart-at 2026-07-30T08:00:00+02:00 --bike-station best \
  --confirm-rank 3
```

Confirmation stores the selected station ID, display name, timestamp, and
source journey ID when available. It refuses a bike route if the current state
is `station` or `unknown`, so confirmation cannot silently teleport a bicycle.
Confirming an all-transit option leaves bicycle state unchanged.

## Return planning

When the bicycle is recorded at a station, plan the return from work:

```bash
idf-commute plan return --config config.yaml \
  --depart-at 2026-07-30T18:00:00+02:00 \
  --max-bike-minutes 30
```

The transit request is forced from work to the exact stored stop-area. The
planner then adds the retrieval buffer and a Geovelo route from that station
home. It never substitutes a different bicycle station. Bicycle and walking
limits, scoring modes, disruption matching, and detailed transit legs remain
available. The command remains read-only unless a displayed option is
explicitly confirmed:

```bash
idf-commute plan return --config config.yaml \
  --depart-at 2026-07-30T18:00:00+02:00 --confirm-rank 1
```

Confirmation records the bicycle at home only if the stored station still
matches the selected return. A changed or stale state is rejected rather than
silently moving the bicycle.

## Local graphical interface

Launch the Streamlit interface with:

```bash
idf-commute gui --config config.yaml
```

The GUI provides:

- outbound and exact-station return planning;
- departure date/time, bicycle and walking limits, result count, and scoring
  mode controls;
- configured, automatic-best, specific-station, and multi-range RER B search;
- ranked comparison tables with expandable transit legs, bicycle metrics,
  score components, freshness, and alerts;
- visible bicycle-location state and explicit correction controls;
- outbound and return confirmation buttons with the same state-safety checks
  as the CLI.

Opening the GUI or calculating a plan does not change bicycle state. Only an
explicit correction or confirmation button writes to the local SQLite file.
The API token remains in `.env`/the environment and is not displayed. The
server binds to `127.0.0.1`, so it is not exposed to the local network.

## Reliability assessment

Every outbound and return option now includes a rule-based confidence label,
provider-response age, reasons, and a robust-arrival safety view:

- `high`: fresh response, all public-transport legs realtime, and no matched
  material alert;
- `medium`: aging-but-not-stale data or at least one schedule-only transit leg;
- `low`: missing/stale response timing or a matched material disruption.

The displayed provider arrival is never overwritten. Robust arrival is shown
separately and adds the configured explicit safety buffer:

```yaml
reliability:
  fresh_age_seconds: 120
  stale_age_seconds: 300
  minimum_connection_minutes: 5
  medium_buffer_minutes: 5
  low_buffer_minutes: 10
```

This avoids presenting an app-generated buffer as an official realtime ETA.
For transfers, usable margin is the time between public-transport legs after
subtracting required non-waiting transfer sections. Shortfalls below
`minimum_connection_minutes` lower confidence and add a visible score
component; the `reliable` and `fewest-transfers` modes weight it most strongly.

Every planning run also has a hard PRIM HTTP-attempt budget. The default is
configured under `reliability.max_requests_per_plan` and can be overridden with
`--max-api-requests`. Retries count because they consume provider quota; cache
hits do not. Successful plans show `used / allowed`, while an exhausted budget
stops with a specific error before another provider request is sent.

## Offline checks

```bash
pytest
ruff check .
mypy
```
