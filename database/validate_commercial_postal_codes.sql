-- ============================================================================
-- COMMERCIAL POSTAL CODE DATABASE VALIDATION
-- ============================================================================
-- Purpose: Validate commercial postal code data against geocoder reference data
--          to assess data quality and identify discrepancies before integration.
--
-- Validates against:
--   - fsa_boundaries (FSA coverage and location accuracy)
--   - municipality_aliases (city name alignment)
--   - fsa_municipality_mappings (FSA-city relationship validation)
--
-- Usage:
--   PGPASSWORD=$DB_PASSWORD psql -h localhost -U geocoder -d geocoder -f validate_commercial_postal_codes.sql
-- ============================================================================

\echo ''
\echo '================================================================================'
\echo 'COMMERCIAL POSTAL CODE DATABASE VALIDATION'
\echo '================================================================================'
\echo ''

-- ============================================================================
-- 0. DATA OVERVIEW
-- ============================================================================

\echo '0. Data Overview'
\echo '----------------'

SELECT
    'Total postal codes' as metric,
    COUNT(*) as count
FROM commercial_postal_codes
UNION ALL
SELECT
    'Unique FSA codes',
    COUNT(DISTINCT fsa_code)
FROM commercial_postal_codes
UNION ALL
SELECT
    'Unique cities (raw)',
    COUNT(DISTINCT city)
FROM commercial_postal_codes
WHERE city IS NOT NULL
UNION ALL
SELECT
    'Unique cities (normalized)',
    COUNT(DISTINCT city_normalized)
FROM commercial_postal_codes
WHERE city_normalized IS NOT NULL
UNION ALL
SELECT
    'Records with coordinates',
    COUNT(*)
FROM commercial_postal_codes
WHERE location IS NOT NULL;

\echo ''

-- ============================================================================
-- 1. FSA COVERAGE ANALYSIS
-- ============================================================================

\echo '1. FSA Coverage Analysis'
\echo '------------------------'

-- Compare FSAs in commercial DB vs fsa_boundaries
SELECT
    'Commercial DB FSAs' as source,
    COUNT(DISTINCT fsa_code) as count
FROM commercial_postal_codes
UNION ALL
SELECT
    'Ontario FSAs (fsa_boundaries)',
    COUNT(*)
FROM fsa_boundaries
WHERE province_id = '35'
UNION ALL
SELECT
    'FSAs in BOTH sources',
    COUNT(DISTINCT c.fsa_code)
FROM commercial_postal_codes c
JOIN fsa_boundaries f ON c.fsa_code = f.fsa_code
UNION ALL
SELECT
    'Commercial-only FSAs (not in boundaries)',
    COUNT(DISTINCT c.fsa_code)
FROM commercial_postal_codes c
LEFT JOIN fsa_boundaries f ON c.fsa_code = f.fsa_code
WHERE f.fsa_code IS NULL
UNION ALL
SELECT
    'Boundary-only FSAs (not in commercial)',
    COUNT(DISTINCT f.fsa_code)
FROM fsa_boundaries f
LEFT JOIN commercial_postal_codes c ON f.fsa_code = c.fsa_code
WHERE f.province_id = '35'
  AND c.fsa_code IS NULL;

\echo ''
\echo '  FSAs in commercial DB but NOT in fsa_boundaries:'
SELECT DISTINCT c.fsa_code, COUNT(*) as postal_count
FROM commercial_postal_codes c
LEFT JOIN fsa_boundaries f ON c.fsa_code = f.fsa_code
WHERE f.fsa_code IS NULL
GROUP BY c.fsa_code
ORDER BY postal_count DESC;

\echo ''
\echo '  Ontario FSAs in fsa_boundaries but NOT in commercial DB:'
SELECT f.fsa_code, f.province_name, ROUND(f.land_area_km2::numeric, 2) as area_km2
FROM fsa_boundaries f
LEFT JOIN commercial_postal_codes c ON f.fsa_code = c.fsa_code
WHERE f.province_id = '35'
  AND c.fsa_code IS NULL
ORDER BY f.fsa_code;

\echo ''

-- ============================================================================
-- 2. FSA LOCATION ACCURACY
-- ============================================================================

\echo '2. FSA Location Accuracy (Centroid Comparison)'
\echo '-----------------------------------------------'

