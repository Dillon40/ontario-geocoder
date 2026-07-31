# Municipal Dataset Coverage Testing

Tools for testing municipal datasets against NAR's missing geocodes to predict enrichment value before importing.

## Overview

This system helps you determine if a new municipal dataset is worth importing by:
1. Comparing it against NAR addresses that are missing coordinates
2. Predicting match rates and coverage
3. Generating detailed reports with recommendations

## Components

### 1. `data_sourcing/enrichment/add_fsa_to_dataset.py`
Adds FSA (Forward Sortation Area) codes to datasets using PostGIS spatial lookup.

**When to use:**
- Dataset has coordinates but no postal codes
- Want to enable FSA-level matching for better coverage

**Usage:**
```bash
./data_sourcing/enrichment/add_fsa_to_dataset.py \
    --input toronto_addresses.geojson \
    --output toronto_addresses_with_fsa.geojson
```

**What it does:**
- Uses PostGIS ST_Contains() with fsa_boundaries table
- Adds 3-character `fsa_code` field (e.g., "M4K", "K1A")
- Reports statistics on FSA assignment success rate

### 2. `data_sourcing/scripts/analyze_municipal_dataset_coverage.py`
Tests municipal datasets against NAR missing geocodes.

**When to use:**
- Evaluate if a municipal dataset is worth importing
- Predict coverage and match rates
- Generate detailed analysis reports

**Usage:**

For datasets with FSA codes (recommended workflow):
```bash
# Step 1: Add FSA codes
./data_sourcing/enrichment/add_fsa_to_dataset.py \
    --input data.geojson \
    --output data_with_fsa.geojson

# Step 2: Test coverage
./data_sourcing/scripts/analyze_municipal_dataset_coverage.py \
    --dataset data_with_fsa.geojson \
    --config toronto \
    --sample 0  # 0 = full analysis
```

For datasets with postal codes already:
```bash
./data_sourcing/scripts/analyze_municipal_dataset_coverage.py \
    --dataset data.geojson \
    --config guelph \
    --sample 10000
```

For dual datasets (addresses + parcels):
```bash
./data_sourcing/scripts/analyze_municipal_dataset_coverage.py \
    --dataset-addresses addresses.geojson \
    --dataset-parcels parcels.geojson \
    --config guelph
```

## Matching Strategies

### FSA-Based Matching (Recommended)
**Fields used:** civic_no + street_name + fsa_code

**Advantages:**
- Ignores inconsistent fields (street_type, street_dir)
- Achieves 90%+ coverage for well-aligned datasets
- Works well when postal codes aren't available

**Toronto Example Results:**
- 525,409 municipal addresses tested
- 131,924 NAR Toronto addresses without coords
- **93.5% coverage** (123,308 matches)
  - 115,671 unique matches (87.7%)
  - 7,637 ambiguous matches (5.8%)

### Full Postal Code Matching
**Fields used:** All available (civic_no, street_name, street_type, street_dir, postal_code)

**Advantages:**
- More precise matching
- Higher confidence scores

**Use when:**
- Dataset has complete, accurate postal codes
- Want maximum precision over coverage

## Municipality Alias Matching

The system automatically resolves municipality name variations:

**Toronto Pre-Amalgamation Names:**
- Etobicoke → TORONTO
- North York → TORONTO
- Scarborough → TORONTO
- York → TORONTO
- East York → TORONTO
- former Toronto → TORONTO

**Bilingual Names:**
- Greater Sudbury / Grand Sudbury → GREATER SUDBURY
- French River / Rivière des Français → FRENCH RIVER

This is handled automatically using the `municipality_aliases` table.

## Configuration

### Dataset Mappings (`config/dataset-mappings.json`)

Define field mappings for your dataset:

```json
{
  "toronto": {
    "municipality": "City of Toronto",
    "province": "ON",
    "postal_prefixes": ["M1A", "M1B", "M1C", ...],
    "field_mappings": {
      "street_number": "LO_NUM",
      "civic_no_suffix": "LO_NUM_SUF",
      "street_name": "LINEAR_NAME",
      "street_suffix": "LINEAR_NAME_TYPE",
      "street_direction": "LINEAR_NAME_DIR"
    }
  }
}
```

