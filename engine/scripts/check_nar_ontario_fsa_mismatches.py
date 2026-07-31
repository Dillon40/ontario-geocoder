#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "sqlalchemy",
#   "psycopg2-binary",
#   "python-dotenv",
# ]
# ///

"""
Check for NAR database records with FSA codes that don't match Ontario FSAs.
Identifies postal codes in nar_addresses where the FSA (first 3 characters)
doesn't exist in the fsa_boundaries table for Ontario.
"""
import os
import sys
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

# Load environment variables
load_dotenv()


def get_database_url():
    """Construct database URL from environment variables."""
    host = os.getenv('DB_HOST', 'localhost')
    port = os.getenv('DB_PORT', '5432')
    database = os.getenv('DB_NAME', 'geocoder')
    user = os.getenv('DB_USER', 'geocoder')
    password = os.getenv('DB_PASSWORD')
    if not password:
        sys.exit(
            "DB_PASSWORD environment variable is required. "
            "Set it to your PostgreSQL password."
        )

    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


def check_fsa_mismatches():
    """
    Find NAR addresses whose FSA codes don't match any Ontario FSA.
    """
    db_url = get_database_url()
    engine = create_engine(db_url)

    print("Connecting to database...")

    with engine.connect() as conn:
        # First, get count of total NAR addresses
        result = conn.execute(text("""
            SELECT COUNT(*)
            FROM nar_addresses
            WHERE mail_postal_code IS NOT NULL
              AND mail_postal_code != ''
        """))
        total_count = result.scalar()
        print(f"✓ Total NAR addresses with postal codes: {total_count:,}")

        # Get count of Ontario FSAs
        result = conn.execute(text("""
            SELECT COUNT(*)
            FROM fsa_boundaries
            WHERE province_name = 'Ontario'
        """))
        ontario_fsa_count = result.scalar()
        print(f"✓ Ontario FSAs in database: {ontario_fsa_count}")

        print("\n" + "="*70)
        print("CHECKING FOR MISMATCHES")
        print("="*70)

        # Find NAR addresses with FSAs that don't match Ontario FSAs
        query = text("""
            WITH nar_fsa AS (
                SELECT
                    id,
                    addr_guid,
                    mail_postal_code,
                    UPPER(LEFT(mail_postal_code, 3)) as fsa_code,
                    civic_no,
                    official_street_name,
                    csd_eng_name,
                    prov_code
                FROM nar_addresses
                WHERE mail_postal_code IS NOT NULL
                  AND mail_postal_code != ''
                  AND LENGTH(mail_postal_code) >= 3
            )
            SELECT
                n.fsa_code,
                COUNT(*) as count,
                COUNT(DISTINCT n.csd_eng_name) as city_count,
                STRING_AGG(DISTINCT n.csd_eng_name, ', ' ORDER BY n.csd_eng_name) as cities,
                n.prov_code
            FROM nar_fsa n
            LEFT JOIN fsa_boundaries f
                ON n.fsa_code = f.fsa_code
                AND f.province_name = 'Ontario'
            WHERE f.fsa_code IS NULL
            GROUP BY n.fsa_code, n.prov_code
            ORDER BY count DESC
        """)

        result = conn.execute(query)
        mismatches = result.fetchall()

        if mismatches:
            print(f"\n⚠ Found {len(mismatches)} FSA codes that don't match Ontario FSAs:\n")
            print(f"{'FSA':<6} {'Prov':<5} {'Count':<8} {'Cities':<4} Cities")
            print("-" * 70)

            total_mismatched = 0
            for row in mismatches:
                fsa_code, count, city_count, cities, prov_code = row
                total_mismatched += count
                cities_display = cities[:50] + "..." if cities and len(cities) > 50 else cities or ""
                print(f"{fsa_code:<6} {prov_code or 'N/A':<5} {count:>7,} {city_count:>6} {cities_display}")

            print("-" * 70)
            print(f"Total addresses with non-Ontario FSAs: {total_mismatched:,}")
            print(f"Percentage of total: {100 * total_mismatched / total_count:.2f}%")

            # Show sample records for investigation
            print("\n" + "="*70)
            print("SAMPLE RECORDS (first 10 mismatches)")
            print("="*70)

            sample_query = text("""
                WITH nar_fsa AS (
                    SELECT
                        mail_postal_code,
                        UPPER(LEFT(mail_postal_code, 3)) as fsa_code,
                        civic_no,
                        official_street_name,
                        official_street_type,
                        csd_eng_name,
                        prov_code
                    FROM nar_addresses
                    WHERE mail_postal_code IS NOT NULL
                      AND mail_postal_code != ''
                      AND LENGTH(mail_postal_code) >= 3
                )
                SELECT
                    n.mail_postal_code,
                    n.fsa_code,
                    n.civic_no || ' ' ||
                    COALESCE(n.official_street_name, '') || ' ' ||
                    COALESCE(n.official_street_type, '') as address,
                    n.csd_eng_name,
                    n.prov_code
                FROM nar_fsa n
                LEFT JOIN fsa_boundaries f
                    ON n.fsa_code = f.fsa_code
                    AND f.province_name = 'Ontario'
                WHERE f.fsa_code IS NULL
                LIMIT 10
            """)

            result = conn.execute(sample_query)
            samples = result.fetchall()

            for row in samples:
                postal, fsa, address, city, prov = row
                print(f"\nPostal: {postal}")
                print(f"  FSA: {fsa}")
                print(f"  Address: {address}")
                print(f"  City: {city}")
                print(f"  Province Code: {prov}")

        else:
            print("\n✓ All NAR postal codes have FSAs matching Ontario FSAs!")

        # Additional analysis: Check if these are from other provinces
        print("\n" + "="*70)
        print("PROVINCE CODE ANALYSIS")
        print("="*70)

        prov_query = text("""
            WITH nar_fsa AS (
                SELECT
                    UPPER(LEFT(mail_postal_code, 3)) as fsa_code,
                    prov_code
                FROM nar_addresses
                WHERE mail_postal_code IS NOT NULL
                  AND mail_postal_code != ''
                  AND LENGTH(mail_postal_code) >= 3
            )
            SELECT
                n.prov_code,
                COUNT(*) as total_count,
                COUNT(CASE WHEN f.fsa_code IS NULL THEN 1 END) as mismatch_count,
                COUNT(CASE WHEN f.fsa_code IS NOT NULL THEN 1 END) as match_count
            FROM nar_fsa n
            LEFT JOIN fsa_boundaries f
                ON n.fsa_code = f.fsa_code
                AND f.province_name = 'Ontario'
            GROUP BY n.prov_code
            ORDER BY total_count DESC
        """)

        result = conn.execute(prov_query)
        prov_analysis = result.fetchall()

        print(f"\n{'Prov':<5} {'Total':<10} {'Matches':<10} {'Mismatches':<12} {'Mismatch %':<12}")
        print("-" * 70)
        for row in prov_analysis:
            prov, total, mismatches_val, matches = row
            mismatch_pct = 100 * mismatches_val / total if total > 0 else 0
            print(f"{prov or 'N/A':<5} {total:>9,} {matches:>9,} {mismatches_val:>11,} {mismatch_pct:>11.2f}%")


if __name__ == '__main__':
    try:
        check_fsa_mismatches()
        print("\n✓ Analysis completed successfully!")
    except Exception as e:
        print(f"\n✗ Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
