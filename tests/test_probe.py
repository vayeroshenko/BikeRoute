from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from idf_commute.config import AppConfig, MissingAccessError, Settings
from idf_commute.probe import (
    ProbeResult,
    ProbeRunner,
    _first_stop_area_id,
    _matching_line_summaries,
    _redact_provider_specific_fields,
    summarize_result,
)


def settings() -> Settings:
    return Settings(
        _env_file=None,
        PRIM_API_KEY="test-token",
        navitia_base_url="https://api.test/navitia",
        disruptions_url="https://api.test/disruptions",
        geovelo_url="https://api.test/geovelo",
        stop_monitoring_url="https://api.test/stop-monitoring",
    )


def config_with_placeholder_station_id(app_config: AppConfig) -> AppConfig:
    return app_config.model_copy(
        update={
            "candidate_stations": [app_config.candidate_stations[0].model_copy(update={"id": "0"})]
        }
    )


@pytest.mark.asyncio
@respx.mock
async def test_capture_redacts_auth_and_precise_coordinates(
    app_config: AppConfig, fixture_dir: Path
) -> None:
    route = respx.get("https://api.test/disruptions").mock(
        return_value=httpx.Response(
            200,
            json={
                "token": "test-token",
                "source_coordinate": "2.312345;48.812345",
                "disruptions": [],
            },
            headers={"X-RateLimit-Remaining": "999", "X-Unrelated": "omit"},
        )
    )

    async with ProbeRunner(settings(), app_config, fixture_dir) as runner:
        result = (await runner.disruptions())[0]

    assert route.called
    assert route.calls[0].request.headers["apiKey"] == "test-token"
    capture = result.fixture_path.read_text(encoding="utf-8")
    assert "test-token" not in capture
    assert "48.812345" not in capture
    assert "2.312345" not in capture
    parsed = json.loads(capture)["capture"]
    assert parsed["request"]["headers"]["apiKey"] == "<redacted>"
    assert parsed["response"]["headers"] == {
        "content-type": "application/json",
        "x-ratelimit-remaining": "999",
    }


@pytest.mark.asyncio
@respx.mock
async def test_auth_failure_is_saved_then_stops(app_config: AppConfig, fixture_dir: Path) -> None:
    respx.get("https://api.test/disruptions").mock(
        return_value=httpx.Response(403, json={"error": "subscription required"})
    )
    async with ProbeRunner(settings(), app_config, fixture_dir) as runner:
        with pytest.raises(MissingAccessError, match="subscription or token permissions"):
            await runner.disruptions()
    assert (fixture_dir / "disruptions.json").exists()


@pytest.mark.asyncio
@respx.mock
async def test_html_403_is_classified_as_edge_block(
    app_config: AppConfig, fixture_dir: Path
) -> None:
    respx.get("https://api.test/disruptions").mock(
        return_value=httpx.Response(
            403,
            text="<html>blocked</html>",
            headers={"content-type": "text/html"},
        )
    )
    async with ProbeRunner(settings(), app_config, fixture_dir) as runner:
        with pytest.raises(MissingAccessError, match="edge/security layer"):
            await runner.disruptions()


def test_stop_area_extraction_supports_nested_navitia_shape() -> None:
    payload = {
        "places": [
            {
                "embedded_type": "stop_area",
                "stop_area": {"id": "stop_area:IDFM:123", "name": "Example"},
            }
        ]
    }
    assert _first_stop_area_id(payload) == "stop_area:IDFM:123"


def test_park_mode_summary_checks_section_order(tmp_path: Path) -> None:
    result = ProbeResult(
        name="navitia-park-mode",
        status_code=200,
        fixture_path=tmp_path / "capture.json",
        body={
            "journeys": [
                {
                    "sections": [
                        {"type": "street_network", "mode": "bike"},
                        {"type": "park"},
                        {"type": "street_network", "mode": "walking"},
                        {"type": "public_transport"},
                    ]
                }
            ]
        },
    )
    summary = summarize_result(result)
    assert "park=yes" in summary
    assert "bike_after_transit=no" in summary


def test_line_matching_uses_exact_code_and_rer_mode() -> None:
    lines = [
        {
            "id": "line:rer-b",
            "code": "B",
            "name": "B",
            "commercial_mode": {"name": "RER"},
        },
        {"id": "line:bus-b", "code": "B", "name": "B", "commercial_mode": {"name": "Bus"}},
        {"id": "line:4602", "code": "4602", "name": "4602"},
        {"id": "line:not-4602", "code": "46020", "name": "46020"},
    ]
    assert _matching_line_summaries(lines, "RER B") == [
        {"id": "line:rer-b", "code": "B", "name": "B"}
    ]
    assert _matching_line_summaries(lines, "4602") == [
        {"id": "line:4602", "code": "4602", "name": "4602"}
    ]


@pytest.mark.asyncio
@respx.mock
async def test_placeholder_station_id_is_replaced_by_place_result(
    app_config: AppConfig, fixture_dir: Path
) -> None:
    config = config_with_placeholder_station_id(app_config)
    places_url = "https://api.test/navitia/places"
    lines_url = "https://api.test/navitia/lines"
    respx.get(places_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "places": [
                    {
                        "embedded_type": "stop_area",
                        "stop_area": {"id": "stop_area:IDFM:resolved"},
                    }
                ]
            },
        )
    )
    respx.get(lines_url).mock(
        return_value=httpx.Response(
            200,
            json={"lines": [], "pagination": {"total_result": 0}},
        )
    )
    async with ProbeRunner(settings(), config, fixture_dir) as runner:
        _, station_id = await runner.resolve_places_and_lines()
    assert station_id == "stop_area:IDFM:resolved"


def test_geovelo_geometry_and_route_ids_are_redacted() -> None:
    body = [
        {
            "id": "base64-contains-coordinates",
            "sections": [{"geometry": "encoded-private-route", "details": {"duration": 10}}],
        }
    ]
    redacted = _redact_provider_specific_fields("geovelo-routes", body)
    assert redacted[0]["id"] == "<redacted-route-id>"
    assert redacted[0]["sections"][0]["geometry"] == "<redacted-geometry>"
    assert redacted[0]["sections"][0]["details"]["duration"] == 10
