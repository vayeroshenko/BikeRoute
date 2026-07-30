from __future__ import annotations

import asyncio
import html
import math
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from idf_commute.config import (
    AppConfig,
    CandidateStation,
    MissingAccessError,
    Settings,
    load_config,
    safe_settings_summary,
)
from idf_commute.domain.models import (
    BikeRoute,
    Freshness,
    Location,
    OutboundOption,
    OutboundOptionKind,
    Station,
    TransitLeg,
)
from idf_commute.domain.state import BikeLocation, BikeState
from idf_commute.persistence import BikeStateStore
from idf_commute.planning.models import (
    OutboundPlan,
    OutboundPlanningRequest,
    ReturnPlan,
    ReturnPlanningRequest,
)
from idf_commute.planning.planner import (
    OutboundPlanner,
    ReturnPlanner,
    longest_walking_leg_minutes,
    walking_duration_minutes,
)
from idf_commute.planning.scoring import ScoreMode, ScoreWeights, weights_for_mode
from idf_commute.planning.stations import (
    parse_station_range,
    rer_b_stations_in_ranges,
)
from idf_commute.probe import (
    ProbeError,
    ProbeResult,
    ProbeRunner,
    summarize_result,
    validate_live_config,
)
from idf_commute.providers import (
    BulkDisruptionAdapter,
    GeoveloAdapter,
    NavitiaAdapter,
    PrimClient,
)
from idf_commute.providers.prim_client import PrimError
from idf_commute.providers.protocols import PlaceProvider, StationProvider

app = typer.Typer(
    name="idf-commute",
    help="Île-de-France bicycle and transit commute planner.",
    no_args_is_help=True,
)
probe_app = typer.Typer(help="Safely probe authenticated PRIM APIs.", no_args_is_help=True)
plan_app = typer.Typer(help="Plan explicit bicycle and transit journeys.", no_args_is_help=True)
bike_app = typer.Typer(help="Inspect or correct the persisted bicycle location.")
app.add_typer(probe_app, name="probe")
app.add_typer(plan_app, name="plan")
app.add_typer(bike_app, name="bike")
console = Console()
ConfigPath = Annotated[Path, typer.Option(exists=True, dir_okay=False)]
FixturePath = Annotated[Path, typer.Option()]
DryRun = Annotated[bool, typer.Option(help="Validate and show redacted settings only.")]


def _bike_state_store(config_path: Path) -> BikeStateStore:
    config = load_config(config_path)
    sqlite_path = config.state.sqlite_path
    if not sqlite_path.is_absolute():
        sqlite_path = config_path.parent / sqlite_path
    return BikeStateStore(sqlite_path)


def _render_bike_state(state: BikeState) -> None:
    if state.location is BikeLocation.STATION:
        station = state.station_name or state.station_id
        console.print(f"Bicycle location: [bold]station[/bold] — {station}")
        if state.station_name and state.station_id:
            console.print(f"Station ID: {state.station_id}")
    else:
        console.print(f"Bicycle location: [bold]{state.location.value}[/bold]")
    if state.updated_at is None:
        console.print("Last updated: never (no saved state)")
    else:
        console.print(f"Last updated: {state.updated_at:%Y-%m-%d %H:%M:%S %Z}")


def _run_bike_state_command(config_path: Path, action: str, **values: str | None) -> None:
    try:
        store = _bike_state_store(config_path)
        if action == "status":
            state = store.load()
        elif action == "home":
            state = store.set_home()
        elif action == "station":
            station_id = values.get("station_id")
            if not station_id:
                raise ValueError("station_id is required")
            state = store.set_station(
                station_id,
                station_name=values.get("station_name"),
            )
        elif action == "unknown":
            state = store.set_unknown()
        else:
            raise ValueError(f"Unsupported bicycle state action {action!r}")
    except (FileNotFoundError, ValueError, ValidationError, sqlite3.Error) as exc:
        console.print(f"[bold red]Bicycle state stopped:[/bold red] {exc}")
        raise typer.Exit(code=2) from None
    _render_bike_state(state)


def _confirm_outbound_selection(
    plan: OutboundPlan,
    rank: int,
    store: BikeStateStore,
) -> BikeState | None:
    if rank < 1 or rank > len(plan.options):
        raise ValueError(
            f"Cannot confirm rank {rank}; this plan contains {len(plan.options)} option(s)"
        )
    option = plan.options[rank - 1]
    if option.kind is OutboundOptionKind.ALL_TRANSIT:
        return None
    current = store.load()
    if current.location is not BikeLocation.HOME:
        raise ValueError(
            "Cannot confirm a bike outbound route unless the bicycle is recorded "
            "at home; use 'idf-commute bike set-home' after checking its location"
        )
    if option.station is None:
        raise ValueError("Selected bike route has no station")
    return store.set_station(
        option.station.id,
        station_name=option.station.name,
        source_journey_id=option.transit_journey.id,
    )


