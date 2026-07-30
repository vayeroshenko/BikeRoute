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
    _confirm_outbound_selection,
    _effective_bike_thresholds,
    _effective_walking_leg_limit,
    _effective_walking_limit,
    _parse_departure,
    _render_outbound_plan,
    _resolve_candidate_stations,
    _select_bike_stations,
    app,
)
from idf_commute.config import AppConfig, CandidateStation, StationRangeConfig
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
from idf_commute.domain.state import BikeLocation, BikeState
from idf_commute.persistence import BikeStateStore
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


def test_bike_state_can_be_inspected_and_corrected_without_api_access(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
locations:
  home: {latitude: 48.8, longitude: 2.3}
  work: {latitude: 48.7, longitude: 2.4}
state:
  sqlite_path: state/commute.sqlite3
""",
        encoding="utf-8",
    )

    unknown = runner.invoke(app, ["bike", "status", "--config", str(config)])
    assert unknown.exit_code == 0
    assert "Bicycle location: unknown" in unknown.output
    assert "no saved state" in unknown.output

    parked = runner.invoke(
        app,
        [
            "bike",
            "set-station",
            "stop_area:IDFM:70033",
            "--name",
            "Bourg-la-Reine",
            "--config",
            str(config),
        ],
    )
    assert parked.exit_code == 0
    assert "Bourg-la-Reine" in parked.output
    assert "stop_area:IDFM:70033" in parked.output
    assert (tmp_path / "state" / "commute.sqlite3").exists()

    home = runner.invoke(app, ["bike", "set-home", "--config", str(config)])
    assert home.exit_code == 0
    assert "Bicycle location: home" in home.output


def test_plan_outbound_help_is_available() -> None:
    result = runner.invoke(app, ["plan", "outbound", "--help"])
    assert result.exit_code == 0
    assert "--depart-at" in result.output
    assert "--max-bike-minutes" in result.output
    assert "--max-walking-minutes" in result.output
    assert "--max-walking-leg-mi" in result.output
    assert "--max-results" in result.output
    assert "--bike-station" in result.output
    assert "--bike-station-range" in result.output
    assert "--confirm-rank" in result.output
    assert "--score-mode" in result.output


def test_plan_return_help_is_available() -> None:
    result = runner.invoke(app, ["plan", "return", "--help"])
    assert result.exit_code == 0
    assert "--depart-at" in result.output
    assert "--max-bike-minutes" in result.output
    assert "--max-walking-minutes" in result.output
    assert "--max-walking-leg-mi" in result.output
    assert "--max-results" in result.output
    assert "--confirm-rank" in result.output
    assert "--score-mode" in result.output


def test_plan_return_stops_before_api_access_when_bicycle_is_not_at_station(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
locations:
  home: {latitude: 48.8, longitude: 2.3}
  work: {latitude: 48.7, longitude: 2.4}
state:
  sqlite_path: state/commute.sqlite3
""",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "plan",
            "return",
            "--config",
            str(config),
            "--depart-at",
            "2026-07-30T18:00:00+02:00",
        ],
        env={"PRIM_API_KEY": ""},
    )

    assert result.exit_code == 2
    assert "requires the bicycle to be recorded at" in result.output
    assert "a station" in result.output
    assert "PRIM_API_KEY is missing" not in result.output


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


