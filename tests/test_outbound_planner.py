from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from idf_commute.domain.models import (
    BikeRequest,
    BikeRoute,
    Disruption,
    Freshness,
    Location,
    OutboundOptionKind,
    Station,
    TimeInterval,
    TransitJourney,
    TransitLeg,
    TransitRequest,
)
from idf_commute.planning.models import OutboundPlanningRequest
from idf_commute.planning.planner import OutboundPlanner

PARIS = ZoneInfo("Europe/Paris")


class FakeBikeRouter:
    async def routes(self, request: BikeRequest) -> list[BikeRoute]:
        assert request.destination.label == "Candidate"
        return [
            BikeRoute(title="RECOMMENDED", duration_seconds=19 * 60, distance_m=5000),
            BikeRoute(title="SAFER", duration_seconds=22 * 60, distance_m=5200),
            BikeRoute(title="TOO_LONG", duration_seconds=60 * 60, distance_m=15000),
        ]


class FakeTransitRouter:
    def __init__(self) -> None:
        self.requests: list[TransitRequest] = []

    async def journeys(self, request: TransitRequest) -> list[TransitJourney]:
        self.requests.append(request)
        assert request.datetime is not None
        departure = request.datetime + timedelta(minutes=2)
        return [
            TransitJourney(
                duration_seconds=30 * 60,
                departure=departure,
                arrival=departure + timedelta(minutes=30),
                legs=(
                    TransitLeg(
                        type="public_transport",
                        duration_seconds=30 * 60,
                        freshness=Freshness.REALTIME,
                        line_id="line:rer-b",
                        origin_id=request.origin_id,
                        destination_id=request.destination_id,
                    ),
                ),
            )
        ]


class FakeDisruptions:
    async def disruptions(
        self,
        interval: TimeInterval | None = None,
    ) -> list[Disruption]:
        assert interval is not None
        return [
            Disruption(
                id="rer-work",
                title="RER work",
                message="Source message",
                severity="PERTURBEE",
                application_periods=(interval,),
                affected_line_ids=frozenset({"line:rer-b"}),
                source="test",
            )
        ]


def planning_request() -> OutboundPlanningRequest:
    return OutboundPlanningRequest(
        home=Location(latitude=48.8, longitude=2.3, label="Home"),
        home_transit_id="2.3;48.8",
        work_transit_id="stop_area:work",
        candidate_stations=(
            Station(
                id="stop_area:candidate",
                name="Candidate",
                location=Location(latitude=48.75, longitude=2.35, label="Candidate"),
            ),
        ),
        depart_at=datetime(2026, 7, 30, 8, 0, tzinfo=PARIS),
        preferred_bike_minutes=20,
        max_bike_minutes=25,
        parking_buffer_minutes=4,
    )


@pytest.mark.asyncio
async def test_explicit_station_join_thresholds_and_baseline() -> None:
    transit = FakeTransitRouter()
    plan = await OutboundPlanner(
        bike_router=FakeBikeRouter(),
        transit_router=transit,
        disruption_provider=FakeDisruptions(),
    ).plan(planning_request())

    assert len(plan.options) == 3
    assert sum(option.kind is OutboundOptionKind.BIKE_TRANSIT for option in plan.options) == 2
    assert sum(option.kind is OutboundOptionKind.ALL_TRANSIT for option in plan.options) == 1
    assert [rejection.bike_duration_minutes for rejection in plan.rejections] == [60]
    bike_requests = [
        request for request in transit.requests if request.origin_id == "stop_area:candidate"
    ]
    assert [request.datetime.hour for request in bike_requests if request.datetime] == [8, 8]
    assert [request.datetime.minute for request in bike_requests if request.datetime] == [23, 26]
    assert all(
        option.station and option.station.id == "stop_area:candidate"
        for option in plan.options
        if option.kind is OutboundOptionKind.BIKE_TRANSIT
    )


@pytest.mark.asyncio
async def test_disruptions_are_matched_and_scored() -> None:
    plan = await OutboundPlanner(
        bike_router=FakeBikeRouter(),
        transit_router=FakeTransitRouter(),
        disruption_provider=FakeDisruptions(),
    ).plan(planning_request())
    assert all(option.matched_disruptions for option in plan.options)
    assert all(option.score.disruption_penalty_minutes == 12 for option in plan.options)
