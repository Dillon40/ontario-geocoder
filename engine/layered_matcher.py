"""
Layered address matching module with progressive fallback strategy.

Key Principles:
1. Exact match first - when user provides perfect data, match it exactly
2. Never make arbitrary choices - multiple ambiguous results = proceed to next layer
3. Cache results forward for efficiency
4. Geographic fields used for scoring/disambiguation, not just filtering
5. Track which layer matched and why we proceeded through layers

Layers:
- Layer 0: Exact match (all provided fields must match)
- Layer 1: Postal + core identity (civic + street)
- Layer 2: FSA + core identity + city
- Layer 3: Postal zone (2 chars) + core identity
- Layer 4: City-only + core identity
- Layer 5: Fuzzy street (postal zone constrained)
- Layer 6: Fuzzy street (city constrained)

Confidence decreases with each layer, reflecting reduced certainty.
"""
import math
import re
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field
from enum import Enum
from . import config, database
from .address_parser import normalize_street_suffix, normalize_direction
from .address_formatter import format_property_address
from .confidence import MUNICIPAL_LAYER_CONFIDENCE, NAR_LAYER_CONFIDENCE


async def query_candidates(query, params, components):
    """Apply non-relaxable identity constraints before SQL LIMIT in every layer.

    Postal/city constraints may be relaxed, but an explicitly opposite street
    direction must never win just because it is the only remaining candidate.
    Soft-deleted rows are never candidates (including civic-suffix fallbacks).
    """
    nar = 'FROM nar_addresses' in query
    prefix = 'a.' if nar else ''
    conditions = [f'NOT {prefix}is_deleted']
    params = list(params)
    direction = components.get('street_direction_normalized') or components.get('street_direction')
    if direction:
        canonical = normalize_direction(direction)
        from .address_parser import direction_mappings
        variants = {canonical.upper(), str(direction).upper().rstrip('.')}
        variants.update(k.upper() for k, v in direction_mappings['abbreviations'].items()
                        if normalize_direction(v) == canonical)
        column = 'a.official_street_dir' if nar else 'street_direction'
        params.append(sorted(variants))
        conditions.append(f"({column} IS NULL OR btrim({column}) = '' OR "
                          f"UPPER(REPLACE({column}, '.', '')) = ANY(${len(params)}::text[]))")
    query = re.sub(r'\bWHERE\b', 'WHERE ' + ' AND '.join(conditions) + ' AND', query, count=1)
    return await database.query(query, params)


class LayerResult(Enum):
    """Possible outcomes from a layer query."""
    NO_MATCH = "no_match"
    SINGLE_MATCH = "single_match"
    DISAMBIGUATED = "disambiguated"
    AMBIGUOUS = "ambiguous"


@dataclass
class LayerAttempt:
    """Record of a single layer attempt."""
    layer: int
    layer_name: str
    result: LayerResult
    candidates: int = 0
    reason: str = ""


@dataclass
class MatchMetadata:
    """Metadata tracking the matching journey."""
    matched_layer: int = -1
    matched_layer_name: str = ""
    layers_tried: List[LayerAttempt] = field(default_factory=list)
    disambiguation_used: bool = False
    disambiguation_margin: int = 0
    civic_suffix_fallback: bool = False  # True when matched via suffix variant (e.g., "1" -> "1A")


# Backward-compatible name for municipal callers/tests. NAR has a separate
# scale; all strategy tables and final tier ceilings live in confidence.py.
LAYER_CONFIDENCE = MUNICIPAL_LAYER_CONFIDENCE

# Minimum score gap required to declare a clear winner in disambiguation
DISAMBIGUATION_MARGIN = 10

