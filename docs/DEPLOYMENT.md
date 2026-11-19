# Deployment runbook

## Authoritative production architecture

Use Docker Engine with Compose v2 and `docker-compose.prod.yml`. API, PostGIS and Redis run
on a private Compose network. Only the API port is published. The one-shot `init` service
applies schema, checksum-tracked migrations and initial source metadata before API/ETL start.
A successful health check means PostgreSQL and Redis are reachable; it does **not** certify
that all source data was imported. Use the import exit status and database checks below.

Native VM installation is an alternative: see [VM_SETUP.md](VM_SETUP.md). Do not combine its
host-side import/start commands with production Compose, whose DB is not host-accessible.

## Fresh installation

1. Clone the repository and enter its directory.
2. Copy `.env.example` to `.env`; `chmod 600 .env`. Never commit it.
3. Set **separate**, randomly generated `DB_PASSWORD`, `REDIS_PASSWORD`, `API_KEY_PEPPER`,
   and `INITIAL_ADMIN_KEY` (at least 32 characters). Generate each with `openssl rand -hex 32`.
4. Set `ALLOWED_ORIGINS` to explicit browser origins, for example `https://yourdomain.com`
   (no wildcard). Set `API_PORT` to an unused port, e.g. 13000. `PORT` is the internal port;
   Compose deliberately uses 3000 internally. Set `GUNICORN_WORKERS` to size worker count.
5. Start and import:

```bash
make docker-up
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs init
make docker-etl
```

The default ETL target accepts non-commercial licence consent; review the source licences
before running it. Use `make docker-etl ETL_ARGS='--all-confirmed --commercial-only'` if required.
Middlesex municipal data is **blocked by operator decision**, not merely filtered by commercial-use
flags. Both the 2022 snapshot and newer county feed lack a verified permissive grant. Default setup
seeds 36 confirmed sources (33 municipal, NAR and two boundary datasets) and uses NAR for
Middlesex when NAR is available. Migration 38 restores the 30 municipal definitions omitted
from older seeds and repairs known stale defaults for Windsor, Sarnia, London and Elgin while
preserving custom endpoints/licence fields, operator exclusions and audit state.
Bruce County remains blocked too. Recovered endpoints come from `arcgis_downloader.STANDARD_SOURCES`,
with explicit mapping aliases and canonical inventory keys (for example `brantford` maps to
`brantford_addresses`). Seeding is not importing: confirm each upstream endpoint/schema and
review the per-source import summary. Historical commercial-use approvals are retained;
outstanding bespoke attribution reviews and Frontenac licence archival requirements remain
recorded in `license_notes`, not replaced with invented attribution. Windsor, Sarnia, London and
Elgin licence evidence is archived in [`licences/README.md`](licences/README.md). London's credit
is optional; redistribution must still include its terms/URL and bind recipients. Haldimand's
configured endpoint remains reachable but serves parcel polygons: its first vertex is not a
verified address location. Migration 39 and the fresh seed block Haldimand until an approved
parcel-coordinate policy is implemented; `--all-confirmed` skips it, and explicit orchestrated
requests/direct imports reject it. Its endpoint, mapping and licence metadata are retained. Migration 40 also retires
previously imported Haldimand rows and their SQL cache entries; response-cache namespace
v6 prevents reuse of older Redis matches. `--force`, `--accept-non-commercial`,
and direct municipal importer calls cannot override the block. Migration 37 retires previously
imported Middlesex municipal rows; a fresh seed also starts blocked.

Do not run `make dev` afterward: the API is already running. Allow disk space for raw downloads,
PostGIS indexes and import WAL (tens of GB), and time for millions of NAR rows. Keep the downloaded
files until validation completes. No Google API key or paid geocoding service is needed.

## Authentication and bootstrap

`INITIAL_ADMIN_KEY` creates the first admin key at API startup only when no active admin exists.
The API uses the same pepper/HMAC scheme for bootstrapped and API-created keys. Startup is
idempotent across workers. Store this key securely, create normal least-privilege consumer keys,
then remove `INITIAL_ADMIN_KEY` from `.env` and recreate the API. Removing the env value does
not revoke the database key; revoke it via the admin API when it is no longer needed.

