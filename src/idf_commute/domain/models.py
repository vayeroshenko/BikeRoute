from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

PARIS = ZoneInfo("Europe/Paris")
AwareDatetime = Annotated[datetime, Field()]


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Freshness(StrEnum):
    REALTIME = "realtime"
    BASE_SCHEDULE = "base_schedule"
    UNKNOWN = "unknown"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class OutboundOptionKind(StrEnum):
    BIKE_TRANSIT = "bike_transit"
    ALL_TRANSIT = "all_transit"


class Location(DomainModel):
    latitude: float = Field(ge=48.0, le=49.5)
    longitude: float = Field(ge=1.0, le=3.7)
    label: str | None = None


class Station(DomainModel):
    id: str
    name: str
    location: Location | None = None
    line_ids: tuple[str, ...] = ()


class TimeInterval(DomainModel):
    begin: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def validate_interval(self) -> TimeInterval:
        _require_aware(self.begin, "begin")
        _require_aware(self.end, "end")
        if self.end <= self.begin:
            raise ValueError("end must be after begin")
        return self

    def intersects(self, other: TimeInterval) -> bool:
        return self.begin < other.end and other.begin < self.end


class TransitRequest(DomainModel):
    origin_id: str
    destination_id: str
    datetime: AwareDatetime | None = None
    arrive_by: bool = False
    data_freshness: Freshness = Freshness.REALTIME
    forbidden_ids: tuple[str, ...] = ()
    min_journeys: int | None = Field(default=None, ge=1, le=20)

    @model_validator(mode="after")
    def validate_datetime(self) -> TransitRequest:
        if self.datetime is not None:
            _require_aware(self.datetime, "datetime")
        return self


class TransitLeg(DomainModel):
    type: str
    mode: str | None = None
    duration_seconds: int = Field(ge=0)
    departure: AwareDatetime | None = None
    arrival: AwareDatetime | None = None
    base_departure: AwareDatetime | None = None
    base_arrival: AwareDatetime | None = None
    freshness: Freshness = Freshness.UNKNOWN
    line_id: str | None = None
    line_code: str | None = None
    equivalent_line_codes: tuple[str, ...] = ()
    commercial_mode: str | None = None
    direction: str | None = None
    origin_id: str | None = None
    destination_id: str | None = None
    origin_name: str | None = None
    destination_name: str | None = None
    disruption_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_times(self) -> TransitLeg:
        for field_name in ("departure", "arrival", "base_departure", "base_arrival"):
            value = getattr(self, field_name)
            if value is not None:
                _require_aware(value, field_name)
        return self


class TransitJourney(DomainModel):
    id: str | None = None
    type: str | None = None
    status: str | None = None
    duration_seconds: int = Field(ge=0)
    departure: AwareDatetime
    arrival: AwareDatetime
    requested_datetime: AwareDatetime | None = None
    transfers: int = Field(default=0, ge=0)
    legs: tuple[TransitLeg, ...]
    response_timestamp: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_times(self) -> TransitJourney:
        for field_name in ("departure", "arrival", "requested_datetime", "response_timestamp"):
            value = getattr(self, field_name)
            if value is not None:
                _require_aware(value, field_name)
        if self.arrival < self.departure:
            raise ValueError("arrival must not precede departure")
        return self


class BikeRequest(DomainModel):
    origin: Location
    destination: Location
    profile: str = "MEDIAN"
    bike_type: str = "TRADITIONAL"
    average_speed_kmh: int = Field(default=16, ge=5, le=45)
    alternatives: bool = True
    geometry: bool = True
    elevations: bool = True
    instructions: bool = True


class ElevationPoint(DomainModel):
    distance_from_start_m: float = Field(ge=0)
    elevation_m: float
    geometry_index: int = Field(ge=0)


class BikeFacilitySegment(DomainModel):
    direction: str | None = None
    road_name: str | None = None
    distance_m: float = Field(ge=0)
    facility: str | None = None
    cyclability: float | None = Field(default=None, ge=0, le=5)
    geometry_index: int | None = Field(default=None, ge=0)
    orientation: str | None = None
    city_names: str | tuple[str, ...] | None = None
    duration_seconds: float | None = Field(default=None, ge=0)


