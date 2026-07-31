#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "asyncpg>=0.29.0",
# ]
# ///
"""
Extract Community → Municipality Aliases from Commercial Postal Codes

This script extracts alias mappings from postal codes where the city name
(community name like "Almonte", "Carp") differs from the official municipality
name (like "Mississippi Mills", "Ottawa") determined by spatial containment.

The results are inserted into municipality_aliases to improve geocoding accuracy
when users search using community names rather than official municipality names.

Prerequisites:
- Run enrich_postal_municipalities.py first to assign official municipalities
- municipality_aliases table must exist (created by Migration 6)

Usage:
    DB_HOST=localhost uv run python data_sourcing/enrichment/extract_postal_city_aliases.py
"""

import asyncio
import asyncpg
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path for db_utils import
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from data_sourcing.db_utils import get_db_config


class PostalCityAliasExtractor:
    """Extracts community → municipality aliases from postal code data"""

    def __init__(self):
        self.pool = None
        self.aliases_found = 0
        self.aliases_inserted = 0
        self.aliases_updated = 0
        self.min_postal_count = 10  # Minimum postal codes to create alias

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
            command_timeout=300
        )

        print("Connected to database\n")

    async def close(self):
        """Close database connection"""
        if self.pool:
            await self.pool.close()

    async def check_prerequisites(self) -> bool:
        """Verify required tables and data exist"""
        async with self.pool.acquire() as conn:
            # Check municipality_aliases table exists
            aliases_exists = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'municipality_aliases'
                )
            """)
            if not aliases_exists:
                print("ERROR: municipality_aliases table does not exist")
                return False

            # Check commercial_postal_codes has enriched data
            enriched_count = await conn.fetchval("""
                SELECT COUNT(*) FROM commercial_postal_codes
                WHERE official_municipality IS NOT NULL
            """)
            if enriched_count == 0:
                print("ERROR: No postal codes have official_municipality assigned")
                print("       Run enrich_postal_municipalities.py first")
                return False

            print(f"Postal codes with municipality assignment: {enriched_count:,}")

            # Check existing aliases
            existing = await conn.fetchval("SELECT COUNT(*) FROM municipality_aliases")
            print(f"Existing municipality aliases: {existing:,}")

        return True

    async def find_community_aliases(self):
        """
        Find city→municipality pairs where they differ.

        Only includes pairs with sufficient postal code count to filter noise.
        """
        print("\n" + "="*80)
        print("EXTRACTING COMMUNITY → MUNICIPALITY ALIASES")
        print("="*80)
        print()

        async with self.pool.acquire() as conn:
            print(f"Finding city names that differ from official municipality...")
            print(f"(Minimum {self.min_postal_count} postal codes required)\n")

            # Find all city→municipality pairs where they differ
            # Normalizes both sides to avoid false positives from case differences
            aliases = await conn.fetch("""
                SELECT
                    city as alias,
                    city_normalized as normalized_alias,
                    official_municipality as canonical_name,
                    official_municipal_id as municipal_id,
                    COUNT(*) as postal_code_count
                FROM commercial_postal_codes
                WHERE city IS NOT NULL
                  AND official_municipality IS NOT NULL
                  AND city_normalized != normalize_municipality_name(official_municipality)
                GROUP BY city, city_normalized, official_municipality, official_municipal_id
                HAVING COUNT(*) >= $1
                ORDER BY postal_code_count DESC
            """, self.min_postal_count)

            self.aliases_found = len(aliases)
            print(f"Found {self.aliases_found} community → municipality mappings\n")

            return aliases

    async def normalize_canonical_name(self, conn, name: str) -> str:
        """
        Normalize canonical municipality name to title case.

        Converts "CITY OF TORONTO" -> "Toronto"
        """
        if not name:
            return name

        # Remove common prefixes
        prefixes = ['CITY OF ', 'TOWN OF ', 'MUNICIPALITY OF ',
                    'TOWNSHIP OF ', 'COUNTY OF ']
        upper_name = name.upper()
        for prefix in prefixes:
            if upper_name.startswith(prefix):
                name = name[len(prefix):]
                break

        # Convert to title case
        return name.strip().title()

    async def insert_aliases(self, aliases):
        """
        Insert aliases into municipality_aliases table.

        Uses ON CONFLICT DO UPDATE to handle existing aliases.
        Tags all aliases with type='community_name' for identification.
        """
        print("Inserting aliases into municipality_aliases...")

        async with self.pool.acquire() as conn:
            for alias_data in aliases:
                # Normalize canonical name
                canonical_normalized = await self.normalize_canonical_name(
                    conn, alias_data['canonical_name']
                )

                # Create notes with postal code count for reference
                notes = f"Extracted from commercial postal codes ({alias_data['postal_code_count']:,} postal codes)"

                # Insert or update the alias
                await conn.execute("""
                    INSERT INTO municipality_aliases
                    (alias, normalized_alias, canonical_name, municipal_id, alias_type, notes)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (normalized_alias, canonical_name) DO UPDATE
                    SET alias = EXCLUDED.alias,
                        municipal_id = EXCLUDED.municipal_id,
                        alias_type = EXCLUDED.alias_type,
                        notes = EXCLUDED.notes
                """,
                    alias_data['alias'],
                    alias_data['normalized_alias'],
                    canonical_normalized,
                    alias_data['municipal_id'],
                    'community_name',
                    notes
                )

                # Note: can't easily distinguish insert/update without more complex logic
                # Just count total processed

            self.aliases_inserted = self.aliases_found  # Approximate

        print(f"Processed {self.aliases_found} aliases\n")

    async def print_summary(self):
        """Print summary statistics"""
        print("="*80)
        print("SUMMARY")
        print("="*80)
        print()

        async with self.pool.acquire() as conn:
            # Total aliases now in table
            total = await conn.fetchval("SELECT COUNT(*) FROM municipality_aliases")
            print(f"Total aliases in municipality_aliases: {total:,}")

            # Aliases by type
            print(f"\nAliases by type:")
            by_type = await conn.fetch("""
                SELECT alias_type, COUNT(*) as count
                FROM municipality_aliases
                GROUP BY alias_type
                ORDER BY count DESC
            """)
            for row in by_type:
                print(f"  - {row['alias_type'] or 'untyped'}: {row['count']:,}")

            # Sample of newly added community_name aliases
            print(f"\nSample community_name aliases (top 20 by postal count):")
            samples = await conn.fetch("""
                SELECT alias, canonical_name, notes
                FROM municipality_aliases
                WHERE alias_type = 'community_name'
                ORDER BY
                    CAST(REGEXP_REPLACE(notes, '[^0-9]', '', 'g') AS INTEGER) DESC NULLS LAST
                LIMIT 20
            """)
            for row in samples:
                # Extract count from notes
                count_str = ''.join(c for c in (row['notes'] or '') if c.isdigit())
                count = int(count_str) if count_str else 0
                print(f"  {row['alias']:<25} → {row['canonical_name']:<25} ({count:,} codes)")

    async def show_coverage_analysis(self):
        """Analyze how many postal code cities are now covered by aliases"""
        print("\n" + "="*80)
        print("COVERAGE ANALYSIS")
        print("="*80)
        print()

        async with self.pool.acquire() as conn:
            # Count how many distinct city names are now in aliases
            coverage = await conn.fetchrow("""
                WITH postal_cities AS (
                    SELECT DISTINCT city_normalized
                    FROM commercial_postal_codes
                    WHERE city IS NOT NULL
                ),
                alias_cities AS (
                    SELECT DISTINCT normalized_alias
                    FROM municipality_aliases
                )
                SELECT
                    (SELECT COUNT(*) FROM postal_cities) as total_postal_cities,
                    (SELECT COUNT(*) FROM postal_cities pc
                     JOIN alias_cities ac ON pc.city_normalized = ac.normalized_alias) as cities_with_alias,
                    (SELECT COUNT(*) FROM postal_cities pc
                     WHERE NOT EXISTS (
                         SELECT 1 FROM alias_cities ac
                         WHERE pc.city_normalized = ac.normalized_alias
                     )) as cities_without_alias
            """)

            total = coverage['total_postal_cities']
            with_alias = coverage['cities_with_alias']
            without_alias = coverage['cities_without_alias']
            pct = (with_alias / total * 100) if total > 0 else 0

            print(f"Total unique city names in postal codes: {total:,}")
            print(f"City names with alias mapping: {with_alias:,} ({pct:.1f}%)")
            print(f"City names without alias mapping: {without_alias:,}")

            # Note: cities without alias are likely either:
            # 1. Already the canonical municipality name, or
            # 2. Small communities with <10 postal codes (filtered out)
            print(f"\nNote: Cities without aliases are typically:")
            print(f"  - Already using canonical municipality name, or")
            print(f"  - Small communities with <{self.min_postal_count} postal codes (filtered out)")


async def main():
    """Main execution workflow"""
    print("\n" + "="*80)
    print("POSTAL CODE CITY ALIAS EXTRACTION")
    print("="*80)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*80)
    print()

    extractor = PostalCityAliasExtractor()

    try:
        await extractor.connect()

        # Check prerequisites
        if not await extractor.check_prerequisites():
            print("\nAborting due to missing prerequisites")
            return

        # Find community → municipality aliases
        aliases = await extractor.find_community_aliases()

        if not aliases:
            print("No new community aliases found")
        else:
            # Insert into municipality_aliases
            await extractor.insert_aliases(aliases)

        # Print summary
        await extractor.print_summary()

        # Show coverage analysis
        await extractor.show_coverage_analysis()

        print("\n" + "="*80)
        print("EXTRACTION COMPLETE")
        print("="*80)
        print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print()
        print("The municipality_aliases table has been enriched with community name mappings.")
        print("These mappings enable geocoding with community names like 'Almonte' → 'Mississippi Mills'.")
        print("="*80)
        print()

    except Exception as e:
        print(f"\nERROR during extraction: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await extractor.close()


if __name__ == '__main__':
    asyncio.run(main())
