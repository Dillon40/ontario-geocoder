#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "asyncpg>=0.29.0",
# ]
# ///
"""
Municipal Dataset Coverage Analyzer

Tests new municipal dataset files against NAR's missing-geocode records to
predict enrichment value before importing.

ENHANCED Features:
- Municipality alias matching (bilingual names, pre-amalgamation names)
- FSA-based matching for datasets with fsa_code field
- Optimized matching strategy based on available fields
- Geographic alignment validation
- Comprehensive coverage reports

Key Features:
1. Municipality Alias Matching:
   - Resolves city names through municipality_aliases table
   - Handles pre-amalgamation names (e.g., "Etobicoke" → "TORONTO")
   - Supports bilingual names (e.g., "Greater Sudbury / Grand Sudbury")
   - Uses normalize_municipality_name() database function

2. FSA-Based Matching:
   - Detects datasets with fsa_code field (added via add_fsa_to_dataset.py)
   - Uses optimized matching: civic_no + street_name + fsa_code
   - Ignores optional fields (street_type, street_dir) for better match rates
   - Achieves 90%+ coverage for well-aligned datasets

3. Smart Normalization:
   - Handles "None" string values common in municipal datasets
   - Preserves FSA codes through normalization pipeline
   - Normalizes addresses for consistent matching

Workflow:

For datasets with coordinates but no FSA codes:
    # Step 1: Add FSA codes using separate script
    ./add_fsa_to_dataset.py \\
        --input toronto_addresses.geojson \\
        --output toronto_addresses_with_fsa.geojson

    # Step 2: Test coverage with FSA-enriched dataset
    ./test_municipal_dataset_coverage.py \\
        --dataset toronto_addresses_with_fsa.geojson \\
        --config toronto

For datasets with postal codes:
    ./test_municipal_dataset_coverage.py --dataset data.geojson --config guelph
    ./test_municipal_dataset_coverage.py --dataset data.csv --sample 5000

For dual datasets (addresses + parcels):
    ./test_municipal_dataset_coverage.py \\
        --dataset-addresses addresses.geojson \\
        --dataset-parcels parcels.geojson \\
        --config guelph

Options:
    --sample N      Test with random sample of N NAR records (0 = full analysis)
    --output-dir    Directory for generated reports (default: ./reports)

Reports Generated:
    - coverage_summary.txt: Overall statistics and recommendations
    - match_breakdown.csv: Detailed match results per NAR record
    - geographic_coverage.csv: FSA and municipality coverage analysis
    - sample_matches.json: Example matched records for validation
"""

import asyncio
import asyncpg
import sys
import json
import csv
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Any
from collections import defaultdict, Counter

# Add project root to path for db_utils import
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from data_sourcing.db_utils import get_db_config


# Common patterns for join key detection
JOIN_KEY_PATTERNS = {
    'pin': ['PIN', 'PARCEL_ID', 'PID', 'PARCEL_PIN'],
    'gpid': ['GPID', 'GEO_ID', 'GEOGRAPHIC_ID'],
    'roll_number': ['ROLL_NO', 'ROLL_NUMBER', 'ARN', 'ASSESSMENT_ROLL', 'ASSESSMENT_ROLL_NUMBER'],
    'property_id': ['PROPERTY_ID', 'PROP_ID', 'OBJECTID', 'OBJECT_ID'],
    'address_full': ['ADDRESS', 'FULL_ADDRESS', 'ADDRESS_FULL', 'FULLADD']
}


class DatasetJoinWizard:
    """Wizard to analyze two datasets and generate join configuration"""

    def __init__(self, address_file: str, parcel_file: str, dataset_name: str):
        self.address_file = Path(address_file)
        self.parcel_file = Path(parcel_file)
        self.dataset_name = dataset_name
        self.address_records = []
        self.parcel_records = []
        self.address_fields = set()
        self.parcel_fields = set()
        self.potential_join_keys = []

    async def analyze_and_recommend(self):
        """Main wizard flow"""
        print("\n" + "=" * 80)
        print("DUAL DATASET DETECTED - JOIN CONFIGURATION WIZARD")
        print("=" * 80)

        # Load both datasets
        await self._load_datasets()

        # Detect potential join keys
        self._detect_join_keys()

        if not self.potential_join_keys:
            print("\n[FAIL] No common join keys found between datasets!")
            print("Available fields in address dataset:", sorted(self.address_fields))
            print("Available fields in parcel dataset:", sorted(self.parcel_fields))
            return

        # Analyze each potential join key
        for join_info in self.potential_join_keys:
            await self._analyze_join_quality(join_info)

        # Generate configuration
        self._generate_config()

    async def _load_datasets(self):
        """Load both address and parcel datasets"""
        print(f"Address dataset: {self.address_file.name}")
        print(f"Parcel dataset: {self.parcel_file.name}")
        print()

        # Load address dataset
        print(f"Loading address dataset...")
        self.address_records = self._load_geojson(self.address_file)
        self.address_fields = set(self.address_records[0].keys()) if self.address_records else set()
        print(f"✓ Loaded {len(self.address_records):,} address records")

        # Load parcel dataset
        print(f"Loading parcel dataset...")
        self.parcel_records = self._load_geojson(self.parcel_file)
        self.parcel_fields = set(self.parcel_records[0].keys()) if self.parcel_records else set()
        print(f"✓ Loaded {len(self.parcel_records):,} parcel records")
        print()

    def _load_geojson(self, file_path: Path) -> List[Dict]:
        """Load GeoJSON file"""
        with open(file_path, 'r') as f:
            data = json.load(f)

        if data.get('type') == 'FeatureCollection':
            features = data.get('features', [])
        elif data.get('type') == 'Feature':
            features = [data]
        else:
            raise ValueError("Invalid GeoJSON")

        records = []
        for feature in features:
            properties = feature.get('properties', {})
            geometry = feature.get('geometry', {})

            # Extract coordinates
            if geometry.get('type') == 'Point':
                coords = geometry.get('coordinates', [])
                if len(coords) >= 2:
                    properties['longitude'] = coords[0]
                    properties['latitude'] = coords[1]

            records.append(properties)

        return records

    def _detect_join_keys(self):
        """Find common fields that could be join keys"""
        print("=" * 80)
        print("ANALYZING DATASETS FOR POTENTIAL JOIN KEYS")
        print("=" * 80)
        print()

        print(f"ADDRESS DATASET FIELDS ({len(self.address_fields)} fields):")
        for i, field in enumerate(sorted(self.address_fields)[:10], 1):
            sample_value = self.address_records[0].get(field)
            if isinstance(sample_value, str):
                sample_str = f'"{sample_value[:30]}..."' if len(sample_value) > 30 else f'"{sample_value}"'
            else:
                sample_str = str(sample_value)
            print(f"  {i:2}. {field:<25} → {sample_str}")
        if len(self.address_fields) > 10:
            print(f"  ... and {len(self.address_fields) - 10} more fields")
        print()

        print(f"PARCEL DATASET FIELDS ({len(self.parcel_fields)} fields):")
        for i, field in enumerate(sorted(self.parcel_fields)[:10], 1):
            sample_value = self.parcel_records[0].get(field)
            if isinstance(sample_value, str):
                sample_str = f'"{sample_value[:30]}..."' if len(sample_value) > 30 else f'"{sample_value}"'
            else:
                sample_str = str(sample_value)
            print(f"  {i:2}. {field:<25} → {sample_str}")
        if len(self.parcel_fields) > 10:
            print(f"  ... and {len(self.parcel_fields) - 10} more fields")
        print()

        # Also check for pattern-based matches
        address_lower = {f.lower(): f for f in self.address_fields}
        parcel_lower = {f.lower(): f for f in self.parcel_fields}

        for join_type, patterns in JOIN_KEY_PATTERNS.items():
            for pattern in patterns:
                pattern_lower = pattern.lower()
                if pattern_lower in address_lower and pattern_lower in parcel_lower:
                    addr_field = address_lower[pattern_lower]
                    parcel_field = parcel_lower[pattern_lower]

                    # Calculate confidence
                    confidence = 'high' if addr_field == parcel_field else 'medium'

                    self.potential_join_keys.append({
                        'type': join_type,
                        'address_field': addr_field,
                        'parcel_field': parcel_field,
                        'pattern': pattern,
                        'confidence': confidence
                    })
                    break  # Only add first match for each type

        print(f"FOUND {len(self.potential_join_keys)} POTENTIAL JOIN KEYS\n")

    async def _analyze_join_quality(self, join_info: Dict):
        """Analyze quality of a potential join key"""
        addr_field = join_info['address_field']
        parcel_field = join_info['parcel_field']

        # Build indexes
        parcel_index = {}
        for record in self.parcel_records:
            key = record.get(parcel_field)
            if key:
                parcel_index[key] = record

        # Test join
        matched_count = 0
        matched_samples = []

        for addr_record in self.address_records[:1000]:  # Sample first 1000
            key = addr_record.get(addr_field)
            if key and key in parcel_index:
                matched_count += 1
                if len(matched_samples) < 5:
                    parcel_record = parcel_index[key]
                    matched_samples.append({
                        'key': key,
                        'address': addr_record,
                        'parcel': parcel_record
                    })

        # Calculate statistics
        addr_non_null = sum(1 for r in self.address_records if r.get(addr_field))
        parcel_non_null = sum(1 for r in self.parcel_records if r.get(parcel_field))

        addr_non_null_pct = (addr_non_null / len(self.address_records) * 100) if self.address_records else 0
        parcel_non_null_pct = (parcel_non_null / len(self.parcel_records) * 100) if self.parcel_records else 0

        # Extrapolate match rate
        sample_match_rate = matched_count / min(1000, len(self.address_records))
        estimated_matches = int(len(self.address_records) * sample_match_rate)
        match_rate_pct = sample_match_rate * 100

        # Store results
        join_info['addr_non_null'] = addr_non_null
        join_info['addr_non_null_pct'] = addr_non_null_pct
        join_info['parcel_non_null'] = parcel_non_null
        join_info['parcel_non_null_pct'] = parcel_non_null_pct
        join_info['estimated_matches'] = estimated_matches
        join_info['match_rate_pct'] = match_rate_pct
        join_info['matched_samples'] = matched_samples

        # Print analysis
        confidence_label = join_info['confidence'].upper()
        print(f"{len(self.potential_join_keys)}. {addr_field} (EXACT MATCH - {confidence_label} CONFIDENCE)")
        print(f"   Address dataset: {addr_non_null:,} non-null values ({addr_non_null_pct:.1f}%)")
        print(f"   Parcel dataset:  {parcel_non_null:,} non-null values ({parcel_non_null_pct:.1f}%)")

        # Show sample matched records
        if matched_samples:
            print(f"   \n   Sample matched records:")
            print(f"   ┌{'─' * 12}┬{'─' * 30}┬{'─' * 15}┬{'─' * 12}┬{'─' * 12}┐")
            print(f"   │ {addr_field:<10} │ {'Address':<28} │ {'City':<13} │ {'Lat':<10} │ {'Long':<10} │")
            print(f"   ├{'─' * 12}┼{'─' * 30}┼{'─' * 15}┼{'─' * 12}┼{'─' * 12}┤")

            for sample in matched_samples[:3]:
                key = str(sample['key'])[:10]
                addr_rec = sample['address']
                parcel_rec = sample['parcel']

                # Try to build address string
                addr_str = self._format_address(addr_rec)[:28]
                city = str(addr_rec.get('PLACE', addr_rec.get('CITY', '')))[:13]
                lat = parcel_rec.get('latitude', parcel_rec.get('LAT', ''))
                lon = parcel_rec.get('longitude', parcel_rec.get('LONG', ''))

                lat_str = f"{lat:.4f}" if isinstance(lat, (int, float)) else str(lat)[:10]
                lon_str = f"{lon:.4f}" if isinstance(lon, (int, float)) else str(lon)[:10]

                print(f"   │ {key:<10} │ {addr_str:<28} │ {city:<13} │ {lat_str:<10} │ {lon_str:<10} │")

            print(f"   └{'─' * 12}┴{'─' * 30}┴{'─' * 15}┴{'─' * 12}┴{'─' * 12}┘")

        print(f"   ✓ Join quality: {estimated_matches:,} matches ({match_rate_pct:.1f}% of address records)")
        print(f"                   {len(self.address_records) - estimated_matches:,} address records without parcel match")
        print()

    def _format_address(self, record: Dict) -> str:
        """Format address from record fields"""
        parts = []
        for field in ['STREETNO', 'ADDRESS_NUMBER', 'CIVIC_NO']:
            if record.get(field):
                parts.append(str(record[field]))
                break

        for field in ['STNAME', 'LINEAR_NAME', 'STREET_NAME']:
            if record.get(field):
                parts.append(str(record[field]))
                break

        for field in ['STSUF', 'LINEAR_NAME_TYPE', 'STREET_TYPE']:
            if record.get(field):
                parts.append(str(record[field]))
                break

        return ' '.join(parts) if parts else 'N/A'

    def _generate_config(self):
        """Generate copy-paste ready configuration"""
        if not self.potential_join_keys:
            return

        # Use best join key (highest match rate)
        best_join = max(self.potential_join_keys, key=lambda x: x.get('match_rate_pct', 0))

        print("=" * 80)
        print("RECOMMENDED CONFIGURATION")
        print("=" * 80)
        print()
        print(f"Add this to config/dataset-mappings.json under \"{self.dataset_name}\":\n")

        config = {
            "dual_dataset": {
                "enabled": True,
                "join_key": best_join['address_field'],
                "address_file_fields": list(self.address_fields - {'latitude', 'longitude'}),
                "parcel_file_fields": ["latitude", "longitude"],
                "join_strategy": "inner"
            }
        }

        print(json.dumps(config, indent=2))
        print()

        print("⚠️  NOTES:")
        print(f"- Recommended join key: '{best_join['address_field']}' ({best_join['match_rate_pct']:.1f}% match rate)")
        print(f"- {len(self.address_records) - best_join['estimated_matches']:,} address records ({100 - best_join['match_rate_pct']:.1f}%) will not have coordinates")
        print("- Consider using \"join_strategy\": \"left\" to keep addresses without parcels")
        print()

        print("NEXT STEPS:")
        print("1. Copy the dual_dataset configuration above")
        print("2. Open: config/dataset-mappings.json")
        print(f"3. Add the configuration under the \"{self.dataset_name}\" section")
        print("4. Re-run this command to perform the coverage test")
        print()
        print("Command to re-run:")
        print(f"  ./test_municipal_dataset_coverage.py \\")
        print(f"    --dataset-addresses {self.address_file.name} \\")
        print(f"    --dataset-parcels {self.parcel_file.name} \\")
        print(f"    --config {self.dataset_name} \\")
        print(f"    --sample 5000")
        print()
        print("=" * 80)


