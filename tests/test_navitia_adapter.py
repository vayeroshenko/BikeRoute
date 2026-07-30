from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from idf_commute.domain.models import Freshness, TransitRequest
from idf_commute.providers.navitia import (
    NavitiaAdapter,
    NavitiaSchemaError,
    normalize_navitia_journeys,
    normalize_navitia_stations,
)
from idf_commute.providers.prim_client import PrimClient

FIXTURE = Path("tests/fixtures/navitia/journey_realtime.json")
PARIS = ZoneInfo("Europe/Paris")


def fixture_payload() -> object:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def station_fixture_payload() -> object:
    return json.loads(Path("tests/fixtures/navitia/stations.json").read_text(encoding="utf-8"))


def test_normalizes_mixed_section_freshness() -> None:
    payload = fixture_payload()
    assert isinstance(payload, dict)
    section = payload["journeys"][0]["sections"][1]
    section["display_informations"] = {
        "code": "B",
        "direction": "Saint-Rémy-lès-Chevreuse",
        "commercial_mode": {"name": "RER"},
    }
    section["from"] = {
        "id": "stop_point:bourg",
        "stop_point": {"name": "Bourg-la-Reine"},
    }
    section["to"] = {
        "id": "stop_point:massyp",
        "stop_point": {"name": "Massy - Palaiseau"},
    }
    journeys = normalize_navitia_journeys(payload)
    assert len(journeys) == 1
    journey = journeys[0]
    assert journey.departure.tzinfo == PARIS
    assert journey.duration_seconds == 1720
    assert journey.response_timestamp == datetime(2026, 7, 30, 11, 9, 47, tzinfo=PARIS)
    transit_legs = [leg for leg in journey.legs if leg.type == "public_transport"]
    assert [leg.freshness for leg in transit_legs] == [
        Freshness.REALTIME,
        Freshness.BASE_SCHEDULE,
    ]
    assert transit_legs[0].commercial_mode == "RER"
    assert transit_legs[0].line_code == "B"
    assert transit_legs[0].direction == "Saint-Rémy-lès-Chevreuse"
    assert transit_legs[0].origin_name == "Bourg-la-Reine"
    assert transit_legs[0].destination_name == "Massy - Palaiseau"


def test_rejects_missing_required_journey_times() -> None:
    payload = {"journeys": [{"duration": 1, "sections": []}]}
    with pytest.raises(NavitiaSchemaError, match="datetime is missing"):
        normalize_navitia_journeys(payload)


@pytest.mark.asyncio
async def test_adapter_builds_arrive_by_request() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/marketplace/v2/navitia/journeys"
        assert request.url.params["from"] == "stop_area:origin"
        assert request.url.params["to"] == "stop_area:destination"
        assert request.url.params["datetime"] == "20260730T083000"
        assert request.url.params["datetime_represents"] == "arrival"
        assert request.url.params.get_list("forbidden_uris[]") == ["line:one", "line:two"]
        assert request.url.params["min_nb_journeys"] == "10"
        return httpx.Response(200, json=fixture_payload())

    transit_request = TransitRequest(
        origin_id="stop_area:origin",
        destination_id="stop_area:destination",
        datetime=datetime(2026, 7, 30, 8, 30, tzinfo=PARIS),
        arrive_by=True,
        forbidden_ids=("line:one", "line:two"),
        min_journeys=10,
    )
    async with PrimClient(
        "secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        journeys = await NavitiaAdapter(
            client,
            "https://prim.test/marketplace/v2/navitia",
        ).journeys(transit_request)
    assert journeys[0].type == "best"


def test_normalizes_station_id_coordinates_and_lines() -> None:
    stations = normalize_navitia_stations(station_fixture_payload())
    assert len(stations) == 1
    station = stations[0]
    assert station.id == "stop_area:IDFM:70033"
    assert station.location is not None
    assert station.location.latitude == 48.7801
    assert station.line_ids == ("line:IDFM:C01743", "line:IDFM:C01193")


@pytest.mark.asyncio
async def test_adapter_resolves_station_query() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/marketplace/v2/navitia/places"
        assert request.url.params["q"] == "Bourg-la-Reine"
        assert request.url.params["type[]"] == "stop_area"
        return httpx.Response(200, json=station_fixture_payload())

    async with PrimClient(
        "secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        stations = await NavitiaAdapter(
            client,
            "https://prim.test/marketplace/v2/navitia",
        ).stations("Bourg-la-Reine")
    assert stations[0].id == "stop_area:IDFM:70033"
