from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from idf_commute.domain.models import (
    BikeRoute,
    Freshness,
    TransitJourney,
    TransitLeg,
)
from idf_commute.planning.scoring import bike_penalty_minutes, score_outbound

PARIS = ZoneInfo("Europe/Paris")


def transit_journey(departure: datetime) -> TransitJourney:
    return TransitJourney(
        duration_seconds=1800,
        departure=departure,
        arrival=departure + timedelta(minutes=30),
        transfers=1,
        legs=(
            TransitLeg(
                type="public_transport",
                duration_seconds=1800,
                freshness=Freshness.BASE_SCHEDULE,
            ),
        ),
    )


def bike_route(minutes: int, discouraged_m: int = 0) -> BikeRoute:
    return BikeRoute(
        title="RECOMMENDED",
        duration_seconds=minutes * 60,
        distance_m=5000,
        discouraged_roads_m=discouraged_m,
    )


def test_soft_bicycle_penalty_matches_report_thresholds() -> None:
    assert bike_penalty_minutes(19, 20, 25) == 0
    assert bike_penalty_minutes(20, 20, 25) == 0
    assert bike_penalty_minutes(22, 20, 25) == pytest.approx(2.8)
    assert math.isinf(bike_penalty_minutes(60, 20, 25))


def test_score_preserves_interpretable_components() -> None:
    departure = datetime(2026, 7, 30, 8, 0, tzinfo=PARIS)
    score = score_outbound(
        departure=departure,
        arrival=departure + timedelta(minutes=55),
        transit_journey=transit_journey(departure + timedelta(minutes=20)),
        bike_route=bike_route(22, discouraged_m=1000),
        preferred_bike_minutes=20,
        max_bike_minutes=25,
        disruption_penalty_minutes=12,
    )
    assert score.door_to_door_minutes == 55
    assert score.bike_penalty_minutes == pytest.approx(2.8)
    assert score.transfer_penalty_minutes == 4
    assert score.freshness_penalty_minutes == 3
    assert score.cycling_comfort_penalty_minutes == pytest.approx(0.6)
    assert score.total_minutes == pytest.approx(77.4)


def test_score_rejects_route_above_hard_limit() -> None:
    departure = datetime(2026, 7, 30, 8, 0, tzinfo=PARIS)
    with pytest.raises(ValueError, match="hard maximum"):
        score_outbound(
            departure=departure,
            arrival=departure + timedelta(hours=2),
            transit_journey=transit_journey(departure + timedelta(hours=1)),
            bike_route=bike_route(60),
            preferred_bike_minutes=20,
            max_bike_minutes=25,
        )
