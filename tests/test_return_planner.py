from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from idf_commute.cli import _confirm_return_selection
from idf_commute.domain.models import (
    BikeRequest,
    BikeRoute,
    Disruption,
    Freshness,
    Location,
    Station,
    TimeInterval,
    TransitJourney,
    TransitLeg,
    TransitRequest,
)
from idf_commute.domain.state import BikeLocation, BikeState
from idf_commute.persistence import BikeStateStore
from idf_commute.planning.models import ReturnPlanningRequest
from idf_commute.planning.planner import ReturnPlanner

PARIS = ZoneInfo("Europe/Paris")


class ReturnBikeRouter:
    def __init__(self) -> None:
        self.requests: list[BikeRequest] = []

    async def routes(self, request: BikeRequest) -> list[BikeRoute]:
        self.requests.append(request)
        return [
            BikeRoute(
                title="RECOMMENDED",
                duration_seconds=18 * 60,
                distance_m=4800,
            ),
            BikeRoute(
                title="TOO_LONG",
                duration_seconds=60 * 60,
                distance_m=15000,
            ),
        ]


class ReturnTransitRouter:
    def __init__(self) -> None:
        self.requests: list[TransitRequest] = []

    async def journeys(self, request: TransitRequest) -> list[TransitJourney]:
        self.requests.append(request)
        departure = request.datetime
        assert departure is not None
        arrival = departure + timedelta(minutes=40)
        return [
            TransitJourney(
                id="journey:return",
                duration_seconds=40 * 60,
                departure=departure,
                arrival=arrival,
                legs=(
                    TransitLeg(
                        type="public_transport",
                        duration_seconds=40 * 60,
                        departure=departure,
                        arrival=arrival,
                        freshness=Freshness.REALTIME,
                        line_id="line:rer-b",
                        origin_id=request.origin_id,
                        destination_id=request.destination_id,
                    ),
                ),
            )
        ]


class NoReturnDisruptions:
    async def disruptions(
        self,
        interval: TimeInterval | None = None,
    ) -> list[Disruption]:
        assert interval is not None
        return []


def return_request() -> ReturnPlanningRequest:
    station = Station(
        id="stop_area:sceaux",
        name="Sceaux",
        location=Location(latitude=48.781, longitude=2.297, label="Sceaux"),
    )
    return ReturnPlanningRequest(
        work_transit_id="2.4;48.7",
        home=Location(latitude=48.8, longitude=2.3, label="HOME"),
        bike_station=station,
        bike_state=BikeState(
            location=BikeLocation.STATION,
            station_id=station.id,
            station_name=station.name,
        ),
        depart_at=datetime(2026, 7, 30, 18, 0, tzinfo=PARIS),
        preferred_bike_minutes=20,
        max_bike_minutes=25,
        retrieval_buffer_minutes=4,
    )


@pytest.mark.asyncio
async def test_return_forces_transit_to_bicycle_station_then_cycles_home() -> None:
    bikes = ReturnBikeRouter()
    transit = ReturnTransitRouter()

    plan = await ReturnPlanner(
        bike_router=bikes,
        transit_router=transit,
        disruption_provider=NoReturnDisruptions(),
    ).plan(return_request())

    assert len(transit.requests) == 1
    assert transit.requests[0].origin_id == "2.4;48.7"
    assert transit.requests[0].destination_id == "stop_area:sceaux"
    assert len(bikes.requests) == 1
    assert bikes.requests[0].origin.label == "Sceaux"
    assert bikes.requests[0].destination.label == "HOME"
    assert len(plan.options) == 1
    assert plan.options[0].arrival == datetime(2026, 7, 30, 19, 2, tzinfo=PARIS)
    assert plan.options[0].station.id == "stop_area:sceaux"
    assert [rejection.bike_duration_minutes for rejection in plan.rejections] == [60]


@pytest.mark.asyncio
async def test_return_confirmation_only_moves_bicycle_from_matching_station(
    tmp_path: Path,
) -> None:
    plan = await ReturnPlanner(
        bike_router=ReturnBikeRouter(),
        transit_router=ReturnTransitRouter(),
        disruption_provider=NoReturnDisruptions(),
    ).plan(return_request())
    store = BikeStateStore(tmp_path / "commute.sqlite3")
    timestamp = datetime(2026, 7, 30, 18, 0, tzinfo=PARIS)

    store.set_station(
        "stop_area:antony",
        station_name="Antony",
        updated_at=timestamp,
    )
    with pytest.raises(ValueError, match="no longer matches"):
        _confirm_return_selection(plan, 1, store)

    store.set_station(
        "stop_area:sceaux",
        station_name="Sceaux",
        updated_at=timestamp,
    )
    with pytest.raises(ValueError, match="contains 1 option"):
        _confirm_return_selection(plan, 2, store)

    state = _confirm_return_selection(plan, 1, store)
    assert state.location is BikeLocation.HOME
    assert state.source_journey_id == "journey:return"
    assert store.load() == state


def test_return_request_rejects_a_station_other_than_stored_bicycle() -> None:
    request = return_request()
    with pytest.raises(ValueError, match="must match"):
        ReturnPlanningRequest.model_validate(
            {
                **request.model_dump(),
                "bike_station": {
                    "id": "stop_area:antony",
                    "name": "Antony",
                    "location": {
                        "latitude": 48.754,
                        "longitude": 2.301,
                    },
                },
            }
        )