def _confirm_return_selection(
    plan: ReturnPlan,
    rank: int,
    store: BikeStateStore,
) -> BikeState:
    if rank < 1 or rank > len(plan.options):
        raise ValueError(
            f"Cannot confirm rank {rank}; this plan contains {len(plan.options)} option(s)"
        )
    option = plan.options[rank - 1]
    current = store.load()
    if (
        current.location is not BikeLocation.STATION
        or current.station_id != option.station.id
    ):
        raise ValueError(
            "Cannot confirm return because the current bicycle state no longer "
            f"matches {option.station.name} ({option.station.id})"
        )
    return store.set_home(source_journey_id=option.transit_journey.id)


@bike_app.command("status")
def bike_status(config: ConfigPath = Path("config.yaml")) -> None:
    """Show where the planner currently believes the bicycle is."""
    _run_bike_state_command(config, "status")


@bike_app.command("set-home")
def bike_set_home(config: ConfigPath = Path("config.yaml")) -> None:
    """Explicitly record that the bicycle is at home."""
    _run_bike_state_command(config, "home")


@bike_app.command("set-station")
def bike_set_station(
    station_id: Annotated[str, typer.Argument(help="Stable Navitia stop-area ID.")],
    config: ConfigPath = Path("config.yaml"),
    station_name: Annotated[
        str | None,
        typer.Option("--name", help="Optional station name for human-readable status."),
    ] = None,
) -> None:
    """Explicitly record that the bicycle is parked at a station."""
    _run_bike_state_command(
        config,
        "station",
        station_id=station_id,
        station_name=station_name,
    )


@bike_app.command("set-unknown")
def bike_set_unknown(config: ConfigPath = Path("config.yaml")) -> None:
    """Clear certainty about the bicycle location."""
    _run_bike_state_command(config, "unknown")


async def _run_probe(
    kind: Literal["all", "identifiers", "navitia", "disruptions", "geovelo", "stop-monitoring"],
    config_path: Path,
    fixture_dir: Path,
    dry_run: bool,
) -> None:
    config = load_config(config_path)
    settings = Settings()
    if dry_run:
        console.print("[bold]Dry run[/bold] — no network requests made.")
        for key, value in safe_settings_summary(settings).items():
            console.print(f"{key}: {value}")
        console.print(f"probe: {kind}")
        console.print(f"fixture directory: {fixture_dir}")
        return

    if kind in {"all", "navitia", "geovelo"}:
        validate_live_config(config, require_station=kind in {"all", "navitia"})
    async with ProbeRunner(settings, config, fixture_dir) as runner:
        method = getattr(runner, kind.replace("-", "_"))
        results: list[ProbeResult] = await method()
    table = Table("Probe", "HTTP", "Observation", "Sanitized fixture")
    for result in results:
        table.add_row(
            result.name,
            str(result.status_code),
            summarize_result(result),
            str(result.fixture_path),
        )
    console.print(table)


def _probe_command(
    kind: Literal["all", "identifiers", "navitia", "disruptions", "geovelo", "stop-monitoring"],
    config: Path,
    fixture_dir: Path,
    dry_run: bool,
) -> None:
    try:
        asyncio.run(_run_probe(kind, config, fixture_dir, dry_run))
    except (FileNotFoundError, ValueError, ValidationError, MissingAccessError, ProbeError) as exc:
        console.print(f"[bold red]Probe stopped:[/bold red] {exc}")
        raise typer.Exit(code=2) from None


@probe_app.command("all")
def probe_all(
    config: ConfigPath = Path("config.yaml"),
    fixture_dir: FixturePath = Path("tests/fixtures/live"),
    dry_run: DryRun = False,
) -> None:
    """Run required probes, plus Stop Monitoring when configured."""
    _probe_command("all", config, fixture_dir, dry_run)


@probe_app.command("navitia")
def probe_navitia(
    config: ConfigPath = Path("config.yaml"),
    fixture_dir: FixturePath = Path("tests/fixtures/live"),
    dry_run: DryRun = False,
) -> None:
    """Resolve station/line IDs, capture a journey, and test park_mode."""
    _probe_command("navitia", config, fixture_dir, dry_run)


@probe_app.command("identifiers")
def probe_identifiers(
    config: ConfigPath = Path("config.yaml"),
    fixture_dir: FixturePath = Path("tests/fixtures/live"),
    dry_run: DryRun = False,
) -> None:
    """Resolve configured RER and bus line queries to stable Navitia IDs."""
    _probe_command("identifiers", config, fixture_dir, dry_run)


@probe_app.command("disruptions")
def probe_disruptions(
    config: ConfigPath = Path("config.yaml"),
    fixture_dir: FixturePath = Path("tests/fixtures/live"),
    dry_run: DryRun = False,
) -> None:
    """Capture the configured PRIM disruptions endpoint."""
    _probe_command("disruptions", config, fixture_dir, dry_run)


@probe_app.command("geovelo")
def probe_geovelo(
    config: ConfigPath = Path("config.yaml"),
    fixture_dir: FixturePath = Path("tests/fixtures/live"),
    dry_run: DryRun = False,
) -> None:
    """Capture Geovelo alternatives with rich bicycle fields requested."""
    _probe_command("geovelo", config, fixture_dir, dry_run)