If you did not set an initial key, the included recovery/bootstrap script runs inside the API:

```bash
docker compose -f docker-compose.prod.yml exec api \
  python api/create_admin_key.py --name 'Local administrator' --owner 'operator'
```

It prints a secret **once**. Do not paste that output in logs or issues. The script is a
privileged operator action, not a public endpoint. In a shell set `API_KEY` to your admin key
without committing it (e.g. `read -rs API_KEY; export API_KEY`). Set `BASE_URL` for your port:

```bash
export BASE_URL=http://localhost:13000
curl "$BASE_URL/v1/health"
curl -H "X-API-Key: $API_KEY" "$BASE_URL/v1/stats"
curl -H "X-API-Key: $API_KEY" "$BASE_URL/v1/attributions"
curl -X POST "$BASE_URL/v1/admin/keys" \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"name":"website","permissions":{"geocode":true,"batch":true,"stats":false,"admin":false}}'
```

Store the returned consumer key; it cannot be retrieved later. Admin keys support GET/POST
`/v1/admin/keys` and GET/PATCH/DELETE `/v1/admin/keys/{id}`. DELETE revokes the key.

## API verification

See [manual tests](../api/docs/APIMANUALTESTS.md) for commands using authentication.
Canonical endpoints:

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/health` | Public DB/Redis health |
| POST | `/v1/geocode` | Single match; 404 on no match |
| POST | `/v1/geocode/batch` | 1–1000 addresses; per-item success flags |
| GET | `/v1/stats` | Counts and runtime metrics |
| GET | `/v1/attributions` | Source licence attribution |
| GET | `/docs`, `/openapi.json` | Public API schema/UI; operations require a key |

Swagger's **Authorize** accepts `X-API-Key`; schema access never grants access to protected
operations. Malformed JSON is 400; well-formed but invalid requests are 422. All errors use
`{"error":{"code":"...","message":"..."}}`. Unversioned API routes remain deprecated aliases.

```bash
curl -X POST "$BASE_URL/v1/geocode" -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"Address":{"StreetNumber":"51","StreetName":"Cork","StreetSuffix":"Street","StreetDirection":"West","City":"Guelph","Province":"ON","PostalCode":"N1H 2X3"}}'
```

Check the matched address/direction, source, coordinates and attribution—not just HTTP 200.
Repeat to verify `cached=true`. A healthy empty deployment cannot geocode real addresses.
Hamilton supplies separate components rather than a complete label: the API formats the matched
row (for example `681 Hall Road, Hamilton, ON L0R 1C0`). Stored `full_address` may legitimately
remain NULL; no reimport/backfill is needed for this display fallback. Unit/civic suffixes are
preserved and labels are never synthesized from possibly misspelled request text. Unit
selection compares the actual database fields (`properties.unit_no`, `nar_addresses.apt_no_label`),
not an unrelated civic suffix, and unit-specific requests have separate response-cache keys.

NAR scores have a hard 0.90 ceiling, including geographic boosts and cached/batch responses.
`confidence_factors.base_confidence` is the matcher score before geography; the breakdown also
reports adjustment, unclamped score, tier cap and final score. See
[NAR_QUERY_CONFIDENCE_RATINGS.md](NAR_QUERY_CONFIDENCE_RATINGS.md); these are heuristic scores,
not measured probabilities of coordinate accuracy.

## Imports and recovery

The ETL image's entrypoint is `python -m`; pass a **module name**, not `python -m` again.

```bash
make docker-etl ETL_ARGS='--list'
make docker-etl ETL_ARGS='--source guelph_addresses'
make docker-etl ETL_ARGS='--all-confirmed --import-only --accept-non-commercial'
# Force re-download AND re-import (e.g. after mapping changes):
make docker-etl ETL_ARGS='--source guelph_addresses --force'
# Or force import from retained files without downloading:
make docker-etl ETL_ARGS='--source guelph_addresses --import-only --force'
```

### Headless NAR access

The downloader uses ordinary HTTP, streams the ZIP to disk and extracts only Ontario CSVs
with bounded memory. It requires neither a browser nor a paid PUMF subscription. However,
**unattended access is not guaranteed**: Statistics Canada can challenge the release page
and the archive itself. Both the June 2026 ZIP and current PDF returned HTTP 403 with
`cf-mitigated: challenge` during verification. The PDF is documentation, not address data.
The former public NAR API was discontinued with the GC API Store in September 2023
([official catalogue notice](https://open.canada.ca/data/en/dataset/849db3e4-6e26-462a-b7dd-db0e6c8325fe)).

A Cloudflare challenge is a nonzero setup failure with actionable diagnostics; the downloader
does not retry indefinitely, spoof browsers or install a challenge solver. For reliable headless
operation, obtain an approved bulk-download route/allowlisting from Statistics Canada, or
stage an authorized release archive through your deployment process. Open-data reuse permission
does not guarantee unrestricted access to a particular server.

Optional `.env` settings, forwarded to the Compose ETL service (set **only one**):

```dotenv
# Explicit authorized HTTPS ZIP URL; skips discovery, does not bypass access controls.
# Pin and maintain the release deliberately; this does not track the latest release.
NAR_DOWNLOAD_URL=https://www150.statcan.gc.ca/n1/pub/46-26-0002/2022001/202606.zip
```

If the archive URL is also challenged, an operator-supplied archive avoids network access:

```dotenv
# Place release.zip under data_sourcing/data_sources/nar_ontario/ on the host.
NAR_ARCHIVE_PATH=/app/data_sourcing/data_sources/nar_ontario/release.zip
```

Then run `make docker-etl ETL_ARGS='--source nar_ontario --commercial-only'`.
Native deployments use the same variables with native filesystem paths. Existing extracted
Ontario CSVs can instead be imported using `--source nar_ontario --import-only`.
Allow disk for the national ZIP, Ontario CSVs, PostGIS indexes and WAL; low RAM does not
eliminate these disk requirements. Extraction validates all selected files in staging before
replacing retained Ontario CSVs, and removes obsolete part files only after validation.
Do not treat municipal-only operation as complete province-wide coverage.

NAR normally downloads automatically. `--skip-manual` only skips sources explicitly marked manual in
the metadata; it does not skip NAR. Setup processes sources in dependency order. Failures are
reported with a nonzero exit status; noninteractive runs do not prompt on failure. Inspect the
summary before rerunning failed sources. Do not treat partial imports as complete coverage.
The import audit stores file-level results and row diagnostics; skipped files report zero current work.
Municipal imports quarantine explicitly recognized validation defects (such as missing or invalid
coordinates) as `validation_quarantine` in `import_row_errors`, retaining the source feature,
property ID and reason. These increment `rows_skipped`, not `rows_failed`. Successful imports
with quarantines report **COMPLETED WITH WARNINGS**; `import_history.status` stays `completed`
for compatibility, while `metadata.outcome` is `completed_with_warnings` and
`metadata.rows_quarantined` records the separate count. Identical-file skips retain a warning.
Unexpected transformer exceptions, critical mapping/schema loss, corrupt input and SQL/connection
failures still fail setup; they are not treated as acceptable source dirtiness. Reference-setup
failures and exhausted FSA/municipality lookup retries are fatal; a successful spatial query with
no match remains a warning.

Two independent safety gates run before municipal import completion or stale retirement:
`IMPORT_MIN_ACCEPT_RATIO` (default **0.99**) requires accepted / eligible input rows to be at
least 99%, including on fresh imports. `STALE_DELETE_MIN_RATIO` (default **0.90**) requires at
least 90% of the previously active source IDs to be present. Both settings must be finite values
in `(0, 1]`; reducing the deletion threshold does not relax quality validation. Quarantined stable
IDs count as present, but not accepted, and preserve their previous good database rows. If a
rejected row has only a generated coordinate/row-number ID, stale retirement is suppressed for
the entire import. Completeness still applies in `report_only`/`skip` modes. Batches commit
individually: a failed gate can leave newly imported good rows, but cannot trigger stale retirement.
Hastings continues preferring its published LAT/LONG attributes; invalid pairs are quarantined,
not recovered from geometry. Use `--import-only --force` to re-evaluate retained files after
changing validation policy; unchanged-file skips do not reprocess rows.

One explicit exclusion policy applies to published point layers: a feature with **neither a civic
number nor a full address** is not an address record. It is audited as `not_address_record`, counted
in `rows_skipped`, excluded from the quality denominator, and never geocoded. This is distinct
from a validation quarantine or processing failure. An import with no accepted rows still fails.
The historical Middlesex test import contained four such non-address features. That entire municipal dataset is now excluded, not just those four
features; its import history is retained for audit.

```bash
docker compose -f docker-compose.prod.yml exec postgres psql -U geocoder -d geocoder -c \
  "SELECT source_dataset, count(*) FROM properties WHERE NOT is_deleted GROUP BY 1;"
