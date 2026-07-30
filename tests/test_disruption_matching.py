from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from idf_commute.domain.models import (
    Disruption,
    Freshness,
    TimeInterval,
    TransitJourney,
    TransitLeg,
)
from idf_commute.planning.disruptions import (
    disruption_penalty_minutes,
    match_journey_disruptions,
)

PARIS = ZoneInfo("Europe/Paris")


def journey() -> TransitJourney:
    departure = datetime(2026, 7, 30, 8, 0, tzinfo=PARIS)
    return TransitJourney(
        duration_seconds=1800,
        departure=departure,
        arrival=departure + timedelta(minutes=30),
        legs=(
            TransitLeg(
                type="public_transport",
                duration_seconds=1800,
                freshness=Freshness.REALTIME,
                line_id="line:rer-b",
                origin_id="stop_area:a",
                destination_id="stop_area:b",
            ),
        ),
    )


def disruption(
    identifier: str,
    *,
    line_id: str = "line:rer-b",
    begin_hour: int = 7,
    severity: str = "PERTURBEE",
) -> Disruption:
    begin = datetime(2026, 7, 30, begin_hour, 0, tzinfo=PARIS)
    return Disruption(
        id=identifier,
        title="Work",
        message="Source message",
        severity=severity,
        application_periods=(TimeInterval(begin=begin, end=begin + timedelta(hours=2)),),
        affected_line_ids=frozenset({line_id}),
        source="test",
    )


def test_matches_only_active_stable_id_scope() -> None:
    matches = match_journey_disruptions(
        journey(),
        [
            disruption("active"),
            disruption("inactive", begin_hour=10),
            disruption("other-line", line_id="line:other"),
        ],
    )
    assert [item.id for item in matches] == ["active"]


def test_stop_scope_can_match_without_line_scope() -> None:
    stop_disruption = Disruption(
        id="stop",
        title="Stop closed",
        message="Source message",
        application_periods=(
            TimeInterval(
                begin=datetime(2026, 7, 30, 7, 0, tzinfo=PARIS),
                end=datetime(2026, 7, 30, 10, 0, tzinfo=PARIS),
            ),
        ),
        affected_stop_area_ids=frozenset({"stop_area:b"}),
        source="test",
    )
    assert match_journey_disruptions(journey(), [stop_disruption]) == (stop_disruption,)


def test_penalty_reflects_reported_severity() -> None:
    assert disruption_penalty_minutes((disruption("reduced"),)) == 12
    cancelled = disruption("cancelled", severity="NO_SERVICE")
    assert disruption_penalty_minutes((cancelled,)) == 30