@probe_app.command("stop-monitoring")
def probe_stop_monitoring(
    config: ConfigPath = Path("config.yaml"),
    fixture_dir: FixturePath = Path("tests/fixtures/live"),
    dry_run: DryRun = False,
) -> None:
    """Capture optional Stop Monitoring data for a configured stop."""
    _probe_command("stop-monitoring", config, fixture_dir, dry_run)


async def _run_outbound_plan(
    config_path: Path,
    depart_at_text: str,
    max_bike_minutes: float | None,
    max_walking_minutes: float | None,
    max_walking_leg_minutes: float | None,
    max_results: int,
    bike_station: str | None,
    bike_station_ranges: list[str] | None,
    score_mode: ScoreMode | None,
) -> OutboundPlan:
    config = load_config(config_path)
    settings = Settings()
    departure = _parse_departure(depart_at_text, config.timezone)
    preferred_minutes, hard_minutes = _effective_bike_thresholds(
        config.bicycle.preferred_bike_minutes,
        config.bicycle.max_bike_minutes,
        max_bike_minutes,
    )
    walking_limit = _effective_walking_limit(
        config.walking.max_minutes,
        max_walking_minutes,
    )
    walking_leg_limit = _effective_walking_leg_limit(
        config.walking.max_leg_minutes,
        max_walking_leg_minutes,
    )
    active_score_mode = score_mode or config.scoring.mode
    score_weight_overrides = config.scoring.weights.model_dump(exclude_none=True)
    score_weights = weights_for_mode(active_score_mode, score_weight_overrides)
    bike_state = _bike_state_store(config_path).load()
    async with PrimClient(
        settings.require_api_key(),
        api_key_header=settings.api_key_header,
        timeout_seconds=settings.timeout_seconds,
    ) as client:
        navitia = NavitiaAdapter(client, str(settings.navitia_base_url))
        stations = (
            await _select_bike_stations(
                config,
                navitia,
                bike_station,
                hard_minutes,
                bike_station_ranges,
            )
            if bike_state.location is BikeLocation.HOME
            else []
        )
        planner = OutboundPlanner(
            bike_router=GeoveloAdapter(client, str(settings.geovelo_url)),
            transit_router=navitia,
            disruption_provider=BulkDisruptionAdapter(
                client,
                str(settings.disruptions_url),
            ),
        )
        return await planner.plan(
            OutboundPlanningRequest(
                home=Location(
                    latitude=config.locations.home.latitude,
                    longitude=config.locations.home.longitude,
                    label="HOME",
                ),
                home_transit_id=config.locations.home.navitia_coord,
                work_transit_id=config.locations.work.navitia_coord,
                candidate_stations=tuple(stations),
                depart_at=departure,
                preferred_bike_minutes=preferred_minutes,
                max_bike_minutes=hard_minutes,
                max_walking_minutes=walking_limit,
                max_walking_leg_minutes=walking_leg_limit,
                parking_buffer_minutes=config.bicycle.parking_buffer_minutes,
                bike_profile=config.bicycle.profile,
                bike_type=config.bicycle.bike_type,
                bike_average_speed_kmh=config.bicycle.average_speed_kmh,
                max_results=max_results,
                score_mode=active_score_mode,
                score_weights=score_weights,
                bike_state=bike_state,
            )
        )


async def _run_return_plan(
    config_path: Path,
    depart_at_text: str,
    max_bike_minutes: float | None,
    max_walking_minutes: float | None,
    max_walking_leg_minutes: float | None,
    max_results: int,
    score_mode: ScoreMode | None,
) -> ReturnPlan:
    config = load_config(config_path)
    bike_state = _bike_state_store(config_path).load()
    if bike_state.location is not BikeLocation.STATION or not bike_state.station_id:
        raise ValueError(
            "Return planning requires the bicycle to be recorded at a station; "
            "check with 'idf-commute bike status'"
        )
    settings = Settings()
    departure = _parse_departure(depart_at_text, config.timezone)
    preferred_minutes, hard_minutes = _effective_bike_thresholds(
        config.bicycle.preferred_bike_minutes,
        config.bicycle.max_bike_minutes,
        max_bike_minutes,
    )
    walking_limit = _effective_walking_limit(
        config.walking.max_minutes,
        max_walking_minutes,
    )
    walking_leg_limit = _effective_walking_leg_limit(
        config.walking.max_leg_minutes,
        max_walking_leg_minutes,
    )
    active_score_mode = score_mode or config.scoring.mode
    score_weight_overrides = config.scoring.weights.model_dump(exclude_none=True)
    score_weights = weights_for_mode(active_score_mode, score_weight_overrides)
    async with PrimClient(
        settings.require_api_key(),
        api_key_header=settings.api_key_header,
        timeout_seconds=settings.timeout_seconds,
    ) as client:
        navitia = NavitiaAdapter(client, str(settings.navitia_base_url))
        stations = await _select_bike_stations(
            config,
            navitia,
            bike_state.station_id,
            hard_minutes,
        )
        station = stations[0]
        planner = ReturnPlanner(
            bike_router=GeoveloAdapter(client, str(settings.geovelo_url)),
            transit_router=navitia,
            disruption_provider=BulkDisruptionAdapter(
                client,
                str(settings.disruptions_url),
            ),
        )
        return await planner.plan(
            ReturnPlanningRequest(
                work_transit_id=config.locations.work.navitia_coord,
                home=Location(
                    latitude=config.locations.home.latitude,
                    longitude=config.locations.home.longitude,
                    label="HOME",
                ),
                bike_station=station,
                bike_state=bike_state,
                depart_at=departure,
                preferred_bike_minutes=preferred_minutes,
                max_bike_minutes=hard_minutes,
                max_walking_minutes=walking_limit,
                max_walking_leg_minutes=walking_leg_limit,
                retrieval_buffer_minutes=config.bicycle.parking_buffer_minutes,
                bike_profile=config.bicycle.profile,
                bike_type=config.bicycle.bike_type,
                bike_average_speed_kmh=config.bicycle.average_speed_kmh,
                max_results=max_results,
                score_mode=active_score_mode,
                score_weights=score_weights,
            )
        )