docker compose -f docker-compose.prod.yml exec postgres psql -U geocoder -d geocoder -c \
  "SELECT source_key,status,rows_processed,rows_failed,error_message FROM import_history ORDER BY id DESC LIMIT 20;"
```

Substitute your configured DB user/name if not `geocoder`. Boundary tables must be nonempty;
NAR coordinate coverage is less than 100% where upstream coordinates are absent. Setup performs
NAR coordinate/FSA enrichment after importing all parts. Standalone repair tools are also available:

```bash
docker compose -f docker-compose.prod.yml run --rm etl data_sourcing.enrichment.add_latlong_to_nar --db geocoder
docker compose -f docker-compose.prod.yml run --rm etl data_sourcing.enrichment.add_fsa_to_nar --db geocoder
```

ETL subprocesses use the image's locked Python environment, not ephemeral `uv run` script
environments. Rebuild images after code/dependency changes. Source download access is still required;
package registry access is not required at ETL runtime.

## Verified upstream sources and mappings

- FSA: Statistics Canada's official **2021 Digital_boundary_files/MapServer/14** on
  `geo.statcan.gc.ca`, the same digital boundary product as `lfsa000a21a_e.zip`. The ZIP host
  can require browser security verification; the public query service is a separate official
  distribution, not an unofficial mirror. Detailed polygons use small pages, no simplification,
  and exact returned-count validation before replacing a downloaded file.
- Municipal boundaries: Ontario LIO `LIO_Open03/MapServer/14`. GeoJSON retains full field names;
  the importer maps those names explicitly and repairs invalid geometry before spatial joins.
- Hamilton: official `Addresses/FeatureServer/1`, using NUMBER_COMPLETE and split street fields.
- Middlesex: **NAR fallback only; municipal source blocked**. Neither the 2022 snapshot nor the
  newer public-map feed is imported or used for matching. Endpoints/mappings remain recorded for
  research, not as enabled inputs: public accessibility does not establish reuse permission.
  In the June 2026 NAR release, 21,792 of
  33,106 rows across the eight county municipalities have coordinates (~65.8%, excluding London).
  This is a deliberate coverage tradeoff: missing coordinates are not invented, and unmatched
  requests may return 404.
- Guelph: upstream ADDID is the stable **address-level** identifier. PIN is a parcel identifier
  and must not be used to collapse multiple addresses or units into a single row.

The exact endpoints are versioned in `database/seed_data_sources.sql` and migration 35;
migration 36 corrects the Middlesex licence metadata; migration 37 blocks it and soft-deletes
its municipal records. Audit history and NAR rows are preserved. The current API response-cache
namespace is **v6**, superseding v3/v4/v5: old municipal hits, missing Hamilton labels and pre-fix NAR
scores cannot be served from old caches. Address cache keys include the requested unit, preventing
one unit's matched label from being reused for another. Authentication and rate-limit state are not flushed.
Rebuild/recreate the API to deploy the cache policy and apply migration 40 to retire existing
Haldimand imports. Base-source seed inserts fill missing rows without resetting existing
operator endpoints, attribution, download settings or audit state.
Do not reinitialize from the historical markdown source lists.

## Migrations and upgrades

Back up before upgrades. New Compose installs run the Python migration runner, not raw
`migrations.sql` in Postgres init. Each migration and its checksum commit atomically under a
migration advisory lock. Previously verified migration SQL must not be edited; append migrations.

```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml exec api python database/run_migrations.py --status
docker compose -f docker-compose.prod.yml exec api python database/run_migrations.py --dry-run
```

The init service uses `--replay-unverified` for older raw-SQL installations: NULL-checksum
migrations are actually replayed before a checksum is recorded, **not** silently blessed as verified.
A changed non-NULL checksum always stops initialization. Repeated runs are idempotent. The API
and ETL wait for successful initialization. A failed init requires log inspection, not disabling
its dependency gate. Native host users can run `uv run python database/run_migrations.py
--initialize --replay-unverified` after creating the database (back up first).

### Existing Guelph ID migration (not needed on fresh installs)

Back up first. Force a full download/import using the new ADDID mapping. The stale-delete safety
guard may stop retirement because old parcel IDs coexist with the new address IDs. Do **not**
reduce the global deletion threshold. The reconciliation tool requires the file count to match
verified download metadata, its hash to match the latest fully processed import, and every new
ID/address to be present before it will retire any legacy IDs:

```bash
make docker-etl ETL_ARGS='--source guelph_addresses --force'
# If the only failure was the expected stale-ID guard:
docker compose -f docker-compose.prod.yml run --rm etl data_sourcing.management.reconcile_property_ids \
  --source guelph --file data_sourcing/data_sources/guelph_addresses/guelph_addresses.geojson
