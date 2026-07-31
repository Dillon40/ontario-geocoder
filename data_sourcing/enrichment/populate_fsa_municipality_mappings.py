#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "asyncpg>=0.29.0",
# ]
# ///
"""
Populate FSA-Municipality Mapping Table

This script performs spatial analysis to map FSAs to municipalities:
1. Geographic intersection (ST_Intersects) - authoritative source
2. Coverage percentage calculation (area-based)
3. Primary municipality detection (largest overlap)
4. NAR address count validation (address-based verification)

The results enable:
- Fast FSA → Municipality lookups
- Municipality → FSA lookups
- Geographic validation for queries
- Query performance optimization via geographic filtering
"""

import asyncio
import asyncpg
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path for db_utils import
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from data_sourcing.db_utils import get_db_config


class FSAMunicipalityMapper:
    """Populates FSA-Municipality mappings using spatial analysis"""

    def __init__(self):
        self.pool = None
        self.total_fsas = 0
        self.total_municipalities = 0
        self.total_mappings = 0

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
            command_timeout=600  # 10 minutes for complex spatial queries
        )

        print("✓ Connected to database\n")

    async def close(self):
        """Close database connection"""
        if self.pool:
            await self.pool.close()

    async def get_counts(self):
        """Get count of FSAs and municipalities"""
        async with self.pool.acquire() as conn:
            self.total_fsas = await conn.fetchval(
                "SELECT COUNT(*) FROM fsa_boundaries WHERE province_id = '35'"
            )
            self.total_municipalities = await conn.fetchval(
                "SELECT COUNT(DISTINCT municipal_id) FROM lower_municipal_boundaries"
            )

        print(f"Ontario FSAs: {self.total_fsas}")
        print(f"Ontario Municipalities: {self.total_municipalities}\n")

    async def clear_existing_mappings(self):
        """Clear existing mappings to allow re-population"""
        async with self.pool.acquire() as conn:
            deleted = await conn.fetchval("SELECT COUNT(*) FROM fsa_municipality_mappings")
            await conn.execute("TRUNCATE TABLE fsa_municipality_mappings RESTART IDENTITY")

        if deleted > 0:
            print(f"✓ Cleared {deleted:,} existing mappings\n")

    async def populate_geographic_mappings(self):
        """
        Method 1: Geographic Intersection (Authoritative)

        Uses PostGIS spatial analysis to find FSA-municipality intersections.
        Calculates coverage percentage and marks primary municipality.
        """
        print("="*80)
        print("METHOD 1: GEOGRAPHIC INTERSECTION ANALYSIS")
        print("="*80)
        print()

        async with self.pool.acquire() as conn:
            print("Analyzing spatial intersections (this may take a few minutes)...")

            # Complex spatial query to find all FSA-municipality intersections
            # Note: Some municipalities have multiple geometries (islands), so we group by municipal_id
            query = """
                INSERT INTO fsa_municipality_mappings (
                    fsa_code,
                    municipal_id,
                    municipality_name_short,
                    municipality_name_full,
                    coverage_pct,
                    geometry_intersection,
                    is_primary
                )
                WITH intersections AS (
                    SELECT
                        f.fsa_code,
                        m.municipal_id,
                        m.short_name as municipality_name_short,
                        m.name as municipality_name_full,
                        f.land_area_km2 as fsa_area,
                        ST_Intersection(f.geometry, m.geometry) as intersection_geom,
                        -- Calculate area of intersection in km² (convert from m²)
                        ST_Area(ST_Intersection(f.geometry, m.geometry)::geography) / 1000000.0 as intersection_area_km2
                    FROM fsa_boundaries f
                    CROSS JOIN lower_municipal_boundaries m
                    WHERE f.province_id = '35'
                      AND ST_Intersects(f.geometry, m.geometry)
                ),
                grouped_intersections AS (
                    -- Group by fsa_code + municipal_id to combine multiple geometries (islands)
                    SELECT
                        fsa_code,
                        municipal_id,
                        -- Use MAX to get consistent municipality names (all rows have same value per municipal_id)
                        MAX(municipality_name_short) as municipality_name_short,
                        MAX(municipality_name_full) as municipality_name_full,
                        MAX(fsa_area) as fsa_area,
                        -- Union all intersection geometries for this FSA+municipality
                        ST_Union(intersection_geom) as combined_intersection,
                        SUM(intersection_area_km2) as total_intersection_area_km2
                    FROM intersections
                    GROUP BY fsa_code, municipal_id
                ),
                coverage AS (
                    SELECT
                        fsa_code,
                        municipal_id,
                        municipality_name_short,
                        municipality_name_full,
                        combined_intersection,
                        total_intersection_area_km2,
                        -- Calculate what % of FSA area is in this municipality
                        ROUND((total_intersection_area_km2 / NULLIF(fsa_area, 0) * 100)::numeric, 2) as coverage_pct,
                        -- Rank municipalities by coverage for each FSA
                        ROW_NUMBER() OVER (
                            PARTITION BY fsa_code
                            ORDER BY total_intersection_area_km2 DESC
                        ) as coverage_rank
                    FROM grouped_intersections
                )
                SELECT
                    fsa_code,
                    municipal_id,
                    municipality_name_short,
                    municipality_name_full,
                    coverage_pct,
                    combined_intersection as geometry_intersection,
                    (coverage_rank = 1) as is_primary  -- Mark municipality with largest coverage
                FROM coverage
                ORDER BY fsa_code, coverage_pct DESC;
            """

            result = await conn.execute(query)
            # Extract number from "INSERT 0 N"
            inserted = int(result.split()[-1]) if result else 0

            print(f"✓ Created {inserted:,} FSA-municipality mappings\n")

            return inserted

    async def populate_address_counts(self):
        """
        Method 2: NAR Address Analysis (Validation)

        Counts NAR addresses for each FSA-municipality combination.
        Uses normalized municipality names to handle variations.
        Updates existing mappings with address counts.
        """
        print("="*80)
        print("METHOD 2: NAR ADDRESS COUNT ANALYSIS")
        print("="*80)
        print()

        async with self.pool.acquire() as conn:
            print("Counting NAR addresses by FSA + municipality...")

            # Update mappings with address counts from NAR
            query = """
                WITH nar_counts AS (
                    SELECT
                        fsa_code,
                        normalize_municipality_name(csd_eng_name) as normalized_city,
                        COUNT(*) as address_count
                    FROM nar_addresses
                    WHERE prov_code = 35
                      AND fsa_code IS NOT NULL
                      AND csd_eng_name IS NOT NULL
                    GROUP BY fsa_code, normalize_municipality_name(csd_eng_name)
                )
                UPDATE fsa_municipality_mappings fm
                SET address_count = nc.address_count,
                    updated_at = NOW()
                FROM nar_counts nc
                WHERE fm.fsa_code = nc.fsa_code
                  AND normalize_municipality_name(fm.municipality_name_short) = nc.normalized_city;
            """

            result = await conn.execute(query)
            # Extract number from "UPDATE N"
            updated = int(result.split()[-1]) if result else 0

            print(f"✓ Updated {updated:,} mappings with NAR address counts\n")

            return updated

    async def print_summary_statistics(self):
        """Print summary statistics about the mappings"""
        print("="*80)
        print("SUMMARY STATISTICS")
        print("="*80)
        print()

        async with self.pool.acquire() as conn:
            # Total mappings
            total = await conn.fetchval("SELECT COUNT(*) FROM fsa_municipality_mappings")
            print(f"Total FSA-Municipality mappings: {total:,}")

            # FSAs with single vs multiple municipalities
            fsa_stats = await conn.fetchrow("""
                SELECT
                    COUNT(DISTINCT fsa_code) as total_fsas,
                    COUNT(DISTINCT CASE WHEN muni_count = 1 THEN fsa_code END) as single_muni_fsas,
                    COUNT(DISTINCT CASE WHEN muni_count > 1 THEN fsa_code END) as multi_muni_fsas,
                    MAX(muni_count) as max_municipalities_per_fsa
                FROM (
                    SELECT fsa_code, COUNT(*) as muni_count
                    FROM fsa_municipality_mappings
                    GROUP BY fsa_code
                ) sub;
            """)

            print(f"\nFSA Distribution:")
            print(f"  - Total FSAs mapped: {fsa_stats['total_fsas']:,}")
            print(f"  - FSAs in single municipality: {fsa_stats['single_muni_fsas']:,} ({fsa_stats['single_muni_fsas']/fsa_stats['total_fsas']*100:.1f}%)")
            print(f"  - FSAs spanning multiple municipalities: {fsa_stats['multi_muni_fsas']:,} ({fsa_stats['multi_muni_fsas']/fsa_stats['total_fsas']*100:.1f}%)")
            print(f"  - Max municipalities per FSA: {fsa_stats['max_municipalities_per_fsa']}")

            # Municipality coverage
            muni_stats = await conn.fetchrow("""
                SELECT
                    COUNT(DISTINCT municipal_id) as total_munis,
                    COUNT(DISTINCT CASE WHEN fsa_count = 1 THEN municipal_id END) as single_fsa_munis,
                    COUNT(DISTINCT CASE WHEN fsa_count > 1 THEN municipal_id END) as multi_fsa_munis,
                    MAX(fsa_count) as max_fsas_per_municipality
                FROM (
                    SELECT municipal_id, COUNT(*) as fsa_count
                    FROM fsa_municipality_mappings
                    WHERE municipal_id IS NOT NULL
                    GROUP BY municipal_id
                ) sub;
            """)

            print(f"\nMunicipality Distribution:")
            print(f"  - Total municipalities mapped: {muni_stats['total_munis']:,}")
            print(f"  - Municipalities with single FSA: {muni_stats['single_fsa_munis']:,}")
            print(f"  - Municipalities with multiple FSAs: {muni_stats['multi_fsa_munis']:,}")
            print(f"  - Max FSAs per municipality: {muni_stats['max_fsas_per_municipality']}")

            # Address count statistics
            addr_stats = await conn.fetchrow("""
                SELECT
                    SUM(address_count) as total_addresses,
                    COUNT(*) FILTER (WHERE address_count > 0) as mappings_with_addresses,
                    COUNT(*) FILTER (WHERE address_count = 0) as mappings_without_addresses,
                    AVG(address_count) FILTER (WHERE address_count > 0) as avg_addresses_per_mapping,
                    MAX(address_count) as max_addresses
                FROM fsa_municipality_mappings;
            """)

            print(f"\nNAR Address Distribution:")
            print(f"  - Total NAR addresses mapped: {addr_stats['total_addresses']:,}")
            print(f"  - Mappings with NAR addresses: {addr_stats['mappings_with_addresses']:,}")
            print(f"  - Mappings without NAR addresses: {addr_stats['mappings_without_addresses']:,}")
            print(f"  - Avg addresses per mapping: {addr_stats['avg_addresses_per_mapping']:.0f}")
            print(f"  - Max addresses in single FSA+muni: {addr_stats['max_addresses']:,}")

            # Top 10 FSA-municipality combinations by address count
            print(f"\nTop 10 FSA-Municipality Combinations by NAR Address Count:")
            top_combos = await conn.fetch("""
                SELECT
                    fsa_code,
                    municipality_name_short,
                    address_count,
                    coverage_pct,
                    is_primary
                FROM fsa_municipality_mappings
                WHERE address_count > 0
                ORDER BY address_count DESC
                LIMIT 10;
            """)

            for i, combo in enumerate(top_combos, 1):
                primary_marker = " [PRIMARY]" if combo['is_primary'] else ""
                print(f"  {i:2d}. {combo['fsa_code']} + {combo['municipality_name_short']:<25} "
                      f"{combo['address_count']:>7,} addresses ({combo['coverage_pct']:>5.1f}% coverage){primary_marker}")

    async def validate_mappings(self):
        """Validate the mappings for data quality issues"""
        print("\n" + "="*80)
        print("VALIDATION CHECKS")
        print("="*80)
        print()

        async with self.pool.acquire() as conn:
            # Check 1: All Ontario FSAs should have at least one mapping
            unmapped_fsas = await conn.fetch("""
                SELECT f.fsa_code, f.province_name
                FROM fsa_boundaries f
                WHERE f.province_id = '35'
                  AND NOT EXISTS (
                      SELECT 1 FROM fsa_municipality_mappings m
                      WHERE m.fsa_code = f.fsa_code
                  )
                LIMIT 10;
            """)

            if unmapped_fsas:
                print(f"⚠ WARNING: {len(unmapped_fsas)} FSAs have no municipality mappings:")
                for fsa in unmapped_fsas[:5]:
                    print(f"  - {fsa['fsa_code']} ({fsa['province_name']})")
                if len(unmapped_fsas) > 5:
                    print(f"  ... and {len(unmapped_fsas) - 5} more")
            else:
                print("✓ All Ontario FSAs have at least one municipality mapping")

            # Check 2: Each FSA should have exactly one primary municipality
            multi_primary = await conn.fetchval("""
                SELECT COUNT(DISTINCT fsa_code)
                FROM (
                    SELECT fsa_code, COUNT(*) as primary_count
                    FROM fsa_municipality_mappings
                    WHERE is_primary = true
                    GROUP BY fsa_code
                    HAVING COUNT(*) > 1
                ) sub;
            """)

            if multi_primary > 0:
                print(f"⚠ WARNING: {multi_primary} FSAs have multiple primary municipalities")
            else:
                print("✓ Each FSA has exactly one primary municipality")

            # Check 3: Coverage percentages should be reasonable
            invalid_coverage = await conn.fetchval("""
                SELECT COUNT(*)
                FROM fsa_municipality_mappings
                WHERE coverage_pct < 0 OR coverage_pct > 100;
            """)

            if invalid_coverage > 0:
                print(f"⚠ WARNING: {invalid_coverage} mappings have invalid coverage percentages")
            else:
                print("✓ All coverage percentages are within valid range (0-100%)")

            # Check 4: Address counts should match NAR totals
            nar_total = await conn.fetchval("""
                SELECT COUNT(*)
                FROM nar_addresses
                WHERE prov_code = 35
                  AND fsa_code IS NOT NULL
                  AND csd_eng_name IS NOT NULL;
            """)

            mapping_total = await conn.fetchval("""
                SELECT SUM(address_count)
                FROM fsa_municipality_mappings;
            """)

            if nar_total != mapping_total:
                diff = abs(nar_total - mapping_total)
                pct_diff = diff / nar_total * 100
                print(f"⚠ INFO: Address count discrepancy: {diff:,} addresses ({pct_diff:.1f}%)")
                print(f"  NAR addresses with FSA+city: {nar_total:,}")
                print(f"  Total in mappings: {mapping_total:,}")
                print(f"  This is expected due to municipality name normalization differences")
            else:
                print("✓ Address counts match NAR totals exactly")


async def main():
    """Main execution workflow"""
    print("\n" + "="*80)
    print("FSA-MUNICIPALITY MAPPING POPULATION")
    print("="*80)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*80)
    print()

    mapper = FSAMunicipalityMapper()

    try:
        await mapper.connect()
        await mapper.get_counts()
        await mapper.clear_existing_mappings()

        # Method 1: Geographic intersection
        await mapper.populate_geographic_mappings()

        # Method 2: NAR address counts
        await mapper.populate_address_counts()

        # Statistics and validation
        await mapper.print_summary_statistics()
        await mapper.validate_mappings()

        print("\n" + "="*80)
        print("POPULATION COMPLETE")
        print("="*80)
        print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print()
        print("Next steps:")
        print("1. Run extract_municipality_aliases.py to populate municipality aliases")
        print("2. Review summary statistics above")
        print("3. Investigate any validation warnings")
        print("="*80)
        print()

    except Exception as e:
        print(f"\n[FAIL] Error during population: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await mapper.close()


if __name__ == '__main__':
    asyncio.run(main())