-- Calculate distance between commercial postal code centroids and FSA boundary centroids
WITH commercial_fsa_centroids AS (
    SELECT
        fsa_code,
        AVG(latitude) as comm_lat,
        AVG(longitude) as comm_lon,
        COUNT(*) as postal_count
    FROM commercial_postal_codes
    WHERE latitude IS NOT NULL AND longitude IS NOT NULL
    GROUP BY fsa_code
),
fsa_boundary_centroids AS (
    SELECT
        fsa_code,
        ST_Y(ST_Centroid(geometry::geometry))::decimal(12,8) as bound_lat,
        ST_X(ST_Centroid(geometry::geometry))::decimal(12,8) as bound_lon
    FROM fsa_boundaries
    WHERE province_id = '35'
),
distance_analysis AS (
    SELECT
        c.fsa_code,
        c.postal_count,
        c.comm_lat,
        c.comm_lon,
        b.bound_lat,
        b.bound_lon,
        ST_Distance(
            ST_SetSRID(ST_MakePoint(c.comm_lon, c.comm_lat), 4326)::geography,
            ST_SetSRID(ST_MakePoint(b.bound_lon, b.bound_lat), 4326)::geography
        ) / 1000 as distance_km
    FROM commercial_fsa_centroids c
    JOIN fsa_boundary_centroids b ON c.fsa_code = b.fsa_code
)
SELECT
    CASE
        WHEN distance_km < 1 THEN '< 1 km (excellent)'
        WHEN distance_km < 5 THEN '1-5 km (good)'
        WHEN distance_km < 10 THEN '5-10 km (acceptable)'
        WHEN distance_km < 25 THEN '10-25 km (review)'
        ELSE '> 25 km (ERROR)'
    END as accuracy_band,
    COUNT(*) as fsa_count,
    ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM distance_analysis), 1) as percentage
FROM distance_analysis
GROUP BY 1
ORDER BY MIN(distance_km);

\echo ''
\echo '  Top 15 FSAs with largest centroid discrepancy:'
WITH commercial_fsa_centroids AS (
    SELECT
        fsa_code,
        AVG(latitude) as comm_lat,
        AVG(longitude) as comm_lon,
        COUNT(*) as postal_count
    FROM commercial_postal_codes
    WHERE latitude IS NOT NULL
    GROUP BY fsa_code
),
fsa_boundary_centroids AS (
    SELECT
        fsa_code,
        ST_Y(ST_Centroid(geometry::geometry))::decimal(12,8) as bound_lat,
        ST_X(ST_Centroid(geometry::geometry))::decimal(12,8) as bound_lon
    FROM fsa_boundaries
    WHERE province_id = '35'
)
SELECT
    c.fsa_code,
    c.postal_count,
    ROUND(ST_Distance(
        ST_SetSRID(ST_MakePoint(c.comm_lon, c.comm_lat), 4326)::geography,
        ST_SetSRID(ST_MakePoint(b.bound_lon, b.bound_lat), 4326)::geography
    ) / 1000, 2) as distance_km,
    ROUND(c.comm_lat::numeric, 4) as comm_lat,
    ROUND(c.comm_lon::numeric, 4) as comm_lon,
    ROUND(b.bound_lat::numeric, 4) as bound_lat,
    ROUND(b.bound_lon::numeric, 4) as bound_lon
FROM commercial_fsa_centroids c
JOIN fsa_boundary_centroids b ON c.fsa_code = b.fsa_code
ORDER BY distance_km DESC
LIMIT 15;

\echo ''

-- ============================================================================
-- 3. CITY-MUNICIPALITY ALIGNMENT
-- ============================================================================

\echo '3. City-Municipality Alignment'
\echo '------------------------------'

-- Check how many commercial cities match municipality aliases
SELECT
    'Unique commercial cities (normalized)' as metric,
    COUNT(DISTINCT city_normalized) as count
FROM commercial_postal_codes
WHERE city_normalized IS NOT NULL
UNION ALL
SELECT
    'Cities matching municipality_aliases',
    COUNT(DISTINCT c.city_normalized)
FROM commercial_postal_codes c
JOIN municipality_aliases a ON c.city_normalized = a.normalized_alias
UNION ALL
SELECT
    'Cities NOT in aliases (need review)',
    COUNT(DISTINCT c.city_normalized)
FROM commercial_postal_codes c
LEFT JOIN municipality_aliases a ON c.city_normalized = a.normalized_alias
WHERE a.id IS NULL
  AND c.city_normalized IS NOT NULL;

\echo ''
\echo '  Top 50 unmapped cities by postal code count:'
SELECT
    c.city as city_raw,
    c.city_normalized,
    COUNT(*) as postal_count,
    COUNT(DISTINCT c.fsa_code) as fsa_count
FROM commercial_postal_codes c
LEFT JOIN municipality_aliases a ON c.city_normalized = a.normalized_alias
WHERE a.id IS NULL
  AND c.city_normalized IS NOT NULL
