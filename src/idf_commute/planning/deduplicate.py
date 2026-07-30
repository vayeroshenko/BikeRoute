from __future__ import annotations

from idf_commute.domain.models import OutboundOption, OutboundOptionKind


def deduplicate_outbound_options(
    options: list[OutboundOption],
    *,
    limit: int,
) -> list[OutboundOption]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    unique: list[OutboundOption] = []
    seen: set[tuple[object, ...]] = set()
    for option in options:
        signature = outbound_option_signature(option)
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(option)
        if len(unique) == limit:
            break
    return unique


def select_diverse_outbound_options(
    options: list[OutboundOption],
    *,
    limit: int,
) -> list[OutboundOption]:
    """Keep score order while reserving up to 60% for transit-only alternatives."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    ranked = sorted(options, key=lambda option: option.score.total_minutes)
    unique = deduplicate_outbound_options(ranked, limit=max(len(ranked), 1))
    transit_reserve = min(6, max(1, (limit * 3 + 4) // 5))
    selected = [
        option
        for option in unique
        if option.kind is OutboundOptionKind.ALL_TRANSIT
    ][:transit_reserve]
    selected_ids = {id(option) for option in selected}
    for option in unique:
        if len(selected) == limit:
            break
        if id(option) not in selected_ids:
            selected.append(option)
            selected_ids.add(id(option))
    return sorted(selected, key=lambda option: option.score.total_minutes)


def outbound_option_signature(option: OutboundOption) -> tuple[object, ...]:
    transit_signature = tuple(
        (
            leg.type,
            leg.mode,
            leg.line_id,
            leg.origin_id,
            leg.destination_id,
        )
        for leg in option.transit_journey.legs
    )
    return (
        option.kind,
        option.station.id if option.station else None,
        option.bike_route.title if option.bike_route else None,
        transit_signature,
    )
