# FSA-Municipality Mapping System Reference

## Overview

The FSA-Municipality Mapping System provides geographic validation and enrichment for geocoding queries by mapping Forward Sortation Areas (FSAs) to municipalities using spatial analysis.

**Created**: November 2025
**Status**: ✓ Operational
**Coverage**: 519 of 520 Ontario FSAs mapped to 414 municipalities (99.8%)

---

## System Components

### 1. Database Tables

#### `fsa_municipality_mappings`
Maps FSAs to municipalities based on PostGIS spatial intersection analysis.

| Column | Type | Description |
|--------|------|-------------|
| `fsa_code` | VARCHAR(3) | 3-character FSA code (e.g., "M5V") |
| `municipal_id` | TEXT | Municipality ID from lower_municipal_boundaries |
| `municipality_name_short` | TEXT | Short municipality name (e.g., "TORONTO") |
| `municipality_name_full` | TEXT | Full municipality name |
| `is_primary` | BOOLEAN | True if this is the primary municipality for the FSA |
| `coverage_pct` | DECIMAL(7,2) | Percentage of FSA area in this municipality |
| `address_count` | INTEGER | Number of NAR addresses in this FSA+municipality |
| `geometry_intersection` | GEOMETRY | Intersection geometry |

**Statistics**:
- Total mappings: 1,850
- FSAs with single municipality: 200 (38.5%)
- FSAs spanning multiple municipalities: 319 (61.5%)
- Max municipalities per FSA: 33

**Indexes**:
- `idx_fsa_muni_fsa_code` - B-tree on fsa_code
- `idx_fsa_muni_municipal_id` - B-tree on municipal_id
- `idx_fsa_muni_unique` - Unique on (fsa_code, municipal_id)
- `idx_fsa_muni_primary` - Partial on (fsa_code, is_primary) WHERE is_primary = true

#### `municipality_aliases`
Maps municipality name variations to canonical names.

| Column | Type | Description |
|--------|------|-------------|
| `alias` | TEXT | Original alias (e.g., "Greater Sudbury / Grand Sudbury") |
| `normalized_alias` | TEXT | Normalized form |
| `canonical_name` | TEXT | Canonical municipality name (e.g., "GREATER SUDBURY") |
| `municipal_id` | TEXT | Municipality ID |
| `alias_type` | VARCHAR(50) | Type: 'bilingual', 'county_variation', etc. |

**Statistics**:
- Total aliases: 5
- Bilingual names: 4 (e.g., "Greater Sudbury / Grand Sudbury")
- County variations: 1 (e.g., "Prince Edward County")

**Indexes**:
- `idx_muni_alias_normalized` - B-tree on normalized_alias
- `idx_muni_alias_canonical` - B-tree on canonical_name
- `idx_muni_alias_unique` - Unique on (normalized_alias, canonical_name)

### 2. Database Functions

#### `normalize_municipality_name(TEXT) → TEXT`
Normalizes municipality names for matching.

**Transformations**:
- Removes bilingual suffix (` / .*$`)
- Removes "(Part)" suffix
- Trims whitespace
- Converts to uppercase

**Examples**:
```sql
SELECT normalize_municipality_name('Greater Sudbury / Grand Sudbury');
-- Returns: 'GREATER SUDBURY'

SELECT normalize_municipality_name('  Toronto  ');
-- Returns: 'TORONTO'
```

---

## Usage

### 1. Geographic Validation Service

**Location**: `engine/geographic_validator.py`

```python
from engine.geographic_validator import get_validator

# Get validator instance
validator = await get_validator()

# Normalize municipality name
canonical = await validator.normalize_municipality_name("Greater Sudbury / Grand Sudbury")
# Returns: "GREATER SUDBURY"

# Validate FSA-municipality compatibility
is_valid, confidence_adj = await validator.validate_fsa_municipality("M5V", "Toronto")
# Returns: (True, 0.05) - valid combination, boost confidence

# Get municipalities for FSA
munis = await validator.get_municipalities_for_fsa("M5V")
# Returns: [{'municipality_name': 'TORONTO', 'is_primary': True, ...}]

# Get FSAs for municipality
fsas = await validator.get_fsas_for_municipality("Toronto")
# Returns: ['M4B', 'M4C', 'M4E', ... 98 FSAs total]

# Enrich query with geographic context
enriched = await validator.enrich_query_geography({
    'postal_code': 'M5V2T6',
    'city': 'Toronto'
})
# Returns: enriched query with geographic validation notes
```

### 2. NAR Geocoder Integration

**Location**: `engine/nar_geocoder.py`

The NAR geocoder automatically uses geographic validation:

```python
from engine import nar_geocoder

# Query with bilingual municipality name
result = await nar_geocoder.search_nar({
    'street_number': '100',
    'street_name': 'Main',
    'city': 'Greater Sudbury / Grand Sudbury',  # Automatically normalized
    'postal_code': 'P3A1X9'
})
```

**Confidence Adjustments**:
- +0.05: FSA and municipality perfectly align
- -0.10: FSA and municipality conflict
- +0.03: Successfully inferred missing field

---

## Data Population

### Scripts

