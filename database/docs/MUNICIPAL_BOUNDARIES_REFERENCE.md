# Ontario Municipal Boundaries Reference

## Table: `lower_municipal_boundaries`

This table contains the boundaries for Ontario's Lower Tier and Single Tier municipalities, imported from the Ontario GeoHub dataset (updated January 22, 2025).

### Data Summary
- **Total municipalities**: 685
- **Unique municipal IDs**: 414 (some municipalities have multiple geometries, e.g. mainland + islands)
- **CRS**: EPSG:4326 (WGS84) - reprojected from EPSG:4269

### Schema

`import_municipal_boundaries.py` renames a handful of the Ontario LIO shapefile's
truncated (10-character) DBF field names on import; everything else is kept as the raw,
mixed-case shapefile field name (quoted below) with no renaming or documented semantics.
See `database/schema.sql` for the authoritative definition — it explicitly documents this
table as reflecting the live, geopandas-imported shape rather than the older design in
`migrations.sql`'s Migration 11.

| Column | Type | Description |
|--------|------|-------------|
| `id` | integer | Primary key (auto-generated) |
| `"OGF_ID"` | integer | Ontario GeoHub Feature ID |
| `tier_type` | text | Municipality tier type (English) - e.g. "Single Tier", "Lower Tier" (renamed from `MUNICIPAL_`) |
| `"MUNICIPA_1"` | text | Raw shapefile field (French tier type, per import-script comment) |
| `name` | text | Full municipality name (English), e.g. "TOWNSHIP OF ARMOUR" (renamed from `MUNICIPA_2`) |
| `"MUNICIPA_3"` | text | Raw shapefile field (French full name, per import-script comment) |
| `municipal_id` | text | Municipal ID - 414 unique values for 685 records (renamed from `MUNID`) |
| `"MAH_CODE"` | text | Ministry of Municipal Affairs and Housing code |
| `"MUNICIPA_4"` | text | Raw shapefile field (undocumented) |
| `"MUNICIPA_5"` | text | Raw shapefile field (undocumented) |
| `"ASSESSMENT"` | text | Raw shapefile field (assessment code) |
| `"MUNICIPA_6"` | text | Raw shapefile field (type prefix, e.g. "TOWNSHIP OF", per import-script comment) |
| `"MUNICIPA_7"` | text | Raw shapefile field (undocumented) |
| `short_name` | text | Short municipality name (English), e.g. "ARMOUR" (renamed from `MUNICIPA_8`) |
| `"MUNICIPA_9"` | text | Raw shapefile field (undocumented) |
| `"UPPER_TIER"` | text | Raw shapefile field (undocumented) |
| `upper_tier_name` | text | Upper tier name, for lower-tier municipalities (renamed from `UPPER_TI_1`) |
| `"UPPER_TI_2"` | text | Raw shapefile field (undocumented) |
| `"EFFECTIVE_"` | timestamp | Effective date |
| `"SYSTEM_DAT"` | timestamp | System date |
| `"OBJECTID"` | integer | Shapefile object ID |
| `"SHAPEAREA"` | integer | Shape area |
| `"SHAPELEN"` | integer | Shape perimeter length |
| `geometry` | geometry | PostGIS geometry (polygon), EPSG:4326 |

### Indexes

- `id` - Primary key
- `idx_lower_municipal_boundaries_geometry` - Spatial index (GIST) on geometry
- `idx_lower_municipal_boundaries_municipal_id` - Index on municipal_id
- `idx_lower_municipal_boundaries_name` - Index on name
- `idx_lower_municipal_boundaries_tier_type` - Index on tier_type

### Example Queries

#### Find municipality for a point (reverse geocoding)
```sql
SELECT name, tier_type, municipal_id
FROM lower_municipal_boundaries
WHERE ST_Contains(
    geometry,
    ST_SetSRID(ST_MakePoint(-79.3832, 43.6532), 4326)
);
```

#### Validate properties against municipal boundaries
```sql
SELECT
    p.id,
    p.address,
    m.name AS municipality,
    m.tier_type AS municipality_type
FROM properties p
LEFT JOIN lower_municipal_boundaries m
    ON ST_Contains(m.geometry, ST_SetSRID(ST_MakePoint(p.longitude, p.latitude), 4326))
WHERE p.latitude IS NOT NULL AND p.longitude IS NOT NULL
LIMIT 10;
```

#### Find all municipalities of a specific tier type
```sql
SELECT name, upper_tier_name
FROM lower_municipal_boundaries
WHERE tier_type = 'Lower Tier'
ORDER BY name;
```

#### Find municipalities within a region using upper tier
```sql
SELECT name, short_name
FROM lower_municipal_boundaries
WHERE upper_tier_name = 'REGIONAL MUNICIPALITY OF YORK'
ORDER BY name;
```

### Data Source

- **Dataset**: Ontario Lower and Single Tier Municipal Boundaries
- **Source**: Ontario GeoHub
- **File**: `ontario_municipalities_2025_01_22/MUNIC_BND_LOWER_AND_SINGLE.shp`
- **Last Updated**: January 22, 2025
- **Import Script**: `data_sourcing/importers/import_municipal_boundaries.py`

### Notes

- The `municipal_id` has 414 unique values for 685 records because some municipalities have multiple geometries (e.g., mainland + islands)
- Single Tier municipalities don't have an upper tier (UPR_TIER_E/F is NULL)
- Lower Tier municipalities belong to a regional or county government (UPR_TIER_E/F contains the upper tier name)
- All geometries are in EPSG:4326 (WGS84) for consistency with other geocoder data

### Related Tables

- `fsa_boundaries` - Forward Sortation Area boundaries for postal code validation
- `nar_locations` - NAR truth source data with municipal relationships
