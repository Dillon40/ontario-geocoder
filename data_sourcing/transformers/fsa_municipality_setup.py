"""
FSA-Municipality mapping setup transformer.

Populates fsa_municipality_mappings table via spatial analysis.
This enables geographic validation and FSA-based query optimization.

The mappings show which municipalities each FSA (Forward Sortation Area)
intersects with and what percentage of the FSA is in each municipality.
"""
from typing import Tuple

from .base import SetupTransformer, SetupResult


class FSAMunicipalitySetup(SetupTransformer):
    """
    Setup transformer that populates fsa_municipality_mappings table.

    Performs spatial intersection analysis between FSA boundaries
    and municipal boundaries to create the many-to-many mapping.

    Each FSA can span multiple municipalities (boundary crossing),
    and each municipality can contain multiple FSAs.
    """

    @property
    def name(self) -> str:
        return "FSAMunicipalitySetup"

    async def needs_setup(self, pool) -> bool:
        """Check if fsa_municipality_mappings table is empty."""
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM fsa_municipality_mappings"
            )
            return count == 0

    async def check_dependencies(self, pool) -> Tuple[bool, str]:
        """Verify FSA and municipal boundaries exist."""
        async with pool.acquire() as conn:
            fsa_count = await conn.fetchval(
                "SELECT COUNT(*) FROM fsa_boundaries WHERE province_id = '35'"
            )
            if fsa_count == 0:
                return False, "FSA boundaries table is empty (import FSA data first)"

            muni_count = await conn.fetchval(
                "SELECT COUNT(*) FROM lower_municipal_boundaries"
            )
            if muni_count == 0:
                return False, "Municipal boundaries table is empty"

            return True, ""

    async def run_setup(self, pool) -> SetupResult:
        """Populate FSA-Municipality mappings via spatial analysis."""
        import time
        start = time.time()

        async with pool.acquire() as conn:
            # Clear any existing mappings
            await conn.execute("TRUNCATE TABLE fsa_municipality_mappings RESTART IDENTITY")

            # Complex spatial query to find all FSA-municipality intersections
            # Calculates coverage percentage and marks primary municipality
            await conn.execute("""
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
                        ST_Area(ST_Intersection(f.geometry, m.geometry)::geography) / 1000000.0 as intersection_area_km2
                    FROM fsa_boundaries f
                    CROSS JOIN lower_municipal_boundaries m
                    WHERE f.province_id = '35'
                      AND ST_Intersects(f.geometry, m.geometry)
                ),
                grouped_intersections AS (
                    SELECT
                        fsa_code,
                        municipal_id,
                        MAX(municipality_name_short) as municipality_name_short,
                        MAX(municipality_name_full) as municipality_name_full,
                        MAX(fsa_area) as fsa_area,
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
                        LEAST(
                            ROUND(
                                (total_intersection_area_km2 / NULLIF(fsa_area, 0) * 100)::numeric,
                                2
                            ),
                            100.00
                        ) as coverage_pct,
                        ROW_NUMBER() OVER (
                            PARTITION BY fsa_code
                            ORDER BY total_intersection_area_km2 DESC
                        ) as rank
                    FROM grouped_intersections
                )
                SELECT
                    fsa_code,
                    municipal_id,
                    municipality_name_short,
                    municipality_name_full,
                    coverage_pct,
                    combined_intersection,
                    (rank = 1) as is_primary
                FROM coverage
            """)

            # Get count of inserted rows
            row_count = await conn.fetchval(
                "SELECT COUNT(*) FROM fsa_municipality_mappings"
            )

            # Get some statistics
            stats = await conn.fetchrow("""
                SELECT
                    COUNT(DISTINCT fsa_code) as unique_fsas,
                    COUNT(DISTINCT municipal_id) as unique_municipalities,
                    COUNT(*) FILTER (WHERE is_primary) as primary_mappings,
                    ROUND(AVG(coverage_pct)::numeric, 2) as avg_coverage
                FROM fsa_municipality_mappings
            """)

        duration = time.time() - start
        return SetupResult(
            status='success',
            rows_affected=row_count,
            duration_seconds=duration,
            details={
                'unique_fsas': stats['unique_fsas'],
                'unique_municipalities': stats['unique_municipalities'],
                'primary_mappings': stats['primary_mappings'],
                'avg_coverage_pct': float(stats['avg_coverage']) if stats['avg_coverage'] else 0
            }
        )
