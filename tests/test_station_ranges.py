from __future__ import annotations

import pytest

from idf_commute.domain.models import Location, Station
from idf_commute.planning.stations import (
    parse_station_range,
    rer_b_stations_in_ranges,
)


def _station(identifier: str, name: str) -> Station:
    return Station(
        id=identifier,
        name=name,
        location=Location(latitude=48.8, longitude=2.3),
        line_ids=("line:IDFM:C01743",),
    )


def test_multiple_rer_b_ranges_are_merged_and_deduplicated() -> None:
    stations = [
        _station("laplace", "Laplace"),
        _station("arcueil", "Arcueil-Cachan"),
        _station("bagneux", "Bagneux"),
        _station("blr", "Bourg-la-Reine"),
        _station("blr", "Bourg-la-Reine"),
        _station("sceaux", "Sceaux"),
        _station("fontenay", "Fontenay-aux-Roses"),
        _station("parc", "Parc de Sceaux"),
    ]

    selected = rer_b_stations_in_ranges(
        stations,
        [
            ("Laplace", "Bourg-la-Reine"),
            ("Bourg-la-Reine", "Sceaux"),
        ],
    )

    assert [station.id for station in selected] == [
        "laplace",
        "arcueil",
        "bagneux",
        "blr",
        "sceaux",
    ]


def test_range_can_connect_stations_on_different_southern_branches() -> None:
    stations = [
        _station("sceaux", "Sceaux"),
        _station("blr", "Bourg-la-Reine"),
        _station("parc", "Parc de Sceaux"),
        _station("antony", "Antony"),
    ]

    selected = rer_b_stations_in_ranges(stations, [("Sceaux", "Antony")])

    assert [station.id for station in selected] == [
        "sceaux",
        "blr",
        "parc",
        "antony",
    ]


def test_station_range_cli_notation_is_validated() -> None:
    assert parse_station_range(" Laplace .. Sceaux ") == ("Laplace", "Sceaux")
    with pytest.raises(ValueError, match=r"START\.\.END"):
        parse_station_range("Laplace:Sceaux")
