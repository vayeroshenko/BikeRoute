from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from idf_commute.domain.models import (
    Confidence,
    Disruption,
    Freshness,
    TransitJourney,
    TransitLeg,
)
from idf_commute.planning.reliability import (
    ReliabilityPolicy,
    assess_reliability,
    connection_margins_minutes,
    connection_shortfall_penalty_minutes,
)

PARIS = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 7, 30, 8, 0, tzinfo=PARIS)
ARRIVAL = NOW + timedelta(hours=1)


def _journey(
    *,
    freshness: Freshness = Freshness.REALTIME,
    response_timestamp: datetime | None = NOW,
) -> TransitJourney:
    return TransitJourney(
        duration_seconds=60 * 60,
        departure=NOW,
        arrival=ARRIVAL,
        response_timestamp=response_timestamp,
        legs=(
            TransitLeg(
                type="public_transport",
                duration_seconds=60 * 60,
                freshness=freshness,
            ),
        ),
    )


def test_fresh_realtime_journey_has_high_confidence() -> None:
    assessment = assess_reliability(
        _journey(response_timestamp=NOW - timedelta(seconds=30)),
        arrival=ARRIVAL,
        now=NOW,
    )

    assert assessment.confidence is Confidence.HIGH
    assert assessment.data_age_seconds == 30
    assert assessment.robust_arrival == ARRIVAL
    assert assessment.safety_buffer_minutes == 0


def test_schedule_only_or_aging_data_has_medium_confidence_and_buffer() -> None:
    assessment = assess_reliability(
        _journey(
            freshness=Freshness.BASE_SCHEDULE,
            response_timestamp=NOW - timedelta(minutes=3),
        ),
        arrival=ARRIVAL,
        now=NOW,
    )

    assert assessment.confidence is Confidence.MEDIUM
    assert assessment.robust_arrival == ARRIVAL + timedelta(minutes=5)
    assert assessment.scheduled_leg_count == 1
    assert "schedule-only" in " ".join(assessment.reasons)


def test_stale_data_or_material_alert_has_low_confidence() -> None:
    disruption = Disruption(
        id="works",
        title="Service interrupted",
        message="No service",
        severity="BLOCKING",
        source="test",
    )
    assessment = assess_reliability(
        _journey(response_timestamp=NOW - timedelta(minutes=6)),
        arrival=ARRIVAL,
        disruptions=(disruption,),
        now=NOW,
    )

    assert assessment.confidence is Confidence.LOW
    assert assessment.robust_arrival == ARRIVAL + timedelta(minutes=10)
    assert len(assessment.reasons) == 2


def test_connection_margin_subtracts_required_transfer_but_not_waiting() -> None:
    first_arrival = NOW + timedelta(minutes=20)
    second_departure = NOW + timedelta(minutes=27)
    journey = TransitJourney(
        duration_seconds=45 * 60,
        departure=NOW,
        arrival=NOW + timedelta(minutes=45),
        response_timestamp=NOW,
        transfers=1,
        legs=(
            TransitLeg(
                type="public_transport",
                duration_seconds=20 * 60,
                departure=NOW,
                arrival=first_arrival,
                freshness=Freshness.REALTIME,
            ),
            TransitLeg(
                type="transfer",
                mode="walking",
                duration_seconds=4 * 60,
            ),
            TransitLeg(type="waiting", duration_seconds=2 * 60),
            TransitLeg(
                type="public_transport",
                duration_seconds=18 * 60,
                departure=second_departure,
                arrival=NOW + timedelta(minutes=45),
                freshness=Freshness.REALTIME,
            ),
        ),
    )

    assert connection_margins_minutes(journey) == (3.0,)
    assert connection_shortfall_penalty_minutes(journey, 5) == 2
    assessment = assess_reliability(journey, arrival=journey.arrival, now=NOW)
    assert assessment.confidence is Confidence.MEDIUM
    assert assessment.minimum_connection_margin_minutes == 3
    assert assessment.tight_connection_count == 1
    assert "less than 5 minutes" in " ".join(assessment.reasons)


def test_reliability_age_thresholds_must_be_ordered() -> None:
    with pytest.raises(ValueError, match="must exceed"):
        ReliabilityPolicy(fresh_age_seconds=300, stale_age_seconds=120)
