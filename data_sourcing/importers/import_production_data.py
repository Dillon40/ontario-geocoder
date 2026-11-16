#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "asyncpg",
#   "python-dotenv",
#   "geopandas",
#   "ijson",
#   "pyproj",
# ]
# ///

"""
Import GeoJSON property data into PostgreSQL database.
Maps fields from municipal datasets to standardized schema.
"""
import sys
import json
import argparse
import asyncio
import traceback
from pathlib import Path
from typing import Iterator, Optional
from dotenv import load_dotenv
import asyncpg
import geopandas as gpd
import ijson
from pyproj import Transformer, CRS

# Support both the orchestrator's direct-script call and documented python -m.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from data_sourcing.importers.import_tracker import ImportTracker
from data_sourcing.importers.import_utils import FileHashManager, StaleRecordDetector, PropertyIdGenerator
from data_sourcing.transformers import TransformPipeline
from data_sourcing.db_utils import get_db_connection, get_db_pool

# File size threshold for streaming mode (100MB)
STREAMING_THRESHOLD_MB = 100

# Load environment variables
load_dotenv()

# Load config - find project root by looking for pyproject.toml
script_dir = Path(__file__).parent
project_root = script_dir
while project_root.parent != project_root:
    if (project_root / 'pyproject.toml').exists():
        break
    project_root = project_root.parent

config_path = project_root / 'config' / 'dataset-mappings.json'
with open(config_path, 'r') as f:
    dataset_mappings = json.load(f)

# Database field requirements
DB_FIELD_REQUIREMENTS = {
    'required': {
        'coordinates': 'Latitude and longitude (can be in geometry)',
        'address_identifier': 'Either street_number OR full_address'
    },
    'recommended': [
        'property_id',
        'street_number',
        'street_name',
        'street_suffix',
        'city',
        'full_address'
    ],
    'optional': [
        'postal_code',
        'province',
        'street_direction',
        'pin',
        'gpid',
        'assessment_roll_number',
        'unit_no'
    ]
}

# Common field name patterns for smart matching (case-insensitive)
# Order matters: more specific patterns first
FIELD_PATTERNS = {
    'property_id': [
        'PARCELID', 'PARCEL_ID', 'OBJECTID', 'OBJECT_ID', 'ADDRESSID', 'ADDRESS_ID',
        'ADDRESS_POINT_ID', 'POINT_ID', 'GIS_ID', 'GISID', 'UID', 'UNIQUE_ID', 'PID'
    ],
    'street_number': [
        'ADDRNUMBER', 'ADDR_NUMBER', 'ADDRESS_NUMBER', 'ADDRESS_NO', 'ADDRESSNO',
        'STREET_NUM', 'STREETNUM', 'STREETNO', 'STREET_NO', 'ST_NUM', 'STNUM',
        'CIVIC_NO', 'CIVICNO', 'CIVIC_NUM', 'CIVICNUM', 'MUNICNO', 'MUNIC_NO',
        'LO_NUM', 'LONUM', 'HOUSE_NUM', 'HOUSENUM', 'BLDG_NUM', 'NUMBER', 'NUM'
    ],
    'street_name': [
        'SSTRNAME', 'SSTNAME', 'STRNAME', 'STR_NAME',  # Barrie-style
        'LINEAR_NAME', 'LINEARNAME', 'STREET_NAME', 'STREETNAME', 'STNAME', 'ST_NAME',
        'ROAD_NAME', 'ROADNAME', 'OFFICIAL_STREET_NAME', 'STREET', 'ROAD', 'NAME'
    ],
    'street_suffix': [
        'SSTRSUFF', 'SSTSUFF', 'STRSUFF', 'STR_SUFF',  # Barrie-style
        'LINEAR_NAME_TYPE', 'LINEARNAMETYP', 'STREET_TYPE', 'STREETTYPE', 'STRTYPE',
        'STSUF', 'ST_SUF', 'ROAD_TYPE', 'ROADTYPE', 'SUFFIX', 'TYPE'
    ],
    'street_direction': [
        'SSTRDIR', 'SSTDIR', 'STRDIR', 'STR_DIR',  # Barrie-style
        'LINEAR_NAME_DIR', 'LINEARNAMEDIR', 'STREET_DIR', 'STREETDIR', 'STDIR', 'ST_DIR',
        'DIRECTION', 'DIR', 'OFFICIAL_STREET_DIR'
    ],
    'city': [
        'MUNICIPALITY_NAME', 'MUNICIPALITYNAME', 'MUNICIPALITY', 'MUNIC', 'MUN',
        'CITY', 'PLACE', 'PLACE_NAME', 'PLACENAME', 'CSD_ENG_NAME', 'CSDENGNAME',
        'TOWN', 'VILLAGE', 'COMMUNITY', 'ZONE_MUN', 'ZONEMUN', 'MUN_NAME'
    ],
    'postal_code': [
        'POSTAL_CODE', 'POSTALCODE', 'POSTAL', 'POSTCODE', 'POST_CODE',
        'MAIL_POSTAL_CODE', 'MAILPOSTALCODE', 'ZIP', 'ZIPCODE', 'ZIP_CODE'
    ],
    'full_address': [
        'FULLADDR', 'FULL_ADDR',  # Barrie-style
        'ADDRESS_FULL', 'ADDRESSFULL', 'FULL_ADDRESS', 'FULLADDRESS',
        'COMPLETE_ADDRESS', 'COMPLETEADDRESS', 'CIVIC_ADDRESS', 'CIVICADDRESS',
        'STREET_ADDRESS', 'STREETADDRESS', 'ADDRESS', 'ADDR'
    ],
    'latitude': ['LATITUDE', 'LAT', 'Y', 'YCOORD', 'Y_COORD', 'BG_LATITUDE', 'BGLATITUDE'],
    'longitude': ['LONGITUDE', 'LONG', 'LNG', 'LON', 'X', 'XCOORD', 'X_COORD', 'BG_LONGITUDE', 'BGLONGITUDE'],
    'province': ['PROVINCE', 'PROV', 'PROV_CODE', 'STATE'],
    'pin': ['PIN', 'PARCEL_ID', 'PARCELID'],
    'gpid': ['GPID', 'GEOGRAPHIC_PARCEL_ID'],
    'assessment_roll_number': ['ROLL_NO', 'ROLLNO', 'ROLL_NUMBER', 'ASSESSMENT_ROLL'],
    'unit_no': ['UNIT_NO', 'UNITNO', 'UNIT', 'UNITNUMBER', 'UNIT_NUMBER', 'APT_NO', 'APTNO', 'APT_NO_LABEL', 'APARTMENT', 'SUITE'],
    'civic_no_suffix': ['CIVIC_NO_SUFFIX', 'CIVICNOSUFFIX', 'STREET_NUM_SUFFIX', 'ADDR_SUFFIX', 'STNUMSUFF']
}


