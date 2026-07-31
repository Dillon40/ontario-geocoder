#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "asyncpg>=0.29.0",
# ]
# ///
"""
Extract Municipality Aliases from NAR Data

This script analyzes NAR addresses to find municipality name variations:
1. Bilingual names (e.g., "Greater Sudbury / Grand Sudbury")
2. Pre-amalgamation names (e.g., Toronto: North York, Scarborough)
3. County variations (e.g., "Prince Edward County" vs "Prince Edward")
4. Other variations and informal names

The results populate the municipality_aliases table for name normalization
during geocoding queries.
"""

import argparse
import asyncio
import asyncpg
import re
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path for db_utils import
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from data_sourcing.db_utils import get_db_config


class MunicipalityAliasExtractor:
    """Extracts and categorizes municipality name aliases from NAR data"""

    def __init__(self):
        self.pool = None
        self.aliases_found = []
        self.canonical_municipalities = {}

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
            command_timeout=600
        )

        print("✓ Connected to database\n")

    async def close(self):
        """Close database connection"""
        if self.pool:
            await self.pool.close()

    async def load_canonical_municipalities(self):
        """Load canonical municipality names from lower_municipal_boundaries"""
        async with self.pool.acquire() as conn:
            munis = await conn.fetch("""
                SELECT DISTINCT
                    municipal_id,
                    short_name as name_short,
                    name as name_full,
                    normalize_municipality_name(short_name) as normalized
                FROM lower_municipal_boundaries
                WHERE municipal_id IS NOT NULL
                ORDER BY short_name;
            """)

            for muni in munis:
                self.canonical_municipalities[muni['normalized']] = {
                    'municipal_id': muni['municipal_id'],
                    'name_short': muni['name_short'],
                    'name_full': muni['name_full']
                }

        print(f"Loaded {len(self.canonical_municipalities)} canonical municipalities")

    async def find_bilingual_names(self):
        """
        Find bilingual municipality names (pattern: "Name / Nom")
        Example: "Greater Sudbury / Grand Sudbury"
        """
        print("\n" + "="*80)
        print("EXTRACTING BILINGUAL NAME ALIASES")
        print("="*80)
        print()

        async with self.pool.acquire() as conn:
            bilingual = await conn.fetch("""
                SELECT
                    csd_eng_name as original_name,
                    normalize_municipality_name(csd_eng_name) as normalized_name,
                    COUNT(*) as address_count
                FROM nar_addresses
                WHERE prov_code = 35
                  AND csd_eng_name LIKE '% / %'
                GROUP BY csd_eng_name
                ORDER BY address_count DESC;
            """)

            print(f"Found {len(bilingual)} bilingual municipality names:")

            for item in bilingual:
                # Extract the English part (before " / ")
                english_part = item['original_name'].split(' / ')[0].strip()
                normalized_english = english_part.upper()

                # Find canonical match
                canonical = self.canonical_municipalities.get(normalized_english)

                if canonical:
                    self.aliases_found.append({
                        'alias': item['original_name'],
                        'normalized_alias': item['normalized_name'],
                        'canonical_name': canonical['name_short'],
                        'municipal_id': canonical['municipal_id'],
                        'alias_type': 'bilingual',
                        'notes': f'Bilingual variation with {item["address_count"]:,} NAR addresses'
                    })

                    print(f"  '{item['original_name']}' → '{canonical['name_short']}' ({item['address_count']:,} addresses)")
                else:
                    print(f"  ⚠ '{item['original_name']}' (no canonical match found)")

            return len(bilingual)

    async def find_toronto_pre_amalgamation(self):
        """
        Find Toronto pre-amalgamation municipality names
        Toronto amalgamated in 1998: North York, Scarborough, Etobicoke, York, East York
        """
        print("\n" + "="*80)
        print("EXTRACTING TORONTO PRE-AMALGAMATION ALIASES")
        print("="*80)
        print()

        # Toronto's municipal_id from lower_municipal_boundaries
        toronto_municipal_id = '20002'  # This may vary - will look up dynamically

        async with self.pool.acquire() as conn:
            # Get Toronto's municipal_id
            toronto_info = await conn.fetchrow("""
                SELECT municipal_id, short_name
                FROM lower_municipal_boundaries
                WHERE UPPER(short_name) = 'TORONTO'
                LIMIT 1;
            """)

            if not toronto_info:
                print("⚠ WARNING: Toronto not found in municipal boundaries")
                return 0

            toronto_municipal_id = toronto_info['municipal_id']

            # Known pre-amalgamation municipalities
            pre_amalgamation_names = [
                'North York',
                'Scarborough',
                'Etobicoke',
                'York',
                'East York'
            ]

            print(f"Searching for pre-amalgamation municipalities in NAR data...")

            found_count = 0
            for name in pre_amalgamation_names:
                count = await conn.fetchval("""
                    SELECT COUNT(*)
                    FROM nar_addresses
                    WHERE prov_code = 35
                      AND UPPER(csd_eng_name) = UPPER($1);
                """, name)

                if count > 0:
                    self.aliases_found.append({
                        'alias': name,
                        'normalized_alias': name.upper(),
                        'canonical_name': 'TORONTO',
                        'municipal_id': toronto_municipal_id,
                        'alias_type': 'pre_amalgamation',
                        'notes': f'Toronto pre-amalgamation municipality with {count:,} NAR addresses'
                    })

                    print(f"  ✓ {name} → Toronto ({count:,} addresses)")
                    found_count += 1
                else:
                    print(f"  - {name} (0 addresses in NAR)")

            return found_count

    async def find_county_variations(self):
        """
        Find county name variations (e.g., "Prince Edward County" vs "Prince Edward")
        """
        print("\n" + "="*80)
        print("EXTRACTING COUNTY NAME VARIATIONS")
        print("="*80)
        print()

        async with self.pool.acquire() as conn:
            # Find NAR names ending with "County" that don't match canonical names
            county_names = await conn.fetch("""
                SELECT
                    csd_eng_name as original_name,
                    normalize_municipality_name(csd_eng_name) as normalized_name,
                    COUNT(*) as address_count
                FROM nar_addresses
                WHERE prov_code = 35
                  AND csd_eng_name LIKE '%County%'
                  AND csd_eng_name NOT LIKE '%/%'  -- Exclude bilinguals
                GROUP BY csd_eng_name
                ORDER BY address_count DESC;
            """)

            print(f"Found {len(county_names)} county name variations:")

            found_count = 0
            for item in county_names:
                # Try removing "County" to find canonical match
                name_without_county = re.sub(r'\s*,?\s*County\s*$', '', item['original_name'], flags=re.IGNORECASE)
                normalized_without_county = name_without_county.upper().strip()

                # Check if this normalized version exists in canonical municipalities
                canonical = self.canonical_municipalities.get(normalized_without_county)

                if canonical and canonical['name_short'].upper() != item['normalized_name']:
                    self.aliases_found.append({
                        'alias': item['original_name'],
                        'normalized_alias': item['normalized_name'],
                        'canonical_name': canonical['name_short'],
                        'municipal_id': canonical['municipal_id'],
                        'alias_type': 'county_variation',
                        'notes': f'County suffix variation with {item["address_count"]:,} NAR addresses'
                    })

                    print(f"  '{item['original_name']}' → '{canonical['name_short']}' ({item['address_count']:,} addresses)")
                    found_count += 1

            if found_count == 0:
                print("  (No county variations requiring aliases found)")

            return found_count

    async def find_unmatched_variations(self):
        """
        Find NAR municipality names that don't match canonical names
        These are "other" variations that don't fit specific patterns
        """
        print("\n" + "="*80)
        print("EXTRACTING OTHER MUNICIPALITY VARIATIONS")
        print("="*80)
        print()

        async with self.pool.acquire() as conn:
            # Find NAR names that don't normalize to canonical names
            unmatched = await conn.fetch("""
                WITH nar_munis AS (
                    SELECT
                        csd_eng_name as original_name,
                        normalize_municipality_name(csd_eng_name) as normalized_name,
                        COUNT(*) as address_count
                    FROM nar_addresses
                    WHERE prov_code = 35
                      AND csd_eng_name IS NOT NULL
                      AND csd_eng_name != ''
                    GROUP BY csd_eng_name
                ),
                canonical_munis AS (
                    SELECT DISTINCT normalize_municipality_name(short_name) as normalized
                    FROM lower_municipal_boundaries
                )
                SELECT
                    nm.original_name,
                    nm.normalized_name,
                    nm.address_count
                FROM nar_munis nm
                WHERE NOT EXISTS (
                    SELECT 1 FROM canonical_munis cm
                    WHERE cm.normalized = nm.normalized_name
                )
                  -- Exclude already-found patterns
                  AND nm.original_name NOT LIKE '% / %'  -- Bilinguals handled
                  AND nm.original_name NOT IN ('North York', 'Scarborough', 'Etobicoke', 'York', 'East York')
                ORDER BY nm.address_count DESC
                LIMIT 20;  -- Top 20 unmatched to review
            """)

            print(f"Found {len(unmatched)} other municipality variations (showing top {min(20, len(unmatched))}):")

            if len(unmatched) == 0:
                print("  (No additional variations found)")
                return 0

            for item in unmatched[:20]:
                # Try fuzzy matching - look for similar canonical names
                # For now, just report them
                print(f"  ⚠ '{item['original_name']}' ({item['address_count']:,} addresses) - no canonical match")

                # Could add fuzzy matching logic here in the future
                # For now, we'll just report these for manual review

            print(f"\n  ℹ These variations may require manual review and alias creation")
            print(f"    or may represent unorganized territories not in municipal boundaries")

            return 0  # Don't auto-create aliases for these

    async def populate_aliases(self):
        """Insert aliases into municipality_aliases table"""
        print("\n" + "="*80)
        print("POPULATING MUNICIPALITY_ALIASES TABLE")
        print("="*80)
        print()

        if not self.aliases_found:
            print("⚠ No aliases found to populate")
            return 0

        async with self.pool.acquire() as conn:
            # Clear existing aliases
            deleted = await conn.fetchval("SELECT COUNT(*) FROM municipality_aliases")
            await conn.execute("TRUNCATE TABLE municipality_aliases RESTART IDENTITY")

            if deleted > 0:
                print(f"✓ Cleared {deleted} existing aliases\n")

            # Insert new aliases
            print(f"Inserting {len(self.aliases_found)} aliases...")

            for alias in self.aliases_found:
                await conn.execute("""
                    INSERT INTO municipality_aliases (
                        alias, normalized_alias, canonical_name, municipal_id, alias_type, notes
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (normalized_alias, canonical_name) DO UPDATE
                    SET alias = EXCLUDED.alias,
                        municipal_id = EXCLUDED.municipal_id,
                        alias_type = EXCLUDED.alias_type,
                        notes = EXCLUDED.notes;
                """, alias['alias'], alias['normalized_alias'], alias['canonical_name'],
                    alias['municipal_id'], alias['alias_type'], alias['notes'])

            print(f"✓ Inserted {len(self.aliases_found)} aliases\n")

            return len(self.aliases_found)

    async def print_summary(self):
        """Print summary statistics"""
        print("="*80)
        print("SUMMARY STATISTICS")
        print("="*80)
        print()

        async with self.pool.acquire() as conn:
            # Total aliases
            total = await conn.fetchval("SELECT COUNT(*) FROM municipality_aliases")
            print(f"Total aliases: {total}")

            # By type
            by_type = await conn.fetch("""
                SELECT
                    alias_type,
                    COUNT(*) as count
                FROM municipality_aliases
                GROUP BY alias_type
                ORDER BY count DESC;
            """)

            print(f"\nAliases by type:")
            for row in by_type:
                print(f"  {row['alias_type']:20s}: {row['count']:3d}")

            # Top canonical municipalities by alias count
            by_canonical = await conn.fetch("""
                SELECT
                    canonical_name,
                    COUNT(*) as alias_count
                FROM municipality_aliases
                GROUP BY canonical_name
                ORDER BY alias_count DESC
                LIMIT 10;
            """)

            print(f"\nTop municipalities by alias count:")
            for i, row in enumerate(by_canonical, 1):
                print(f"  {i:2d}. {row['canonical_name']:30s}: {row['alias_count']} aliases")

    async def test_normalization(self):
        """Test that aliases normalize correctly"""
        print("\n" + "="*80)
        print("TESTING ALIAS NORMALIZATION")
        print("="*80)
        print()

        async with self.pool.acquire() as conn:
            test_cases = [
                "North York",
                "Greater Sudbury / Grand Sudbury",
                "Scarborough",
                "Prince Edward County"
            ]

            print("Testing municipality name resolution:")
            for test_name in test_cases:
                result = await conn.fetchrow("""
                    SELECT
                        alias,
                        canonical_name,
                        alias_type
                    FROM municipality_aliases
                    WHERE normalized_alias = normalize_municipality_name($1)
                    LIMIT 1;
                """, test_name)

                if result:
                    print(f"  ✓ '{test_name}' → '{result['canonical_name']}' ({result['alias_type']})")
                else:
                    print(f"  - '{test_name}' (no alias found)")


