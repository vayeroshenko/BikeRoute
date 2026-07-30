from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from idf_commute.domain.models import TimeInterval
from idf_commute.providers.disruptions import (
    BulkDisruptionAdapter,
    DisruptionSchemaError,
    normalize_bulk_disruptions,
)
from idf_commute.providers.prim_client import PrimClient

FIXTURE = Path("tests/fixtures/disruptions/active_work.json")
PARIS = ZoneInfo("Europe/Paris")


def fixture_payload() -> object:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_normalizes_bulk_schema_and_joins_line_impacts() -> None:
    disruptions = normalize_bulk_disruptions(fixture_payload())
    assert len(disruptions) == 1
    disruption = disruptions[0]
    assert disruption.id == "a24514c3-e5bd-4249-a659-904f3d1fc4dd"
    assert disruption.severity == "PERTURBEE"
    assert disruption.affected_line_ids == frozenset({"line:IDFM:C00157"})
    assert disruption.application_periods[0].begin.tzinfo == PARIS
    assert disruption.updated_at == datetime(2026, 7, 16, 17, 30, 13, tzinfo=PARIS)


def test_filters_disruptions_outside_requested_interval() -> None:
    interval = TimeInterval(
        begin=datetime(2027, 1, 1, tzinfo=PARIS),
        end=datetime(2027, 1, 2, tzinfo=PARIS),
    )
    assert normalize_bulk_disruptions(fixture_payload(), interval=interval) == []


def test_rejects_missing_bulk_collections() -> None:
    with pytest.raises(DisruptionSchemaError, match="disruptions and lines"):
        normalize_bulk_disruptions({"disruptions": []})


def test_invalid_application_period_does_not_abort_feed() -> None:
    payload = fixture_payload()
    payload["disruptions"][0]["applicationPeriods"].append(
        {"begin": "20260730T080000", "end": "20260730T080000"}
    )
    disruptions = normalize_bulk_disruptions(payload)
    assert len(disruptions) == 1
    assert len(disruptions[0].application_periods) == 1


@pytest.mark.asyncio
async def test_adapter_uses_short_cache_hook_contract() -> None:
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        assert request.url.path == "/marketplace/disruptions_bulk/disruptions/v2"
        return httpx.Response(200, json=fixture_payload())

    async with PrimClient(
        "secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        adapter = BulkDisruptionAdapter(
            client,
            "https://prim.test/marketplace/disruptions_bulk/disruptions/v2",
        )
        disruptions = await adapter.disruptions()
    assert requests == 1
    assert disruptions[0].source == "prim_disruptions_bulk"
