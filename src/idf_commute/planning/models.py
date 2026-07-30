from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from idf_commute.domain.models import (
    AwareDatetime,
    DomainModel,
    Location,
    OutboundOption,
    Station,
    _require_aware,
)


class OutboundPlanningRequest(DomainModel):
    home: Location
    home_transit_id: str
    work_transit_id: str
    candidate_stations: tuple[Station, ...]
    depart_at: AwareDatetime
    preferred_bike_minutes: float = Field(default=20, ge=0)
    max_bike_minutes: float = Field(default=25, gt=0)
    parking_buffer_minutes: float = Field(default=4, ge=0)
    max_results: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def validate_request(self) -> OutboundPlanningRequest:
        _require_aware(self.depart_at, "depart_at")
        if self.max_bike_minutes < self.preferred_bike_minutes:
            raise ValueError("max_bike_minutes must be at least preferred_bike_minutes")
        if not self.candidate_stations:
            raise ValueError("at least one candidate station is required")
        if any(station.location is None for station in self.candidate_stations):
            raise ValueError("every candidate station requires coordinates")
        return self


class CandidateRejection(DomainModel):
    station_id: str
    bike_route_title: str | None = None
    bike_duration_minutes: float | None = Field(default=None, ge=0)
    reason: str


class OutboundPlan(DomainModel):
    requested_departure: AwareDatetime
    options: tuple[OutboundOption, ...]
    rejections: tuple[CandidateRejection, ...] = ()

    @model_validator(mode="after")
    def validate_departure(self) -> OutboundPlan:
        _require_aware(self.requested_departure, "requested_departure")
        return self


def planning_horizon_end(departure: datetime, hours: int = 6) -> datetime:
    from datetime import timedelta

    return departure + timedelta(hours=hours)