def load_spatial_file(file_path: str) -> dict:
    """
    Load spatial file (GeoJSON, Shapefile, etc.) and auto-reproject to EPSG:4326.

    Uses GeoPandas to:
    - Auto-detect file format (GeoJSON, Shapefile, GDB, etc.)
    - Auto-detect CRS from file metadata
    - Auto-reproject to WGS84 (EPSG:4326) if needed

    Returns: GeoJSON-like dict with 'features' array
    """
    print(f'Loading spatial file: {file_path}')

    # Read file with GeoPandas (works for GeoJSON, Shapefile, GDB, etc.)
    gdf = gpd.read_file(file_path)

    # Check and report CRS
    if gdf.crs:
        print(f'  Detected CRS: {gdf.crs}')
        if str(gdf.crs) != 'EPSG:4326':
            print(f'  Reprojecting to EPSG:4326...')
            gdf = gdf.to_crs(epsg=4326)
            print(f'  ✓ Reprojection complete')
    else:
        print(f'  ⚠️  Warning: No CRS detected, assuming EPSG:4326')

    # Convert datetime columns to strings for JSON serialization
    for col in gdf.columns:
        if gdf[col].dtype == 'datetime64[ns]' or str(gdf[col].dtype).startswith('datetime'):
            gdf[col] = gdf[col].astype(str)

    # Convert to GeoJSON-like dict
    geojson_str = gdf.to_json()
    data = json.loads(geojson_str)

    return data


def get_file_size_mb(file_path: str) -> float:
    """Get file size in megabytes."""
    return Path(file_path).stat().st_size / (1024 * 1024)


def detect_crs_from_geojson(file_path: str) -> Optional[str]:
    """
    Detect CRS from GeoJSON file by reading just the crs property.
    Returns EPSG code string like 'EPSG:4326' or None if not found.
    """
    with open(file_path, 'rb') as f:
        # Use ijson to parse just the crs field
        parser = ijson.parse(f)
        for prefix, event, value in parser:
            if prefix == 'crs.properties.name':
                crs_value = str(value)

                # Handle CRS84 which is equivalent to EPSG:4326
                if 'CRS84' in crs_value or 'CRS:84' in crs_value:
                    return 'EPSG:4326'

                # Handle URN format: urn:ogc:def:crs:EPSG::4326
                if 'EPSG' in crs_value:
                    parts = crs_value.split(':')
                    for i, part in enumerate(parts):
                        if part == 'EPSG' and i + 1 < len(parts):
                            # Handle both ::4326 and :4326 formats
                            epsg_code = parts[-1]
                            if epsg_code.isdigit():
                                return f'EPSG:{epsg_code}'
                return crs_value
            # Stop after we've passed the crs section
            if prefix.startswith('features'):
                break
    return None


def count_features_streaming(file_path: str) -> int:
    """
    Count features in a GeoJSON file using streaming parser.
    Memory efficient for large files.
    """
    count = 0
    with open(file_path, 'rb') as f:
        # Parse only the features array items
        for _ in ijson.items(f, 'features.item'):
            count += 1
    return count


def stream_geojson_features(file_path: str, transformer: Optional[Transformer] = None) -> Iterator[dict]:
    """
    Stream features from a GeoJSON file one at a time using ijson.

    This is memory-efficient for large files - only one feature is in memory at a time.

    Args:
        file_path: Path to GeoJSON file
        transformer: Optional pyproj Transformer for coordinate reprojection

    Yields:
        dict: GeoJSON feature dict with 'geometry' and 'properties'
    """
    with open(file_path, 'rb') as f:
        # ijson.items returns an iterator of parsed features
        for feature in ijson.items(f, 'features.item'):
            # Reproject coordinates if needed
            if transformer and feature.get('geometry'):
                geometry = feature['geometry']
                coords = geometry.get('coordinates')
                if coords:
                    geometry['coordinates'] = _reproject_coordinates(coords, transformer)

            yield feature


def _reproject_coordinates(coords, transformer: Transformer):
    """
    Recursively reproject coordinates.
    Handles Point, MultiPoint, LineString, Polygon, etc.
    Also handles Decimal types from ijson.
    """
    from decimal import Decimal

    if not coords:
        return coords

    # Convert coords to a list if needed (ijson may return other types)
    if not isinstance(coords, (list, tuple)):
        return coords

    # Check if this is a coordinate pair [x, y] or [x, y, z]
    first = coords[0]
    if isinstance(first, (int, float, Decimal)):
        # This is a single point
        if len(coords) >= 2:
            x, y = float(coords[0]), float(coords[1])
            new_x, new_y = transformer.transform(x, y)
            if len(coords) > 2:
                return [new_x, new_y] + [float(c) for c in coords[2:]]
            return [new_x, new_y]
        return [float(c) for c in coords]

    # This is a list of coordinates - recurse
    return [_reproject_coordinates(c, transformer) for c in coords]


def get_sample_features_streaming(file_path: str, count: int = 10) -> list:
    """
    Get a sample of features from a large GeoJSON file using streaming.
    Memory efficient - only loads requested number of features.
    """
    features = []
    with open(file_path, 'rb') as f:
        for i, feature in enumerate(ijson.items(f, 'features.item')):
            if i >= count:
                break
            features.append(feature)
    return features


def map_fields(properties: dict, field_mappings: dict) -> dict:
    """Map GeoJSON properties to database fields."""
    mapped = {}
    for db_field, source_field in field_mappings.items():
        mapped[db_field] = properties.get(source_field)
    return mapped


def smart_match_field(db_field: str, available_fields: set) -> tuple[str, str]:
    """
    Smart field matching with confidence level.
    Returns: (matched_field, confidence) where confidence is 'high', 'medium', 'low', or None
    """
    # Check pattern list first (high confidence)
    if db_field in FIELD_PATTERNS:
        for pattern in FIELD_PATTERNS[db_field]:
            for field in available_fields:
                if field.upper() == pattern.upper():
                    return field, 'high'

    # Try partial match (medium confidence)
    db_field_clean = db_field.lower().replace('_', '')
    for field in available_fields:
        field_clean = field.lower().replace('_', '')
        if db_field_clean in field_clean or field_clean in db_field_clean:
            if len(field_clean) > 2:  # Avoid single-letter matches
                return field, 'medium'

    return None, None


def suggest_field_mapping(db_field: str, available_fields: set) -> str:
    """Suggest a field mapping based on name similarity."""
    db_field_lower = db_field.lower().replace('_', '')

    # Try to find exact or close matches
    for field in available_fields:
        field_lower = field.lower().replace('_', '')
        if db_field_lower in field_lower or field_lower in db_field_lower:
            return field

    # Specific mapping suggestions
    suggestions = {
        'property_id': ['PARCELID', 'OBJECTID', 'ID', '_id'],
        'street_number': ['ADDRESS_NUMBER', 'STREET_NUM', 'CIVIC_NO'],
        'street_name': ['LINEAR_NAME', 'STREET_NAME', 'STREET'],
        'street_suffix': ['LINEAR_NAME_TYPE', 'STREET_TYPE', 'SUFFIX'],
        'full_address': ['ADDRESS_FULL', 'FULL_ADDRESS', 'ADDRESS'],
        'city': ['MUNICIPALITY_NAME', 'CITY', 'MUNICIPALITY'],
    }

    if db_field in suggestions:
        for suggestion in suggestions[db_field]:
            if suggestion in available_fields:
                return suggestion

    return None


