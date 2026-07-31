"""
Postal Municipality Setup Transformer

Optional setup transformer that enriches postal codes with official municipality
assignments and extracts community→municipality aliases.

This transformer is designed to be additive:
- If commercial_postal_codes table doesn't exist or is empty, it skips gracefully
- If postal codes are already enriched, it skips to avoid redundant work
- Safe to run multiple times (idempotent)

The enrichment:
1. Spatial join: Assigns official_municipality to each postal code based on which
   lower_municipal_boundary contains the postal code's location
2. Alias extraction: Creates municipality_aliases entries for community names that
   differ from their official municipality (e.g., "Almonte" → "Mississippi Mills")
"""
from typing import Tuple

from .base import SetupTransformer, SetupResult


class PostalMunicipalitySetup(SetupTransformer):
    """
    Setup transformer that enriches postal codes with municipality assignments
    and extracts community aliases.

    Skips gracefully if:
    - commercial_postal_codes table doesn't exist or is empty
    - All postal codes already have official_municipality assigned
    """

    def __init__(self, min_alias_count: int = 10):
        """
        Initialize the transformer.

        Args:
            min_alias_count: Minimum postal codes for a city→municipality pair
                           to be considered a significant alias (default: 10)
        """
        super().__init__()
        self.min_alias_count = min_alias_count

    @property
    def name(self) -> str:
        return "PostalMunicipalitySetup"

    async def needs_setup(self, pool) -> bool:
        """
        Check if postal code municipality enrichment is needed.

        Returns False (skip setup) if:
        - commercial_postal_codes table doesn't exist
        - Table is empty
        - All postal codes already have official_municipality assigned
        """
        async with pool.acquire() as conn:
            # Check if table exists
            table_exists = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'commercial_postal_codes'
                )
            """)
            if not table_exists:
                return False  # No postal data, skip

            # Check if table has data
            has_data = await conn.fetchval("""
                SELECT EXISTS (SELECT 1 FROM commercial_postal_codes LIMIT 1)
            """)
            if not has_data:
                return False  # Empty table, skip

            # Check if official_municipality column exists
            col_exists = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'commercial_postal_codes'
                    AND column_name = 'official_municipality'
                )
            """)
            if not col_exists:
                return False  # Column not added yet, skip (migrations need to run first)

            # Check if there are unenriched postal codes
            unenriched = await conn.fetchval("""
                SELECT COUNT(*) FROM commercial_postal_codes
                WHERE official_municipality IS NULL
                  AND location IS NOT NULL
            """)
            return unenriched > 0

    async def check_dependencies(self, pool) -> Tuple[bool, str]:
        """
        Verify that required dependencies exist.

        Requires:
        - lower_municipal_boundaries table with data
        - municipality_aliases table (for alias insertion)
        """
        async with pool.acquire() as conn:
            # Check lower_municipal_boundaries has data
            has_boundaries = await conn.fetchval("""
                SELECT EXISTS (SELECT 1 FROM lower_municipal_boundaries LIMIT 1)
            """)
            if not has_boundaries:
                return False, "lower_municipal_boundaries table is empty or doesn't exist"

            # Check municipality_aliases table exists
            aliases_exist = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'municipality_aliases'
                )
            """)
            if not aliases_exist:
                return False, "municipality_aliases table doesn't exist"

            return True, ""

    async def _enrich_postal_municipalities(self, conn) -> int:
        """
        Spatial join to assign official municipality to postal codes.

        Returns count of updated records.
        """
        # Spatial join using ST_Contains
        result = await conn.execute("""
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
            WHERE pc.id = sm.id
        """)

        # Parse "UPDATE N" to get count
        return int(result.split()[-1]) if result else 0

    def _normalize_canonical_name(self, name: str) -> str:
        """Normalize canonical name to title case, removing common prefixes."""
        if not name:
            return name

        prefixes = ['CITY OF ', 'TOWN OF ', 'MUNICIPALITY OF ',
                    'TOWNSHIP OF ', 'COUNTY OF ']
        upper_name = name.upper()
        for prefix in prefixes:
            if upper_name.startswith(prefix):
                name = name[len(prefix):]
                break

        return name.strip().title()

    async def _extract_community_aliases(self, conn) -> int:
        """
        Extract community→municipality aliases from enriched postal codes.

        Returns count of aliases inserted/updated.
        """
        # Find city→municipality pairs that differ
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
        """, self.min_alias_count)

        # Insert aliases
        inserted = 0
        for alias_data in aliases:
            canonical_normalized = self._normalize_canonical_name(alias_data['canonical_name'])
            notes = f"Extracted from commercial postal codes ({alias_data['postal_code_count']:,} postal codes)"

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
            inserted += 1

        return inserted

    async def run_setup(self, pool) -> SetupResult:
        """
        Run the postal municipality enrichment.

        1. Spatial join to assign official municipalities
        2. Extract community→municipality aliases
        """
        import time
        start = time.time()

        async with pool.acquire() as conn:
            # Step 1: Spatial municipality assignment
            enriched_count = await self._enrich_postal_municipalities(conn)

            # Step 2: Extract community aliases
            alias_count = await self._extract_community_aliases(conn)

        duration = time.time() - start

        return SetupResult(
            status='success',
            rows_affected=enriched_count + alias_count,
            duration_seconds=duration,
            details={
                'postal_codes_enriched': enriched_count,
                'aliases_extracted': alias_count,
                'min_alias_count_threshold': self.min_alias_count
            }
        )
