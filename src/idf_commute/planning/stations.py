from __future__ import annotations

import unicodedata
from collections import deque
from collections.abc import Iterable
from itertools import pairwise

from idf_commute.domain.models import Station

# RER B is a tree with two northern and two southern branches. Keeping the
# topology here makes configured ranges deterministic and avoids extra PRIM
# requests merely to decide which stations should be queried.
_RER_B_SEGMENTS = (
    (
        "Aéroport Charles de Gaulle 2 TGV",
        "Aéroport Charles de Gaulle 1",
        "Parc des Expositions",
        "Villepinte",
        "Sevran Beaudottes",
        "Aulnay-sous-Bois",
    ),
    (
        "Mitry-Claye",
        "Villeparisis - Mitry-le-Neuf",
        "Vert-Galant",
        "Sevran-Livry",
        "Aulnay-sous-Bois",
    ),
    (
        "Aulnay-sous-Bois",
        "Le Blanc-Mesnil",
        "Drancy",
        "Le Bourget",
        "La Courneuve - Aubervilliers",
        "La Plaine - Stade de France",
        "Gare du Nord",
        "Châtelet - Les Halles",
        "Saint-Michel - Notre-Dame",
        "Luxembourg",
        "Port-Royal",
        "Denfert-Rochereau",
        "Cité Universitaire",
        "Gentilly",
        "Laplace",
        "Arcueil - Cachan",
        "Bagneux",
        "Bourg-la-Reine",
    ),
    (
        "Bourg-la-Reine",
        "Sceaux",
        "Fontenay-aux-Roses",
        "Robinson",
    ),
    (
        "Bourg-la-Reine",
        "Parc de Sceaux",
        "La Croix de Berny",
        "Antony",
        "Fontaine-Michalon",
        "Les Baconnets",
        "Massy - Verrières",
        "Massy - Palaiseau",
        "Palaiseau",
        "Palaiseau - Villebon",
        "Lozère",
        "Le Guichet",
        "Orsay-Ville",
        "Bures-sur-Yvette",
        "La Hacquinière",
        "Gif-sur-Yvette",
        "Courcelle-sur-Yvette",
        "Saint-Rémy-lès-Chevreuse",
    ),
)


def rer_b_stations_in_ranges(
    stations: list[Station],
    ranges: Iterable[tuple[str, str]],
) -> list[Station]:
    """Return the union of RER B graph paths, deduplicated by stop-area ID."""
    graph = _rer_b_graph()
    station_nodes = {
        node
        for station in stations
        if (node := _resolve_topology_station(station.name, graph, required=False)) is not None
    }
    selected_nodes: set[str] = set()
    for start, end in ranges:
        start_node = _resolve_topology_station(start, graph)
        end_node = _resolve_topology_station(end, graph)
        assert start_node is not None
        assert end_node is not None
        if start_node not in station_nodes:
            raise ValueError(f"RER B range start {start!r} was not returned by PRIM")
        if end_node not in station_nodes:
            raise ValueError(f"RER B range end {end!r} was not returned by PRIM")
        selected_nodes.update(_shortest_path(graph, start_node, end_node))

    selected: list[Station] = []
    seen_ids: set[str] = set()
    for station in stations:
        node = _resolve_topology_station(station.name, graph, required=False)
        if node in selected_nodes and station.id not in seen_ids:
            selected.append(station)
            seen_ids.add(station.id)
    if not selected:
        raise ValueError("Configured RER B ranges did not match any returned stations")
    return selected


def parse_station_range(value: str) -> tuple[str, str]:
    """Parse the CLI START..END notation without conflicting with Navitia IDs."""
    parts = [part.strip() for part in value.split("..")]
    if len(parts) != 2 or not all(parts):
        raise ValueError(
            f"Invalid station range {value!r}; expected START..END, for example Laplace..Sceaux"
        )
    return parts[0], parts[1]


def _rer_b_graph() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for segment in _RER_B_SEGMENTS:
        nodes = [_station_key(name) for name in segment]
        for node in nodes:
            graph.setdefault(node, set())
        for left, right in pairwise(nodes):
            graph[left].add(right)
            graph[right].add(left)
    return graph


def _resolve_topology_station(
    value: str,
    graph: dict[str, set[str]],
    *,
    required: bool = True,
) -> str | None:
    key = _station_key(value)
    if key in graph:
        return key
    partial = [node for node in graph if key in node or node in key]
    if len(partial) == 1:
        return partial[0]
    if not required:
        return None
    if partial:
        raise ValueError(f"RER B station {value!r} is ambiguous")
    raise ValueError(f"RER B station {value!r} is not present in the range topology")


def _shortest_path(
    graph: dict[str, set[str]],
    start: str,
    end: str,
) -> tuple[str, ...]:
    queue: deque[tuple[str, tuple[str, ...]]] = deque([(start, (start,))])
    visited = {start}
    while queue:
        node, path = queue.popleft()
        if node == end:
            return path
        for neighbor in graph[node]:
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, (*path, neighbor)))
    raise ValueError("RER B station range endpoints are not connected")


def _station_key(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in decomposed if character.isalnum())
