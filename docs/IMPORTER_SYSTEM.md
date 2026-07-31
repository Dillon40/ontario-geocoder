# Importer System Documentation

The geocoder importer system handles loading address data from various sources into PostgreSQL with full audit trails, duplicate resolution, and stale record management.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            IMPORT PIPELINE                                   │
└─────────────────────────────────────────────────────────────────────────────┘

     ┌──────────────┐
     │  Data Source │  (GeoJSON, CSV, Shapefile)
     └──────┬───────┘
            │
            ▼
┌───────────────────────────────────────┐
│  1. FILE HASH CHECK                   │
│  ┌─────────────────────────────────┐  │
│  │ FileHashManager.should_skip()  │  │
│  │                                 │  │
│  │  • Compute SHA256 of file      │  │
│  │  • Compare with last import    │  │
│  │  • Skip if unchanged           │  │
│  └─────────────────────────────────┘  │
│           │                           │
│     ┌─────┴─────┐                     │
│     │           │                     │
│  [unchanged] [changed/--force]        │
│     │           │                     │
│     ▼           ▼                     │
│   SKIP      CONTINUE                  │
└───────────────────────────────────────┘
            │
            ▼
┌───────────────────────────────────────┐
│  2. IMPORT TRACKING                   │
│  ┌─────────────────────────────────┐  │
│  │ ImportTracker (context mgr)    │  │
│  │                                 │  │
│  │  • Create import_history row   │  │
│  │  • Track stats (insert/update) │  │
│  │  • Log row-level errors        │  │
│  │  • Record completion status    │  │
│  └─────────────────────────────────┘  │
└───────────────────────────────────────┘
            │
            ▼
┌───────────────────────────────────────┐
│  3. RECORD PROCESSING                 │
│  ┌─────────────────────────────────┐  │
│  │ For each record:               │  │
│  │                                 │  │
│  │  a) Generate property_id       │  │
│  │     PropertyIdGenerator        │  │
│  │     Format: {SRC}-{TYPE}-{ID}  │  │
│  │     e.g., TOR-PIN-12345        │  │
│  │                                 │  │
│  │  b) UPSERT to database         │  │
│  │     ON CONFLICT DO UPDATE      │  │
│  │     • Update all fields        │  │
│  │     • Set last_seen_import_id  │  │
│  │     • Clear is_deleted flag    │  │
│  │                                 │  │
│  │  c) Track seen IDs             │  │
│  │     StaleRecordDetector        │  │
│  │     .mark_seen(property_id)    │  │
│  └─────────────────────────────────┘  │
└───────────────────────────────────────┘
            │
            ▼
┌───────────────────────────────────────┐
│  4. STALE RECORD DETECTION            │
│  ┌─────────────────────────────────┐  │
│  │ StaleRecordDetector             │  │
│  │                                 │  │
│  │  • Query all IDs for source    │  │
│  │  • Compare with seen IDs       │  │
│  │  • Stale = DB - Seen           │  │
│  │                                 │  │
│  │  Strategy options:             │  │
│  │  ├─ soft_delete (default)      │  │
│  │  │   is_deleted = TRUE         │  │
│  │  │   deleted_at = NOW()        │  │
│  │  │                             │  │
│  │  ├─ report_only                │  │
│  │  │   Log stale IDs only        │  │
│  │  │                             │  │
│  │  └─ skip                       │  │
│  │      No stale detection        │  │
│  └─────────────────────────────────┘  │
└───────────────────────────────────────┘
            │
            ▼
┌───────────────────────────────────────┐
│  5. COMPLETION                        │
│  ┌─────────────────────────────────┐  │
│  │ ImportTracker finalization     │  │
│  │                                 │  │
│  │  • Update import_history       │  │
│  │  • Record final counts         │  │
│  │  • Set status: completed/failed│  │
│  │  • Log duration                │  │
│  └─────────────────────────────────┘  │
└───────────────────────────────────────┘
```

## Key Components

### 1. FileHashManager

Prevents redundant imports by comparing file hashes.

```python
from import_utils import FileHashManager

# Check if file should be skipped
should_skip, reason = await FileHashManager.should_skip_file(
    pool, 'toronto_addresses', '/path/to/data.geojson'
)

if should_skip:
    print(f"Skipping: {reason}")
    # "File unchanged since last import (2024-01-15 10:30:00)"
```

**Behavior:**
- Computes SHA256 hash of import file
- Queries `import_history` for last successful import of same source
- Compares hashes; returns skip=True if identical
- Override with `--force` flag

### 2. ImportTracker

Async context manager for full audit trail.

```python
from import_tracker import ImportTracker

