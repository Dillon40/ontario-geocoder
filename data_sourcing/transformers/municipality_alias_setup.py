"""
Municipality alias setup transformer.

Extracts municipality name variations from the properties table and populates
the municipality_aliases table. This enables MunicipalityMapper to resolve
pre-amalgamation names, bilingual names, and other variations.

Unlike the original extract_municipality_aliases.py which extracted from NAR,
this setup transformer extracts from the properties table (imported municipal
datasets) since that's where the actual variations exist.

Also seeds known pre-amalgamation aliases for major Ontario municipalities
to ensure they're available even before data is imported.
"""
from typing import Tuple

from .base import SetupTransformer, SetupResult


# Known pre-amalgamation and alias mappings for Ontario municipalities
# These are seeded to ensure MunicipalityMapper works even before data import
KNOWN_MUNICIPALITY_ALIASES = [
    # Toronto pre-amalgamation (1998)
    {'alias': 'Etobicoke', 'canonical': 'Toronto', 'type': 'pre_amalgamation',
     'notes': 'Pre-1998 Toronto amalgamation'},
    {'alias': 'Scarborough', 'canonical': 'Toronto', 'type': 'pre_amalgamation',
     'notes': 'Pre-1998 Toronto amalgamation'},
    {'alias': 'North York', 'canonical': 'Toronto', 'type': 'pre_amalgamation',
     'notes': 'Pre-1998 Toronto amalgamation'},
    {'alias': 'York', 'canonical': 'Toronto', 'type': 'pre_amalgamation',
     'notes': 'Pre-1998 Toronto amalgamation'},
    {'alias': 'East York', 'canonical': 'Toronto', 'type': 'pre_amalgamation',
     'notes': 'Pre-1998 Toronto amalgamation'},
    {'alias': 'former Toronto', 'canonical': 'Toronto', 'type': 'pre_amalgamation',
     'notes': 'Pre-1998 Toronto amalgamation'},

    # Ottawa pre-amalgamation (2001)
    {'alias': 'Nepean', 'canonical': 'Ottawa', 'type': 'pre_amalgamation',
     'notes': 'Pre-2001 Ottawa amalgamation'},
    {'alias': 'Kanata', 'canonical': 'Ottawa', 'type': 'pre_amalgamation',
     'notes': 'Pre-2001 Ottawa amalgamation'},
    {'alias': 'Gloucester', 'canonical': 'Ottawa', 'type': 'pre_amalgamation',
     'notes': 'Pre-2001 Ottawa amalgamation'},
    {'alias': 'Vanier', 'canonical': 'Ottawa', 'type': 'pre_amalgamation',
     'notes': 'Pre-2001 Ottawa amalgamation'},
    {'alias': 'Cumberland', 'canonical': 'Ottawa', 'type': 'pre_amalgamation',
     'notes': 'Pre-2001 Ottawa amalgamation'},
    {'alias': 'Osgoode', 'canonical': 'Ottawa', 'type': 'pre_amalgamation',
     'notes': 'Pre-2001 Ottawa amalgamation'},
    {'alias': 'Rideau', 'canonical': 'Ottawa', 'type': 'pre_amalgamation',
     'notes': 'Pre-2001 Ottawa amalgamation'},
    {'alias': 'Goulbourn', 'canonical': 'Ottawa', 'type': 'pre_amalgamation',
     'notes': 'Pre-2001 Ottawa amalgamation'},
    {'alias': 'West Carleton', 'canonical': 'Ottawa', 'type': 'pre_amalgamation',
     'notes': 'Pre-2001 Ottawa amalgamation'},
    {'alias': 'Rockcliffe Park', 'canonical': 'Ottawa', 'type': 'pre_amalgamation',
     'notes': 'Pre-2001 Ottawa amalgamation'},

    # Hamilton pre-amalgamation (2001)
    {'alias': 'Ancaster', 'canonical': 'Hamilton', 'type': 'pre_amalgamation',
     'notes': 'Pre-2001 Hamilton amalgamation'},
    {'alias': 'Dundas', 'canonical': 'Hamilton', 'type': 'pre_amalgamation',
     'notes': 'Pre-2001 Hamilton amalgamation'},
    {'alias': 'Flamborough', 'canonical': 'Hamilton', 'type': 'pre_amalgamation',
     'notes': 'Pre-2001 Hamilton amalgamation'},
    {'alias': 'Glanbrook', 'canonical': 'Hamilton', 'type': 'pre_amalgamation',
     'notes': 'Pre-2001 Hamilton amalgamation'},
    {'alias': 'Stoney Creek', 'canonical': 'Hamilton', 'type': 'pre_amalgamation',
     'notes': 'Pre-2001 Hamilton amalgamation'},

    # Greater Sudbury pre-amalgamation (2001)
    {'alias': 'Sudbury', 'canonical': 'Greater Sudbury', 'type': 'pre_amalgamation',
     'notes': 'Pre-2001 Greater Sudbury amalgamation'},
    {'alias': 'Capreol', 'canonical': 'Greater Sudbury', 'type': 'pre_amalgamation',
     'notes': 'Pre-2001 Greater Sudbury amalgamation'},
    {'alias': 'Nickel Centre', 'canonical': 'Greater Sudbury', 'type': 'pre_amalgamation',
     'notes': 'Pre-2001 Greater Sudbury amalgamation'},
    {'alias': 'Onaping Falls', 'canonical': 'Greater Sudbury', 'type': 'pre_amalgamation',
     'notes': 'Pre-2001 Greater Sudbury amalgamation'},
    {'alias': 'Rayside-Balfour', 'canonical': 'Greater Sudbury', 'type': 'pre_amalgamation',
     'notes': 'Pre-2001 Greater Sudbury amalgamation'},
    {'alias': 'Valley East', 'canonical': 'Greater Sudbury', 'type': 'pre_amalgamation',
     'notes': 'Pre-2001 Greater Sudbury amalgamation'},
    {'alias': 'Walden', 'canonical': 'Greater Sudbury', 'type': 'pre_amalgamation',
     'notes': 'Pre-2001 Greater Sudbury amalgamation'},

    # Kawartha Lakes pre-amalgamation (2001)
    {'alias': 'Lindsay', 'canonical': 'Kawartha Lakes', 'type': 'pre_amalgamation',
     'notes': 'Pre-2001 Kawartha Lakes amalgamation'},
    {'alias': 'Ops', 'canonical': 'Kawartha Lakes', 'type': 'pre_amalgamation',
     'notes': 'Pre-2001 Kawartha Lakes amalgamation'},

    # Bilingual names
    {'alias': 'Greater Sudbury / Grand Sudbury', 'canonical': 'Greater Sudbury',
     'type': 'bilingual', 'notes': 'Official bilingual name'},
    {'alias': 'Grand Sudbury', 'canonical': 'Greater Sudbury',
     'type': 'bilingual', 'notes': 'French name variant'},

    # Common prefix variations
    {'alias': 'City of Toronto', 'canonical': 'Toronto', 'type': 'prefix_variation',
     'notes': 'Common formal name'},
    {'alias': 'City of Ottawa', 'canonical': 'Ottawa', 'type': 'prefix_variation',
     'notes': 'Common formal name'},
    {'alias': 'City of Hamilton', 'canonical': 'Hamilton', 'type': 'prefix_variation',
     'notes': 'Common formal name'},
    {'alias': 'City of London', 'canonical': 'London', 'type': 'prefix_variation',
     'notes': 'Common formal name'},
    {'alias': 'City of Windsor', 'canonical': 'Windsor', 'type': 'prefix_variation',
     'notes': 'Common formal name'},
    {'alias': 'City of Guelph', 'canonical': 'Guelph', 'type': 'prefix_variation',
     'notes': 'Common formal name'},
    {'alias': 'City of Kitchener', 'canonical': 'Kitchener', 'type': 'prefix_variation',
     'notes': 'Common formal name'},
    {'alias': 'City of Cambridge', 'canonical': 'Cambridge', 'type': 'prefix_variation',
     'notes': 'Common formal name'},
    {'alias': 'City of Waterloo', 'canonical': 'Waterloo', 'type': 'prefix_variation',
     'notes': 'Common formal name'},
    {'alias': 'City of Mississauga', 'canonical': 'Mississauga', 'type': 'prefix_variation',
     'notes': 'Common formal name'},
    {'alias': 'City of Brampton', 'canonical': 'Brampton', 'type': 'prefix_variation',
     'notes': 'Common formal name'},
    {'alias': 'Town of Oakville', 'canonical': 'Oakville', 'type': 'prefix_variation',
     'notes': 'Common formal name'},
    {'alias': 'Town of Milton', 'canonical': 'Milton', 'type': 'prefix_variation',
     'notes': 'Common formal name'},
    {'alias': 'Town of Burlington', 'canonical': 'Burlington', 'type': 'prefix_variation',
     'notes': 'Common formal name'},
]


