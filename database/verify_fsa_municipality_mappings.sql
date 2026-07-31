-- ==============================================================================
-- FSA-Municipality Mapping Verification Queries
-- ==============================================================================
-- Purpose: Verify the integrity and completeness of FSA-municipality mappings
--          and municipality aliases created for geographic validation
--
-- Usage: Run these queries after populating the mapping tables to verify
--        data quality and identify any potential issues
-- ==============================================================================

\echo '================================================================================'
\echo 'FSA-MUNICIPALITY MAPPING VERIFICATION'
\echo '================================================================================'
\echo ''

-- ==============================================================================
-- 1. TABLE EXISTENCE AND STRUCTURE
-- ==============================================================================

\echo '1. Verifying table existence and structure...'
\echo ''

SELECT
    table_name,
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_name = t.table_name) as column_count
FROM information_schema.tables t
WHERE table_name IN ('fsa_municipality_mappings', 'municipality_aliases', 'fsa_boundaries', 'lower_municipal_boundaries')
ORDER BY table_name;

\echo ''

-- ==============================================================================
-- 2. ROW COUNTS
-- ==============================================================================

\echo '2. Verifying row counts...'
\echo ''

SELECT
    'fsa_municipality_mappings' as table_name,
    COUNT(*) as row_count
FROM fsa_municipality_mappings
UNION ALL
SELECT
    'municipality_aliases',
    COUNT(*)
FROM municipality_aliases
UNION ALL
SELECT
    'fsa_boundaries (Ontario)',
    COUNT(*)
FROM fsa_boundaries
WHERE province_id = '35'
UNION ALL
SELECT
    'lower_municipal_boundaries (unique)',
    COUNT(DISTINCT municipal_id)
FROM lower_municipal_boundaries;

\echo ''

-- ==============================================================================
-- 3. FSA COVERAGE
-- ==============================================================================

\echo '3. Verifying FSA coverage...'
\echo ''

-- Check that all Ontario FSAs have at least one municipality mapping
SELECT
    'Total Ontario FSAs' as metric,
    COUNT(*) as count
FROM fsa_boundaries
WHERE province_id = '35'
UNION ALL
SELECT
    'FSAs with mappings',
    COUNT(DISTINCT fsa_code)
FROM fsa_municipality_mappings
UNION ALL
SELECT
    'FSAs WITHOUT mappings (should be 0-1)',
    COUNT(*)
FROM (
    SELECT f.fsa_code
    FROM fsa_boundaries f
    WHERE f.province_id = '35'
      AND NOT EXISTS (
          SELECT 1
          FROM fsa_municipality_mappings m
          WHERE m.fsa_code = f.fsa_code
      )
) unmapped;

\echo ''

-- List any unmapped FSAs
\echo '  Unmapped FSAs (if any):'
SELECT f.fsa_code, f.province_name, f.land_area_km2
FROM fsa_boundaries f
WHERE f.province_id = '35'
  AND NOT EXISTS (
      SELECT 1
      FROM fsa_municipality_mappings m
      WHERE m.fsa_code = f.fsa_code
  )
ORDER BY f.fsa_code;

\echo ''

-- ==============================================================================
-- 4. PRIMARY MUNICIPALITY ASSIGNMENT
-- ==============================================================================

\echo '4. Verifying primary municipality assignments...'
\echo ''

-- Each FSA should have exactly one primary municipality
SELECT
    'FSAs with exactly 1 primary' as metric,
    COUNT(*) as count
FROM (
    SELECT fsa_code
    FROM fsa_municipality_mappings
    WHERE is_primary = true
    GROUP BY fsa_code
    HAVING COUNT(*) = 1
) correct
UNION ALL
SELECT
    'FSAs with 0 primary (ERROR)',
    COUNT(*)
FROM (
    SELECT m.fsa_code
    FROM fsa_municipality_mappings m
    GROUP BY m.fsa_code
    HAVING SUM(CASE WHEN is_primary THEN 1 ELSE 0 END) = 0
) zero_primary
UNION ALL
SELECT
    'FSAs with multiple primary (ERROR)',
    COUNT(*)
FROM (
    SELECT fsa_code
    FROM fsa_municipality_mappings
    WHERE is_primary = true
    GROUP BY fsa_code
    HAVING COUNT(*) > 1
) multi_primary;

\echo ''

-- ==============================================================================
-- 5. COVERAGE PERCENTAGE VALIDATION
-- ==============================================================================

\echo '5. Verifying coverage percentages...'
\echo ''

SELECT
    'Valid coverage (0-100%)' as metric,
    COUNT(*) as count
FROM fsa_municipality_mappings
WHERE coverage_pct BETWEEN 0 AND 100
UNION ALL
SELECT
    'Coverage > 100% (acceptable for island aggregation)',
    COUNT(*)
FROM fsa_municipality_mappings
WHERE coverage_pct > 100 AND coverage_pct <= 200
UNION ALL
SELECT
    'Invalid coverage (< 0 or > 200%)',
    COUNT(*)
FROM fsa_municipality_mappings
WHERE coverage_pct < 0 OR coverage_pct > 200;

\echo ''