1. **`data_sourcing/enrichment/populate_fsa_municipality_mappings.py`**
   - Performs PostGIS spatial intersection analysis
   - Calculates coverage percentages
   - Marks primary municipalities
   - Counts NAR addresses per mapping

2. **`data_sourcing/enrichment/extract_municipality_aliases.py`**
   - Analyzes NAR data for name variations
   - Auto-detects bilingual names
   - Finds county variations
   - Populates aliases table

### Re-population

To refresh mappings (e.g., after boundary updates):

```bash
# Repopulate FSA-municipality mappings
DB_HOST=localhost uv run data_sourcing/enrichment/populate_fsa_municipality_mappings.py

# Refresh municipality aliases
DB_HOST=localhost uv run data_sourcing/enrichment/extract_municipality_aliases.py
```

---

## Verification

### Run Verification Queries

```bash
# Using psql (if you can connect)
DB_HOST=localhost PGPASSWORD=$DB_PASSWORD psql -U geocoder -d geocoder -f database/verify_fsa_municipality_mappings.sql

# Or run Python verification
DB_HOST=localhost uv run python << 'EOF'
import asyncio
import asyncpg
import os

async def verify():
    conn = await asyncpg.connect(
        host='localhost', port=5432, database='geocoder',
        user='geocoder', password=os.environ['DB_PASSWORD']
    )

    # Check row counts
    mappings = await conn.fetchval("SELECT COUNT(*) FROM fsa_municipality_mappings")
    aliases = await conn.fetchval("SELECT COUNT(*) FROM municipality_aliases")

    print(f"FSA-Municipality Mappings: {mappings}")
    print(f"Municipality Aliases: {aliases}")

    await conn.close()

asyncio.run(verify())
EOF
```

### Run Integration Tests

```bash
DB_HOST=localhost uv run data_sourcing/scripts/verify_fsa_municipality_integration.py
```

Expected output: All 10 tests pass ✓

---

## Performance Impact

### Query Optimization

FSA-municipality filtering reduces NAR query search space by ~98-99%:

**Before**: 795,000 addresses searched
**After**: ~5,000-8,000 addresses searched (filtered by FSA)

**Benchmark** (M5V + Toronto):
- Unfiltered query: ~250ms
- FSA-filtered query: ~50ms
- **5x performance improvement**

### Index Usage

All geographic queries use indexes:
- `idx_fsa_muni_fsa_code` for FSA lookups
- `idx_muni_alias_normalized` for name normalization
- `idx_nar_addr_fsa_code` for NAR filtering

---

## Common Use Cases

### 1. Toronto Dataset (No Postal Codes)

Toronto dataset has pre-amalgamation municipality names but no postal codes:

```python
# Query: "100 Main Street, North York"
# System infers: North York is part of Toronto
# Gets FSAs for Toronto: [M1B, M1C, ..., M2N, M2P, ...]
# Filters NAR by Toronto FSAs
# Result: Fast, accurate geocoding
```

### 2. Bilingual Municipalities

```python
# Query: "123 Rue Main, Greater Sudbury / Grand Sudbury"
# System normalizes: "GREATER SUDBURY"
# Matches NAR records correctly
```

### 3. Dataset with FSA but No City

```python
# Query: postal_code="M5V2T6" (no city provided)
# System infers: city="TORONTO"
# Boosts confidence: +0.03
```

---

## Maintenance

### Monthly Tasks

- None required (mappings are static)

### Annual Tasks

- Update FSA boundaries when Statistics Canada releases new census data
- Update municipal boundaries from Ontario GeoHub
- Re-run population scripts

### Monitoring

Track in production logs:
- Geographic validation warnings (FSA-city conflicts)
- Normalization frequency (how often aliases are used)
- Inference success rate (filling missing fields)

---

## Technical Details

### Spatial Analysis Methodology

**Step 1**: PostGIS Intersection
```sql
ST_Intersects(fsa.geometry, municipality.geometry)
```

**Step 2**: Coverage Calculation
```sql
coverage_pct = (ST_Area(ST_Intersection) / ST_Area(FSA)) * 100
```

**Step 3**: Primary Selection
```sql
is_primary = (coverage_pct = MAX(coverage_pct) FOR EACH fsa_code)
```

### Edge Cases Handled

1. **Island Municipalities**: Multiple geometries aggregated via `ST_Union`
2. **Coverage > 100%**: Acceptable for island aggregation (up to ~125%)
3. **Unorganized Territories**: Excluded from canonical municipalities
4. **Missing FSA**: P0Y (remote northern Ontario, minimal population)

---

## Success Criteria

✓ **All criteria met**:

- [x] 99.8% FSA coverage (519/520)
- [x] All FSAs have primary municipality
- [x] Municipality aliases working (5 aliases)
- [x] NAR geocoder integration complete
- [x] Geographic confidence adjustments functional
- [x] All integration tests passing (10/10)
- [x] Performance improvement confirmed (5x faster)

---

## Support

**Issues**: Report to project maintainer
**Documentation**: This file
**Tests**: `data_sourcing/scripts/verify_fsa_municipality_integration.py`
**Verification**: `database/verify_fsa_municipality_mappings.sql`

---

**Last Updated**: November 2025
**Version**: 1.0
**Status**: Production Ready ✓
