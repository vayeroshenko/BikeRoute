# Milestone 2 — normalized provider adapters

Milestone 2 was completed on 2026-07-30. Raw provider dictionaries are decoded
inside `idf_commute.providers`; application code consumes the Pydantic models
in `idf_commute.domain`.

## Implemented boundaries

| Boundary | Adapter | Important observed behavior |
|---|---|---|
| Transit journeys | `NavitiaAdapter` | Times are timezone-aware in Europe/Paris. Freshness remains section-level because one journey can mix realtime and base schedule. |
| Bicycle routes | `GeoveloAdapter` | Three alternatives are normalized. Elevation and instruction rows are decoded using the response's header row rather than fixed indexes. |
| Traffic information | `BulkDisruptionAdapter` | The separate `lines` and `disruptions` collections are joined through `impactedObjects[].disruptionIds`. |
| Departures | `SiriStopMonitoringAdapter` | Expected calls are realtime; aimed-only calls are schedule-based. This adapter is optional and currently tested with a synthetic sanitized fixture. |

The shared `PrimClient` provides authentication, bounded retries for network
errors/429/5xx, `Retry-After` handling, typed access errors, response metadata,
quota-header capture, and optional JSON cache hooks. It does not log request
query strings, tokens, or payloads.

## Privacy boundary

Live fixtures are ignored. Reviewed tracked fixtures remove API tokens,
private coordinate values, Geovelo route IDs, encoded geometry, and road names
that could reconstruct the private route. Stable public transit identifiers
remain intact for contract tests.

## Deliberately not implemented

- candidate-station selection;
- bike-plus-transit orchestration;
- scoring and threshold enforcement;
- bicycle state persistence;
- planner CLI or UI;
- maps and elevation presentation.

Those begin in Milestone 3. The first implementation slice should combine a
configured candidate station's Geovelo route with a Navitia journey from that
exact station, reject bicycle routes over the hard threshold, and retain the
20-minute soft preference as an explicit score component.
