from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest
from typer.testing import CliRunner

import idf_commute.cli as cli_module
from idf_commute.cli import app
from idf_commute.domain.state import BikeLocation
from idf_commute.persistence import BikeStateStore

runner = CliRunner()


def _write_config(path: Path) -> None:
    path.write_text(
        """
locations:
  home: {latitude: 48.8, longitude: 2.3}
  work: {latitude: 48.7, longitude: 2.4}
state:
  sqlite_path: state/commute.sqlite3
""",
        encoding="utf-8",
    )


def test_streamlit_gui_starts_without_network_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.yaml"
    _write_config(config)
    monkeypatch.setenv("IDF_COMMUTE_CONFIG", str(config))

    interface = AppTest.from_file("src/idf_commute/gui.py").run(timeout=10)

    assert not interface.exception
    assert interface.title[0].value == "IDF Commute"
    assert interface.radio[0].value == "Outbound"
    assert any(button.label == "Plan current journey" for button in interface.button)
    assert any(warning.value == "Unknown" for warning in interface.warning)
    assert not (tmp_path / "state" / "commute.sqlite3").exists()

    home_button = next(button for button in interface.button if button.label == "Mark home")
    interface = home_button.click().run(timeout=10)
    state = BikeStateStore(tmp_path / "state" / "commute.sqlite3").load()
    assert state.location is BikeLocation.HOME
    assert any(success.value == "At home" for success in interface.success)

    interface = interface.radio[0].set_value("Return").run(timeout=10)
    assert any("return destination is locked" in caption.value for caption in interface.caption)


def test_cli_gui_command_launches_streamlit_with_selected_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.yaml"
    _write_config(config)
    captured: dict[str, Any] = {}

    def fake_run(
        command: list[str],
        *,
        env: dict[str, str],
        check: bool,
    ) -> CompletedProcess[str]:
        captured.update(command=command, env=env, check=check)
        return CompletedProcess(command, 0)

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    result = runner.invoke(app, ["gui", "--config", str(config)])

    assert result.exit_code == 0
    command = captured["command"]
    assert isinstance(command, list)
    assert command[1:4] == ["-m", "streamlit", "run"]
    assert "--server.address=127.0.0.1" in command
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["IDF_COMMUTE_CONFIG"] == str(config.resolve())
    assert captured["check"] is False
