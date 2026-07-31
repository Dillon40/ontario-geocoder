"""
Municipality mapper transformer.

Maps variant municipality names to canonical forms using aliases from database:
- Pre-amalgamation: "Etobicoke" → "Toronto"
- Bilingual: "Greater Sudbury / Grand Sudbury" → "Greater Sudbury"
- Variations: "City of Toronto" → "Toronto"

Also supports:
- source_field: Read municipality from a custom field (for multi-municipality datasets)
- derive_from_coordinates: Spatial lookup when city is missing (for regional datasets)

Preserves original city name in city_original for audit trail.
"""
import logging
from typing import Any, Dict, Optional

from .base import AsyncTransformer, TransformContext, TransformResult
from .db_retry import retry_async

logger = logging.getLogger(__name__)


def normalize_bilingual_name(name: str) -> str:
    """
    Handle bilingual names with slashes.

    "Greater Sudbury / Grand Sudbury" → "Greater Sudbury"
    """
    if '/' in name:
        parts = name.split('/')
        # Take English name (usually first)
        return parts[0].strip()
    return name


def normalize_city_name(name: str) -> str:
    """
    Basic normalization for city name comparison.

    Removes common prefixes, trims, title cases.
    """
    if not name:
        return ''

    # Handle bilingual names
    name = normalize_bilingual_name(name)

    # Remove common prefixes for comparison
    prefixes = ['City of ', 'Town of ', 'Municipality of ', 'Township of ']
    for prefix in prefixes:
        if name.lower().startswith(prefix.lower()):
            name = name[len(prefix):]
            break

    return name.strip().title()


