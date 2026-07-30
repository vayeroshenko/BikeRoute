from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from idf_commute.domain.models import BikeRequest, Location
from idf_commute.providers.geovelo import (
    GeoveloAdapter,
    GeoveloSchemaError,
    normalize_geovelo_routes,
)
from idf_commute.providers.prim_client import PrimClient

FIXTURE = Path("tests/fixtures/geovelo/alternatives.json")


def fixture_payload() -> object:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_normalizes_reviewed_live_fixture() -> None:
    routes = normalize_geovelo_routes(fixture_payload())
    assert [route.title for route in routes] == ["RECOMMENDED", "SAFER", "FASTER"]
    recommended = routes[0]
    assert recommended.distance_m == 19771
    assert recommended.vertical_gain_m == 271
    assert recommended.encoded_geometry is None
    assert recommended.provider_id is None
    assert len(recommended.elevations) == 2
    assert recommended.elevations[1].distance_from_start_m == 17
    assert recommended.facility_segments[0].facility == "RESIDENTIAL"
    assert recommended.facility_distances_m["cycleway"] == 11893


def test_rejects_positional_rows_without_header() -> None:
    payload = [
        {
            "title": "RECOMMENDED",
            "duration": 1,
            "distances": {"total": 1},
            "sections": [
                {
                    "transportMode": "BIKE",
                    "details": {"instructions": [["HEAD_ON", "Road", 1]]},
                }
            ],
        }
    ]
    with pytest.raises(GeoveloSchemaError, match="string header row"):
        normalize_geovelo_routes(payload)


@pytest.mark.asyncio
async def test_adapter_sends_observed_gateway_contract() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/marketplace/computedroutes"
        assert request.url.params["instructions"] == "true"
        assert request.url.params["single_result"] == "false"
        body = json.loads(request.content)
        assert body["bikeDetails"] == {
            "profile": "MEDIAN",
            "bikeType": "TRADITIONAL",
            "averageSpeed": 16,
        }
        assert body["waypoints"][0]["title"] == "Home"
        return httpx.Response(200, json=fixture_payload())

    request = BikeRequest(
        origin=Location(latitude=48.8, longitude=2.3, label="Home"),
        destination=Location(latitude=48.7, longitude=2.4, label="Station"),
    )
    async with PrimClient(
        "secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        routes = await GeoveloAdapter(
            client,
            "https://prim.test/marketplace/computedroutes",
        ).routes(request)
    assert len(routes) == 3
