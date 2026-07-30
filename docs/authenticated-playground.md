# Authenticated PRIM playground checklist

1. Sign in to PRIM and subscribe to Navitia generic access v2,
   traffic/disruptions, Geovelo, and optionally Stop Monitoring.
2. Generate a token. Store it only as `PRIM_API_KEY` in the ignored `.env`
   file or process environment.
3. Copy `config.example.yaml` to the ignored `config.yaml`. Add private
   home/work coordinates, candidate station names, and—when known—stable IDs.
4. In each subscribed API's authenticated playground, make one minimal request.
   Compare its path, authentication header, method, and body shape with
   `docs/api-findings.md`. Override endpoint URLs/header using the environment
   variables documented in `.env.example` when the deployment differs.
5. Run `idf-commute probe all --config config.yaml`. A 401 or 403 stops the run
   and saves the sanitized error response for diagnosis.
6. Review every file in `tests/fixtures/live/`. Only after review, copy a
   representative capture into the corresponding tracked fixture directory
   and replace its synthetic fixture.

Do not paste a token into a command line, URL, issue, fixture, or terminal
transcript. The CLI deliberately prints endpoint paths without query strings.
`PRIMtoken.txt` is ignored defensively, but `.env` or the process environment
is the supported configuration mechanism.