class BikeRoute(DomainModel):
    provider_id: str | None = None
    title: str
    duration_seconds: int = Field(ge=0)
    distance_m: float = Field(ge=0)
    normal_roads_m: float = Field(default=0, ge=0)
    recommended_roads_m: float = Field(default=0, ge=0)
    discouraged_roads_m: float = Field(default=0, ge=0)
    vertical_gain_m: float = Field(default=0, ge=0)
    vertical_loss_m: float = Field(default=0, ge=0)
    profile: str | None = None
    bike_type: str | None = None
    average_speed_kmh: float | None = Field(default=None, ge=0)
    encoded_geometry: str | None = None
    elevations: tuple[ElevationPoint, ...] = ()
    facility_segments: tuple[BikeFacilitySegment, ...] = ()
    facility_distances_m: dict[str, float] = Field(default_factory=dict)


class Disruption(DomainModel):
    id: str
    title: str
    message: str
    cause: str | None = None
    severity: str | None = None
    effect: str | None = None
    planned: bool | None = None
    application_periods: tuple[TimeInterval, ...] = ()
    affected_line_ids: frozenset[str] = frozenset()
    affected_stop_area_ids: frozenset[str] = frozenset()
    affected_stop_point_ids: frozenset[str] = frozenset()
    affected_segment: tuple[str, str] | None = None
    updated_at: AwareDatetime | None = None
    source: str

    @model_validator(mode="after")
    def validate_updated_at(self) -> Disruption:
        if self.updated_at is not None:
            _require_aware(self.updated_at, "updated_at")
        return self


class Departure(DomainModel):
    line_id: str | None = None
    stop_id: str
    destination_name: str | None = None
    scheduled_time: AwareDatetime | None = None
    expected_time: AwareDatetime | None = None
    response_timestamp: AwareDatetime | None = None
    freshness: Freshness = Freshness.UNKNOWN

    @model_validator(mode="after")
    def validate_times(self) -> Departure:
        for field_name in ("scheduled_time", "expected_time", "response_timestamp"):
            value = getattr(self, field_name)
            if value is not None:
                _require_aware(value, field_name)
        return self


class ScoreBreakdown(DomainModel):
    door_to_door_minutes: float = Field(ge=0)
    bike_penalty_minutes: float = Field(ge=0)
    transfer_penalty_minutes: float = Field(ge=0)
    disruption_penalty_minutes: float = Field(ge=0)
    freshness_penalty_minutes: float = Field(ge=0)
    connection_margin_penalty_minutes: float = Field(default=0, ge=0)
    cycling_comfort_penalty_minutes: float = Field(ge=0)
    total_minutes: float = Field(ge=0)


class ReliabilityAssessment(DomainModel):
    confidence: Confidence
    data_age_seconds: float | None = Field(default=None, ge=0)
    robust_arrival: AwareDatetime
    safety_buffer_minutes: float = Field(ge=0)
    realtime_leg_count: int = Field(ge=0)
    scheduled_leg_count: int = Field(ge=0)
    minimum_connection_margin_minutes: float | None = None
    tight_connection_count: int = Field(default=0, ge=0)
    reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_robust_arrival(self) -> ReliabilityAssessment:
        _require_aware(self.robust_arrival, "robust_arrival")
        return self


class OutboundOption(DomainModel):
    kind: OutboundOptionKind
    station: Station | None = None
    bike_route: BikeRoute | None = None
    transit_journey: TransitJourney
    departure: AwareDatetime
    arrival: AwareDatetime
    parking_buffer_seconds: int = Field(default=0, ge=0)
    matched_disruptions: tuple[Disruption, ...] = ()
    reliability: ReliabilityAssessment | None = None
    score: ScoreBreakdown

    @model_validator(mode="after")
    def validate_option(self) -> OutboundOption:
        _require_aware(self.departure, "departure")
        _require_aware(self.arrival, "arrival")
        if self.arrival < self.departure:
            raise ValueError("arrival must not precede departure")
        if self.kind is OutboundOptionKind.BIKE_TRANSIT:
            if self.station is None or self.bike_route is None:
                raise ValueError("bike-transit options require a station and bike route")
        elif self.station is not None or self.bike_route is not None:
            raise ValueError("all-transit options cannot contain a bike route or station")
        return self


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
