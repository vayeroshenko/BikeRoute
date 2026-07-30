# idf-commute-planner

This repository currently implements Milestones 0–2: configuration,
secret-safe fixture capture, the authenticated PRIM API discovery CLI, a
resilient shared PRIM client, normalized domain models, and provider adapters
for Navitia journeys, Geovelo routes, bulk disruptions, and optional SIRI Stop
Monitoring. It does not implement the commute planner or a user interface.

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
normalized adapter boundary.

## Offline checks

```bash
pytest
ruff check .
mypy
```