GROUP BY c.city, c.city_normalized
ORDER BY postal_count DESC
LIMIT 50;

\echo ''
\echo '  Cities with multiple FSAs (likely valid municipalities needing aliases):'
SELECT
    c.city as city_raw,
    c.city_normalized,
    COUNT(*) as postal_count,
    COUNT(DISTINCT c.fsa_code) as fsa_count,
    STRING_AGG(DISTINCT c.fsa_code, ', ' ORDER BY c.fsa_code) as fsa_codes
FROM commercial_postal_codes c
LEFT JOIN municipality_aliases a ON c.city_normalized = a.normalized_alias
WHERE a.id IS NULL
  AND c.city_normalized IS NOT NULL
GROUP BY c.city, c.city_normalized
HAVING COUNT(DISTINCT c.fsa_code) >= 3
ORDER BY fsa_count DESC, postal_count DESC
LIMIT 25;

\echo ''

-- ============================================================================
-- 4. FSA-MUNICIPALITY MAPPING VALIDATION
-- ============================================================================

\echo '4. FSA-Municipality Mapping Validation'
\echo '--------------------------------------'

-- Compare FSA+city combinations against fsa_municipality_mappings
WITH commercial_fsa_city AS (
    SELECT DISTINCT
        fsa_code,
        city_normalized
    FROM commercial_postal_codes
    WHERE city_normalized IS NOT NULL
)
SELECT
    'Commercial FSA+city combinations' as metric,
    COUNT(*) as count
FROM commercial_fsa_city
UNION ALL
SELECT
    'Combinations in fsa_municipality_mappings',
    COUNT(*)
FROM commercial_fsa_city c
JOIN fsa_municipality_mappings m
    ON c.fsa_code = m.fsa_code
    AND c.city_normalized = normalize_municipality_name(m.municipality_name_short)
UNION ALL
SELECT
    'Combinations NOT in mappings (new)',
    COUNT(*)
FROM commercial_fsa_city c
LEFT JOIN fsa_municipality_mappings m
    ON c.fsa_code = m.fsa_code
    AND c.city_normalized = normalize_municipality_name(m.municipality_name_short)
WHERE m.id IS NULL;

\echo ''
\echo '  New FSA-city combinations not in fsa_municipality_mappings (top 50):'
WITH commercial_fsa_city AS (
    SELECT
        fsa_code,
        city_normalized,
        city,
        COUNT(*) as postal_count
    FROM commercial_postal_codes
    WHERE city_normalized IS NOT NULL
    GROUP BY fsa_code, city_normalized, city
)
SELECT
    c.fsa_code,
    c.city as city_raw,
    c.city_normalized,
    c.postal_count,
    COALESCE(
        (SELECT municipality_name_short
         FROM fsa_municipality_mappings
         WHERE fsa_code = c.fsa_code AND is_primary = true
         LIMIT 1),
        '(no primary)'
    ) as current_primary_municipality
FROM commercial_fsa_city c
LEFT JOIN fsa_municipality_mappings m
    ON c.fsa_code = m.fsa_code
    AND c.city_normalized = normalize_municipality_name(m.municipality_name_short)
WHERE m.id IS NULL
ORDER BY c.postal_count DESC
LIMIT 50;

\echo ''
\echo '  Commercial DB vs fsa_municipality_mappings primary assignment conflicts:'
WITH commercial_primary AS (
    -- For each FSA, get the city with the most postal codes
    SELECT DISTINCT ON (fsa_code)
        fsa_code,
        city_normalized as comm_primary,
        COUNT(*) as postal_count
    FROM commercial_postal_codes
    WHERE city_normalized IS NOT NULL
    GROUP BY fsa_code, city_normalized
    ORDER BY fsa_code, COUNT(*) DESC
),
mapping_primary AS (
    SELECT
        fsa_code,
        normalize_municipality_name(municipality_name_short) as map_primary
    FROM fsa_municipality_mappings
    WHERE is_primary = true
)
SELECT
    c.fsa_code,
    c.comm_primary as commercial_city,
    m.map_primary as mapping_primary,
    c.postal_count as comm_postal_count
FROM commercial_primary c
JOIN mapping_primary m ON c.fsa_code = m.fsa_code
WHERE c.comm_primary != m.map_primary
ORDER BY c.postal_count DESC
LIMIT 25;

\echo ''

-- ============================================================================
-- 5. DATA QUALITY CHECKS
-- ============================================================================

\echo '5. Data Quality Checks'
\echo '----------------------'

SELECT
    'Total records' as check_name,
    COUNT(*) as count,
    100.0 as percentage
FROM commercial_postal_codes
UNION ALL
SELECT
    'With valid coordinates',
    COUNT(*),
    ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM commercial_postal_codes), 1)
