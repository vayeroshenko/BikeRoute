from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from idf_commute.config import AppConfig, MissingAccessError, Settings, coordinate_secrets
from idf_commute.redaction import redact, redact_mapping, redact_url

RESPONSE_HEADER_ALLOWLIST = {
    "cf-ray",
    "content-type",
    "date",
    "etag",
    "last-modified",
    "retry-after",
    "server",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
}


class ProbeError(RuntimeError):
    """A provider returned a response that prevents discovery."""


@dataclass(frozen=True)
class ProbeResult:
    name: str
    status_code: int
    fixture_path: Path
    body: Any


class ProbeRunner:
    def __init__(
        self,
        settings: Settings,
        config: AppConfig,
        fixture_dir: Path,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.config = config
        self.fixture_dir = fixture_dir
        self._secrets = coordinate_secrets(config)
        api_key = settings.require_api_key().get_secret_value()
        self._secrets.add(api_key)
        self._client = httpx.AsyncClient(
            headers={settings.api_key_header: api_key, "Accept": "application/json"},
            timeout=settings.timeout_seconds,
            transport=transport,
        )

    async def __aenter__(self) -> ProbeRunner:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.aclose()

    async def _request(
        self,
        name: str,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> ProbeResult:
        started = datetime.now(UTC)
        try:
            response = await self._client.request(
                method,
                url,
                params=params,
                json=json_body,
            )
        except httpx.HTTPError as exc:
            raise ProbeError(f"{name}: network request failed: {type(exc).__name__}") from exc

        try:
            body: Any = response.json()
        except ValueError:
            body = {"non_json_body": response.text[:2000]}

        fixture = {
            "capture": {
                "name": name,
                "captured_at": started.isoformat(),
                "response_received_at": datetime.now(UTC).isoformat(),
                "request": {
                    "method": method,
                    "url": redact_url(str(response.request.url), self._secrets),
                    "headers": {
                        "accept": response.request.headers.get("accept", ""),
                        self.settings.api_key_header: "<redacted>",
                    },
                    "json": redact_mapping(json_body, self._secrets) if json_body else None,
                },
                "response": {
                    "status_code": response.status_code,
                    "headers": {
                        key.lower(): value
                        for key, value in response.headers.items()
                        if key.lower() in RESPONSE_HEADER_ALLOWLIST
                    },
                    "body": redact(
                        _redact_provider_specific_fields(name, body),
                        self._secrets,
                    ),
                },
            }
        }
        fixture_path = self._write_fixture(name, fixture)

        if response.status_code == 401:
            raise MissingAccessError(
                f"{name}: PRIM returned HTTP 401. The token is invalid or expired. "
                "Sanitized response: "
                f"{fixture_path}"
            )
        if response.status_code == 403:
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type.casefold():
                raise MissingAccessError(
                    f"{name}: PRIM returned a non-JSON HTTP 403 from its edge/security "
                    f"layer. Subscription status could not be determined. Sanitized "
                    f"response: {fixture_path}"
                )
            raise MissingAccessError(
                f"{name}: PRIM returned JSON HTTP 403. The API subscription or token "
                f"permissions are missing. Sanitized response: {fixture_path}"
            )
        if response.status_code >= 400:
            raise ProbeError(
                f"{name}: PRIM returned HTTP {response.status_code}. "
                f"Sanitized response: {fixture_path}"
            )
        return ProbeResult(name, response.status_code, fixture_path, body)

    def _write_fixture(self, name: str, fixture: dict[str, Any]) -> Path:
        safe_name = re.sub(r"[^a-z0-9_-]+", "_", name.lower()).strip("_")
        self.fixture_dir.mkdir(parents=True, exist_ok=True)
        destination = self.fixture_dir / f"{safe_name}.json"
        destination.write_text(
            json.dumps(fixture, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return destination

    def _navitia_url(self, suffix: str) -> str:
        base = str(self.settings.navitia_base_url).rstrip("/")
        return f"{base}/{suffix.lstrip('/')}"

    async def resolve_places_and_lines(self) -> tuple[list[ProbeResult], str | None]:
        results: list[ProbeResult] = []
        resolved_station_id: str | None = next(
            (
                station.id
                for station in self.config.candidate_stations
                if station.id and station.id not in {"0", "REPLACE_WITH_NAVITIA_STOP_AREA_ID"}
            ),
            None,
        )
        for index, station in enumerate(self.config.candidate_stations, start=1):
            result = await self._request(
                f"navitia-place-{index}",
                "GET",
                self._navitia_url("places"),
                params={"q": station.query, "type[]": "stop_area"},
            )
            results.append(result)
            if resolved_station_id is None:
                resolved_station_id = _first_stop_area_id(result.body)

        results.extend(await self.identifiers())
        return results, resolved_station_id

    async def identifiers(self) -> list[ProbeResult]:
        results: list[ProbeResult] = []
        first_page = await self._request(
            "navitia-lines-page-0",
            "GET",
            self._navitia_url("lines"),
            params={"count": 1000, "start_page": 0},
        )
        results.append(first_page)
        all_lines = _line_collection(first_page.body)
        total = _pagination_total(first_page.body)
        start_page = 1
        while len(all_lines) < total:
            page = await self._request(
                f"navitia-lines-page-{start_page}",
                "GET",
                self._navitia_url("lines"),
                params={"count": 1000, "start_page": start_page},
            )
            results.append(page)
            page_lines = _line_collection(page.body)
            if not page_lines:
                break
            all_lines.extend(page_lines)
            start_page += 1

        resolved = {
            query: _matching_line_summaries(all_lines, query) for query in self.config.line_queries
        }
        manifest = {
            "capture": {
                "name": "navitia-identifiers-resolved",
                "captured_at": datetime.now(UTC).isoformat(),
                "source_fixtures": [str(result.fixture_path) for result in results],
                "queries": resolved,
            }
        }
        manifest_path = self._write_fixture("navitia-identifiers-resolved", manifest)
        results.append(
            ProbeResult(
                name="navitia-identifiers-resolved",
                status_code=200,
                fixture_path=manifest_path,
                body={"queries": resolved},
            )
        )
        return results

    async def navitia(self) -> list[ProbeResult]:
        results, station_id = await self.resolve_places_and_lines()
        if not station_id:
            raise ProbeError(
                "No candidate station ID could be resolved. Set a Navitia stop_area ID "
                "in config.yaml after reviewing the sanitized places fixture."
            )
        params: dict[str, Any] = {
            "from": station_id,
            "to": self.config.locations.work.navitia_coord,
            "data_freshness": "realtime",
            "direct_path": "none",
        }
        if self.config.probe.navitia_datetime:
            params["datetime"] = self.config.probe.navitia_datetime
        results.append(
            await self._request(
                "navitia-journey",
                "GET",
                self._navitia_url("journeys"),
                params=params,
            )
        )
        park_params = {
            "from": self.config.locations.home.navitia_coord,
            "to": self.config.locations.work.navitia_coord,
            "first_section_mode[]": "bike",
            "last_section_mode[]": "walking",
            "park_mode": "on_street",
            "direct_path": "none",
            "max_duration_to_pt": 1320,
            "data_freshness": "realtime",
        }
        if self.config.probe.navitia_datetime:
            park_params["datetime"] = self.config.probe.navitia_datetime
        results.append(
            await self._request(
                "navitia-park-mode",
                "GET",
                self._navitia_url("journeys"),
                params=park_params,
            )
        )
        return results

    async def disruptions(self) -> list[ProbeResult]:
        return [
            await self._request(
                "disruptions",
                "GET",
                str(self.settings.disruptions_url),
            )
        ]

    async def geovelo(self) -> list[ProbeResult]:
        payload = {
            "waypoints": [
                {
                    "latitude": self.config.locations.home.latitude,
                    "longitude": self.config.locations.home.longitude,
                    "title": "HOME",
                },
                {
                    "latitude": self.config.locations.work.latitude,
                    "longitude": self.config.locations.work.longitude,
                    "title": "WORK",
                },
            ],
            "bikeDetails": {
                "profile": self.config.probe.geovelo_profile,
                "bikeType": self.config.probe.geovelo_bike_type,
                "averageSpeed": self.config.probe.geovelo_average_speed_kmh,
            },
            "transportModes": ["BIKE"],
        }
        return [
            await self._request(
                "geovelo-routes",
                "POST",
                str(self.settings.geovelo_url),
                params={
                    "instructions": "true",
                    "elevations": "true",
                    "geometry": "true",
                    "single_result": "false",
                },
                json_body=payload,
            )
        ]

    async def stop_monitoring(self) -> list[ProbeResult]:
        stop_id = self.config.probe.stop_monitoring_stop_id
        if not stop_id:
            raise ProbeError(
                "Stop Monitoring is optional and probe.stop_monitoring_stop_id is not set."
            )
        return [
            await self._request(
                "stop-monitoring",
                "GET",
                str(self.settings.stop_monitoring_url),
                params={"MonitoringRef": stop_id},
            )
        ]

    async def all(self) -> list[ProbeResult]:
        results = await self.navitia()
        results.extend(await self.disruptions())
        results.extend(await self.geovelo())
        if self.config.probe.stop_monitoring_stop_id:
            results.extend(await self.stop_monitoring())
        return results


def _first_stop_area_id(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    places = body.get("places")
    if not isinstance(places, list):
        return None
    for place in places:
        if not isinstance(place, dict):
            continue
        stop_area = place.get("stop_area")
        if isinstance(stop_area, dict):
            nested_id = stop_area.get("id")
            if isinstance(nested_id, str):
                return nested_id
        place_id = place.get("id")
        if place.get("embedded_type") == "stop_area" and isinstance(place_id, str):
            return place_id
    return None


def summarize_result(result: ProbeResult) -> str:
    if result.name.startswith("navitia-place"):
        ids = _collection_ids(result.body, "places", nested_key="stop_area")
        return ", ".join(ids) if ids else "no stop_area ID found"
    if result.name.startswith("navitia-line"):
        ids = _collection_ids(result.body, "lines")
        return ", ".join(ids) if ids else "no line ID found"
    if result.name == "navitia-identifiers-resolved":
        queries = result.body.get("queries") if isinstance(result.body, dict) else None
        if not isinstance(queries, dict):
            return "no identifier mapping"
        summaries: list[str] = []
        for query, matches in queries.items():
            if not isinstance(matches, list):
                continue
            ids = [
                match["id"]
                for match in matches
                if isinstance(match, dict) and isinstance(match.get("id"), str)
            ]
            summaries.append(f"{query}={','.join(ids) if ids else 'unresolved'}")
        return "; ".join(summaries)
    if result.name == "navitia-park-mode":
        return _park_mode_summary(result.body)
    if result.name == "navitia-journey":
        freshness = _section_freshness_values(result.body)
        return f"section_freshness={','.join(freshness) if freshness else 'not reported'}"
    return "captured"


def _collection_ids(body: Any, collection: str, *, nested_key: str | None = None) -> list[str]:
    if not isinstance(body, dict) or not isinstance(body.get(collection), list):
        return []
    identifiers: list[str] = []
    for item in body[collection]:
        if not isinstance(item, dict):
            continue
        candidate = item.get(nested_key) if nested_key else item
        if not isinstance(candidate, dict):
            continue
        identifier = candidate.get("id")
        if isinstance(identifier, str):
            identifiers.append(identifier)
    return identifiers[:10]


def _park_mode_summary(body: Any) -> str:
    if not isinstance(body, dict) or not isinstance(body.get("journeys"), list):
        return "no journey returned"
    journeys = body["journeys"]
    if not journeys or not isinstance(journeys[0], dict):
        return "no journey returned"
    sections = journeys[0].get("sections")
    if not isinstance(sections, list):
        return "no sections returned"
    labels: list[str] = []
    bike_indexes: list[int] = []
    transit_indexes: list[int] = []
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            continue
        section_type = section.get("type")
        mode = section.get("mode")
        label = str(mode or section_type or "unknown")
        labels.append(label)
        if mode in {"bike", "bss"}:
            bike_indexes.append(index)
        if section_type == "public_transport":
            transit_indexes.append(index)
    has_park = any(
        isinstance(section, dict) and section.get("type") == "park" for section in sections
    )
    bike_after_transit = bool(
        bike_indexes and transit_indexes and max(bike_indexes) > min(transit_indexes)
    )
    return (
        f"sections={' > '.join(labels) or 'none'}; "
        f"park={'yes' if has_park else 'no'}; "
        f"bike_after_transit={'yes' if bike_after_transit else 'no'}"
    )


def _first_journey_value(body: Any, key: str) -> str | None:
    if not isinstance(body, dict) or not isinstance(body.get("journeys"), list):
        return None
    journeys = body["journeys"]
    if not journeys or not isinstance(journeys[0], dict):
        return None
    value = journeys[0].get(key)
    return str(value) if value is not None else None


def _section_freshness_values(body: Any) -> list[str]:
    if not isinstance(body, dict) or not isinstance(body.get("journeys"), list):
        return []
    values: set[str] = set()
    for journey in body["journeys"]:
        if not isinstance(journey, dict) or not isinstance(journey.get("sections"), list):
            continue
        for section in journey["sections"]:
            if not isinstance(section, dict):
                continue
            freshness = section.get("data_freshness")
            if isinstance(freshness, str):
                values.add(freshness)
    return sorted(values)


def _line_collection(body: Any) -> list[dict[str, Any]]:
    if not isinstance(body, dict) or not isinstance(body.get("lines"), list):
        return []
    return [item for item in body["lines"] if isinstance(item, dict)]


def _pagination_total(body: Any) -> int:
    if not isinstance(body, dict) or not isinstance(body.get("pagination"), dict):
        return len(_line_collection(body))
    total = body["pagination"].get("total_result")
    return total if isinstance(total, int) else len(_line_collection(body))


def _matching_line_summaries(lines: list[dict[str, Any]], query: str) -> list[dict[str, str]]:
    normalized_query = _normalized_label(query)
    rer_query = normalized_query.startswith("rer")
    target = normalized_query.removeprefix("rer") if rer_query else normalized_query
    matches: list[dict[str, str]] = []
    for line in lines:
        identifier = line.get("id")
        code = line.get("code")
        name = line.get("name")
        if (
            not isinstance(identifier, str)
            or not isinstance(code, str)
            or not isinstance(name, str)
        ):
            continue
        labels = {_normalized_label(code), _normalized_label(name)}
        if target not in labels and normalized_query not in labels:
            continue
        if rer_query and "rer" not in _normalized_label(json.dumps(line)):
            continue
        matches.append({"id": identifier, "code": code, "name": name})
    return matches


def _normalized_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _redact_provider_specific_fields(name: str, value: Any) -> Any:
    if name != "geovelo-routes":
        return value
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if key == "geometry":
                redacted[key] = "<redacted-geometry>"
            elif key == "id":
                redacted[key] = "<redacted-route-id>"
            else:
                redacted[key] = _redact_provider_specific_fields(name, item)
        return redacted
    if isinstance(value, list):
        return [_redact_provider_specific_fields(name, item) for item in value]
    return value


def validate_live_config(config: AppConfig, *, require_station: bool = True) -> None:
    locations = (config.locations.home, config.locations.work)
    if any(location.latitude == 0.0 and location.longitude == 0.0 for location in locations):
        raise ProbeError(
            "config.yaml still contains placeholder coordinates. Add private home/work "
            "coordinates to the ignored local file before a live probe."
        )
    if require_station and any(
        station.query.startswith("REPLACE ") for station in config.candidate_stations
    ):
        raise ProbeError("config.yaml still contains a placeholder candidate station query.")
