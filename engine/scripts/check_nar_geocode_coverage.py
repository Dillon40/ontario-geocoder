#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "asyncpg>=0.29.0",
# ]
# ///
"""
NAR Database Geocode Coverage Report

Quickly reports what percentage of NAR addresses have geocode data.
"""

import asyncio
import asyncpg
import os
import sys
from datetime import datetime

# StatCan province code for Ontario (mirrors engine.config.ONTARIO_PROV_CODE;
# duplicated here since this script is self-contained and does not import
# the engine package).
ONTARIO_PROV_CODE = 35


async def check_geocode_coverage():
    """Check what percentage of NAR addresses have geocode data"""

    # Database configuration
    db_password = os.getenv('DB_PASSWORD')
    if not db_password:
        sys.exit(
            "DB_PASSWORD environment variable is required. "
            "Set it to your PostgreSQL password."
        )

    db_config = {
        'host': os.getenv('DB_HOST', 'localhost'),
        'port': int(os.getenv('DB_PORT', 5432)),
        'database': os.getenv('DB_NAME', 'geocoder'),
        'user': os.getenv('DB_USER', 'geocoder'),
        'password': db_password
    }

    print("\n" + "=" * 80)
    print("NAR DATABASE GEOCODE COVERAGE REPORT")
    print("=" * 80)
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Database: {db_config['host']}:{db_config['port']}/{db_config['database']}")
    print("=" * 80)
    print()

    # Connect to database
    conn = await asyncpg.connect(**db_config)

    try:
        # Get total addresses
        print("Querying NAR database...")
        total_addresses = await conn.fetchval(
            f"SELECT COUNT(*) FROM nar_addresses WHERE prov_code = {ONTARIO_PROV_CODE}"
        )

        # Get addresses with coordinates
        with_coords = await conn.fetchval(f"""
            SELECT COUNT(*)
            FROM nar_addresses a
            JOIN nar_locations l ON a.loc_guid = l.loc_guid
            WHERE a.prov_code = {ONTARIO_PROV_CODE}
                AND l.bg_latitude IS NOT NULL
                AND l.bg_longitude IS NOT NULL
        """)

        # Get addresses without coordinates
        without_coords = total_addresses - with_coords

        # Calculate percentages
        with_coords_pct = (with_coords / total_addresses * 100) if total_addresses > 0 else 0
        without_coords_pct = 100 - with_coords_pct

        # Print results
        print()
        print("RESULTS:")
        print("-" * 80)
        print(f"Total NAR Addresses (Ontario):      {total_addresses:>15,}")
        print()
        print(f"Addresses WITH geocodes:             {with_coords:>15,}  ({with_coords_pct:>6.2f}%)")
        print(f"Addresses WITHOUT geocodes:          {without_coords:>15,}  ({without_coords_pct:>6.2f}%)")
        print("-" * 80)
        print()

        # Summary message
        if with_coords_pct >= 90:
            status = "✓ EXCELLENT"
        elif with_coords_pct >= 75:
            status = "✓ GOOD"
        elif with_coords_pct >= 50:
            status = "⚠ MODERATE"
        else:
            status = "✗ POOR"

        print(f"Coverage Status: {status}")
        print()

        # Additional breakdown by province (if requested in future)
        print("Breakdown by Province:")
        print("-" * 80)

        province_stats = await conn.fetch("""
            SELECT
                a.prov_code,
                COUNT(*) as total,
                COUNT(CASE WHEN l.bg_latitude IS NOT NULL THEN 1 END) as with_coords,
                ROUND(COUNT(CASE WHEN l.bg_latitude IS NOT NULL THEN 1 END)::numeric / COUNT(*)::numeric * 100, 2) as coverage_pct
            FROM nar_addresses a
            LEFT JOIN nar_locations l ON a.loc_guid = l.loc_guid
            GROUP BY a.prov_code
            ORDER BY a.prov_code
        """)

        for row in province_stats:
            prov_name = get_province_name(row['prov_code'])
            print(f"  {prov_name:<20} {row['total']:>12,} addresses  |  {row['coverage_pct']:>6.2f}% coverage")

        print("-" * 80)
        print()

    finally:
        await conn.close()

    print("=" * 80)
    print()


def get_province_name(prov_code):
    """Convert province code to name"""
    province_map = {
        10: "Newfoundland",
        11: "Prince Edward Island",
        12: "Nova Scotia",
        13: "New Brunswick",
        24: "Quebec",
        35: "Ontario",
        46: "Manitoba",
        47: "Saskatchewan",
        48: "Alberta",
        59: "British Columbia",
        60: "Yukon",
        61: "Northwest Terr.",
        62: "Nunavut"
    }
    return province_map.get(prov_code, f"Province {prov_code}")


if __name__ == '__main__':
    asyncio.run(check_geocode_coverage())
