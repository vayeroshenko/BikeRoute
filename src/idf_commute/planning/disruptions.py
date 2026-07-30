from __future__ import annotations

from idf_commute.domain.models import Disruption, TimeInterval, TransitJourney


def match_journey_disruptions(
    journey: TransitJourney,
    disruptions: list[Disruption],
) -> tuple[Disruption, ...]:
    journey_interval = TimeInterval(begin=journey.departure, end=journey.arrival)
    line_ids = {leg.line_id for leg in journey.legs if leg.line_id}
    stop_ids = {
        identifier
        for leg in journey.legs
        for identifier in (leg.origin_id, leg.destination_id)
        if identifier
    }
    matches: list[Disruption] = []
    for disruption in disruptions:
        if disruption.application_periods and not any(
            period.intersects(journey_interval) for period in disruption.application_periods
        ):
            continue
        line_match = bool(line_ids & disruption.affected_line_ids)
        stop_match = bool(
            stop_ids & (disruption.affected_stop_area_ids | disruption.affected_stop_point_ids)
        )
        if line_match or stop_match:
            matches.append(disruption)
    return tuple(matches)


def disruption_penalty_minutes(disruptions: tuple[Disruption, ...]) -> float:
    return sum(_single_disruption_penalty(disruption) for disruption in disruptions)


def _single_disruption_penalty(disruption: Disruption) -> float:
    indicators = " ".join(
        value
        for value in (
            disruption.effect,
            disruption.severity,
            disruption.cause,
        )
        if value
    ).upper()
    if any(value in indicators for value in ("NO_SERVICE", "CANCEL", "INTERROMPU")):
        return 30
    if any(value in indicators for value in ("SIGNIFICANT_DELAYS", "FORTEMENT")):
        return 20
    if any(value in indicators for value in ("REDUCED_SERVICE", "PERTURB")):
        return 12
    return 10
