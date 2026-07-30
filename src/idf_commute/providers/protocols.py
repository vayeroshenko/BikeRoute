from __future__ import annotations

from typing import Protocol

from idf_commute.domain.models import (
    BikeRequest,
    BikeRoute,
    Departure,
    Disruption,
    Station,
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


class PlaceProvider(Protocol):
    async def stations(self, query: str) -> list[Station]: ...


class LineStationProvider(Protocol):
    async def line_stations(self, line_id: str) -> list[Station]: ...


class StationProvider(PlaceProvider, LineStationProvider, Protocol):
    """Resolve stations by free-text query or by a stable line identifier."""
