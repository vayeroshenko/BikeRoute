from __future__ import annotations

import math
from datetime import datetime

from idf_commute.domain.models import (
    BikeRoute,
    Freshness,
    ScoreBreakdown,
    TransitJourney,
)


def bike_penalty_minutes(
    minutes: float,
    preferred_minutes: float,
    hard_minutes: float,
) -> float:
    if preferred_minutes < 0 or hard_minutes < preferred_minutes:
        raise ValueError("bike thresholds must satisfy 0 <= preferred <= hard")
    if minutes > hard_minutes:
        return math.inf
    if minutes <= preferred_minutes:
        return 0.0
    return 0.7 * (minutes - preferred_minutes) ** 2


def score_outbound(
    *,
    departure: datetime,
    arrival: datetime,
    transit_journey: TransitJourney,
    bike_route: BikeRoute | None,
    preferred_bike_minutes: float,
    max_bike_minutes: float,
    disruption_penalty_minutes: float = 0,
) -> ScoreBreakdown:
    door_to_door = (arrival - departure).total_seconds() / 60
    if door_to_door < 0:
        raise ValueError("arrival must not precede departure")
    bike_penalty = (
        bike_penalty_minutes(
            bike_route.duration_seconds / 60,
            preferred_bike_minutes,
            max_bike_minutes,
        )
        if bike_route is not None
        else 0
    )
    if math.isinf(bike_penalty):
        raise ValueError("bike route exceeds hard maximum")
    transfer_penalty = transit_journey.transfers * 4.0
    scheduled_critical_legs = sum(
        1
        for leg in transit_journey.legs
        if leg.type == "public_transport" and leg.freshness is not Freshness.REALTIME
    )
    freshness_penalty = scheduled_critical_legs * 3.0
    comfort_penalty = _cycling_comfort_penalty(bike_route)
    total = (
        door_to_door
        + bike_penalty
        + transfer_penalty
        + disruption_penalty_minutes
        + freshness_penalty
        + comfort_penalty
    )
    return ScoreBreakdown(
        door_to_door_minutes=door_to_door,
        bike_penalty_minutes=bike_penalty,
        transfer_penalty_minutes=transfer_penalty,
        disruption_penalty_minutes=disruption_penalty_minutes,
        freshness_penalty_minutes=freshness_penalty,
        cycling_comfort_penalty_minutes=comfort_penalty,
        total_minutes=total,
    )


def _cycling_comfort_penalty(route: BikeRoute | None) -> float:
    if route is None or route.distance_m <= 0:
        return 0
    discouraged_share = min(route.discouraged_roads_m / route.distance_m, 1)
    return discouraged_share * 3
