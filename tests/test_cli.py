from __future__ import annotations

from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from rich.console import Console
from typer.testing import CliRunner

import idf_commute.cli as cli_module
from idf_commute.cli import (
    _effective_bike_thresholds,
    _parse_departure,
    _render_outbound_plan,
    _resolve_candidate_stations,
    app,
)
from idf_commute.config import CandidateStation
from idf_commute.domain.models import (
    BikeRoute,
    Disruption,
    Freshness,
    Location,
    OutboundOption,
    OutboundOptionKind,
    ScoreBreakdown,
    Station,
    TransitJourney,
    TransitLeg,
)
from idf_commute.planning.models import OutboundPlan

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
    assert "--max-bike-minutes" in result.output
    assert "--max-results" in result.output


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


def test_bike_limit_override_is_per_run_and_can_be_stricter_than_preference() -> None:
    assert _effective_bike_thresholds(20, 25, None) == (20, 25)
    assert _effective_bike_thresholds(20, 25, 22) == (20, 22)
    assert _effective_bike_thresholds(20, 25, 18) == (18, 18)
    with pytest.raises(ValueError, match="greater than zero"):
        _effective_bike_thresholds(20, 25, 0)


def test_plan_output_shows_bike_transit_legs_freshness_and_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = StringIO()
    monkeypatch.setattr(
        cli_module,
        "console",
        Console(file=output, width=180, color_system=None),
    )
    departure = datetime(2026, 7, 30, 8, 0, tzinfo=ZoneInfo("Europe/Paris"))
    transit_departure = departure + timedelta(minutes=30)
    journey = TransitJourney(
        status="SIGNIFICANT_DELAYS",
        duration_seconds=40 * 60,
        departure=transit_departure,
        arrival=departure + timedelta(minutes=70),
        transfers=1,
        response_timestamp=departure + timedelta(hours=5),
        legs=(
            TransitLeg(
                type="street_network",
                mode="walking",
                duration_seconds=10 * 60,
                departure=transit_departure,
                arrival=transit_departure + timedelta(minutes=10),
            ),
            TransitLeg(
                type="public_transport",
                duration_seconds=17 * 60,
                departure=transit_departure + timedelta(minutes=10),
                arrival=transit_departure + timedelta(minutes=27),
                base_departure=transit_departure + timedelta(minutes=8),
                freshness=Freshness.REALTIME,
                commercial_mode="Bus",
                line_code="197",
                direction="Bourg-la-Reine",
                origin_name="Laplace RER",
                destination_name="Bourg-la-Reine RER",
            ),
        ),
    )
    score = ScoreBreakdown(
        door_to_door_minutes=70,
        bike_penalty_minutes=1,
        transfer_penalty_minutes=4,
        disruption_penalty_minutes=12,
        freshness_penalty_minutes=0,
        cycling_comfort_penalty_minutes=1,
        total_minutes=88,
    )
    option = OutboundOption(
        kind=OutboundOptionKind.BIKE_TRANSIT,
        station=Station(id="laplace", name="Laplace"),
        bike_route=BikeRoute(
            title="RECOMMENDED",
            duration_seconds=21 * 60,
            distance_m=5600,
            recommended_roads_m=4200,
            discouraged_roads_m=280,
            vertical_gain_m=45,
            vertical_loss_m=31,
            average_speed_kmh=17,
            facility_distances_m={"cycleway": 3200},
        ),
        transit_journey=journey,
        departure=departure,
        arrival=journey.arrival,
        parking_buffer_seconds=4 * 60,
        matched_disruptions=(
            Disruption(
                id="works",
                title="RER B summer works",
                message="<p>Trains do not stop at Laplace; use bus 197 &amp; RER B.</p>",
                severity="PERTURBEE",
                source="test",
            ),
        ),
        score=score,
    )
    _render_outbound_plan(
        OutboundPlan(
            requested_departure=departure,
            preferred_bike_minutes=20,
            max_bike_minutes=30,
            options=(option,),
        )
    )

    rendered = output.getvalue()
    assert "Bike RECOMMENDED: 5.6 km in 21 min" in rendered
    assert "Bus 197" in rendered
    assert "Bus 197 → Bourg-la-Reine" in rendered
    assert "Laplace RER → Bourg-la-Reine RER" in rendered
    assert "realtime · +2 min vs schedule" in rendered
    assert "Trains do not stop at Laplace; use bus 197 & RER B." in rendered
    assert "<p>" not in rendered
