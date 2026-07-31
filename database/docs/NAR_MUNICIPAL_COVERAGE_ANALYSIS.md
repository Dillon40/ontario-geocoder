# NAR Municipal Coverage Analysis

**Date**: 2025-11-15
**Analysis Script**: `check_nar_municipal_coverage.py`

> **Note**: This is a point-in-time analysis. The boundary-table columns referenced
> below have since been renamed (`NM_SHORT_E` → `short_name`, `NAME_E` → `name`);
> the current script uses the new names.

## Executive Summary

Analysis of NAR city names (`csd_eng_name`) against Ontario municipal boundaries revealed that with proper normalization, **99.29% of NAR addresses can be matched** to municipal boundary data, significantly higher than the initial 97.80% direct match rate.

## Initial Findings (No Normalization)

### Coverage Statistics
- **Total NAR addresses with city names**: 794,456
- **Matched addresses**: 776,992 (97.80%)
- **Unmatched addresses**: 17,464 (2.20%)
- **Unmatched unique city names**: 105

### Top Unmatched Cities (Initial)
| City Name | Count | Issue Type |
|-----------|-------|------------|
| Greater Sudbury / Grand Sudbury | 9,960 | Bilingual naming |
| Prince Edward County | 1,805 | County suffix mismatch |
| West Nipissing / Nipissing Ouest | 972 | Bilingual naming |
| The Nation / La Nation | 660 | Bilingual naming |
| Algoma, Unorganized, North Part | 463 | Unorganized territory |
| Thunder Bay, Unorganized | 337 | Unorganized territory |
| French River / Rivière des Français | 246 | Bilingual naming |

## Root Cause Analysis

### Naming Pattern Mismatches

1. **Bilingual Names** (Largest impact)
   - **NAR format**: `Greater Sudbury / Grand Sudbury`
   - **Municipal boundaries**: `GREATER SUDBURY` (NM_SHORT_E)
   - **Impact**: ~12,000 addresses

2. **County Name Variations**
   - **Pattern 1**: NAR has `NORFOLK COUNTY` → Database has `NORFOLK COUNTY` ✓ (matches)
   - **Pattern 2**: NAR has `PRINCE EDWARD COUNTY` → Database has `PRINCE EDWARD` ✗ (no match)
   - **Impact**: ~1,805 addresses

3. **"(Part)" Suffix**
   - **NAR format**: `Six Nations (Part) 40`
   - **Database**: May or may not include "(Part)"
   - **Impact**: ~500 addresses

4. **Unorganized Territories**
   - **NAR format**: `Thunder Bay, Unorganized`, `Algoma, Unorganized, North Part`
   - **Database**: No unorganized territories (0 matches)
   - **Impact**: ~2,000 addresses
   - **Note**: These are legitimately not municipal entities

5. **First Nations Reserves**
   - **NAR format**: Various reserve names (e.g., `Six Nations (Part) 40`, `Wikwemikong Unceded`)
   - **Database**: No First Nations reserves (0 matches)
   - **Impact**: ~2,000 addresses
   - **Note**: Reserves are not Ontario municipalities

## Normalization Strategy

### Implemented Rules

```sql
-- Remove bilingual French translation (after " / ")
REGEXP_REPLACE(csd_eng_name, ' / .*$', '')

-- Remove "(Part)" suffix
REGEXP_REPLACE(csd_eng_name, ' \\(Part\\).*$', '')

-- Apply case-insensitive matching
UPPER(TRIM(normalized_name))
```

### Rules NOT Applied

These were tested but removed as they caused mismatches:
- ~~Remove " County" suffix~~ - Causes issues with counties like Norfolk County vs Prince Edward County
- ~~Remove ", Unorganized" suffix~~ - Not needed; these legitimately aren't municipalities

## Results After Normalization

### Coverage Statistics
- **Total NAR addresses**: 794,456
- **Matched addresses**: 788,830 (99.29%)
- **Unmatched addresses**: 5,626 (0.71%)
- **Improvement**: +11,838 addresses matched (+1.49%)

### Match Performance by Column