async def _resolve_candidate_stations(
    candidates: list[CandidateStation],
    places: PlaceProvider,
) -> list[Station]:
    resolved: list[Station] = []
    for candidate in candidates:
        matches = await places.stations(candidate.query)
        valid_id = candidate.id not in {None, "", "0", "REPLACE_WITH_NAVITIA_STOP_AREA_ID"}
        selected = next(
            (station for station in matches if valid_id and station.id == candidate.id),
            None,
        )
        if selected is None and candidate.required_line_id:
            selected = next(
                (station for station in matches if candidate.required_line_id in station.line_ids),
                None,
            )
        if selected is None and matches:
            selected = matches[0]
        if selected is None or selected.location is None:
            raise ValueError(
                f"Could not resolve candidate station {candidate.label!r} with coordinates"
            )
        resolved.append(
            selected.model_copy(
                update={
                    "name": candidate.label,
                    "location": selected.location.model_copy(update={"label": candidate.label}),
                }
            )
        )
    return resolved


async def _select_bike_stations(
    config: AppConfig,
    stations_provider: StationProvider,
    selection: str | None,
    max_bike_minutes: float,
    range_overrides: list[str] | None = None,
) -> list[Station]:
    configured_ranges = [
        (station_range.start, station_range.end)
        for station_range in config.bicycle.best_station_ranges
    ]
    ranges = (
        [parse_station_range(value) for value in range_overrides]
        if range_overrides
        else configured_ranges
    )
    if range_overrides and (selection or "").casefold() != "best":
        raise ValueError(
            "--bike-station-range can only be used with --bike-station best"
        )
    if selection is None:
        if not config.candidate_stations:
            raise ValueError(
                "No configured candidate stations; use --bike-station best or a "
                "RER B station name/ID"
            )
        return await _resolve_candidate_stations(
            config.candidate_stations,
            stations_provider,
        )

    line_id = config.bicycle.target_line_id
    line_stations = await stations_provider.line_stations(line_id)
    usable = [station for station in line_stations if station.location is not None]
    if selection.casefold() == "best":
        if ranges:
            if line_id != "line:IDFM:C01743":
                raise ValueError("Station ranges currently support RER B only")
            usable = rer_b_stations_in_ranges(usable, ranges)
        radius_km = (
            config.bicycle.average_speed_kmh * max_bike_minutes / 60 * 1.5
        )
        nearby = [
            station
            for station in usable
            if station.location is not None
            and _distance_km(
                config.locations.home.latitude,
                config.locations.home.longitude,
                station.location.latitude,
                station.location.longitude,
            )
            <= radius_km
        ]
        if not nearby:
            raise ValueError(
                f"No {config.bicycle.target_line_label} stations found within the "
                "automatic bicycle search radius"
            )
        return nearby

    selected = _match_line_station(usable, selection)
    if selected is None:
        raise ValueError(
            f"Could not find {config.bicycle.target_line_label} station {selection!r}; "
            "use its station name, stop-area ID, or --bike-station best"
        )
    return [selected]


def _match_line_station(stations: list[Station], selection: str) -> Station | None:
    exact_id = next((station for station in stations if station.id == selection), None)
    if exact_id is not None:
        return exact_id
    key = _station_key(selection)
    exact_name = next(
        (station for station in stations if _station_key(station.name) == key),
        None,
    )
    if exact_name is not None:
        return exact_name
    partial = [station for station in stations if key in _station_key(station.name)]
    return partial[0] if len(partial) == 1 else None


def _station_key(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in decomposed if character.isalnum())


