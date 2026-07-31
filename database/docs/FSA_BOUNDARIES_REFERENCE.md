# FSA Boundaries Reference

## Overview

The `fsa_boundaries` table contains all 1,643 Canadian Forward Sortation Areas (FSAs) with their geographic polygon boundaries. FSAs are the first three characters of a Canadian postal code (e.g., "K1A", "M5V").

**Data Source:** Statistics Canada - 2021 Census Forward Sortation Area Boundary Files
**Import Date:** 2025-11-14
**CRS:** EPSG:4326 (WGS84) - matches all other geocoder spatial data

---

## Table Schema

### `fsa_boundaries`

| Column | Type | Description | Example |
|--------|------|-------------|---------|
| `id` | integer | Primary key (auto-generated) | 1 |
| `fsa_code` | text | 3-character FSA code | "K1A" |
| `province_id` | text | Province PRUID code | "35" |
| `province_name` | text | Full province name (bilingual) | "Ontario" |
| `land_area_km2` | double precision | Land area in square kilometers | 0.56 |
| `dissemination_guid` | text | Statistics Canada DGUID | "2021A0011K1A" |
| `geometry` | geometry(Geometry,4326) | Polygon boundary (WGS84) | MULTIPOLYGON(...) |

### Indexes

- **Primary Key:** `fsa_boundaries_pkey` on `id`
- **Unique Index:** `idx_fsa_boundaries_fsa_code` on `fsa_code` - Fast FSA lookups
- **Spatial Index:** `idx_fsa_boundaries_geometry` (GIST) - Fast spatial queries
- **Province Index:** `idx_fsa_boundaries_province` on `province_id` - Filter by province

---

## FSA Distribution by Province

| Province | FSA Count |
|----------|-----------|
| Ontario | 520 |
| Quebec | 414 |
| British Columbia | 191 |
| Alberta | 154 |
| New Brunswick | 110 |
| Nova Scotia | 77 |
| Manitoba | 74 |
| Saskatchewan | 52 |
| Newfoundland and Labrador | 35 |
| Prince Edward Island | 7 |
| Northwest Territories | 3 |
| Yukon | 3 |
| Nunavut | 3 |
| **Total** | **1,643** |

---

## Common Use Cases

### 1. Validate Postal Codes

Check if postal codes in your data have valid FSAs:

```sql
-- Find invalid postal codes in NAR data
SELECT postal_code, COUNT(*) as count
FROM nar_addresses
WHERE LEFT(postal_code, 3) NOT IN (SELECT fsa_code FROM fsa_boundaries)
GROUP BY postal_code
ORDER BY count DESC
LIMIT 20;
```

### 2. Reverse Geocode to FSA

Given coordinates, find which FSA they fall within:

```sql
-- Find FSA for a specific point (Toronto City Hall example)
SELECT fsa_code, province_name, land_area_km2
FROM fsa_boundaries
WHERE ST_Contains(
    geometry,
    ST_SetSRID(ST_MakePoint(-79.3832, 43.6532), 4326)
);

-- Result: M5G, Ontario, 1.89 km²
```

### 3. Batch Geocode Properties to FSA

Assign FSA to properties based on coordinates:

```sql
-- Add FSA to properties with coordinates
UPDATE properties p
SET fsa = f.fsa_code
FROM fsa_boundaries f
WHERE p.latitude IS NOT NULL
  AND p.longitude IS NOT NULL
  AND ST_Contains(
      f.geometry,
      ST_SetSRID(ST_MakePoint(p.longitude, p.latitude), 4326)
  );
```

### 4. Find Adjacent FSAs

Find FSAs that border a specific FSA:

```sql
-- Find FSAs adjacent to K1A (Ottawa downtown)
SELECT f2.fsa_code, f2.province_name
FROM fsa_boundaries f1
JOIN fsa_boundaries f2 ON ST_Touches(f1.geometry, f2.geometry)
WHERE f1.fsa_code = 'K1A'
ORDER BY f2.fsa_code;
```

### 5. Coverage Analysis

Analyze geocode coverage by FSA:

```sql
-- Count NAR addresses per FSA with/without coordinates
SELECT
    f.fsa_code,
    f.province_name,
    COUNT(n.id) as total_addresses,
    COUNT(nl.bg_latitude) as geocoded_addresses,
    ROUND(100.0 * COUNT(nl.bg_latitude) / NULLIF(COUNT(n.id), 0), 2) as pct_geocoded
FROM fsa_boundaries f
LEFT JOIN nar_addresses n ON LEFT(n.mail_postal_code, 3) = f.fsa_code
LEFT JOIN nar_locations nl ON n.loc_guid = nl.loc_guid
WHERE f.province_id = '35'  -- Ontario only
GROUP BY f.fsa_code, f.province_name
ORDER BY total_addresses DESC
LIMIT 20;
```

