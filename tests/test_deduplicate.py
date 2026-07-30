from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from idf_commute.domain.models import (
    BikeRoute,
    Freshness,
    Location,
    OutboundOption,
    OutboundOptionKind,
    ScoreBreakdown,
    Station,
    TransitJourney,
    TransitLeg,
)
from idf_commute.planning.deduplicate import (
    deduplicate_outbound_options,
    select_diverse_outbound_options,
)

PARIS = ZoneInfo("Europe/Paris")


def option(score: float, line_id: str = "line:rer-b") -> OutboundOption:
    departure = datetime(2026, 7, 30, 8, 0, tzinfo=PARIS)
    journey = TransitJourney(
        duration_seconds=1800,
        departure=departure,
        arrival=departure + timedelta(minutes=30),
        legs=(
            TransitLeg(
                type="public_transport",
                duration_seconds=1800,
                freshness=Freshness.REALTIME,
                line_id=line_id,
                origin_id="stop:a",
                destination_id="stop:b",
            ),
        ),
    )
    breakdown = ScoreBreakdown(
        door_to_door_minutes=score,
        bike_penalty_minutes=0,
        transfer_penalty_minutes=0,
        disruption_penalty_minutes=0,
        freshness_penalty_minutes=0,
        cycling_comfort_penalty_minutes=0,
        total_minutes=score,
    )
    return OutboundOption(
        kind=OutboundOptionKind.ALL_TRANSIT,
        transit_journey=journey,
        departure=departure,
        arrival=journey.arrival,
        score=breakdown,
    )


def test_keeps_first_ranked_materially_distinct_options() -> None:
    first = option(30)
    duplicate = option(35)
    other = option(40, line_id="line:other")
    assert deduplicate_outbound_options(
        [first, duplicate, other],
        limit=5,
    ) == [first, other]


def test_limit_applies_after_deduplication() -> None:
    first = option(30)
    second = option(40, line_id="line:other")
    assert deduplicate_outbound_options([first, second], limit=1) == [first]


def test_diverse_selection_reserves_transit_only_alternatives() -> None:
    bike_options = [
        option(score, line_id=f"line:bike-{score}").model_copy(
            update={
                "kind": OutboundOptionKind.BIKE_TRANSIT,
                "station": Station(
                    id="station",
                    name="Station",
                    location=Location(latitude=48.8, longitude=2.3),
                ),
                "bike_route": BikeRoute(
                    title=f"BIKE-{score}",
                    duration_seconds=600,
                    distance_m=2500,
                ),
            }
        )
        for score in range(10, 18)
    ]
    transit_options = [
        option(score, line_id=f"line:transit-{score}") for score in range(50, 57)
    ]

    selected = select_diverse_outbound_options(
        bike_options + transit_options,
        limit=10,
    )

    assert len(selected) == 10
    assert sum(
        candidate.kind is OutboundOptionKind.ALL_TRANSIT for candidate in selected
    ) == 6