| Column Name | Matched Cities | Total Addresses | Notes |
|-------------|----------------|-----------------|-------|
| NM_SHORT_E | 405 | 776,992 | Primary match source |
| NAME_E | 2 | 6,808 | Catches formal names |
| UPR_TIER_E | 0 | 0 | No city-level matches (as expected) |

## Remaining Unmatched (>50 addresses)

| City Name | Normalized Name | Count | Category |
|-----------|-----------------|-------|----------|
| Prince Edward County | PRINCE EDWARD COUNTY | 1,805 | County naming variant |
| Algoma, Unorganized, North Part | ALGOMA, UNORGANIZED, NORTH PART | 463 | Unorganized territory |
| Thunder Bay, Unorganized | THUNDER BAY, UNORGANIZED | 337 | Unorganized territory |
| Parry Sound, Unorganized, Centre Part | PARRY SOUND, UNORGANIZED, CENTRE PART | 212 | Unorganized territory |
| Kenora, Unorganized | KENORA, UNORGANIZED | 177 | Unorganized territory |
| Six Nations (Part) 40 | SIX NATIONS | 158 | First Nations reserve |
| Wikwemikong Unceded | WIKWEMIKONG UNCEDED | 153 | First Nations reserve |
| Saugeen 29 | SAUGEEN 29 | 152 | First Nations reserve |
| Timiskaming, Unorganized, West Part | TIMISKAMING, UNORGANIZED, WEST PART | 138 | Unorganized territory |
| Tyendinaga Mohawk Territory | TYENDINAGA MOHAWK TERRITORY | 129 | First Nations reserve |
| Nipissing, Unorganized, North Part | NIPISSING, UNORGANIZED, NORTH PART | 129 | Unorganized territory |
| Sudbury, Unorganized, North Part | SUDBURY, UNORGANIZED, NORTH PART | 125 | Unorganized territory |
| Nipissing 10 | NIPISSING 10 | 105 | First Nations reserve |
| Akwesasne | AKWESASNE | 85 | First Nations reserve |
| Kettle Point 44 | KETTLE POINT 44 | 84 | First Nations reserve |
| Walpole Island 46 | WALPOLE ISLAND 46 | 74 | First Nations reserve |
| Christian Island 30 | CHRISTIAN ISLAND 30 | 66 | First Nations reserve |
| Cochrane, Unorganized, North Part | COCHRANE, UNORGANIZED, NORTH PART | 63 | Unorganized territory |
| Garden River 14 | GARDEN RIVER 14 | 60 | First Nations reserve |
| Oneida 41 | ONEIDA 41 | 57 | First Nations reserve |
| Gordon/Barrie Island | GORDON/BARRIE ISLAND | 54 | Unorganized territory |

**Total from above**: ~4,625 addresses (82.2% of remaining unmatched)

### Breakdown by Category

| Category | Addresses | % of Unmatched | Legitimacy |
|----------|-----------|----------------|------------|
| Unorganized Territories | ~2,000 | 35.5% | Legitimate - not municipalities |
| First Nations Reserves | ~2,000 | 35.5% | Legitimate - not municipalities |
| Prince Edward County | 1,805 | 32.1% | Fixable with special rule |
| Other/Small reserves | <200 | 3.6% | Legitimate - not municipalities |

## Key Validation Examples

### Successfully Matched (After Normalization)

| NAR City Name | Normalized | Municipal Boundary Match | Type |
|---------------|------------|--------------------------|------|
| Greater Sudbury / Grand Sudbury | GREATER SUDBURY | GREATER SUDBURY | Single Tier |
| West Nipissing / Nipissing Ouest | WEST NIPISSING | WEST NIPISSING | Single Tier |
| The Nation / La Nation | THE NATION | THE NATION | Lower Tier |
| French River / Rivière des Français | FRENCH RIVER | FRENCH RIVER | Single Tier |
| Norfolk County | NORFOLK COUNTY | NORFOLK COUNTY | Single Tier |
| Haldimand County | HALDIMAND COUNTY | HALDIMAND COUNTY | Single Tier |

### Database Entries Confirmed