# Review the dry-run counts, then repeat with --apply.
docker compose -f docker-compose.prod.yml run --rm etl data_sourcing.management.reconcile_property_ids \
  --source guelph --file data_sourcing/data_sources/guelph_addresses/guelph_addresses.geojson --apply
make docker-etl ETL_ARGS='--source guelph_addresses --import-only --force'
```

Old IDs are soft-deleted and excluded from matching/PIN lookup/stats, not erased from the audit.
Fresh installs directly create 55,908 address IDs for the tested source snapshot and need no repair.

## Live regression gate

After imports, run the checked-in 46-check live probe with an **admin** key. It tests real middleware,
single/batch parity, the README coordinates/direction, Hamilton labels and distinct unit identities, Middlesex exclusion,
NAR scores/breakdowns (including an exact postal match), caching, CORS, key revocation, rate limiting
and proxy spoof denial. Temporary keys are revoked even if a check fails.
For shell use, `API_KEY` must be exported in the caller's environment:

```bash
docker compose -f docker-compose.prod.yml exec -e API_KEY api python -m api.scripts.verify_deployment
```

Do not disable authentication to run the probe. When testing a second DB on the same Redis server,
use a separate Redis logical database (e.g. `REDIS_URL=redis://redis:6379/1`) so cached geocodes and
API-key caches from the first DB cannot mask failures. If a source is intentionally not imported,
the full-deployment probe should fail rather than certify an incomplete installation.

