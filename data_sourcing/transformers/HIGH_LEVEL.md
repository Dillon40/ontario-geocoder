# Transform Pipeline

High-level overview of the `data_sourcing/transformers/` module.

## Purpose

The transform pipeline processes address records during import, applying parsing, normalization, enrichment, and validation before database insertion. It handles the heterogeneous nature of municipal open data sources.

## Pipeline Flow Diagram

```
                              ┌─────────────────────────────────────────────────────┐
                              │              TRANSFORM PIPELINE                     │
                              └─────────────────────────────────────────────────────┘
                                                      │
    ┌──────────────┐                                  │
    │ Raw Record   │──────────────────────────────────┼─────────────────────────────────┐
    │ from Source  │                                  ▼                                 │
    └──────────────┘                     ┌────────────────────────┐                     │
          │                              │   TransformContext     │                     │
          │                              │  ┌──────────────────┐  │                     │
          │                              │  │ record: {...}    │  │                     │
          │                              │  │ warnings: []     │  │                     │
          │                              │  │ errors: []       │  │                     │
          │                              │  │ modifications: []│  │                     │
          │                              │  └──────────────────┘  │                     │
          │                              └───────────┬────────────┘                     │
          │                                          │                                  │
          │         ┌────────────────────────────────┼────────────────────────────────┐ │
          │         │                                ▼                                │ │
          │         │  ┌─────────────────────────────────────────────────────────┐    │ │
          │         │  │                   1. StreetNameParser                   │    │ │
          │         │  │                                                         │    │ │
          │         │  │   "CHURCHILL AVE N" → street_name: "Churchill"          │    │ │
          │         │  │                       street_suffix: "Avenue"           │    │ │
          │         │  │                       street_direction: "North"         │    │ │
          │         │  └─────────────────────────────┬───────────────────────────┘    │ │
          │         │                                │                                │ │
          │         │                                ▼                                │ │
          │         │  ┌─────────────────────────────────────────────────────────┐    │ │
          │         │  │                  2. CivicNumberParser                   │    │ │
          │         │  │                                                         │    │ │
          │         │  │   "100A" → street_number: "100", civic_no_suffix: "A"   │    │ │
          │         │  │   "15 1/2" → street_number: "15", civic_no_suffix: "1/2"│    │ │
          │         │  │   "100-102" → unchanged (address range, not suffix)     │    │ │
          │         │  └─────────────────────────────┬───────────────────────────┘    │ │
          │         │                                │                                │ │
          │         │                                ▼                                │ │
          │         │  ┌─────────────────────────────────────────────────────────┐    │ │
          │         │  │                   3. UnitNumberParser                   │    │ │
          │         │  │                                                         │    │ │
          │         │  │   "Unit 5 - 123 Main St" → unit_no: "5"                 │    │ │
          │         │  │   "123 Main St, Suite 200" → unit_no: "200"             │    │ │
          │         │  │   "5-123 Main St" → unit_no: "5" (if hyphenated enabled)│    │ │
          │         │  └─────────────────────────────┬───────────────────────────┘    │ │
          │         │                                │                                │ │
          │         │                                ▼                                │ │
          │         │  ┌─────────────────────────────────────────────────────────┐    │ │
          │         │  │                   4. FieldNormalizer                    │    │ │
          │         │  │                                                         │    │ │
          │         │  │   postal_code: "n1e 1a1" → "N1E1A1"                     │    │ │
          │         │  │   fsa_code: extracted "N1E" (if postal exists)          │    │ │
          │         │  │   province: "ontario" → "ON"                            │    │ │
          │         │  │   street_suffix: "ST" → "Street"                        │    │ │
          │         │  │   street_direction: "None" → null (Toronto cleanup)     │    │ │
          │         │  │   unit_no: "  5  " → "5" (trim whitespace)              │    │ │
          │         │  │   civic_no_suffix: "a" → "A" (uppercase)                │    │ │
          │         │  └─────────────────────────────┬───────────────────────────┘    │ │
          │         │                                │                                │ │
          │         │                                ▼                                │ │
          │         │  ┌─────────────────────────────────────────────────────────┐    │ │
          │         │  │                 5. MunicipalityMapper                   │    │ │
          │         │  │                                                         │    │ │
          │         │  │   "Etobicoke" → city: "Toronto"                         │    │ │
          │         │  │                 city_original: "Etobicoke"              │    │ │
          │         │  │   (default city/province from config if missing)        │    │ │
          │         │  └─────────────────────────────┬───────────────────────────┘    │ │
          │         │                                │                                │ │
          │         │                                ▼                                │ │
          │         │  ┌─────────────────────────────────────────────────────────┐    │ │
          │         │  │               6. FSAEnricher (async)                    │    │ │
          │         │  │                                                         │    │ │
          │         │  │   Spatial lookup: (lat, lon) → fsa_code: "K2C"          │    │ │
          │         │  │   Uses PostGIS ST_Contains against fsa_boundaries       │    │ │
          │         │  │   (skipped if fsa_code already exists)                  │    │ │
          │         │  └─────────────────────────────┬───────────────────────────┘    │ │
          │         │                                │                                │ │
          │         │                                ▼                                │ │
          │         │  ┌─────────────────────────────────────────────────────────┐    │ │
          │         │  │                  7. RecordValidator                     │    │ │
          │         │  │                                                         │    │ │
          │         │  │   Required: coordinates + address identifier            │    │ │
          │         │  │   Bounds check: Ontario lat/lon ranges                  │    │ │
          │         │  │   Postal code format validation (warnings only)         │    │ │
          │         │  └─────────────────────────────┬───────────────────────────┘    │ │
          │         │                                │                                │ │
          │         └────────────────────────────────┼────────────────────────────────┘ │
          │                                          │                                  │
          │                                          ▼                                  │
          │                              ┌────────────────────────┐                     │
          │                              │ TransformResult        │                     │
          │                              │ ┌────────────────────┐ │                     │
          │                              │ │ SUCCESS │ MODIFIED │ │                     │
          │                              │ │ SKIPPED │ ERROR    │ │                     │
          │                              │ └────────────────────┘ │                     │
          │                              └───────────┬────────────┘                     │
          │                                          │                                  │
          │                                          ▼                                  │
          │                              ┌────────────────────────┐                     │
          │                              │  Transformed Record    │─────────────────────┘
          │                              │  Ready for INSERT      │
          │                              └────────────────────────┘
          │
          └─── context tracks all modifications for dry-run preview
```

