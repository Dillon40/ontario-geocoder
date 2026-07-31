#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "asyncpg>=0.29.0",
# ]
# ///
"""
Enrich Commercial Postal Codes with Official Municipality Assignment

This script performs a spatial join between commercial_postal_codes and
lower_municipal_boundaries to assign the official municipality name to each
postal code based on its geographic location.

The results enable:
- Accurate community→municipality alias extraction
- Validation of postal code city names against official municipalities
- Enhanced geocoding with official municipality context

Usage:
    DB_HOST=localhost uv run python data_sourcing/enrichment/enrich_postal_municipalities.py
"""

import argparse
import asyncio
import asyncpg
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path for db_utils import
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from data_sourcing.db_utils import get_db_config


class PostalMunicipalityEnricher:
    """Enriches postal codes with official municipality via spatial join"""

    def __init__(self):
        self.pool = None
        self.total_postal_codes = 0
        self.total_municipalities = 0
        self.enriched_count = 0
        self.unmatched_count = 0

    async def connect(self):
        """Connect to database"""
        db_config = get_db_config()

        print(f"Connecting to database: {db_config['host']}:{db_config['port']}/{db_config['database']}")

        self.pool = await asyncpg.create_pool(
            host=db_config['host'],
            port=db_config['port'],
            database=db_config['database'],
            user=db_config['user'],
            password=db_config['password'],
            min_size=1,
            max_size=10,
            command_timeout=600  # 10 minutes for spatial queries
        )

        print("Connected to database\n")

    async def close(self):
        """Close database connection"""
        if self.pool:
            await self.pool.close()

    async def check_prerequisites(self) -> bool:
        """Verify required tables exist and have data"""
        async with self.pool.acquire() as conn:
            # Check commercial_postal_codes exists and has data
            postal_exists = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'commercial_postal_codes'
                )
            """)
            if not postal_exists:
                print("ERROR: commercial_postal_codes table does not exist")
                return False

            self.total_postal_codes = await conn.fetchval(
                "SELECT COUNT(*) FROM commercial_postal_codes"
            )
            if self.total_postal_codes == 0:
                print("WARNING: commercial_postal_codes table is empty - nothing to enrich")
                return False

            # Check lower_municipal_boundaries exists and has data
            muni_exists = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'lower_municipal_boundaries'
                )
            """)
            if not muni_exists:
                print("ERROR: lower_municipal_boundaries table does not exist")
                return False

            self.total_municipalities = await conn.fetchval(
                "SELECT COUNT(DISTINCT municipal_id) FROM lower_municipal_boundaries"
            )
            if self.total_municipalities == 0:
                print("ERROR: lower_municipal_boundaries table is empty")
                return False

            # Check if columns exist
            cols_exist = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'commercial_postal_codes'
                    AND column_name = 'official_municipality'
                )
            """)
            if not cols_exist:
                print("ERROR: official_municipality column does not exist - run Migration 24 first")
                return False

        print(f"Commercial postal codes: {self.total_postal_codes:,}")
        print(f"Lower municipal boundaries: {self.total_municipalities}")
        return True

    async def get_current_status(self):
        """Check how many postal codes already have municipality assigned"""
        async with self.pool.acquire() as conn:
            enriched = await conn.fetchval("""
                SELECT COUNT(*) FROM commercial_postal_codes
                WHERE official_municipality IS NOT NULL
            """)
            unenriched = self.total_postal_codes - enriched
            print(f"\nCurrent status:")
            print(f"  - Already enriched: {enriched:,}")
            print(f"  - Need enrichment: {unenriched:,}")
            return unenriched

    async def enrich_postal_municipalities(self):
        """
        Perform spatial join to assign official municipality to each postal code.

        Uses ST_Contains to find which municipal boundary contains each postal
        code's location. Handles multi-geometry municipalities (islands) by
        preferring the largest geometry.
        """
        print("\n" + "="*80)
        print("SPATIAL MUNICIPALITY ASSIGNMENT")
        print("="*80)
        print()

        async with self.pool.acquire() as conn:
            print("Running spatial join (this may take a few minutes)...")

            # Spatial join query
            # Uses ST_Contains to find which municipality contains each postal code point
            # For multi-geometry municipalities, uses DISTINCT ON with ORDER BY area DESC
            # to prefer the main geometry over islands
            query = """
                WITH spatial_matches AS (
                    SELECT DISTINCT ON (pc.id)
                        pc.id,
                        lm.short_name as official_municipality,
                        lm.municipal_id as official_municipal_id
                    FROM commercial_postal_codes pc
                    JOIN lower_municipal_boundaries lm
                        ON ST_Contains(lm.geometry, pc.location::geometry)
                    WHERE pc.official_municipality IS NULL
                      AND pc.location IS NOT NULL
                    ORDER BY pc.id, ST_Area(lm.geometry) DESC
                )
                UPDATE commercial_postal_codes pc
                SET official_municipality = sm.official_municipality,
                    official_municipal_id = sm.official_municipal_id,
                    municipality_assigned_at = NOW()
                FROM spatial_matches sm
                WHERE pc.id = sm.id;
            """

            result = await conn.execute(query)
            # Extract number from "UPDATE N"
            self.enriched_count = int(result.split()[-1]) if result else 0

            print(f"Updated {self.enriched_count:,} postal codes with municipality assignment\n")

            return self.enriched_count

    async def count_unmatched(self):
        """Count postal codes that couldn't be matched to a municipality"""
        async with self.pool.acquire() as conn:
            self.unmatched_count = await conn.fetchval("""
                SELECT COUNT(*) FROM commercial_postal_codes
                WHERE official_municipality IS NULL
                  AND location IS NOT NULL
            """)

            no_location = await conn.fetchval("""
                SELECT COUNT(*) FROM commercial_postal_codes
                WHERE location IS NULL
            """)

            print(f"Unmatched postal codes: {self.unmatched_count:,}")
            if no_location > 0:
                print(f"Postal codes without location: {no_location:,}")

            return self.unmatched_count

    async def print_summary_statistics(self):
        """Print summary statistics about the enrichment"""
        print("\n" + "="*80)
        print("SUMMARY STATISTICS")
        print("="*80)
        print()

        async with self.pool.acquire() as conn:
            # Total enriched
            enriched = await conn.fetchval("""
                SELECT COUNT(*) FROM commercial_postal_codes
                WHERE official_municipality IS NOT NULL
            """)
            pct = (enriched / self.total_postal_codes * 100) if self.total_postal_codes > 0 else 0
            print(f"Postal codes with municipality assigned: {enriched:,} ({pct:.1f}%)")

            # Unique municipalities
            unique_munis = await conn.fetchval("""
                SELECT COUNT(DISTINCT official_municipality)
                FROM commercial_postal_codes
                WHERE official_municipality IS NOT NULL
            """)
            print(f"Unique municipalities assigned: {unique_munis}")

            # Top 10 municipalities by postal code count
            print(f"\nTop 10 municipalities by postal code count:")
            top_munis = await conn.fetch("""
                SELECT official_municipality, COUNT(*) as postal_count
                FROM commercial_postal_codes
                WHERE official_municipality IS NOT NULL
                GROUP BY official_municipality
                ORDER BY postal_count DESC
                LIMIT 10
            """)
            for i, row in enumerate(top_munis, 1):
                print(f"  {i:2d}. {row['official_municipality']:<30} {row['postal_count']:>6,} postal codes")

            # City vs official municipality comparison
            print(f"\nCity name alignment with official municipality:")
            alignment = await conn.fetch("""
                SELECT
                    CASE
                        WHEN city_normalized = normalize_municipality_name(official_municipality)
                        THEN 'Matching'
                        ELSE 'Different'
                    END as alignment_status,
                    COUNT(*) as count
                FROM commercial_postal_codes
                WHERE official_municipality IS NOT NULL
                  AND city IS NOT NULL
                GROUP BY alignment_status
                ORDER BY count DESC
            """)
            for row in alignment:
                pct = row['count'] / enriched * 100 if enriched > 0 else 0
                print(f"  - {row['alignment_status']}: {row['count']:,} ({pct:.1f}%)")

    async def show_sample_community_mappings(self):
        """Show sample of community → municipality mappings discovered"""
        print("\n" + "="*80)
        print("SAMPLE COMMUNITY → MUNICIPALITY MAPPINGS")
        print("="*80)
        print()
        print("These city names differ from their official municipality (potential aliases):\n")

        async with self.pool.acquire() as conn:
            samples = await conn.fetch("""
                SELECT
                    city as community_name,
                    official_municipality,
                    COUNT(*) as postal_count
                FROM commercial_postal_codes
                WHERE official_municipality IS NOT NULL
                  AND city IS NOT NULL
                  AND city_normalized != normalize_municipality_name(official_municipality)
                GROUP BY city, official_municipality
                ORDER BY postal_count DESC
                LIMIT 25
            """)

            for row in samples:
                print(f"  {row['community_name']:<25} → {row['official_municipality']:<25} ({row['postal_count']:,} codes)")