class MunicipalityMapper(AsyncTransformer):
    """
    Async transformer that maps municipality names to canonical forms.

    Loads aliases from municipality_aliases table at initialization.
    Caches mappings in memory for duration of import.

    Configuration options:
        source_field: Field to read municipality from (default: 'city')
                      Use for multi-municipality datasets with custom field names
        default_city: Apply when city field is empty (e.g., "Ottawa")
        default_province: Apply when province field is empty (e.g., "ON")
        preserve_original: Save original city in city_original (default: True)
        derive_from_coordinates: If True, derive municipality from coordinates
                                 when city is empty (spatial lookup)
    """

    @property
    def name(self) -> str:
        return "MunicipalityMapper"

    def __init__(self, config: Optional[Dict[str, Any]] = None, pool=None):
        super().__init__(config)
        self.source_field = self.config.get('source_field', 'city')
        self.default_city = self.config.get('default_city')
        self.default_province = self.config.get('default_province')
        self.preserve_original = self.config.get('preserve_original', True)
        self.derive_from_coordinates = self.config.get('derive_from_coordinates', False)

        # Database pool for spatial lookups
        self._pool = pool

        # Alias cache: lowercase alias → canonical name
        self._alias_cache: Dict[str, str] = {}
        # Normalized alias cache: normalized lowercase → canonical name
        self._normalized_cache: Dict[str, str] = {}
        self._loaded = False

    def set_pool(self, pool) -> None:
        """Set the database connection pool for spatial lookups."""
        self._pool = pool

    async def load_aliases(self, pool) -> None:
        """
        Load municipality aliases from database.

        Retries on transient connection errors. Failure is fatal
        (exception propagates) since this runs during pipeline init.

        Args:
            pool: asyncpg connection pool
        """
        query = """
            SELECT alias, normalized_alias, canonical_name, alias_type
            FROM municipality_aliases
            WHERE canonical_name IS NOT NULL
        """

        async def _do_load():
            async with pool.acquire() as conn:
                return await conn.fetch(query)

        rows = await retry_async(
            _do_load,
            operation_name="MunicipalityMapper.load_aliases",
        )

        for row in rows:
            alias = row['alias']
            normalized = row['normalized_alias']
            canonical = row['canonical_name']

            if alias:
                self._alias_cache[alias.lower()] = canonical
            if normalized:
                self._normalized_cache[normalized.lower()] = canonical

        self._loaded = True

    def lookup_canonical(self, city: str) -> Optional[str]:
        """
        Look up canonical name for a city.

        Args:
            city: City name to look up

        Returns:
            Canonical name if found, None otherwise
        """
        if not city:
            return None

        lower = city.lower()

        # Direct match
        if lower in self._alias_cache:
            return self._alias_cache[lower]

        # Normalized match
        normalized = normalize_city_name(city).lower()
        if normalized in self._normalized_cache:
            return self._normalized_cache[normalized]

        # Also check if normalized version is in direct cache
        if normalized in self._alias_cache:
            return self._alias_cache[normalized]

        return None

    async def lookup_municipality_from_coords(
        self,
        latitude: float,
        longitude: float
    ) -> Optional[str]:
        """
        Look up municipality from coordinates using spatial query.

        Uses lower_municipal_boundaries table to find which municipality
        contains the given point.

        Args:
            latitude: Latitude coordinate
            longitude: Longitude coordinate

        Returns:
            Municipality name if found, None otherwise
        """
        if not self._pool:
            return None

        # Query uses 'name' column which contains values like "CITY OF TORONTO"
        # We extract and normalize to title case (e.g., "Toronto")
        query = """
            SELECT name as municipality_name
            FROM lower_municipal_boundaries
            WHERE ST_Contains(
                geometry,
                ST_SetSRID(ST_MakePoint($1, $2), 4326)
            )
            LIMIT 1
        """

        async def _do_lookup():
            async with self._pool.acquire() as conn:
                return await conn.fetchrow(query, longitude, latitude)

        try:
            row = await retry_async(
                _do_lookup,
                operation_name="MunicipalityMapper.lookup_municipality_from_coords",
            )
            if row and row['municipality_name']:
                # Normalize: "CITY OF TORONTO" -> "Toronto"
                name = row['municipality_name']
                # Remove common prefixes
                prefixes = ['CITY OF ', 'TOWN OF ', 'MUNICIPALITY OF ',
                            'TOWNSHIP OF ', 'COUNTY OF ']
                for prefix in prefixes:
                    if name.upper().startswith(prefix):
                        name = name[len(prefix):]
                        break
                # Convert to title case
                return name.strip().title()
        except Exception as exc:
            logger.warning(
                "MunicipalityMapper.lookup_municipality_from_coords failed "
                "for (%s, %s): %s: %s",
                latitude, longitude, type(exc).__name__, exc,
            )

        return None

    async def transform_async(self, context: TransformContext) -> TransformResult:
        """Map municipality to canonical form."""
        if not self.should_run(context):
            return context.last_result

        if not self._loaded:
            context.add_warning(
                self.name,
                "Municipality aliases not loaded - call load_aliases() first"
            )

        modified = False

        # Read city from configured source field (supports multi-municipality datasets)
        city = context.get_field(self.source_field)

        # If source_field is different from 'city', copy to city field
        if self.source_field != 'city' and city:
            context.set_field(
                'city',
                city,
                self.name,
                reason=f"Copied from source field '{self.source_field}'"
            )
            modified = True

        # Derive municipality from coordinates if configured and city is missing
        if not city and self.derive_from_coordinates:
            lat = context.get_field('latitude')
            lon = context.get_field('longitude')
            if lat is not None and lon is not None:
                try:
                    derived_city = await self.lookup_municipality_from_coords(
                        float(lat), float(lon)
                    )
                    if derived_city:
                        context.set_field(
                            'city',
                            derived_city,
                            self.name,
                            reason="Derived from coordinates via spatial lookup"
                        )
                        modified = True
                        city = derived_city
                except (ValueError, TypeError):
                    # Invalid coordinates - continue with other methods
                    pass

        # Apply default city if still missing
        if not city and self.default_city:
            context.set_field(
                'city',
                self.default_city,
                self.name,
                reason="Applied default city from config"
            )
            modified = True
            city = self.default_city

        # Apply default province if missing
        province = context.get_field('province')
        if not province and self.default_province:
            context.set_field(
                'province',
                self.default_province,
                self.name,
                reason="Applied default province from config"
            )
            modified = True

        # Look up canonical name
        if city:
            canonical = self.lookup_canonical(city)

            if canonical and canonical != city:
                # Preserve original if configured
                if self.preserve_original:
                    context.set_field(
                        'city_original',
                        city,
                        self.name,
                        reason="Preserving pre-canonical city name"
                    )

                context.set_field(
                    'city',
                    canonical,
                    self.name,
                    reason=f"Mapped to canonical: {city} → {canonical}"
                )
                modified = True
            elif city:
                # Handle bilingual names even if not in alias table
                normalized = normalize_bilingual_name(city)
                if normalized != city:
                    if self.preserve_original:
                        context.set_field(
                            'city_original',
                            city,
                            self.name,
                            reason="Preserving bilingual city name"
                        )
                    context.set_field(
                        'city',
                        normalized,
                        self.name,
                        reason=f"Normalized bilingual: {city} → {normalized}"
                    )
                    modified = True

        return TransformResult.MODIFIED if modified else TransformResult.SUCCESS