def _distance_km(
    origin_latitude: float,
    origin_longitude: float,
    destination_latitude: float,
    destination_longitude: float,
) -> float:
    lat1, lon1, lat2, lon2 = map(
        math.radians,
        (
            origin_latitude,
            origin_longitude,
            destination_latitude,
            destination_longitude,
        ),
    )
    latitude_delta = lat2 - lat1
    longitude_delta = lon2 - lon1
    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(longitude_delta / 2) ** 2
    )
    return 2 * 6371.0088 * math.asin(math.sqrt(haversine))


def _parse_departure(value: str, timezone: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            "--depart-at must be ISO 8601, for example 2026-07-30T08:00+02:00"
        ) from exc
    zone = ZoneInfo(timezone)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def _effective_bike_thresholds(
    configured_preferred: float,
    configured_maximum: float,
    override_maximum: float | None,
) -> tuple[float, float]:
    maximum = configured_maximum if override_maximum is None else override_maximum
    if maximum <= 0:
        raise ValueError("--max-bike-minutes must be greater than zero")
    return min(configured_preferred, maximum), maximum


def _effective_walking_limit(
    configured_maximum: float,
    override_maximum: float | None,
) -> float:
    maximum = configured_maximum if override_maximum is None else override_maximum
    if maximum <= 0:
        raise ValueError("--max-walking-minutes must be greater than zero")
    return maximum


def _effective_walking_leg_limit(
    configured_maximum: float,
    override_maximum: float | None,
) -> float:
    maximum = configured_maximum if override_maximum is None else override_maximum
    if maximum <= 0:
        raise ValueError("--max-walking-leg-minutes must be greater than zero")
    return maximum


def _render_outbound_plan(plan: OutboundPlan) -> None:
    if plan.bike_state.location is not BikeLocation.HOME:
        location = (
            plan.bike_state.station_name
            or plan.bike_state.station_id
            or plan.bike_state.location.value
        )
        console.print(
            "[yellow]Bike options suppressed:[/yellow] bicycle location is "
            f"[bold]{location}[/bold]. Use 'idf-commute bike set-home' only "
            "after checking that the bicycle is home."
        )
    console.print(
        f"Bike thresholds: preferred {plan.preferred_bike_minutes:g} min, "
        f"hard maximum {plan.max_bike_minutes:g} min"
    )
    console.print(f"Bike stations evaluated: {plan.candidate_station_count}")
    console.print(f"Walking hard maximum: {plan.max_walking_minutes:g} min")
    console.print(
        f"Walking-leg hard maximum: {plan.max_walking_leg_minutes:g} min"
    )
    console.print(
        f"Score mode: {plan.score_mode.value} "
        f"({_score_weights_summary(plan.score_weights)})"
    )
    table = Table(
        "Rank",
        "Type",
        "Station",
        "Bike",
        "Walk total/max",
        "Transit",
        "Arrival",
        "Score",
        "Alerts",
    )
    for rank, option in enumerate(plan.options, start=1):
        bike = (
            f"{option.bike_route.duration_seconds / 60:.0f} min ({option.bike_route.title})"
            if option.bike_route
            else "—"
        )
        table.add_row(
            str(rank),
            "bike + transit" if option.kind is OutboundOptionKind.BIKE_TRANSIT else "all transit",
            option.station.name if option.station else "—",
            bike,
            f"{walking_duration_minutes(option.transit_journey):.1f} / "
            f"{longest_walking_leg_minutes(option.transit_journey):.1f} min",
            _transit_summary(option.transit_journey.legs),
            option.arrival.strftime("%H:%M"),
            f"{option.score.total_minutes:.1f}",
            str(len(option.matched_disruptions)),
        )
    console.print(table)
    if plan.options:
        console.print("\n[bold]Detailed itineraries[/bold]")
    for rank, option in enumerate(plan.options, start=1):
        _render_option_details(rank, option, plan.score_weights)
    if plan.rejections:
        console.print(f"[yellow]{len(plan.rejections)} route(s) rejected.[/yellow]")
        for rejection in plan.rejections:
            bike_duration = (
                f", {rejection.bike_duration_minutes:.1f} min"
                if rejection.bike_duration_minutes is not None
                else ""
            )
            walking_duration = (
                f", walk {rejection.walking_duration_minutes:.1f} min"
                if rejection.walking_duration_minutes is not None
                else ""
            )
            walking_leg_duration = (
                f", longest walk {rejection.walking_leg_duration_minutes:.1f} min"
                if rejection.walking_leg_duration_minutes is not None
                else ""
            )
            console.print(
                f"- {rejection.station_name or rejection.station_id} / "
                f"{rejection.bike_route_title or 'no bike route'}"
                f"{bike_duration}{walking_duration}{walking_leg_duration}: "
                f"{rejection.reason}"
            )