-- Show distribution of coverage percentages
\echo '  Coverage percentage distribution:'
SELECT
    CASE
        WHEN coverage_pct < 10 THEN '0-10%'
        WHEN coverage_pct < 25 THEN '10-25%'
        WHEN coverage_pct < 50 THEN '25-50%'
        WHEN coverage_pct < 75 THEN '50-75%'
        WHEN coverage_pct < 100 THEN '75-100%'
        WHEN coverage_pct <= 125 THEN '100-125% (island aggregation)'
        ELSE '> 125% (review needed)'
    END as coverage_range,
    CASE
        WHEN coverage_pct < 10 THEN 1
        WHEN coverage_pct < 25 THEN 2
        WHEN coverage_pct < 50 THEN 3
        WHEN coverage_pct < 75 THEN 4
        WHEN coverage_pct < 100 THEN 5
        WHEN coverage_pct <= 125 THEN 6
        ELSE 7
    END as sort_key,
    COUNT(*) as mapping_count,
    ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM fsa_municipality_mappings), 1) as percentage
FROM fsa_municipality_mappings
GROUP BY coverage_range, sort_key
ORDER BY sort_key;

\echo ''

-- ==============================================================================
-- 6. NAR ADDRESS COUNT VALIDATION
-- ==============================================================================

\echo '6. Verifying NAR address counts...'
\echo ''

WITH nar_total AS (
    SELECT COUNT(*) as total
    FROM nar_addresses
    WHERE prov_code = 35
      AND fsa_code IS NOT NULL
      AND csd_eng_name IS NOT NULL
),
mapping_total AS (
    SELECT SUM(address_count) as total
    FROM fsa_municipality_mappings
)
SELECT
    'NAR addresses (with FSA+city)' as source,
    n.total as count
FROM nar_total n
UNION ALL
SELECT
    'Mapping address sum',
    m.total
FROM mapping_total m
UNION ALL
SELECT
    'Difference (due to normalization)',
    ABS(n.total - m.total)
FROM nar_total n, mapping_total m;

\echo ''

-- Mappings with vs without NAR addresses
SELECT
    'Mappings with NAR addresses' as metric,
    COUNT(*) as count,
    ROUND(COUNT(*) * 100.0 / NULLIF((SELECT COUNT(*) FROM fsa_municipality_mappings), 0), 1) as percentage
FROM fsa_municipality_mappings
WHERE address_count > 0
UNION ALL
SELECT
    'Mappings without NAR addresses (boundary-only)',
    COUNT(*),
    ROUND(COUNT(*) * 100.0 / NULLIF((SELECT COUNT(*) FROM fsa_municipality_mappings), 0), 1)
FROM fsa_municipality_mappings
WHERE address_count = 0 OR address_count IS NULL;

\echo ''

-- ==============================================================================
-- 7. MUNICIPALITY ALIAS VALIDATION
-- ==============================================================================

\echo '7. Verifying municipality aliases...'
\echo ''

SELECT
    'Total aliases' as metric,
    COUNT(*) as count
FROM municipality_aliases
UNION ALL
SELECT
    alias_type,
    COUNT(*)
FROM municipality_aliases
GROUP BY alias_type
ORDER BY count DESC;

\echo ''

-- Test normalization function
\echo '  Testing normalization function:'
SELECT
    'Greater Sudbury / Grand Sudbury' as input,
    normalize_municipality_name('Greater Sudbury / Grand Sudbury') as normalized,
    (SELECT canonical_name FROM municipality_aliases WHERE normalized_alias = normalize_municipality_name('Greater Sudbury / Grand Sudbury') LIMIT 1) as canonical
UNION ALL
SELECT
    'Prince Edward County',
    normalize_municipality_name('Prince Edward County'),
    (SELECT canonical_name FROM municipality_aliases WHERE normalized_alias = normalize_municipality_name('Prince Edward County') LIMIT 1)
UNION ALL
SELECT
    '  Toronto  ',
    normalize_municipality_name('  Toronto  '),
    (SELECT canonical_name FROM municipality_aliases WHERE normalized_alias = normalize_municipality_name('  Toronto  ') LIMIT 1)
UNION ALL
SELECT
    'West Nipissing / Nipissing Ouest',
    normalize_municipality_name('West Nipissing / Nipissing Ouest'),
    (SELECT canonical_name FROM municipality_aliases WHERE normalized_alias = normalize_municipality_name('West Nipissing / Nipissing Ouest') LIMIT 1);

\echo ''

-- ==============================================================================
-- 8. FSA-MUNICIPALITY RELATIONSHIP PATTERNS
-- ==============================================================================

\echo '8. Analyzing FSA-municipality relationship patterns...'
\echo ''

