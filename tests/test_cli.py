from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from idf_commute.cli import app

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