def validate_field_mappings(features: list, field_mappings: dict, source_name: str) -> tuple[bool, list]:
    """Validate that mapped fields exist in the source data."""
    if not features:
        return False, ["No features found in GeoJSON"]

    # Get all available fields from first 10 features
    available_fields = set()
    for feature in features[:10]:
        available_fields.update(feature.get('properties', {}).keys())

    # Check which mapped fields are missing
    missing_fields = []
    found_fields = []
    suggested_mappings = {}

    for db_field, source_field in field_mappings.items():
        # Skip fields explicitly mapped to None (field doesn't exist in source)
        if source_field is None:
            continue
        if source_field not in available_fields:
            missing_fields.append(f"  - {db_field} -> '{source_field}' (NOT FOUND)")
            # Suggest alternative
            suggestion = suggest_field_mapping(db_field, available_fields)
            if suggestion:
                suggested_mappings[db_field] = suggestion
        else:
            found_fields.append(f"  - {db_field} -> '{source_field}' ✓")

    # Display results
    print(f"\nField Mapping Validation for '{source_name}':")
    print("=" * 80)

    if found_fields:
        print(f"\nFound ({len(found_fields)}):")
        for field in found_fields:
            print(field)

    if missing_fields:
        print(f"\n[FAIL] Missing ({len(missing_fields)}):")
        for field in missing_fields:
            print(field)

        if suggested_mappings:
            print(f"\nSuggested fixes:")
            for db_field, suggested_field in suggested_mappings.items():
                print(f"  - {db_field}: use '{suggested_field}' instead")

        print(f"\nAvailable fields in source data ({len(available_fields)}):")
        for field in sorted(available_fields):
            print(f"  - {field}")

        # Generate suggested config
        print(f"\n" + "=" * 80)
        print("HOW TO FIX:")
        print("=" * 80)
        print(f"\n1. Open: config/dataset-mappings.json")
        print(f"2. Update the '{source_name}' section's field_mappings")
        print(f"\n3. Suggested configuration based on available fields:")
        print(f"\n\"{source_name}\": {{")
        print(f"  \"municipality\": \"Your Municipality Name\",")
        print(f"  \"province\": \"ON\",")
        print(f"  \"postal_prefixes\": [],")
        print(f"  \"field_mappings\": {{")

        # Build suggested field mappings
        for db_field, source_field in field_mappings.items():
            if db_field in suggested_mappings:
                print(f"    \"{db_field}\": \"{suggested_mappings[db_field]}\",  // CHANGED from '{source_field}'")
            elif source_field in available_fields:
                print(f"    \"{db_field}\": \"{source_field}\",")
            else:
                print(f"    // \"{db_field}\": \"???\",  // MISSING - choose from available fields above")

        print(f"  }}")
        print(f"}}")

        print(f"\n4. Run the dry-run again to validate your changes")
        print("=" * 80 + "\n")

        return False, missing_fields

    print("\n✓ All field mappings are valid!")
    return True, []


def extract_postal_prefixes(features: list, postal_field: str, sample_size: int = 1000) -> list:
    """Extract unique 3-char postal code prefixes from dataset."""
    prefixes = set()
    sample = features[:min(sample_size, len(features))]

    for feature in sample:
        postal = feature.get('properties', {}).get(postal_field)
        if postal and isinstance(postal, str) and postal.strip():
            # Normalize: remove spaces, take first 3 chars, uppercase
            normalized = postal.replace(' ', '').strip().upper()
            if len(normalized) >= 3:
                # Validate it looks like a Canadian postal code prefix (letter-digit-letter)
                prefix = normalized[:3]
                if prefix[0].isalpha() and prefix[1].isdigit() and prefix[2].isalpha():
                    prefixes.add(prefix)

    return sorted(list(prefixes))


def validate_record(record: dict) -> tuple[bool, str]:
    """Normalize and validate record."""
    # Required fields
    if not record.get('latitude') or not record.get('longitude'):
        return False, 'Missing coordinates'

    if not record.get('street_number') and not record.get('full_address'):
        return False, 'Missing street number'

    # Validate coordinate ranges
    lat = float(record['latitude'])
    lng = float(record['longitude'])

    if lat < -90 or lat > 90 or lng < -180 or lng > 180:
        return False, 'Invalid coordinate range'

    # Normalize postal code
    if record.get('postal_code'):
        record['postal_code'] = record['postal_code'].replace(' ', '').upper()

    return True, ''


def infer_municipality_name(source_name: str) -> str:
    """Infer a human-readable municipality name from source key.

    Examples:
        barrie_addresses -> Barrie
        greater_sudbury -> Greater Sudbury
        leeds_and_grenville -> Leeds And Grenville
        haldimand_county_addresses -> Haldimand County
    """
    # Remove common suffixes
    name = source_name
    for suffix in ['_addresses', '_address', '_points']:
        name = name.replace(suffix, '')

    # Handle special patterns
    name = name.replace('_county', ' County')
    name = name.replace('_region', ' Region')
    name = name.replace('_district', ' District')

    # Title case each word
    words = name.split('_')
    titled = ' '.join(word.capitalize() for word in words)

    return titled