## Components

| File | Purpose |
|------|---------|
| `base.py` | Core abstractions: `TransformContext`, `TransformResult`, `BaseTransformer`, `AsyncTransformer` |
| `pipeline.py` | `TransformPipeline` orchestrator - chains transformers, handles sync/async execution |
| `street_name_parser.py` | Parse composite street names ("CHURCHILL AVE N" → components) |
| `civic_number_parser.py` | Extract civic suffixes from street numbers ("100A" → "100" + suffix "A") |
| `unit_number_parser.py` | Extract unit numbers from address fields ("Unit 5 - 123 Main St" → unit_no "5") |
| `field_normalizer.py` | Normalize suffix/direction/postal/province/unit_no/civic_suffix to canonical forms |
| `municipality_mapper.py` | Map pre-amalgamation cities to canonical names (loads from DB) |
| `fsa_enricher.py` | Async spatial FSA lookup using PostGIS `fsa_boundaries` table |
| `record_validator.py` | Validate required fields and coordinate bounds |
| `config_loader.py` | Load transform config from `dataset-mappings.json` |

## Key Classes

### TransformContext

Wraps a record and tracks all modifications, warnings, and errors:

```python
context = TransformContext(record={'street_name': 'MAIN ST'}, source_name='ottawa')
context.set_field('street_name', 'Main', 'StreetNameParser', reason='Parsed from MAIN ST')
context.add_warning('FSAEnricher', 'No FSA found for coordinates')
```

### TransformResult

