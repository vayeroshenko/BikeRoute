from __future__ import annotations

import pytest

from idf_commute.domain.models import BikeRoute, Location, TransitLeg
from idf_commute.presentation.map import (
    GeometryDecodeError,
    build_route_map,
    decode_polyline,
)


def test_rejects_decoded_polyline_outside_ile_de_france() -> None:
    with pytest.raises(GeometryDecodeError, match="outside Île-de-France"):
        decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")


def test_decodes_ile_de_france_polyline() -> None:
    points = decode_polyline("_gzhH_f`M?o}@o}@?")

    assert [(point.latitude, point.longitude) for point in points] == [
        (48.8, 2.3),
        (48.8, 2.31),
        (48.81, 2.31),
    ]


def test_rejects_truncated_polyline() -> None:
    with pytest.raises(GeometryDecodeError, match="ended unexpectedly"):
        decode_polyline("g")


def test_builds_exact_and_schematic_map_paths() -> None:
    bike = BikeRoute(
        title="FASTER",
        duration_seconds=600,
        distance_m=3000,
        encoded_geometry="_gzhH_f`M?o}@",
    )
    exact = TransitLeg(
        type="public_transport",
        commercial_mode="RER",
        line_code="B",
        duration_seconds=300,
        geometry=(
            Location(latitude=48.8, longitude=2.31),
            Location(latitude=48.81, longitude=2.32),
        ),
    )
    schematic = TransitLeg(
        type="street_network",
        mode="walking",
        duration_seconds=120,
        origin_location=Location(latitude=48.81, longitude=2.32),
        destination_location=Location(latitude=48.812, longitude=2.322),
    )

    route_map = build_route_map(bike, (exact, schematic))

    assert [path.label for path in route_map.paths] == [
        "Bike · FASTER",
        "RER B",
        "walking",
    ]
    assert route_map.paths[0].coordinates[0] == (2.3, 48.8)
    assert route_map.paths[1].schematic is False
    assert route_map.paths[2].schematic is True
    assert route_map.uses_schematic_segments is True