## Network, security and operation

The API mapping publishes `${API_PORT:-3000}` on all host interfaces. Restrict access with a
host firewall or change the mapping to `127.0.0.1:${API_PORT:-3000}:3000` for a local reverse proxy.
Put TLS/reverse proxy or a tunnel in front before Internet use. DB/Redis have no host mappings.
`TRUSTED_PROXIES` accepts explicit IPs/CIDRs; empty means ignore forwarded IP headers. The server
must not independently rewrite peers before this check; the supplied worker disables Uvicorn's
proxy rewriting. Configure the same policy when running alternative ASGI servers.

Compose prefixes the network name with its project name (normally
`ontario-geocoder_geocoder-network`), not literal `geocoder-network`.
`docker compose ps` and `docker compose logs --tail=100 api init` show health/startup errors.
Allowed-origin preflight is public; actual API operations still authenticate, rate-limit and
check key IP restrictions. Security/request-ID headers also apply to errors.

Redis troubleshooting must authenticate, without putting the password in process arguments:

```bash
docker compose -f docker-compose.prod.yml exec redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli ping'
```

Backups (substitute DB credentials' **names**, not passwords):

```bash
mkdir -p backups
docker compose -f docker-compose.prod.yml exec -T postgres pg_dump -U geocoder -d geocoder -Fc > backups/geocoder.dump
# Stop services without deleting imported data:
docker compose -f docker-compose.prod.yml down
```

`down -v` deletes the database/cache volumes. Downloaded bind-mounted source files are separate;
remove them only when intentionally testing downloads from scratch. Automated offsite backups,
PITR, HA and restore drills are not provided. `uv sync --frozen && uv run pytest` runs tests;
`uv run pytest tests/ --cov=api --cov-report=term-missing` includes coverage.
