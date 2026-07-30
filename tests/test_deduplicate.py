from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from idf_commute.domain.models import (
    Freshness,
    OutboundOption,
    OutboundOptionKind,
    ScoreBreakdown,
    TransitJourney,
    TransitLeg,
)
from idf_commute.planning.deduplicate import deduplicate_outbound_options

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