# When scoring can't separate the leading candidates, they may still be the same
# physical place recorded more than once: a source dataset can hold several rows
# for one civic address (e.g. a condo tower whose units share a street number,
# or the same address arriving in two files), each geocoded independently so the
# coordinates differ by a few metres rather than being bit-identical. If every
# leading candidate lies within this radius (metres) of the others, any of them
# returns the right building, so we take the top-scored row instead of dropping
# to a coarser tier.
#
# Chosen at building/parcel scale. The observed duplicate spread is bimodal: a
# tight cluster of geocoding noise (a few metres) and a long tail of genuinely
# different places that happen to share a civic+street+city string across an
# amalgamated municipality (hundreds of metres to kilometres). This radius
# recovers the former as Tier-2 matches while leaving the latter ambiguous, and
# bounds the worst-case position error it can introduce to its own value.
SAME_LOCATION_RADIUS_M = 50.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in metres."""
    radius = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _rows_colocated(rows: List[Any], table_type: str) -> bool:
    """True if every row has coordinates and all pairwise distances are within
    SAME_LOCATION_RADIUS_M — i.e. the rows denote one physical location. Any
    missing coordinate returns False (we can't prove co-location, so don't)."""
    coords = [_row_coordinates(row, table_type) for row in rows]
    if len(coords) < 2 or any(c is None for c in coords):
        return False
    for i in range(len(coords)):
        for j in range(i + 1, len(coords)):
            if _haversine_m(coords[i][0], coords[i][1], coords[j][0], coords[j][1]) > SAME_LOCATION_RADIUS_M:
                return False
    return True


def _row_coordinates(row: Any, table_type: str) -> Optional[Tuple[float, float]]:
    """Extract (lat, lon) from a row, rounded so float noise doesn't split
    identical locations. Returns None if either coordinate is missing."""
    if table_type == 'properties':
        lat, lon = row.get('latitude'), row.get('longitude')
    else:  # NAR
        lat, lon = row.get('bg_latitude'), row.get('bg_longitude')

    if lat is None or lon is None:
        return None

    return (round(float(lat), 6), round(float(lon), 6))


def disambiguate(results: List[Any], input_components: Dict[str, Any], table_type: str = 'properties') -> Tuple[Optional[Any], bool, int]:
    """
    Attempt to disambiguate multiple results using secondary fields.

    Returns (best_row, was_disambiguated, score_margin) where:
    - best_row: The winning row, or None if still ambiguous
    - was_disambiguated: True if we found a clear winner
    - score_margin: The gap between best and second-best scores

    A clear winner requires score_margin > DISAMBIGUATION_MARGIN.
    """
    if not results:
        return None, False, 0

    if len(results) == 1:
        return results[0], False, 100  # Single result, no disambiguation needed

    scores = []
    for row in results:
        score = 0

        # Direction match - highly discriminating (30 points)
        input_dir = input_components.get('street_direction_normalized') or input_components.get('street_direction')
        if input_dir:
            if table_type == 'properties':
                row_dir = row.get('street_direction')
            else:  # NAR
                row_dir = normalize_direction(row.get('official_street_dir'))

            # Normalize both sides to handle abbreviation mismatches
            if row_dir:
                norm_input_dir = normalize_direction(input_dir)
                norm_row_dir = normalize_direction(row_dir)
                if norm_input_dir and norm_row_dir and norm_input_dir.upper() == norm_row_dir.upper():
                    score += 30

        # Street suffix match - moderately discriminating (20 points)
        input_suffix = input_components.get('street_suffix_normalized') or input_components.get('street_suffix')
        if input_suffix:
            if table_type == 'properties':
                row_suffix = row.get('street_suffix')
            else:  # NAR
                row_suffix = normalize_street_suffix(row.get('official_street_type'))

            # Normalize both sides to handle abbreviation mismatches
            if row_suffix:
                norm_input_suffix = normalize_street_suffix(input_suffix)
                norm_row_suffix = normalize_street_suffix(row_suffix)
                if norm_input_suffix and norm_row_suffix and norm_input_suffix.upper() == norm_row_suffix.upper():
                    score += 20

        # Unit number match - can help disambiguate apartments (15 points)
        input_unit = input_components.get('unit_number') or input_components.get('unit_no')
        if input_unit:
            if table_type == 'properties':
                row_unit = row.get('unit_no')
            else:  # NAR: apartment/unit is distinct from the civic-number suffix
                row_unit = row.get('apt_no_label')

            if row_unit and str(input_unit).upper() == str(row_unit).upper():
                score += 15

        # Civic number suffix match ("68A") - decisive between 68 and 68A (25 points).
        # Column is civic_no_suffix in both properties and nar_addresses.
        input_civic_suffix = input_components.get('civic_no_suffix')
        if input_civic_suffix:
            row_civic = row.get('civic_no_suffix')
            if row_civic and str(input_civic_suffix).upper() == str(row_civic).upper():
                score += 25

        # Postal code exact match - bonus for full postal match (10 points)
        input_postal = input_components.get('postal_code')
        if input_postal:
            if table_type == 'properties':
                row_postal = row.get('postal_code')
            else:  # NAR
                row_postal = row.get('mail_postal_code')

            if row_postal and input_postal.upper() == row_postal.upper():
                score += 10

        # FSA match - highly discriminating geographic disambiguation (25 points)
        # Uses pre-populated fsa_code column from ETL enrichment
        if input_postal and len(input_postal) >= 3:
            input_fsa = input_postal[:3].upper()
            if table_type == 'properties':
                row_fsa = row.get('fsa_code')
            else:  # NAR - use mail_postal_code's FSA
                row_mail_postal = row.get('mail_postal_code')
                row_fsa = row_mail_postal[:3].upper() if row_mail_postal and len(row_mail_postal) >= 3 else None

            if row_fsa and input_fsa == row_fsa.upper():
                score += 25

        scores.append((score, row))

    # Sort by score descending
    scores.sort(key=lambda x: x[0], reverse=True)

    best_score = scores[0][0]
    second_score = scores[1][0] if len(scores) > 1 else 0
    margin = best_score - second_score

    # Only return a winner if there's a clear margin
    if margin > DISAMBIGUATION_MARGIN:
        return scores[0][1], True, margin

    # No clear scoring winner. Before dropping to a coarser tier, check whether
    # the leading candidates — those scoring can't cleanly separate — are all
    # the SAME physical location. A source dataset often holds several rows for
    # one civic address (condo units without unit numbers, or the same address
    # from two files), each geocoded with a few metres of noise. If every
    # contender sits within SAME_LOCATION_RADIUS_M of the others, any of them
    # returns the right building, so we take the top-scored row.
    #
    # "Contenders" are the rows within DISAMBIGUATION_MARGIN of the best score:
    # the ones that couldn't be clearly beaten. A genuinely different place that
    # shares the civic+street+city string sits far outside the radius, so this
    # stays ambiguous and proceeds to the next layer rather than silently
    # returning wrong coordinates.
    if best_score > 0:
        contenders = [row for score, row in scores if score >= best_score - DISAMBIGUATION_MARGIN]
        if _rows_colocated(contenders, table_type):
            return scores[0][1], True, margin

    # Still ambiguous (all scores 0, or contenders spread beyond the radius)
    return None, False, margin


def build_result_from_properties_row(row: Any, layer: int, metadata: MatchMetadata) -> Dict[str, Any]:
    """Build result object from properties database row."""
    return {
        'latitude': float(row['latitude']),
        'longitude': float(row['longitude']),
        'matched_address': format_property_address(row),
        'source': f"municipal_{row['source_dataset']}" if row.get('source_dataset') else 'municipal_match',
        'confidence': LAYER_CONFIDENCE.get(layer, 0.5),
        'match_tier': 2,
        'match_layer': layer,
        'match_method': metadata.matched_layer_name,
        'property_id': row.get('property_id'),
        'assessment_roll_number': row.get('assessment_roll_number'),
        'pin': row.get('pin'),
        'gpid': row.get('gpid'),
        'match_metadata': {
            'matched_layer': metadata.matched_layer,
            'matched_layer_name': metadata.matched_layer_name,
            'layers_tried': [
                {
                    'layer': a.layer,
                    'name': a.layer_name,
                    'result': a.result.value,
                    'candidates': a.candidates,
                    'reason': a.reason
                }
                for a in metadata.layers_tried
            ],
            'disambiguation_used': metadata.disambiguation_used,
            'disambiguation_margin': metadata.disambiguation_margin,
            'civic_suffix_fallback': metadata.civic_suffix_fallback
        }
    }


def build_result_from_nar_row(row: Any, layer: int, metadata: MatchMetadata) -> Dict[str, Any]:
    """Build result object from NAR database row."""
    # Construct full address
    street_type = normalize_street_suffix(row.get('official_street_type')) or row.get('official_street_type') or ''
    direction = normalize_direction(row.get('official_street_dir')) or row.get('official_street_dir') or ''

    address_parts = [
        row.get('civic_no'),
        row.get('civic_no_suffix'),
        row.get('official_street_name'),
        street_type,
        direction
    ]
    address_parts = [p for p in address_parts if p]
    address_str = ' '.join(address_parts)

    full_address = f"{address_str}, {row.get('csd_eng_name', '')}, ON {row.get('mail_postal_code', '')}"

    return {
        'latitude': float(row['bg_latitude']),
        'longitude': float(row['bg_longitude']),
        'matched_address': full_address.strip(),
        'source': 'nar_match',
        'confidence': NAR_LAYER_CONFIDENCE.get(layer, 0.5),
        'match_tier': 3,
        'match_layer': layer,
        'match_method': metadata.matched_layer_name,
        'property_id': None,
        'assessment_roll_number': None,
        'match_metadata': {
            'matched_layer': metadata.matched_layer,
            'matched_layer_name': metadata.matched_layer_name,
            'layers_tried': [
                {
                    'layer': a.layer,
                    'name': a.layer_name,
                    'result': a.result.value,
                    'candidates': a.candidates,
                    'reason': a.reason
                }
                for a in metadata.layers_tried
            ],
            'disambiguation_used': metadata.disambiguation_used,
            'disambiguation_margin': metadata.disambiguation_margin,
            'civic_suffix_fallback': metadata.civic_suffix_fallback
        }
    }


class LayeredMatcher:
    """
    Layered address matcher for municipal (properties) data.

    Implements progressive matching with strict disambiguation rules.
    """

    def __init__(self):
        self.metadata = MatchMetadata()

    async def match(self, address_components: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Attempt to match address using layered approach.

        Args:
            address_components: Parsed address components

        Returns:
            Geocoding result or None
        """
        self.metadata = MatchMetadata()

        # Extract commonly used fields
        postal_code = address_components.get('postal_code')
        fsa_code = postal_code[:3].upper() if postal_code and len(postal_code) >= 3 else None
        postal_zone = postal_code[:2].upper() if postal_code and len(postal_code) >= 2 else None
        street_number = address_components.get('street_number')
        street_name = address_components.get('street_name')
        normalized_city = address_components.get('normalized_city') or address_components.get('city')

        # Must have core identity fields
        if not street_number or not street_name:
            return None

        # Layer 0: Exact match (all provided fields)
        result = await self._layer_0_exact(address_components, postal_code, street_number, street_name, normalized_city)
        if result:
            return result

        # Layer 1: Postal + core identity
        if postal_code:
            result = await self._layer_1_postal_core(address_components, postal_code, street_number, street_name)
            if result:
                return result

        # Layer 2: FSA + core identity + city
        if fsa_code and normalized_city:
            result = await self._layer_2_fsa_core_city(address_components, fsa_code, street_number, street_name, normalized_city)
            if result:
                return result

        # Layer 3: Postal zone + core identity
        if postal_zone:
            result = await self._layer_3_postal_zone_core(address_components, postal_zone, street_number, street_name)
            if result:
                return result

        # Layer 4: City + core identity (no postal constraint)
        if normalized_city:
            result = await self._layer_4_city_core(address_components, street_number, street_name, normalized_city)
            if result:
                return result

        # Layer 5: Fuzzy street (postal zone constrained)
        if postal_zone:
            result = await self._layer_5_fuzzy_postal_zone(address_components, postal_zone, street_number, street_name)
            if result:
                return result

        # Layer 6: Fuzzy street (city constrained)
        if normalized_city:
            result = await self._layer_6_fuzzy_city(address_components, street_number, street_name, normalized_city)
            if result:
                return result

        return None

    async def _layer_0_exact(self, components: Dict, postal_code: str, street_number: str,
                             street_name: str, city: str) -> Optional[Dict[str, Any]]:
        """Layer 0: Exact match on all provided fields."""
        layer_name = "exact_all_fields"

        if not postal_code:
            self.metadata.layers_tried.append(LayerAttempt(
                layer=0, layer_name=layer_name, result=LayerResult.NO_MATCH,
                reason="Missing postal code for exact match"
            ))
            return None

        try:
            # Build query with ALL provided fields
            query = '''
                SELECT * FROM properties
                WHERE postal_code = $1
                    AND UPPER(street_number) = UPPER($2)
                    AND UPPER(street_name) = UPPER($3)
            '''
            params = [postal_code, street_number, street_name]

            # Add suffix if provided (must match exactly)
            suffix = components.get('street_suffix_normalized') or components.get('street_suffix')
            if suffix:
                query += f' AND UPPER(street_suffix) = UPPER(${len(params) + 1})'
                params.append(suffix)

            # Add direction if provided (must match exactly)
            direction = components.get('street_direction_normalized') or components.get('street_direction')
            if direction:
                query += f' AND UPPER(street_direction) = UPPER(${len(params) + 1})'
                params.append(direction)

            # Add city if provided
            if city:
                query += f'''
                    AND (
                        normalize_municipality_name(city) = normalize_municipality_name(${len(params) + 1})
                        OR EXISTS (
                            SELECT 1 FROM municipality_aliases ma
                            WHERE UPPER(ma.canonical_name) = normalize_municipality_name(${len(params) + 1})
                            AND normalize_municipality_name(city) = ma.normalized_alias
                        )
                    )
                '''
                params.append(city)

            # Deterministic ordering: without it, LIMIT can drop candidates arbitrarily
            query += ' ORDER BY id LIMIT 10'

            results = await query_candidates(query, params, components)

            if not results:
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=0, layer_name=layer_name, result=LayerResult.NO_MATCH,
                    reason="No exact matches found"
                ))
                return None

            if len(results) == 1:
                self.metadata.matched_layer = 0
                self.metadata.matched_layer_name = layer_name
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=0, layer_name=layer_name, result=LayerResult.SINGLE_MATCH,
                    candidates=1
                ))
                return build_result_from_properties_row(results[0], 0, self.metadata)

            # Multiple results - shouldn't happen with exact match, but try disambiguation
            best_row, was_disambiguated, margin = disambiguate(results, components, 'properties')

            if best_row and was_disambiguated:
                self.metadata.matched_layer = 0
                self.metadata.matched_layer_name = layer_name
                self.metadata.disambiguation_used = True
                self.metadata.disambiguation_margin = margin
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=0, layer_name=layer_name, result=LayerResult.DISAMBIGUATED,
                    candidates=len(results), reason=f"Disambiguated with margin {margin}"
                ))
                return build_result_from_properties_row(best_row, 0, self.metadata)

            # Ambiguous - proceed to next layer
            self.metadata.layers_tried.append(LayerAttempt(
                layer=0, layer_name=layer_name, result=LayerResult.AMBIGUOUS,
                candidates=len(results), reason=f"Multiple matches, margin only {margin}"
            ))
            return None

        except Exception as error:
            self.metadata.layers_tried.append(LayerAttempt(
                layer=0, layer_name=layer_name, result=LayerResult.NO_MATCH,
                reason=f"Error: {str(error)}"
            ))
            return None

    async def _layer_1_postal_core(self, components: Dict, postal_code: str,
                                    street_number: str, street_name: str) -> Optional[Dict[str, Any]]:
        """Layer 1: Postal code + street number + street name."""
        layer_name = "postal_core"

        try:
            query = '''
                SELECT * FROM properties
                WHERE postal_code = $1
                    AND UPPER(street_number) = UPPER($2)
                    AND UPPER(street_name) = UPPER($3)
                ORDER BY id
                LIMIT 10
            '''
            params = [postal_code, street_number, street_name]

            results = await query_candidates(query, params, components)

            if not results:
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=1, layer_name=layer_name, result=LayerResult.NO_MATCH,
                    reason="No matches found"
                ))
                return None

            if len(results) == 1:
                self.metadata.matched_layer = 1
                self.metadata.matched_layer_name = layer_name
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=1, layer_name=layer_name, result=LayerResult.SINGLE_MATCH,
                    candidates=1
                ))
                return build_result_from_properties_row(results[0], 1, self.metadata)

            # Multiple results - try disambiguation
            best_row, was_disambiguated, margin = disambiguate(results, components, 'properties')

            if best_row and was_disambiguated:
                self.metadata.matched_layer = 1
                self.metadata.matched_layer_name = layer_name
                self.metadata.disambiguation_used = True
                self.metadata.disambiguation_margin = margin
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=1, layer_name=layer_name, result=LayerResult.DISAMBIGUATED,
                    candidates=len(results), reason=f"Disambiguated with margin {margin}"
                ))
                return build_result_from_properties_row(best_row, 1, self.metadata)

            self.metadata.layers_tried.append(LayerAttempt(
                layer=1, layer_name=layer_name, result=LayerResult.AMBIGUOUS,
                candidates=len(results), reason=f"Multiple matches, margin only {margin}"
            ))
            return None

        except Exception as error:
            self.metadata.layers_tried.append(LayerAttempt(
                layer=1, layer_name=layer_name, result=LayerResult.NO_MATCH,
                reason=f"Error: {str(error)}"
            ))
            return None

    async def _layer_2_fsa_core_city(self, components: Dict, fsa_code: str,
                                      street_number: str, street_name: str, city: str) -> Optional[Dict[str, Any]]:
        """Layer 2: FSA + street number + street name + city."""
        layer_name = "fsa_core_city"

        try:
            query = '''
                SELECT * FROM properties
                WHERE fsa_code = $1
                    AND UPPER(street_number) = UPPER($2)
                    AND UPPER(street_name) = UPPER($3)
                    AND (
                        normalize_municipality_name(city) = normalize_municipality_name($4)
                        OR EXISTS (
                            SELECT 1 FROM municipality_aliases ma
                            WHERE UPPER(ma.canonical_name) = normalize_municipality_name($4)
                            AND normalize_municipality_name(city) = ma.normalized_alias
                        )
                    )
                ORDER BY id
                LIMIT 10
            '''
            params = [fsa_code, street_number, street_name, city]

            results = await query_candidates(query, params, components)

            if not results:
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=2, layer_name=layer_name, result=LayerResult.NO_MATCH,
                    reason="No matches found"
                ))
                return None

            if len(results) == 1:
                self.metadata.matched_layer = 2
                self.metadata.matched_layer_name = layer_name
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=2, layer_name=layer_name, result=LayerResult.SINGLE_MATCH,
                    candidates=1
                ))
                return build_result_from_properties_row(results[0], 2, self.metadata)

            best_row, was_disambiguated, margin = disambiguate(results, components, 'properties')

            if best_row and was_disambiguated:
                self.metadata.matched_layer = 2
                self.metadata.matched_layer_name = layer_name
                self.metadata.disambiguation_used = True
                self.metadata.disambiguation_margin = margin
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=2, layer_name=layer_name, result=LayerResult.DISAMBIGUATED,
                    candidates=len(results), reason=f"Disambiguated with margin {margin}"
                ))
                return build_result_from_properties_row(best_row, 2, self.metadata)

            self.metadata.layers_tried.append(LayerAttempt(
                layer=2, layer_name=layer_name, result=LayerResult.AMBIGUOUS,
                candidates=len(results), reason=f"Multiple matches, margin only {margin}"
            ))
            return None

        except Exception as error:
            self.metadata.layers_tried.append(LayerAttempt(
                layer=2, layer_name=layer_name, result=LayerResult.NO_MATCH,
                reason=f"Error: {str(error)}"
            ))
            return None

    async def _layer_3_postal_zone_core(self, components: Dict, postal_zone: str,
                                         street_number: str, street_name: str) -> Optional[Dict[str, Any]]:
        """Layer 3: Postal zone (first 2 chars) + street number + street name."""
        layer_name = "postal_zone_core"

        try:
            # Prefer rows in the input's full FSA (best-quality candidates first),
            # then id for a deterministic order under LIMIT.
            postal_code = components.get('postal_code')
            input_fsa = postal_code[:3].upper() if postal_code and len(postal_code) >= 3 else None

            query = '''
                SELECT * FROM properties
                WHERE fsa_code LIKE $1
                    AND UPPER(street_number) = UPPER($2)
                    AND UPPER(street_name) = UPPER($3)
                ORDER BY (fsa_code = $4) DESC NULLS LAST, id
                LIMIT 10
            '''
            params = [f"{postal_zone}%", street_number, street_name, input_fsa]

            results = await query_candidates(query, params, components)

            if not results:
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=3, layer_name=layer_name, result=LayerResult.NO_MATCH,
                    reason="No matches found"
                ))
                return None

            if len(results) == 1:
                self.metadata.matched_layer = 3
                self.metadata.matched_layer_name = layer_name
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=3, layer_name=layer_name, result=LayerResult.SINGLE_MATCH,
                    candidates=1
                ))
                return build_result_from_properties_row(results[0], 3, self.metadata)

            best_row, was_disambiguated, margin = disambiguate(results, components, 'properties')

            if best_row and was_disambiguated:
                self.metadata.matched_layer = 3
                self.metadata.matched_layer_name = layer_name
                self.metadata.disambiguation_used = True
                self.metadata.disambiguation_margin = margin
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=3, layer_name=layer_name, result=LayerResult.DISAMBIGUATED,
                    candidates=len(results), reason=f"Disambiguated with margin {margin}"
                ))
                return build_result_from_properties_row(best_row, 3, self.metadata)

            self.metadata.layers_tried.append(LayerAttempt(
                layer=3, layer_name=layer_name, result=LayerResult.AMBIGUOUS,
                candidates=len(results), reason=f"Multiple matches, margin only {margin}"
            ))
            return None

        except Exception as error:
            self.metadata.layers_tried.append(LayerAttempt(
                layer=3, layer_name=layer_name, result=LayerResult.NO_MATCH,
                reason=f"Error: {str(error)}"
            ))
            return None

    async def _layer_4_city_core(self, components: Dict, street_number: str,
                                  street_name: str, city: str) -> Optional[Dict[str, Any]]:
        """Layer 4: City + street number + street name (no postal constraint)."""
        layer_name = "city_core"

        try:
            query = '''
                SELECT * FROM properties
                WHERE UPPER(street_number) = UPPER($1)
                    AND UPPER(street_name) = UPPER($2)
                    AND (
                        normalize_municipality_name(city) = normalize_municipality_name($3)
                        OR EXISTS (
                            SELECT 1 FROM municipality_aliases ma
                            WHERE UPPER(ma.canonical_name) = normalize_municipality_name($3)
                            AND normalize_municipality_name(city) = ma.normalized_alias
                        )
                    )
                ORDER BY id
                LIMIT 10
            '''
            params = [street_number, street_name, city]

            results = await query_candidates(query, params, components)

            # Civic suffix fallback: if no results and input has no suffix, try matching with suffix variants
            if not results:
                input_civic_suffix = components.get('civic_no_suffix')
                if not input_civic_suffix:
                    # Try fallback query with any civic_no_suffix
                    fallback_query = '''
                        SELECT * FROM properties
                        WHERE UPPER(street_number) = UPPER($1)
                            AND civic_no_suffix IS NOT NULL AND civic_no_suffix != ''
                            AND UPPER(street_name) = UPPER($2)
                            AND (
                                normalize_municipality_name(city) = normalize_municipality_name($3)
                                OR EXISTS (
                                    SELECT 1 FROM municipality_aliases ma
                                    WHERE UPPER(ma.canonical_name) = normalize_municipality_name($3)
                                    AND normalize_municipality_name(city) = ma.normalized_alias
                                )
                            )
                        ORDER BY civic_no_suffix, id
                        LIMIT 10
                    '''
                    results = await query_candidates(fallback_query, params, components)
                    if results:
                        self.metadata.civic_suffix_fallback = True

            if not results:
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=4, layer_name=layer_name, result=LayerResult.NO_MATCH,
                    reason="No matches found"
                ))
                return None

            if len(results) == 1:
                self.metadata.matched_layer = 4
                self.metadata.matched_layer_name = layer_name
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=4, layer_name=layer_name, result=LayerResult.SINGLE_MATCH,
                    candidates=1
                ))
                return build_result_from_properties_row(results[0], 4, self.metadata)

            best_row, was_disambiguated, margin = disambiguate(results, components, 'properties')

            if best_row and was_disambiguated:
                self.metadata.matched_layer = 4
                self.metadata.matched_layer_name = layer_name
                self.metadata.disambiguation_used = True
                self.metadata.disambiguation_margin = margin
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=4, layer_name=layer_name, result=LayerResult.DISAMBIGUATED,
                    candidates=len(results), reason=f"Disambiguated with margin {margin}"
                ))
                return build_result_from_properties_row(best_row, 4, self.metadata)

            self.metadata.layers_tried.append(LayerAttempt(
                layer=4, layer_name=layer_name, result=LayerResult.AMBIGUOUS,
                candidates=len(results), reason=f"Multiple matches, margin only {margin}"
            ))
            return None

        except Exception as error:
            self.metadata.layers_tried.append(LayerAttempt(
                layer=4, layer_name=layer_name, result=LayerResult.NO_MATCH,
                reason=f"Error: {str(error)}"
            ))
            return None

    async def _layer_5_fuzzy_postal_zone(self, components: Dict, postal_zone: str,
                                          street_number: str, street_name: str) -> Optional[Dict[str, Any]]:
        """Layer 5: Fuzzy street name matching within postal zone."""
        layer_name = "fuzzy_postal_zone"

        try:
            query = '''
                SELECT *,
                       similarity(UPPER(street_name), UPPER($3)) as street_similarity
                FROM properties
                WHERE fsa_code LIKE $1
                    AND UPPER(street_number) = UPPER($2)
                    AND similarity(UPPER(street_name), UPPER($3)) > 0.6
                ORDER BY similarity(UPPER(street_name), UPPER($3)) DESC, id
                LIMIT 10
            '''
            params = [f"{postal_zone}%", street_number, street_name]

            results = await query_candidates(query, params, components)

            if not results:
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=5, layer_name=layer_name, result=LayerResult.NO_MATCH,
                    reason="No fuzzy matches found"
                ))
                return None

            # For fuzzy matching, we need to be more careful about disambiguation
            # Filter to only results with similar similarity scores
            best_similarity = float(results[0]['street_similarity'])
            similar_results = [r for r in results if abs(float(r['street_similarity']) - best_similarity) < 0.05]

            if len(similar_results) == 1:
                self.metadata.matched_layer = 5
                self.metadata.matched_layer_name = layer_name
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=5, layer_name=layer_name, result=LayerResult.SINGLE_MATCH,
                    candidates=1
                ))
                result = build_result_from_properties_row(similar_results[0], 5, self.metadata)
                # Scale confidence by similarity
                result['confidence'] = round(LAYER_CONFIDENCE[5] * best_similarity, 3)
                result['fuzzy_match'] = True
                result['street_similarity'] = round(best_similarity, 3)
                result['input_street_name'] = street_name
                result['matched_street_name'] = similar_results[0]['street_name']
                return result

            best_row, was_disambiguated, margin = disambiguate(similar_results, components, 'properties')

            if best_row and was_disambiguated:
                self.metadata.matched_layer = 5
                self.metadata.matched_layer_name = layer_name
                self.metadata.disambiguation_used = True
                self.metadata.disambiguation_margin = margin
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=5, layer_name=layer_name, result=LayerResult.DISAMBIGUATED,
                    candidates=len(similar_results), reason=f"Disambiguated with margin {margin}"
                ))
                result = build_result_from_properties_row(best_row, 5, self.metadata)
                similarity = float(best_row['street_similarity'])
                result['confidence'] = round(LAYER_CONFIDENCE[5] * similarity, 3)
                result['fuzzy_match'] = True
                result['street_similarity'] = round(similarity, 3)
                result['input_street_name'] = street_name
                result['matched_street_name'] = best_row['street_name']
                return result

            self.metadata.layers_tried.append(LayerAttempt(
                layer=5, layer_name=layer_name, result=LayerResult.AMBIGUOUS,
                candidates=len(similar_results), reason=f"Multiple fuzzy matches, margin only {margin}"
            ))
            return None

        except Exception as error:
            # pg_trgm might not be available
            if 'function similarity' in str(error).lower():
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=5, layer_name=layer_name, result=LayerResult.NO_MATCH,
                    reason="pg_trgm extension not available"
                ))
            else:
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=5, layer_name=layer_name, result=LayerResult.NO_MATCH,
                    reason=f"Error: {str(error)}"
                ))
            return None

    async def _layer_6_fuzzy_city(self, components: Dict, street_number: str,
                                   street_name: str, city: str) -> Optional[Dict[str, Any]]:
        """Layer 6: Fuzzy street name matching within city."""
        layer_name = "fuzzy_city"

        try:
            query = '''
                SELECT *,
                       similarity(UPPER(street_name), UPPER($3)) as street_similarity
                FROM properties
                WHERE UPPER(street_number) = UPPER($1)
                    AND similarity(UPPER(street_name), UPPER($3)) > 0.6
                    AND (
                        normalize_municipality_name(city) = normalize_municipality_name($2)
                        OR EXISTS (
                            SELECT 1 FROM municipality_aliases ma
                            WHERE UPPER(ma.canonical_name) = normalize_municipality_name($2)
                            AND normalize_municipality_name(city) = ma.normalized_alias
                        )
                    )
                ORDER BY similarity(UPPER(street_name), UPPER($3)) DESC, id
                LIMIT 10
            '''
            params = [street_number, city, street_name]

            results = await query_candidates(query, params, components)

            # Civic suffix fallback: if no results and input has no suffix, try matching with suffix variants
            if not results:
                input_civic_suffix = components.get('civic_no_suffix')
                if not input_civic_suffix:
                    fallback_query = '''
                        SELECT *,
                               similarity(UPPER(street_name), UPPER($3)) as street_similarity
                        FROM properties
                        WHERE UPPER(street_number) = UPPER($1)
                            AND civic_no_suffix IS NOT NULL AND civic_no_suffix != ''
                            AND similarity(UPPER(street_name), UPPER($3)) > 0.6
                            AND (
                                normalize_municipality_name(city) = normalize_municipality_name($2)
                                OR EXISTS (
                                    SELECT 1 FROM municipality_aliases ma
                                    WHERE UPPER(ma.canonical_name) = normalize_municipality_name($2)
                                    AND normalize_municipality_name(city) = ma.normalized_alias
                                )
                            )
                        ORDER BY similarity(UPPER(street_name), UPPER($3)) DESC, civic_no_suffix, id
                        LIMIT 10
                    '''
                    results = await query_candidates(fallback_query, params, components)
                    if results:
                        self.metadata.civic_suffix_fallback = True

            if not results:
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=6, layer_name=layer_name, result=LayerResult.NO_MATCH,
                    reason="No fuzzy matches found"
                ))
                return None

            best_similarity = float(results[0]['street_similarity'])
            similar_results = [r for r in results if abs(float(r['street_similarity']) - best_similarity) < 0.05]

            if len(similar_results) == 1:
                self.metadata.matched_layer = 6
                self.metadata.matched_layer_name = layer_name
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=6, layer_name=layer_name, result=LayerResult.SINGLE_MATCH,
                    candidates=1
                ))
                result = build_result_from_properties_row(similar_results[0], 6, self.metadata)
                result['confidence'] = round(LAYER_CONFIDENCE[6] * best_similarity, 3)
                result['fuzzy_match'] = True
                result['street_similarity'] = round(best_similarity, 3)
                result['input_street_name'] = street_name
                result['matched_street_name'] = similar_results[0]['street_name']
                return result

            best_row, was_disambiguated, margin = disambiguate(similar_results, components, 'properties')

            if best_row and was_disambiguated:
                self.metadata.matched_layer = 6
                self.metadata.matched_layer_name = layer_name
                self.metadata.disambiguation_used = True
                self.metadata.disambiguation_margin = margin
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=6, layer_name=layer_name, result=LayerResult.DISAMBIGUATED,
                    candidates=len(similar_results), reason=f"Disambiguated with margin {margin}"
                ))
                result = build_result_from_properties_row(best_row, 6, self.metadata)
                similarity = float(best_row['street_similarity'])
                result['confidence'] = round(LAYER_CONFIDENCE[6] * similarity, 3)
                result['fuzzy_match'] = True
                result['street_similarity'] = round(similarity, 3)
                result['input_street_name'] = street_name
                result['matched_street_name'] = best_row['street_name']
                return result

            self.metadata.layers_tried.append(LayerAttempt(
                layer=6, layer_name=layer_name, result=LayerResult.AMBIGUOUS,
                candidates=len(similar_results), reason=f"Multiple fuzzy matches, margin only {margin}"
            ))
            return None

        except Exception as error:
            if 'function similarity' in str(error).lower():
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=6, layer_name=layer_name, result=LayerResult.NO_MATCH,
                    reason="pg_trgm extension not available"
                ))
            else:
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=6, layer_name=layer_name, result=LayerResult.NO_MATCH,
                    reason=f"Error: {str(error)}"
                ))
            return None

    def get_metadata(self) -> MatchMetadata:
        """Get the metadata from the last match attempt."""
        return self.metadata


