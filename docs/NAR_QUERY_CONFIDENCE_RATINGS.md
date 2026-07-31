# NAR Query Confidence Ratings

## Purpose
Assign confidence ratings to NAR database queries based on which address fields are available in the input data.

## Data Source
Analysis of NAR Ontario dataset: **565,644 total addresses**

---

## NAR Data Quality Assessment

### Overview of Duplicates

| Category | Count | % of Total | % of Duplicates |
|----------|-------|------------|-----------------|
| **Unique (postal+civic)** | 521,006 | 92.11% | N/A |
| **Duplicates (postal+civic)** | 44,638 | 7.89% | 100% |
| ↳ Condo Towers | 5,963 | 1.05% | 13.36% |
| ↳ Resolvable Streets | ~14,707 | 2.6% | ~33% |
| ↳ OLD Problematic* | 23,556 | 4.16% | ~53% |

\* *OLD Problematic cases were resolved by adding `street_type` and `apt_no_label` to the matching logic*

---

## Duplicate Resolution Methods

### 1. Condo Towers (5,963 addresses)
**Resolvable** if they have:
- ✓ Same coordinates
- ✓ Same street name
- ✓ Different unit numbers (`apt_no_label`)
- ✓ Coordinates present

**Example:**
```
K1A2B3 + 100 Main St, Unit 501, Ottawa (43.123, -79.456)
K1A2B3 + 100 Main St, Unit 502, Ottawa (43.123, -79.456)
K1A2B3 + 100 Main St, Unit 503, Ottawa (43.123, -79.456)
```
**Resolution:** Unique with `apt_no_label`

---

### 2. Resolvable Streets (~14,707 addresses)
**Resolvable** with unique combination of:
- `street_name`
- `street_type`
- `street_dir`
- `city` (csd_eng_name)
- `civic_suffix`
- `apt_no_label`

#### Field Effectiveness Analysis (tested on 2,958 ambiguous cases):

| Field Combination | Resolution Rate | Addresses Resolved |
|-------------------|----------------|-------------------|
| `street_name` alone | 93.51% | 2,766 / 2,958 |
| `street_name` + `street_dir` + `city` + `civic_suffix` | 94.22% | 2,787 / 2,958 |
| All 6 fields above | ~100% | Nearly all |

**Key Finding:** `street_name` is the single most powerful disambiguating field (93.51% effectiveness)

**Example:**
```
K1A2B3 + 100 Main STREET North, Ottawa
K1A2B3 + 100 Main AVENUE North, Ottawa
```
**Resolution:** `street_type` distinguishes them

---

### 3. OLD Problematic Cases (23,556 addresses) - NOW RESOLVED
These cases were previously unresolvable because when grouped by:
- `postal_code`
- `civic_no`
- `street_name`
- `street_dir`
- `city`
- `civic_suffix`

All addresses had **identical values** (count > 1 for all groups).

**Example of OLD Problematic:**
```
K1A2B3 + 100 Main St N, Ottawa (no suffix)
K1A2B3 + 100 Main St N, Ottawa (no suffix)
K1A2B3 + 100 Main St N, Ottawa (no suffix)
```

**Resolution:** Adding `street_type` and `apt_no_label` resolved these cases, making them distinguishable.

---

## Confidence Rating Formula

### Baseline Confidence by Fields Available

| Input Fields Available | Unique Match Rate | Confidence Score | Expected Results |
|------------------------|-------------------|------------------|------------------|
| **Minimal: postal + civic** | 92.11% | **0.92** | 1 result (92% of time)<br>2-5 results (8% of time) |
| **Basic: postal + civic + street_name** | ~96-97%* | **0.96** | 1 result (most cases)<br>2-3 results (rare) |
| **Standard: postal + civic + street_name + street_type + city** | ~98%* | **0.98** | 1-2 results |
| **Complete: All 8 fields** | ~99.9%* | **0.99** | 1 result (nearly guaranteed) |

\* *Estimated based on 92.11% base rate + resolution of duplicates*

### The 8 Fields for Complete Address Match:
1. `mail_postal_code`
2. `civic_no`
3. `official_street_name`
4. `official_street_type` ← Critical for disambiguation
5. `official_street_dir`
6. `csd_eng_name` (city)
7. `civic_no_suffix`
8. `apt_no_label` ← Critical for condo towers

---

## Confidence Adjustments

### Boost Confidence (+0.02 to +0.05):
- **Single result returned:** +0.05
  - If query returns exactly 1 result, confidence increases
- **Coordinates present in result:** +0.02
  - NAR record has coordinates (only 18.69% have coords)

### Lower Confidence (-0.05 to -0.10):
- **Multiple results returned (2-5):** -0.05
  - Query matched multiple records
- **Many results (6+):** -0.10
  - Highly ambiguous query
- **No coordinates in result:** -0.02
  - NAR record missing geocode data

---

## Implementation Examples

### Example 1: Minimal Query (Postal + Civic Only)
```python
input_query = {
    'postal': 'K1A2B3',
    'civic': '100'
}

# Base confidence: 0.92 (92.11% unique rate)
base_confidence = 0.92

results = query_nar(input_query)

if len(results) == 1:
    final_confidence = 0.92 + 0.05  # = 0.97
elif len(results) <= 5:
    final_confidence = 0.92 - 0.05  # = 0.87
else:
    final_confidence = 0.92 - 0.10  # = 0.82

return {
    'matches': results,
    'confidence': final_confidence,
    'reason': 'Postal+civic only - may have duplicates'
}
```

