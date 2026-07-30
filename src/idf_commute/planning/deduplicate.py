from __future__ import annotations

from idf_commute.domain.models import (
    OutboundOption,
    OutboundOptionKind,
    TransitLeg,
)


def deduplicate_outbound_options(
    options: list[OutboundOption],
    *,
    limit: int,
) -> list[OutboundOption]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    grouped: dict[tuple[object, ...], OutboundOption] = {}
    for option in sorted(options, key=lambda candidate: candidate.score.total_minutes):
        signature = outbound_option_signature(option)
        fastest = grouped.get(signature)
        grouped[signature] = (
            option if fastest is None else _merge_equivalent_line_codes(fastest, option)
        )
    return list(grouped.values())[:limit]


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
            _transport_kind(leg),
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


def _transport_kind(leg: TransitLeg) -> str | None:
    if leg.type != "public_transport":
        return None
    return leg.commercial_mode.casefold() if leg.commercial_mode else leg.line_id


def _merge_equivalent_line_codes(
    fastest: OutboundOption,
    alternative: OutboundOption,
) -> OutboundOption:
    merged_legs = tuple(
        _merge_leg_line_codes(fastest_leg, alternative_leg)
        for fastest_leg, alternative_leg in zip(
            fastest.transit_journey.legs,
            alternative.transit_journey.legs,
            strict=True,
        )
    )
    journey = fastest.transit_journey.model_copy(update={"legs": merged_legs})
    return fastest.model_copy(update={"transit_journey": journey})


def _merge_leg_line_codes(fastest: TransitLeg, alternative: TransitLeg) -> TransitLeg:
    if fastest.type != "public_transport":
        return fastest
    codes = {
        code
        for code in (
            *fastest.equivalent_line_codes,
            fastest.line_code,
            *alternative.equivalent_line_codes,
            alternative.line_code,
        )
        if code
    }
    return fastest.model_copy(update={"equivalent_line_codes": tuple(sorted(codes))})