def _render_return_plan(plan: ReturnPlan) -> None:
    station = plan.bike_state.station_name or plan.bike_state.station_id
    console.print(f"Retrieving bicycle from: [bold]{station}[/bold]")
    console.print(
        f"Bike thresholds: preferred {plan.preferred_bike_minutes:g} min, "
        f"hard maximum {plan.max_bike_minutes:g} min"
    )
    console.print(
        f"Walking limits: {plan.max_walking_minutes:g} min total, "
        f"{plan.max_walking_leg_minutes:g} min per leg"
    )
    table = Table(
        "Rank",
        "Transit to bicycle",
        "Walk total/max",
        "Bike home",
        "Home",
        "Score",
        "Alerts",
    )
    for rank, option in enumerate(plan.options, start=1):
        table.add_row(
            str(rank),
            _transit_summary(option.transit_journey.legs),
            f"{walking_duration_minutes(option.transit_journey):.1f} / "
            f"{longest_walking_leg_minutes(option.transit_journey):.1f} min",
            f"{option.bike_route.duration_seconds / 60:.0f} min "
            f"({option.bike_route.title})",
            option.arrival.strftime("%H:%M"),
            f"{option.score.total_minutes:.1f}",
            str(len(option.matched_disruptions)),
        )
    console.print(table)
    for rank, option in enumerate(plan.options, start=1):
        journey = option.transit_journey
        console.rule(f"#{rank} · retrieve bicycle at {option.station.name}")
        console.print(
            f"Door to door: {option.departure:%H:%M} → {option.arrival:%H:%M} "
            f"({_format_duration(round((option.arrival - option.departure).total_seconds()))})"
        )
        _render_transit_legs(journey.legs)
        bike_start = journey.arrival + timedelta(
            seconds=option.retrieval_buffer_seconds
        )
        console.print(
            f"Retrieve bicycle: arrive {journey.arrival:%H:%M}, "
            f"unlock {option.retrieval_buffer_seconds / 60:g} min, "
            f"ride {option.bike_route.duration_seconds / 60:.0f} min "
            f"({option.bike_route.distance_m / 1000:.1f} km), "
            f"home {option.arrival:%H:%M}"
        )
        console.print(
            f"Bike starts at {bike_start:%H:%M} from exact station "
            f"{option.station.name} ({option.station.id})"
        )
        console.print(f"Score: {option.score.total_minutes:.1f}")
    if plan.rejections:
        console.print(f"[yellow]{len(plan.rejections)} route(s) rejected.[/yellow]")
        for rejection in plan.rejections:
            console.print(
                f"- {rejection.station_name or rejection.station_id} / "
                f"{rejection.bike_route_title or 'route'}: {rejection.reason}"
            )


def _render_option_details(
    rank: int,
    option: OutboundOption,
    weights: ScoreWeights,
) -> None:
    journey = option.transit_journey
    kind = (
        f"bike to {option.station.name}"
        if option.station is not None
        else "all transit"
    )
    console.rule(f"#{rank} · {kind}")
    status = f" · status {journey.status}" if journey.status else ""
    console.print(
        f"Door to door: {option.departure:%H:%M} → {option.arrival:%H:%M} "
        f"({_format_duration(round((option.arrival - option.departure).total_seconds()))})"
        f"{status}"
    )
    console.print(
        f"Walking: {walking_duration_minutes(journey):.1f} min total · "
        f"{longest_walking_leg_minutes(journey):.1f} min longest leg"
    )
    if journey.response_timestamp is not None:
        console.print(
            f"Transit response generated: "
            f"{journey.response_timestamp:%Y-%m-%d %H:%M:%S %Z}"
        )
    if option.bike_route is not None:
        _render_bike_details(option)
    _render_transit_legs(journey.legs)
    score = option.score
    console.print(
        f"Score {score.total_minutes:.1f}: "
        f"door {score.door_to_door_minutes:.1f}x{weights.door_to_door:g} + "
        f"bike {score.bike_penalty_minutes:.1f}x{weights.bike_penalty:g} + "
        f"transfers {score.transfer_penalty_minutes:.1f}x{weights.transfers:g} + "
        f"alerts {score.disruption_penalty_minutes:.1f}x{weights.disruptions:g} + "
        f"freshness {score.freshness_penalty_minutes:.1f}x{weights.freshness:g} + "
        f"comfort {score.cycling_comfort_penalty_minutes:.1f}x"
        f"{weights.cycling_comfort:g}"
    )
    displayed_alerts: set[tuple[str, str]] = set()
    for disruption in option.matched_disruptions:
        alert_key = (disruption.title, disruption.message)
        if alert_key in displayed_alerts:
            continue
        displayed_alerts.add(alert_key)
        qualifiers = " / ".join(
            value
            for value in (disruption.severity, disruption.effect, disruption.cause)
            if value
        )
        suffix = f" [{qualifiers}]" if qualifiers else ""
        console.print(f"Alert: {disruption.title}{suffix}", style="yellow", markup=False)
        message = _clean_alert_message(disruption.message)
        if message and message != disruption.title:
            console.print(f"  {message}", markup=False)