async def generate_new_dataset_config(file_path: str, source_name: str):
    """Generate configuration for a new dataset (wizard mode)."""
    print(f"\n{'=' * 80}")
    print(f"NEW DATASET WIZARD - Source '{source_name}' not found in config")
    print(f"{'=' * 80}\n")

    print("Analyzing dataset...\n")

    # Check file size - use streaming for large files to avoid OOM
    file_size_mb = get_file_size_mb(file_path)
    use_streaming = file_size_mb > STREAMING_THRESHOLD_MB

    if use_streaming:
        print(f"  Large file detected ({file_size_mb:.1f} MB) - using streaming mode")
        # Get sample features using streaming (memory efficient)
        features = get_sample_features_streaming(file_path, count=100)
        if not features:
            raise ValueError('No features found in GeoJSON')
    else:
        # Load spatial file with auto-reprojection
        data = load_spatial_file(file_path)

        if 'features' not in data or not isinstance(data['features'], list):
            raise ValueError('Invalid GeoJSON format')

        features = data['features']
        if not features:
            raise ValueError('No features found in GeoJSON')

    # Get available fields
    available_fields = set()
    for feature in features[:100]:
        available_fields.update(feature.get('properties', {}).keys())

    # Get sample record for showing example data
    sample_record = features[0].get('properties', {})

    # Check geometry type for coordinates
    geometry_type = features[0].get('geometry', {}).get('type')
    has_geometry_coords = bool(features[0].get('geometry', {}).get('coordinates'))

    # Display available fields with example values
    print(f"AVAILABLE FIELDS ({len(available_fields)}) - Sample from record 1:")
    for field in sorted(available_fields):
        value = sample_record.get(field)
        # Format the value for display
        if value is None:
            value_str = "null"
        elif isinstance(value, str):
            # Truncate long strings
            value_str = f'"{value[:40]}..."' if len(value) > 40 else f'"{value}"'
        else:
            value_str = str(value)
        print(f"  - {field:<25} → {value_str}")

    # Analyze field mappings
    print(f"\n{'=' * 80}")
    print("FIELD MAPPING ANALYSIS:")
    print(f"{'=' * 80}\n")

    suggested_mappings = {}
    warnings = []

    # Check required: coordinates
    print("REQUIRED FIELDS:")
    lat_field, lat_conf = smart_match_field('latitude', available_fields)
    lng_field, lng_conf = smart_match_field('longitude', available_fields)

    if lat_field and lng_field:
        print(f"  ✓ Coordinates         → {lat_field}, {lng_field} (found in properties)")
        suggested_mappings['latitude'] = lat_field
        suggested_mappings['longitude'] = lng_field
    elif has_geometry_coords:
        print(f"  ✓ Coordinates         → Found in geometry ({geometry_type})")
        print(f"                          Note: Will be auto-extracted during import")
    else:
        print(f"  [FAIL] Coordinates    → NOT FOUND")
        warnings.append("No coordinates found in properties or geometry - import may fail")

    # Check required: address identifier
    street_num_field, street_num_conf = smart_match_field('street_number', available_fields)
    full_addr_field, full_addr_conf = smart_match_field('full_address', available_fields)

    if street_num_field or full_addr_field:
        identifier_fields = []
        if street_num_field:
            identifier_fields.append(f"{street_num_field}")
            suggested_mappings['street_number'] = street_num_field
        if full_addr_field:
            identifier_fields.append(f"{full_addr_field}")
            suggested_mappings['full_address'] = full_addr_field
        print(f"  ✓ Address identifier  → {', '.join(identifier_fields)}")
    else:
        print(f"  ⚠️ Address identifier  → Not clearly identified")
        warnings.append("No clear street_number or full_address field - review mappings carefully")

    # Check recommended fields
    # Infer municipality early so we can show it in the output
    inferred_municipality = infer_municipality_name(source_name)

    print(f"\nRECOMMENDED FIELDS:")
    for db_field in DB_FIELD_REQUIREMENTS['recommended']:
        if db_field in suggested_mappings:
            continue  # Already handled above

        matched_field, confidence = smart_match_field(db_field, available_fields)
        if matched_field:
            conf_label = {'high': 'exact match', 'medium': 'likely match', 'low': 'possible match'}
            print(f"  ✓ {db_field:<20} → {matched_field} ({conf_label.get(confidence, 'match')})")
            suggested_mappings[db_field] = matched_field
        elif db_field == 'city':
            # For single-municipality datasets, use the inferred name as default
            print(f"  ✓ {db_field:<20} → Will use default: \"{inferred_municipality}\"")
        else:
            print(f"  [FAIL] {db_field:<20} → Not found")

    # Check optional fields
    optional_found = []
    for db_field in DB_FIELD_REQUIREMENTS['optional']:
        matched_field, confidence = smart_match_field(db_field, available_fields)
        if matched_field:
            optional_found.append((db_field, matched_field))
            suggested_mappings[db_field] = matched_field

    if optional_found:
        print(f"\nOPTIONAL FIELDS (found):")
        for db_field, matched_field in optional_found:
            print(f"  + {db_field:<20} → {matched_field}")

    # Postal code analysis
    print(f"\n{'=' * 80}")
    print("POSTAL CODE ANALYSIS:")
    print(f"{'=' * 80}")

    postal_prefixes = []
    postal_field, _ = smart_match_field('postal_code', available_fields)

    if postal_field:
        print(f"  ✓ Postal code field found: {postal_field}")
        postal_prefixes = extract_postal_prefixes(features, postal_field)

        if postal_prefixes:
            sample_prefixes = postal_prefixes[:10]
            more_text = f" (and {len(postal_prefixes) - 10} more)" if len(postal_prefixes) > 10 else ""
            print(f"  ✓ Extracted {len(postal_prefixes)} unique prefix(es) from {len(features):,} records")
            print(f"  → {sample_prefixes}{more_text}")
            suggested_mappings['postal_code'] = postal_field
            print(f"  → FSA enrichment: DISABLED (postal codes available in data)")
        else:
            print(f"  ⚠️  Postal code field exists but no valid postal codes found")
            print(f"  → FSA enrichment: ENABLED (will use PostGIS spatial lookup)")
    else:
        print(f"  ℹ️  No postal code field found in dataset")
        print(f"  → FSA enrichment: ENABLED (will use PostGIS spatial lookup)")
        print(f"  → FSA codes will be auto-populated from fsa_boundaries table during import")

    # Display warnings
    if warnings:
        print(f"\n{'=' * 80}")
        print("⚠️  WARNINGS:")
        print(f"{'=' * 80}")
        for warning in warnings:
            print(f"  - {warning}")

    # Build the config object
    # Infer municipality name from source (e.g., "barrie_addresses" -> "Barrie")
    default_municipality = infer_municipality_name(source_name)

    # Check if we need to set default_city (when no city field in data)
    has_city_field = 'city' in suggested_mappings
    needs_default_city = not has_city_field

    # Build transforms config
    transforms_config = {
        "parse_street_name": {"enabled": False},
        "parse_civic_number": {"enabled": True, "passthrough_suffix": True},
        "normalize_fields": {"enabled": True},
        "map_municipality": {
            "enabled": needs_default_city,
            "default_city": default_municipality if needs_default_city else None
        },
        "enrich_fsa": {"enabled": len(postal_prefixes) == 0},
        "validate": {"enabled": True, "strict_bounds": "ontario"}
    }

    # Clean up None values
    if not needs_default_city:
        transforms_config["map_municipality"] = {"enabled": False}

    config = {
        "source_name": source_name,
        "municipality": default_municipality,
        "province": "ON",
        "postal_prefixes": postal_prefixes,
        "field_mappings": suggested_mappings,
        "transforms": transforms_config
    }

    # Show generated config
    print(f"\n{'=' * 80}")
    print("GENERATED CONFIGURATION")
    print(f"{'=' * 80}\n")

    config_json = json.dumps({source_name: config}, indent=2)
    print(config_json)

    # Interactive confirmation
    print(f"\n{'=' * 80}")
    print("REVIEW AND CONFIRM")
    print(f"{'=' * 80}")

    print(f"\n  Municipality: {config['municipality']}")
    if needs_default_city:
        print(f"  Default city: {config['municipality']} (no city field in data)")
    print(f"  Postal prefixes: {len(postal_prefixes)} detected")
    print(f"  Field mappings: {len(suggested_mappings)} fields")
    print(f"  FSA enrichment: {'enabled (no postal codes found)' if config['transforms']['enrich_fsa']['enabled'] else 'disabled (has postal codes)'}")

    if warnings:
        print(f"\n  ⚠️  Warnings: {len(warnings)}")
        for w in warnings:
            print(f"      - {w}")

    print(f"\nOptions:")
    print(f"  [y] Save config and continue with import")
    print(f"  [e] Edit municipality name before saving")
    print(f"  [n] Cancel (do not save)")

    while True:
        response = input("\nYour choice (y/e/n): ").strip().lower()

        if response == 'e':
            new_name = input(f"  Enter municipality name [{config['municipality']}]: ").strip()
            if new_name:
                config['municipality'] = new_name
                print(f"  ✓ Municipality updated to: {new_name}")
            response = input("\nSave and continue? (y/n): ").strip().lower()

        if response == 'y':
            # Save to dataset-mappings.json
            config_path = project_root / 'config' / 'dataset-mappings.json'
            try:
                with open(config_path, 'r') as f:
                    existing = json.load(f)

                existing[source_name] = config
                with open(config_path, 'w') as f:
                    json.dump(existing, f, indent=2)

                print(f"\n✓ Saved to {config_path}")

                # Update database dataset_mapping_key
                try:
                    conn = await get_db_connection()
                    await conn.execute('''
                        UPDATE data_sources
                        SET dataset_mapping_key = $1
                        WHERE dataset_mapping_key IS NULL
                          AND (source_key = $1 OR source_key LIKE $2)
                    ''', source_name, f'%{source_name}%')
                    await conn.close()
                    print(f"✓ Updated database dataset_mapping_key")
                except Exception as e:
                    print(f"  Note: Could not update database: {e}")

                # Return config so import can continue
                return config

            except Exception as e:
                print(f"\n[FAIL] Error saving config: {e}")
                return None

        elif response == 'n':
            print("\nCancelled. Config not saved.")
            return None

        else:
            print("Please enter 'y', 'e', or 'n'")


