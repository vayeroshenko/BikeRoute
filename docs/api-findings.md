# PRIM API findings — Milestone 1

Status date: 2026-07-30  
Observation status: **required Milestone 1 APIs observed**

`PRIM_API_KEY`, private home/work coordinates, and a candidate-station query
are configured locally. Navitia, bulk disruptions, and Geovelo were observed
live. Optional Stop Monitoring has no configured stop ID. Tracked fixtures
include reviewed, minimized live excerpts; only the optional SIRI shape
remains synthetic.

## Discovery candidates to verify

| Service | Candidate path/method in the probe | Authentication | Requested features | Live status |
|---|---|---|---|---|
| Navitia places | `GET {navitia_base}/places` | Header `apiKey`; `Accept: application/json` | `q`, `type[]=stop_area` | HTTP 200; Bourg-la-Reine resolved to `stop_area:IDFM:70033` |
| Navitia lines | `GET {navitia_base}/lines` | Same | `count=1000`, `start_page`; local exact matching | HTTP 200; 1,470 lines over two pages |
| Navitia journeys | `GET {navitia_base}/journeys` | Same | exact station ID, coordinate destination, `data_freshness=realtime`, `direct_path=none` | HTTP 200; two journeys; mixed section-level realtime/base schedule |
| Navitia bicycle parking | Same journeys path | Same | `first_section_mode[]=bike`, `last_section_mode[]=walking`, `park_mode=on_street`, `max_duration_to_pt=1320` | HTTP 200, but bike/park parameters ignored for tested route |
| Bulk disruptions | `GET {disruptions_url}` | `apiKey` | response timestamps, affected lines/stops/segments | HTTP 200 observed; 998 disruptions; top-level `lastUpdatedDate`, `lines`, `disruptions` |
| Geovelo | `POST /marketplace/computedroutes` | `apiKey`; `Accept: application/json`; JSON content type | query feature flags; waypoints and nested `bikeDetails` body | HTTP 200 with three rich alternatives |
| Stop Monitoring | `GET {stop_monitoring_url}` | Same candidate | `MonitoringRef` | Optional; no stop ID configured, not called |

All candidate URLs are configurable because the report requires the exact
paths and headers to be confirmed in the user's authenticated PRIM playground.
The CLI allowlists response metadata headers: content type, date, validators,
retry timing, and common rate-limit fields.

## Required observations still outstanding

- Confirm the authenticated-playground subscription names associated with each
  successful endpoint.
- Check the relevant Stop Monitoring stop points against actual coverage.
- Re-test `park_mode=on_street` only if PRIM documents a deployment-specific
  syntax; the current syntax was accepted but not applied.

## Deviations from the research report

- On 2026-07-30 at 08:53:00 UTC, authenticated
  `GET {navitia_base}/coverage/fr-idf/lines?q=RER+B` returned HTTP 400 with
  `unknown type: coverage`. The PRIM regional deployment uses implicit
  coverage under `{navitia_base}`; the probe now calls collection paths
  directly.
- The generic Navitia `q` parameter was accepted with HTTP 200 but ignored on
  the lines collection: each query returned the same first 25 of 1,470 lines.
  The probe now fetches two pages with `count=1000` and performs exact local
  matching.
- At 08:54:46 UTC the resolved mappings were RER B primary
  `line:IDFM:C01743`, bus 4602 `line:IDFM:C01697`, bus 4621
  `line:IDFM:C01698`, and bus 4622 `line:IDFM:C01699`. Two additional code-B
  lines are explicitly named replacement RER B services and are not collapsed
  into the primary ID.
- At 09:02:57 UTC the bulk disruptions endpoint returned HTTP 200 with JSON
  content and 998 disruption objects. No rate-limit headers were exposed in
  the response.
- The bulk disruptions schema is not Navitia's nested disruption schema. It
  uses top-level `lastUpdatedDate`, separate `lines` and `disruptions`
  collections, camelCase fields such as `applicationPeriods`, string severity,
  and joins through `impactedObjects[].disruptionIds`.
