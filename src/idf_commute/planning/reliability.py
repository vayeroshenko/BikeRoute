from __future__ import annotations

from datetime import datetime, timedelta

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
        reasons=tuple(reasons),
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