class MunicipalityMapperSync:
    """
    Synchronous version for testing without database.

    Use MunicipalityMapper for production with actual DB.
    """

    def __init__(
        self,
        aliases: Optional[Dict[str, str]] = None,
        config: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize with optional pre-loaded aliases.

        Args:
            aliases: Dict of lowercase alias → canonical name
            config: Configuration options
        """
        self.config = config or {}
        self.default_city = self.config.get('default_city')
        self.default_province = self.config.get('default_province')
        self.preserve_original = self.config.get('preserve_original', True)
        self._alias_cache = aliases or {}
        self._normalized_cache: Dict[str, str] = {}

        # Build normalized cache
        for alias, canonical in self._alias_cache.items():
            normalized = normalize_city_name(alias).lower()
            self._normalized_cache[normalized] = canonical

    @property
    def name(self) -> str:
        return "MunicipalityMapper"

    def lookup_canonical(self, city: str) -> Optional[str]:
        """Look up canonical name for a city."""
        if not city:
            return None

        lower = city.lower()

        if lower in self._alias_cache:
            return self._alias_cache[lower]

        normalized = normalize_city_name(city).lower()
        if normalized in self._normalized_cache:
            return self._normalized_cache[normalized]

        return None

    def transform(self, context: TransformContext) -> TransformResult:
        """Map municipality to canonical form (sync version)."""
        modified = False
        city = context.get_field('city')

        # Apply default city if missing
        if not city and self.default_city:
            context.set_field(
                'city',
                self.default_city,
                self.name,
                reason=f"Applied default city from config"
            )
            modified = True
            city = self.default_city

        # Apply default province if missing
        province = context.get_field('province')
        if not province and self.default_province:
            context.set_field(
                'province',
                self.default_province,
                self.name,
                reason=f"Applied default province from config"
            )
            modified = True

        # Look up canonical name
        if city:
            canonical = self.lookup_canonical(city)

            if canonical and canonical != city:
                if self.preserve_original:
                    context.set_field(
                        'city_original',
                        city,
                        self.name,
                        reason="Preserving pre-canonical city name"
                    )

                context.set_field(
                    'city',
                    canonical,
                    self.name,
                    reason=f"Mapped to canonical: {city} → {canonical}"
                )
                modified = True
            elif city:
                # Handle bilingual names
                normalized = normalize_bilingual_name(city)
                if normalized != city:
                    if self.preserve_original:
                        context.set_field(
                            'city_original',
                            city,
                            self.name,
                            reason="Preserving bilingual city name"
                        )
                    context.set_field(
                        'city',
                        normalized,
                        self.name,
                        reason=f"Normalized bilingual: {city} → {normalized}"
                    )
                    modified = True

        return TransformResult.MODIFIED if modified else TransformResult.SUCCESS