async def main():
    """Main execution workflow"""
    print("\n" + "="*80)
    print("POSTAL CODE MUNICIPALITY ENRICHMENT")
    print("="*80)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*80)
    print()

    enricher = PostalMunicipalityEnricher()

    try:
        await enricher.connect()

        # Check prerequisites
        if not await enricher.check_prerequisites():
            print("\nAborting due to missing prerequisites")
            return

        # Check current status
        unenriched = await enricher.get_current_status()

        if unenriched == 0:
            print("\nAll postal codes already have municipality assigned - nothing to do")
        else:
            # Perform enrichment
            await enricher.enrich_postal_municipalities()

        # Count any unmatched
        await enricher.count_unmatched()

        # Print statistics
        await enricher.print_summary_statistics()

        # Show sample mappings
        await enricher.show_sample_community_mappings()

        print("\n" + "="*80)
        print("ENRICHMENT COMPLETE")
        print("="*80)
        print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print()
        print("Next steps:")
        print("1. Run extract_postal_city_aliases.py to create municipality aliases")
        print("2. Review the community → municipality mappings above")
        print("="*80)
        print()

    except Exception as e:
        print(f"\nERROR during enrichment: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await enricher.close()


def parse_args():
    parser = argparse.ArgumentParser(
        description='Spatially join commercial_postal_codes to '
                     'lower_municipal_boundaries to assign an official municipality '
                     'to each postal code. Connects to the database and writes results.'
    )
    return parser.parse_args()


if __name__ == '__main__':
    parse_args()
    asyncio.run(main())