-- FSAs per municipality distribution
\echo '  FSAs per municipality distribution:'
SELECT
    CASE
        WHEN fsa_count = 1 THEN '1 FSA'
        WHEN fsa_count BETWEEN 2 AND 5 THEN '2-5 FSAs'
        WHEN fsa_count BETWEEN 6 AND 20 THEN '6-20 FSAs'
        WHEN fsa_count BETWEEN 21 AND 50 THEN '21-50 FSAs'
        ELSE '> 50 FSAs (major city)'
    END as range,
    CASE
        WHEN fsa_count = 1 THEN 1
        WHEN fsa_count BETWEEN 2 AND 5 THEN 2
        WHEN fsa_count BETWEEN 6 AND 20 THEN 3
        WHEN fsa_count BETWEEN 21 AND 50 THEN 4
        ELSE 5
    END as sort_key,
    COUNT(*) as municipality_count,
    ROUND(AVG(fsa_count), 1) as avg_fsas_in_range
FROM (
    SELECT municipal_id, COUNT(DISTINCT fsa_code) as fsa_count
    FROM fsa_municipality_mappings
    WHERE municipal_id IS NOT NULL
    GROUP BY municipal_id
) sub
GROUP BY range, sort_key
ORDER BY sort_key;

\echo ''

-- Municipalities per FSA distribution
\echo '  Municipalities per FSA distribution:'
SELECT
    CASE
        WHEN muni_count = 1 THEN '1 municipality (exclusive)'
        WHEN muni_count BETWEEN 2 AND 3 THEN '2-3 municipalities'
        WHEN muni_count BETWEEN 4 AND 10 THEN '4-10 municipalities'
        ELSE '> 10 municipalities (complex)'
    END as range,
    CASE
        WHEN muni_count = 1 THEN 1
        WHEN muni_count BETWEEN 2 AND 3 THEN 2
        WHEN muni_count BETWEEN 4 AND 10 THEN 3
        ELSE 4
    END as sort_key,
    COUNT(*) as fsa_count,
    ROUND(COUNT(*) * 100.0 / NULLIF((SELECT COUNT(DISTINCT fsa_code) FROM fsa_municipality_mappings), 0), 1) as percentage
FROM (
    SELECT fsa_code, COUNT(*) as muni_count
    FROM fsa_municipality_mappings
    GROUP BY fsa_code
) sub
GROUP BY range, sort_key
ORDER BY sort_key;

\echo ''

-- ==============================================================================
-- 9. TOP FSA-MUNICIPALITY COMBINATIONS
-- ==============================================================================

\echo '9. Top FSA-municipality combinations by NAR addresses...'
\echo ''

SELECT
    ROW_NUMBER() OVER (ORDER BY address_count DESC) as rank,
    fsa_code,
    municipality_name_short,
    address_count,
    ROUND(coverage_pct, 1) as coverage_pct,
    CASE WHEN is_primary THEN 'YES' ELSE 'no' END as is_primary
FROM fsa_municipality_mappings
WHERE address_count > 0
ORDER BY address_count DESC
LIMIT 15;

\echo ''

-- ==============================================================================
-- 10. POTENTIAL ISSUES
-- ==============================================================================

\echo '10. Checking for potential issues...'
\echo ''

-- Issue 1: FSAs with suspiciously many municipalities
\echo '  FSAs with > 20 municipalities (may need review):'
SELECT
    fsa_code,
    COUNT(*) as municipality_count,
    STRING_AGG(municipality_name_short, ', ' ORDER BY coverage_pct DESC) FILTER (WHERE coverage_pct > 10) as top_municipalities
FROM fsa_municipality_mappings
GROUP BY fsa_code
HAVING COUNT(*) > 20
ORDER BY COUNT(*) DESC
LIMIT 5;

\echo ''

-- Issue 2: Municipalities with very low coverage in their FSAs
\echo '  Mappings with very low coverage (< 1%, possible errors):'
SELECT
    fsa_code,
    municipality_name_short,
    ROUND(coverage_pct, 3) as coverage_pct,
    address_count
FROM fsa_municipality_mappings
WHERE coverage_pct < 1 AND coverage_pct > 0
ORDER BY coverage_pct ASC
LIMIT 10;

\echo ''

-- Issue 3: Aliases without canonical matches
\echo '  Aliases with no canonical match in municipalities:'
SELECT
    a.alias,
    a.canonical_name,
    a.alias_type,
    CASE
        WHEN EXISTS (
            SELECT 1 FROM lower_municipal_boundaries m
            WHERE normalize_municipality_name(m.short_name) = normalize_municipality_name(a.canonical_name)
        ) THEN 'EXISTS'
        ELSE 'NOT FOUND'
    END as canonical_exists
FROM municipality_aliases a
WHERE NOT EXISTS (
    SELECT 1 FROM lower_municipal_boundaries m
    WHERE normalize_municipality_name(m.short_name) = normalize_municipality_name(a.canonical_name)
);

\echo ''

-- ==============================================================================
-- SUMMARY
-- ==============================================================================

\echo '================================================================================'
\echo 'VERIFICATION SUMMARY'
\echo '================================================================================'
\echo ''
\echo 'If all checks passed:'
\echo '  ✓ All Ontario FSAs have municipality mappings'
\echo '  ✓ Each FSA has exactly one primary municipality'
\echo '  ✓ Coverage percentages are reasonable'
\echo '  ✓ NAR address counts match expectations'
\echo '  ✓ Municipality aliases are properly configured'
\echo ''
\echo 'The FSA-municipality mapping system is ready for use in geocoding!'
\echo '================================================================================'
