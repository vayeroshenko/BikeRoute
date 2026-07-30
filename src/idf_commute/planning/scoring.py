from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from idf_commute.domain.models import (
    BikeRoute,
    Freshness,
    ScoreBreakdown,
    TransitJourney,
)


class ScoreMode(StrEnum):
    BALANCED = "balanced"
    FASTEST = "fastest"
    FEWEST_TRANSFERS = "fewest-transfers"
    EASY_RIDE = "easy-ride"
    RELIABLE = "reliable"


class ScoreWeights(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    door_to_door: float = Field(default=1, ge=0)
    bike_penalty: float = Field(default=1, ge=0)
    transfers: float = Field(default=1, ge=0)
    disruptions: float = Field(default=1, ge=0)
    freshness: float = Field(default=1, ge=0)
    cycling_comfort: float = Field(default=1, ge=0)


SCORE_MODE_WEIGHTS: dict[ScoreMode, ScoreWeights] = {
    ScoreMode.BALANCED: ScoreWeights(),
    ScoreMode.FASTEST: ScoreWeights(
        bike_penalty=0,
        transfers=0,
        disruptions=0,
        freshness=0,
        cycling_comfort=0,
    ),
    ScoreMode.FEWEST_TRANSFERS: ScoreWeights(
        bike_penalty=0.5,
        transfers=5,
        disruptions=1,
        freshness=1,
        cycling_comfort=0.5,
    ),
    ScoreMode.EASY_RIDE: ScoreWeights(
        bike_penalty=3,
        transfers=1,
        disruptions=1,
        freshness=1,
        cycling_comfort=4,
    ),
    ScoreMode.RELIABLE: ScoreWeights(
        bike_penalty=0.5,
        transfers=1.5,
        disruptions=3,
        freshness=3,
        cycling_comfort=0.5,
    ),
}


def weights_for_mode(
    mode: ScoreMode,
    overrides: dict[str, float] | None = None,
) -> ScoreWeights:
    return SCORE_MODE_WEIGHTS[mode].model_copy(update=overrides or {})


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
    weights: ScoreWeights | None = None,
) -> ScoreBreakdown:
    active_weights = weights or ScoreWeights()
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
        door_to_door * active_weights.door_to_door
        + bike_penalty * active_weights.bike_penalty
        + transfer_penalty * active_weights.transfers
        + disruption_penalty_minutes * active_weights.disruptions
        + freshness_penalty * active_weights.freshness
        + comfort_penalty * active_weights.cycling_comfort
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
