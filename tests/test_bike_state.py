from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from idf_commute.domain.state import BikeLocation, BikeState
from idf_commute.persistence import BikeStateStore


def test_missing_database_means_unknown_without_creating_a_file(tmp_path: Path) -> None:
    path = tmp_path / "state" / "commute.sqlite3"

    state = BikeStateStore(path).load()

    assert state == BikeState.unknown()
    assert not path.exists()


def test_station_state_round_trips_and_is_replaced_by_home(tmp_path: Path) -> None:
    path = tmp_path / "state" / "commute.sqlite3"
    store = BikeStateStore(path)
    updated_at = datetime(2026, 7, 30, 9, 5, tzinfo=ZoneInfo("Europe/Paris"))

    saved = store.set_station(
        "stop_area:IDFM:70033",
        station_name="Bourg-la-Reine",
        updated_at=updated_at,
        source_journey_id="journey:morning",
    )

    assert store.load() == saved
    assert path.exists()

    home = store.set_home(
        updated_at=updated_at,
        source_journey_id="journey:return",
    )
    assert store.load() == home
    assert home.station_id is None


def test_station_state_requires_a_station_id() -> None:
    with pytest.raises(ValueError, match="requires station_id"):
        BikeState(location=BikeLocation.STATION)
