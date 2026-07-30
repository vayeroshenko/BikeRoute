from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from idf_commute.cli import _parse_departure, _resolve_candidate_stations, app
from idf_commute.config import CandidateStation
from idf_commute.domain.models import Location, Station

runner = CliRunner()


def test_dry_run_never_requires_or_prints_token(tmp_path: Path, monkeypatch: object) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
locations:
  home: {latitude: 0.0, longitude: 0.0}
  work: {latitude: 0.0, longitude: 0.0}
candidate_stations:
  - {query: REPLACE WITH STATION, label: Example}
""",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        ["probe", "all", "--config", str(config), "--dry-run"],
        env={"PRIM_API_KEY": "must-not-be-printed"},
    )
    assert result.exit_code == 0
    assert "must-not-be-printed" not in result.output
    assert "<configured>" in result.output


def test_plan_outbound_help_is_available() -> None:
    result = runner.invoke(app, ["plan", "outbound", "--help"])
    assert result.exit_code == 0
    assert "--depart-at" in result.output


def test_naive_departure_uses_configured_timezone() -> None:
    parsed = _parse_departure("2026-07-30T08:00", "Europe/Paris")
    assert parsed.tzinfo == ZoneInfo("Europe/Paris")


class FakePlaces:
    async def stations(self, query: str) -> list[Station]:
        assert query == "Bourg-la-Reine"
        return [
            Station(
                id="stop_area:bus",
                name="Bus stop",
                location=Location(latitude=48.78, longitude=2.31),
                line_ids=("line:bus",),
            ),
            Station(
                id="stop_area:rer",
                name="RER station",
                location=Location(latitude=48.7801, longitude=2.3125),
                line_ids=("line:IDFM:C01743",),
            ),
        ]


@pytest.mark.asyncio
async def test_candidate_resolution_prefers_required_line() -> None:
    stations = await _resolve_candidate_stations(
        [
            CandidateStation(
                query="Bourg-la-Reine",
                id="0",
                label="Bike station",
                required_line_id="line:IDFM:C01743",
            )
        ],
        FakePlaces(),
    )
    assert stations[0].id == "stop_area:rer"
    assert stations[0].name == "Bike station"