async def main():
    """Main execution workflow"""
    print("\n" + "="*80)
    print("MUNICIPALITY ALIAS EXTRACTION")
    print("="*80)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*80)
    print()

    extractor = MunicipalityAliasExtractor()

    try:
        await extractor.connect()
        await extractor.load_canonical_municipalities()

        # Extract different types of aliases
        await extractor.find_bilingual_names()
        await extractor.find_toronto_pre_amalgamation()
        await extractor.find_county_variations()
        await extractor.find_unmatched_variations()

        # Populate database
        await extractor.populate_aliases()

        # Statistics and testing
        await extractor.print_summary()
        await extractor.test_normalization()

        print("\n" + "="*80)
        print("EXTRACTION COMPLETE")
        print("="*80)
        print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print()
        print("Next steps:")
        print("1. Review unmatched variations (if any)")
        print("2. Test geocoding queries with alias names")
        print("3. Update NAR geocoder to use municipality_aliases table")
        print("="*80)
        print()

    except Exception as e:
        print(f"\n[FAIL] Error during extraction: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await extractor.close()


def parse_args():
    parser = argparse.ArgumentParser(
        description='Extract municipality name aliases (bilingual, pre-amalgamation, '
                     'county variations) from NAR data and populate municipality_aliases. '
                     'Connects to the database and truncates/repopulates the table.'
    )
    return parser.parse_args()


if __name__ == '__main__':
    parse_args()
    asyncio.run(main())
