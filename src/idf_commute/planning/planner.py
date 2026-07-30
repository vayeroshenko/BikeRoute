from __future__ import annotations

import asyncio
from datetime import timedelta

from idf_commute.domain.models import (
    BikeRequest,
    BikeRoute,
    Disruption,
    OutboundOption,
    OutboundOptionKind,
    Station,
    TimeInterval,
    TransitJourney,
    TransitRequest,
)
from idf_commute.planning.deduplicate import select_diverse_outbound_options
from idf_commute.planning.disruptions import (
    disruption_penalty_minutes,
    match_journey_disruptions,
)
from idf_commute.planning.models import (
    CandidateRejection,
    OutboundPlan,
    OutboundPlanningRequest,
    planning_horizon_end,
)
from idf_commute.planning.scoring import score_outbound
from idf_commute.providers.protocols import (
    BikeRouter,
    DisruptionProvider,
    TransitRouter,
)


class OutboundPlanner:
    def __init__(
        self,
        *,
        bike_router: BikeRouter,
        transit_router: TransitRouter,
        disruption_provider: DisruptionProvider,
        concurrency: int = 4,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self._bike_router = bike_router
        self._transit_router = transit_router
        self._disruption_provider = disruption_provider
        self._semaphore = asyncio.Semaphore(concurrency)

    async def plan(self, request: OutboundPlanningRequest) -> OutboundPlan:
        interval = TimeInterval(
            begin=request.depart_at,
            end=planning_horizon_end(request.depart_at),
        )
        disruptions_task = asyncio.create_task(self._disruption_provider.disruptions(interval))
        baseline_task = asyncio.create_task(self._baseline_journeys(request))
        bike_results = await asyncio.gather(
            *(self._bike_routes(request, station) for station in request.candidate_stations)
        )

        rejections: list[CandidateRejection] = []
        eligible: list[tuple[Station, BikeRoute]] = []
        for station, routes in zip(
            request.candidate_stations,
            bike_results,
            strict=True,
        ):
            for route in routes:
                minutes = route.duration_seconds / 60
                if minutes > request.max_bike_minutes:
                    rejections.append(
                        CandidateRejection(
                            station_id=station.id,
                            station_name=station.name,
                            bike_route_title=route.title,
                            bike_duration_minutes=minutes,
                            reason="bike route exceeds hard maximum",
                        )
                    )
                else:
                    eligible.append((station, route))

        candidate_journeys = await asyncio.gather(
            *(self._station_journeys(request, station, route) for station, route in eligible)
        )
        disruptions = await disruptions_task
        baseline_journeys = await baseline_task

        options: list[OutboundOption] = []
        for (station, route), journeys in zip(
            eligible,
            candidate_journeys,
            strict=True,
        ):
            ready_at = request.depart_at + timedelta(
                seconds=route.duration_seconds,
                minutes=request.parking_buffer_minutes,
            )
            if not journeys:
                rejections.append(
                    CandidateRejection(
                        station_id=station.id,
                        station_name=station.name,
                        bike_route_title=route.title,
                        bike_duration_minutes=route.duration_seconds / 60,
                        reason="no transit journey returned from exact station",
                    )
                )
                continue
            viable_journeys = [
                journey for journey in journeys if journey.departure >= ready_at
            ]
            if not viable_journeys:
                rejections.append(
                    CandidateRejection(
                        station_id=station.id,
                        station_name=station.name,
                        bike_route_title=route.title,
                        bike_duration_minutes=route.duration_seconds / 60,
                        reason="transit departure leaves insufficient bike/parking time",
                    )
                )
                continue
            walkable_journeys = [
                journey
                for journey in viable_journeys
                if _within_walking_limits(journey, request)
            ]
            if not walkable_journeys:
                closest = min(
                    viable_journeys,
                    key=lambda journey: _walking_limit_ratio(journey, request),
                )
                total_walk = walking_duration_minutes(closest)
                longest_walk = longest_walking_leg_minutes(closest)
                rejections.append(
                    CandidateRejection(
                        station_id=station.id,
                        station_name=station.name,
                        bike_route_title=route.title,
                        bike_duration_minutes=route.duration_seconds / 60,
                        walking_duration_minutes=total_walk,
                        walking_leg_duration_minutes=longest_walk,
                        reason=_walking_rejection_reason(closest, request),
                    )
                )
                continue
            options.extend(
                self._bike_transit_option(
                    request, station, route, journey, disruptions
                )
                for journey in walkable_journeys
            )

        walkable_baselines = [
            journey
            for journey in baseline_journeys
            if _within_walking_limits(journey, request)
        ]
        if baseline_journeys and not walkable_baselines:
            closest = min(
                baseline_journeys,
                key=lambda journey: _walking_limit_ratio(journey, request),
            )
            rejections.append(
                CandidateRejection(
                    station_id="all-transit",
                    station_name="All transit",
                    walking_duration_minutes=walking_duration_minutes(closest),
                    walking_leg_duration_minutes=longest_walking_leg_minutes(closest),
                    reason=_walking_rejection_reason(closest, request),
                )
            )
        options.extend(
            self._baseline_option(request, journey, disruptions)
            for journey in walkable_baselines
        )
        return OutboundPlan(
            requested_departure=request.depart_at,
            preferred_bike_minutes=request.preferred_bike_minutes,
            max_bike_minutes=request.max_bike_minutes,
            max_walking_minutes=request.max_walking_minutes,
            max_walking_leg_minutes=request.max_walking_leg_minutes,
            candidate_station_count=len(request.candidate_stations),
            score_mode=request.score_mode,
            score_weights=request.score_weights,
            options=tuple(
                select_diverse_outbound_options(
                    options,
                    limit=request.max_results,
                )
            ),
            rejections=tuple(rejections),
        )

    async def _bike_routes(
        self,
        request: OutboundPlanningRequest,
        station: Station,
    ) -> list[BikeRoute]:
        if station.location is None:
            return []
        async with self._semaphore:
            return await self._bike_router.routes(
                BikeRequest(
                    origin=request.home,
                    destination=station.location,
                    profile=request.bike_profile,
                    bike_type=request.bike_type,
                    average_speed_kmh=request.bike_average_speed_kmh,
                )
            )

    async def _station_journeys(
        self,
        request: OutboundPlanningRequest,
        station: Station,
        route: BikeRoute,
    ) -> list[TransitJourney]:
        ready_at = request.depart_at + timedelta(
            seconds=route.duration_seconds,
            minutes=request.parking_buffer_minutes,
        )
        async with self._semaphore:
            return await self._transit_router.journeys(
                TransitRequest(
                    origin_id=station.id,
                    destination_id=request.work_transit_id,
                    datetime=ready_at,
                    min_journeys=request.max_results,
                )
            )

    async def _baseline_journeys(
        self,
        request: OutboundPlanningRequest,
    ) -> list[TransitJourney]:
        async with self._semaphore:
            return await self._transit_router.journeys(
                TransitRequest(
                    origin_id=request.home_transit_id,
                    destination_id=request.work_transit_id,
                    datetime=request.depart_at,
                    min_journeys=request.max_results,
                )
            )

    def _bike_transit_option(
        self,
        request: OutboundPlanningRequest,
        station: Station,
        route: BikeRoute,
        journey: TransitJourney,
        disruptions: list[Disruption],
    ) -> OutboundOption:
        matched = match_journey_disruptions(journey, disruptions)
        score = score_outbound(
            departure=request.depart_at,
            arrival=journey.arrival,
            transit_journey=journey,
            bike_route=route,
            preferred_bike_minutes=request.preferred_bike_minutes,
            max_bike_minutes=request.max_bike_minutes,
            disruption_penalty_minutes=disruption_penalty_minutes(matched),
            weights=request.score_weights,
        )
        return OutboundOption(
            kind=OutboundOptionKind.BIKE_TRANSIT,
            station=station,
            bike_route=route,
            transit_journey=journey,
            departure=request.depart_at,
            arrival=journey.arrival,
            parking_buffer_seconds=round(request.parking_buffer_minutes * 60),
            matched_disruptions=matched,
            score=score,
        )
    def _baseline_option(
        self,
        request: OutboundPlanningRequest,
        journey: TransitJourney,
        disruptions: list[Disruption],
    ) -> OutboundOption:
        matched = match_journey_disruptions(journey, disruptions)
        score = score_outbound(
            departure=request.depart_at,
            arrival=journey.arrival,
            transit_journey=journey,
            bike_route=None,
            preferred_bike_minutes=request.preferred_bike_minutes,
            max_bike_minutes=request.max_bike_minutes,
            disruption_penalty_minutes=disruption_penalty_minutes(matched),
            weights=request.score_weights,
        )
        return OutboundOption(
            kind=OutboundOptionKind.ALL_TRANSIT,
            transit_journey=journey,
            departure=request.depart_at,
            arrival=journey.arrival,
            matched_disruptions=matched,
            score=score,
        )


def walking_duration_minutes(journey: TransitJourney) -> float:
    return (
        sum(
            leg.duration_seconds
            for leg in journey.legs
            if leg.mode == "walking"
        )
        / 60
    )


def longest_walking_leg_minutes(journey: TransitJourney) -> float:
    return max(
        (
            leg.duration_seconds / 60
            for leg in journey.legs
            if leg.mode == "walking"
        ),
        default=0,
    )


def _within_walking_limits(
    journey: TransitJourney,
    request: OutboundPlanningRequest,
) -> bool:
    return (
        walking_duration_minutes(journey) <= request.max_walking_minutes
        and longest_walking_leg_minutes(journey) <= request.max_walking_leg_minutes
    )


def _walking_limit_ratio(
    journey: TransitJourney,
    request: OutboundPlanningRequest,
) -> float:
    return max(
        walking_duration_minutes(journey) / request.max_walking_minutes,
        longest_walking_leg_minutes(journey) / request.max_walking_leg_minutes,
    )


def _walking_rejection_reason(
    journey: TransitJourney,
    request: OutboundPlanningRequest,
) -> str:
    violations: list[str] = []
    if walking_duration_minutes(journey) > request.max_walking_minutes:
        violations.append("total walking")
    if longest_walking_leg_minutes(journey) > request.max_walking_leg_minutes:
        violations.append("single walking leg")
    return f"transit journey exceeds {' and '.join(violations)} hard maximum"
