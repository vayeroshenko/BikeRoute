from __future__ import annotations

from dataclasses import dataclass

from idf_commute.domain.models import BikeRoute, Location, TransitLeg


class GeometryDecodeError(ValueError):
    """Raised when an encoded bicycle geometry is incomplete or malformed."""


@dataclass(frozen=True)
class MapPath:
    label: str
    coordinates: tuple[tuple[float, float], ...]
    color: tuple[int, int, int]
    schematic: bool = False


@dataclass(frozen=True)
class RouteMap:
    paths: tuple[MapPath, ...]

    @property
    def coordinates(self) -> tuple[tuple[float, float], ...]:
        return tuple(point for path in self.paths for point in path.coordinates)

    @property
    def uses_schematic_segments(self) -> bool:
        return any(path.schematic for path in self.paths)


def build_route_map(
    bike_route: BikeRoute | None,
    transit_legs: tuple[TransitLeg, ...],
) -> RouteMap:
    paths: list[MapPath] = []
    if bike_route is not None and bike_route.encoded_geometry:
        try:
            bike_points = decode_polyline(bike_route.encoded_geometry)
        except GeometryDecodeError:
            bike_points = ()
        if len(bike_points) >= 2:
            paths.append(
                MapPath(
                    label=f"Bike · {bike_route.title}",
                    coordinates=tuple(_lon_lat(point) for point in bike_points),
                    color=(22, 163, 74),
                )
            )

    for leg in transit_legs:
        locations = leg.geometry
        schematic = False
        if len(locations) < 2 and leg.origin_location and leg.destination_location:
            locations = (leg.origin_location, leg.destination_location)
            schematic = True
        if len(locations) < 2:
            continue
        paths.append(
            MapPath(
                label=_leg_label(leg),
                coordinates=tuple(_lon_lat(point) for point in locations),
                color=_leg_color(leg),
                schematic=schematic,
            )
        )
    return RouteMap(paths=tuple(paths))


def decode_polyline(encoded: str, *, precision: int = 5) -> tuple[Location, ...]:
    """Decode the Google-style polyline returned by Geovelo."""
    if not encoded or encoded.startswith("<redacted"):
        return ()
    index = 0
    latitude = 0
    longitude = 0
    factor = 10**precision
    points: list[Location] = []
    while index < len(encoded):
        latitude_delta, index = _decode_value(encoded, index)
        longitude_delta, index = _decode_value(encoded, index)
        latitude += latitude_delta
        longitude += longitude_delta
        try:
            points.append(
                Location(
                    latitude=latitude / factor,
                    longitude=longitude / factor,
                )
            )
        except ValueError as exc:
            raise GeometryDecodeError("Decoded geometry lies outside Île-de-France") from exc
    return tuple(points)


def _decode_value(encoded: str, index: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if index >= len(encoded):
            raise GeometryDecodeError("Encoded geometry ended unexpectedly")
        value = ord(encoded[index]) - 63
        index += 1
        if value < 0:
            raise GeometryDecodeError("Encoded geometry contains an invalid character")
        result |= (value & 0x1F) << shift
        if value < 0x20:
            break
        shift += 5
        if shift > 60:
            raise GeometryDecodeError("Encoded geometry contains an invalid value")
    delta = ~(result >> 1) if result & 1 else result >> 1
    return delta, index


def _lon_lat(location: Location) -> tuple[float, float]:
    return location.longitude, location.latitude


def _leg_label(leg: TransitLeg) -> str:
    mode = leg.commercial_mode or leg.mode or leg.type.replace("_", " ")
    line = leg.line_code or ""
    return f"{mode} {line}".strip()


def _leg_color(leg: TransitLeg) -> tuple[int, int, int]:
    if leg.type == "public_transport":
        return 37, 99, 235
    if leg.mode == "walking" or leg.type in {"transfer", "street_network", "crow_fly"}:
        return 100, 116, 139
    return 234, 88, 12
