from __future__ import annotations

from pathlib import Path

import pytest

from idf_commute.config import AppConfig, MissingAccessError, Settings


def test_example_config_is_valid() -> None:
    config = AppConfig.from_yaml(Path("config.example.yaml"))
    assert config.scoring.mode.value == "balanced"


def test_yaml_config_loads(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
locations:
  home: {latitude: 48.8, longitude: 2.3}
  work: {latitude: 48.7, longitude: 2.4}
candidate_stations:
  - {query: Example, id: null, label: Example}
scoring:
  mode: fewest-transfers
  weights:
    transfers: 7
""",
        encoding="utf-8",
    )
    config = AppConfig.from_yaml(path)
    assert config.timezone == "Europe/Paris"
    assert config.locations.home.navitia_coord == "2.300000;48.800000"
    assert config.line_queries == ["RER B", "4602", "21", "22"]
    assert config.bicycle.preferred_bike_minutes == 20
    assert config.bicycle.max_bike_minutes == 25
    assert config.walking.max_minutes == 30
    assert config.walking.max_leg_minutes == 20
    assert config.scoring.mode.value == "fewest-transfers"
    assert config.scoring.weights.transfers == 7


def test_missing_api_key_stops_live_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRIM_API_KEY", raising=False)
    settings = Settings(_env_file=None)
    with pytest.raises(MissingAccessError, match="PRIM_API_KEY is missing"):
        settings.require_api_key()


def test_numeric_yaml_station_id_is_normalized() -> None:
    config = AppConfig.model_validate(
        {
            "locations": {
                "home": {"latitude": 48.8, "longitude": 2.3},
                "work": {"latitude": 48.7, "longitude": 2.4},
            },
            "candidate_stations": [{"query": "Example", "id": 123, "label": "Example"}],
        }
    )
    assert config.candidate_stations[0].id == "123"
