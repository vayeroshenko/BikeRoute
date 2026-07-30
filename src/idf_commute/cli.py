from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from idf_commute.config import (
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
from idf_commute.planning.models import OutboundPlan, OutboundPlanningRequest
from idf_commute.planning.planner import OutboundPlanner
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
from idf_commute.providers.protocols import PlaceProvider

app = typer.Typer(
    name="idf-commute",
    help="Île-de-France bicycle and transit commute planner.",
    no_args_is_help=True,
)
probe_app = typer.Typer(help="Safely probe authenticated PRIM APIs.", no_args_is_help=True)
plan_app = typer.Typer(help="Plan explicit bicycle and transit journeys.", no_args_is_help=True)
app.add_typer(probe_app, name="probe")
app.add_typer(plan_app, name="plan")
console = Console()
ConfigPath = Annotated[Path, typer.Option(exists=True, dir_okay=False)]
FixturePath = Annotated[Path, typer.Option()]
DryRun = Annotated[bool, typer.Option(help="Validate and show redacted settings only.")]


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
) -> OutboundPlan:
    config = load_config(config_path)
    settings = Settings()
    departure = _parse_departure(depart_at_text, config.timezone)
    preferred_minutes, hard_minutes = _effective_bike_thresholds(
        config.bicycle.preferred_bike_minutes,
        config.bicycle.max_bike_minutes,
        max_bike_minutes,
    )
    async with PrimClient(
        settings.require_api_key(),
        api_key_header=settings.api_key_header,
        timeout_seconds=settings.timeout_seconds,
    ) as client:
        navitia = NavitiaAdapter(client, str(settings.navitia_base_url))
        stations = await _resolve_candidate_stations(config.candidate_stations, navitia)
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
                parking_buffer_minutes=config.bicycle.parking_buffer_minutes,
                bike_profile=config.bicycle.profile,
                bike_type=config.bicycle.bike_type,
                bike_average_speed_kmh=config.bicycle.average_speed_kmh,
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


def _render_outbound_plan(plan: OutboundPlan) -> None:
    console.print(
        f"Bike thresholds: preferred {plan.preferred_bike_minutes:g} min, "
        f"hard maximum {plan.max_bike_minutes:g} min"
    )
    table = Table("Rank", "Type", "Station", "Bike", "Arrival", "Score", "Alerts")
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
            option.arrival.strftime("%H:%M"),
            f"{option.score.total_minutes:.1f}",
            str(len(option.matched_disruptions)),
        )
    console.print(table)
    if plan.options:
        console.print("\n[bold]Detailed itineraries[/bold]")
    for rank, option in enumerate(plan.options, start=1):
        _render_option_details(rank, option)
    if plan.rejections:
        console.print(f"[yellow]{len(plan.rejections)} candidate route(s) rejected.[/yellow]")
        for rejection in plan.rejections:
            duration = (
                f", {rejection.bike_duration_minutes:.1f} min"
                if rejection.bike_duration_minutes is not None
                else ""
            )
            console.print(
                f"- {rejection.station_name or rejection.station_id} / "
                f"{rejection.bike_route_title or 'no bike route'}{duration}: "
                f"{rejection.reason}"
            )


def _render_option_details(rank: int, option: OutboundOption) -> None:
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
        f"Score {score.total_minutes:.1f}: door {score.door_to_door_minutes:.1f} + "
        f"bike {score.bike_penalty_minutes:.1f} + "
        f"transfers {score.transfer_penalty_minutes:.1f} + "
        f"alerts {score.disruption_penalty_minutes:.1f} + "
        f"freshness {score.freshness_penalty_minutes:.1f} + "
        f"comfort {score.cycling_comfort_penalty_minutes:.1f}"
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
        line = " ".join(part for part in (leg.commercial_mode, leg.line_code) if part)
        direction = f" → {leg.direction}" if leg.direction else ""
        return f"{line or 'Transit'}{direction}"
    labels = {
        "crow_fly": "Station access",
        "street_network": "Walk" if leg.mode == "walking" else (leg.mode or "Street"),
        "transfer": "Transfer walk" if leg.mode == "walking" else "Transfer",
        "waiting": "Wait",
        "park": "Park bicycle",
    }
    return labels.get(leg.type, leg.mode or leg.type.replace("_", " ").title())


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
) -> None:
    """Plan bike-to-station plus transit options and an all-transit baseline."""
    try:
        plan = asyncio.run(_run_outbound_plan(config, depart_at, max_bike_minutes))
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


if __name__ == "__main__":
    app()
