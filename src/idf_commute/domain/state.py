from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import model_validator

from idf_commute.domain.models import AwareDatetime, DomainModel, _require_aware


class BikeLocation(StrEnum):
    HOME = "home"
    STATION = "station"
    UNKNOWN = "unknown"


class BikeState(DomainModel):
    location: BikeLocation
    station_id: str | None = None
    station_name: str | None = None
    updated_at: AwareDatetime | None = None
    source_journey_id: str | None = None

    @model_validator(mode="after")
    def validate_location(self) -> BikeState:
        if self.updated_at is not None:
            _require_aware(self.updated_at, "updated_at")
        if self.location is BikeLocation.STATION:
            if not self.station_id:
                raise ValueError("station bicycle state requires station_id")
        elif self.station_id is not None or self.station_name is not None:
            raise ValueError("home/unknown bicycle state cannot contain station details")
        return self

    @classmethod
    def unknown(cls) -> BikeState:
        return cls(location=BikeLocation.UNKNOWN)


def state_timestamp(value: datetime) -> str:
    _require_aware(value, "updated_at")
    return value.isoformat()