class LayeredNARMatcher:
    """
    Layered address matcher for NAR (National Address Register) data.

    Same layered approach as municipal matcher but queries NAR tables.
    """

    def __init__(self):
        self.metadata = MatchMetadata()

    async def match(self, address_components: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Attempt to match address using layered approach against NAR data.

        Args:
            address_components: Parsed address components

        Returns:
            Geocoding result or None
        """
        self.metadata = MatchMetadata()

        postal_code = address_components.get('postal_code')
        fsa_code = postal_code[:3].upper() if postal_code and len(postal_code) >= 3 else None
        postal_zone = postal_code[:2].upper() if postal_code and len(postal_code) >= 2 else None
        street_number = address_components.get('street_number')
        street_name = address_components.get('street_name')
        normalized_city = address_components.get('normalized_city') or address_components.get('city')

        if not street_number or not street_name:
            return None

        # Layer 0: Exact match
        result = await self._layer_0_exact(address_components, postal_code, street_number, street_name, normalized_city)
        if result:
            return result

        # Layer 1: Postal + core
        if postal_code:
            result = await self._layer_1_postal_core(address_components, postal_code, street_number, street_name)
            if result:
                return result

        # Layer 2: FSA + core + city
        if fsa_code and normalized_city:
            result = await self._layer_2_fsa_core_city(address_components, fsa_code, street_number, street_name, normalized_city)
            if result:
                return result

        # Layer 3: Postal zone + core
        if postal_zone:
            result = await self._layer_3_postal_zone_core(address_components, postal_zone, street_number, street_name)
            if result:
                return result

        # Layer 4: City + core
        if normalized_city:
            result = await self._layer_4_city_core(address_components, street_number, street_name, normalized_city)
            if result:
                return result

        # Layer 5: Fuzzy (postal zone)
        if postal_zone:
            result = await self._layer_5_fuzzy_postal_zone(address_components, postal_zone, street_number, street_name)
            if result:
                return result

        # Layer 6: Fuzzy (city)
        if normalized_city:
            result = await self._layer_6_fuzzy_city(address_components, street_number, street_name, normalized_city)
            if result:
                return result

        return None

    async def _layer_0_exact(self, components: Dict, postal_code: str, street_number: str,
                             street_name: str, city: str) -> Optional[Dict[str, Any]]:
        """Layer 0: Exact match on all provided fields in NAR."""
        layer_name = "nar_exact_all_fields"

        if not postal_code:
            self.metadata.layers_tried.append(LayerAttempt(
                layer=0, layer_name=layer_name, result=LayerResult.NO_MATCH,
                reason="Missing postal code for exact match"
            ))
            return None

        try:
            query = f'''
                SELECT a.*, a.latitude AS bg_latitude, a.longitude AS bg_longitude
                FROM nar_addresses a
                WHERE a.mail_postal_code = $1
                    AND UPPER(a.civic_no) = UPPER($2)
                    AND UPPER(a.official_street_name) = UPPER($3)
                    AND a.prov_code = {config.ONTARIO_PROV_CODE}
                    AND a.latitude IS NOT NULL
                    AND a.longitude IS NOT NULL
            '''
            params = [postal_code, street_number, street_name]

            suffix = components.get('street_suffix') or components.get('street_suffix_normalized')
            if suffix:
                query += f' AND UPPER(a.official_street_type) = UPPER(${len(params) + 1})'
                params.append(suffix)

            direction = components.get('street_direction') or components.get('street_direction_normalized')
            if direction:
                query += f' AND UPPER(a.official_street_dir) = UPPER(${len(params) + 1})'
                params.append(direction)

            if city:
                query += f' AND normalize_municipality_name(a.csd_eng_name) = normalize_municipality_name(${len(params) + 1})'
                params.append(city)

            # Deterministic ordering: without it, LIMIT can drop candidates arbitrarily
            query += ' ORDER BY a.id LIMIT 10'

            results = await query_candidates(query, params, components)

            if not results:
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=0, layer_name=layer_name, result=LayerResult.NO_MATCH,
                    reason="No exact matches found"
                ))
                return None

            if len(results) == 1:
                self.metadata.matched_layer = 0
                self.metadata.matched_layer_name = layer_name
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=0, layer_name=layer_name, result=LayerResult.SINGLE_MATCH,
                    candidates=1
                ))
                return build_result_from_nar_row(results[0], 0, self.metadata)

            best_row, was_disambiguated, margin = disambiguate(results, components, 'nar')

            if best_row and was_disambiguated:
                self.metadata.matched_layer = 0
                self.metadata.matched_layer_name = layer_name
                self.metadata.disambiguation_used = True
                self.metadata.disambiguation_margin = margin
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=0, layer_name=layer_name, result=LayerResult.DISAMBIGUATED,
                    candidates=len(results), reason=f"Disambiguated with margin {margin}"
                ))
                return build_result_from_nar_row(best_row, 0, self.metadata)

            self.metadata.layers_tried.append(LayerAttempt(
                layer=0, layer_name=layer_name, result=LayerResult.AMBIGUOUS,
                candidates=len(results), reason=f"Multiple matches, margin only {margin}"
            ))
            return None

        except Exception as error:
            self.metadata.layers_tried.append(LayerAttempt(
                layer=0, layer_name=layer_name, result=LayerResult.NO_MATCH,
                reason=f"Error: {str(error)}"
            ))
            return None

    async def _layer_1_postal_core(self, components: Dict, postal_code: str,
                                    street_number: str, street_name: str) -> Optional[Dict[str, Any]]:
        """Layer 1: Postal + core identity in NAR."""
        layer_name = "nar_postal_core"

        try:
            query = f'''
                SELECT a.*, a.latitude AS bg_latitude, a.longitude AS bg_longitude
                FROM nar_addresses a
                WHERE a.mail_postal_code = $1
                    AND UPPER(a.civic_no) = UPPER($2)
                    AND UPPER(a.official_street_name) = UPPER($3)
                    AND a.prov_code = {config.ONTARIO_PROV_CODE}
                    AND a.latitude IS NOT NULL
                    AND a.longitude IS NOT NULL
                ORDER BY a.id
                LIMIT 10
            '''
            params = [postal_code, street_number, street_name]

            results = await query_candidates(query, params, components)

            if not results:
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=1, layer_name=layer_name, result=LayerResult.NO_MATCH,
                    reason="No matches found"
                ))
                return None

            if len(results) == 1:
                self.metadata.matched_layer = 1
                self.metadata.matched_layer_name = layer_name
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=1, layer_name=layer_name, result=LayerResult.SINGLE_MATCH,
                    candidates=1
                ))
                return build_result_from_nar_row(results[0], 1, self.metadata)

            best_row, was_disambiguated, margin = disambiguate(results, components, 'nar')

            if best_row and was_disambiguated:
                self.metadata.matched_layer = 1
                self.metadata.matched_layer_name = layer_name
                self.metadata.disambiguation_used = True
                self.metadata.disambiguation_margin = margin
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=1, layer_name=layer_name, result=LayerResult.DISAMBIGUATED,
                    candidates=len(results), reason=f"Disambiguated with margin {margin}"
                ))
                return build_result_from_nar_row(best_row, 1, self.metadata)

            self.metadata.layers_tried.append(LayerAttempt(
                layer=1, layer_name=layer_name, result=LayerResult.AMBIGUOUS,
                candidates=len(results), reason=f"Multiple matches, margin only {margin}"
            ))
            return None

        except Exception as error:
            self.metadata.layers_tried.append(LayerAttempt(
                layer=1, layer_name=layer_name, result=LayerResult.NO_MATCH,
                reason=f"Error: {str(error)}"
            ))
            return None

    async def _layer_2_fsa_core_city(self, components: Dict, fsa_code: str,
                                      street_number: str, street_name: str, city: str) -> Optional[Dict[str, Any]]:
        """Layer 2: FSA + core + city in NAR."""
        layer_name = "nar_fsa_core_city"

        try:
            query = f'''
                SELECT a.*, a.latitude AS bg_latitude, a.longitude AS bg_longitude
                FROM nar_addresses a
                WHERE a.fsa_code = $1
                    AND UPPER(a.civic_no) = UPPER($2)
                    AND UPPER(a.official_street_name) = UPPER($3)
                    AND normalize_municipality_name(a.csd_eng_name) = normalize_municipality_name($4)
                    AND a.prov_code = {config.ONTARIO_PROV_CODE}
                    AND a.latitude IS NOT NULL
                    AND a.longitude IS NOT NULL
                ORDER BY a.id
                LIMIT 10
            '''
            params = [fsa_code, street_number, street_name, city]

            results = await query_candidates(query, params, components)

            if not results:
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=2, layer_name=layer_name, result=LayerResult.NO_MATCH,
                    reason="No matches found"
                ))
                return None

            if len(results) == 1:
                self.metadata.matched_layer = 2
                self.metadata.matched_layer_name = layer_name
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=2, layer_name=layer_name, result=LayerResult.SINGLE_MATCH,
                    candidates=1
                ))
                return build_result_from_nar_row(results[0], 2, self.metadata)

            best_row, was_disambiguated, margin = disambiguate(results, components, 'nar')

            if best_row and was_disambiguated:
                self.metadata.matched_layer = 2
                self.metadata.matched_layer_name = layer_name
                self.metadata.disambiguation_used = True
                self.metadata.disambiguation_margin = margin
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=2, layer_name=layer_name, result=LayerResult.DISAMBIGUATED,
                    candidates=len(results), reason=f"Disambiguated with margin {margin}"
                ))
                return build_result_from_nar_row(best_row, 2, self.metadata)

            self.metadata.layers_tried.append(LayerAttempt(
                layer=2, layer_name=layer_name, result=LayerResult.AMBIGUOUS,
                candidates=len(results), reason=f"Multiple matches, margin only {margin}"
            ))
            return None

        except Exception as error:
            self.metadata.layers_tried.append(LayerAttempt(
                layer=2, layer_name=layer_name, result=LayerResult.NO_MATCH,
                reason=f"Error: {str(error)}"
            ))
            return None

    async def _layer_3_postal_zone_core(self, components: Dict, postal_zone: str,
                                         street_number: str, street_name: str) -> Optional[Dict[str, Any]]:
        """Layer 3: Postal zone + core in NAR."""
        layer_name = "nar_postal_zone_core"

        try:
            # Prefer rows in the input's full FSA (best-quality candidates first),
            # then id for a deterministic order under LIMIT.
            postal_code = components.get('postal_code')
            input_fsa = postal_code[:3].upper() if postal_code and len(postal_code) >= 3 else None

            query = f'''
                SELECT a.*, a.latitude AS bg_latitude, a.longitude AS bg_longitude
                FROM nar_addresses a
                WHERE a.fsa_code LIKE $1
                    AND UPPER(a.civic_no) = UPPER($2)
                    AND UPPER(a.official_street_name) = UPPER($3)
                    AND a.prov_code = {config.ONTARIO_PROV_CODE}
                    AND a.latitude IS NOT NULL
                    AND a.longitude IS NOT NULL
                ORDER BY (a.fsa_code = $4) DESC NULLS LAST, a.id
                LIMIT 10
            '''
            params = [f"{postal_zone}%", street_number, street_name, input_fsa]

            results = await query_candidates(query, params, components)

            if not results:
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=3, layer_name=layer_name, result=LayerResult.NO_MATCH,
                    reason="No matches found"
                ))
                return None

            if len(results) == 1:
                self.metadata.matched_layer = 3
                self.metadata.matched_layer_name = layer_name
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=3, layer_name=layer_name, result=LayerResult.SINGLE_MATCH,
                    candidates=1
                ))
                return build_result_from_nar_row(results[0], 3, self.metadata)

            best_row, was_disambiguated, margin = disambiguate(results, components, 'nar')

            if best_row and was_disambiguated:
                self.metadata.matched_layer = 3
                self.metadata.matched_layer_name = layer_name
                self.metadata.disambiguation_used = True
                self.metadata.disambiguation_margin = margin
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=3, layer_name=layer_name, result=LayerResult.DISAMBIGUATED,
                    candidates=len(results), reason=f"Disambiguated with margin {margin}"
                ))
                return build_result_from_nar_row(best_row, 3, self.metadata)

            self.metadata.layers_tried.append(LayerAttempt(
                layer=3, layer_name=layer_name, result=LayerResult.AMBIGUOUS,
                candidates=len(results), reason=f"Multiple matches, margin only {margin}"
            ))
            return None

        except Exception as error:
            self.metadata.layers_tried.append(LayerAttempt(
                layer=3, layer_name=layer_name, result=LayerResult.NO_MATCH,
                reason=f"Error: {str(error)}"
            ))
            return None

    async def _layer_4_city_core(self, components: Dict, street_number: str,
                                  street_name: str, city: str) -> Optional[Dict[str, Any]]:
        """Layer 4: City + core in NAR."""
        layer_name = "nar_city_core"

        try:
            query = f'''
                SELECT a.*, a.latitude AS bg_latitude, a.longitude AS bg_longitude
                FROM nar_addresses a
                WHERE UPPER(a.civic_no) = UPPER($1)
                    AND UPPER(a.official_street_name) = UPPER($2)
                    AND normalize_municipality_name(a.csd_eng_name) = normalize_municipality_name($3)
                    AND a.prov_code = {config.ONTARIO_PROV_CODE}
                    AND a.latitude IS NOT NULL
                    AND a.longitude IS NOT NULL
                ORDER BY a.id
                LIMIT 10
            '''
            params = [street_number, street_name, city]

            results = await query_candidates(query, params, components)

            # Civic suffix fallback: if no results and input has no suffix, try matching with suffix variants
            if not results:
                input_civic_suffix = components.get('civic_no_suffix')
                if not input_civic_suffix:
                    fallback_query = f'''
                        SELECT a.*, a.latitude AS bg_latitude, a.longitude AS bg_longitude
                        FROM nar_addresses a
                        WHERE UPPER(a.civic_no) = UPPER($1)
                            AND a.civic_no_suffix IS NOT NULL AND a.civic_no_suffix != ''
                            AND UPPER(a.official_street_name) = UPPER($2)
                            AND normalize_municipality_name(a.csd_eng_name) = normalize_municipality_name($3)
                            AND a.prov_code = {config.ONTARIO_PROV_CODE}
                            AND a.latitude IS NOT NULL
                            AND a.longitude IS NOT NULL
                        ORDER BY a.civic_no_suffix, a.id
                        LIMIT 10
                    '''
                    results = await query_candidates(fallback_query, params, components)
                    if results:
                        self.metadata.civic_suffix_fallback = True

            if not results:
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=4, layer_name=layer_name, result=LayerResult.NO_MATCH,
                    reason="No matches found"
                ))
                return None

            if len(results) == 1:
                self.metadata.matched_layer = 4
                self.metadata.matched_layer_name = layer_name
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=4, layer_name=layer_name, result=LayerResult.SINGLE_MATCH,
                    candidates=1
                ))
                return build_result_from_nar_row(results[0], 4, self.metadata)

            best_row, was_disambiguated, margin = disambiguate(results, components, 'nar')

            if best_row and was_disambiguated:
                self.metadata.matched_layer = 4
                self.metadata.matched_layer_name = layer_name
                self.metadata.disambiguation_used = True
                self.metadata.disambiguation_margin = margin
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=4, layer_name=layer_name, result=LayerResult.DISAMBIGUATED,
                    candidates=len(results), reason=f"Disambiguated with margin {margin}"
                ))
                return build_result_from_nar_row(best_row, 4, self.metadata)

            self.metadata.layers_tried.append(LayerAttempt(
                layer=4, layer_name=layer_name, result=LayerResult.AMBIGUOUS,
                candidates=len(results), reason=f"Multiple matches, margin only {margin}"
            ))
            return None

        except Exception as error:
            self.metadata.layers_tried.append(LayerAttempt(
                layer=4, layer_name=layer_name, result=LayerResult.NO_MATCH,
                reason=f"Error: {str(error)}"
            ))
            return None

    async def _layer_5_fuzzy_postal_zone(self, components: Dict, postal_zone: str,
                                          street_number: str, street_name: str) -> Optional[Dict[str, Any]]:
        """Layer 5: Fuzzy street in NAR within postal zone."""
        layer_name = "nar_fuzzy_postal_zone"

        try:
            query = f'''
                SELECT a.*, a.latitude AS bg_latitude, a.longitude AS bg_longitude,
                       similarity(UPPER(a.official_street_name), UPPER($3)) as street_similarity
                FROM nar_addresses a
                WHERE a.fsa_code LIKE $1
                    AND UPPER(a.civic_no) = UPPER($2)
                    AND similarity(UPPER(a.official_street_name), UPPER($3)) > 0.6
                    AND a.prov_code = {config.ONTARIO_PROV_CODE}
                    AND a.latitude IS NOT NULL
                    AND a.longitude IS NOT NULL
                ORDER BY similarity(UPPER(a.official_street_name), UPPER($3)) DESC, a.id
                LIMIT 10
            '''
            params = [f"{postal_zone}%", street_number, street_name]

            results = await query_candidates(query, params, components)

            if not results:
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=5, layer_name=layer_name, result=LayerResult.NO_MATCH,
                    reason="No fuzzy matches found"
                ))
                return None

            best_similarity = float(results[0]['street_similarity'])
            similar_results = [r for r in results if abs(float(r['street_similarity']) - best_similarity) < 0.05]

            if len(similar_results) == 1:
                self.metadata.matched_layer = 5
                self.metadata.matched_layer_name = layer_name
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=5, layer_name=layer_name, result=LayerResult.SINGLE_MATCH,
                    candidates=1
                ))
                result = build_result_from_nar_row(similar_results[0], 5, self.metadata)
                result['confidence'] = round(NAR_LAYER_CONFIDENCE[5] * best_similarity, 3)
                result['fuzzy_match'] = True
                result['street_similarity'] = round(best_similarity, 3)
                result['input_street_name'] = street_name
                result['matched_street_name'] = similar_results[0]['official_street_name']
                return result

            best_row, was_disambiguated, margin = disambiguate(similar_results, components, 'nar')

            if best_row and was_disambiguated:
                self.metadata.matched_layer = 5
                self.metadata.matched_layer_name = layer_name
                self.metadata.disambiguation_used = True
                self.metadata.disambiguation_margin = margin
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=5, layer_name=layer_name, result=LayerResult.DISAMBIGUATED,
                    candidates=len(similar_results), reason=f"Disambiguated with margin {margin}"
                ))
                result = build_result_from_nar_row(best_row, 5, self.metadata)
                similarity = float(best_row['street_similarity'])
                result['confidence'] = round(NAR_LAYER_CONFIDENCE[5] * similarity, 3)
                result['fuzzy_match'] = True
                result['street_similarity'] = round(similarity, 3)
                result['input_street_name'] = street_name
                result['matched_street_name'] = best_row['official_street_name']
                return result

            self.metadata.layers_tried.append(LayerAttempt(
                layer=5, layer_name=layer_name, result=LayerResult.AMBIGUOUS,
                candidates=len(similar_results), reason=f"Multiple fuzzy matches, margin only {margin}"
            ))
            return None

        except Exception as error:
            if 'function similarity' in str(error).lower():
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=5, layer_name=layer_name, result=LayerResult.NO_MATCH,
                    reason="pg_trgm extension not available"
                ))
            else:
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=5, layer_name=layer_name, result=LayerResult.NO_MATCH,
                    reason=f"Error: {str(error)}"
                ))
            return None

    async def _layer_6_fuzzy_city(self, components: Dict, street_number: str,
                                   street_name: str, city: str) -> Optional[Dict[str, Any]]:
        """Layer 6: Fuzzy street in NAR within city."""
        layer_name = "nar_fuzzy_city"

        try:
            query = f'''
                SELECT a.*, a.latitude AS bg_latitude, a.longitude AS bg_longitude,
                       similarity(UPPER(a.official_street_name), UPPER($3)) as street_similarity
                FROM nar_addresses a
                WHERE UPPER(a.civic_no) = UPPER($1)
                    AND similarity(UPPER(a.official_street_name), UPPER($3)) > 0.6
                    AND normalize_municipality_name(a.csd_eng_name) = normalize_municipality_name($2)
                    AND a.prov_code = {config.ONTARIO_PROV_CODE}
                    AND a.latitude IS NOT NULL
                    AND a.longitude IS NOT NULL
                ORDER BY similarity(UPPER(a.official_street_name), UPPER($3)) DESC, a.id
                LIMIT 10
            '''
            params = [street_number, city, street_name]

            results = await query_candidates(query, params, components)

            # Civic suffix fallback: if no results and input has no suffix, try matching with suffix variants
            if not results:
                input_civic_suffix = components.get('civic_no_suffix')
                if not input_civic_suffix:
                    fallback_query = f'''
                        SELECT a.*, a.latitude AS bg_latitude, a.longitude AS bg_longitude,
                               similarity(UPPER(a.official_street_name), UPPER($3)) as street_similarity
                        FROM nar_addresses a
                        WHERE UPPER(a.civic_no) = UPPER($1)
                            AND a.civic_no_suffix IS NOT NULL AND a.civic_no_suffix != ''
                            AND similarity(UPPER(a.official_street_name), UPPER($3)) > 0.6
                            AND normalize_municipality_name(a.csd_eng_name) = normalize_municipality_name($2)
                            AND a.prov_code = {config.ONTARIO_PROV_CODE}
                            AND a.latitude IS NOT NULL
                            AND a.longitude IS NOT NULL
                        ORDER BY similarity(UPPER(a.official_street_name), UPPER($3)) DESC, a.civic_no_suffix, a.id
                        LIMIT 10
                    '''
                    results = await query_candidates(fallback_query, params, components)
                    if results:
                        self.metadata.civic_suffix_fallback = True

            if not results:
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=6, layer_name=layer_name, result=LayerResult.NO_MATCH,
                    reason="No fuzzy matches found"
                ))
                return None

            best_similarity = float(results[0]['street_similarity'])
            similar_results = [r for r in results if abs(float(r['street_similarity']) - best_similarity) < 0.05]

            if len(similar_results) == 1:
                self.metadata.matched_layer = 6
                self.metadata.matched_layer_name = layer_name
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=6, layer_name=layer_name, result=LayerResult.SINGLE_MATCH,
                    candidates=1
                ))
                result = build_result_from_nar_row(similar_results[0], 6, self.metadata)
                result['confidence'] = round(NAR_LAYER_CONFIDENCE[6] * best_similarity, 3)
                result['fuzzy_match'] = True
                result['street_similarity'] = round(best_similarity, 3)
                result['input_street_name'] = street_name
                result['matched_street_name'] = similar_results[0]['official_street_name']
                return result

            best_row, was_disambiguated, margin = disambiguate(similar_results, components, 'nar')

            if best_row and was_disambiguated:
                self.metadata.matched_layer = 6
                self.metadata.matched_layer_name = layer_name
                self.metadata.disambiguation_used = True
                self.metadata.disambiguation_margin = margin
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=6, layer_name=layer_name, result=LayerResult.DISAMBIGUATED,
                    candidates=len(similar_results), reason=f"Disambiguated with margin {margin}"
                ))
                result = build_result_from_nar_row(best_row, 6, self.metadata)
                similarity = float(best_row['street_similarity'])
                result['confidence'] = round(NAR_LAYER_CONFIDENCE[6] * similarity, 3)
                result['fuzzy_match'] = True
                result['street_similarity'] = round(similarity, 3)
                result['input_street_name'] = street_name
                result['matched_street_name'] = best_row['official_street_name']
                return result

            self.metadata.layers_tried.append(LayerAttempt(
                layer=6, layer_name=layer_name, result=LayerResult.AMBIGUOUS,
                candidates=len(similar_results), reason=f"Multiple fuzzy matches, margin only {margin}"
            ))
            return None

        except Exception as error:
            if 'function similarity' in str(error).lower():
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=6, layer_name=layer_name, result=LayerResult.NO_MATCH,
                    reason="pg_trgm extension not available"
                ))
            else:
                self.metadata.layers_tried.append(LayerAttempt(
                    layer=6, layer_name=layer_name, result=LayerResult.NO_MATCH,
                    reason=f"Error: {str(error)}"
                ))
            return None

    def get_metadata(self) -> MatchMetadata:
        """Get the metadata from the last match attempt."""
        return self.metadata


# Convenience functions for use from geocoder.py
async def match_municipal_layered(address_components: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Match address against municipal data using layered approach."""
    matcher = LayeredMatcher()
    return await matcher.match(address_components)


async def match_nar_layered(address_components: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Match address against NAR data using layered approach."""
    matcher = LayeredNARMatcher()
    return await matcher.match(address_components)