# VARCHAR column limits in the properties table (see database/schema.sql).
# Used to sanitize over-length values before insert so a single junk value
# (e.g. a description string landing in street_direction) cannot abort an
# entire import with "value too long for type character varying(N)".
PROPERTIES_VARCHAR_LIMITS = {
    'property_id': 50,
    'assessment_roll_number': 50,
    'street_number': 20,
    'street_name': 100,
    'street_suffix': 20,
    'street_direction': 20,  # widened from 10 by migration 25
    # unit_no is TEXT: retain upstream labels verbatim.
    'civic_no_suffix': 50,
    'city': 100,
    'city_original': 100,
    'province': 50,
    'postal_code': 10,
    'fsa_code': 3,
}


def sanitize_varchar_lengths(record: dict, source_name: str,
                             truncations: Optional[list] = None) -> dict:
    """Truncate string values that exceed their properties column limit.

    Logs a warning for each truncation so bad upstream data is visible
    instead of silently crashing the batch insert. When a `truncations`
    list is supplied, each truncation is also appended to it so callers
    can record the event in import_row_errors for auditability (a
    truncated address imports "successfully" but is wrong data).
    """
    for field, limit in PROPERTIES_VARCHAR_LIMITS.items():
        value = record.get(field)
        if isinstance(value, str) and len(value) > limit:
            print(f"\n⚠️  WARNING [{source_name}] property_id={record.get('property_id')!r}: "
                  f"{field} value exceeds varchar({limit}), truncating: {value!r}")
            if truncations is not None:
                truncations.append({
                    'property_id': record.get('property_id'),
                    'field': field,
                    'limit': limit,
                    'original_value': value,
                    'truncated_value': value[:limit],
                })
            record[field] = value[:limit]
    return record


async def insert_batch(batch: list, source_name: str, pool: asyncpg.Pool,
                       import_id: int = None) -> tuple[int, int]:
    """Insert batch of records.

    Args:
        batch: List of records to insert
        source_name: Dataset name
        pool: Database connection pool
        import_id: Current import ID for tracking (optional)

    Returns:
        Tuple of (rows_inserted, rows_updated). Distinguished via the
        standard Postgres trick: xmax = 0 in the RETURNING clause means
        the row was freshly inserted; non-zero means the ON CONFLICT
        UPDATE path fired.
    """
    if not batch:
        return 0, 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            inserted_count = 0
            updated_count = 0
            truncations = []

            for record in batch:
                sanitize_varchar_lengths(record, source_name, truncations)
                query = '''
                    INSERT INTO properties (
                        property_id, assessment_roll_number, street_number, street_name,
                        street_suffix, street_direction, unit_no, civic_no_suffix,
                        city, city_original, province, postal_code, fsa_code,
                        full_address, latitude, longitude, location, pin, gpid,
                        source_dataset, import_date, last_seen_import_id
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                        $15::DECIMAL(10,8), $16::DECIMAL(11,8),
                        ST_SetSRID(ST_MakePoint($16::DECIMAL, $15::DECIMAL), 4326)::geography,
                        $17::BIGINT, $18::BIGINT, $19, NOW(), $20
                    )
                    ON CONFLICT (property_id)
                    DO UPDATE SET
                        assessment_roll_number = EXCLUDED.assessment_roll_number,
                        street_number = EXCLUDED.street_number,
                        street_name = EXCLUDED.street_name,
                        street_suffix = EXCLUDED.street_suffix,
                        street_direction = EXCLUDED.street_direction,
                        unit_no = EXCLUDED.unit_no,
                        civic_no_suffix = EXCLUDED.civic_no_suffix,
                        city = EXCLUDED.city,
                        city_original = EXCLUDED.city_original,
                        province = EXCLUDED.province,
                        postal_code = EXCLUDED.postal_code,
                        fsa_code = EXCLUDED.fsa_code,
                        full_address = EXCLUDED.full_address,
                        -- Coordinate-overwrite protection: only take incoming
                        -- coordinates when the new file actually has BOTH.
                        -- A re-import from a file missing coordinates must
                        -- never wipe existing good ones. lat/long/location
                        -- are updated as a unit so they stay consistent
                        -- (EXCLUDED.location is NULL whenever either
                        -- coordinate is NULL, since ST_MakePoint(NULL,..)
                        -- yields NULL).
                        latitude = CASE
                            WHEN EXCLUDED.latitude IS NOT NULL
                             AND EXCLUDED.longitude IS NOT NULL
                            THEN EXCLUDED.latitude
                            ELSE properties.latitude END,
                        longitude = CASE
                            WHEN EXCLUDED.latitude IS NOT NULL
                             AND EXCLUDED.longitude IS NOT NULL
                            THEN EXCLUDED.longitude
                            ELSE properties.longitude END,
                        location = CASE
                            WHEN EXCLUDED.latitude IS NOT NULL
                             AND EXCLUDED.longitude IS NOT NULL
                            THEN EXCLUDED.location
                            ELSE properties.location END,
                        pin = EXCLUDED.pin,
                        gpid = EXCLUDED.gpid,
                        source_dataset = EXCLUDED.source_dataset,
                        import_date = NOW(),
                        last_seen_import_id = EXCLUDED.last_seen_import_id,
                        is_deleted = FALSE,
                        deleted_at = NULL
                    RETURNING (xmax = 0) AS was_inserted
                '''

                values = [
                    str(record.get('property_id')) if record.get('property_id') is not None else None,
                    record.get('assessment_roll_number'),
                    str(record.get('street_number')) if record.get('street_number') is not None else None,
                    record.get('street_name'),
                    record.get('street_suffix'),
                    record.get('street_direction'),
                    record.get('unit_no'),
                    record.get('civic_no_suffix'),
                    record.get('city'),
                    record.get('city_original'),  # Preserved original city name
                    record.get('province', 'ON'),
                    record.get('postal_code'),
                    record.get('fsa_code'),  # FSA code from spatial lookup or postal code
                    record.get('full_address'),
                    float(record['latitude']) if record.get('latitude') else None,
                    float(record['longitude']) if record.get('longitude') else None,
                    int(float(record['pin'])) if record.get('pin') else None,
                    int(float(record['gpid'])) if record.get('gpid') else None,
                    source_name,
                    import_id
                ]

                row = await conn.fetchrow(query, *values)
                if row and row['was_inserted']:
                    inserted_count += 1
                else:
                    updated_count += 1

            # Record truncations in import_row_errors so silently-shortened
            # values are auditable, not just stdout noise. Written inside
            # the batch transaction so they persist iff the rows do.
            if truncations and import_id is not None:
                await conn.executemany('''
                    INSERT INTO import_row_errors
                        (import_id, row_number, error_type, error_message, row_data)
                    VALUES ($1, $2, $3, $4, $5::jsonb)
                ''', [
                    (
                        import_id,
                        None,  # row number unknown at batch level
                        'truncation_warning',
                        f"{t['field']} exceeded varchar({t['limit']}) and was "
                        f"truncated for property_id={t['property_id']}",
                        json.dumps(t)
                    )
                    for t in truncations
                ])

            return inserted_count, updated_count


