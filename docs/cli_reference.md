# CLI Reference

Makefile targets, the data-source management CLI, and per-script import/enrichment options.
For the ETL pipeline's architecture (audit trail, stale-record handling, ID generation), see
[IMPORTER_SYSTEM.md](IMPORTER_SYSTEM.md). For production/Docker deployment, see
[DEPLOYMENT.md](DEPLOYMENT.md). For a from-scratch VM without Docker, see [VM_SETUP.md](VM_SETUP.md).

## Makefile reference

Run `make help` to print this list from the Makefile itself.

### Setup

| Target | Description |
|--------|-------------|
| `make setup` | Full first-time setup: system packages, uv, database, env, seeds |
| `make setup-db` | Create database, user, extensions, apply schema + migrations |
| `make setup-env` | Generate `.env` from template |
| `make setup-python` | Install uv and sync Python dependencies |

### Database

| Target | Description |
|--------|-------------|
| `make db-schema` | Apply `database/schema.sql` (idempotent) |
| `make db-migrate` | Run all database migrations (`database/run_migrations.py`; never falls back to untracked SQL) |
| `make db-seed` | Seed the `data_sources` table from `database/seed_data_sources.sql` |
| `make db-init` | `db-schema` + `db-migrate` + `db-seed`, for a database that already exists |
| `make db-status` | Show table row counts and applied-migration status |
| `make db-shell` | Open a `psql` shell using the `DB_*` variables from `.env` |

### Data import

| Target | Description |
|--------|-------------|
| `make import-all` | Download + import all confirmed sources (interactive licence consent) |
| `make import-all-auto` | Same, but auto-accepts non-commercial licences |
| `make import-only` | Import from already-downloaded files, no download step |
| `make import-commercial` | Import only commercially-licensed sources |
| `make import-source SRC=<source_key>` | Import a single source, e.g. `make import-source SRC=guelph_addresses` |
| `make import-nar` | Import NAR Ontario from `data_sourcing/data_sources/nar_ontario/` (files must already be there; use `make import-all` or `--source nar_ontario` to download automatically) |
| `make list-sources` | List all confirmed data sources and their status |

Middlesex municipal data is blocked by the approved source policy; default setup selects seven
confirmed sources and relies on NAR for Middlesex. Explicit source selection, `--force`, and
`--accept-non-commercial` do not override that exclusion. Direct municipal importer calls also
check blocked/rejected registry status before processing a file. See [DEPLOYMENT.md](DEPLOYMENT.md)
for the coverage tradeoff and upgrade behavior.

### Run

| Target | Description |
|--------|-------------|
| `make dev` | Start the API with `uvicorn --reload` on port 3000 |
| `make prod` | Start the API with `gunicorn` (4 uvicorn workers, 120s timeout) |

### Docker

| Target | Description |
|--------|-------------|
| `make docker-up` | Start postgres + redis + api via `docker-compose.prod.yml` |
| `make docker-down` | Stop the Docker Compose stack |
| `make docker-logs` | Tail Docker Compose logs |
| `make docker-etl` | Run the ETL module inside Docker; override `ETL_ARGS='--source guelph_addresses'` or `ETL_ARGS='--help'` |

### Test & verify

| Target | Description |
|--------|-------------|
| `make test` | Run the full test suite (`uv run pytest`) |
| `make test-parser` | Run `tests/engine/test_address_parser.py` only |
| `make test-transformers` | Run `tests/data_sourcing/transformers/` only |
| `make verify` | Run `scripts/verify_etl_pipeline.py --diff`, comparing a verify DB against prod |
| `make verify-quick` | Count-only verification, no hash comparison |
| `make verify-skip-etl` | Re-compare an existing verify DB without re-running the ETL |

### Utilities

| Target | Description |
|--------|-------------|
| `make clean-data` | Remove downloaded data files (keeps NAR, FSA, and municipal boundary files) |
| `make disk` | Show disk usage for downloaded data files and the database |

## Data-source management CLI

`data_sourcing/management/manage_data_sources.py` manages the `data_sources` table (metadata:
URLs, licences, coverage, status) — separate from `config/dataset-mappings.json`, which holds the
per-source *field mappings* used at import time. They're linked by `source_key`
(`data_sources.source_key = 'toronto'` ↔ `dataset-mappings.json["toronto"]`).

