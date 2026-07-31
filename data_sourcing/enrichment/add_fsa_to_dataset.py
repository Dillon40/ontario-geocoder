#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "asyncpg>=0.29.0",
# ]
# ///
"""
Add FSA Codes to Dataset

Uses PostGIS spatial lookup to assign Forward Sortation Area (FSA) codes to
records based on coordinates. Adds a new 'fsa_code' field with 3-character FSA.

FSA codes are the first 3 characters of Canadian postal codes (e.g., "M4K", "K1A").
This script uses the fsa_boundaries table with ST_Contains() to perform reverse
geocoding from (latitude, longitude) to FSA.

Usage:
    # Add FSA codes to a GeoJSON dataset
    ./add_fsa_to_dataset.py --input toronto_address_dataset.geojson --output toronto_with_fsa.geojson

    # Specify custom field names
    ./add_fsa_to_dataset.py --input data.geojson --output data_fsa.geojson --lat-field lat --lon-field lon

    # Overwrite existing FSA field
    ./add_fsa_to_dataset.py --input data.geojson --output data_fsa.geojson --overwrite

Output:
    - Creates new dataset file with 'fsa_code' field added
    - Preserves all original fields
    - Reports statistics on FSA assignment success rate
"""

import asyncio
import asyncpg
import json
import argparse
import sys
from pathlib import Path
from typing import Dict, List, Any, Tuple
from datetime import datetime

# Add project root to path for db_utils import
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from data_sourcing.db_utils import get_db_config


async def assign_fsa_codes(
    records: List[Dict],
    pool: asyncpg.Pool,
    lat_field: str = 'latitude',
    lon_field: str = 'longitude',
    fsa_field: str = 'fsa_code',
    overwrite: bool = False
) -> Tuple[List[Dict], Dict[str, Any]]:
    """
    Assign FSA codes to records based on coordinates.

    Args:
        records: List of records with coordinate fields
        pool: Database connection pool
        lat_field: Name of latitude field
        lon_field: Name of longitude field
        fsa_field: Name of FSA field to create
        overwrite: Whether to overwrite existing FSA field

    Returns:
        Tuple of (enriched_records, stats)
    """
    stats = {
        'total_records': len(records),
        'records_with_coords': 0,
        'fsa_assigned': 0,
        'fsa_not_found': 0,
        'records_without_coords': 0,
        'already_has_fsa': 0,
        'unique_fsas': set()
    }

    enriched_records = []

    async with pool.acquire() as conn:
        for i, record in enumerate(records):
            # Check if FSA already exists
            if fsa_field in record and not overwrite:
                stats['already_has_fsa'] += 1
                enriched_records.append(record)
                continue

            # Get coordinates
            lat = record.get(lat_field)
            lon = record.get(lon_field)

            # Skip if no coordinates
            if lat is None or lon is None:
                stats['records_without_coords'] += 1
                enriched_records.append(record)
                continue

            stats['records_with_coords'] += 1

            # Query fsa_boundaries to find which FSA contains this point
            fsa_code = await conn.fetchval("""
                SELECT fsa_code
                FROM fsa_boundaries
                WHERE ST_Contains(
                    geometry,
                    ST_SetSRID(ST_MakePoint($1, $2), 4326)
                )
                LIMIT 1
            """, lon, lat)  # Note: PostGIS uses (lon, lat) order

            if fsa_code:
                # Assign FSA code
                record[fsa_field] = fsa_code
                stats['fsa_assigned'] += 1
                stats['unique_fsas'].add(fsa_code)
            else:
                # Point not within any FSA polygon (rare - possibly bad coordinates)
                stats['fsa_not_found'] += 1

            enriched_records.append(record)

            # Progress indicator for large datasets
            if (i + 1) % 10000 == 0:
                print(f"  Processed {i + 1:,} / {len(records):,} records...")

    # Convert set to sorted list for reporting
    stats['unique_fsas'] = sorted(list(stats['unique_fsas']))

    return enriched_records, stats


def load_geojson(file_path: Path) -> Tuple[Dict, List[Dict]]:
    """
    Load GeoJSON file and extract records.

    Returns:
        Tuple of (geojson_structure, records)
        - geojson_structure: The original GeoJSON structure for preservation
        - records: List of feature properties with coordinates
    """
    with open(file_path, 'r') as f:
        data = json.load(f)

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
                properties['longitude'] = coords[0]
                properties['latitude'] = coords[1]
            elif geom_type == 'MultiPoint' and len(coords) > 0:
                if len(coords[0]) >= 2:
                    properties['longitude'] = coords[0][0]
                    properties['latitude'] = coords[0][1]

            records.append({
                'properties': properties,
                'geometry': geometry,
                'type': feature.get('type', 'Feature')
            })

        return data, records

    else:
        raise ValueError("Only GeoJSON FeatureCollection format is supported")


