from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from idf_commute.domain.models import (
    BikeFacilitySegment,
    BikeRequest,
    BikeRoute,
    ElevationPoint,
)
from idf_commute.providers.prim_client import PrimClient


class GeoveloSchemaError(ValueError):
    """A Geovelo response does not match the observed contract."""


class GeoveloAdapter:
    def __init__(self, client: PrimClient, endpoint_url: str) -> None:
        self._client = client
        self._endpoint_url = endpoint_url

    async def routes(self, request: BikeRequest) -> list[BikeRoute]:
        response = await self._client.post_json(
            self._endpoint_url,
            params={
                "instructions": str(request.instructions).lower(),
                "elevations": str(request.elevations).lower(),
                "geometry": str(request.geometry).lower(),
                "single_result": str(not request.alternatives).lower(),
            },
            json_body={
                "waypoints": [
                    {
                        "latitude": request.origin.latitude,
                        "longitude": request.origin.longitude,
                        "title": request.origin.label or "ORIGIN",
                    },
                    {
                        "latitude": request.destination.latitude,
                        "longitude": request.destination.longitude,
                        "title": request.destination.label or "DESTINATION",
                    },
                ],
                "bikeDetails": {
                    "profile": request.profile,
                    "bikeType": request.bike_type,
                    "averageSpeed": request.average_speed_kmh,
                },
                "transportModes": ["BIKE"],
            },
        )
        return normalize_geovelo_routes(response.body)


def normalize_geovelo_routes(payload: Any) -> list[BikeRoute]:
    raw_routes = _route_collection(payload)
    return [_normalize_route(route) for route in raw_routes]


def _route_collection(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        collection = payload
    elif isinstance(payload, dict) and isinstance(payload.get("routes"), list):
        collection = payload["routes"]
    else:
        raise GeoveloSchemaError("Expected a route array or an object containing routes")
    routes = [route for route in collection if isinstance(route, Mapping)]
    if len(routes) != len(collection):
        raise GeoveloSchemaError("Every Geovelo route must be an object")
    return routes


def _normalize_route(route: Mapping[str, Any]) -> BikeRoute:
    sections = route.get("sections")
    bike_section = _first_bike_section(sections)
    details = _mapping(bike_section.get("details")) if bike_section else {}
    summary_distances = _mapping(route.get("distances"))
    detail_distances = _mapping(details.get("distances"))

    elevations = tuple(
        ElevationPoint(
            distance_from_start_m=_number(row.get("distanceFromStart"), default=0),
            elevation_m=_number(row.get("elevation"), default=0),
            geometry_index=_integer(row.get("geometryIndex"), default=0),
        )
        for row in _decode_positional_rows(details.get("elevations"), "elevations")
    )
    facility_segments = tuple(
        BikeFacilitySegment(
            direction=_optional_string(row.get("direction")),
            road_name=_optional_string(row.get("roadName")),
            distance_m=_number(row.get("roadLength"), default=0),
            facility=_optional_string(row.get("facility")),
            cyclability=_optional_number(row.get("cyclability")),
            geometry_index=_optional_integer(row.get("geometryIndex")),
            orientation=_optional_string(row.get("orientation")),
            city_names=_city_names(row.get("cityNames")),
            duration_seconds=_optional_number(row.get("duration")),
        )
        for row in _decode_positional_rows(details.get("instructions"), "instructions")
    )

    geometry = _optional_string(bike_section.get("geometry")) if bike_section else None
    if geometry in {"<redacted-geometry>", ""}:
        geometry = None
    provider_id = _optional_string(route.get("id"))
    if provider_id in {"<redacted-route-id>", ""}:
        provider_id = None

    facility_distances = {
        key: _number(value, default=0)
        for key, value in detail_distances.items()
        if key
        not in {
            "total",
            "normalRoads",
            "recommendedRoads",
            "discouragedRoads",
        }
        and isinstance(key, str)
        and isinstance(value, (int, float))
    }
    return BikeRoute(
        provider_id=provider_id,
        title=_required_string(route.get("title"), "route.title"),
        duration_seconds=_integer(
            route.get("duration", bike_section.get("duration") if bike_section else 0),
            default=0,
        ),
        distance_m=_number(summary_distances.get("total"), default=0),
        normal_roads_m=_number(summary_distances.get("normalRoads"), default=0),
        recommended_roads_m=_number(summary_distances.get("recommendedRoads"), default=0),
        discouraged_roads_m=_number(summary_distances.get("discouragedRoads"), default=0),
        vertical_gain_m=_number(
            details.get("verticalGain", route.get("verticalGain")),
            default=0,
        ),
        vertical_loss_m=_number(
            details.get("verticalLoss", route.get("verticalLoss")),
            default=0,
        ),
        profile=_optional_string(details.get("profile", route.get("profile"))),
        bike_type=_optional_string(details.get("bikeType", route.get("bikeType"))),
        average_speed_kmh=_optional_number(details.get("averageSpeed", route.get("averageSpeed"))),
        encoded_geometry=geometry,
        elevations=elevations,
        facility_segments=facility_segments,
        facility_distances_m=facility_distances,
    )


def _first_bike_section(value: Any) -> Mapping[str, Any] | None:
    if not isinstance(value, list):
        return None
    for section in value:
        if isinstance(section, Mapping) and section.get("transportMode") == "BIKE":
            return section
    return None


def _decode_positional_rows(value: Any, field_name: str) -> list[dict[str, Any]]:
    if value in (None, []):
        return []
    if not isinstance(value, list) or not value or not _is_string_row(value[0]):
        raise GeoveloSchemaError(f"{field_name} must start with a string header row")
    header = value[0]
    decoded: list[dict[str, Any]] = []
    for row in value[1:]:
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
            raise GeoveloSchemaError(f"{field_name} contains a non-array row")
        decoded.append(
            {key: row[index] if index < len(row) else None for index, key in enumerate(header)}
        )
    return decoded


def _is_string_row(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(item, str) for item in value)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise GeoveloSchemaError(f"{field_name} must be a non-empty string")
    return value


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _number(value: Any, *, default: float) -> float:
    if isinstance(value, bool):
        return default
    return float(value) if isinstance(value, (int, float)) else default


def _optional_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _integer(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    return int(value) if isinstance(value, (int, float)) else default


def _optional_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return int(value) if isinstance(value, (int, float)) else None


def _city_names(value: Any) -> str | tuple[str, ...] | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return None