### 6. Distance Calculations

Find FSAs within a certain distance:

```sql
-- Find FSAs within 10km of a point (Ottawa downtown)
SELECT
    fsa_code,
    province_name,
    ROUND(ST_Distance(
        geometry::geography,
        ST_SetSRID(ST_MakePoint(-75.6972, 45.4215), 4326)::geography
    ) / 1000, 2) as distance_km
FROM fsa_boundaries
WHERE ST_DWithin(
    geometry::geography,
    ST_SetSRID(ST_MakePoint(-75.6972, 45.4215), 4326)::geography,
    10000  -- 10km in meters
)
ORDER BY distance_km;
```

### 7. Postal Code Validation with Feedback

Validate and provide suggestions for invalid postal codes:

```sql
-- Check if postal code FSA exists, suggest nearby if not
WITH input_postal AS (
    SELECT 'K1A2B3' as postal_code
)
SELECT
    i.postal_code,
    CASE
        WHEN f.fsa_code IS NOT NULL THEN 'Valid FSA'
        ELSE 'Invalid FSA'
    END as status,
    f.fsa_code as matched_fsa,
    f.province_name
FROM input_postal i
LEFT JOIN fsa_boundaries f ON LEFT(i.postal_code, 3) = f.fsa_code;
```

### 8. Geographic Filtering for External Datasets

Before importing external municipal data, check if it falls within target FSAs:

```sql
-- Check if Guelph addresses fall within expected FSAs
SELECT DISTINCT f.fsa_code, COUNT(*) as address_count
FROM external_addresses e
JOIN fsa_boundaries f ON ST_Contains(
    f.geometry,
    ST_SetSRID(ST_MakePoint(e.longitude, e.latitude), 4326)
)
WHERE f.province_name = 'Ontario'
GROUP BY f.fsa_code
ORDER BY address_count DESC;
```

---

## Integration with NAR Geocoding

### Enhanced Postal Code Matching

Use FSA boundaries to improve geocoding confidence:

```sql
-- Verify postal code matches geographic location
SELECT
    n.mail_postal_code,
    n.civic_no,
    n.official_street_name,
    nl.bg_latitude,
    nl.bg_longitude,
    LEFT(n.mail_postal_code, 3) as postal_fsa,
    f.fsa_code as geographic_fsa,
    CASE
        WHEN LEFT(n.mail_postal_code, 3) = f.fsa_code THEN 'Match'
        ELSE 'Mismatch'
    END as validation_status
FROM nar_addresses n
JOIN nar_locations nl ON n.loc_guid = nl.loc_guid
LEFT JOIN fsa_boundaries f ON ST_Contains(
    f.geometry,
    ST_SetSRID(ST_MakePoint(nl.bg_longitude, nl.bg_latitude), 4326)
)
WHERE nl.bg_latitude IS NOT NULL
  AND LEFT(n.mail_postal_code, 3) != f.fsa_code
LIMIT 100;
```

### Filling Missing Postal Codes

When external datasets have coordinates but no postal code:

```sql
-- Assign FSA-based postal code prefix to records with coordinates
UPDATE external_addresses e
SET postal_code_fsa = f.fsa_code
FROM fsa_boundaries f
WHERE e.postal_code IS NULL
  AND e.latitude IS NOT NULL
  AND e.longitude IS NOT NULL
  AND ST_Contains(
      f.geometry,
      ST_SetSRID(ST_MakePoint(e.longitude, e.latitude), 4326)
  );
```

---

## Import Script Documentation

### Script: `data_sourcing/importers/import_fsa_boundaries.py`

**Purpose:** Import Canadian FSA boundary shapefiles into PostgreSQL with PostGIS support.

#### Requirements

```bash
# Python 3.11+
# Dependencies (auto-installed by uv):
- geopandas
- sqlalchemy
- geoalchemy2
- psycopg2-binary
- python-dotenv
```

#### Usage

```bash
# Basic usage (uses default shapefile path)
DB_HOST=localhost uv run data_sourcing/importers/import_fsa_boundaries.py

# Specify custom shapefile
DB_HOST=localhost uv run data_sourcing/importers/import_fsa_boundaries.py \
    --shapefile path/to/custom.shp \
    --table custom_fsa_table

# View help
uv run data_sourcing/importers/import_fsa_boundaries.py --help
```

#### Environment Variables

