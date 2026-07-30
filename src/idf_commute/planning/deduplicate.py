from __future__ import annotations

from idf_commute.domain.models import OutboundOption


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
