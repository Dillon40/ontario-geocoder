"""
FSA enricher transformer.

Performs spatial lookup to determine FSA (Forward Sortation Area) code
for addresses that lack postal codes. Uses PostGIS ST_Contains against
the fsa_boundaries table.

Generates warning (not error) if FSA not found - coordinates near
boundaries or offshore may not match any FSA.
"""
import logging
from typing import Any, Dict, List, Optional, Tuple

from .base import AsyncTransformer, TransformContext, TransformResult
from .db_retry import retry_async

logger = logging.getLogger(__name__)


class FSAEnricher(AsyncTransformer):
    """
    Async transformer that enriches records with FSA codes from spatial lookup.

    Uses PostGIS to find which FSA polygon contains the record's coordinates.

    Configuration options:
        overwrite_existing: Overwrite existing fsa_code/postal_code (default: False)
        batch_size: Records per batch query (default: 500)
    """

    @property
    def name(self) -> str:
        return "FSAEnricher"

    def __init__(self, config: Optional[Dict[str, Any]] = None, pool=None):
        super().__init__(config)
        self.overwrite_existing = self.config.get('overwrite_existing', False)
        self.batch_size = self.config.get('batch_size', 500)
        self._pool = pool

    def set_pool(self, pool) -> None:
        """Set the database connection pool."""
        self._pool = pool

    async def lookup_fsa(self, lon: float, lat: float) -> Optional[str]:
        """
        Lookup FSA code for a single coordinate.

        Args:
            lon: Longitude
            lat: Latitude

        Returns:
            FSA code (3 characters) or None if not found
        """
        if not self._pool:
            return None

        query = """
            SELECT fsa_code FROM fsa_boundaries
            WHERE ST_Contains(geometry, ST_SetSRID(ST_MakePoint($1, $2), 4326))
            LIMIT 1
        """

        async def _do_lookup():
            async with self._pool.acquire() as conn:
                return await conn.fetchrow(query, lon, lat)

        try:
            row = await retry_async(
                _do_lookup,
                operation_name="FSAEnricher.lookup_fsa",
            )
            return row['fsa_code'] if row else None
        except Exception:
            logger.warning("FSAEnricher.lookup_fsa failed after retries for (%s, %s)", lon, lat)
            return None

    async def lookup_fsa_batch(
        self,
        coords: List[Tuple[int, float, float]]
    ) -> Dict[int, Optional[str]]:
        """
        Batch lookup FSA codes for multiple coordinates.

        Falls back to individual lookups if the batch query fails after retries.

        Args:
            coords: List of (index, lon, lat) tuples

        Returns:
            Dict mapping index → FSA code (or None)
        """
        if not self._pool or not coords:
            return {}

        # Build arrays for batch query
        indices = [c[0] for c in coords]
        lons = [c[1] for c in coords]
        lats = [c[2] for c in coords]

        query = """
            WITH coords AS (
                SELECT
                    unnest($1::int[]) as idx,
                    unnest($2::float[]) as lon,
                    unnest($3::float[]) as lat
            )
            SELECT c.idx, fb.fsa_code
            FROM coords c
            LEFT JOIN fsa_boundaries fb
                ON ST_Contains(fb.geometry, ST_SetSRID(ST_MakePoint(c.lon, c.lat), 4326))
        """

        results: Dict[int, Optional[str]] = {i: None for i in indices}

        async def _do_batch():
            async with self._pool.acquire() as conn:
                return await conn.fetch(query, indices, lons, lats)

        try:
            rows = await retry_async(
                _do_batch,
                operation_name="FSAEnricher.lookup_fsa_batch",
            )
            for row in rows:
                results[row['idx']] = row['fsa_code']
        except Exception:
            logger.warning(
                "FSAEnricher.lookup_fsa_batch failed after retries, "
                "falling back to individual lookups for %d coordinates",
                len(coords),
            )
            for idx, lon, lat in coords:
                results[idx] = await self.lookup_fsa(lon, lat)

        return results

    async def transform_async(self, context: TransformContext) -> TransformResult:
        """Enrich record with FSA code from spatial lookup."""
        if not self.should_run(context):
            return context.last_result

        # Skip if already has fsa_code or postal_code and not overwriting
        if not self.overwrite_existing:
            existing_fsa = context.get_field('fsa_code')
            existing_postal = context.get_field('postal_code')
            if existing_fsa or existing_postal:
                return TransformResult.SUCCESS

        # Get coordinates
        lat = context.get_field('latitude')
        lon = context.get_field('longitude')

        if lat is None or lon is None:
            context.add_warning(
                self.name,
                "Missing coordinates for FSA lookup",
                details={'latitude': lat, 'longitude': lon}
            )
            return TransformResult.SUCCESS

        # Validate coordinate types
        try:
            lat = float(lat)
            lon = float(lon)
        except (TypeError, ValueError):
            context.add_warning(
                self.name,
                "Invalid coordinate values for FSA lookup",
                details={'latitude': lat, 'longitude': lon}
            )
            return TransformResult.SUCCESS

        # Perform lookup
        fsa_code = await self.lookup_fsa(lon, lat)

        if fsa_code:
            context.set_field(
                'fsa_code',
                fsa_code,
                self.name,
                reason=f"Spatial lookup from coordinates ({lat}, {lon})"
            )
            return TransformResult.MODIFIED
        else:
            # Warning only - coordinates may be near boundary or offshore
            context.add_warning(
                self.name,
                f"No FSA found for coordinates ({lat}, {lon})",
                field='fsa_code',
                details={'latitude': lat, 'longitude': lon}
            )
            return TransformResult.SUCCESS

    async def transform_batch_async(
        self,
        contexts: List[TransformContext],
        batch_size: int = 0
    ) -> List[TransformResult]:
        """
        Efficient batch FSA lookup for multiple records.

        Args:
            contexts: List of TransformContexts
            batch_size: Override instance batch_size if provided

        Returns:
            List of TransformResults
        """
        if not batch_size:
            batch_size = self.batch_size

        results: List[TransformResult] = []
        coords_to_lookup: List[Tuple[int, float, float]] = []
        context_map: Dict[int, TransformContext] = {}

        # Collect coordinates that need lookup
        for i, context in enumerate(contexts):
            if not self.should_run(context):
                results.append(context.last_result)
                continue

            # Skip if already has fsa_code or postal_code and not overwriting
            if not self.overwrite_existing:
                existing_fsa = context.get_field('fsa_code')
                existing_postal = context.get_field('postal_code')
                if existing_fsa or existing_postal:
                    results.append(TransformResult.SUCCESS)
                    continue

            lat = context.get_field('latitude')
            lon = context.get_field('longitude')

            if lat is None or lon is None:
                context.add_warning(
                    self.name,
                    "Missing coordinates for FSA lookup"
                )
                results.append(TransformResult.SUCCESS)
                continue

            try:
                lat = float(lat)
                lon = float(lon)
            except (TypeError, ValueError):
                context.add_warning(
                    self.name,
                    "Invalid coordinate values for FSA lookup"
                )
                results.append(TransformResult.SUCCESS)
                continue

            coords_to_lookup.append((i, lon, lat))
            context_map[i] = context
            results.append(None)  # Placeholder

        # Perform batch lookup
        if coords_to_lookup:
            # Process in batches
            for start in range(0, len(coords_to_lookup), batch_size):
                batch = coords_to_lookup[start:start + batch_size]
                fsa_results = await self.lookup_fsa_batch(batch)

                for idx, fsa_code in fsa_results.items():
                    context = context_map[idx]
                    if fsa_code:
                        lat = context.get_field('latitude')
                        lon = context.get_field('longitude')
                        context.set_field(
                            'fsa_code',
                            fsa_code,
                            self.name,
                            reason=f"Spatial lookup from coordinates ({lat}, {lon})"
                        )
                        results[idx] = TransformResult.MODIFIED
                    else:
                        lat = context.get_field('latitude')
                        lon = context.get_field('longitude')
                        context.add_warning(
                            self.name,
                            f"No FSA found for coordinates ({lat}, {lon})"
                        )
                        results[idx] = TransformResult.SUCCESS

        # Fill in any remaining None values
        for i, result in enumerate(results):
            if result is None:
                results[i] = TransformResult.SUCCESS

        return results
