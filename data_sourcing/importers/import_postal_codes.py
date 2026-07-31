#!/usr/bin/env python3
"""
Import commercial postal code database from MySQL dump into PostgreSQL.

Uses:
- parse_mysql_postal_dump.py for parsing MySQL INSERT statements
- SyncImportTracker for audit trail and import history
- Atomic staging table swap for zero-downtime imports
- Batch inserts for performance (~1000 records/batch)

Usage:
    DB_HOST=localhost uv run python import_postal_codes.py

    # Or with explicit path:
    DB_HOST=localhost uv run python import_postal_codes.py --file path/to/cdn_db.sql
"""

import sys
from pathlib import Path
from typing import List, Dict, Any

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from import_tracker import SyncImportTracker
from import_utils import atomic_table_swap
from parse_mysql_postal_dump import parse_mysql_dump

# Add project root to path for db_utils import
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from data_sourcing.db_utils import get_db_config

# Load environment variables
load_dotenv()

# Default path to MySQL dump file
DEFAULT_DUMP_PATH = 'data_sourcing/data_sources/postal_db/cdn_db.sql'

# Table names
TABLE_NAME = 'commercial_postal_codes'
STAGING_TABLE = f'{TABLE_NAME}_staging'


def get_database_url():
    """Construct database URL from environment variables."""
    return get_db_config()['connection_string']


def create_staging_table(engine) -> None:
    """Create the staging table for import."""
    print(f"Creating staging table '{STAGING_TABLE}'...")

    with engine.connect() as conn:
        # Drop any existing staging table
        conn.execute(text(f"DROP TABLE IF EXISTS {STAGING_TABLE} CASCADE;"))

        # Create staging table matching production schema
        conn.execute(text(f"""
            CREATE TABLE {STAGING_TABLE} (
                id SERIAL PRIMARY KEY,
                postal_code_raw VARCHAR(7) NOT NULL,
                postal_code VARCHAR(6) NOT NULL,
                fsa_code VARCHAR(3) NOT NULL,
                city VARCHAR(100),
                city_normalized VARCHAR(100),
                uri VARCHAR(100),
                latitude DECIMAL(12,8),
                longitude DECIMAL(12,8),
                location GEOGRAPHY(Point, 4326),
                source_community_id INTEGER,
                source_county_id INTEGER,
                source_region_id INTEGER,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            );
        """))
        conn.commit()

    print(f"✓ Staging table created")


def batch_insert(engine, records: List[Dict[str, Any]]) -> int:
    """
    Insert a batch of records into staging table.

    Uses normalize_municipality_name() SQL function for city normalization.
    Creates PostGIS GEOGRAPHY points from lat/lon coordinates.

    Returns number of rows inserted.
    """
    if not records:
        return 0

    with engine.connect() as conn:
        for record in records:
            # Build the INSERT with city normalization and geography creation
            conn.execute(text(f"""
                INSERT INTO {STAGING_TABLE} (
                    postal_code_raw, postal_code, fsa_code,
                    city, city_normalized, uri,
                    latitude, longitude, location,
                    source_community_id, source_county_id, source_region_id
                ) VALUES (
                    :postal_code_raw, :postal_code, :fsa_code,
                    :city,
                    normalize_municipality_name(:city),
                    :uri,
                    :latitude, :longitude,
                    CASE
                        WHEN :latitude IS NOT NULL AND :longitude IS NOT NULL
                        THEN ST_SetSRID(ST_MakePoint(:longitude, :latitude), 4326)::geography
                        ELSE NULL
                    END,
                    :source_community_id, :source_county_id, :source_region_id
                );
            """), {
                'postal_code_raw': record['postal_code_raw'],
                'postal_code': record['postal_code'],
                'fsa_code': record['fsa_code'],
                'city': record['city'],
                'uri': record['uri'],
                'latitude': record['latitude'],
                'longitude': record['longitude'],
                'source_community_id': record['source_community_id'],
                'source_county_id': record['source_county_id'],
                'source_region_id': record['source_region_id'],
            })
        conn.commit()

    return len(records)


def create_indexes(engine) -> None:
    """Create indexes on staging table before swap."""
    print("\nCreating indexes on staging table...")

    with engine.connect() as conn:
        # Index on normalized postal code
        conn.execute(text(f"""
            CREATE INDEX idx_{STAGING_TABLE}_postal_code
            ON {STAGING_TABLE}(postal_code);
        """))
        print("✓ Created index on postal_code")

        # Index on FSA code
        conn.execute(text(f"""
            CREATE INDEX idx_{STAGING_TABLE}_fsa
            ON {STAGING_TABLE}(fsa_code);
        """))
        print("✓ Created index on fsa_code")

        # Index on normalized city
        conn.execute(text(f"""
            CREATE INDEX idx_{STAGING_TABLE}_city_norm
            ON {STAGING_TABLE}(city_normalized);
        """))
        print("✓ Created index on city_normalized")

        # Spatial index on location
        conn.execute(text(f"""
            CREATE INDEX idx_{STAGING_TABLE}_location
            ON {STAGING_TABLE} USING GIST(location);
        """))
        print("✓ Created spatial index on location")

        # Index on raw postal code (non-unique - source data has duplicates)
        conn.execute(text(f"""
            CREATE INDEX idx_{STAGING_TABLE}_raw
            ON {STAGING_TABLE}(postal_code_raw);
        """))
        print("✓ Created index on postal_code_raw")

        conn.commit()