Enum indicating transformer outcome:

| Value | Meaning |
|-------|---------|
| `SUCCESS` | Completed without changes |
| `MODIFIED` | Record was changed |
| `SKIPPED` | Transformer skipped (disabled or not applicable) |
| `ERROR` | Fatal error - record should be rejected |

### TransformPipeline

Orchestrates transformer chain:

```python
# Async (production - includes FSA spatial lookup)
pipeline = await TransformPipeline.for_source_async('ottawa', db_pool)
context = await pipeline.transform_async(record)

# Sync (testing - no DB required)
pipeline = TransformPipeline.for_source('ottawa')
context = pipeline.transform(record)
```

## Data Flow Examples

### Ottawa Address

```
Input:  {street_name: "CHURCHILL AVE N", street_number: "371"}

StreetNameParser:   street_name → "Churchill", suffix → "Avenue", direction → "North"
CivicNumberParser:  (no suffix in "371")
FieldNormalizer:    (no postal to normalize)
MunicipalityMapper: city → "Ottawa" (from default_city config)
FSAEnricher:        fsa_code → "K1A" (spatial lookup)
RecordValidator:    ✓ PASSED

Output: {street_name: "Churchill", street_suffix: "Avenue",
         street_direction: "North", city: "Ottawa", fsa_code: "K1A", ...}
```

### Toronto Address with Civic Suffix and Unit Number

```
Input:  {street_name: "Main", street_suffix: "ST", city: "Etobicoke",
         street_number: "100A", street_direction: "None",
         full_address: "Unit 5 - 100A Main St"}

StreetNameParser:   (disabled - Toronto already has components)
CivicNumberParser:  street_number "100A" → "100", civic_no_suffix → "A"
UnitNumberParser:   full_address "Unit 5 - 100A Main St" → unit_no → "5"
FieldNormalizer:    suffix "ST" → "Street", direction "None" → null
MunicipalityMapper: city "Etobicoke" → "Toronto", city_original → "Etobicoke"
FSAEnricher:        fsa_code → "M9V" (spatial lookup)
RecordValidator:    ✓ PASSED

Output: {street_name: "Main", street_suffix: "Street", street_number: "100",
         civic_no_suffix: "A", unit_no: "5", city: "Toronto",
         city_original: "Etobicoke", fsa_code: "M9V", ...}
```

### Guelph Address with Unit Number

```
Input:  {street_name: "Gordon", street_suffix: "ST", street_number: "50",
         unit_no: "Unit 5", postal_code: "N1H 4G8"}

StreetNameParser:   (disabled - Guelph has structured fields)
CivicNumberParser:  (no suffix in "50")
FieldNormalizer:    suffix "ST" → "Street", postal → "N1H4G8", fsa_code → "N1H",
                    unit_no "Unit 5" → "Unit 5" (or "5" if strip_unit_labels enabled)
MunicipalityMapper: (disabled - Guelph names are canonical)
FSAEnricher:        (disabled - postal codes in source)
RecordValidator:    ✓ PASSED

Output: {street_name: "Gordon", street_suffix: "Street", street_number: "50",
         unit_no: "Unit 5", postal_code: "N1H4G8", fsa_code: "N1H", ...}
```

## Configuration

Per-source transform settings in `config/dataset-mappings.json`:

```json
{
  "ottawa": {
    "transforms": {
      "parse_street_name": {"enabled": true, "source_field": "street_name"},
      "parse_civic_number": {"enabled": true, "passthrough_suffix": true},
      "normalize_fields": {"enabled": true},
      "map_municipality": {"enabled": true, "default_city": "Ottawa", "default_province": "ON"},
      "enrich_fsa": {"enabled": true},
      "validate": {"strict_bounds": "ontario"}
    }
  },
  "toronto": {
    "transforms": {
      "parse_street_name": {"enabled": false},
      "parse_civic_number": {"enabled": true, "passthrough_suffix": true},
      "parse_unit_number": {"enabled": true, "source_field": "full_address", "passthrough_unit": true},
      "normalize_fields": {"enabled": true},
      "map_municipality": {"enabled": true, "preserve_original": true},
      "enrich_fsa": {"enabled": true},
      "validate": {"strict_bounds": "ontario"}
    }
  },
  "guelph": {
    "transforms": {
      "parse_street_name": {"enabled": false},
      "parse_civic_number": {"enabled": true, "passthrough_suffix": true},
      "normalize_fields": {"enabled": true},
      "map_municipality": {"enabled": false},
      "enrich_fsa": {"enabled": false},
      "validate": {"strict_bounds": "ontario"}
    }
  }
}
```