## Reports Generated

### 1. Coverage Summary (`*_coverage_summary.txt`)
- Overall statistics
- Match breakdown (unique/ambiguous/no-match)
- Enrichment potential
- Recommendation (HIGH/MEDIUM/LOW value)

### 2. Match Breakdown (`*_match_breakdown.csv`)
- Detailed results for each NAR record
- Match counts and confidence scores
- Municipal matches found

### 3. Geographic Coverage (`*_geographic_coverage.csv`)
- FSA coverage analysis
- Municipality alignment
- Geographic data quality warnings

### 4. Sample Matches (`*_sample_matches.json`)
- Example matched records
- Useful for validation and debugging

## Common Workflows

### Testing a New Toronto Dataset
```bash
# Add FSA codes
./data_sourcing/enrichment/add_fsa_to_dataset.py \
    --input toronto_new_addresses.geojson \
    --output toronto_new_with_fsa.geojson

# Test coverage (full analysis)
./data_sourcing/scripts/analyze_municipal_dataset_coverage.py \
    --dataset toronto_new_with_fsa.geojson \
    --config toronto \
    --sample 0

# Review reports in ./reports/ directory
```

### Quick Test with Sample
```bash
# Test with 10,000 random NAR records for quick feedback
./data_sourcing/scripts/analyze_municipal_dataset_coverage.py \
    --dataset data.geojson \
    --config guelph \
    --sample 10000
```

### Creating New Configuration
```bash
# Script will detect missing config and guide you
./data_sourcing/scripts/analyze_municipal_dataset_coverage.py \
    --dataset new_city.geojson \
    --config new_city_name

# For dual datasets, wizard will help create join configuration
./data_sourcing/scripts/analyze_municipal_dataset_coverage.py \
    --dataset-addresses addresses.geojson \
    --dataset-parcels parcels.geojson \
    --config new_city_name
```

## Interpreting Results

### Coverage Thresholds
- **90%+ coverage**: HIGH VALUE - Import recommended
- **70-90% coverage**: MEDIUM VALUE - Consider importing
- **<70% coverage**: LOW VALUE - May not be worth the effort

### Confidence Scores
- **0.90-1.0**: High confidence (FSA-based unique match)
- **0.80-0.90**: Medium-high confidence (FSA-based 2-3 matches)
- **0.70-0.80**: Medium confidence (FSA-based 4-5 matches)
- **<0.70**: Lower confidence (many matches or no FSA)

## Tips

1. **Always add FSA codes for datasets without postal codes** - Dramatically improves match rates
2. **Use --sample for initial testing** - Faster feedback (10,000 is good for quick tests)
3. **Review geographic warnings** - Indicates potential data quality issues
4. **Check sample matches** - Validates matching is working correctly
5. **Compare postal_prefixes in config vs data** - Flags bad data in municipal dataset

## Troubleshooting

### Low Match Rates
- Check if dataset needs FSA codes added
- Verify field mappings in config
- Review sample matches to see why addresses don't match
- Check for municipality name mismatches

### "None" String Values
- System automatically handles "None" strings in data
- Common in municipal GeoJSON exports
- Converted to NULL during normalization

### Missing Coordinates in Municipal Dataset
- Use `data_sourcing/enrichment/add_fsa_to_dataset.py` to assign FSAs
- Requires latitude/longitude fields in source data
- FSA assignment uses PostGIS spatial containment

## Database Requirements

- PostgreSQL with PostGIS extension
- Tables required:
  - `nar_addresses` - NAR address data
  - `nar_locations` - NAR coordinate data
  - `fsa_boundaries` - FSA polygon boundaries
  - `municipality_aliases` - Municipality name variations
- Database functions:
  - `normalize_municipality_name()` - Municipality name normalization
