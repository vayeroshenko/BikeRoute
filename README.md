# idf-commute-planner

This repository currently implements Milestones 0–3: configuration,
secret-safe fixture capture, the authenticated PRIM API discovery CLI, a
resilient shared PRIM client, normalized domain models, and provider adapters
for Navitia journeys, Geovelo routes, bulk disruptions, and optional SIRI Stop
Monitoring. It also implements explicit outbound bike-to-station orchestration,
hard/soft bicycle thresholds, disruption-aware ranking, an all-transit
baseline, and terminal output. Return/round-trip bicycle state and a graphical
UI are not implemented.

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
  --bike-station best \
  --max-results 10
```

The command never relaxes the configured hard bicycle limit. Rejected routes
show their measured duration and reason. `--max-bike-minutes` changes the hard
limit for one run without editing `config.yaml`; for a persistent change, set
`bicycle.max_bike_minutes` in the ignored local configuration.

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

## Offline checks

```bash
pytest
ruff check .
mypy
```
