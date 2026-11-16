# Data Sourcing System

High-level overview of the `data_sourcing/` module.

## Components

| Component | Purpose |
|-----------|---------|
| `downloaders/` | Download source data using URLs from `data_sources` table. Tracks versions via ETag/hash in `data_sources.upstream_*` columns |
| `enrichment/` | Apply enhancements to imported data (e.g., FSA codes via spatial lookup) |
| `importers/` | Import source files into database. `ImportTracker` logs to `import_history` table |
| `management/` | Source metadata CRUD and documentation generation (not operational tracking) |
| `scripts/` | Testing, validation, and analysis scripts |
| `setup_data.py` | Orchestrates download → import workflows |

## Tracking Responsibilities

**Operational tracking** (what was downloaded/imported, when, success/failure):

| Actor | Writes To |
|-------|-----------|
| `downloaders/` | `data_sources.sync_status`, `upstream_etag`, `upstream_hash`, `last_sync_check` |
| `importers/` | `import_history` and `import_row_errors` tables (via `ImportTracker`) |

**Metadata management** (what sources exist and their configs):

| Actor | Writes To |
|-------|-----------|
| `management/` | `data_sources` table (source definitions, URLs, licenses, regions) |

## Key Database Tables

- `data_sources` — Source metadata + sync status
- `import_history` — Full audit trail for imports
- `import_row_errors` — Row-level error logging
- `schema_migrations` — Applied migration tracking

## Entry Points

```bash
# Unified setup (schema + migrations + seed)
./setup/initialize_geocoder.sh

# Download + import all confirmed sources
DB_HOST=localhost uv run data_sourcing/setup_data.py --all-confirmed

# Download only
DB_HOST=localhost uv run data_sourcing/setup_data.py --all-confirmed --download-only

# Import only
DB_HOST=localhost uv run data_sourcing/setup_data.py --all-confirmed --import-only

# Check for upstream changes without downloading
DB_HOST=localhost uv run data_sourcing/downloaders/download_data_sources.py --check-only

# Manage source metadata
DB_HOST=localhost uv run data_sourcing/management/manage_data_sources.py list-sources
```

> **Note:** `DB_HOST=localhost` here assumes a host-level or dev Postgres (e.g. the native
> `VM_SETUP.md` install, or a manually-run local container publishing 5432). In the
> `docker-compose.prod.yml` deployment, `postgres` has no host port mapping at all — run these
> through the `etl` Compose service instead (`DB_HOST=postgres`). See `docs/DEPLOYMENT.md`.

## Generated Documentation

The `data_sourcing/docs/` directory contains auto-generated markdown documentation:
- `confirmed_sources.md` - Confirmed data sources ready for import
- `potential_sources.md` - Potential sources being evaluated
- `excluded_sources.md` - Rejected or blocked sources

These files are generated from the `data_sources` database table via:
```bash
DB_HOST=localhost uv run data_sourcing/management/generate_data_source_docs.py
```

**Auto-regeneration:** Occurs automatically when adding new sources via `manage_data_sources.py add-source` (can be disabled with `--no-regenerate` flag).
