# Ontario Geocoder

A geocoding microservice for Ontario property addresses, built entirely on open data. Given a
street address, it returns coordinates, a confidence score, and the licence/attribution
information required by the source that produced the match.

## Why this exists

Commercial geocoding APIs (Google, Mapbox, etc.) are usable for a public real-estate site, but
they come with per-request cost, rate limits, and terms of service that restrict how results can
be cached, stored, and redistributed. This project takes a different approach: it builds its own
address index from data that is already public and licensed for reuse —

- the **National Address Register (NAR)**, Statistics Canada's Canada-wide address dataset,
- **municipal open-data portals** (Toronto, Guelph, and others), which publish parcel-level
  address data directly, and
- **StatCan Forward Sortation Area (FSA) boundaries**, used to resolve a postal-code prefix to a
  geographic area for spatial lookups.

There is no external geocoding API in the request path. A match either comes from this service's
own database or the request returns a clean no-match — nothing is proxied to a third party at
query time. (One offline benchmarking script optionally calls Google's Geocoding API to
sanity-check accuracy against an external source; see [Accuracy benchmarking](#accuracy-benchmarking).
It is not part of the live geocoding path.)

## How it works

Every request is tried against three tiers in order, stopping at the first confident match:

1. **Tier 1 — PIN lookup** (confidence 1.0 for parcel identity). If the request includes a Property
   Identification Number, this is an indexed parcel lookup against active `properties` rows.
   A parcel may have several distinct addresses/units; PIN-only requests select a deterministic
   address point for that parcel. PIN is **not** the address-level primary key.

2. **Tier 2 — Municipal datasets** (confidence roughly 0.65–0.99). Searches all imported municipal
   datasets together (not routed by postal code first). It runs a cascading sequence of match
   strategies, starting with an exact match on every field supplied in the request and relaxing
   constraints step by step — postal code, then FSA, then postal-code prefix, then city alone —
   before falling back to PostgreSQL trigram (`pg_trgm`) fuzzy street-name matching to catch typos.
   The first layer that returns a single, unambiguous match wins; when a layer returns several
   candidates, a scoring pass picks the best one before falling through to the next, looser layer.

3. **Tier 3 — National Address Register fallback** (final confidence capped at **0.90**). If
   Tier 2 does not match, NAR uses a similar component cascade: exact supplied fields, postal/core,
   FSA/core/city, postal-zone/core, city/core, then fuzzy street matching. NAR uses its own lower
   strategy scores because coordinate quality can be coarser than municipal sources. Geographic
   boosts cannot exceed the tier ceiling. Scores are heuristics, not calibrated probabilities;
   fuzzy matching and penalties can produce scores below 0.36. See the current
   [confidence policy](docs/NAR_QUERY_CONFIDENCE_RATINGS.md) for all weights and adjustments.

If no tier produces a match, the API returns `404` with a structured error — not a guess.
When a source supplies only address components (such as Hamilton), `matched_address` is assembled
from the matched database row, including known unit/civic suffix, city, province and postal code.
Existing source-provided labels are preserved; request text is never substituted for a matched label.

Every successful result also carries `license_info` and `attribution`, resolved from the
`data_sources` table for whichever source produced the match, so a consuming application can
satisfy each open-data licence's attribution requirement. See
[Licensing & attribution](#licensing--attribution) below.

## Components

```
├── api/                 FastAPI service: HTTP layer, auth, rate limiting
├── engine/              Geocoding engine: the 3-tier cascade described above
├── data_sourcing/       ETL pipeline: downloaders -> transformers -> importers
├── database/            PostgreSQL + PostGIS schema and migrations
├── setup/               VM/host provisioning scripts
├── config/              Operational config (dataset field mappings)
├── docs/                Deeper reference docs (see "Further documentation" below)
└── tests/               Test suite
```

**`engine/`** is the geocoding logic itself: `geocoder.py` runs the tier cascade,
`layered_matcher.py` implements the Tier 2/3 layered matching described above,
`confidence.py` defines strategy scores/tier ceilings, `address_formatter.py` builds missing
matched labels from database components, `dataset_router.py` and
`geographic_validator.py` support FSA/municipality resolution, and `license_service.py` resolves
licence and attribution metadata per data source. It has no HTTP or database-provisioning
concerns of its own — it expects a connected `asyncpg` pool.

**`api/`** wraps the engine in a FastAPI app: request/response models (`models.py`), API-key
authentication and per-key rate limiting (`middleware/`), admin endpoints for key management, and
startup config validation that refuses to boot with insecure settings in production (wildcard
CORS, no Redis password, etc. — see `api/services/config_validator.py`).

**`data_sourcing/`** is the ETL pipeline that populates the database: `downloaders/` pull raw
files from municipal portals and Statistics Canada with ETag/hash-based change detection;
`transformers/` normalize fields (street parsing, municipality aliasing, FSA enrichment) into the
common schema; `importers/` upsert the normalized records with full audit logging
(`import_history`, `import_row_errors`) and stale-record handling. `management/` holds the CLI for
tracking data-source metadata (licences, coverage, status) in the database itself, not just in
code. See [`docs/IMPORTER_SYSTEM.md`](docs/IMPORTER_SYSTEM.md) for the import pipeline in detail.

**`database/`** holds `schema.sql` (base schema), `migrations.sql` (all migrations, numbered and
checksum-verified by `run_migrations.py`), and `seed_data_sources.sql`. See
[`docs/schema_reference.txt`](docs/schema_reference.txt) for the field-level schema reference.

## Setup

### Prerequisites

- Linux (Ubuntu 22.04+ or RHEL/AlmaLinux 8+), or Docker
- Python 3.11+
- PostgreSQL 14+ with PostGIS
- Redis 7+
- [uv](https://github.com/astral-sh/uv) (Python package/dependency manager)

### Quick start

Clone this repository, then choose **one** deployment path.

#### Docker (recommended)

Requires Docker Engine and Compose v2; host Python/PostgreSQL/Redis are not required.

```bash
cp .env.example .env
chmod 600 .env
# Edit .env FIRST: set DB_PASSWORD, REDIS_PASSWORD, API_KEY_PEPPER,
# ALLOWED_ORIGINS and INITIAL_ADMIN_KEY (32+ random characters).
# Generate each secret separately with: openssl rand -hex 32
# Set API_PORT to an unused host port (default 3000).
make docker-up
make docker-etl   # downloads + imports; nonzero exit means setup is incomplete
curl http://localhost:3000/v1/health
```

Compose starts the API and runs tracked database initialization automatically. Do **not** run
host `make import-all` or `make dev` against this stack: PostgreSQL has no host port and the API
is already running. See [the deployment runbook](docs/DEPLOYMENT.md) for authentication,
per-source imports, verification and recovery. Health means DB/Redis connectivity, not complete data coverage.
Middlesex municipal data is excluded because permissive reuse rights could not be verified;
Middlesex requests use the NAR fallback, with its documented coordinate-coverage limitations.

#### Native host (separate alternative)

Use a dedicated VM, not a host already running unrelated PostgreSQL/Redis services.

```bash
export DB_PASSWORD="$(openssl rand -hex 32)"  # retain securely for native setup
make setup       # system packages, uv, database, environment and seeds
make import-all  # download + import; review licence prompts
make dev         # API on http://localhost:3000, interactive docs at /docs
```

See [VM setup](docs/VM_SETUP.md). Use `uv sync --frozen` to install the development test tools.
The test suite needs no database password; tests that exercise configuration use explicit dummy credentials
and verify that missing production credentials are rejected.

Run `make help` for the full list of targets. The most-used ones:

| Target | Description |
|--------|-------------|
| `make setup` | First-time host setup: packages, uv, database, env, seeds |
| `make db-init` | Schema + migrations + seed, for a database that already exists |
| `make import-all` | Download + import all confirmed data sources |
| `make dev` | Start the API in development mode (auto-reload, port 3000) |
| `make prod` | Start the API in production mode (gunicorn) |
| `make test` | Run the test suite |
| `make docker-up` | Start postgres + redis + api via Docker Compose |

See [`docs/cli_reference.md`](docs/cli_reference.md) for the complete Makefile reference, the
data-source management CLI, and per-source import options.

### Environment configuration

Copy `.env.example` to `.env` (or run `make setup-env`) and fill in the required values —
database credentials, `REDIS_PASSWORD`, and `ALLOWED_ORIGINS` have no defaults and the app will
not start without them in production. `.env.example` documents every setting, including
connection-pool sizing, rate limiting, and batch-processing tuning.

### Database migrations

```bash
uv run python database/run_migrations.py           # apply any migrations not yet applied
uv run python database/run_migrations.py --status  # show what's applied
uv run python database/run_migrations.py --dry-run # preview without applying
```

The runner tracks applied migrations in a `schema_migrations` table and verifies the SHA256
checksum of every previously-applied migration against `migrations.sql` on each run, so a stale
or hand-edited migration file is caught rather than silently ignored.

### Geocode one address

Once the API is running and at least one data source is imported:

```bash
curl -X POST http://localhost:3000/v1/geocode \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "Address": {
      "StreetNumber": "51",
      "StreetName": "Cork",
      "StreetSuffix": "Street",
      "StreetDirection": "West",
      "City": "Guelph",
      "Province": "ON",
      "PostalCode": "N1H 2X3"
    }
  }'
```

```json
{
  "success": true,
  "latitude": 43.543327,
  "longitude": -80.253393,
  "source": "municipal_guelph",
  "confidence": 0.99,
  "matched_address": "51 Cork Street West, Guelph, ON N1H 2X3",
  "attribution": "Contains information licensed under the Open Government Licence - Guelph"
}
```

`X-API-Key` is required unless `API_KEY_REQUIRED=false`. See
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for API key creation and the full endpoint list.

## Further documentation

- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — Docker Compose production architecture, API key
  management, security configuration, troubleshooting.
- [`docs/VM_SETUP.md`](docs/VM_SETUP.md) — provisioning a host without Docker (system packages,
  PostgreSQL/Redis directly on the VM).
- [`docs/IMPORTER_SYSTEM.md`](docs/IMPORTER_SYSTEM.md) — the ETL import pipeline: audit trail,
  stale-record detection, ID generation, atomic staging swaps for boundary data.
- [`docs/cli_reference.md`](docs/cli_reference.md) — full Makefile reference, data-source
  management CLI, NAR import, FSA enrichment scripts.
- [`docs/NAR_QUERY_CONFIDENCE_RATINGS.md`](docs/NAR_QUERY_CONFIDENCE_RATINGS.md) — how Tier 3
  confidence scores were derived from NAR data-quality analysis.
- [`docs/schema_reference.txt`](docs/schema_reference.txt) — generated field-level schema
  reference for every importable data source.

## Licensing & attribution

Every open-data source this service uses requires attribution under its licence (Open Government
Licence variants, the Statistics Canada Open Licence, and per-municipality equivalents). This is
database-driven, not hardcoded: `data_sources.required_attribution` holds the attribution
statement each licence mandates, and every geocode result includes the `license_info` and
`attribution` for the source that produced the match. `GET /v1/attributions` lists the required
attribution for every active data source, for building a complete attributions page. If you build
a client on this API, you are responsible for displaying that attribution wherever matched
addresses are shown.

## Accuracy benchmarking

`tests/engine/test_accuracy_regression.py` replays a sample of a benchmark address set through the
live Tier 2 matcher and checks accuracy against pinned thresholds. It compares this service's
coordinates against Google's forward-geocoding API purely as an offline, external ground truth —
this is unrelated to request-time geocoding, which never calls Google or any other external API.
These integration tests are skipped by default. To opt in, supply the benchmark file,
database credentials and Google configuration described in the module, then run:

```bash
uv run pytest tests/engine/test_accuracy_regression.py -m integration -v -s
```

They also skip when prerequisites are unavailable; check the skip summary rather than
interpreting a run with no comparisons as evidence of accuracy.

## Testing

For an imported Docker deployment, also run the [live regression gate](docs/DEPLOYMENT.md#live-regression-gate).
Unit tests do not certify source coverage or external-data accuracy. Validate real imports and
live API responses using the deployment runbook before relying on the service.

```bash
uv run pytest                                    # full test suite
uv run pytest tests/engine/ -v                   # geocoding engine tests only
uv run pytest tests/ --cov=api --cov-report=term-missing
```

## License

The code is released under the [MIT License](LICENSE). The address data this service imports is
**not** covered by that licence — each data source remains under its own open-data licence with
its own attribution requirements, as described in
[Licensing & attribution](#licensing--attribution).
