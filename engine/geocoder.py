"""
Main geocoding service with 3-tier cascading strategy.
Tier 1: PIN lookup
Tier 2: Municipal data matching (searches all datasets)
Tier 3: National Address Register (NAR)
"""
import os
import asyncio
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List

logger = logging.getLogger('geocoder.matching')
from . import database, cache
from .geographic_validator import get_validator
from .address_parser import parse_address, generate_address_hash, build_full_address
from .layered_matcher import match_municipal_layered, match_nar_layered


# Statistics
_stats = {
    'total_requests': 0,
    'successful': 0,
    'failed': 0,
    'tier_1_pin': 0,
    'tier_2_municipal': 0,
    'tier_3_nar': 0,
    'cache_hits': 0,
    'total_response_time_ms': 0
}


async def lookup_by_pin(address_components: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Tier 1: PIN-based lookup.

    Args:
        address_components: Parsed address components

    Returns:
        Geocoding result or None
    """
    if not address_components.get('pin'):
        return None

    try:
        # Convert PIN to integer for database query
        pin = int(address_components['pin'])

        result = await database.query(
            'SELECT * FROM properties WHERE pin = $1 AND NOT is_deleted ORDER BY id LIMIT 1',
            [pin]
        )

        if not result:
            return None

        row = result[0]

        return {
            'latitude': float(row['latitude']),
            'longitude': float(row['longitude']),
            'matched_address': row['full_address'],
            'source': f"municipal_{row['source_dataset']}" if row['source_dataset'] else 'municipal_match',
            'confidence': 1.0,
            'match_tier': 1,
            'match_method': 'pin_lookup',
            'property_id': row['property_id'],
            'assessment_roll_number': row['assessment_roll_number'],
            'pin': row['pin'],
            'gpid': row['gpid']
        }

    except Exception as error:
        logger.error(f'PIN lookup error: {error}')
        return None


async def match_municipal_data(address_components: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Tier 2: Municipal data matching using layered approach.

    Uses progressive matching strategy:
    - Layer 0: Exact match (all provided fields)
    - Layer 1: Postal + core identity
    - Layer 2: FSA + core + city
    - Layer 3: Postal zone + core
    - Layer 4: City + core
    - Layer 5: Fuzzy (postal zone)
    - Layer 6: Fuzzy (city)

    Never makes arbitrary choices - ambiguous results proceed to next layer.

    Args:
        address_components: Parsed address components

    Returns:
        Geocoding result or None
    """
    # GEOGRAPHIC ENRICHMENT: Normalize and validate geographic fields
    validator = await get_validator()
    enriched_components = await validator.enrich_query_geography(address_components)

    # Use enriched components for searching (includes normalized_city if available)
    search_components = {**address_components, **enriched_components}

    # Use layered matcher
    result = await match_municipal_layered(search_components)

    if result:
        # Apply geographic confidence adjustment if available
        if 'geography_confidence_adjustment' in enriched_components:
            result['confidence'] = min(0.99, result['confidence'] + enriched_components['geography_confidence_adjustment'])
            if enriched_components.get('geography_notes'):
                result['geography_notes'] = enriched_components['geography_notes']

    return result


async def match_nar_data(address_components: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Tier 3: NAR data matching using layered approach.

    Same layered strategy as municipal but queries NAR tables.

    Args:
        address_components: Parsed address components

    Returns:
        Geocoding result or None
    """
    # GEOGRAPHIC ENRICHMENT: Normalize and validate geographic fields
    validator = await get_validator()
    enriched_components = await validator.enrich_query_geography(address_components)

    # Use enriched components for searching
    search_components = {**address_components, **enriched_components}

    # Use layered NAR matcher
    result = await match_nar_layered(search_components)

    if result:
        # Apply geographic confidence adjustment if available
        if 'geography_confidence_adjustment' in enriched_components:
            result['confidence'] = min(0.99, result['confidence'] + enriched_components['geography_confidence_adjustment'])
            if enriched_components.get('geography_notes'):
                result['geography_notes'] = enriched_components['geography_notes']

    return result


def calculate_enhanced_confidence(result: Dict[str, Any], address_components: Dict[str, Any]) -> Dict[str, Any]:
    """Calculate enhanced confidence score."""
    # Public identifiers are strings in both single and batch responses. asyncpg
    # returns BIGINTs as ints; normalize before caching/serializing either route.
    for field in ('pin', 'gpid'):
        if result.get(field) is not None:
            result[field] = str(result[field])
    base_confidence = result.get('confidence', 0.5)

    # Add confidence breakdown
    result['confidence_factors'] = {
        'base_confidence': base_confidence,
        'tier': result['match_tier'],
        'match_method': result['match_method']
    }

    return result


async def geocode(address_input: Any) -> Dict[str, Any]:
    """
    Main geocoding function with 3-tier cascading.

    Args:
        address_input: Address input (string or structured)

    Returns:
        Geocoding result
    """
    global _stats

    start_time = datetime.now()
    _stats['total_requests'] += 1

    try:
        # Parse address
        address_components = parse_address(address_input)

        # Reject un-parseable input up front: no identity fields means nothing to
        # match, AND every such input shares one cache hash ('|||...'), so it must
        # never reach the cache (one negative entry would fail all later ones).
        if not any(address_components.get(k) for k in
                   ('pin', 'street_number', 'street_name', 'postal_code')):
            _stats['failed'] += 1
            return {
                'success': False,
                'error': 'Unparseable address input: no recognizable address fields',
                'cached': False,
                'response_time_ms': (datetime.now() - start_time).total_seconds() * 1000
            }

        address_hash = generate_address_hash(address_components)

        # Check cache
        # v3 excludes Middlesex municipal data, including previously cached hits.
        # Keep auth/rate-limit namespaces intact during this policy transition.
        cache_key = f'geocode:v3:{address_hash}'
        cached = await cache.get(cache_key)

        if cached:
            _stats['cache_hits'] += 1
            cached['cached'] = True
            cached['response_time_ms'] = (datetime.now() - start_time).total_seconds() * 1000
            return cached

        # TIER 1: PIN Lookup
        result = await lookup_by_pin(address_components)
        if result:
            _stats['tier_1_pin'] += 1
            _stats['successful'] += 1
            result = calculate_enhanced_confidence(result, address_components)
            result['cached'] = False
            result['response_time_ms'] = (datetime.now() - start_time).total_seconds() * 1000
            _stats['total_response_time_ms'] += result['response_time_ms']
            await cache.set(cache_key, result)
            return result

        # TIER 2: Municipal Data Match (searches all datasets)
        result = await match_municipal_data(address_components)
        if result:
            _stats['tier_2_municipal'] += 1
            _stats['successful'] += 1
            result = calculate_enhanced_confidence(result, address_components)
            result['cached'] = False
            result['response_time_ms'] = (datetime.now() - start_time).total_seconds() * 1000
            _stats['total_response_time_ms'] += result['response_time_ms']
            await cache.set(cache_key, result)
            return result

        # TIER 3: NAR Fallback (using layered approach)
        result = await match_nar_data(address_components)
        if result:
            _stats['tier_3_nar'] += 1
            _stats['successful'] += 1
            result = calculate_enhanced_confidence(result, address_components)
            result['cached'] = False
            result['response_time_ms'] = (datetime.now() - start_time).total_seconds() * 1000
            _stats['total_response_time_ms'] += result['response_time_ms']
            await cache.set(cache_key, result)
            return result

        # No match found in any tier
        _stats['failed'] += 1
        failure = {
            'success': False,
            'error': 'No match found in any data source',
            'input_address': build_full_address(address_components),
            'cached': False,
            'response_time_ms': (datetime.now() - start_time).total_seconds() * 1000
        }

        # Cache negative result (never for all-None parses — belt-and-braces
        # with the guard above, since they all share one hash)
        if any(address_components.get(k) for k in
               ('pin', 'street_number', 'street_name', 'postal_code')):
            await cache.set_negative(cache_key)

        return failure

    except Exception:
        # Log the full exception internally; never leak internal details
        # (SQL fragments, file paths, driver messages) to API clients.
        logger.exception('Geocoding error')
        _stats['failed'] += 1
        return {
            'success': False,
            'error': 'Internal geocoding error',
            'cached': False,
            'response_time_ms': (datetime.now() - start_time).total_seconds() * 1000
        }


async def geocode_batch(addresses: List[Any]) -> Dict[str, Any]:
    """
    Batch geocode multiple addresses.

    Args:
        addresses: Array of address inputs

    Returns:
        Batch results
    """
    results = []
    concurrency_limit = int(os.getenv('BATCH_CONCURRENCY_LIMIT', 10))

    # Process in chunks to avoid overloading
    for i in range(0, len(addresses), concurrency_limit):
        chunk = addresses[i:i + concurrency_limit]

        # Process chunk concurrently
        chunk_results = await asyncio.gather(
            *[geocode(addr) for addr in chunk],
            return_exceptions=True
        )

        # Add index to results
        for idx, result in enumerate(chunk_results):
            if isinstance(result, Exception):
                results.append({
                    'index': i + idx,
                    'success': False,
                    'error': str(result)
                })
            else:
                result['index'] = i + idx
                results.append(result)

    successful = sum(1 for r in results if r.get('success') is not False)
    failed = len(results) - successful
    cache_hits = sum(1 for r in results if r.get('cached'))

    return {
        'results': results,
        'summary': {
            'total': len(results),
            'successful': successful,
            'failed': failed,
            'cache_hits': cache_hits
        }
    }


def get_stats() -> Dict[str, Any]:
    """
    Get geocoding statistics.

    Returns:
        Statistics
    """
    avg_response_time = (
        round(_stats['total_response_time_ms'] / _stats['total_requests'])
        if _stats['total_requests'] > 0 else 0
    )

    success_rate = (
        round(_stats['successful'] / _stats['total_requests'], 2)
        if _stats['total_requests'] > 0 else 0
    )

    return {
        'total_requests': _stats['total_requests'],
        'successful': _stats['successful'],
        'failed': _stats['failed'],
        'success_rate': success_rate,
        'cache_hits': _stats['cache_hits'],
        'tier_distribution': {
            'tier_1_pin': _stats['tier_1_pin'],
            'tier_2_municipal': _stats['tier_2_municipal'],
            'tier_3_nar': _stats['tier_3_nar']
        },
        'avg_response_time_ms': avg_response_time
    }


def reset_stats():
    """Reset statistics."""
    global _stats
    _stats = {
        'total_requests': 0,
        'successful': 0,
        'failed': 0,
        'tier_1_pin': 0,
        'tier_2_municipal': 0,
        'tier_3_nar': 0,
        'cache_hits': 0,
        'total_response_time_ms': 0
    }
