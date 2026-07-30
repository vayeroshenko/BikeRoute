from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from idf_commute.domain.models import PARIS
from idf_commute.domain.state import BikeLocation, BikeState, state_timestamp

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bicycle_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    location TEXT NOT NULL CHECK (location IN ('home', 'station', 'unknown')),
    station_id TEXT,
    station_name TEXT,
    updated_at TEXT NOT NULL,
    source_journey_id TEXT
)
"""


class BikeStateStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> BikeState:
        if not self._path.exists():
            return BikeState.unknown()
        with closing(self._connect(create_parent=False)) as connection:
            connection.execute(_SCHEMA)
            row = connection.execute(
                """
                SELECT location, station_id, station_name, updated_at, source_journey_id
                FROM bicycle_state
                WHERE singleton = 1
                """
            ).fetchone()
        if row is None:
            return BikeState.unknown()
        return BikeState(
            location=BikeLocation(row[0]),
            station_id=row[1],
            station_name=row[2],
            updated_at=datetime.fromisoformat(row[3]),
            source_journey_id=row[4],
        )

    def set_home(
        self,
        *,
        updated_at: datetime | None = None,
        source_journey_id: str | None = None,
    ) -> BikeState:
        return self.save(
            BikeState(
                location=BikeLocation.HOME,
                updated_at=updated_at or datetime.now(PARIS),
                source_journey_id=source_journey_id,
            )
        )

    def set_station(
        self,
        station_id: str,
        *,
        station_name: str | None = None,
        updated_at: datetime | None = None,
        source_journey_id: str | None = None,
    ) -> BikeState:
        return self.save(
            BikeState(
                location=BikeLocation.STATION,
                station_id=station_id,
                station_name=station_name,
                updated_at=updated_at or datetime.now(PARIS),
                source_journey_id=source_journey_id,
            )
        )

    def set_unknown(self, *, updated_at: datetime | None = None) -> BikeState:
        return self.save(
            BikeState(
                location=BikeLocation.UNKNOWN,
                updated_at=updated_at or datetime.now(PARIS),
            )
        )

    def save(self, state: BikeState) -> BikeState:
        if state.updated_at is None:
            raise ValueError("persisted bicycle state requires updated_at")
        with closing(self._connect(create_parent=True)) as connection:
            connection.execute(_SCHEMA)
            connection.execute(
                """
                INSERT INTO bicycle_state (
                    singleton,
                    location,
                    station_id,
                    station_name,
                    updated_at,
                    source_journey_id
                )
                VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    location = excluded.location,
                    station_id = excluded.station_id,
                    station_name = excluded.station_name,
                    updated_at = excluded.updated_at,
                    source_journey_id = excluded.source_journey_id
                """,
                (
                    state.location.value,
                    state.station_id,
                    state.station_name,
                    state_timestamp(state.updated_at),
                    state.source_journey_id,
                ),
            )
            connection.commit()
        return state

    def _connect(self, *, create_parent: bool) -> sqlite3.Connection:
        if create_parent:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path)
        connection.execute("PRAGMA journal_mode = WAL")
        return connection