```sql
-- Greater Sudbury
NAME_E: CITY OF GREATER SUDBURY
NM_SHORT_E: GREATER SUDBURY
MUN_TYPE_E: Single Tier Municipality

-- Prince Edward (mismatch example)
NAME_E: COUNTY OF PRINCE EDWARD
NM_SHORT_E: PRINCE EDWARD
NAR has: PRINCE EDWARD COUNTY (doesn't match either)

-- Norfolk County (works)
NAME_E: NORFOLK COUNTY
NM_SHORT_E: NORFOLK COUNTY
NAR has: NORFOLK COUNTY ✓
```

## Recommendations

### 1. Implement Normalization in Geocoding Pipeline

Apply the following normalization to NAR `csd_eng_name` before matching:

```python
def normalize_nar_city(city_name):
    """Normalize NAR city name for municipal boundary matching."""
    if not city_name:
        return None

    # Remove bilingual French part (after " / ")
    normalized = re.sub(r' / .*$', '', city_name)

    # Remove "(Part)" suffix
    normalized = re.sub(r' \(Part\).*$', '', normalized)

    # Uppercase and trim
    normalized = normalized.upper().strip()

    return normalized
```

### 2. Special Handling for Prince Edward County

Add a lookup table or special case:
```python
SPECIAL_CASES = {
    'PRINCE EDWARD COUNTY': 'PRINCE EDWARD'
}
```

### 3. Flag Unorganized Territories and Reserves

Create separate handling for:
- Names containing ", Unorganized"
- Names ending with numeric reserve codes (e.g., "40", "29", "10")
- Known First Nations territories

These should be flagged as "valid but not municipal" rather than errors.

### 4. Match Priority Order

When matching NAR cities to municipal boundaries:

1. Try exact match on `NM_SHORT_E` (normalized)
2. Try exact match on `NAME_E` (normalized)
3. Try special case mappings
4. Check if unorganized territory or reserve
5. Flag as unmatched only if none apply

### 5. Monitoring

Track match rates over time:
- Expected match rate: ~99.3% for municipal addresses
- ~0.5% legitimately non-municipal (reserves, unorganized)
- <0.2% true mismatches needing investigation

## Technical Details

### Municipal Boundaries Table
- **Table**: `lower_municipal_boundaries`
- **Source**: Ontario GeoHub (updated 2025-01-22)
- **Records**: 685 municipalities
- **Unique names**: 414 (NM_SHORT_E)
- **CRS**: EPSG:4326 (WGS84)

### NAR Data
- **Table**: `nar_addresses`
- **City field**: `csd_eng_name` (Census Subdivision English name)
- **Total records with cities**: 794,456
- **Unique city values**: 510

### Normalization Query Template

```sql
WITH nar_cities AS (
    SELECT
        csd_eng_name,
        UPPER(TRIM(
            REGEXP_REPLACE(
                REGEXP_REPLACE(
                    csd_eng_name,
                    ' / .*$', ''  -- Remove bilingual
                ),
                ' \(Part\).*$', ''  -- Remove (Part)
            )
        )) as normalized_name
    FROM nar_addresses
    WHERE csd_eng_name IS NOT NULL
      AND csd_eng_name != ''
),
muni_names AS (
    SELECT DISTINCT UPPER(TRIM("NM_SHORT_E")) as name
    FROM lower_municipal_boundaries
    WHERE "NM_SHORT_E" IS NOT NULL

    UNION

    SELECT DISTINCT UPPER(TRIM("NAME_E")) as name
    FROM lower_municipal_boundaries
    WHERE "NAME_E" IS NOT NULL
)
SELECT
    n.csd_eng_name,
    n.normalized_name,
    m.name as matched_name,
    CASE WHEN m.name IS NOT NULL THEN true ELSE false END as is_matched
FROM nar_cities n
LEFT JOIN muni_names m ON n.normalized_name = m.name;
```

## Conclusion

The analysis confirms that the vast majority of NAR city names can be successfully matched to Ontario municipal boundaries with simple normalization rules. The remaining 0.71% of unmatched addresses are primarily:

1. **Legitimate non-matches**: Unorganized territories and First Nations reserves that are not Ontario municipalities
2. **Edge cases**: Minor naming variations (e.g., Prince Edward County) that can be handled with special rules

**Bottom line**: With the implemented normalization, the geocoding system can reliably match 99.29% of NAR addresses to municipal boundaries, with the remaining addresses properly categorized rather than lost.