```bash
# List / inspect sources
uv run python data_sourcing/management/manage_data_sources.py list-sources
uv run python data_sourcing/management/manage_data_sources.py list-sources --status confirmed
uv run python data_sourcing/management/manage_data_sources.py list-sources --region Toronto
uv run python data_sourcing/management/manage_data_sources.py list-sources --data-type addresses
uv run python data_sourcing/management/manage_data_sources.py show-source toronto

# Add / update / delete a source
uv run python data_sourcing/management/manage_data_sources.py add-source \
  --key hamilton \
  --name "City of Hamilton Open Data" \
  --regions "Hamilton,Dundas,Ancaster,Flamborough,Stoney Creek" \
  --status potential \
  --region-type city \
  --portal-url "https://www.hamilton.ca/city-council/city-departments/planning-and-economic-development/gis-open-data" \
  --api-type "REST/FeatureServer" \
  --license-type "Open Government Licence" \
  --commercial-use true \
  --data-types "addresses,parcels,zoning"

uv run python data_sourcing/management/manage_data_sources.py update-source toronto \
  --status confirmed --record-count 525409 --commercial-use true

uv run python data_sourcing/management/manage_data_sources.py delete-source old_source

# Data types (the addresses/parcels/zoning/... reference table)
uv run python data_sourcing/management/manage_data_sources.py list-types
uv run python data_sourcing/management/manage_data_sources.py add-type \
  --key heritage_sites --display-name "Heritage Sites" \
  --description "Designated heritage properties and conservation districts"
uv run python data_sourcing/management/manage_data_sources.py link-types toronto "addresses,parcels,building_footprints"
uv run python data_sourcing/management/manage_data_sources.py unlink-types toronto "roads"

# Cross-source queries
uv run python data_sourcing/management/manage_data_sources.py sources-by-type addresses
uv run python data_sourcing/management/manage_data_sources.py types-for-region Toronto
uv run python data_sourcing/management/manage_data_sources.py common-types "toronto,ottawa,guelph"
uv run python data_sourcing/management/manage_data_sources.py export-json > data_sources_export.json
```

Adding or updating a source auto-regenerates the markdown docs in `data_sourcing/docs/`
(`confirmed_sources.md`, `potential_sources.md`, `excluded_sources.md`) unless `--no-regenerate`
is passed. To regenerate manually:

```bash
uv run python data_sourcing/management/generate_data_source_docs.py
```

Fresh installs use `database/seed_data_sources.sql` via the tracked initializer; Compose does
this automatically. The markdown files are descriptive snapshots, not an initialization source.
`migrate_markdown_to_db.py` is a legacy recovery tool: do not run it over a fresh seeded database,
because old markdown metadata can overwrite corrected endpoints. See [DEPLOYMENT.md](DEPLOYMENT.md).

## Orchestrated download + import (`setup_data.py`)

`data_sourcing/setup_data.py` is what `make import-*` calls under the hood — download and import
across all confirmed sources, or one source at a time:

```bash
uv run python data_sourcing/setup_data.py --all-confirmed                        # download + import (asks about non-commercial licences)
uv run python data_sourcing/setup_data.py --all-confirmed --accept-non-commercial
uv run python data_sourcing/setup_data.py --all-confirmed --download-only
uv run python data_sourcing/setup_data.py --all-confirmed --import-only --accept-non-commercial
uv run python data_sourcing/setup_data.py --all-confirmed --commercial-only
uv run python data_sourcing/setup_data.py --all-confirmed --skip-manual          # skip sources explicitly marked manual (not NAR)
uv run python data_sourcing/setup_data.py --source guelph_addresses
uv run python data_sourcing/setup_data.py --list
```

## Importing a single dataset directly

`data_sourcing/importers/import_production_data.py` is the underlying municipal-dataset importer
(UPSERT with stale-record soft-delete, full audit trail — see IMPORTER_SYSTEM.md). It also
doubles as a **dataset wizard**: run it with `--dry-run` against a new file and it analyzes the
fields and prints a mapping you can copy into `config/dataset-mappings.json`.

```bash
# Analyze a new dataset (dry run, no database changes)
uv run python data_sourcing/importers/import_production_data.py \
  --file path/to/new_city_dataset.geojson \
  --source new_city \
  --dry-run

# Import it, once config/dataset-mappings.json has an entry for "new_city"
uv run python data_sourcing/importers/import_production_data.py \
  --file path/to/new_city_dataset.geojson \
  --source new_city \
  --batch-size 1000

# Other options
uv run python data_sourcing/importers/import_production_data.py --file <f> --source <s> --force              # re-import even if the file hash is unchanged
uv run python data_sourcing/importers/import_production_data.py --file <f> --source <s> --stale-strategy report_only  # log stale records instead of soft-deleting them
uv run python data_sourcing/importers/import_production_data.py --file <f> --source <s> --stale-strategy skip         # skip stale-record detection entirely
```

Required fields for a new dataset: coordinates (`latitude`/`longitude` properties, or coordinate
geometry) and an address identifier (`street_number` or `full_address`). Recommended:
`property_id`, `street_name`, `street_suffix`, `city`, `full_address`. Optional: `postal_code`,
`street_direction`, `pin`, `gpid`, `assessment_roll_number`, `unit_no`.

## National Address Register (NAR) import

NAR downloads automatically as part of `make import-all`; `data_sourcing/importers/import_nar_data.py`
is the underlying importer if you need to run it directly against files already on disk:

```bash
uv run python data_sourcing/importers/import_nar_data.py \
  --addresses path/to/Address_10.csv \
  --locations path/to/Location_10.csv \
  --province 35 \
  --batch-size 5000

# Force reimport even if the file hash is unchanged
uv run python data_sourcing/importers/import_nar_data.py --addresses <f> --force
```

