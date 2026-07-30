from __future__ import annotations

from pathlib import Path

import pytest

from idf_commute.config import AppConfig


@pytest.fixture
def app_config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "locations": {
                "home": {"latitude": 48.812345, "longitude": 2.312345},
                "work": {"latitude": 48.712345, "longitude": 2.412345},
            },
            "candidate_stations": [
                {
                    "query": "Example RER B",
                    "id": "stop_area:IDFM:example",
                    "label": "Example",
                }
            ],
            "line_queries": ["RER B", "4602", "21", "22"],
        }
    )


@pytest.fixture
def fixture_dir(tmp_path: Path) -> Path:
    return tmp_path / "fixtures"