async with ImportTracker(pool, 'toronto_addresses', file_path) as tracker:
    for batch in data:
        inserted = await import_batch(batch)
        tracker.stats.rows_inserted += inserted
        tracker.stats.rows_processed += len(batch)

    if error:
        await tracker.log_row_error(row_num, 'validation_error', str(e))
```

**Tracks:**
- `rows_processed` - Total records attempted
- `rows_inserted` - New records created
- `rows_updated` - Existing records modified
- `rows_failed` - Records with errors
- `rows_skipped` - Records filtered out
- `rows_deleted` - Stale records soft-deleted

### 3. StaleRecordDetector

Identifies and handles records missing from new imports.

```python
from import_utils import StaleRecordDetector

detector = StaleRecordDetector(
    pool, import_id, 'toronto_addresses',
    'properties', 'property_id'
)

# During import: track each record
for record in records:
    await upsert(record)
    detector.mark_seen(record['property_id'])

# After import: find and handle stale records
stale_ids = await detector.detect_stale_records()
deleted = await detector.soft_delete_stale_records(stale_ids)
```

### 4. PropertyIdGenerator

Generates consistent, source-prefixed identifiers.

```python
from import_utils import PropertyIdGenerator

# Priority: existing_id > PIN > ARN > coordinates > row_number
property_id = PropertyIdGenerator.generate('toronto', record, row_num)
# Returns: "TOR-PIN-12345" or "TOR-ARN-9876" or "TOR-GEO-abc123..."
```

**ID Format:** `{PREFIX}-{TYPE}-{VALUE}`

| Prefix | Source |
|--------|--------|
| TOR | Toronto |
| OTT | Ottawa |
| GUE | Guelph |
| BRC | Bruce County |
| ... | First 3 chars |

### 5. atomic_table_swap

Enables zero-downtime boundary imports using staging table pattern.

```python
from import_utils import atomic_table_swap

# Import data to staging table
gdf.to_postgis(name='fsa_boundaries_staging', con=engine, ...)

# Create indexes on staging table (while production remains available)
conn.execute(text("CREATE INDEX ... ON fsa_boundaries_staging ..."))

# Atomically swap staging into production
atomic_table_swap(engine, 'fsa_boundaries', 'fsa_boundaries_staging')
```

**Atomic Swap Sequence:**
```
1. DROP TABLE IF EXISTS {table}_old CASCADE
2. ALTER TABLE {table} RENAME TO {table}_old
3. ALTER TABLE {table}_staging RENAME TO {table}
4. DROP TABLE {table}_old (cleanup)
```

**Benefits:**
- Zero query downtime (renames are near-instant)
- Indexes built offline before swap
- Rollback capability until cleanup
- Failed imports leave production untouched

**Used by:** `import_fsa_boundaries.py`, `import_municipal_boundaries.py`

## Database Schema

### Soft Delete Columns (Migration 19)

Added to `properties`, `nar_addresses`, `nar_locations`:

| Column | Type | Purpose |
|--------|------|---------|
| `is_deleted` | BOOLEAN | Soft delete flag |
| `deleted_at` | TIMESTAMP | When record was deleted |
| `deleted_by_import_id` | INTEGER | Which import deleted it |
| `last_seen_import_id` | INTEGER | Last import that saw record |

### Import History

```sql
SELECT * FROM import_history ORDER BY started_at DESC LIMIT 5;
```

| Column | Description |
|--------|-------------|
| `source_key` | Data source identifier |
| `import_type` | 'full', 'incremental', 'partial' |
| `status` | 'started', 'completed', 'failed' |
| `file_hash` | SHA256 for deduplication |
| `rows_*` | Count statistics |
| `error_message` | Failure details |

## CLI Usage

### import_production_data.py

```bash
# Standard import (skips unchanged files)
uv run data_sourcing/importers/import_production_data.py --file /path/to/addresses.geojson --source toronto

# Force import even if file unchanged
uv run data_sourcing/importers/import_production_data.py --file /path/to/addresses.geojson --source toronto --force

# Report stale records without deleting
uv run data_sourcing/importers/import_production_data.py --file /path/to/addresses.geojson --source toronto \
    --stale-strategy report_only

# Skip stale detection entirely
uv run data_sourcing/importers/import_production_data.py --file /path/to/addresses.geojson --source toronto \
    --stale-strategy skip
```

### import_nar_data.py

```bash
# Standard NAR import with UPSERT and stale detection
uv run data_sourcing/importers/import_nar_data.py \
    --addresses /path/to/Address_10.csv \
    --locations /path/to/Location_10.csv

# Force reimport
uv run data_sourcing/importers/import_nar_data.py \
    --addresses /path/to/Address_10.csv \
    --locations /path/to/Location_10.csv \
    --force
