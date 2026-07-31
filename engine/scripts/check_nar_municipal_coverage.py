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
Check for NAR database city values that don't match Ontario municipal boundaries.

ENHANCED with FSA-Municipality mapping system:
- Uses normalize_municipality_name() function for proper name matching
- Resolves aliases via municipality_aliases table (bilingual names, etc.)
- Distinguishes between true mismatches and resolved variations

Identifies cities in nar_addresses where the csd_eng_name doesn't match any of:
- short_name (short municipality name) - after normalization
- name (full municipality name) - after normalization
- UPR_TIER_E (upper tier name) - after normalization
- Municipality aliases (canonical_name)
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


def check_municipal_coverage():
    """
    Find NAR city names that don't match any Ontario municipal boundary names.
    """
    db_url = get_database_url()
    engine = create_engine(db_url)

    print("Connecting to database...")

    with engine.connect() as conn:
        # First, get count of total NAR addresses
        result = conn.execute(text("""
            SELECT COUNT(*)
            FROM nar_addresses
            WHERE csd_eng_name IS NOT NULL
              AND csd_eng_name != ''
        """))
        total_count = result.scalar()
        print(f"✓ Total NAR addresses with city names: {total_count:,}")

        # Get count of municipalities
        result = conn.execute(text("""
            SELECT COUNT(DISTINCT name)
            FROM lower_municipal_boundaries
        """))
        muni_count = result.scalar()
        print(f"✓ Unique municipalities in lower_municipal_boundaries: {muni_count}")

        # Get count of unique NAR city names
        result = conn.execute(text("""
            SELECT COUNT(DISTINCT csd_eng_name)
            FROM nar_addresses
            WHERE csd_eng_name IS NOT NULL
              AND csd_eng_name != ''
        """))
        unique_nar_cities = result.scalar()
        print(f"✓ Unique city names in NAR: {unique_nar_cities}")

        print("\n" + "="*80)
        print("CHECKING FOR CITY NAME MISMATCHES (Enhanced with normalization)")
        print("="*80)

        # Find NAR cities that don't match any municipal boundary name
        # ENHANCED: Uses normalize_municipality_name() and municipality_aliases
        query = text("""
            WITH nar_cities AS (
                SELECT
                    id,
                    addr_guid,
                    csd_eng_name,
                    mail_postal_code,
                    civic_no,
                    official_street_name,
                    prov_code,
                    normalize_municipality_name(csd_eng_name) as city_normalized
                FROM nar_addresses
                WHERE csd_eng_name IS NOT NULL
                  AND csd_eng_name != ''
            ),
            muni_names AS (
                -- Normalized municipal boundary names
                SELECT DISTINCT normalize_municipality_name(short_name) as name
                FROM lower_municipal_boundaries
                WHERE short_name IS NOT NULL

                UNION

                SELECT DISTINCT normalize_municipality_name(name) as name
                FROM lower_municipal_boundaries
                WHERE name IS NOT NULL

                UNION

                SELECT DISTINCT normalize_municipality_name("UPR_TIER_E") as name
                FROM lower_municipal_boundaries
                WHERE "UPR_TIER_E" IS NOT NULL
                  AND "UPR_TIER_E" != ''

                UNION

                -- ENHANCED: Include canonical names from aliases table
                SELECT DISTINCT canonical_name as name
                FROM municipality_aliases
                WHERE canonical_name IS NOT NULL
            )
            SELECT
                n.csd_eng_name as original_name,
                COUNT(*) as count,
                COUNT(DISTINCT n.mail_postal_code) as postal_code_count,
                STRING_AGG(
                    DISTINCT LEFT(n.mail_postal_code, 3),
                    ', '
                    ORDER BY LEFT(n.mail_postal_code, 3)
                ) as fsa_codes,
                n.prov_code
            FROM nar_cities n
            LEFT JOIN muni_names m
                ON n.city_normalized = m.name
            WHERE m.name IS NULL
            GROUP BY n.csd_eng_name, n.prov_code
            ORDER BY count DESC
        """)

        result = conn.execute(query)
        mismatches = result.fetchall()

        if mismatches:
            print(f"\n⚠ Found {len(mismatches)} city names that don't match municipal boundaries:\n")
            print(f"{'City Name':<40} {'Prov':<5} {'Count':<10} {'Postal':<7} FSA Codes")
            print("-" * 80)

            total_mismatched = 0
            for row in mismatches:
                city_name, count, postal_count, fsa_codes, prov_code = row
                total_mismatched += count
                city_display = city_name[:38] + ".." if len(city_name) > 40 else city_name
                fsa_display = (fsa_codes[:20] + "...") if fsa_codes and len(fsa_codes) > 20 else (fsa_codes or "N/A")
                print(f"{city_display:<40} {prov_code or 'N/A':<5} {count:>9,} {postal_count:>7} {fsa_display}")

            print("-" * 80)
            print(f"Total addresses with unmatched cities: {total_mismatched:,}")
            print(f"Percentage of total: {100 * total_mismatched / total_count:.2f}%")

            # Show sample records for investigation
            print("\n" + "="*80)
            print("SAMPLE RECORDS (first 15 unmatched cities)")
            print("="*80)

            sample_query = text("""
                WITH nar_cities AS (
                    SELECT
                        csd_eng_name,
                        mail_postal_code,
                        civic_no,
                        official_street_name,
                        official_street_type,
                        prov_code,
                        normalize_municipality_name(csd_eng_name) as city_normalized
                    FROM nar_addresses
                    WHERE csd_eng_name IS NOT NULL
                      AND csd_eng_name != ''
                ),
                muni_names AS (
                    SELECT DISTINCT normalize_municipality_name(short_name) as name
                    FROM lower_municipal_boundaries
                    WHERE short_name IS NOT NULL

                    UNION

                    SELECT DISTINCT normalize_municipality_name(name) as name
                    FROM lower_municipal_boundaries
                    WHERE name IS NOT NULL

                    UNION

                    SELECT DISTINCT normalize_municipality_name("UPR_TIER_E") as name
                    FROM lower_municipal_boundaries
                    WHERE "UPR_TIER_E" IS NOT NULL
                      AND "UPR_TIER_E" != ''

                    UNION

                    SELECT DISTINCT canonical_name as name
                    FROM municipality_aliases
                    WHERE canonical_name IS NOT NULL
                )
                SELECT
                    n.csd_eng_name,
                    n.mail_postal_code,
                    n.civic_no || ' ' ||
                    COALESCE(n.official_street_name, '') || ' ' ||
                    COALESCE(n.official_street_type, '') as address,
                    n.prov_code
                FROM nar_cities n
                LEFT JOIN muni_names m
                    ON n.city_normalized = m.name
                WHERE m.name IS NULL
                LIMIT 15
            """)

            result = conn.execute(sample_query)
            samples = result.fetchall()

            for row in samples:
                city, postal, address, prov = row
                print(f"\nCity: {city}")
                print(f"  Postal: {postal}")
                print(f"  Address: {address}")
                print(f"  Province Code: {prov}")

        else:
            print("\n✓ All NAR city names match municipal boundary names!")

        # Additional analysis: Show what municipal name formats exist
        print("\n" + "="*80)
        print("MUNICIPAL NAME COVERAGE ANALYSIS")
        print("="*80)

        coverage_query = text("""
            WITH nar_cities AS (
                SELECT
                    normalize_municipality_name(csd_eng_name) as city_normalized
                FROM nar_addresses
                WHERE csd_eng_name IS NOT NULL
                  AND csd_eng_name != ''
            ),
            short_names AS (
                SELECT DISTINCT normalize_municipality_name(short_name) as name
                FROM lower_municipal_boundaries
                WHERE short_name IS NOT NULL
            ),
            full_names AS (
                SELECT DISTINCT normalize_municipality_name(name) as name
                FROM lower_municipal_boundaries
                WHERE name IS NOT NULL
            ),
            upper_tier_names AS (
                SELECT DISTINCT normalize_municipality_name("UPR_TIER_E") as name
                FROM lower_municipal_boundaries
                WHERE "UPR_TIER_E" IS NOT NULL
                  AND "UPR_TIER_E" != ''
            ),
            alias_names AS (
                SELECT DISTINCT canonical_name as name
                FROM municipality_aliases
                WHERE canonical_name IS NOT NULL
            )
            SELECT
                'Short Names (NM_SHORT_E)' as name_type,
                COUNT(DISTINCT n.city_normalized) as matched_cities,
                COUNT(*) as total_addresses
            FROM nar_cities n
            INNER JOIN short_names s ON n.city_normalized = s.name

            UNION ALL

            SELECT
                'Full Names (NAME_E)' as name_type,
                COUNT(DISTINCT n.city_normalized) as matched_cities,
                COUNT(*) as total_addresses
            FROM nar_cities n
            INNER JOIN full_names f ON n.city_normalized = f.name

            UNION ALL

            SELECT
                'Upper Tier (UPR_TIER_E)' as name_type,
                COUNT(DISTINCT n.city_normalized) as matched_cities,
                COUNT(*) as total_addresses
            FROM nar_cities n
            INNER JOIN upper_tier_names u ON n.city_normalized = u.name

            UNION ALL

            SELECT
                'Aliases (ENHANCED)' as name_type,
                COUNT(DISTINCT n.city_normalized) as matched_cities,
                COUNT(*) as total_addresses
            FROM nar_cities n
            INNER JOIN alias_names a ON n.city_normalized = a.name

            UNION ALL

            SELECT
                'Any Match (Normalized)' as name_type,
                COUNT(DISTINCT n.city_normalized) as matched_cities,
                COUNT(*) as total_addresses
            FROM nar_cities n
            WHERE EXISTS (
                SELECT 1 FROM short_names s WHERE n.city_normalized = s.name
                UNION
                SELECT 1 FROM full_names f WHERE n.city_normalized = f.name
                UNION
                SELECT 1 FROM upper_tier_names u WHERE n.city_normalized = u.name
                UNION
                SELECT 1 FROM alias_names a WHERE n.city_normalized = a.name
            )
        """)

        result = conn.execute(coverage_query)
        coverage = result.fetchall()

        print(f"\n{'Name Type':<30} {'Matched Cities':<16} {'Total Addresses':<16}")
        print("-" * 80)
        for row in coverage:
            name_type, matched_cities, total_addresses = row
            print(f"{name_type:<30} {matched_cities:>15,} {total_addresses:>15,}")

        # Show top 20 matched vs unmatched comparison
        print("\n" + "="*80)
        print("TOP 20 CITIES BY ADDRESS COUNT (Matched vs Unmatched)")
        print("="*80)

        top_cities_query = text("""
            WITH nar_cities AS (
                SELECT
                    csd_eng_name,
                    normalize_municipality_name(csd_eng_name) as city_normalized
                FROM nar_addresses
                WHERE csd_eng_name IS NOT NULL
                  AND csd_eng_name != ''
            ),
            muni_names AS (
                SELECT DISTINCT normalize_municipality_name(short_name) as name
                FROM lower_municipal_boundaries
                WHERE short_name IS NOT NULL

                UNION

                SELECT DISTINCT normalize_municipality_name(name) as name
                FROM lower_municipal_boundaries
                WHERE name IS NOT NULL

                UNION

                SELECT DISTINCT normalize_municipality_name("UPR_TIER_E") as name
                FROM lower_municipal_boundaries
                WHERE "UPR_TIER_E" IS NOT NULL
                  AND "UPR_TIER_E" != ''

                UNION

                SELECT DISTINCT canonical_name as name
                FROM municipality_aliases
                WHERE canonical_name IS NOT NULL
            )
            SELECT
                n.csd_eng_name,
                COUNT(*) as count,
                CASE
                    WHEN m.name IS NOT NULL THEN 'Matched'
                    ELSE 'Unmatched'
                END as match_status
            FROM nar_cities n
            LEFT JOIN muni_names m ON n.city_normalized = m.name
            GROUP BY n.csd_eng_name, m.name
            ORDER BY count DESC
            LIMIT 20
        """)

        result = conn.execute(top_cities_query)
        top_cities = result.fetchall()

        print(f"\n{'City Name':<40} {'Count':<12} {'Status':<10}")
        print("-" * 80)
        for row in top_cities:
            city, count, status = row
            city_display = city[:38] + ".." if len(city) > 40 else city
            status_symbol = "✓" if status == "Matched" else "✗"
            print(f"{city_display:<40} {count:>11,} {status_symbol} {status}")


if __name__ == '__main__':
    try:
        check_municipal_coverage()
        print("\n✓ Analysis completed successfully!")
    except Exception as e:
        print(f"\n✗ Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
