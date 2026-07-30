from __future__ import annotations

from typing import Protocol

from idf_commute.domain.models import (
    BikeRequest,
    BikeRoute,
    Departure,
    Disruption,
    TimeInterval,
    TransitJourney,
    TransitRequest,
)


class TransitRouter(Protocol):
    async def journeys(self, request: TransitRequest) -> list[TransitJourney]: ...


class BikeRouter(Protocol):
    async def routes(self, request: BikeRequest) -> list[BikeRoute]: ...


class DisruptionProvider(Protocol):
    async def disruptions(self, interval: TimeInterval | None = None) -> list[Disruption]: ...


class DepartureProvider(Protocol):
    async def departures(self, stop_id: str, line_id: str | None = None) -> list[Departure]: ...