class MunicipalDatasetLoader:
    """Loads and parses municipal dataset files (GeoJSON or CSV)"""

    def __init__(self, file_path: str = None, address_file: str = None,
                 parcel_file: str = None, config_name: Optional[str] = None):
        # Determine mode
        if file_path:
            self.mode = 'single'
            self.file_path = self._resolve_file_path(file_path)
            self.address_file = None
            self.parcel_file = None
        elif address_file and parcel_file:
            self.mode = 'dual'
            self.file_path = None
            self.address_file = self._resolve_file_path(address_file)
            self.parcel_file = self._resolve_file_path(parcel_file)
        else:
            raise ValueError("Must provide either file_path OR both address_file and parcel_file")

        self.config_name = config_name
        self.records = []
        self.detected_fields = {}
        self.field_mapping = None
        self.dual_config = None

    def _resolve_file_path(self, file_path: str) -> Path:
        """
        Resolve file path, checking common locations if not found.

        Search order:
        1. Exact path as provided
        2. Relative to script directory
        3. In data_sources/ subdirectory
        """
        path = Path(file_path)

        # If absolute path or exists as-is, return it
        if path.is_absolute() or path.exists():
            return path

        # Try relative to script directory
        script_dir = Path(__file__).parent
        script_relative = script_dir / file_path
        if script_relative.exists():
            return script_relative

        # Try in data_sources/ subdirectory
        data_sources_path = script_dir / 'data_sources' / file_path
        if data_sources_path.exists():
            return data_sources_path

        # Return original path (will fail later with clear error)
        return path

    def load(self) -> List[Dict[str, Any]]:
        """Load dataset from file(s)"""
        if self.mode == 'single':
            return self._load_single()
        else:
            return self._load_dual()

    def _load_single(self) -> List[Dict[str, Any]]:
        """Load single dataset file"""
        if not self.file_path.exists():
            raise FileNotFoundError(f"Dataset file not found: {self.file_path}")

        # Detect file format
        suffix = self.file_path.suffix.lower()
        if suffix in ['.geojson', '.json']:
            return self._load_geojson()
        elif suffix == '.csv':
            return self._load_csv()
        else:
            raise ValueError(f"Unsupported file format: {self.file_path.suffix}")

    def _load_dual(self) -> List[Dict[str, Any]]:
        """Load and join two dataset files"""
        if not self.address_file.exists():
            raise FileNotFoundError(f"Address file not found: {self.address_file}")
        if not self.parcel_file.exists():
            raise FileNotFoundError(f"Parcel file not found: {self.parcel_file}")

        # Load dual config
        self._load_dual_config()

        if not self.dual_config:
            raise ValueError(f"No dual_dataset configuration found for '{self.config_name}'. Run wizard first.")

        # Load both files
        print(f"Loading address dataset: {self.address_file.name}")
        address_records = self._load_geojson_file(self.address_file)
        print(f"✓ Loaded {len(address_records):,} address records")

        print(f"Loading parcel dataset: {self.parcel_file.name}")
        parcel_records = self._load_geojson_file(self.parcel_file)
        print(f"✓ Loaded {len(parcel_records):,} parcel records")

        # Perform join
        print(f"Joining datasets on key: {self.dual_config['join_key']}")
        joined_records = self._perform_join(address_records, parcel_records)
        print(f"✓ Join complete: {len(joined_records):,} records")

        self.records = joined_records
        self._detect_fields()
        return joined_records

    def _load_dual_config(self):
        """Load dual dataset configuration"""
        config_path = Path(__file__).parent / 'config' / 'dataset-mappings.json'

        if not config_path.exists():
            return

        with open(config_path, 'r') as f:
            configs = json.load(f)

        if self.config_name in configs:
            self.dual_config = configs[self.config_name].get('dual_dataset')

    def _load_geojson_file(self, file_path: Path) -> List[Dict]:
        """Load a single GeoJSON file without field mapping"""
        with open(file_path, 'r') as f:
            data = json.load(f)

        if data.get('type') == 'FeatureCollection':
            features = data.get('features', [])
        elif data.get('type') == 'Feature':
            features = [data]
        else:
            raise ValueError("Invalid GeoJSON")

        records = []
        for feature in features:
            properties = feature.get('properties', {})
            geometry = feature.get('geometry', {})

            # Extract coordinates
            if geometry.get('type') == 'Point':
                coords = geometry.get('coordinates', [])
                if len(coords) >= 2:
                    properties['longitude'] = coords[0]
                    properties['latitude'] = coords[1]

            records.append(properties)

        return records

    def _perform_join(self, address_records: List[Dict], parcel_records: List[Dict]) -> List[Dict]:
        """Join address and parcel datasets"""
        join_key = self.dual_config['join_key']
        join_strategy = self.dual_config.get('join_strategy', 'inner')

        # Build parcel index
        parcel_index = {}
        for parcel in parcel_records:
            key = parcel.get(join_key)
            if key:
                parcel_index[key] = parcel

        # Perform join
        joined = []
        for address in address_records:
            key = address.get(join_key)

            if key and key in parcel_index:
                # Match found - merge records
                parcel = parcel_index[key]
                merged = {**address, **parcel}  # Parcel fields override address fields
                joined.append(merged)
            elif join_strategy == 'left':
                # Keep address even without parcel match
                joined.append(address)
            # For 'inner' join, skip addresses without parcel match

        return joined

    def _load_geojson(self) -> List[Dict[str, Any]]:
        """Load GeoJSON or JSON file"""
        print(f"Loading JSON file: {self.file_path}")

        with open(self.file_path, 'r') as f:
            data = json.load(f)

        # Handle standard GeoJSON formats
        if data.get('type') == 'FeatureCollection':
            features = data.get('features', [])
            records = []
            for feature in features:
                properties = feature.get('properties', {})
                geometry = feature.get('geometry', {})

                # Extract coordinates
                geom_type = geometry.get('type')
                coords = geometry.get('coordinates', [])

                if geom_type == 'Point' and len(coords) >= 2:
                    # Point: coordinates = [longitude, latitude]
                    properties['longitude'] = coords[0]
                    properties['latitude'] = coords[1]
                elif geom_type == 'MultiPoint' and len(coords) > 0:
                    # MultiPoint: coordinates = [[longitude, latitude], ...]
                    # Use first point
                    if len(coords[0]) >= 2:
                        properties['longitude'] = coords[0][0]
                        properties['latitude'] = coords[0][1]

                records.append(properties)

        elif data.get('type') == 'Feature':
            properties = data.get('properties', {})
            geometry = data.get('geometry', {})

            geom_type = geometry.get('type')
            coords = geometry.get('coordinates', [])

            if geom_type == 'Point' and len(coords) >= 2:
                properties['longitude'] = coords[0]
                properties['latitude'] = coords[1]
            elif geom_type == 'MultiPoint' and len(coords) > 0:
                if len(coords[0]) >= 2:
                    properties['longitude'] = coords[0][0]
                    properties['latitude'] = coords[0][1]

            records = [properties]

        # Handle simple JSON with 'addresses' array (Toronto format)
        elif 'addresses' in data and isinstance(data['addresses'], list):
            print(f"Detected addresses array format")
            records = data['addresses']

        # Handle simple JSON array
        elif isinstance(data, list):
            print(f"Detected JSON array format")
            records = data

        else:
            raise ValueError("Invalid JSON format: Expected GeoJSON (FeatureCollection/Feature) or JSON array with 'addresses' key")

        print(f"✓ Loaded {len(records):,} records from JSON")
        self.records = records
        self._detect_fields()
        return records

    def _load_csv(self) -> List[Dict[str, Any]]:
        """Load CSV file"""
        print(f"Loading CSV file: {self.file_path}")

        records = []
        with open(self.file_path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                records.append(row)

        print(f"✓ Loaded {len(records):,} records from CSV")
        self.records = records
        self._detect_fields()
        return records

    def _detect_fields(self):
        """Detect available fields in dataset"""
        if not self.records:
            return

        # Sample first record to detect fields
        sample = self.records[0]
        all_fields = set(sample.keys())

        print(f"\nDetected {len(all_fields)} fields in dataset")

        # Try to load config if provided
        if self.config_name:
            self.field_mapping = self._load_config(self.config_name)
        else:
            # Auto-detect field mappings
            self.field_mapping = self._auto_detect_mappings(all_fields)

        print(f"Field mapping:")
        for nar_field, source_field in self.field_mapping.items():
            print(f"  {nar_field:<20} ← {source_field}")

    def _load_config(self, config_name: str) -> Dict[str, str]:
        """Load field mapping from config file"""
        config_path = Path(__file__).parent / 'config' / 'dataset-mappings.json'

        if not config_path.exists():
            print(f"⚠ Config file not found: {config_path}")
            return {}

        with open(config_path, 'r') as f:
            configs = json.load(f)

        # Get NAR field mapping (intermediate → NAR fields)
        nar_mapping = configs.get('_nar_field_mapping', {})

        if config_name not in configs:
            print(f"⚠ Config '{config_name}' not found in dataset-mappings.json")
            return {}

        # Get source field mapping (source → intermediate)
        source_mapping = configs[config_name].get('field_mappings', {})

        # Combine: source → intermediate → NAR
        # Result: source field → NAR field
        final_mapping = {}
        for intermediate_field, source_field in source_mapping.items():
            if intermediate_field in nar_mapping:
                nar_field = nar_mapping[intermediate_field]
                final_mapping[nar_field] = source_field

        return final_mapping

    def _auto_detect_mappings(self, fields: set) -> Dict[str, str]:
        """Auto-detect field mappings using pattern matching"""
        mapping = {}

        # Common patterns for each field
        patterns = {
            'civic_no': ['street_number', 'civic_no', 'streetno', 'house_number', 'number', 'addr_num'],
            'official_street_name': ['street_name', 'stname', 'st_name', 'street'],
            'official_street_type': ['street_suffix', 'street_type', 'stsuf', 'st_type', 'suffix'],
            'official_street_dir': ['street_direction', 'street_dir', 'direction', 'dir'],
            'csd_eng_name': ['city', 'municipality', 'csd_name', 'muni'],
            'mail_postal_code': ['postal_code', 'postcode', 'postal'],
            'civic_no_suffix': ['civic_suffix', 'street_number_suffix', 'suffix'],
            'apt_no_label': ['unit', 'apt', 'apartment', 'apt_no', 'unit_number'],
            'latitude': ['latitude', 'lat', 'y'],
            'longitude': ['longitude', 'long', 'lon', 'x']
        }

        fields_lower = {f.lower(): f for f in fields}

        for nar_field, pattern_list in patterns.items():
            for pattern in pattern_list:
                if pattern in fields_lower:
                    mapping[nar_field] = fields_lower[pattern]
                    break

        return mapping

    def get_normalized_records(self) -> List[Dict[str, Any]]:
        """Get records with fields mapped to NAR schema"""
        normalized = []

        for record in self.records:
            norm_record = {}

            # Map fields using field_mapping
            for nar_field, source_field in self.field_mapping.items():
                if source_field in record:
                    value = record[source_field]
                    # Clean up value
                    if value and isinstance(value, str):
                        value = value.strip()
                    norm_record[nar_field] = value

            # Preserve coordinates if they exist (for FSA assignment)
            if 'latitude' in record:
                norm_record['latitude'] = record['latitude']
            if 'longitude' in record:
                norm_record['longitude'] = record['longitude']

            # Preserve FSA code if it exists (from add_fsa_to_dataset.py)
            if 'fsa_code' in record:
                norm_record['fsa_code'] = record['fsa_code']

            # Keep original record for reference
            norm_record['_original'] = record

            normalized.append(norm_record)

        return normalized

    def get_available_nar_fields(self) -> List[str]:
        """Get list of NAR field names that are available in this dataset"""
        fields = list(self.field_mapping.keys())

        # Add fsa_code if it exists in the dataset
        if self.records and 'fsa_code' in self.records[0]:
            fields.append('fsa_code')

        return fields


class NARMissingGeocodeQuery:
    """Queries NAR database for addresses without coordinates"""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.missing_geocode_records = []
        self.total_count = 0

    async def fetch_missing_geocodes(self, sample_size: Optional[int] = None) -> List[Dict[str, Any]]:
        """Fetch NAR addresses without coordinates"""
        async with self.pool.acquire() as conn:
            # Get total count first
            self.total_count = await conn.fetchval("""
                SELECT COUNT(*)
                FROM nar_addresses a
                LEFT JOIN nar_locations l ON a.loc_guid = l.loc_guid
                WHERE a.prov_code = 35
                    AND (l.bg_latitude IS NULL OR l.bg_longitude IS NULL OR l.loc_guid IS NULL)
            """)

            print(f"\nTotal NAR addresses without coordinates: {self.total_count:,}")

            # Fetch records (with optional sampling)
            if sample_size and sample_size > 0:
                print(f"Fetching sample of {sample_size:,} records...")
                # Use TABLESAMPLE for large samples, LIMIT with ORDER BY RANDOM() for small
                if sample_size > 10000:
                    query = f"""
                        SELECT
                            a.addr_guid,
                            a.civic_no,
                            a.civic_no_suffix,
                            a.apt_no_label,
                            a.official_street_name,
                            a.official_street_type,
                            a.official_street_dir,
                            a.csd_eng_name,
                            a.mail_postal_code
                        FROM nar_addresses a
                        LEFT JOIN nar_locations l ON a.loc_guid = l.loc_guid
                        WHERE a.prov_code = 35
                            AND (l.bg_latitude IS NULL OR l.bg_longitude IS NULL OR l.loc_guid IS NULL)
                        ORDER BY RANDOM()
                        LIMIT {sample_size}
                    """
                else:
                    query = f"""
                        SELECT
                            a.addr_guid,
                            a.civic_no,
                            a.civic_no_suffix,
                            a.apt_no_label,
                            a.official_street_name,
                            a.official_street_type,
                            a.official_street_dir,
                            a.csd_eng_name,
                            a.mail_postal_code
                        FROM nar_addresses a
                        LEFT JOIN nar_locations l ON a.loc_guid = l.loc_guid
                        WHERE a.prov_code = 35
                            AND (l.bg_latitude IS NULL OR l.bg_longitude IS NULL OR l.loc_guid IS NULL)
                        ORDER BY RANDOM()
                        LIMIT {sample_size}
                    """
            else:
                print(f"Fetching all {self.total_count:,} records...")
                query = """
                    SELECT
                        a.addr_guid,
                        a.civic_no,
                        a.civic_no_suffix,
                        a.apt_no_label,
                        a.official_street_name,
                        a.official_street_type,
                        a.official_street_dir,
                        a.csd_eng_name,
                        a.mail_postal_code
                    FROM nar_addresses a
                    LEFT JOIN nar_locations l ON a.loc_guid = l.loc_guid
                    WHERE a.prov_code = 35
                        AND (l.bg_latitude IS NULL OR l.bg_longitude IS NULL OR l.loc_guid IS NULL)
                """

            rows = await conn.fetch(query)
            self.missing_geocode_records = [dict(row) for row in rows]

        print(f"✓ Fetched {len(self.missing_geocode_records):,} NAR records")
        return self.missing_geocode_records

    async def fetch_coverage_stats(self, city_name: Optional[str] = None, postal_codes: Optional[List[str]] = None) -> Dict[str, int]:
        """
        Fetch NAR coverage statistics for a specific city and/or postal codes

        ENHANCED: Uses normalize_municipality_name() and municipality_aliases for proper matching

        Returns dict with:
        - city_total: Total NAR records for the city
        - city_without_coords: NAR records for city without coordinates
        - postal_total: Total NAR records matching postal codes
        - postal_without_coords: NAR records matching postal codes without coordinates
        """
        stats = {
            'city_total': 0,
            'city_without_coords': 0,
            'postal_total': 0,
            'postal_without_coords': 0
        }

        async with self.pool.acquire() as conn:
            # Query by city name if provided (ENHANCED with normalization and aliases)
            if city_name:
                # Total records for city
                stats['city_total'] = await conn.fetchval("""
                    SELECT COUNT(*)
                    FROM nar_addresses a
                    WHERE normalize_municipality_name(a.csd_eng_name) = normalize_municipality_name($1)
                       OR normalize_municipality_name(a.csd_eng_name) = (
                           SELECT canonical_name
                           FROM municipality_aliases
                           WHERE normalized_alias = normalize_municipality_name($1)
                           LIMIT 1
                       )
                """, city_name)

                # Records without coordinates for city
                stats['city_without_coords'] = await conn.fetchval("""
                    SELECT COUNT(*)
                    FROM nar_addresses a
                    LEFT JOIN nar_locations l ON a.loc_guid = l.loc_guid
                    WHERE (
                        normalize_municipality_name(a.csd_eng_name) = normalize_municipality_name($1)
                        OR normalize_municipality_name(a.csd_eng_name) = (
                            SELECT canonical_name
                            FROM municipality_aliases
                            WHERE normalized_alias = normalize_municipality_name($1)
                            LIMIT 1
                        )
                    )
                    AND (l.bg_latitude IS NULL OR l.bg_longitude IS NULL OR l.loc_guid IS NULL)
                """, city_name)

            # Query by postal codes if provided
            if postal_codes:
                # Build regex pattern for postal codes (e.g., '^(N1C|N1E|N1G)')
                postal_pattern = '^(' + '|'.join(postal_codes) + ')'

                # Total records for postal codes
                stats['postal_total'] = await conn.fetchval("""
                    SELECT COUNT(*)
                    FROM nar_addresses
                    WHERE mail_postal_code ~ $1
                """, postal_pattern)

                # Records without coordinates for postal codes
                stats['postal_without_coords'] = await conn.fetchval("""
                    SELECT COUNT(*)
                    FROM nar_addresses a
                    LEFT JOIN nar_locations l ON a.loc_guid = l.loc_guid
                    WHERE a.mail_postal_code ~ $1
                        AND (l.bg_latitude IS NULL OR l.bg_longitude IS NULL OR l.loc_guid IS NULL)
                """, postal_pattern)

        return stats


class FieldCombinationMatcher:
    """Uses field combination analysis to predict match quality"""

    def __init__(self):
        self.matrix = None
        self._load_matrix()

    def _load_matrix(self):
        """Load field combination matrix from analysis results"""
        matrix_path = Path(__file__).parent / 'nar_field_combination_matrix.json'

        if not matrix_path.exists():
            print(f"⚠ Field combination matrix not found: {matrix_path}")
            print(f"  Run analyze_nar_field_combinations.py first")
            self.matrix = None
            return

        with open(matrix_path, 'r') as f:
            self.matrix = json.load(f)

    def predict_match_quality(self, available_fields: List[str]) -> Dict[str, Any]:
        """Predict match quality based on available fields"""
        if not self.matrix:
            return {
                'predicted_unique_pct': 0.0,
                'predicted_ambiguous_pct': 0.0,
                'confidence_score': 0.0,
                'recommendation': 'Unable to predict - matrix not loaded'
            }

        # Remove coordinate fields (not used for matching)
        matching_fields = [f for f in available_fields if f not in ['latitude', 'longitude']]

        # Find matching combination in matrix
        field_set = set(matching_fields)

        for combo in self.matrix.get('field_combinations', []):
            combo_fields = set(combo['fields'])

            if field_set == combo_fields:
                return {
                    'predicted_unique_pct': combo['unique_pct'],
                    'predicted_ambiguous_pct': combo['ambiguous_pct'],
                    'confidence_score': min(0.99, combo['unique_pct'] / 100),
                    'recommendation': self._get_recommendation(combo['unique_pct']),
                    'exact_match': True
                }

        # Find closest subset match
        best_match = None
        best_overlap = 0

        for combo in self.matrix.get('field_combinations', []):
            combo_fields = set(combo['fields'])
            overlap = len(field_set.intersection(combo_fields))

            if overlap > best_overlap and field_set.issuperset(combo_fields):
                best_match = combo
                best_overlap = overlap

        if best_match:
            return {
                'predicted_unique_pct': best_match['unique_pct'],
                'predicted_ambiguous_pct': best_match['ambiguous_pct'],
                'confidence_score': min(0.99, best_match['unique_pct'] / 100),
                'recommendation': self._get_recommendation(best_match['unique_pct']),
                'exact_match': False,
                'note': 'Using closest field combination (dataset has additional fields)'
            }

        return {
            'predicted_unique_pct': 0.0,
            'predicted_ambiguous_pct': 0.0,
            'confidence_score': 0.0,
            'recommendation': 'No matching field combination found',
            'exact_match': False
        }

    def _get_recommendation(self, unique_pct: float) -> str:
        """Get recommendation based on unique match percentage"""
        if unique_pct >= 99:
            return "EXCELLENT - Use for high-confidence NAR enrichment"
        elif unique_pct >= 95:
            return "GOOD - Acceptable for NAR enrichment with validation"
        elif unique_pct >= 90:
            return "MODERATE - Manual review recommended for ambiguous cases"
        elif unique_pct >= 80:
            return "POOR - High ambiguity rate, use with caution"
        else:
            return "UNACCEPTABLE - Too many ambiguous matches for reliable enrichment"


class GeographicValidator:
    """Validates geographic alignment using FSA-municipality mappings"""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.fsa_muni_cache = {}  # Cache FSA-municipality mappings
        self.alias_cache = {}  # Cache municipality aliases

    async def analyze_geographic_coverage(self, municipal_records: List[Dict], available_fields: List[str]) -> Dict[str, Any]:
        """
        Analyze how well the municipal dataset aligns with FSA-municipality mappings

        Returns:
        - fsa_coverage: Percentage of municipal FSAs that have NAR mappings
        - muni_coverage: Whether municipality names match NAR
        - alignment_score: Overall geographic alignment score
        - warnings: List of potential data quality issues
        """
        warnings = []
        analysis = {
            'fsa_coverage_pct': 0.0,
            'municipality_matched': False,
            'alignment_score': 0.0,
            'warnings': warnings,
            'fsa_stats': {},
            'municipality_info': {}
        }

        # Skip if no geographic fields available
        if 'mail_postal_code' not in available_fields and 'csd_eng_name' not in available_fields:
            warnings.append("No geographic fields (postal code or city) available for validation")
            return analysis

        async with self.pool.acquire() as conn:
            # Analyze FSA coverage
            if 'mail_postal_code' in available_fields:
                fsa_codes = set()
                for record in municipal_records:
                    postal = record.get('mail_postal_code')
                    if postal and len(postal) >= 3:
                        fsa_codes.add(postal[:3])

                if fsa_codes:
                    # Check how many FSAs have mappings in our system
                    fsa_list = list(fsa_codes)
                    mapped_fsas = await conn.fetch("""
                        SELECT DISTINCT fsa_code
                        FROM fsa_municipality_mappings
                        WHERE fsa_code = ANY($1::text[])
                    """, fsa_list)

                    mapped_fsa_set = {row['fsa_code'] for row in mapped_fsas}
                    unmapped_fsas = fsa_codes - mapped_fsa_set

                    fsa_coverage_pct = (len(mapped_fsa_set) / len(fsa_codes) * 100) if fsa_codes else 0

                    analysis['fsa_stats'] = {
                        'total_fsas': len(fsa_codes),
                        'mapped_fsas': len(mapped_fsa_set),
                        'unmapped_fsas': len(unmapped_fsas),
                        'fsa_coverage_pct': fsa_coverage_pct,
                        'fsa_list': sorted(list(fsa_codes)),
                        'unmapped_fsa_list': sorted(list(unmapped_fsas))
                    }
                    analysis['fsa_coverage_pct'] = fsa_coverage_pct

                    if unmapped_fsas:
                        warnings.append(f"Found {len(unmapped_fsas)} FSAs without NAR mappings: {sorted(unmapped_fsas)}")

            # Analyze municipality name matching
            if 'csd_eng_name' in available_fields:
                city_names = set()
                for record in municipal_records:
                    city = record.get('csd_eng_name')
                    if city:
                        city_names.add(city)

                if city_names:
                    # Check if city names match known municipalities (with normalization)
                    city_list = list(city_names)
                    matched_cities = await conn.fetch("""
                        SELECT DISTINCT
                            $1::text as input_city,
                            normalize_municipality_name($1) as normalized_city,
                            m.municipality_name_short as matched_municipality,
                            CASE
                                WHEN EXISTS (
                                    SELECT 1 FROM municipality_aliases a
                                    WHERE a.normalized_alias = normalize_municipality_name($1)
                                ) THEN 'alias_match'
                                WHEN EXISTS (
                                    SELECT 1 FROM lower_municipal_boundaries b
                                    WHERE normalize_municipality_name(b.short_name) = normalize_municipality_name($1)
                                ) THEN 'direct_match'
                                ELSE 'no_match'
                            END as match_type
                        FROM (
                            SELECT municipality_name_short
                            FROM fsa_municipality_mappings
                            WHERE normalize_municipality_name(municipality_name_short) = normalize_municipality_name($1)
                            LIMIT 1
                        ) m
                    """, city_list[0])  # Check first city (assumes single city dataset)

                    if matched_cities and len(matched_cities) > 0:
                        match_info = matched_cities[0]
                        match_type = match_info['match_type']

                        analysis['municipality_matched'] = (match_type != 'no_match')
                        analysis['municipality_info'] = {
                            'input_city': match_info['input_city'],
                            'normalized_city': match_info['normalized_city'],
                            'matched_municipality': match_info['matched_municipality'],
                            'match_type': match_type
                        }

                        if match_type == 'alias_match':
                            warnings.append(f"Municipality '{match_info['input_city']}' matched via alias system")
                        elif match_type == 'no_match':
                            warnings.append(f"Municipality '{match_info['input_city']}' not found in NAR system")

            # Calculate overall alignment score
            alignment_score = 0.0
            if 'fsa_coverage_pct' in analysis:
                alignment_score += analysis['fsa_coverage_pct'] * 0.6  # FSA coverage worth 60%
            if analysis['municipality_matched']:
                alignment_score += 40.0  # Municipality match worth 40%

            analysis['alignment_score'] = min(100.0, alignment_score)

        return analysis

    async def validate_fsa_municipality_alignment(self, municipal_records: List[Dict]) -> Dict[str, Any]:
        """
        Validate that FSA codes in the dataset align with their municipality names

        Returns dict with:
        - total_checked: Number of records checked
        - aligned_count: Records where FSA+municipality are consistent
        - misaligned_count: Records where FSA+municipality conflict
        - alignment_rate: Percentage of aligned records
        - sample_misalignments: Examples of misaligned records
        """
        total_checked = 0
        aligned_count = 0
        misaligned_count = 0
        sample_misalignments = []

        async with self.pool.acquire() as conn:
            for record in municipal_records[:1000]:  # Sample first 1000
                postal = record.get('mail_postal_code')
                city = record.get('csd_eng_name')

                if not postal or not city or len(postal) < 3:
                    continue

                fsa_code = postal[:3]
                total_checked += 1

                # Check if this FSA+municipality combination exists in our mappings
                is_valid = await conn.fetchval("""
                    SELECT EXISTS (
                        SELECT 1 FROM fsa_municipality_mappings
                        WHERE fsa_code = $1
                          AND normalize_municipality_name(municipality_name_short) = normalize_municipality_name($2)
                    )
                """, fsa_code, city)

                if is_valid:
                    aligned_count += 1
                else:
                    misaligned_count += 1
                    if len(sample_misalignments) < 10:
                        # Get what municipality this FSA should map to
                        correct_muni = await conn.fetchval("""
                            SELECT municipality_name_short
                            FROM fsa_municipality_mappings
                            WHERE fsa_code = $1 AND is_primary = true
                            LIMIT 1
                        """, fsa_code)

                        sample_misalignments.append({
                            'fsa': fsa_code,
                            'dataset_city': city,
                            'expected_city': correct_muni or 'Unknown',
                            'address': f"{record.get('civic_no', '')} {record.get('official_street_name', '')}"
                        })

        alignment_rate = (aligned_count / total_checked * 100) if total_checked > 0 else 0

        return {
            'total_checked': total_checked,
            'aligned_count': aligned_count,
            'misaligned_count': misaligned_count,
            'alignment_rate': alignment_rate,
            'sample_misalignments': sample_misalignments
        }


class AddressNormalizer:
    """Normalizes addresses for consistent matching"""

    # Street type abbreviation mappings (full word -> standard abbreviation)
    STREET_TYPE_ABBREV = {
        'STREET': 'ST',
        'ROAD': 'RD',
        'DRIVE': 'DR',
        'AVENUE': 'AVE',
        'CRESCENT': 'CRES',
        'COURT': 'CRT',
        'PLACE': 'PL',
        'LANE': 'LANE',
        'PARKWAY': 'PKY',
        'BOULEVARD': 'BLVD',
        'WAY': 'WAY',
        'GARDENS': 'GDNS',
        'TRAIL': 'TRAIL',
        'CIRCLE': 'CIR',
        'SQUARE': 'SQ',
        'WALK': 'WALK',
        'RUN': 'RUN',
        'GLEN': 'GLEN',
        'CROSSING': 'CRSSNG',
        'TERRACE': 'TER',
        'GROVE': 'GRV',
        'HEIGHTS': 'HTS',
        'HILL': 'HILL',
        'PATH': 'PATH',
        'GATE': 'GATE',
        'RIDGE': 'RIDGE',
        'COMMON': 'COMMON',
        'GREEN': 'GREEN',
        'MEWS': 'MEWS',
    }

    # Direction abbreviation mappings (full word -> standard abbreviation)
    DIRECTION_ABBREV = {
        'NORTH': 'N',
        'SOUTH': 'S',
        'EAST': 'E',
        'WEST': 'W',
        'NORTHEAST': 'NE',
        'NORTHWEST': 'NW',
        'SOUTHEAST': 'SE',
        'SOUTHWEST': 'SW',
    }

    @staticmethod
    def normalize_postal_code(postal: Optional[str]) -> Optional[str]:
        """Normalize postal code"""
        if not postal:
            return None

        # Remove spaces, uppercase
        postal = postal.replace(' ', '').upper()

        # Validate format (A1A1A1)
        if len(postal) == 6 and postal[0].isalpha() and postal[1].isdigit():
            return postal

        return None

    @staticmethod
    def normalize_string(value: Optional[str]) -> Optional[str]:
        """Normalize string field"""
        if not value or not isinstance(value, str):
            return None

        # Strip, uppercase, remove extra spaces
        value = ' '.join(value.strip().upper().split())

        # Handle "None" string values (common in some datasets)
        if value == 'NONE':
            return None

        return value if value else None

    @staticmethod
    def normalize_civic_number(civic: Any) -> Optional[str]:
        """Normalize civic number"""
        if not civic:
            return None

        # Convert to string, strip
        civic_str = str(civic).strip()

        return civic_str if civic_str else None

    @classmethod
    def normalize_street_type(cls, value: Optional[str]) -> Optional[str]:
        """Normalize street type to standard abbreviation"""
        if not value or not isinstance(value, str):
            return None

        # Uppercase and strip
        value = value.strip().upper()

        # If it's already an abbreviation, return it
        if value in cls.STREET_TYPE_ABBREV.values():
            return value

        # If it's a full word, convert to abbreviation
        if value in cls.STREET_TYPE_ABBREV:
            return cls.STREET_TYPE_ABBREV[value]

        # Return as-is if not in mapping (e.g., rare or non-standard types)
        return value

    @classmethod
    def normalize_direction(cls, value: Optional[str]) -> Optional[str]:
        """Normalize direction to standard abbreviation"""
        if not value or not isinstance(value, str):
            return None

        # Uppercase and strip
        value = value.strip().upper()

        # If already abbreviated, return it
        if value in cls.DIRECTION_ABBREV.values():
            return value

        # If full word, convert to abbreviation
        if value in cls.DIRECTION_ABBREV:
            return cls.DIRECTION_ABBREV[value]

        # Return as-is if not in mapping
        return value

    @classmethod
    def normalize_address(cls, record: Dict[str, Any]) -> Dict[str, Optional[str]]:
        """Normalize all address fields in a record"""
        return {
            'civic_no': cls.normalize_civic_number(record.get('civic_no')),
            'civic_no_suffix': cls.normalize_string(record.get('civic_no_suffix')),
            'apt_no_label': cls.normalize_string(record.get('apt_no_label')),
            'official_street_name': cls.normalize_string(record.get('official_street_name')),
            'official_street_type': cls.normalize_street_type(record.get('official_street_type')),
            'official_street_dir': cls.normalize_direction(record.get('official_street_dir')),
            'csd_eng_name': cls.normalize_string(record.get('csd_eng_name')),
            'mail_postal_code': cls.normalize_postal_code(record.get('mail_postal_code')),
        }


class MatchingEngine:
    """
    Performs address matching between municipal and NAR datasets.

    Matching Strategy:
    - When fsa_code is available: Uses core fields only (civic_no, street_name, fsa_code)
      - Ignores optional fields (street_type, street_dir) for better match rates
      - Achieves 90%+ coverage for well-aligned datasets
    - Without fsa_code: Uses all available fields for more precise matching

    Features:
    - Municipality alias resolution for city names
    - Smart normalization preserving FSA codes
    - Confidence scoring based on match uniqueness
    """

    def __init__(self, municipal_records: List[Dict], nar_records: List[Dict], available_fields: List[str], pool: asyncpg.Pool = None):
        self.municipal_records = municipal_records
        self.nar_records = nar_records
        self.available_fields = [f for f in available_fields if f not in ['latitude', 'longitude']]
        self.pool = pool
        self.matches = []
        self.match_stats = {
            'unique_matches': 0,
            'ambiguous_matches': 0,
            'no_matches': 0,
            'total_nar_tested': len(nar_records)
        }
        self.muni_name_cache = {}  # Cache normalized municipality names

    async def match(self) -> List[Dict[str, Any]]:
        """Perform matching"""
        print(f"\nMatching {len(self.municipal_records):,} municipal records against {len(self.nar_records):,} NAR records...")

        using_fsa = 'fsa_code' in self.available_fields
        if using_fsa:
            core_fields = ['civic_no', 'official_street_name', 'fsa_code']
            print(f"Available fields: {', '.join(self.available_fields)}")
            print(f"Matching on core fields only: {', '.join(core_fields)} (street type/dir ignored for FSA matching)")
        else:
            print(f"Using fields: {', '.join(self.available_fields)}")

        # Normalize all records first
        # Note: municipal_records are already normalized from get_normalized_records()
        # but we normalize again to ensure consistency. Preserve fsa_code if present.
        municipal_normalized = [
            {
                **AddressNormalizer.normalize_address(rec),
                'fsa_code': rec.get('fsa_code'),  # Preserve FSA code
                '_original': rec
            }
            for rec in self.municipal_records
        ]

        nar_normalized = [
            {**AddressNormalizer.normalize_address(rec), '_original': rec}
            for rec in self.nar_records
        ]

        # If city field is available and we have DB access, normalize municipality names
        if 'csd_eng_name' in self.available_fields and self.pool:
            print("  Normalizing municipality names using database aliases...")
            municipal_normalized = await self._normalize_municipality_names(municipal_normalized)
            nar_normalized = await self._normalize_municipality_names(nar_normalized)
            print("  ✓ Municipality names normalized")

        # Build index for municipal records for fast lookup
        municipal_index = self._build_index(municipal_normalized)
        print(f"  Built index with {len(municipal_index):,} unique keys")

        # Debug: Show sample keys
        if len(municipal_index) > 0:
            sample_keys = list(municipal_index.keys())[:3]
            print(f"  Sample municipal keys: {sample_keys}")

        # Match each NAR record
        sample_nar_keys = []
        for i, nar_rec in enumerate(nar_normalized):
            match_result = self._find_matches(nar_rec, municipal_index)

            # Debug: Collect sample NAR keys for first 3 records
            if i < 3 and hasattr(self, '_last_nar_key'):
                sample_nar_keys.append(self._last_nar_key)

            if match_result['match_count'] == 1:
                self.match_stats['unique_matches'] += 1
            elif match_result['match_count'] > 1:
                self.match_stats['ambiguous_matches'] += 1
            else:
                self.match_stats['no_matches'] += 1

            self.matches.append(match_result)

        # Debug: Show sample NAR keys
        if sample_nar_keys:
            print(f"  Sample NAR keys: {sample_nar_keys}")

        print(f"✓ Matching complete")
        print(f"  Unique matches: {self.match_stats['unique_matches']:,}")
        print(f"  Ambiguous matches: {self.match_stats['ambiguous_matches']:,}")
        print(f"  No matches: {self.match_stats['no_matches']:,}")

        return self.matches

    async def _normalize_municipality_names(self, records: List[Dict]) -> List[Dict]:
        """
        Normalize municipality names using database function and aliases.
        Handles bilingual names, pre-amalgamation names, etc.
        """
        async with self.pool.acquire() as conn:
            for record in records:
                city = record.get('csd_eng_name')
                if city and city not in self.muni_name_cache:
                    # Query database for normalized/canonical municipality name
                    normalized = await conn.fetchval("""
                        SELECT COALESCE(
                            (SELECT canonical_name
                             FROM municipality_aliases
                             WHERE normalized_alias = normalize_municipality_name($1)
                             LIMIT 1),
                            normalize_municipality_name($1)
                        )
                    """, city)

                    self.muni_name_cache[city] = normalized

                # Update record with normalized municipality name
                if city and city in self.muni_name_cache:
                    record['csd_eng_name'] = self.muni_name_cache[city]

        return records

    def _build_index(self, records: List[Dict]) -> Dict[str, List[Dict]]:
        """Build index for fast lookups"""
        index = defaultdict(list)
        using_fsa = 'fsa_code' in self.available_fields

        # Determine which fields to use for matching
        # When using FSA, we can be more lenient with optional fields
        if using_fsa:
            # Core fields that are reliable: civic_no, street_name, fsa_code
            core_fields = ['civic_no', 'official_street_name', 'fsa_code']
        else:
            # Without FSA, use all available fields
            core_fields = self.available_fields

        for i, record in enumerate(records):
            # Create key based on core fields only
            key_parts = []
            for field in core_fields:
                value = record.get(field)
                if value:
                    key_parts.append(str(value))

            # Debug first record
            if i == 0 and using_fsa:
                print(f"  DEBUG first municipal record:")
                print(f"    civic_no: {repr(record.get('civic_no'))}")
                print(f"    official_street_name: {repr(record.get('official_street_name'))}")
                print(f"    fsa_code: {repr(record.get('fsa_code'))}")
                print(f"    original has fsa_code: {repr(record.get('_original', {}).get('fsa_code'))}")
                print(f"    Key built: {key_parts}")

            if key_parts:
                key = '||'.join(key_parts)
                index[key].append(record)

        return index

    def _find_matches(self, nar_record: Dict, municipal_index: Dict) -> Dict[str, Any]:
        """Find matches for a NAR record"""
        using_fsa = 'fsa_code' in self.available_fields

        # Determine which fields to use for matching (same as _build_index)
        if using_fsa:
            core_fields = ['civic_no', 'official_street_name', 'fsa_code']
        else:
            core_fields = self.available_fields

        # Build key for NAR record
        key_parts = []
        for field in core_fields:
            if field == 'fsa_code':
                # Extract FSA from NAR's postal code (first 3 characters)
                postal_code = nar_record.get('mail_postal_code')
                if postal_code and len(postal_code) >= 3:
                    fsa = postal_code[:3]
                    key_parts.append(fsa)
            else:
                value = nar_record.get(field)
                if value:
                    key_parts.append(str(value))

        if not key_parts:
            return {
                'nar_record': nar_record['_original'],
                'match_count': 0,
                'municipal_matches': [],
                'confidence': 0.0
            }

        key = '||'.join(key_parts)
        self._last_nar_key = key  # Store for debugging
        matches = municipal_index.get(key, [])

        # Calculate confidence based on match count
        # FSA-level matching gets slightly lower confidence than full postal code
        if using_fsa:
            if len(matches) == 1:
                confidence = 0.90  # Slightly lower than postal code match
            elif len(matches) <= 3:
                confidence = 0.80
            elif len(matches) <= 5:
                confidence = 0.70
            else:
                confidence = 0.60
        else:
            if len(matches) == 1:
                confidence = 0.95
            elif len(matches) <= 3:
                confidence = 0.85
            elif len(matches) <= 5:
                confidence = 0.75
            else:
                confidence = 0.65

        return {
            'nar_record': nar_record['_original'],
            'match_count': len(matches),
            'municipal_matches': [m['_original'] for m in matches],
            'confidence': confidence
        }


class CoverageReporter:
    """Generates comprehensive coverage reports"""

    def __init__(self, dataset_name: str, municipal_count: int, matches: List[Dict],
                 match_stats: Dict, prediction: Dict, available_fields: List[str],
                 nar_total_missing: int, coverage_stats: Dict = None,
                 coverage_city: str = None, coverage_postal_codes: List[str] = None,
                 output_dir: str = './reports', geo_analysis: Dict = None,
                 alignment_result: Dict = None):
        self.dataset_name = dataset_name
        self.municipal_count = municipal_count
        self.matches = matches
        self.match_stats = match_stats
        self.prediction = prediction
        self.available_fields = available_fields
        self.nar_total_missing = nar_total_missing
        self.coverage_stats = coverage_stats or {}
        self.coverage_city = coverage_city
        self.coverage_postal_codes = coverage_postal_codes or []
        self.geo_analysis = geo_analysis or {}
        self.alignment_result = alignment_result or {}
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)

    def generate_all_reports(self):
        """Generate all report types"""
        print(f"\nGenerating reports...")

        self.generate_summary_report()
        self.generate_match_breakdown_csv()
        self.generate_geographic_coverage_csv()
        self.generate_sample_matches_json()

        print(f"✓ All reports generated in: {self.output_dir}")

    def generate_summary_report(self):
        """Generate text summary report"""
        filename = self.output_dir / f"{self.dataset_name}_coverage_summary.txt"

        unique_count = self.match_stats['unique_matches']
        ambiguous_count = self.match_stats['ambiguous_matches']
        total_tested = self.match_stats['total_nar_tested']

        total_enrichable = unique_count + ambiguous_count

        # Determine the relevant NAR population for percentage calculations
        # Use city-specific NAR count if available and testing city-specific dataset
        relevant_nar_count = total_tested
        scope_label = "tested NAR records"

        if self.coverage_city and self.coverage_stats.get('city_without_coords', 0) > 0:
            relevant_nar_count = self.coverage_stats['city_without_coords']
            scope_label = f"{self.coverage_city} NAR without coords"
        elif self.coverage_postal_codes and self.coverage_stats.get('postal_without_coords', 0) > 0:
            relevant_nar_count = self.coverage_stats['postal_without_coords']
            scope_label = f"postal area NAR without coords"

        # Calculate percentages against relevant NAR population
        unique_pct = (unique_count / relevant_nar_count * 100) if relevant_nar_count > 0 else 0
        ambiguous_pct = (ambiguous_count / relevant_nar_count * 100) if relevant_nar_count > 0 else 0
        enrichable_pct = (total_enrichable / relevant_nar_count * 100) if relevant_nar_count > 0 else 0

        # Extrapolate to full relevant NAR population if we tested a sample
        if total_tested < relevant_nar_count:
            # We tested a sample, extrapolate to full population
            extrapolated_unique = int(unique_count / total_tested * relevant_nar_count)
            extrapolated_ambiguous = int(ambiguous_count / total_tested * relevant_nar_count)
            extrapolated_total = extrapolated_unique + extrapolated_ambiguous
            is_extrapolated = True
        else:
            # We tested the full population (or close to it), use actual numbers
            extrapolated_unique = unique_count
            extrapolated_ambiguous = ambiguous_count
            extrapolated_total = total_enrichable
            is_extrapolated = False

        with open(filename, 'w') as f:
            f.write("=" * 80 + "\n")
            f.write("MUNICIPAL DATASET COVERAGE ANALYSIS\n")
            f.write("=" * 80 + "\n")
            f.write(f"Dataset: {self.dataset_name}\n")
            f.write(f"Analysis Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"NAR Records Tested: {total_tested:,}")
            if total_tested < self.nar_total_missing:
                f.write(f" (sample of {self.nar_total_missing:,})\n")
            else:
                f.write(" (full analysis)\n")
            f.write("\n")

            f.write("FIELD ANALYSIS\n")
            f.write("-" * 80 + "\n")
            # Show fields used for matching (exclude coordinates)
            matching_fields = [f for f in self.available_fields if f not in ['bg_latitude', 'bg_longitude']]
            f.write(f"Fields used for matching: {', '.join(matching_fields)}\n")
            f.write(f"\n")
            f.write(f"Historical NAR Data Prediction:\n")
            f.write(f"  Based on province-wide NAR field combination analysis:\n")
            f.write(f"  Expected unique match rate: {self.prediction.get('predicted_unique_pct', 0):.2f}%\n")
            f.write(f"  Expected ambiguous rate: {self.prediction.get('predicted_ambiguous_pct', 0):.2f}%\n")
            f.write(f"  Enrichment recommendation: {self.prediction.get('recommendation', 'N/A')}\n")
            f.write("\n")

            # ENHANCED: Geographic Alignment Analysis
            if self.geo_analysis:
                f.write("GEOGRAPHIC ALIGNMENT ANALYSIS (ENHANCED)\n")
                f.write("-" * 80 + "\n")
                f.write(f"Overall geographic alignment score: {self.geo_analysis.get('alignment_score', 0):.1f}%\n")
                f.write("\n")

                # FSA Coverage
                if 'fsa_stats' in self.geo_analysis and self.geo_analysis['fsa_stats']:
                    fsa_stats = self.geo_analysis['fsa_stats']
                    f.write("FSA Coverage Analysis:\n")
                    f.write(f"  Total FSAs in dataset: {fsa_stats.get('total_fsas', 0)}\n")
                    f.write(f"  FSAs with NAR mappings: {fsa_stats.get('mapped_fsas', 0)}\n")
                    f.write(f"  FSA coverage rate: {fsa_stats.get('fsa_coverage_pct', 0):.1f}%\n")
                    if fsa_stats.get('fsa_list'):
                        f.write(f"  FSA codes: {', '.join(fsa_stats['fsa_list'])}\n")
                    if fsa_stats.get('unmapped_fsa_list'):
                        f.write(f"  ⚠ Unmapped FSAs: {', '.join(fsa_stats['unmapped_fsa_list'])}\n")
                    f.write("\n")

                # Municipality Matching
                if 'municipality_info' in self.geo_analysis and self.geo_analysis['municipality_info']:
                    muni_info = self.geo_analysis['municipality_info']
                    f.write("Municipality Matching:\n")
                    f.write(f"  Dataset city name: {muni_info.get('input_city', 'N/A')}\n")
                    f.write(f"  Normalized: {muni_info.get('normalized_city', 'N/A')}\n")
                    f.write(f"  Match type: {muni_info.get('match_type', 'N/A')}\n")
                    if muni_info.get('matched_municipality'):
                        f.write(f"  Matched to NAR municipality: {muni_info['matched_municipality']}\n")
                    f.write("\n")

                # FSA-Municipality Alignment
                if self.alignment_result and self.alignment_result.get('total_checked', 0) > 0:
                    f.write("FSA-Municipality Alignment Validation:\n")
                    f.write(f"  Records checked: {self.alignment_result.get('total_checked', 0):,}\n")
                    f.write(f"  Aligned records: {self.alignment_result.get('aligned_count', 0):,}\n")
                    f.write(f"  Misaligned records: {self.alignment_result.get('misaligned_count', 0):,}\n")
                    f.write(f"  Alignment rate: {self.alignment_result.get('alignment_rate', 0):.1f}%\n")

                    if self.alignment_result.get('sample_misalignments'):
                        f.write(f"\n  Sample Misalignments (data quality issues):\n")
                        for i, mis in enumerate(self.alignment_result['sample_misalignments'][:5], 1):
                            f.write(f"    {i}. FSA {mis['fsa']}: '{mis['dataset_city']}' should be '{mis['expected_city']}'\n")
                            f.write(f"       Address: {mis['address']}\n")
                    f.write("\n")

                # Geographic warnings
                if self.geo_analysis.get('warnings'):
                    f.write("Geographic Data Quality Warnings:\n")
                    for warning in self.geo_analysis['warnings']:
                        f.write(f"  ⚠ {warning}\n")
                    f.write("\n")


            f.write("COVERAGE SUMMARY\n")
            f.write("-" * 80 + "\n")
            f.write(f"Total municipal records: {self.municipal_count:,}\n")
            f.write(f"NAR records without coords (province-wide): {self.nar_total_missing:,}\n")
            f.write("\n")

            # Enhanced NAR Coverage Statistics
            if self.coverage_stats:
                f.write("NAR Coverage for This Geographic Area:\n")
                if self.coverage_city:
                    city_total = self.coverage_stats.get('city_total', 0)
                    city_no_coords = self.coverage_stats.get('city_without_coords', 0)
                    city_with_coords = city_total - city_no_coords
                    f.write(f"  By City ({self.coverage_city}):\n")
                    f.write(f"    Total NAR records:           {city_total:,}\n")
                    f.write(f"    With coordinates:            {city_with_coords:,} ({city_with_coords/city_total*100 if city_total > 0 else 0:.1f}%)\n")
                    f.write(f"    Without coordinates:         {city_no_coords:,} ({city_no_coords/city_total*100 if city_total > 0 else 0:.1f}%)\n")

                if self.coverage_postal_codes:
                    postal_total = self.coverage_stats.get('postal_total', 0)
                    postal_no_coords = self.coverage_stats.get('postal_without_coords', 0)
                    postal_with_coords = postal_total - postal_no_coords
                    f.write(f"  By Postal Codes ({', '.join(self.coverage_postal_codes)}):\n")
                    f.write(f"    Total NAR records:           {postal_total:,}\n")
                    f.write(f"    With coordinates:            {postal_with_coords:,} ({postal_with_coords/postal_total*100 if postal_total > 0 else 0:.1f}%)\n")
                    f.write(f"    Without coordinates:         {postal_no_coords:,} ({postal_no_coords/postal_total*100 if postal_total > 0 else 0:.1f}%)\n")

                # Municipal dataset size comparison
                f.write(f"\n")
                f.write(f"Municipal Dataset Assessment:\n")

                if self.coverage_city:
                    city_total = self.coverage_stats.get('city_total', 0)
                    city_no_coords = self.coverage_stats.get('city_without_coords', 0)
                    size_ratio = (self.municipal_count / city_total) if city_total > 0 else 0

                    f.write(f"  Dataset size: {self.municipal_count:,} addresses\n")
                    f.write(f"  NAR coverage ({self.coverage_city}): {city_total:,} addresses\n")
                    f.write(f"  Size comparison: Municipal is {size_ratio:.1f}x the size of NAR\n")
                    f.write(f"\n")

                    if size_ratio > 1.5:
                        f.write(f"  Note: Municipal dataset is significantly larger than NAR, indicating:\n")
                        f.write(f"    - Municipal may include non-residential or commercial addresses\n")
                        f.write(f"    - NAR coverage for {self.coverage_city} may be incomplete\n")
                        f.write(f"    - Different inclusion criteria between datasets\n")
                        f.write(f"    - Municipal dataset serves as valuable supplement and standalone source\n")
                    elif size_ratio < 0.5:
                        f.write(f"  Note: Municipal dataset is smaller than NAR, indicating:\n")
                        f.write(f"    - Municipal may focus on specific address types\n")
                        f.write(f"    - Provides targeted enrichment for subset of NAR gaps\n")
                    else:
                        f.write(f"  Note: Municipal and NAR datasets are comparable in size\n")
                        f.write(f"    - Good overlap expected for enrichment opportunities\n")

                f.write("\n")

            # Calculate no-match count for the relevant NAR population
            relevant_no_match = relevant_nar_count - total_enrichable

            f.write(f"Match Results (against {scope_label}: {relevant_nar_count:,}):\n")
            f.write(f"  ✓ Unique matches:        {unique_count:,} ({unique_pct:.1f}%)\n")
            f.write(f"  ⚠ Ambiguous matches:     {ambiguous_count:,} ({ambiguous_pct:.1f}%)\n")
            f.write(f"  ✗ No match found:        {relevant_no_match:,} ({100-enrichable_pct:.1f}%)\n")
            f.write("\n")

            # Geocode Enrichment Potential
            f.write("Geocode Enrichment Potential:\n")
            if is_extrapolated:
                f.write(f"  (Extrapolated from sample of {total_tested:,} to population of {relevant_nar_count:,})\n")
                f.write(f"  High confidence (unique): ~{extrapolated_unique:,} addresses ({extrapolated_unique/relevant_nar_count*100:.1f}%)\n")
                f.write(f"  Medium confidence (ambiguous): ~{extrapolated_ambiguous:,} addresses ({extrapolated_ambiguous/relevant_nar_count*100:.1f}%)\n")
                f.write(f"  Total enrichable: ~{extrapolated_total:,} addresses ({extrapolated_total/relevant_nar_count*100:.1f}%)\n")
            else:
                f.write(f"  High confidence (unique): {unique_count:,} addresses ({unique_pct:.1f}%)\n")
                f.write(f"  Medium confidence (ambiguous): {ambiguous_count:,} addresses ({ambiguous_pct:.1f}%)\n")
                f.write(f"  Total enrichable: {total_enrichable:,} addresses ({enrichable_pct:.1f}%)\n")
            f.write("\n")

            # Confidence distribution
            confidence_dist = Counter()
            for match in self.matches:
                if match['match_count'] > 0:
                    conf = match['confidence']
                    if conf >= 0.95:
                        confidence_dist['0.95-1.0'] += 1
                    elif conf >= 0.85:
                        confidence_dist['0.85-0.95'] += 1
                    elif conf >= 0.70:
                        confidence_dist['0.70-0.85'] += 1
                    else:
                        confidence_dist['<0.70'] += 1

            f.write("MATCH BREAKDOWN BY CONFIDENCE\n")
            f.write("-" * 80 + "\n")
            total_with_conf = sum(confidence_dist.values())
            for conf_range in ['0.95-1.0', '0.85-0.95', '0.70-0.85', '<0.70']:
                count = confidence_dist.get(conf_range, 0)
                pct = (count / total_with_conf * 100) if total_with_conf > 0 else 0
                f.write(f"Confidence {conf_range}: {count:,} matches ({pct:.1f}%)\n")
            f.write("\n")

            f.write("RECOMMENDATION\n")
            f.write("-" * 80 + "\n")
            if enrichable_pct >= 15:
                status = "✓ HIGH VALUE"
            elif enrichable_pct >= 8:
                status = "⚠ MODERATE VALUE"
            else:
                status = "✗ LOW VALUE"

            f.write(f"{status}: This dataset can enrich {enrichable_pct:.1f}% of NAR's missing geocodes.\n")
            f.write("\n")

            f.write("Files generated:\n")
            f.write(f"  - {self.dataset_name}_coverage_summary.txt\n")
            f.write(f"  - {self.dataset_name}_match_breakdown.csv\n")
            f.write(f"  - {self.dataset_name}_geographic_coverage.csv\n")
            f.write(f"  - {self.dataset_name}_sample_matches.json\n")
            f.write("=" * 80 + "\n")

        print(f"  ✓ Summary: {filename.name}")

    def generate_match_breakdown_csv(self):
        """Generate detailed match breakdown CSV"""
        filename = self.output_dir / f"{self.dataset_name}_match_breakdown.csv"

        with open(filename, 'w', newline='') as f:
            fieldnames = ['nar_addr_guid', 'nar_civic_no', 'nar_street_name', 'nar_city',
                         'nar_postal_code', 'match_count', 'confidence', 'match_type']
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            for match in self.matches[:1000]:  # Limit to first 1000 for CSV size
                nar = match['nar_record']

                match_type = 'no_match'
                if match['match_count'] == 1:
                    match_type = 'unique'
                elif match['match_count'] > 1:
                    match_type = 'ambiguous'

                writer.writerow({
                    'nar_addr_guid': nar.get('addr_guid', ''),
                    'nar_civic_no': nar.get('civic_no', ''),
                    'nar_street_name': nar.get('official_street_name', ''),
                    'nar_city': nar.get('csd_eng_name', ''),
                    'nar_postal_code': nar.get('mail_postal_code', ''),
                    'match_count': match['match_count'],
                    'confidence': match['confidence'],
                    'match_type': match_type
                })

        print(f"  ✓ Match breakdown: {filename.name}")

    def generate_geographic_coverage_csv(self):
        """Generate geographic coverage breakdown CSV"""
        filename = self.output_dir / f"{self.dataset_name}_geographic_coverage.csv"

        # Group by postal prefix (first 3 characters)
        postal_stats = defaultdict(lambda: {'total': 0, 'matched': 0})

        for match in self.matches:
            nar = match['nar_record']
            postal = nar.get('mail_postal_code', '')

            if postal and len(postal) >= 3:
                prefix = postal[:3]
                postal_stats[prefix]['total'] += 1
                if match['match_count'] > 0:
                    postal_stats[prefix]['matched'] += 1

        # Group by city
        city_stats = defaultdict(lambda: {'total': 0, 'matched': 0})

        for match in self.matches:
            nar = match['nar_record']
            city = nar.get('csd_eng_name', 'Unknown')

            city_stats[city]['total'] += 1
            if match['match_count'] > 0:
                city_stats[city]['matched'] += 1

        with open(filename, 'w', newline='') as f:
            fieldnames = ['geographic_unit', 'unit_name', 'total_nar_records',
                         'matched_records', 'coverage_pct']
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            # Write postal prefix stats
            for prefix, stats in sorted(postal_stats.items()):
                coverage_pct = (stats['matched'] / stats['total'] * 100) if stats['total'] > 0 else 0
                writer.writerow({
                    'geographic_unit': 'postal_prefix',
                    'unit_name': prefix,
                    'total_nar_records': stats['total'],
                    'matched_records': stats['matched'],
                    'coverage_pct': round(coverage_pct, 2)
                })

            # Write city stats
            for city, stats in sorted(city_stats.items()):
                coverage_pct = (stats['matched'] / stats['total'] * 100) if stats['total'] > 0 else 0
                writer.writerow({
                    'geographic_unit': 'city',
                    'unit_name': city,
                    'total_nar_records': stats['total'],
                    'matched_records': stats['matched'],
                    'coverage_pct': round(coverage_pct, 2)
                })

        print(f"  ✓ Geographic coverage: {filename.name}")

    def generate_sample_matches_json(self):
        """Generate sample matches in JSON format"""
        filename = self.output_dir / f"{self.dataset_name}_sample_matches.json"

        # Get sample matches (10 unique, 10 ambiguous, 5 no-match)
        unique_samples = [m for m in self.matches if m['match_count'] == 1][:10]
        ambiguous_samples = [m for m in self.matches if m['match_count'] > 1][:10]
        no_match_samples = [m for m in self.matches if m['match_count'] == 0][:5]

        samples = {
            'unique_matches': unique_samples,
            'ambiguous_matches': ambiguous_samples,
            'no_matches': no_match_samples
        }

        with open(filename, 'w') as f:
            json.dump(samples, f, indent=2, default=str)

        print(f"  ✓ Sample matches: {filename.name}")


async def main():
    """Main execution function"""
    parser = argparse.ArgumentParser(
        description='Test municipal dataset coverage against NAR missing geocodes'
    )
    parser.add_argument('--dataset', help='Path to municipal dataset file (single mode)')
    parser.add_argument('--dataset-addresses', help='Path to address dataset file (dual mode)')
    parser.add_argument('--dataset-parcels', help='Path to parcel dataset file (dual mode)')
    parser.add_argument('--config', help='Config name from dataset-mappings.json')
    parser.add_argument('--sample', type=int, default=10000, help='Sample size (0 for full analysis, default: 10000)')
    parser.add_argument('--output-dir', default='./reports', help='Output directory for reports')
    parser.add_argument('--format', choices=['geojson', 'csv'], help='File format (auto-detect if not specified)')

    args = parser.parse_args()

    # Validate arguments
    if args.dataset and (args.dataset_addresses or args.dataset_parcels):
        print("[FAIL] Cannot specify both --dataset and --dataset-addresses/--dataset-parcels")
        print("Use either single mode (--dataset) OR dual mode (--dataset-addresses + --dataset-parcels)")
        return

    if not args.dataset and not (args.dataset_addresses and args.dataset_parcels):
        print("[FAIL] Must specify either:")
        print("  - Single mode: --dataset FILE")
        print("  - Dual mode: --dataset-addresses FILE --dataset-parcels FILE")
        return

    # Determine mode
    if args.dataset:
        mode = 'single'
        dataset_name = Path(args.dataset).stem
    else:
        mode = 'dual'
        dataset_name = Path(args.dataset_addresses).stem

    print("\n" + "=" * 80)
    print("MUNICIPAL DATASET COVERAGE ANALYZER")
    print("=" * 80)
    print(f"Mode: {mode.upper()}")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    print()

    # If dual mode without config, launch wizard
    if mode == 'dual':
        if not args.config:
            print("⚠ No config specified for dual mode")
            print("Please specify --config NAME (will launch wizard if config doesn't exist)")
            return

        # Check if dual_dataset config exists
        config_path = Path(__file__).parent / 'config' / 'dataset-mappings.json'
        has_dual_config = False

        if config_path.exists():
            with open(config_path, 'r') as f:
                configs = json.load(f)
                if args.config in configs:
                    has_dual_config = configs[args.config].get('dual_dataset', {}).get('enabled', False)

        if not has_dual_config:
            # Launch wizard
            wizard = DatasetJoinWizard(args.dataset_addresses, args.dataset_parcels, args.config)
            await wizard.analyze_and_recommend()
            return  # Exit after wizard

    # Connect to database
    db_config = get_db_config()

    print(f"Connecting to database: {db_config['host']}:{db_config['port']}/{db_config['database']}")
    pool = await asyncpg.create_pool(
        host=db_config['host'],
        port=db_config['port'],
        database=db_config['database'],
        user=db_config['user'],
        password=db_config['password'],
        min_size=1, max_size=5, command_timeout=600
    )
    print("✓ Connected to database\n")

    try:
        # Step 1: Load municipal dataset
        print("=" * 80)
        print("STEP 1: LOAD MUNICIPAL DATASET")
        print("=" * 80)

        if mode == 'single':
            loader = MunicipalDatasetLoader(file_path=args.dataset, config_name=args.config)
        else:
            loader = MunicipalDatasetLoader(
                address_file=args.dataset_addresses,
                parcel_file=args.dataset_parcels,
                config_name=args.config
            )

        loader.load()
        normalized_records = loader.get_normalized_records()
        available_fields = loader.get_available_nar_fields()

        # Check if dataset has FSA codes (from add_fsa_to_dataset.py script)
        if loader.records and 'fsa_code' in loader.records[0].keys():
            fsa_count = sum(1 for r in normalized_records if r.get('fsa_code'))
            print(f"\n✓ Dataset has 'fsa_code' field ({fsa_count:,} records) - will use FSA-level matching")
        elif 'mail_postal_code' not in available_fields:
            print("\n⚠ Dataset has no postal codes or FSA codes")
            print("  Run './add_fsa_to_dataset.py' to add FSA codes for better matching")

        # Step 2: Analyze geographic coverage (ENHANCED with FSA-municipality validation)
        print("\n" + "=" * 80)
        print("STEP 2: ANALYZE GEOGRAPHIC COVERAGE")
        print("=" * 80)
        geo_validator = GeographicValidator(pool)
        geo_analysis = await geo_validator.analyze_geographic_coverage(normalized_records, available_fields)

        print(f"\nGeographic Alignment Analysis:")
        print(f"  Overall alignment score: {geo_analysis.get('alignment_score', 0):.1f}%")

        if 'fsa_stats' in geo_analysis and geo_analysis['fsa_stats']:
            fsa_stats = geo_analysis['fsa_stats']
            print(f"\n  FSA Coverage:")
            print(f"    Total FSAs in dataset: {fsa_stats.get('total_fsas', 0)}")
            print(f"    FSAs with NAR mappings: {fsa_stats.get('mapped_fsas', 0)}")
            print(f"    FSA coverage rate: {fsa_stats.get('fsa_coverage_pct', 0):.1f}%")
            if fsa_stats.get('unmapped_fsas', 0) > 0:
                print(f"    ⚠ Unmapped FSAs: {fsa_stats.get('unmapped_fsa_list', [])}")

        if 'municipality_info' in geo_analysis and geo_analysis['municipality_info']:
            muni_info = geo_analysis['municipality_info']
            print(f"\n  Municipality Matching:")
            print(f"    Dataset city: {muni_info.get('input_city', 'N/A')}")
            print(f"    Normalized: {muni_info.get('normalized_city', 'N/A')}")
            print(f"    Match type: {muni_info.get('match_type', 'N/A')}")
            if muni_info.get('matched_municipality'):
                print(f"    Matched to: {muni_info.get('matched_municipality')}")

        if geo_analysis.get('warnings'):
            print(f"\n  Geographic Warnings:")
            for warning in geo_analysis['warnings']:
                print(f"    ⚠ {warning}")

        # FSA-Municipality alignment validation
        if 'mail_postal_code' in available_fields and 'csd_eng_name' in available_fields:
            print(f"\n  Validating FSA-Municipality alignment...")
            alignment_result = await geo_validator.validate_fsa_municipality_alignment(normalized_records)
            print(f"    Records checked: {alignment_result.get('total_checked', 0)}")
            print(f"    Aligned: {alignment_result.get('aligned_count', 0)}")
            print(f"    Misaligned: {alignment_result.get('misaligned_count', 0)}")
            print(f"    Alignment rate: {alignment_result.get('alignment_rate', 0):.1f}%")

            if alignment_result.get('sample_misalignments'):
                print(f"\n    Sample Misalignments:")
                for i, mis in enumerate(alignment_result['sample_misalignments'][:5], 1):
                    print(f"      {i}. FSA {mis['fsa']}: '{mis['dataset_city']}' → expected '{mis['expected_city']}'")
                    print(f"         {mis['address']}")
        else:
            alignment_result = None

        # Step 3: Predict match quality
        print("\n" + "=" * 80)
        print("STEP 3: PREDICT MATCH QUALITY")
        print("=" * 80)
        matcher = FieldCombinationMatcher()
        prediction = matcher.predict_match_quality(available_fields)
        print(f"\nPrediction:")
        print(f"  Unique match rate: {prediction.get('predicted_unique_pct', 0):.2f}%")
        print(f"  Ambiguous match rate: {prediction.get('predicted_ambiguous_pct', 0):.2f}%")
        print(f"  Confidence score: {prediction.get('confidence_score', 0):.2f}")
        print(f"  Recommendation: {prediction.get('recommendation', 'N/A')}")

        # Step 4: Fetch NAR missing geocodes
        print("\n" + "=" * 80)
        print("STEP 4: FETCH NAR MISSING GEOCODES")
        print("=" * 80)
        nar_query = NARMissingGeocodeQuery(pool)
        nar_records = await nar_query.fetch_missing_geocodes(args.sample if args.sample > 0 else None)

        # Fetch NAR coverage statistics for this geographic area
        print("\nFetching NAR coverage statistics for this geographic area...")

        # Extract city name from normalized records
        city_names = set()
        for record in normalized_records:
            if record.get('csd_eng_name'):
                city_names.add(record['csd_eng_name'])

        # Use the most common city name (or first one if there's only one)
        coverage_city = list(city_names)[0] if len(city_names) == 1 else None
        if coverage_city is None and len(city_names) > 0:
            # If multiple cities, try to find the most common one
            city_counts = {}
            for record in normalized_records:
                city = record.get('csd_eng_name')
                if city:
                    city_counts[city] = city_counts.get(city, 0) + 1
            coverage_city = max(city_counts.items(), key=lambda x: x[1])[0] if city_counts else None

        # Get postal codes from config (authoritative source)
        coverage_postal_codes = None
        config_postal_codes = None

        if args.config:
            config_path = Path(__file__).parent / 'config' / 'dataset-mappings.json'
            if config_path.exists():
                with open(config_path, 'r') as f:
                    configs = json.load(f)
                    if args.config in configs:
                        config_postal_codes = configs[args.config].get('postal_prefixes', [])
                        if config_postal_codes:
                            coverage_postal_codes = sorted(config_postal_codes)
                            print(f"  Using postal codes from config: {', '.join(coverage_postal_codes)}")

        # If no config postal codes, fall back to extraction from data (with warning)
        if not coverage_postal_codes:
            print("  ⚠ Warning: No postal_prefixes in config, extracting from dataset (may include bad data)")
            postal_codes = set()
            for record in normalized_records:
                if record.get('mail_postal_code'):
                    postal_code = record['mail_postal_code']
                    if len(postal_code) >= 3:
                        postal_codes.add(postal_code[:3])
            coverage_postal_codes = sorted(list(postal_codes))

        # Data quality validation: check for postal codes in data that aren't in config
        if config_postal_codes and coverage_postal_codes:
            actual_postal_codes = set()
            for record in normalized_records:
                if record.get('mail_postal_code') and len(record['mail_postal_code']) >= 3:
                    actual_postal_codes.add(record['mail_postal_code'][:3])

            extra_codes = actual_postal_codes - set(config_postal_codes)
            if extra_codes:
                print(f"  ⚠ DATA QUALITY: Found {len(extra_codes)} postal codes not in config: {sorted(extra_codes)}")
                print(f"     These may indicate bad data in the municipal dataset")

        coverage_stats = await nar_query.fetch_coverage_stats(
            city_name=coverage_city,
            postal_codes=coverage_postal_codes if coverage_postal_codes else None
        )

        if coverage_city:
            print(f"  City: {coverage_city}")
            print(f"    NAR records (total): {coverage_stats['city_total']:,}")
            print(f"    NAR records (without coords): {coverage_stats['city_without_coords']:,}")

        if coverage_postal_codes:
            print(f"  Postal codes: {', '.join(coverage_postal_codes)}")
            print(f"    NAR records (total): {coverage_stats['postal_total']:,}")
            print(f"    NAR records (without coords): {coverage_stats['postal_without_coords']:,}")

        # Step 5: Perform matching
        print("\n" + "=" * 80)
        print("STEP 5: PERFORM ADDRESS MATCHING")
        print("=" * 80)
        engine = MatchingEngine(normalized_records, nar_records, available_fields, pool)
        matches = await engine.match()

        # Step 6: Generate reports
        print("\n" + "=" * 80)
        print("STEP 6: GENERATE REPORTS")
        print("=" * 80)
        reporter = CoverageReporter(
            dataset_name=dataset_name,
            municipal_count=len(normalized_records),
            matches=matches,
            match_stats=engine.match_stats,
            prediction=prediction,
            available_fields=available_fields,
            nar_total_missing=nar_query.total_count,
            coverage_stats=coverage_stats,
            coverage_city=coverage_city,
            coverage_postal_codes=coverage_postal_codes,
            output_dir=args.output_dir,
            geo_analysis=geo_analysis,
            alignment_result=alignment_result
        )
        reporter.generate_all_reports()

        print("\n" + "=" * 80)
        print("ANALYSIS COMPLETE")
        print("=" * 80)
        print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Reports saved to: {args.output_dir}")
        print("=" * 80)
        print()

    except Exception as e:
        print(f"\n[FAIL] Error during analysis: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await pool.close()


if __name__ == '__main__':
    asyncio.run(main())
