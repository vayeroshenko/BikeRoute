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
from idf_commute.planning.reliability import ReliabilityPolicy, assess_reliability

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


def test_reliability_age_thresholds_must_be_ordered() -> None:
    with pytest.raises(ValueError, match="must exceed"):
        ReliabilityPolicy(fresh_age_seconds=300, stale_age_seconds=120)