NAR is split across multiple CSV part files, address parts with or without matching location
files; parts without a location file import without coordinates. `--locations` is optional for
that reason.

### Backfilling NAR coordinates and FSA codes

Two one-off enrichment scripts populate columns on `nar_addresses` that aren't set by the import
itself:

```bash
# Populate latitude/longitude from the projected bg_x/bg_y coordinates (EPSG:3347 -> EPSG:4326)
./data_sourcing/enrichment/add_latlong_to_nar.py --db geocoder

# Populate fsa_code: pass 1 from mail_postal_code, pass 2 (spatial) via fsa_boundaries for
# rows still missing an FSA
./data_sourcing/enrichment/add_fsa_to_nar.py --db geocoder --dry-run   # preview coverage
./data_sourcing/enrichment/add_fsa_to_nar.py --db geocoder             # apply
./data_sourcing/enrichment/add_fsa_to_nar.py --db geocoder --spot-check-only  # postal-vs-spatial agreement check only
```

Both are idempotent (only touch rows with the target column still `NULL`) and process in batched,
per-batch transactions rather than one long-running transaction.

## FSA enrichment for a municipal dataset file

`data_sourcing/enrichment/add_fsa_to_dataset.py` adds an `fsa_code` field to a GeoJSON file via
PostGIS spatial lookup (`ST_Contains` against `fsa_boundaries`), before that file is imported:

```bash
./data_sourcing/enrichment/add_fsa_to_dataset.py --input toronto_address_dataset.geojson --output toronto_with_fsa.geojson
./data_sourcing/enrichment/add_fsa_to_dataset.py --input data.geojson --output data_fsa.geojson --lat-field lat --lon-field lon
./data_sourcing/enrichment/add_fsa_to_dataset.py --input data.geojson --output data_fsa.geojson --overwrite
```

## FSA and municipal boundary imports

Boundary reference data uses an atomic staging-table swap instead of UPSERT (see
IMPORTER_SYSTEM.md), so a failed import never leaves production mid-swap:

```bash
uv run python data_sourcing/importers/import_fsa_boundaries.py --shapefile /path/to/lfsa000a21a_e.shp
uv run python data_sourcing/importers/import_municipal_boundaries.py --shapefile /path/to/Municipal_Boundary_-_Lower_and_Single_Tier.shp
```

## Example SQL queries against data-source metadata

```sql
-- All data types available for Toronto
SELECT dt.display_name
FROM data_types dt
JOIN data_source_types dst ON dt.id = dst.data_type_id
JOIN data_sources ds ON ds.id = dst.data_source_id
WHERE 'Toronto' = ANY(ds.regions_covered);

-- All confirmed sources with commercial use allowed
SELECT source_name, regions_covered
FROM data_sources
WHERE status = 'confirmed' AND commercial_use_allowed = true;

-- Sources with both addresses AND parcels data
SELECT ds.source_name
FROM data_sources ds
WHERE EXISTS (
    SELECT 1 FROM data_source_types dst
    JOIN data_types dt ON dst.data_type_id = dt.id
    WHERE dst.data_source_id = ds.id AND dt.type_key = 'addresses'
) AND EXISTS (
    SELECT 1 FROM data_source_types dst
    JOIN data_types dt ON dst.data_type_id = dt.id
    WHERE dst.data_source_id = ds.id AND dt.type_key = 'parcels'
);
```

## Migrations

```bash
uv run python database/run_migrations.py             # apply pending migrations
uv run python database/run_migrations.py --status    # show applied migrations
uv run python database/run_migrations.py --dry-run   # preview without applying
uv run python database/run_migrations.py --force N   # force re-apply migration N (use with caution)
```

## Offline accuracy validation

`engine/scripts/validate_google_reverse.py` is a standalone benchmarking tool, not part of the
live geocoding path: it reverse-geocodes this service's own coordinates through Google's API and
checks whether the returned address matches the original. `GOOGLE_GEOCODING_API_KEY` /
`GOOGLE_API_ENABLED` are one-off env vars read only by this script and by
`scripts/test_accuracy_regression.py` (see "Accuracy benchmarking" in the root
[README](../README.md)) — set them inline rather than adding them to `.env`.

```bash
uv run python engine/scripts/validate_google_reverse.py                       # default: 100 addresses
uv run python engine/scripts/validate_google_reverse.py --sample 500
uv run python engine/scripts/validate_google_reverse.py --input path/to/geocoded_results.json
uv run python engine/scripts/validate_google_reverse.py --output reports/my_validation.json
```

Writes a console summary plus a JSON report to `reports/google_validation_report.json` by default.
Costs roughly $5 per 1,000 Google API calls.

## Running tests

```bash
uv run pytest                                              # everything
uv run pytest tests/engine/test_address_parser.py -v       # one file
uv run pytest tests/data_sourcing/transformers/ -v         # transformer tests
uv run pytest tests/ --cov=api --cov-report=term-missing   # with coverage
```

The pre-release accuracy gate is separate from the regular suite — see "Accuracy benchmarking" in
the root [README](../README.md).