async def update_dataset_metadata(source_name: str, record_count: int, postal_prefixes: list, pool: asyncpg.Pool):
    """Update dataset metadata."""
    try:
        query = '''
            INSERT INTO dataset_metadata (
                dataset_name, municipality_name, postal_code_prefixes,
                record_count, last_updated, active
            ) VALUES ($1, $2, $3, $4, NOW(), true)
            ON CONFLICT (dataset_name)
            DO UPDATE SET
                record_count = EXCLUDED.record_count,
                last_updated = NOW()
        '''

        config = dataset_mappings.get(source_name, {})
        municipality = config.get('municipality', source_name)

        await pool.execute(query, source_name, municipality, postal_prefixes, record_count)

    except asyncpg.UndefinedTableError:
        print('Note: dataset_metadata table not yet created, skipping metadata update')


async def import_geojson(file_path: str, source_name: str, batch_size: int,
                         dry_run: bool = False, force: bool = False,
                         stale_strategy: str = 'soft_delete'):
    """Import GeoJSON file with streaming support for large files.

    Args:
        file_path: Path to the GeoJSON file
        source_name: Dataset name (from dataset-mappings.json)
        batch_size: Number of records per batch insert
        dry_run: If True, validate only without importing
        force: If True, import even if file hash unchanged
        stale_strategy: How to handle stale records ('soft_delete', 'report_only', 'skip')
    """
    mode_label = "DRY RUN - VALIDATION ONLY" if dry_run else "IMPORT MODE"
    print(f'\n{"=" * 80}')
    print(f'{mode_label}')
    print(f'{"=" * 80}')
    print(f'File: {file_path}')
    print(f'Source dataset: {source_name}')
    print(f'Batch size: {batch_size}\n')

    # Check if file exists
    if not Path(file_path).exists():
        raise FileNotFoundError(f'File not found: {file_path}')

    # Check file hash and skip if unchanged (unless dry_run or force)
    if not dry_run and not force:
        # Create temporary pool for hash check
        temp_pool = await get_db_pool(min_size=1, max_size=2)
        try:
            should_skip, reason = await FileHashManager.should_skip_file(
                temp_pool, source_name, file_path)
            if should_skip:
                print(f'\nSkipping import: {reason}')
                print('   Use --force to override file hash check.')
                return
        finally:
            await temp_pool.close()

    # Get field mappings - if source doesn't exist, trigger wizard mode
    field_mappings = dataset_mappings.get(source_name, {}).get('field_mappings')
    if not field_mappings:
        # Source doesn't exist - enter wizard mode
        wizard_config = await generate_new_dataset_config(file_path, source_name)

        if wizard_config is None:
            # User cancelled
            return

        # Reload mappings with new config
        dataset_mappings[source_name] = wizard_config
        field_mappings = wizard_config.get('field_mappings')

        if dry_run:
            print(f"\n✓ Config saved. Re-run without --dry-run to import.")
            return

    # Check file size to determine processing mode
    file_size_mb = get_file_size_mb(file_path)
    use_streaming = file_size_mb > STREAMING_THRESHOLD_MB

    if use_streaming:
        print(f'File size: {file_size_mb:.1f}MB (> {STREAMING_THRESHOLD_MB}MB threshold)')
        print('Using STREAMING mode for memory efficiency...\n')

        # Detect CRS and set up transformer if needed
        transformer = None
        detected_crs = detect_crs_from_geojson(file_path)
        if detected_crs:
            print(f'  Detected CRS: {detected_crs}')
            if detected_crs != 'EPSG:4326':
                print(f'  Setting up coordinate transformation to EPSG:4326...')
                source_crs = CRS.from_string(detected_crs)
                target_crs = CRS.from_epsg(4326)
                transformer = Transformer.from_crs(source_crs, target_crs, always_xy=True)
                print(f'  ✓ Transformer ready')
        else:
            print(f'  ⚠️  No CRS detected, assuming EPSG:4326')

        # Get sample features for validation
        print('  Reading sample features for validation...')
        sample_features = get_sample_features_streaming(file_path, count=10)

        if not sample_features:
            raise ValueError('No features found in GeoJSON')

        # Validate field mappings with sample
        valid, errors = validate_field_mappings(sample_features, field_mappings, source_name)
        if not valid:
            raise ValueError(f'Field mapping validation failed. Please update config/dataset-mappings.json')

        # Count total features (streaming)
        print('  Counting total features (streaming)...')
        total_features = count_features_streaming(file_path)
        print(f'  Total features: {total_features:,}')

        # Create feature iterator
        def feature_iterator():
            return stream_geojson_features(file_path, transformer)

    else:
        print(f'File size: {file_size_mb:.1f}MB (< {STREAMING_THRESHOLD_MB}MB threshold)')
        print('Using STANDARD mode (loading into memory)...\n')

        # Load spatial file with auto-reprojection
        print('Reading spatial file...')
        data = load_spatial_file(file_path)

        if 'features' not in data or not isinstance(data['features'], list):
            raise ValueError('Invalid GeoJSON format')

        total_features = len(data['features'])
        print(f'Total features: {total_features:,}')

        # Validate field mappings
        valid, errors = validate_field_mappings(data['features'], field_mappings, source_name)
        if not valid:
            raise ValueError(f'Field mapping validation failed. Please update config/dataset-mappings.json')

        # Create feature iterator from loaded data
        def feature_iterator():
            return iter(data['features'])

    # If dry run, process a sample and show transform pipeline output
    if dry_run:
        print(f'\n{"=" * 80}')
        print('DRY RUN: Connecting to database for transform pipeline preview...')
        print(f'{"=" * 80}\n')

        # Connect to database for async transforms (FSA lookup, municipality mapping)
        pool = await get_db_pool(min_size=1, max_size=5)

        try:
            # Create transform pipeline
            pipeline = await TransformPipeline.for_source_async(source_name, pool)
            print(f'Transform pipeline initialized for source: {source_name}')
            print(f'Transformers: {[t.name for t in pipeline.transformers]}')

            sample_size = min(5, total_features)
            contexts = []

            # Get sample features (use already loaded sample for streaming, or slice for standard)
            if use_streaming:
                sample = sample_features[:sample_size]
            else:
                sample = data['features'][:sample_size]

            for i, feature in enumerate(sample):
                # Map fields
                record = map_fields(feature.get('properties', {}), field_mappings)

                # Extract coordinates if needed
                if not record.get('latitude') or not record.get('longitude'):
                    coordinates = feature.get('geometry', {}).get('coordinates')
                    if coordinates and isinstance(coordinates, list):
                        # Handle MultiPoint geometry
                        if isinstance(coordinates[0], list):
                            record['longitude'] = coordinates[0][0]
                            record['latitude'] = coordinates[0][1]
                        else:
                            record['longitude'] = coordinates[0]
                            record['latitude'] = coordinates[1]

                # Generate property ID if not provided
                if not record.get('property_id'):
                    if record.get('pin'):
                        record['property_id'] = f"PIN-{record['pin']}"
                    else:
                        record['property_id'] = f"{source_name.upper()}-{i + 1}"

                # Apply transform pipeline
                context = await pipeline.transform_async(record, row_number=i + 1)
                contexts.append(context)

            # Print detailed transform output
            print('\n' + pipeline.format_dry_run_output(contexts, max_records=sample_size))

            # Summary info
            print(f'\nTotal features in file: {total_features:,}')
            print(f'\nTo import this dataset, run without --dry-run flag:')
            print(f'  uv run data_sourcing/importers/import_production_data.py --file {file_path} --source {source_name}')
            print(f'{"=" * 80}\n')

        finally:
            await pool.close()

        return

    # Connect to database
    pool = await get_db_pool(min_size=1, max_size=10)

    try:
        # Initialize transform pipeline
        print('Initializing transform pipeline...')
        pipeline = await TransformPipeline.for_source_async(source_name, pool)
        print(f'  Transformers: {[t.name for t in pipeline.transformers]}')

        # Use ImportTracker to record this import operation
        async with ImportTracker(pool, source_name, file_path, 'full') as tracker:
            # Initialize stale record detector (only if not skipping stale detection)
            stale_detector = None
            if stale_strategy != 'skip':
                stale_detector = StaleRecordDetector(
                    pool, tracker.import_id, source_name,
                    'properties', 'property_id'
                )
                print(f'Stale record detection: {stale_strategy}')

            # Process features
            batch = []
            batch_property_ids = []  # Track IDs for stale detection
            postal_prefix_set = set()
            row_number = 0
            transform_errors = 0

            # Use the feature iterator (streaming or in-memory)
            for feature in feature_iterator():
                row_number += 1

                # Map fields from properties first
                record = map_fields(feature.get('properties', {}), field_mappings)

                # Only use geometry coordinates if LAT/LONG not in properties
                if not record.get('latitude') or not record.get('longitude'):
                    geometry = feature.get('geometry') or {}
                    coordinates = geometry.get('coordinates') if geometry else None
                    if coordinates and isinstance(coordinates, list):
                        # Drill down through nested coordinate arrays to find
                        # the first [lon, lat] pair. Handles Point, MultiPoint,
                        # Polygon, MultiPolygon, etc.
                        coord = coordinates
                        while isinstance(coord, list) and len(coord) > 0 and isinstance(coord[0], list):
                            coord = coord[0]
                        # coord should now be [lon, lat] (a list of numbers)
                        if isinstance(coord, list) and len(coord) >= 2 and not isinstance(coord[0], list):
                            record['longitude'] = coord[0]
                            record['latitude'] = coord[1]

                # Generate property ID using standardized generator
                # ALWAYS generate a prefixed ID to avoid collisions between sources
                # (many sources use OBJECTID 1,2,3... which would otherwise collide)
                record['property_id'] = PropertyIdGenerator.generate(
                    source_name, record, row_number
                )

                # Some published point layers include empty/non-address features.
                # Exclude ONLY absent civic/full-address identities, audit them,
                # and distinguish this documented policy from processing errors.
                if not any(record.get(k) is not None and str(record[k]).strip()
                           for k in ('street_number', 'full_address')):
                    await tracker.log_row_error(row_number, 'not_address_record',
                        'Excluded source point without civic number or full address',
                        record, count_failure=False)
                    tracker.stats.rows_processed += 1
                    continue

                # Apply transform pipeline (includes validation)
                context = await pipeline.transform_async(record, row_number=row_number)

                if context.has_errors:
                    # Log transform/validation error
                    error_msg = '; '.join(e.message for e in context.errors)
                    await tracker.log_row_error(row_number, 'transform_error', error_msg, record)
                    tracker.stats.rows_skipped += 1
                    transform_errors += 1
                else:
                    # Use transformed record
                    transformed_record = context.record
                    batch.append(transformed_record)
                    batch_property_ids.append(transformed_record.get('property_id'))

                    # Track FSA codes (from transformed data)
                    fsa_code = transformed_record.get('fsa_code')
                    if fsa_code:
                        postal_prefix_set.add(fsa_code)
                    elif transformed_record.get('postal_code') and len(transformed_record['postal_code']) >= 3:
                        postal_prefix_set.add(transformed_record['postal_code'][:3])

                    # Insert batch when it reaches batch size
                    if len(batch) >= batch_size:
                        inserted, updated = await insert_batch(batch, source_name, pool, tracker.import_id)
                        tracker.stats.rows_inserted += inserted
                        tracker.stats.rows_updated += updated
                        tracker.stats.rows_processed += len(batch)

                        # Mark all IDs in this batch as seen
                        if stale_detector:
                            for pid in batch_property_ids:
                                stale_detector.mark_seen(pid)

                        batch = []
                        batch_property_ids = []

                        # Progress update
                        percent = round((tracker.stats.rows_processed / total_features) * 100)
                        print(f'\rProgress: {tracker.stats.rows_processed}/{total_features} ({percent}%) - '
                              f'Inserted: {tracker.stats.rows_inserted}, Updated: {tracker.stats.rows_updated}, '
                              f'Skipped: {tracker.stats.rows_skipped}', end='')

            # Insert remaining records
            if batch:
                inserted, updated = await insert_batch(batch, source_name, pool, tracker.import_id)
                # Mark remaining IDs as seen
                if stale_detector:
                    for pid in batch_property_ids:
                        stale_detector.mark_seen(pid)
                tracker.stats.rows_inserted += inserted
                tracker.stats.rows_updated += updated
                tracker.stats.rows_processed += len(batch)

            print(f'\n\nImport complete!')
            print(f'Total processed: {total_features}')
            print(f'Newly inserted: {tracker.stats.rows_inserted}')
            print(f'Updated existing: {tracker.stats.rows_updated}')
            print(f'Skipped: {tracker.stats.rows_skipped}')
            print(f'Transform errors: {transform_errors}')
            print(f'Failed: {tracker.stats.rows_failed}')
            print(f'Postal prefixes found: {", ".join(sorted(postal_prefix_set))}')

            # Never retire old records or report success after processing errors.
            if tracker.stats.rows_failed:
                raise RuntimeError(f'{tracker.stats.rows_failed} rows failed; see import_row_errors')
            if total_features and not (tracker.stats.rows_inserted + tracker.stats.rows_updated):
                raise RuntimeError('Source contained no importable address records')

            # Handle stale records
            if stale_detector:
                print(f'\nDetecting stale records...')
                stale_ids = await stale_detector.detect_stale_records()

                if stale_ids:
                    print(f'Found {len(stale_ids):,} stale records (in DB but not in import file)')

                    if stale_strategy == 'soft_delete':
                        deleted = await stale_detector.soft_delete_stale_records(stale_ids)
                        tracker.stats.rows_deleted = deleted
                        print(f'Soft-deleted {deleted:,} stale records')
                    elif stale_strategy == 'report_only':
                        await stale_detector.report_stale_records(stale_ids)
                        print('(Report only mode - no records modified)')
                else:
                    print('No stale records found')

            # Print pipeline statistics
            print(f'\nTransform Pipeline Statistics:')
            print(pipeline.statistics.summary())

            # Print final summary with all stats
            print(f'\nFinal Import Statistics:')
            print(f'  {tracker.stats.summary()}')

            # Update metadata
            print('\nUpdating dataset metadata...')
            # record_count = all rows carried by this file (inserted + updated);
            # rows_inserted alone now only counts NEW rows since the xmax split.
            await update_dataset_metadata(
                source_name,
                tracker.stats.rows_inserted + tracker.stats.rows_updated,
                sorted(list(postal_prefix_set)), pool)

            print('Done!\n')

    finally:
        await pool.close()