The script uses these environment variables (with defaults):

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_HOST` | localhost | PostgreSQL host |
| `DB_PORT` | 5432 | PostgreSQL port |
| `DB_NAME` | geocoder | Database name |
| `DB_USER` | geocoder | Database username |
| `DB_PASSWORD` | geocoder123 | Database password |

#### What the Script Does

1. **Reads shapefile** - Loads FSA boundaries from Statistics Canada shapefile
2. **Reprojects geometry** - Converts from EPSG:3347 (Statistics Canada Lambert) to EPSG:4326 (WGS84)
3. **Renames columns** - Converts StatCan column names to readable format
4. **Imports to a staging table** - Loads all 1,643 FSA polygons into `fsa_boundaries_staging`, leaving the live `fsa_boundaries` table untouched
5. **Creates indexes on staging** - Adds primary key, unique FSA index, spatial index, and province index before the swap
6. **Atomically swaps staging into production** - Near-instant rename, zero query downtime (see IMPORTER_SYSTEM.md's `atomic_table_swap`)
7. **Displays summary** - Shows import statistics and example queries

#### Output Example

```
Reading shapefile: statcan_FSA_shapes/lfsa000a21a_e/lfsa000a21a_e.shp
✓ Loaded 1643 FSA boundaries
  Original CRS: EPSG:3347
  Columns: ['CFSAUID', 'DGUID', 'PRUID', 'PRNAME', 'LANDAREA', 'geometry']

Reprojecting to EPSG:4326 (WGS84)...
✓ Reprojected to EPSG:4326

Connecting to database...
✓ Connected (PostGIS version: 3.3 USE_GEOS=1 USE_PROJ=1 USE_STATS=1)

Importing 1643 FSA boundaries to staging table 'fsa_boundaries_staging'...
✓ Data imported to staging table

Creating indexes on staging table...
✓ Created primary key
✓ Created spatial index on geometry
✓ Created unique index on fsa_code
✓ Created index on province_id

Atomically swapping staging table into production...
✓ Swap complete

============================================================
IMPORT SUMMARY
============================================================
Total FSAs imported: 1643

FSAs by Province:
  Ontario: 520
  Quebec / Québec: 414
  ...

✓ Import completed successfully!
```

#### Re-running the Script

The script is **idempotent** - it can be safely re-run multiple times:
- Imports into a fresh staging table and atomically swaps it into place each time, so a failed
  or interrupted run leaves the live `fsa_boundaries` table untouched
- No duplicate data issues

---

## Performance Notes

- **Spatial queries are FAST** - The GIST index makes `ST_Contains` queries on 1,643 polygons near-instant
- **Unique FSA lookups are instant** - B-tree index on `fsa_code` enables microsecond lookups
- **Geometry size** - Each FSA polygon is relatively simple (most are <100 vertices)
- **Memory footprint** - Entire table with indexes is ~50MB

---

## Data Limitations

1. **No city/municipality information** - FSAs don't align with municipal boundaries
   - A single FSA can span multiple cities
   - A city can have many FSAs
   - Use separate municipal boundary data for city-level analysis

2. **FSAs can cross provincial borders** - Rare but possible in border regions

3. **Rural FSAs are large** - Urban FSAs (e.g., M5G = 1.89 km²) vs rural (e.g., T2P = 84.65 km²)

4. **Postal codes != FSAs** - Full 6-character postal codes are more precise
   - FSA is only first 3 characters
   - Use for area-level analysis, not precise geocoding

5. **No population data** - This table only has geographic boundaries
   - Join with census data for population statistics

---

## Maintenance

### Updating FSA Boundaries

Statistics Canada releases updated FSA boundaries with each census (every 5 years):

```bash
# Download new shapefile from StatCan
# Run import script with new file
DB_HOST=localhost uv run data_sourcing/importers/import_fsa_boundaries.py \
    --shapefile statcan_FSA_shapes/new_census/lfsa000a26a_e.shp
```

### Verifying Data Integrity

```sql
-- Check for duplicate FSA codes (should return 0)
SELECT fsa_code, COUNT(*)
FROM fsa_boundaries
GROUP BY fsa_code
HAVING COUNT(*) > 1;

-- Check for invalid geometries (should return 0)
SELECT fsa_code
FROM fsa_boundaries
WHERE NOT ST_IsValid(geometry);

-- Verify all provinces have FSAs
SELECT province_name, COUNT(*) as fsa_count
FROM fsa_boundaries
GROUP BY province_name
ORDER BY fsa_count DESC;
```

---

## Related Tables

- **`nar_addresses`** - Contains `mail_postal_code` field (join on `LEFT(mail_postal_code, 3)`)
- **`nar_locations`** - Contains coordinates for spatial joins
- **`properties`** - Contains `postal_code` field for validation

---

## Additional Resources

- [Statistics Canada FSA Boundary Files](https://www12.statcan.gc.ca/census-recensement/2021/geo/sip-pis/boundary-limites/index2021-eng.cfm?year=21)
- [Canada Post Postal Code Information](https://www.canadapost-postescanada.ca/cpc/en/support/kb/addressing/general-information/postal-code-formats)
- [PostGIS Documentation - Spatial Relationships](https://postgis.net/docs/reference.html#Spatial_Relationships)