def rename_indexes(engine) -> None:
    """Rename indexes from staging to production names after swap."""
    print("\nRenaming indexes to production names...")

    with engine.connect() as conn:
        index_renames = [
            ('postal_code', 'comm_postal_code'),
            ('fsa', 'comm_postal_fsa'),
            ('city_norm', 'comm_postal_city_norm'),
            ('location', 'comm_postal_location'),
            ('raw', 'comm_postal_raw'),
        ]

        for suffix, new_suffix in index_renames:
            old_name = f"idx_{STAGING_TABLE}_{suffix}"
            new_name = f"idx_{new_suffix}"
            conn.execute(text(f"ALTER INDEX IF EXISTS {old_name} RENAME TO {new_name};"))

        conn.commit()

    print("✓ Indexes renamed")


def import_postal_codes(dump_path: str) -> None:
    """
    Import postal codes from MySQL dump file.

    Args:
        dump_path: Path to MySQL dump file (.sql)
    """
    dump_path = Path(dump_path)
    if not dump_path.exists():
        raise FileNotFoundError(f"MySQL dump file not found: {dump_path}")

    # Initialize database connection
    print(f"Connecting to database...")
    db_url = get_database_url()
    engine = create_engine(db_url)

    with engine.connect() as conn:
        result = conn.execute(text("SELECT PostGIS_version();"))
        version = result.scalar()
        print(f"✓ Connected (PostGIS version: {version})")

        # Verify normalize_municipality_name function exists
        result = conn.execute(text("""
            SELECT EXISTS (
                SELECT 1 FROM pg_proc WHERE proname = 'normalize_municipality_name'
            );
        """))
        if not result.scalar():
            print("✗ Error: normalize_municipality_name() function not found.")
            print("  Run database migrations first.")
            sys.exit(1)
        print("✓ normalize_municipality_name() function available")

    # Track import
    with SyncImportTracker('commercial_postal_codes', str(dump_path), 'full') as tracker:
        # Create staging table
        create_staging_table(engine)

        # Parse and insert in batches
        print(f"\nParsing and importing from: {dump_path}")
        print("-" * 60)

        total_inserted = 0
        batch_count = 0

        for batch in parse_mysql_dump(str(dump_path), batch_size=1000):
            inserted = batch_insert(engine, batch)
            total_inserted += inserted
            batch_count += 1

            if batch_count % 50 == 0:
                print(f"  Processed {total_inserted:,} records ({batch_count} batches)...")

        print(f"\n✓ Inserted {total_inserted:,} records in {batch_count} batches")
        tracker.rows_processed = total_inserted
        tracker.rows_inserted = total_inserted

        # Create indexes before swap
        create_indexes(engine)

        # Atomic swap: staging -> production
        print(f"\nPerforming atomic table swap...")
        atomic_table_swap(engine, TABLE_NAME, STAGING_TABLE)
        print("✓ Staging table swapped to production")

        # Rename indexes
        rename_indexes(engine)

        # Display summary
        print("\n" + "=" * 60)
        print("IMPORT SUMMARY")
        print("=" * 60)

        with engine.connect() as conn:
            # Total count
            result = conn.execute(text(f"SELECT COUNT(*) FROM {TABLE_NAME};"))
            count = result.scalar()
            print(f"Total postal codes imported: {count:,}")

            # Unique FSAs
            result = conn.execute(text(f"SELECT COUNT(DISTINCT fsa_code) FROM {TABLE_NAME};"))
            fsa_count = result.scalar()
            print(f"Unique FSA codes: {fsa_count}")

            # Unique cities
            result = conn.execute(text(f"SELECT COUNT(DISTINCT city_normalized) FROM {TABLE_NAME} WHERE city_normalized IS NOT NULL;"))
            city_count = result.scalar()
            print(f"Unique cities (normalized): {city_count}")

            # Records with coordinates
            result = conn.execute(text(f"SELECT COUNT(*) FROM {TABLE_NAME} WHERE location IS NOT NULL;"))
            geo_count = result.scalar()
            print(f"Records with coordinates: {geo_count:,}")

            # Sample data
            print("\nSample records:")
            result = conn.execute(text(f"""
                SELECT postal_code_raw, fsa_code, city, city_normalized, latitude, longitude
                FROM {TABLE_NAME}
                ORDER BY id
                LIMIT 5;
            """))
            for row in result:
                print(f"  {row[0]} | {row[1]} | {row[2]} -> {row[3]} | ({row[4]}, {row[5]})")

        print(f"\n✓ Import completed successfully! (Import ID: {tracker.import_id})")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='Import commercial postal code database from MySQL dump'
    )
    parser.add_argument(
        '--file',
        default=DEFAULT_DUMP_PATH,
        help=f'Path to MySQL dump file (default: {DEFAULT_DUMP_PATH})'
    )

    args = parser.parse_args()

    try:
        import_postal_codes(args.file)
    except Exception as e:
        print(f"\n✗ Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