def _render_bike_details(option: OutboundOption) -> None:
    route = option.bike_route
    if route is None:
        return
    bike_arrival = option.departure + timedelta(seconds=route.duration_seconds)
    ready_at = bike_arrival + timedelta(seconds=option.parking_buffer_seconds)
    console.print(
        f"Bike {route.title}: {route.distance_m / 1000:.1f} km in "
        f"{_format_duration(route.duration_seconds)} · "
        f"elevation +{route.vertical_gain_m:.0f}/-{route.vertical_loss_m:.0f} m"
    )
    quality_parts = _bike_quality_parts(route)
    if quality_parts:
        console.print(f"Bike network: {' · '.join(quality_parts)}")
    speed = (
        f" · provider speed {route.average_speed_kmh:g} km/h"
        if route.average_speed_kmh is not None
        else ""
    )
    console.print(
        f"Bike timing: leave {option.departure:%H:%M}, arrive {bike_arrival:%H:%M}, "
        f"park {option.parking_buffer_seconds / 60:g} min, ready {ready_at:%H:%M}{speed}"
    )


def _bike_quality_parts(route: BikeRoute) -> list[str]:
    parts: list[str] = []
    if route.distance_m > 0:
        parts.extend(
            [
                f"recommended {route.recommended_roads_m / route.distance_m:.0%}",
                f"discouraged {route.discouraged_roads_m / route.distance_m:.0%}",
            ]
        )
    facilities = sorted(
        (
            (name.replace("_", " "), distance)
            for name, distance in route.facility_distances_m.items()
            if distance > 0
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    parts.extend(f"{name} {distance / 1000:.1f} km" for name, distance in facilities[:4])
    return parts


def _render_transit_legs(legs: tuple[TransitLeg, ...]) -> None:
    table = Table("Time", "Step", "Stops", "Duration", "Data", expand=True)
    for leg in legs:
        times = _leg_times(leg)
        step = _leg_step(leg)
        stops = (
            f"{leg.origin_name or '?'} → {leg.destination_name or '?'}"
            if leg.type == "public_transport"
            else "—"
        )
        table.add_row(
            times,
            step,
            stops,
            _format_duration(leg.duration_seconds),
            _leg_data(leg),
        )
    console.print(table)


def _leg_times(leg: TransitLeg) -> str:
    if leg.departure is not None and leg.arrival is not None:
        return f"{leg.departure:%H:%M} → {leg.arrival:%H:%M}"
    return "—"


def _leg_step(leg: TransitLeg) -> str:
    if leg.type == "public_transport":
        line = _leg_line_label(leg)
        direction = (
            f" → {leg.direction}"
            if leg.direction and len(leg.equivalent_line_codes) <= 1
            else ""
        )
        return f"{line or 'Transit'}{direction}"
    labels = {
        "crow_fly": "Station access",
        "street_network": "Walk" if leg.mode == "walking" else (leg.mode or "Street"),
        "transfer": "Transfer walk" if leg.mode == "walking" else "Transfer",
        "waiting": "Wait",
        "park": "Park bicycle",
    }
    return labels.get(leg.type, leg.mode or leg.type.replace("_", " ").title())


def _transit_summary(legs: tuple[TransitLeg, ...]) -> str:
    labels = [
        _leg_line_label(leg) or leg.line_id or "transit"
        for leg in legs
        if leg.type == "public_transport"
    ]
    return " → ".join(labels) or "—"


def _leg_line_label(leg: TransitLeg) -> str:
    codes = leg.equivalent_line_codes or ((leg.line_code,) if leg.line_code else ())
    code_label = " / ".join(codes)
    return " ".join(part for part in (leg.commercial_mode, code_label) if part)


def _leg_data(leg: TransitLeg) -> str:
    if leg.freshness is Freshness.REALTIME:
        label = "realtime"
    elif leg.freshness is Freshness.BASE_SCHEDULE:
        label = "schedule only"
    else:
        return "—"
    if leg.departure is None or leg.base_departure is None:
        return label
    delay_minutes = round((leg.departure - leg.base_departure).total_seconds() / 60)
    if delay_minutes == 0:
        return f"{label} · on time"
    sign = "+" if delay_minutes > 0 else ""
    return f"{label} · {sign}{delay_minutes} min vs schedule"


def _format_duration(seconds: int) -> str:
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, remaining = divmod(minutes, 60)
    return f"{hours} h {remaining:02d} min"


def _clean_alert_message(value: str, limit: int = 700) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    cleaned = " ".join(html.unescape(without_tags).split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 1].rstrip()}…"


def _score_weights_summary(weights: ScoreWeights) -> str:
    return (
        f"door x{weights.door_to_door:g}, bike x{weights.bike_penalty:g}, "
        f"transfers x{weights.transfers:g}, alerts x{weights.disruptions:g}, "
        f"freshness x{weights.freshness:g}, comfort x{weights.cycling_comfort:g}"
    )


@plan_app.command("outbound")
def plan_outbound(
    depart_at: Annotated[
        str,
        typer.Option(help="ISO 8601 departure, with an offset when possible."),
    ],
    config: ConfigPath = Path("config.yaml"),
    max_bike_minutes: Annotated[
        float | None,
        typer.Option(help="Override bicycle hard limit for this run; config.yaml is unchanged."),
    ] = None,
    max_walking_minutes: Annotated[
        float | None,
        typer.Option(help="Override total walking hard limit for this run."),
    ] = None,
    max_walking_leg_minutes: Annotated[
        float | None,
        typer.Option(help="Override maximum duration of any single walking leg."),
    ] = None,
    max_results: Annotated[
        int,
        typer.Option(
            min=1,
            max=20,
            help="Maximum number of distinct ranked itineraries to display.",
        ),
    ] = 10,
    bike_station: Annotated[
        str | None,
        typer.Option(
            help=(
                "Bike endpoint: RER B station name/stop-area ID, or 'best' to "
                "evaluate nearby RER B stations. Omit to use config candidates."
            )
        ),
    ] = None,
    bike_station_range: Annotated[
        list[str] | None,
        typer.Option(
            help=(
                "RER B range for 'best' as START..END. Repeat to combine "
                "branches; repeated stations are queried once."
            )
        ),
    ] = None,
    confirm_rank: Annotated[
        int | None,
        typer.Option(
            min=1,
            max=20,
            help=(
                "Confirm a displayed rank after planning. A bike route records "
                "its station; an all-transit route leaves bicycle state unchanged."
            ),
        ),
    ] = None,
    score_mode: Annotated[
        ScoreMode | None,
        typer.Option(help="Scoring profile; overrides scoring.mode from config.yaml."),
    ] = None,
) -> None:
    """Plan bike-to-station plus transit options and an all-transit baseline."""
    try:
        plan = asyncio.run(
            _run_outbound_plan(
                config,
                depart_at,
                max_bike_minutes,
                max_walking_minutes,
                max_walking_leg_minutes,
                max_results,
                bike_station,
                bike_station_range,
                score_mode,
            )
        )
    except (
        FileNotFoundError,
        ValueError,
        ValidationError,
        MissingAccessError,
        PrimError,
    ) as exc:
        console.print(f"[bold red]Planning stopped:[/bold red] {exc}")
        raise typer.Exit(code=2) from None
    _render_outbound_plan(plan)
    if confirm_rank is not None:
        try:
            state = _confirm_outbound_selection(
                plan,
                confirm_rank,
                _bike_state_store(config),
            )
        except (FileNotFoundError, ValueError, ValidationError, sqlite3.Error) as exc:
            console.print(f"[bold red]Route confirmation stopped:[/bold red] {exc}")
            raise typer.Exit(code=2) from None
        if state is None:
            console.print(
                f"Confirmed #{confirm_rank}: all transit; bicycle state unchanged."
            )
        else:
            station = state.station_name or state.station_id
            console.print(
                f"Confirmed #{confirm_rank}: bicycle recorded at [bold]{station}[/bold]."
            )


@plan_app.command("return")
def plan_return(
    depart_at: Annotated[
        str,
        typer.Option(help="ISO 8601 departure from work, with an offset when possible."),
    ],
    config: ConfigPath = Path("config.yaml"),
    max_bike_minutes: Annotated[
        float | None,
        typer.Option(help="Override bicycle hard limit for this run."),
    ] = None,
    max_walking_minutes: Annotated[
        float | None,
        typer.Option(help="Override total walking hard limit for this run."),
    ] = None,
    max_walking_leg_minutes: Annotated[
        float | None,
        typer.Option(help="Override maximum duration of any single walking leg."),
    ] = None,
    max_results: Annotated[
        int,
        typer.Option(
            min=1,
            max=20,
            help="Maximum number of ranked return itineraries to display.",
        ),
    ] = 10,
    confirm_rank: Annotated[
        int | None,
        typer.Option(
            min=1,
            max=20,
            help="Confirm a displayed return rank and record the bicycle at home.",
        ),
    ] = None,
    score_mode: Annotated[
        ScoreMode | None,
        typer.Option(help="Scoring profile; overrides scoring.mode from config.yaml."),
    ] = None,
) -> None:
    """Plan transit to the stored bicycle station, then cycle home."""
    try:
        plan = asyncio.run(
            _run_return_plan(
                config,
                depart_at,
                max_bike_minutes,
                max_walking_minutes,
                max_walking_leg_minutes,
                max_results,
                score_mode,
            )
        )
    except (
        FileNotFoundError,
        ValueError,
        ValidationError,
        MissingAccessError,
        PrimError,
        sqlite3.Error,
    ) as exc:
        console.print(f"[bold red]Return planning stopped:[/bold red] {exc}")
        raise typer.Exit(code=2) from None
    _render_return_plan(plan)
    if confirm_rank is not None:
        try:
            _confirm_return_selection(
                plan,
                confirm_rank,
                _bike_state_store(config),
            )
        except (FileNotFoundError, ValueError, ValidationError, sqlite3.Error) as exc:
            console.print(f"[bold red]Return confirmation stopped:[/bold red] {exc}")
            raise typer.Exit(code=2) from None
        console.print(
            f"Confirmed #{confirm_rank}: bicycle recorded at [bold]home[/bold]."
        )


if __name__ == "__main__":
    app()
