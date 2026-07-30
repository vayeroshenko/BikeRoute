from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Literal

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from idf_commute.config import MissingAccessError, Settings, load_config, safe_settings_summary
from idf_commute.probe import (
    ProbeError,
    ProbeResult,
    ProbeRunner,
    summarize_result,
    validate_live_config,
)

app = typer.Typer(
    name="idf-commute",
    help="Île-de-France commute planner API discovery tools (Milestones 0-1).",
    no_args_is_help=True,
)
probe_app = typer.Typer(help="Safely probe authenticated PRIM APIs.", no_args_is_help=True)
app.add_typer(probe_app, name="probe")
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


if __name__ == "__main__":
    app()
