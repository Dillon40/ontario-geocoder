#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "geopandas",
#   "sqlalchemy",
#   "geoalchemy2",
#   "psycopg2-binary",
#   "python-dotenv",
#   "asyncpg",
# ]
# ///

"""
Import Canadian FSA (Forward Sortation Area) boundary shapefiles into PostgreSQL.
Reprojects from EPSG:3347 to EPSG:4326 for compatibility with existing geocoder data.
"""
import sys
from pathlib import Path
from dotenv import load_dotenv
import geopandas as gpd
from sqlalchemy import create_engine, text

from import_tracker import SyncImportTracker
from import_utils import atomic_table_swap

# Add project root to path for db_utils import
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from data_sourcing.db_utils import get_db_config

# Load environment variables
load_dotenv()


def get_database_url():
    """Construct database URL from environment variables."""
    return get_db_config()['connection_string']


def import_fsa_boundaries(shapefile_path: str, table_name: str = 'fsa_boundaries'):
    """
    Import FSA boundaries from shapefile to PostgreSQL.

    Uses atomic staging table swap pattern to ensure zero query downtime:
    1. Import data to staging table ({table_name}_staging)
    2. Create all indexes on staging table
    3. Atomically swap staging table into production position

    Args:
        shapefile_path: Path to the .shp file
        table_name: Name of the table to create (default: fsa_boundaries)
    """
    staging_table = f"{table_name}_staging"

    # Use SyncImportTracker to record this import operation
    with SyncImportTracker('fsa_boundaries', shapefile_path, 'full') as tracker:
        print(f"Reading shapefile: {shapefile_path}")

        # Read shapefile
        gdf = gpd.read_file(shapefile_path)

        print(f"✓ Loaded {len(gdf)} FSA boundaries")
        print(f"  Original CRS: {gdf.crs}")
        print(f"  Columns: {list(gdf.columns)}")
        tracker.rows_processed = len(gdf)

        # Reproject to EPSG:4326 (WGS84) to match existing geocoder data
        print("\nReprojecting to EPSG:4326 (WGS84)...")
        gdf = gdf.to_crs(epsg=4326)
        print(f"✓ Reprojected to {gdf.crs}")

        # Rename columns for clarity
        gdf = gdf.rename(columns={
            'CFSAUID': 'fsa_code',
            'PRUID': 'province_id',
            'PRNAME': 'province_name',
            'LANDAREA': 'land_area_km2',
            'DGUID': 'dissemination_guid'
        })

        # Connect to database
        print(f"\nConnecting to database...")
        db_url = get_database_url()
        engine = create_engine(db_url)

        with engine.connect() as conn:
            # Check if PostGIS is enabled
            result = conn.execute(text("SELECT PostGIS_version();"))
            version = result.scalar()
            print(f"✓ Connected (PostGIS version: {version})")

            # Clean up any previous staging table (from failed runs)
            print(f"\nCleaning up any previous staging table...")
            conn.execute(text(f"DROP TABLE IF EXISTS {staging_table} CASCADE;"))
            conn.commit()

        # Import to STAGING table using geopandas
        print(f"Importing {len(gdf)} FSA boundaries to staging table '{staging_table}'...")
        gdf.to_postgis(
            name=staging_table,
            con=engine,
            if_exists='replace',
            index=False,
            dtype={'geometry': 'Geometry'}
        )
        print(f"✓ Data imported to staging table")

        # Create indexes on STAGING table
        print("\nCreating indexes on staging table...")
        with engine.connect() as conn:
            # Primary key
            conn.execute(text(f"""
                ALTER TABLE {staging_table}
                ADD COLUMN id SERIAL PRIMARY KEY;
            """))
            print("✓ Created primary key")

            # Spatial index (GIST)
            conn.execute(text(f"""
                CREATE INDEX IF NOT EXISTS idx_{staging_table}_geometry
                ON {staging_table} USING GIST(geometry);
            """))
            print("✓ Created spatial index on geometry")

            # Index on FSA code for fast lookups
            conn.execute(text(f"""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_{staging_table}_fsa_code
                ON {staging_table}(fsa_code);
            """))
            print("✓ Created unique index on fsa_code")

            # Index on province
            conn.execute(text(f"""
                CREATE INDEX IF NOT EXISTS idx_{staging_table}_province
                ON {staging_table}(province_id);
            """))
            print("✓ Created index on province_id")

            conn.commit()

        # ATOMIC SWAP: Promote staging table to production
        print(f"\nPerforming atomic table swap...")
        atomic_table_swap(engine, table_name, staging_table)

        # Rename indexes to production names (optional but cleaner)
        print("Renaming indexes to production names...")
        with engine.connect() as conn:
            conn.execute(text(f"ALTER INDEX IF EXISTS idx_{staging_table}_geometry RENAME TO idx_{table_name}_geometry;"))
            conn.execute(text(f"ALTER INDEX IF EXISTS idx_{staging_table}_fsa_code RENAME TO idx_{table_name}_fsa_code;"))
            conn.execute(text(f"ALTER INDEX IF EXISTS idx_{staging_table}_province RENAME TO idx_{table_name}_province;"))
            conn.commit()
        print("✓ Indexes renamed")

        # Display summary
        print("\n" + "="*60)
        print("IMPORT SUMMARY")
        print("="*60)

        with engine.connect() as conn:
            result = conn.execute(text(f"SELECT COUNT(*) FROM {table_name};"))
            count = result.scalar()
            print(f"Total FSAs imported: {count}")
            tracker.rows_inserted = count

            result = conn.execute(text(f"""
                SELECT province_name, COUNT(*) as count
                FROM {table_name}
                GROUP BY province_name
                ORDER BY count DESC;
            """))

            print("\nFSAs by Province:")
            for row in result:
                print(f"  {row[0]}: {row[1]}")

            print("\nExample query to find FSA for a point:")
            print(f"""
  SELECT fsa_code, province_name
  FROM {table_name}
  WHERE ST_Contains(
      geometry,
      ST_SetSRID(ST_MakePoint(-75.6972, 45.4215), 4326)
  );
            """)

            print("\nExample query to validate postal codes:")
            print(f"""
  SELECT p.postal_code, f.fsa_code
  FROM properties p
  LEFT JOIN {table_name} f
      ON LEFT(p.postal_code, 3) = f.fsa_code
  WHERE f.fsa_code IS NULL
  LIMIT 10;
            """)

        print(f"\n✓ Import completed successfully! (Tracked as import ID: {tracker.import_id})")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='Import Canadian FSA boundary shapefiles to PostgreSQL'
    )
    parser.add_argument(
        '--shapefile',
        default='statcan_FSA_shapes/lfsa000a21a_e/lfsa000a21a_e.shp',
        help='Path to FSA shapefile (default: statcan_FSA_shapes/lfsa000a21a_e/lfsa000a21a_e.shp)'
    )
    parser.add_argument(
        '--table',
        default='fsa_boundaries',
        help='Table name (default: fsa_boundaries)'
    )

    args = parser.parse_args()

    # Validate shapefile exists
    shapefile_path = Path(args.shapefile)
    if not shapefile_path.exists():
        print(f"Error: Shapefile not found: {shapefile_path}")
        sys.exit(1)

    try:
        import_fsa_boundaries(str(shapefile_path), args.table)
    except Exception as e:
        print(f"\n✗ Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
