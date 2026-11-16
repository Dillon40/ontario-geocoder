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
Import Ontario Lower and Single Tier Municipal boundary shapefiles into PostgreSQL.
Reprojects to EPSG:4326 for compatibility with existing geocoder data.
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


def import_municipal_boundaries(shapefile_path: str, table_name: str = 'lower_municipal_boundaries'):
    """
    Import Ontario municipal boundaries from shapefile to PostgreSQL.

    Uses atomic staging table swap pattern to ensure zero query downtime:
    1. Import data to staging table ({table_name}_staging)
    2. Create all indexes on staging table
    3. Atomically swap staging table into production position

    Args:
        shapefile_path: Path to the .shp file
        table_name: Name of the table to create (default: lower_municipal_boundaries)
    """
    staging_table = f"{table_name}_staging"

    # Use SyncImportTracker to record this import operation
    with SyncImportTracker('ontario_municipal_boundaries', shapefile_path, 'full') as tracker:
        print(f"Reading shapefile: {shapefile_path}")

        # Read shapefile
        gdf = gpd.read_file(shapefile_path)

        print(f"✓ Loaded {len(gdf)} municipal boundaries")
        print(f"  Original CRS: {gdf.crs}")
        print(f"  Columns: {list(gdf.columns)}")
        tracker.rows_processed = len(gdf)

        # Display first record to understand the data
        print(f"\nFirst record sample:")
        for col in gdf.columns:
            if col != 'geometry':
                print(f"  {col}: {gdf.iloc[0][col]}")

        # Reproject to EPSG:4326 (WGS84) to match existing geocoder data
        if gdf.crs != 'EPSG:4326':
            print(f"\nReprojecting from {gdf.crs} to EPSG:4326 (WGS84)...")
            gdf = gdf.to_crs(epsg=4326)
            print(f"✓ Reprojected to {gdf.crs}")

        # Rename columns for clarity
        # The Ontario LIO shapefile has truncated column names (10-char limit):
        #   MUNICIPAL_ = tier type (Single Tier / Lower Tier)
        #   MUNICIPA_1 = French tier type
        #   MUNICIPA_2 = Full English name (e.g., "TOWNSHIP OF ARMOUR")
        #   MUNICIPA_3 = French full name
        #   MUNID = Municipal ID
        #   MAH_CODE = Ministry code
        #   MUNICIPA_6 = Type prefix (e.g., "TOWNSHIP OF")
        #   MUNICIPA_8 = Short English name (e.g., "ARMOUR")
        #   UPPER_TI_1 = Upper tier name (for lower tier municipalities)
        column_mapping = {
            'MUNICIPAL_TYPE': 'tier_type',
            'MUNICIPAL_NAME': 'name',
            'MUNICIPAL_NAME_SHORTFORM': 'short_name',
            'UPPER_TIER_MUNICIPALITY': 'upper_tier_name',
        }
        column_mapping = {k: v for k, v in column_mapping.items() if k in gdf.columns}

        # Map known truncated column names from Ontario LIO shapefile
        col_names = [col.upper() for col in gdf.columns]

        # Map specific known columns
        if 'MUNICIPAL_' in col_names:
            column_mapping['MUNICIPAL_'] = 'tier_type'
        if 'MUNICIPA_2' in col_names:
            column_mapping['MUNICIPA_2'] = 'name'
        if 'MUNICIPA_8' in col_names:
            column_mapping['MUNICIPA_8'] = 'short_name'
        if 'MUNID' in col_names:
            column_mapping['MUNID'] = 'municipal_id'
        if 'UPPER_TI_1' in col_names:
            column_mapping['UPPER_TI_1'] = 'upper_tier_name'

        # Fallback: try generic patterns for other shapefiles
        for col in gdf.columns:
            if col in column_mapping:
                continue
            col_lower = col.lower()
            if 'munid' in col_lower or 'mun_id' in col_lower:
                column_mapping[col] = 'municipal_id'
            elif 'full_name' in col_lower or 'name_en' in col_lower:
                column_mapping[col] = 'name'
            elif ('tier' in col_lower and 'type' in col_lower) or col_lower == 'municipal_':
                column_mapping[col] = 'tier_type'
            elif 'area' in col_lower and 'land' in col_lower:
                column_mapping[col] = 'land_area_km2'

        if column_mapping:
            print(f"\nRenaming columns: {column_mapping}")
            gdf = gdf.rename(columns=column_mapping)

        required = {'municipal_id', 'name', 'short_name', 'tier_type'}
        if not required.issubset(gdf.columns) or gdf.empty:
            raise ValueError(f'Municipal boundary schema missing {required - set(gdf.columns)} or empty')
        gdf.geometry = gdf.geometry.make_valid()

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
        print(f"Importing {len(gdf)} municipal boundaries to staging table '{staging_table}'...")
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

            # Check if we have a municipal_id column and create index
            result = conn.execute(text(f"""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = '{staging_table}'
                AND column_name IN ('municipal_id', 'name');
            """))
            columns = [row[0] for row in result]

            if 'municipal_id' in columns:
                conn.execute(text(f"""
                    CREATE INDEX IF NOT EXISTS idx_{staging_table}_municipal_id
                    ON {staging_table}(municipal_id);
                """))
                print("✓ Created index on municipal_id")

            if 'name' in columns:
                conn.execute(text(f"""
                    CREATE INDEX IF NOT EXISTS idx_{staging_table}_name
                    ON {staging_table}(name);
                """))
                print("✓ Created index on name")

            # Check for tier_type column
            result = conn.execute(text(f"""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = '{staging_table}'
                AND column_name = 'tier_type';
            """))
            if result.fetchone():
                conn.execute(text(f"""
                    CREATE INDEX IF NOT EXISTS idx_{staging_table}_tier_type
                    ON {staging_table}(tier_type);
                """))
                print("✓ Created index on tier_type")

            conn.commit()

        # ATOMIC SWAP: Promote staging table to production
        print(f"\nPerforming atomic table swap...")
        atomic_table_swap(engine, table_name, staging_table)

        # Rename indexes to production names (optional but cleaner)
        print("Renaming indexes to production names...")
        with engine.connect() as conn:
            conn.execute(text(f"ALTER INDEX IF EXISTS idx_{staging_table}_geometry RENAME TO idx_{table_name}_geometry;"))
            conn.execute(text(f"ALTER INDEX IF EXISTS idx_{staging_table}_municipal_id RENAME TO idx_{table_name}_municipal_id;"))
            conn.execute(text(f"ALTER INDEX IF EXISTS idx_{staging_table}_name RENAME TO idx_{table_name}_name;"))
            conn.execute(text(f"ALTER INDEX IF EXISTS idx_{staging_table}_tier_type RENAME TO idx_{table_name}_tier_type;"))
            conn.commit()
        print("✓ Indexes renamed")

        # Display summary
        print("\n" + "="*60)
        print("IMPORT SUMMARY")
        print("="*60)

        with engine.connect() as conn:
            result = conn.execute(text(f"SELECT COUNT(*) FROM {table_name};"))
            count = result.scalar()
            print(f"Total municipalities imported: {count}")
            tracker.rows_inserted = count

            # Try to group by tier type if available
            result = conn.execute(text(f"""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = '{table_name}'
                AND column_name LIKE '%tier%';
            """))
            tier_columns = [row[0] for row in result]

            if tier_columns:
                tier_col = tier_columns[0]
                result = conn.execute(text(f"""
                    SELECT {tier_col}, COUNT(*) as count
                    FROM {table_name}
                    GROUP BY {tier_col}
                    ORDER BY count DESC;
                """))

                print(f"\nMunicipalities by {tier_col}:")
                for row in result:
                    print(f"  {row[0]}: {row[1]}")

            print("\nExample query to find municipality for a point (e.g., Toronto City Hall):")
            print(f"""
  SELECT *
  FROM {table_name}
  WHERE ST_Contains(
      geometry,
      ST_SetSRID(ST_MakePoint(-79.3832, 43.6532), 4326)
  );
            """)

            print("\nExample query to validate properties against municipal boundaries:")
            print(f"""
  SELECT p.id, p.address, m.name as municipality
  FROM properties p
  LEFT JOIN {table_name} m
      ON ST_Contains(m.geometry, ST_SetSRID(ST_MakePoint(p.longitude, p.latitude), 4326))
  WHERE p.latitude IS NOT NULL AND p.longitude IS NOT NULL
  LIMIT 10;
            """)

        print(f"\n✓ Import completed successfully! (Tracked as import ID: {tracker.import_id})")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='Import Ontario municipal boundary shapefiles to PostgreSQL'
    )
    parser.add_argument(
        '--shapefile',
        default='data_sourcing/data_sources/ontario_municipal_boundaries/Municipal_Boundary_-_Lower_and_Single_Tier.shp',
        help='Path to municipal shapefile (default: data_sourcing/data_sources/ontario_municipal_boundaries/Municipal_Boundary_-_Lower_and_Single_Tier.shp)'
    )
    parser.add_argument(
        '--table',
        default='lower_municipal_boundaries',
        help='Table name (default: lower_municipal_boundaries)'
    )

    args = parser.parse_args()

    # Validate shapefile exists
    shapefile_path = Path(args.shapefile)
    if not shapefile_path.exists():
        print(f"Error: Shapefile not found: {shapefile_path}")
        sys.exit(1)

    try:
        import_municipal_boundaries(str(shapefile_path), args.table)
    except Exception as e:
        print(f"\n✗ Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