async def get_source_from_db(source_key: str) -> dict:
    """
    Look up source configuration from data_sources table.
    Returns dict with file path and dataset mapping key.
    """
    conn = await get_db_connection()
    try:
        row = await conn.fetchrow("""
            SELECT source_key, local_file_path, dataset_mapping_key
            FROM data_sources
            WHERE source_key = $1
        """, source_key)
        if row:
            return dict(row)
        return None
    finally:
        await conn.close()


async def main():
    """Main execution."""
    parser = argparse.ArgumentParser(
        description='Import GeoJSON property data into PostgreSQL database',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Validate field mappings without importing (dry run)
  python import_production_data.py --file data.geojson --source toronto --dry-run

  # Import data into database
  python import_production_data.py --file data.geojson --source toronto

  # Import from data_sources table configuration
  python import_production_data.py --from-source guelph_addresses

Available sources: ''' + ', '.join(dataset_mappings.keys())
    )
    parser.add_argument('--file', help='Path to GeoJSON file')
    parser.add_argument('--source', help=f'Dataset name (from dataset-mappings.json)')
    parser.add_argument('--from-source', dest='from_source',
                       help='Source key from data_sources table (auto-resolves file and mapping)')
    parser.add_argument('--batch-size', type=int, default=1000, help='Batch size (default: 1000)')
    parser.add_argument('--dry-run', action='store_true',
                       help='Validate field mappings without importing to database')
    parser.add_argument('--force', action='store_true',
                       help='Force import even if file hash unchanged from last import')
    parser.add_argument('--stale-strategy', choices=['soft_delete', 'report_only', 'skip'],
                       default='soft_delete',
                       help='How to handle stale records (default: soft_delete)')

    args = parser.parse_args()

    # Handle --from-source option
    if args.from_source:
        source_info = await get_source_from_db(args.from_source)
        if not source_info:
            print(f"Error: Source '{args.from_source}' not found in data_sources table")
            sys.exit(1)

        # Resolve file path
        project_root = Path(__file__).parent.parent
        file_path = source_info.get('local_file_path')
        if file_path:
            args.file = str(project_root / file_path)
        else:
            args.file = str(project_root / 'data_sources' / args.from_source / f"{args.from_source}.geojson")

        # Resolve dataset mapping key
        args.source = source_info.get('dataset_mapping_key') or args.from_source

        print(f"Resolved from data_sources table:")
        print(f"  File: {args.file}")
        print(f"  Source mapping: {args.source}")

    # Validate required arguments
    if not args.file or not args.source:
        parser.error("Either --from-source OR both --file and --source are required")

    # Auto-resolve source key to mapping key if needed
    if args.source not in dataset_mappings:
        # Check if it's a source_key in the database that maps to a different config key
        source_info = await get_source_from_db(args.source)
        if source_info and source_info.get('dataset_mapping_key'):
            mapping_key = source_info['dataset_mapping_key']
            if mapping_key in dataset_mappings:
                print(f"Note: '{args.source}' is a source_key, using mapping key '{mapping_key}'")
                args.source = mapping_key
            else:
                print(f"Warning: Source '{args.source}' found in database with mapping key '{mapping_key}', but '{mapping_key}' is not in dataset-mappings.json")
                print("Proceeding with wizard mode...")
        elif source_info:
            print(f"Note: Source '{args.source}' found in database but has no dataset_mapping_key configured")
            print("Proceeding with wizard mode...")
        # If not found in DB either, wizard mode will handle it

    try:
        await import_geojson(args.file, args.source, args.batch_size, args.dry_run,
                             args.force, args.stale_strategy)
    except Exception as error:
        print(f'\n[FAIL] Error: {error}')
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())