### Example 2: Standard Query (With Street Name)
```python
input_query = {
    'postal': 'K1A2B3',
    'civic': '100',
    'street_name': 'MAIN',
    'city': 'OTTAWA'
}

# Base confidence: 0.96 (street_name resolves 93.51% of duplicates)
base_confidence = 0.96

results = query_nar(input_query)

if len(results) == 1:
    final_confidence = 0.96 + 0.05  # = 1.00 (capped at 0.99)
    final_confidence = min(0.99, final_confidence)
else:
    final_confidence = 0.96 - 0.05  # = 0.91

return {
    'matches': results,
    'confidence': final_confidence,
    'reason': 'Missing street_type - may have duplicates'
}
```

### Example 3: Complete Query (All 8 Fields)
```python
input_query = {
    'postal': 'K1A2B3',
    'civic': '100',
    'street_name': 'MAIN',
    'street_type': 'STREET',
    'street_dir': 'N',
    'city': 'OTTAWA',
    'civic_suffix': None,
    'apt_no': '501'
}

# Base confidence: 0.99 (nearly 100% unique with all fields)
base_confidence = 0.99

results = query_nar(input_query)

# With all 8 fields, should always return 1 result
if len(results) == 1:
    final_confidence = 0.99  # Already at max

return {
    'matches': results,
    'confidence': final_confidence,
    'reason': 'Complete address match - highly confident'
}
```

---

## API Response Format

### Recommended Response Structure:
```json
{
  "query": {
    "postal": "K1A2B3",
    "civic": "100",
    "street_name": "MAIN",
    "city": "OTTAWA"
  },
  "results": {
    "count": 2,
    "confidence": 0.91,
    "confidence_factors": {
      "base": 0.96,
      "adjustments": {
        "multiple_results": -0.05
      }
    },
    "addresses": [
      {
        "addr_guid": "abc-123",
        "full_address": "100 Main Street North, Ottawa, ON K1A2B3",
        "coordinates": {
          "latitude": 45.4215,
          "longitude": -75.6972
        },
        "has_coordinates": true
      },
      {
        "addr_guid": "def-456",
        "full_address": "100 Main Avenue North, Ottawa, ON K1A2B3",
        "coordinates": null,
        "has_coordinates": false
      }
    ]
  },
  "recommendation": "Query returned multiple results. Provide 'street_type' to disambiguate."
}
```

---

## Fuzzy Street Name Matching (NEW)

When exact matching methods fail, the NAR geocoder falls back to **fuzzy street name matching** using PostgreSQL's `pg_trgm` extension.

### How It Works

Uses trigram similarity to catch typos like:
- "Elizbeth" → "Elizabeth" (missing 'a')
- "Queenswei" → "Queensway" (wrong ending)
- "Chruchill" → "Churchill" (transposed letters)

### Requirements

| Field | Required |
|-------|----------|
| FSA (postal code prefix) | ✓ Yes |
| Civic number | ✓ Yes |
| Street name (with typo) | ✓ Yes |

### Confidence Calculation

```
confidence = 0.60 × similarity_score
```

| Similarity Score | Confidence | Match Quality |
|------------------|------------|---------------|
| 0.60 (minimum) | 0.36 | Marginal - use with caution |
| 0.70 | 0.42 | Acceptable |
| 0.80 | 0.48 | Good |
| 0.90 | 0.54 | Very good |
| 1.00 | 0.60 | Perfect (exact match) |

### Result Metadata

Fuzzy matches include additional fields:

```json
{
  "fuzzy_match": true,
  "street_similarity": 0.85,
  "input_street_name": "Elizbeth",
  "matched_street_name": "Elizabeth",
  "match_method": "fsa_street_fuzzy_nar"
}
```

### When Fuzzy Matching Runs

Fuzzy matching is **Method 4** in the NAR cascade:
1. Method 1: Postal + Civic + Components (0.80-0.90)
2. Method 2: Full Address String (0.70-0.80)
3. Method 3: Component Fuzzy (0.60-0.70)
4. **Method 4: FSA + Fuzzy Street (0.36-0.60)** ← Only runs if Methods 1-3 fail

---

## Key Insights

1. **92.11% of NAR addresses** are unique on postal+civic alone
2. **`street_name` is the most powerful field** - resolves 93.51% of ambiguous cases by itself
3. **`street_type` is critical** - "100 Main Street" vs "100 Main Avenue" are different addresses
4. **`apt_no_label` is critical** - distinguishes units in condo towers
5. **All 8 fields together** provide ~99.9% unique matching
6. **Fuzzy matching catches typos** - "Elizbeth" → "Elizabeth" with scaled confidence

---

## Confidence Rating Quick Reference

| Fields Provided | Confidence | When to Use |
|-----------------|-----------|-------------|
| postal + civic | 0.92 | Minimal data available, expect some duplicates |
| + street_name | 0.96 | Standard lookup, high confidence |
| + street_type + city | 0.98 | Very high confidence, few duplicates |
| All 8 fields | 0.99 | Maximum confidence, nearly guaranteed unique |
| **Fuzzy (FSA + civic + typo)** | **0.36-0.60** | Fallback for typos when exact matching fails |

---

## Monitoring & Validation

Track these metrics in production:
- Distribution of query types (how many fields users provide)
- Actual match rates vs predicted confidence
- Cases where confidence was high but multiple results returned
- User feedback on result quality
- **Fuzzy match frequency**: How often fuzzy matching is used as fallback
- **Fuzzy match accuracy**: Verify fuzzy matches are correct (sample validation)
- **Similarity score distribution**: Track range of similarity scores returned

Adjust confidence formulas based on real-world performance.
