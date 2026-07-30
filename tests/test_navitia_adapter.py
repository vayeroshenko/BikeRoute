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
)
from idf_commute.providers.prim_client import PrimClient

FIXTURE = Path("tests/fixtures/navitia/journey_realtime.json")
PARIS = ZoneInfo("Europe/Paris")


def fixture_payload() -> object:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_normalizes_mixed_section_freshness() -> None:
    journeys = normalize_navitia_journeys(fixture_payload())
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
        return httpx.Response(200, json=fixture_payload())

    transit_request = TransitRequest(
        origin_id="stop_area:origin",
        destination_id="stop_area:destination",
        datetime=datetime(2026, 7, 30, 8, 30, tzinfo=PARIS),
        arrive_by=True,
        forbidden_ids=("line:one", "line:two"),
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