class MunicipalityAliasSetup(SetupTransformer):
    """
    Setup transformer that populates municipality_aliases table.

    Extracts alias patterns from properties table by comparing:
    - City names in properties
    - Canonical municipality names from lower_municipal_boundaries (via spatial lookup)

    When a city name doesn't match the canonical municipality it's located in,
    that's an alias (e.g., "Scarborough" → "Toronto").
    """

    @property
    def name(self) -> str:
        return "MunicipalityAliasSetup"

    async def needs_setup(self, pool) -> bool:
        """Check if municipality_aliases table is empty."""
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM municipality_aliases"
            )
            return count == 0

    async def check_dependencies(self, pool) -> Tuple[bool, str]:
        """
        Verify municipality_aliases table exists.

        Note: We no longer require properties data since we seed known aliases.
        The spatial extraction from properties is now optional/supplementary.
        """
        async with pool.acquire() as conn:
            # Just verify the table exists
            table_exists = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_name = 'municipality_aliases'
                )
            """)
            if not table_exists:
                return False, "municipality_aliases table does not exist"

            return True, ""

    async def _seed_known_aliases(self, conn) -> int:
        """
        Seed the known pre-amalgamation and common aliases.

        Returns the number of aliases inserted/updated.
        """
        seeded = 0
        for alias_data in KNOWN_MUNICIPALITY_ALIASES:
            await conn.execute("""
                INSERT INTO municipality_aliases
                (alias, normalized_alias, canonical_name, alias_type, notes)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (normalized_alias, canonical_name) DO UPDATE
                SET alias = EXCLUDED.alias,
                    alias_type = EXCLUDED.alias_type,
                    notes = EXCLUDED.notes
            """, alias_data['alias'],
                alias_data['alias'].upper(),
                alias_data['canonical'],
                alias_data['type'],
                alias_data['notes'])
            seeded += 1
        return seeded

    def _normalize_canonical_name(self, name: str) -> str:
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

    async def run_setup(self, pool) -> SetupResult:
        """
        Populate municipality aliases table.

        1. Seeds known pre-amalgamation and common aliases
        2. Optionally extracts additional aliases from properties table via spatial join
        """
        import time
        start = time.time()

        async with pool.acquire() as conn:
            # Step 1: Seed known aliases (always runs)
            seeded_count = await self._seed_known_aliases(conn)

            # Step 2: Try to extract additional aliases from properties
            # This may fail if properties table is empty, which is fine
            extracted_count = 0
            try:
                # Check if we have properties data
                props_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM properties WHERE city IS NOT NULL"
                )

                if props_count > 0:
                    # Find city names that don't match their canonical municipality
                    # by doing a spatial join with municipal boundaries
                    # Uses 'name' column which contains values like "CITY OF TORONTO"
                    variations = await conn.fetch("""
                        WITH property_municipalities AS (
                            SELECT DISTINCT
                                p.city,
                                m.name as canonical_municipality,
                                COUNT(*) as address_count
                            FROM properties p
                            JOIN lower_municipal_boundaries m
                                ON ST_Contains(
                                    m.geometry,
                                    ST_SetSRID(ST_MakePoint(p.longitude, p.latitude), 4326)
                                )
                            WHERE p.city IS NOT NULL
                              AND p.latitude IS NOT NULL
                              AND p.longitude IS NOT NULL
                            GROUP BY p.city, m.name
                        )
                        SELECT
                            city as alias,
                            UPPER(city) as normalized_alias,
                            canonical_municipality as canonical_name,
                            address_count
                        FROM property_municipalities
                        WHERE UPPER(city) != UPPER(canonical_municipality)
                          AND address_count >= 100  -- Only significant variations
                        ORDER BY address_count DESC
                    """)

                    # Process variations if any found
                    if variations:
                        # Determine alias type based on patterns
                        def get_alias_type(alias: str, canonical: str) -> str:
                            alias_upper = alias.upper()
                            canonical_upper = canonical.upper()

                            # Toronto pre-amalgamation
                            toronto_areas = ['NORTH YORK', 'SCARBOROUGH', 'ETOBICOKE',
                                             'YORK', 'EAST YORK', 'FORMER TORONTO']
                            if alias_upper in toronto_areas and 'TORONTO' in canonical_upper:
                                return 'pre_amalgamation'

                            # Ottawa pre-amalgamation
                            ottawa_areas = ['NEPEAN', 'KANATA', 'GLOUCESTER', 'VANIER',
                                            'CUMBERLAND', 'OSGOODE', 'RIDEAU', 'GOULBOURN',
                                            'WEST CARLETON']
                            if alias_upper in ottawa_areas and 'OTTAWA' in canonical_upper:
                                return 'pre_amalgamation'

                            # Hamilton pre-amalgamation
                            hamilton_areas = ['ANCASTER', 'DUNDAS', 'FLAMBOROUGH', 'GLANBROOK',
                                              'STONEY CREEK']
                            if alias_upper in hamilton_areas and 'HAMILTON' in canonical_upper:
                                return 'pre_amalgamation'

                            # Bilingual (contains slash)
                            if '/' in alias:
                                return 'bilingual'

                            # County variation (contains County)
                            if 'COUNTY' in alias_upper:
                                return 'county_variation'

                            return 'other'

                        # Insert aliases with normalized canonical names
                        for row in variations:
                            # Normalize canonical name: "CITY OF TORONTO" -> "Toronto"
                            canonical_normalized = self._normalize_canonical_name(row['canonical_name'])
                            alias_type = get_alias_type(row['alias'], row['canonical_name'])

                            await conn.execute("""
                                INSERT INTO municipality_aliases
                                (alias, normalized_alias, canonical_name, alias_type, notes)
                                VALUES ($1, $2, $3, $4, $5)
                                ON CONFLICT (normalized_alias, canonical_name) DO UPDATE
                                SET alias = EXCLUDED.alias,
                                    alias_type = EXCLUDED.alias_type,
                                    notes = EXCLUDED.notes
                            """, row['alias'], row['normalized_alias'], canonical_normalized,
                                alias_type, f"Extracted from properties ({row['address_count']:,} addresses)")

                            extracted_count += 1

            except Exception:
                # Properties extraction failed - that's okay, we have seeded aliases
                pass

        duration = time.time() - start
        total_count = seeded_count + extracted_count
        return SetupResult(
            status='success',
            rows_affected=total_count,
            duration_seconds=duration,
            details={
                'seeded_aliases': seeded_count,
                'extracted_from_properties': extracted_count,
                'total': total_count
            }
        )