```

### import_fsa_boundaries.py

```bash
# Import FSA boundaries with atomic staging swap (zero downtime)
uv run data_sourcing/importers/import_fsa_boundaries.py \
    --shapefile /path/to/lfsa000a21a_e.shp

# Custom table name
uv run data_sourcing/importers/import_fsa_boundaries.py \
    --shapefile /path/to/lfsa000a21a_e.shp \
    --table fsa_boundaries_v2
```

### import_municipal_boundaries.py

```bash
# Import municipal boundaries with atomic staging swap (zero downtime)
uv run data_sourcing/importers/import_municipal_boundaries.py \
    --shapefile /path/to/Municipal_Boundary_-_Lower_and_Single_Tier.shp

# Custom table name
uv run data_sourcing/importers/import_municipal_boundaries.py \
    --shapefile /path/to/Municipal_Boundary.shp \
    --table lower_municipal_boundaries_v2
```

## Import Flow Diagram

```
                    ┌─────────────────┐
                    │   Start Import  │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │ Compute File    │
                    │ SHA256 Hash     │
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
        ┌─────▼─────┐  ┌─────▼─────┐  ┌─────▼─────┐
        │  No Prior │  │   Hash    │  │   Hash    │
        │  Import   │  │  Matches  │  │  Differs  │
        └─────┬─────┘  └─────┬─────┘  └─────┬─────┘
              │              │              │
              │        ┌─────▼─────┐        │
              │        │  --force? │        │
              │        └─────┬─────┘        │
              │         no/  │  \yes        │
              │         ▼    │    ▼         │
              │      [SKIP]  │              │
              │              │              │
              └──────────────┼──────────────┘
                             │
                    ┌────────▼────────┐
                    │ Create Import   │
                    │ History Record  │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │ Process Records │◄──────┐
                    │ in Batches      │       │
                    └────────┬────────┘       │
                             │                │
                    ┌────────▼────────┐       │
                    │ UPSERT Record   │       │
                    │ + Mark Seen     │       │
                    └────────┬────────┘       │
                             │                │
                    ┌────────▼────────┐       │
                    │  More Records?  │───yes─┘
                    └────────┬────────┘
                             │ no
                    ┌────────▼────────┐
                    │ Detect Stale    │
                    │ Records         │
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
        ┌─────▼─────┐  ┌─────▼─────┐  ┌─────▼─────┐
        │soft_delete│  │report_only│  │   skip    │
        └─────┬─────┘  └─────┬─────┘  └─────┬─────┘
              │              │              │
        ┌─────▼─────┐  ┌─────▼─────┐        │
        │ SET       │  │ Log Stale │        │
        │is_deleted │  │ IDs Only  │        │
        │ = TRUE    │  └─────┬─────┘        │
        └─────┬─────┘        │              │
              │              │              │
              └──────────────┼──────────────┘
                             │
                    ┌────────▼────────┐
                    │ Update Import   │
                    │ History Stats   │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │   Complete      │
                    └─────────────────┘
```

## Querying Active vs Deleted Records

```sql
-- Only active records (uses partial index)
SELECT * FROM properties WHERE is_deleted = FALSE;

-- Include deleted records
SELECT * FROM properties;

-- Find recently deleted records
SELECT property_id, deleted_at, deleted_by_import_id
FROM properties
WHERE is_deleted = TRUE
ORDER BY deleted_at DESC;

-- Restore accidentally deleted records
UPDATE properties
SET is_deleted = FALSE, deleted_at = NULL
WHERE deleted_by_import_id = 123;
```

## Files

| File | Purpose |
|------|---------|
| `import_tracker.py` | Import history context manager |
| `import_utils.py` | FileHashManager, StaleRecordDetector, PropertyIdGenerator, atomic_table_swap |
| `import_production_data.py` | GeoJSON property importer (UPSERT pattern) |
| `import_nar_data.py` | NAR CSV address importer (UPSERT pattern) |
| `import_fsa_boundaries.py` | FSA boundary shapefile importer (atomic staging swap) |
| `import_municipal_boundaries.py` | Municipal boundary importer (atomic staging swap) |

## Import Patterns

The system uses two import patterns depending on data type:

| Pattern | Used By | Approach |
|---------|---------|----------|
| **UPSERT** | properties, NAR addresses | `ON CONFLICT DO UPDATE` with soft delete tracking |
| **Atomic Staging Swap** | FSA boundaries, municipal boundaries | Staging table → atomic rename → cleanup |

**UPSERT** is best for:
- Incremental updates
- Large tables with frequent small changes
- Records that need individual tracking

**Atomic Staging Swap** is best for:
- Full table replacements
- Reference/lookup data
- Zero query downtime requirement