FROM commercial_postal_codes
WHERE latitude IS NOT NULL AND longitude IS NOT NULL
UNION ALL
SELECT
    'With city name',
    COUNT(*),
    ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM commercial_postal_codes), 1)
FROM commercial_postal_codes
WHERE city IS NOT NULL
UNION ALL
SELECT
    'Coordinates in Ontario bbox',
    COUNT(*),
    ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM commercial_postal_codes), 1)
FROM commercial_postal_codes
WHERE latitude BETWEEN 41.0 AND 57.0
  AND longitude BETWEEN -95.0 AND -74.0
UNION ALL
SELECT
    'Coordinates OUTSIDE Ontario (ERROR)',
    COUNT(*),
    ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM commercial_postal_codes), 1)
FROM commercial_postal_codes
WHERE latitude IS NOT NULL
  AND (latitude < 41.0 OR latitude > 57.0
       OR longitude < -95.0 OR longitude > -74.0);

\echo ''
\echo '  Records with coordinates outside Ontario bounding box:'
SELECT
    postal_code_raw,
    fsa_code,
    city,
    latitude,
    longitude
FROM commercial_postal_codes
WHERE latitude IS NOT NULL
  AND (latitude < 41.0 OR latitude > 57.0
       OR longitude < -95.0 OR longitude > -74.0)
LIMIT 20;

\echo ''

-- ============================================================================
-- 6. POSTAL CODE FORMAT VALIDATION
-- ============================================================================

\echo '6. Postal Code Format Validation'
\echo '--------------------------------'

SELECT
    'Valid format (A1A 1A1)' as check_name,
    COUNT(*) as count,
    ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM commercial_postal_codes), 1) as percentage
FROM commercial_postal_codes
WHERE postal_code_raw ~ '^[A-Z][0-9][A-Z] [0-9][A-Z][0-9]$'
UNION ALL
SELECT
    'Invalid format',
    COUNT(*),
    ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM commercial_postal_codes), 1)
FROM commercial_postal_codes
WHERE NOT (postal_code_raw ~ '^[A-Z][0-9][A-Z] [0-9][A-Z][0-9]$');

\echo ''
\echo '  Sample invalid postal code formats (if any):'
SELECT postal_code_raw, fsa_code, city
FROM commercial_postal_codes
WHERE NOT (postal_code_raw ~ '^[A-Z][0-9][A-Z] [0-9][A-Z][0-9]$')
LIMIT 10;

\echo ''

-- ============================================================================
-- VALIDATION SUMMARY
-- ============================================================================

\echo '================================================================================'
\echo 'VALIDATION SUMMARY'
\echo '================================================================================'
\echo ''

-- Generate summary metrics
WITH fsa_coverage AS (
    SELECT
        COUNT(DISTINCT c.fsa_code) as commercial_fsas,
        (SELECT COUNT(*) FROM fsa_boundaries WHERE province_id = '35') as boundary_fsas,
        COUNT(DISTINCT CASE WHEN f.fsa_code IS NOT NULL THEN c.fsa_code END) as matched_fsas
    FROM commercial_postal_codes c
    LEFT JOIN fsa_boundaries f ON c.fsa_code = f.fsa_code
),
city_coverage AS (
    SELECT
        COUNT(DISTINCT c.city_normalized) as commercial_cities,
        COUNT(DISTINCT CASE WHEN a.id IS NOT NULL THEN c.city_normalized END) as matched_cities
    FROM commercial_postal_codes c
    LEFT JOIN municipality_aliases a ON c.city_normalized = a.normalized_alias
    WHERE c.city_normalized IS NOT NULL
)
SELECT
    'FSA Coverage' as category,
    f.commercial_fsas || ' / ' || f.boundary_fsas as value,
    ROUND(f.matched_fsas * 100.0 / NULLIF(f.boundary_fsas, 0), 1) || '%' as match_rate
FROM fsa_coverage f
UNION ALL
SELECT
    'City Coverage',
    c.matched_cities || ' / ' || c.commercial_cities,
    ROUND(c.matched_cities * 100.0 / NULLIF(c.commercial_cities, 0), 1) || '%'
FROM city_coverage c;

\echo ''
\echo 'Key findings to review:'
\echo '  - FSAs in commercial DB not in boundaries (Section 1)'
\echo '  - FSAs with >10km centroid discrepancy (Section 2)'
\echo '  - Cities needing municipality alias entries (Section 3)'
\echo '  - New FSA-city relationships to consider (Section 4)'
\echo '  - Coordinates outside Ontario bbox (Section 5)'
\echo ''
\echo '================================================================================'
