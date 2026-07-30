# Milestone 3 — outbound planner

Milestone 3 was completed and live-smoke-tested on 2026-07-30.

## Implemented behavior

- Resolve configured station queries to stable Navitia stop-area IDs and public
  station coordinates.
- Request Geovelo alternatives from home to each exact candidate station.
- Reject every bicycle route above the configured hard maximum.
- Apply the report's smooth penalty above the preferred bicycle duration.
- Add a parking/entry buffer before requesting station-to-work transit.
- Reject a returned transit journey if it departs before the bicycle and
  parking legs can finish.
- Fetch disruptions once, match them by active interval and stable line/stop
  IDs, and retain their score penalty.
- Always request an all-transit baseline.
- Deduplicate materially identical options, rank them by a printed score
  breakdown, and show explicit rejection reasons.

The planner constructs the bicycle and transit requests separately. It cannot
place a bicycle after transit or assume that the bicycle travels on the train.

## Scenario coverage

Offline tests cover 19-, 22-, and 60-minute bicycle routes with a 20-minute
preference and 25-minute hard maximum. The 19-minute route has no bicycle
penalty, the 22-minute route remains eligible with a finite penalty, and the
60-minute route is rejected. Tests also cover the exact station origin,
parking-adjusted request times, baseline retention, active disruption matching,
and deduplication.

## Live smoke test

A live request for 2026-07-30 12:30 Europe/Paris completed successfully. The
configured Bourg-la-Reine candidate resolved to `stop_area:IDFM:70033`.
Geovelo returned three alternatives, but all exceeded the configured 25-minute
hard limit, so the planner correctly displayed only the all-transit baseline
and three rejections. No threshold was silently relaxed.

This result means the implementation works, but the current candidate
shortlist/configuration does not produce an eligible bicycle option under the
current hard limit. Add closer RER B candidates or intentionally change the
local maximum after reviewing their measured durations.

## Exact next step

Milestone 4 should persist bicycle location only after the user selects an
outbound route, then force return transit to that exact station. It must not
silently choose a different station where the bicycle is absent.