- At 09:03:07 UTC the Geovelo candidate endpoint returned an HTML Cloudflare
  block page with HTTP 403. Because the response did not originate as the
  API's JSON error schema, it does not establish whether the subscription is
  present. Subsequent documentation review found that the attempted
  `/marketplace/geovelo/v1/routes` path and flat body were wrong. The probe now
  follows the documented upstream `/api/v2/computedroutes` contract: feature
  flags are query parameters, waypoint titles are required, and bicycle
  settings are nested in `bikeDetails`. A retry at 09:29:13 UTC using that
  contract through the inferred PRIM gateway path received the same Cloudflare
  block before API validation. The exact PRIM gateway URL must therefore be
  copied from the logged-in query wizard rather than inferred from the upstream
  Geovelo path.
- The authenticated PRIM query wizard subsequently confirmed that the gateway
  does not preserve the upstream Geovelo path. Its exact endpoint is
  `POST https://prim.iledefrance-mobilites.fr/marketplace/computedroutes`.
  A supplied successful response contained three alternatives
  (`RECOMMENDED`, `SAFER`, `FASTER`), section-level facility distances,
  `verticalGain`, `verticalLoss`, duration, speed, profile, and bike type.
- At 09:40:00 UTC the corrected live probe returned HTTP 200 in about 300 ms
  with `RECOMMENDED`, `SAFER`, and `FASTER` routes. It returned encoded
  geometry, 593–730 elevation points, and 105–130 instruction rows per
  alternative. Geometry and route IDs are redacted because both encode private
  endpoints. The response exposed Cloudflare `cf-ray` and `date` headers but
  no quota headers.
- Elevations and instructions use a header row followed by positional arrays.
  Milestone 2 must decode them against the returned header rather than
  hard-code indexes. Observed facility values include `CYCLEWAY`, `GREENWAY`,
  `LANE`, `LIVINGSTREET`, `FOOTWAY`, `RESIDENTIAL`, `PRIMARY`, `SECONDARY`,
  `TERTIARY`, `NONE`, and `SERVICE`. `SERVICE` was not listed in the report's
  initial grouping and must remain unclassified until explicitly handled.
- At 09:09:47 UTC, places resolved Bourg-la-Reine to
  `stop_area:IDFM:70033`; the station-to-work journey returned HTTP 200 with
  response context `20260730T110947` Europe/Paris. Its first transit section
  was `realtime`, while its second was `base_schedule`, demonstrating that
  realtime coverage must be reported per section.
- At 09:09:47 UTC, the park-mode request returned HTTP 200 and eight journeys,
  but the best journey began with walking, contained no bicycle or `park`
  section, and included both realtime and base-schedule transit. The parameters
  are therefore unsupported or ignored in the tested deployment/route.
- Reviewed live excerpts now cover identifiers, a mixed-freshness journey,
  ignored park mode, the bulk disruption schema, and rich Geovelo
  alternatives. The SIRI structure remains clearly labelled synthetic because
  Stop Monitoring is optional and not configured.
- The probe uses configurable candidate endpoints rather than claiming that
  the report's URLs are deployed paths. Any authenticated-playground
  differences should be recorded here after the first successful run.

## Access failure behavior

Missing `PRIM_API_KEY` stops before creating an HTTP client. HTTP 401, JSON
403, and non-JSON edge/security 403 responses are distinguished; each stores a
sanitized diagnostic fixture and stops the run. Stop Monitoring is skipped by
`probe all` unless a stop ID is configured.

## Exact next implementation step

If Stop Monitoring validation is desired, configure a relevant stop ID and
run:

```bash
idf-commute probe stop-monitoring --config config.yaml
```

Milestone 3 is now implemented and live-smoke-tested. The exact next
implementation step is Milestone 4: persist bicycle location only after route
selection, force return transit to the stored station, and distinguish
retrieval, detour, and explicitly leave-behind recovery options. Do not add UI
code before bicycle-state and return scenario tests pass.
