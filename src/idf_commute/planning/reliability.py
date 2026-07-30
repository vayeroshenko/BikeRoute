from __future__ import annotations

from datetime import datetime, timedelta
from itertools import pairwise

from pydantic import BaseModel, ConfigDict, Field, model_validator

from idf_commute.domain.models import (
    PARIS,
    Confidence,
    Disruption,
    Freshness,
    ReliabilityAssessment,
    TransitJourney,
)


class ReliabilityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fresh_age_seconds: float = Field(default=120, gt=0)
    stale_age_seconds: float = Field(default=300, gt=0)
    minimum_connection_minutes: float = Field(default=5, ge=0)
    medium_buffer_minutes: float = Field(default=5, ge=0)
    low_buffer_minutes: float = Field(default=10, ge=0)

    @model_validator(mode="after")
    def validate_age_thresholds(self) -> ReliabilityPolicy:
        if self.stale_age_seconds <= self.fresh_age_seconds:
            raise ValueError("stale_age_seconds must exceed fresh_age_seconds")
        return self


def assess_reliability(
    journey: TransitJourney,
    *,
    arrival: datetime,
    disruptions: tuple[Disruption, ...] = (),
    policy: ReliabilityPolicy | None = None,
    now: datetime | None = None,
) -> ReliabilityAssessment:
    active_policy = policy or ReliabilityPolicy()
    evaluated_at = now or datetime.now(PARIS)
    public_transport_legs = [leg for leg in journey.legs if leg.type == "public_transport"]
    realtime_count = sum(leg.freshness is Freshness.REALTIME for leg in public_transport_legs)
    scheduled_count = len(public_transport_legs) - realtime_count
    connection_margins = connection_margins_minutes(journey)
    minimum_margin = min(connection_margins) if connection_margins else None
    tight_connection_count = sum(
        margin < active_policy.minimum_connection_minutes for margin in connection_margins
    )
    data_age = (
        max(
            0.0,
            (evaluated_at - journey.response_timestamp).total_seconds(),
        )
        if journey.response_timestamp is not None
        else None
    )

    reasons: list[str] = []
    low_confidence = False
    medium_confidence = False
    if journey.response_timestamp is None:
        reasons.append("provider response timestamp is unavailable")
        low_confidence = True
    elif data_age is not None and data_age > active_policy.stale_age_seconds:
        reasons.append(f"provider data is {data_age / 60:.1f} minutes old")
        low_confidence = True
    elif data_age is not None and data_age > active_policy.fresh_age_seconds:
        reasons.append(f"provider data is {data_age / 60:.1f} minutes old")
        medium_confidence = True

    if scheduled_count:
        noun = "leg is" if scheduled_count == 1 else "legs are"
        reasons.append(f"{scheduled_count} transit {noun} schedule-only")
        medium_confidence = True
    if minimum_margin is not None and minimum_margin < 0:
        reasons.append(f"a connection is infeasible by {abs(minimum_margin):.1f} minutes")
        low_confidence = True
    elif tight_connection_count:
        reasons.append(
            f"{tight_connection_count} connection(s) have less than "
            f"{active_policy.minimum_connection_minutes:g} minutes usable margin"
        )
        medium_confidence = True
    if _has_material_disruption(disruptions):
        reasons.append("a material disruption matches this itinerary")
        low_confidence = True

    if low_confidence:
        confidence = Confidence.LOW
        buffer_minutes = active_policy.low_buffer_minutes
    elif medium_confidence:
        confidence = Confidence.MEDIUM
        buffer_minutes = active_policy.medium_buffer_minutes
    else:
        confidence = Confidence.HIGH
        buffer_minutes = 0.0
        reasons.append("fresh realtime transit data with no matched material alert")

    return ReliabilityAssessment(
        confidence=confidence,
        data_age_seconds=data_age,
        robust_arrival=arrival + timedelta(minutes=buffer_minutes),
        safety_buffer_minutes=buffer_minutes,
        realtime_leg_count=realtime_count,
        scheduled_leg_count=scheduled_count,
        minimum_connection_margin_minutes=minimum_margin,
        tight_connection_count=tight_connection_count,
        reasons=tuple(reasons),
    )


def connection_margins_minutes(journey: TransitJourney) -> tuple[float, ...]:
    public_indices = [
        index for index, leg in enumerate(journey.legs) if leg.type == "public_transport"
    ]
    margins: list[float] = []
    for previous_index, next_index in pairwise(public_indices):
        previous = journey.legs[previous_index]
        following = journey.legs[next_index]
        if previous.arrival is None or following.departure is None:
            continue
        connection_window = (following.departure - previous.arrival).total_seconds()
        required_transfer = sum(
            leg.duration_seconds
            for leg in journey.legs[previous_index + 1 : next_index]
            if leg.type != "waiting"
        )
        margins.append((connection_window - required_transfer) / 60)
    return tuple(margins)


def connection_shortfall_penalty_minutes(
    journey: TransitJourney,
    minimum_connection_minutes: float,
) -> float:
    return sum(
        max(0.0, minimum_connection_minutes - margin)
        for margin in connection_margins_minutes(journey)
    )


def _has_material_disruption(disruptions: tuple[Disruption, ...]) -> bool:
    informational = {"information", "info", "normal"}
    return any(
        not (
            disruption.severity
            and disruption.severity.casefold() in informational
            and not disruption.effect
        )
        for disruption in disruptions
    )
