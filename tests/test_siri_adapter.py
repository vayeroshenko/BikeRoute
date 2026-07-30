from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from idf_commute.domain.models import Freshness
from idf_commute.providers.prim_client import PrimClient
from idf_commute.providers.siri import (
    SiriSchemaError,
    SiriStopMonitoringAdapter,
    normalize_siri_departures,
)

FIXTURE = Path("tests/fixtures/siri/stop_monitoring.json")


def fixture_payload() -> object:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_normalizes_expected_departure_as_realtime() -> None:
    departures = normalize_siri_departures(fixture_payload())
    assert len(departures) == 1
    departure = departures[0]
    assert departure.line_id == "line:IDFM:synthetic"
    assert departure.stop_id == "stop_point:IDFM:synthetic"
    assert departure.expected_time is not None
    assert departure.freshness is Freshness.REALTIME


def test_normalizes_schedule_only_departure() -> None:
    payload = fixture_payload()
    call = payload["Siri"]["ServiceDelivery"]["StopMonitoringDelivery"][0]["MonitoredStopVisit"][0][
        "MonitoredVehicleJourney"
    ]["MonitoredCall"]
    call["AimedDepartureTime"] = call.pop("ExpectedDepartureTime")
    departure = normalize_siri_departures(payload)[0]
    assert departure.expected_time is None
    assert departure.scheduled_time is not None
    assert departure.freshness is Freshness.BASE_SCHEDULE


def test_rejects_missing_service_delivery() -> None:
    with pytest.raises(SiriSchemaError, match="ServiceDelivery"):
        normalize_siri_departures({"Siri": {}})


@pytest.mark.asyncio
async def test_adapter_filters_requested_line() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["MonitoringRef"] == "stop_point:IDFM:synthetic"
        return httpx.Response(200, json=fixture_payload())

    async with PrimClient(
        "secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        adapter = SiriStopMonitoringAdapter(
            client,
            "https://prim.test/marketplace/stop-monitoring",
        )
        matching = await adapter.departures(
            "stop_point:IDFM:synthetic",
            "line:IDFM:synthetic",
        )
        missing = await adapter.departures(
            "stop_point:IDFM:synthetic",
            "line:IDFM:other",
        )
    assert len(matching) == 1
    assert missing == []
