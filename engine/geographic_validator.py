"""
Geographic Validation Service

Provides FSA-municipality validation and enrichment for geocoding queries.

Key Features:
- Municipality name normalization (handles bilingual names, aliases)
- FSA-municipality compatibility validation
- Geographic inference (fill missing FSA from city or vice versa)
- Confidence score adjustments based on geographic alignment

Data Sources:
- fsa_municipality_mappings: 1,850 FSA-municipality relationships
- municipality_aliases: Bilingual names, pre-amalgamation variations
- Spatial analysis based on PostGIS intersections

Usage:
    validator = GeographicValidator()

    # Normalize municipality name
    canonical = await validator.normalize_municipality_name("Greater Sudbury / Grand Sudbury")
    # Returns: "GREATER SUDBURY"

    # Validate FSA-municipality compatibility
    is_valid, confidence_adj = await validator.validate_fsa_municipality("M5V", "Toronto")
    # Returns: (True, 0.05) - compatible, boost confidence

    # Enrich query with missing geography
    enriched = await validator.enrich_query_geography(query)
    # Adds inferred FSA or city if one is missing
"""

from typing import Optional, Dict, Any, List, Tuple
from . import database


class GeographicValidator:
    """Validates and enriches geographic information for geocoding queries"""

    # Confidence adjustments
    CONFIDENCE_BOOST_ALIGNED = 0.05  # FSA and municipality perfectly align
    CONFIDENCE_PENALTY_CONFLICT = -0.10  # FSA and municipality conflict
    CONFIDENCE_BOOST_INFERRED = 0.03  # Successfully inferred missing field from FSA
    CONFIDENCE_BOOST_ZONE_INFERRED = 0.015  # Inferred from zone (less specific than FSA)

    def __init__(self):
        """Initialize geographic validator"""
        self._alias_cache = {}  # Cache for municipality aliases
        self._mapping_cache = {}  # Cache for FSA-municipality mappings

    async def normalize_municipality_name(self, city: Optional[str]) -> Optional[str]:
        """
        Normalize municipality name using aliases table and normalization function.

        Handles:
        - Bilingual names: "Greater Sudbury / Grand Sudbury" → "GREATER SUDBURY"
        - Pre-amalgamation names: "North York" → "TORONTO" (if in aliases)
        - County variations: "Prince Edward County" → "PRINCE EDWARD"
        - Whitespace and case normalization

        Args:
            city: Municipality name to normalize

        Returns:
            Normalized canonical municipality name, or None if input is None/empty
        """
        if not city or not city.strip():
            return None

        # Check cache first
        cache_key = city.upper().strip()
        if cache_key in self._alias_cache:
            return self._alias_cache[cache_key]

        # Query database for alias or normalization
        pool = database.get_pool()
        if not pool:
            raise RuntimeError("Database pool not initialized")

        async with pool.acquire() as conn:
            # First, try to find alias
            alias_result = await conn.fetchrow("""
                SELECT canonical_name
                FROM municipality_aliases
                WHERE normalized_alias = normalize_municipality_name($1)
                LIMIT 1;
            """, city)

            if alias_result:
                canonical = alias_result['canonical_name']
                self._alias_cache[cache_key] = canonical
                return canonical

            # If no alias found, just apply normalization function
            normalized = await conn.fetchval(
                "SELECT normalize_municipality_name($1);", city
            )

            self._alias_cache[cache_key] = normalized
            return normalized

    async def validate_fsa_municipality(
        self,
        fsa_code: Optional[str],
        municipality: Optional[str]
    ) -> Tuple[bool, float]:
        """
        Validate that FSA code and municipality are geographically compatible.

        Uses fsa_municipality_mappings table to check if the FSA-municipality
        combination exists based on spatial intersection analysis.

        Args:
            fsa_code: 3-character FSA code (e.g., "M5V")
            municipality: Municipality name (will be normalized)

        Returns:
            Tuple of (is_valid, confidence_adjustment):
            - (True, 0.05): Compatible, boost confidence
            - (False, -0.10): Conflict detected, penalize confidence
            - (True, 0.00): No validation possible (one field missing)
        """
        if not fsa_code or not municipality:
            # Can't validate if either field is missing
            return True, 0.0

        # Normalize FSA code (uppercase, strip)
        fsa_code = fsa_code.strip().upper()[:3]

        # Normalize municipality name
        normalized_city = await self.normalize_municipality_name(municipality)
        if not normalized_city:
            return True, 0.0

        # Check mapping table
        pool = database.get_pool()
        if not pool:
            raise RuntimeError("Database pool not initialized")

        async with pool.acquire() as conn:
            mapping_exists = await conn.fetchval("""
                SELECT EXISTS(
                    SELECT 1
                    FROM fsa_municipality_mappings
                    WHERE fsa_code = $1
                      AND normalize_municipality_name(municipality_name_short) = $2
                )
            """, fsa_code, normalized_city)

            if mapping_exists:
                # Valid combination - boost confidence
                return True, self.CONFIDENCE_BOOST_ALIGNED
            else:
                # No reference coverage for this FSA is UNKNOWN, not a conflict.
                # Do not cache this absence: a subsequent import may populate it.
                covered = await conn.fetchval(
                    'SELECT EXISTS(SELECT 1 FROM fsa_municipality_mappings WHERE fsa_code = $1)',
                    fsa_code)
                if not covered:
                    return True, 0.0
                return False, self.CONFIDENCE_PENALTY_CONFLICT

    async def get_municipalities_for_fsa(self, fsa_code: str) -> List[Dict[str, Any]]:
        """
        Get all municipalities that intersect with this FSA.

        Returns municipalities ordered by coverage percentage (primary first).

        Args:
            fsa_code: 3-character FSA code

        Returns:
            List of municipality dictionaries with keys:
            - municipal_id: Municipality ID
            - municipality_name: Short name
            - is_primary: True if this is the primary municipality for the FSA
            - coverage_pct: Percentage of FSA area in this municipality
            - address_count: Number of NAR addresses in this FSA+municipality
        """
        if not fsa_code:
            return []

        fsa_code = fsa_code.strip().upper()[:3]

        pool = database.get_pool()
        if not pool:
            raise RuntimeError("Database pool not initialized")

        async with pool.acquire() as conn:
            results = await conn.fetch("""
                SELECT
                    municipal_id,
                    municipality_name_short as municipality_name,
                    municipality_name_full,
                    is_primary,
                    coverage_pct,
                    address_count
                FROM fsa_municipality_mappings
                WHERE fsa_code = $1
                ORDER BY is_primary DESC, coverage_pct DESC;
            """, fsa_code)

            return [dict(row) for row in results]

    async def get_fsas_for_municipality(self, municipality: str) -> List[str]:
        """
        Get all FSA codes that intersect with this municipality.

        Args:
            municipality: Municipality name (will be normalized)

        Returns:
            List of FSA codes (e.g., ["M5V", "M5G", "M5H"])
            Ordered by address count (descending)
        """
        if not municipality:
            return []

        # Normalize municipality name
        normalized_city = await self.normalize_municipality_name(municipality)
        if not normalized_city:
            return []

        pool = database.get_pool()
        if not pool:
            raise RuntimeError("Database pool not initialized")

        async with pool.acquire() as conn:
            results = await conn.fetch("""
                SELECT fsa_code, MAX(address_count) as max_count
                FROM fsa_municipality_mappings
                WHERE normalize_municipality_name(municipality_name_short) = $1
                GROUP BY fsa_code
                ORDER BY max_count DESC NULLS LAST;
            """, normalized_city)

            return [row['fsa_code'] for row in results]

    async def infer_municipality_from_zone(self, postal_zone: str) -> Optional[str]:
        """
        Fallback inference when FSA is unknown but zone maps to single municipality.

        Useful for new FSAs in major metros where an entire postal zone belongs
        to a single municipality (e.g., Toronto M1-M9, Mississauga L5, Hamilton L8).

        This handles the edge case where a new FSA is assigned but not yet in our
        database - we can still infer the municipality from the zone if it's
        a single-municipality zone.

        Args:
            postal_zone: 2-character postal zone (e.g., "M5", "L5")

        Returns:
            Municipality name if zone maps to exactly one municipality, None otherwise
        """
        if not postal_zone or len(postal_zone) != 2:
            return None

        postal_zone = postal_zone.strip().upper()

        # Check cache
        cache_key = f"zone:{postal_zone}"
        if cache_key in self._mapping_cache:
            return self._mapping_cache[cache_key]

        pool = database.get_pool()
        if not pool:
            raise RuntimeError("Database pool not initialized")

        async with pool.acquire() as conn:
            # Find distinct municipalities for this zone (primary mappings only)
            results = await conn.fetch("""
                SELECT DISTINCT municipality_name_short
                FROM fsa_municipality_mappings
                WHERE fsa_code LIKE $1 || '%'
                  AND is_primary = true
            """, postal_zone)

            # Only return if zone maps to exactly one municipality
            if len(results) == 1:
                municipality = results[0]['municipality_name_short']
                self._mapping_cache[cache_key] = municipality
                return municipality

            # Cache negative result too
            self._mapping_cache[cache_key] = None
            return None

    async def enrich_query_geography(
        self,
        query: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Enrich query with inferred geographic information.

        If query has FSA but no city, infer possible cities.
        If query has city but no FSA, infer possible FSAs.
        Validates compatibility if both are present.

        Args:
            query: Query dictionary with address components

        Returns:
            Enriched query dictionary with additional keys:
            - normalized_city: Canonical municipality name
            - possible_cities: List of municipalities if inferred from FSA
            - possible_fsas: List of FSAs if inferred from city
            - geography_valid: Boolean indicating FSA-city compatibility
            - geography_confidence_adjustment: Confidence adjustment value
            - geography_notes: Human-readable validation notes
        """
        enriched = query.copy()
        notes = []

        # Extract geography fields
        fsa_code = query.get('fsa_code') or (query.get('postal_code', '')[:3] if query.get('postal_code') else None)
        city = query.get('city')

        # Normalize city if present
        if city:
            normalized = await self.normalize_municipality_name(city)
            if normalized:
                enriched['normalized_city'] = normalized
                if normalized != city.upper():
                    notes.append(f"Normalized city '{city}' → '{normalized}'")

        # Case 1: Has FSA but no city - infer possible cities
        if fsa_code and not city:
            municipalities = await self.get_municipalities_for_fsa(fsa_code)
            if municipalities:
                enriched['possible_cities'] = [m['municipality_name'] for m in municipalities]
                if len(municipalities) == 1:
                    # Only one possibility - high confidence inference
                    enriched['normalized_city'] = municipalities[0]['municipality_name']
                    enriched['geography_confidence_adjustment'] = self.CONFIDENCE_BOOST_INFERRED
                    notes.append(f"Inferred city '{municipalities[0]['municipality_name']}' from FSA '{fsa_code}'")
                else:
                    # Multiple possibilities - list them
                    notes.append(f"FSA '{fsa_code}' spans {len(municipalities)} municipalities")
            else:
                # FSA not found - try zone-based fallback for unknown FSAs
                # This handles new FSAs in major metros (Toronto M*, Mississauga L5, etc.)
                postal_zone = fsa_code[:2] if len(fsa_code) >= 2 else None
                if postal_zone:
                    zone_inferred = await self.infer_municipality_from_zone(postal_zone)
                    if zone_inferred:
                        enriched['normalized_city'] = zone_inferred
                        # Lower confidence than FSA inference since zone is less specific
                        enriched['geography_confidence_adjustment'] = self.CONFIDENCE_BOOST_ZONE_INFERRED
                        notes.append(f"Inferred city '{zone_inferred}' from zone '{postal_zone}' (unknown FSA '{fsa_code}')")
                    else:
                        notes.append(f"Unknown FSA '{fsa_code}' and zone '{postal_zone}' spans multiple municipalities")

        # Case 2: Has city but no FSA - infer possible FSAs
        elif city and not fsa_code:
            normalized_city = enriched.get('normalized_city')
            if normalized_city:
                fsas = await self.get_fsas_for_municipality(normalized_city)
                if fsas:
                    enriched['possible_fsas'] = fsas
                    notes.append(f"City '{normalized_city}' contains {len(fsas)} FSAs")

        # Case 3: Has both FSA and city - validate compatibility
        elif fsa_code and city:
            is_valid, confidence_adj = await self.validate_fsa_municipality(fsa_code, city)
            enriched['geography_valid'] = is_valid
            enriched['geography_confidence_adjustment'] = confidence_adj

            if is_valid and confidence_adj > 0:
                notes.append(f"FSA '{fsa_code}' and city '{city}' are geographically aligned")
            elif not is_valid:
                notes.append(f"⚠ WARNING: FSA '{fsa_code}' and city '{city}' do not match geographically")

        # Add notes to enriched query
        if notes:
            enriched['geography_notes'] = notes

        return enriched

    async def get_primary_municipality_for_fsa(self, fsa_code: str) -> Optional[str]:
        """
        Get the primary (largest coverage) municipality for an FSA.

        Args:
            fsa_code: 3-character FSA code

        Returns:
            Primary municipality name, or None if FSA not found
        """
        municipalities = await self.get_municipalities_for_fsa(fsa_code)
        if municipalities:
            # First result is primary (ordered by is_primary DESC)
            return municipalities[0]['municipality_name']
        return None

    async def filter_by_geography(
        self,
        fsa_code: Optional[str] = None,
        municipality: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get filter criteria for database queries based on geography.

        Useful for pre-filtering large result sets in geocoding queries.

        Args:
            fsa_code: FSA code to filter by
            municipality: Municipality to filter by (will be normalized)

        Returns:
            Dictionary with filter criteria:
            - fsa_codes: List of valid FSA codes
            - municipalities: List of valid municipality names
            - should_filter: Boolean indicating if filtering is recommended
        """
        result = {
            'fsa_codes': [],
            'municipalities': [],
            'should_filter': False
        }

        if fsa_code:
            # If FSA provided, use it directly
            result['fsa_codes'] = [fsa_code.strip().upper()[:3]]
            result['should_filter'] = True

            # Also get valid municipalities for this FSA
            munis = await self.get_municipalities_for_fsa(fsa_code)
            result['municipalities'] = [m['municipality_name'] for m in munis]

        elif municipality:
            # If only municipality provided, get valid FSAs
            normalized_city = await self.normalize_municipality_name(municipality)
            if normalized_city:
                result['municipalities'] = [normalized_city]
                result['fsa_codes'] = await self.get_fsas_for_municipality(normalized_city)
                result['should_filter'] = True

        return result


# Global instance (lazy-initialized)
_validator_instance = None


async def get_validator() -> GeographicValidator:
    """Get global GeographicValidator instance"""
    global _validator_instance
    if _validator_instance is None:
        _validator_instance = GeographicValidator()
    return _validator_instance