class FakeStationProvider(FakePlaces):
    async def line_stations(self, line_id: str) -> list[Station]:
        assert line_id == "line:IDFM:C01743"
        return [
            Station(
                id="stop_area:near",
                name="Bourg-la-Reine",
                location=Location(latitude=48.78, longitude=2.31),
                line_ids=(line_id,),
            ),
            Station(
                id="stop_area:far",
                name="Aéroport CDG",
                location=Location(latitude=49.0, longitude=2.57),
                line_ids=(line_id,),
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


@pytest.mark.asyncio
async def test_best_station_search_prefilters_rer_b_by_bicycle_radius(
    app_config: AppConfig,
) -> None:
    stations = await _select_bike_stations(
        app_config,
        FakeStationProvider(),
        "best",
        max_bike_minutes=30,
    )
    assert [station.id for station in stations] == ["stop_area:near"]


@pytest.mark.asyncio
async def test_explicit_station_accepts_normalized_name(app_config: AppConfig) -> None:
    app_config.bicycle.best_station_ranges = [
        StationRangeConfig(start="Laplace", end="Sceaux")
    ]
    stations = await _select_bike_stations(
        app_config,
        FakeStationProvider(),
        "Bourg La Reine",
        max_bike_minutes=30,
    )
    assert [station.id for station in stations] == ["stop_area:near"]


def test_bike_limit_override_is_per_run_and_can_be_stricter_than_preference() -> None:
    assert _effective_bike_thresholds(20, 25, None) == (20, 25)
    assert _effective_bike_thresholds(20, 25, 22) == (20, 22)
    assert _effective_bike_thresholds(20, 25, 18) == (18, 18)
    with pytest.raises(ValueError, match="greater than zero"):
        _effective_bike_thresholds(20, 25, 0)


def test_walking_limit_override_is_per_run() -> None:
    assert _effective_walking_limit(30, None) == 30
    assert _effective_walking_limit(30, 12) == 12
    with pytest.raises(ValueError, match="greater than zero"):
        _effective_walking_limit(30, 0)
    assert _effective_walking_leg_limit(20, None) == 20
    assert _effective_walking_leg_limit(20, 8) == 8
    with pytest.raises(ValueError, match="greater than zero"):
        _effective_walking_leg_limit(20, 0)


def test_outbound_confirmation_only_moves_a_bicycle_from_home(tmp_path: Path) -> None:
    departure = datetime(2026, 7, 30, 8, 0, tzinfo=ZoneInfo("Europe/Paris"))
    journey = TransitJourney(
        id="journey:bike",
        duration_seconds=30 * 60,
        departure=departure + timedelta(minutes=20),
        arrival=departure + timedelta(minutes=50),
        legs=(),
    )
    score = ScoreBreakdown(
        door_to_door_minutes=50,
        bike_penalty_minutes=0,
        transfer_penalty_minutes=0,
        disruption_penalty_minutes=0,
        freshness_penalty_minutes=0,
        cycling_comfort_penalty_minutes=0,
        total_minutes=50,
    )
    bike_option = OutboundOption(
        kind=OutboundOptionKind.BIKE_TRANSIT,
        station=Station(id="stop_area:sceaux", name="Sceaux"),
        bike_route=BikeRoute(
            title="RECOMMENDED",
            duration_seconds=20 * 60,
            distance_m=5000,
        ),
        transit_journey=journey,
        departure=departure,
        arrival=journey.arrival,
        score=score,
    )
    baseline = OutboundOption(
        kind=OutboundOptionKind.ALL_TRANSIT,
        transit_journey=journey,
        departure=departure,
        arrival=journey.arrival,
        score=score,
    )
    plan = OutboundPlan(
        requested_departure=departure,
        preferred_bike_minutes=20,
        max_bike_minutes=30,
        options=(baseline, bike_option),
    )
    store = BikeStateStore(tmp_path / "commute.sqlite3")

    with pytest.raises(ValueError, match="contains 2 option"):
        _confirm_outbound_selection(plan, 3, store)
    with pytest.raises(ValueError, match="recorded at home"):
        _confirm_outbound_selection(plan, 2, store)
    assert store.load().location.value == "unknown"

    home = store.set_home(updated_at=departure)
    assert _confirm_outbound_selection(plan, 1, store) is None
    assert store.load() == home

    parked = _confirm_outbound_selection(plan, 2, store)
    assert parked is not None
    assert parked.station_id == "stop_area:sceaux"
    assert parked.station_name == "Sceaux"
    assert parked.source_journey_id == "journey:bike"


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
                equivalent_line_codes=("197", "197B"),
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
    assert "Bus 197 / 197B" in rendered
    assert "Bus 197 / 197B → Bourg-la-Reine" not in rendered
    assert "Laplace RER → Bourg-la-Reine RER" in rendered
    assert "realtime · +2 min vs schedule" in rendered
    assert "Trains do not stop at Laplace; use bus 197 & RER B." in rendered
    assert "<p>" not in rendered


def test_plan_output_explains_suppressed_bike_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = StringIO()
    monkeypatch.setattr(
        cli_module,
        "console",
        Console(file=output, width=180, color_system=None),
    )
    departure = datetime(2026, 7, 30, 8, 0, tzinfo=ZoneInfo("Europe/Paris"))

    _render_outbound_plan(
        OutboundPlan(
            requested_departure=departure,
            preferred_bike_minutes=20,
            max_bike_minutes=30,
            bike_state=BikeState(
                location=BikeLocation.STATION,
                station_id="stop_area:sceaux",
                station_name="Sceaux",
            ),
            options=(),
        )
    )

    rendered = output.getvalue()
    assert "Bike options suppressed" in rendered
    assert "bicycle location is Sceaux" in rendered
    assert "bike set-home" in rendered