### CivicNumberParser Options

| Option | Default | Description |
|--------|---------|-------------|
| `enabled` | `true` | Enable/disable civic number parsing |
| `passthrough_suffix` | `true` | Skip parsing if `civic_no_suffix` already set |
| `source_field` | `street_number` | Field containing the street number to parse |

### UnitNumberParser Options

| Option | Default | Description |
|--------|---------|-------------|
| `enabled` | `false` | Enable/disable unit number parsing |
| `source_field` | `full_address` | Field containing the address to parse for unit number |
| `passthrough_unit` | `true` | Skip parsing if `unit_no` already set |
| `parse_hyphenated` | `false` | Enable parsing of hyphenated format (e.g., "5-123 Main St") |
| `strip_labels` | `true` | Strip "Unit", "Suite", etc. labels from extracted value |

### FieldNormalizer Options

| Option | Default | Description |
|--------|---------|-------------|
| `enabled` | `true` | Enable/disable field normalization |
| `strip_unit_labels` | `false` | Strip "Unit", "Suite", "Apt" prefixes from unit_no |

## Database Dependencies

| Table | Used By | Purpose |
|-------|---------|---------|
| `municipality_aliases` | MunicipalityMapper | Pre-amalgamation name mappings |
| `fsa_boundaries` | FSAEnricher | PostGIS polygons for spatial FSA lookup |

## Database Schema (properties table)

The transform pipeline outputs records for the `properties` table which includes:

| Column | Type | Populated By |
|--------|------|--------------|
| `street_number` | VARCHAR | Source or CivicNumberParser (base number) |
| `civic_no_suffix` | VARCHAR(10) | CivicNumberParser (e.g., "A", "B", "1/2") |
| `unit_no` | VARCHAR(20) | Source field mapping, UnitNumberParser extraction, or FieldNormalizer normalization |
| `street_name` | VARCHAR | StreetNameParser or source |
| `street_suffix` | VARCHAR | StreetNameParser or FieldNormalizer |
| `street_direction` | VARCHAR | StreetNameParser or FieldNormalizer |
| `city` | VARCHAR | MunicipalityMapper (canonical name) |
| `city_original` | VARCHAR | MunicipalityMapper (pre-amalgamation name) |
| `postal_code` | VARCHAR | FieldNormalizer |
| `fsa_code` | VARCHAR(3) | FieldNormalizer or FSAEnricher |

## Testing

```bash
# Run all transformer tests
uv run pytest tests/data_sourcing/transformers/ -v

# Run specific transformer tests
uv run pytest tests/data_sourcing/transformers/test_civic_number_parser.py -v
uv run pytest tests/data_sourcing/transformers/test_unit_number_parser.py -v
uv run pytest tests/data_sourcing/transformers/test_field_normalizer.py -v

# Preview transforms without importing (dry-run)
DB_HOST=localhost uv run data_sourcing/importers/import_production_data.py \
  --file data.geojson --source ottawa --dry-run
```

## Integration with Import Script

The pipeline is invoked in `import_production_data.py` after field mapping:

```python
# Create pipeline with DB pool for async FSA lookups
pipeline = await TransformPipeline.for_source_async(source_name, pool)

# Transform each record
for record in records:
    context = await pipeline.transform_async(record)
    if context.has_errors:
        log_error(context)
        continue
    transformed_record = context.record
    # INSERT transformed_record (includes unit_no, civic_no_suffix)...
```