def save_geojson(file_path: Path, geojson_structure: Dict, records: List[Dict]):
    """
    Save records back to GeoJSON file.

    Args:
        file_path: Output file path
        geojson_structure: Original GeoJSON structure
        records: Enriched records with FSA codes
    """
    # Rebuild features from records
    features = []
    for record in records:
        feature = {
            'type': record['type'],
            'properties': record['properties'],
            'geometry': record['geometry']
        }
        features.append(feature)

    # Update FeatureCollection
    output = {
        **geojson_structure,
        'features': features
    }

    # Write to file
    with open(file_path, 'w') as f:
        json.dump(output, f, indent=2)


async def main():
    """Main execution function"""
    parser = argparse.ArgumentParser(
        description='Add FSA codes to dataset using PostGIS spatial lookup'
    )
    parser.add_argument('--input', required=True, help='Input GeoJSON file')
    parser.add_argument('--output', required=True, help='Output GeoJSON file')
    parser.add_argument('--lat-field', default='latitude', help='Latitude field name (default: latitude)')
    parser.add_argument('--lon-field', default='longitude', help='Longitude field name (default: longitude)')
    parser.add_argument('--fsa-field', default='fsa_code', help='FSA field name to create (default: fsa_code)')
    parser.add_argument('--overwrite', action='store_true', help='Overwrite existing FSA field if present')

    args = parser.parse_args()

    print("=" * 80)
    print("ADD FSA CODES TO DATASET")
    print("=" * 80)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # Resolve input path
    input_path = Path(args.input)
    if not input_path.exists():
        # Try in data_sources/
        script_dir = Path(__file__).parent
        input_path = script_dir / 'data_sources' / args.input
        if not input_path.exists():
            raise FileNotFoundError(f"Input file not found: {args.input}")

    # Resolve output path
    output_path = Path(args.output)
    if not output_path.is_absolute():
        script_dir = Path(__file__).parent
        output_path = script_dir / 'data_sources' / args.output

    print(f"Input file:  {input_path}")
    print(f"Output file: {output_path}")
    print()

    # Load dataset
    print("Loading dataset...")
    geojson_structure, records = load_geojson(input_path)
    print(f"✓ Loaded {len(records):,} records")
    print()

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
    print("✓ Connected to database")
    print()

    # Assign FSA codes
    print("Assigning FSA codes using PostGIS spatial lookup...")
    print(f"  Latitude field:  {args.lat_field}")
    print(f"  Longitude field: {args.lon_field}")
    print(f"  FSA field:       {args.fsa_field}")
    print()

    # Extract just properties for processing
    properties_list = [r['properties'] for r in records]

    enriched_properties, stats = await assign_fsa_codes(
        properties_list,
        pool,
        lat_field=args.lat_field,
        lon_field=args.lon_field,
        fsa_field=args.fsa_field,
        overwrite=args.overwrite
    )

    # Update records with enriched properties
    for i, record in enumerate(records):
        record['properties'] = enriched_properties[i]

    await pool.close()

    # Print statistics
    print()
    print("=" * 80)
    print("FSA ASSIGNMENT STATISTICS")
    print("=" * 80)
    print(f"Total records:             {stats['total_records']:,}")
    print(f"Records with coordinates:  {stats['records_with_coords']:,}")
    print(f"FSAs successfully assigned: {stats['fsa_assigned']:,}")
    print(f"FSAs not found (bad coords): {stats['fsa_not_found']:,}")
    print(f"Records without coordinates: {stats['records_without_coords']:,}")
    if stats['already_has_fsa'] > 0:
        print(f"Already had FSA (skipped):  {stats['already_has_fsa']:,}")
    print()

    if stats['unique_fsas']:
        print(f"Unique FSAs found: {len(stats['unique_fsas'])}")
        print(f"FSA codes: {', '.join(stats['unique_fsas'])}")
        print()

    # Success rate
    if stats['records_with_coords'] > 0:
        success_rate = stats['fsa_assigned'] / stats['records_with_coords'] * 100
        print(f"Success rate: {success_rate:.1f}%")
        print()

    # Save output
    print("Saving enriched dataset...")
    save_geojson(output_path, geojson_structure, records)
    print(f"✓ Saved to: {output_path}")
    print()

    print("=" * 80)
    print("COMPLETE")
    print("=" * 80)
    print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()


if __name__ == '__main__':
    asyncio.run(main())
