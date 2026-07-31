"""
License information service.

Caches license and attribution info from the data_sources table (DB-driven,
so every seeded source resolves without a hardcoded per-source map) and maps
geocode source values to their corresponding license information.
"""
import logging
from typing import Optional, Dict, Any, List

from . import database

logger = logging.getLogger('geocoder.license')

# Cache for license info keyed by source_key (populated at startup)
_license_cache: Dict[str, Dict[str, Any]] = {}

# Reverse-lookup built from data_sources.dataset_mapping_key at startup:
# maps the source_dataset value the geocoder embeds in "municipal_<dataset>"
# (e.g. "toronto") to the data_sources.source_key (e.g. "toronto_addresses").
# Populated by init_license_cache() -- not hand-maintained, so a newly
# seeded municipal source resolves automatically without a code change.
_dataset_to_source_key: Dict[str, str] = {}

# Direct source value mappings for non-municipal sources, i.e. ones whose
# geocoder `source` value doesn't follow the "municipal_<dataset_mapping_key>"
# convention. NAR sets dataset_mapping_key to NULL in the seed data (it uses
# its own schema, not dataset-mappings.json), so it can't be derived the way
# municipal sources are and is kept as a small fixed map instead.
DIRECT_SOURCE_TO_KEY = {
    'nar_match': 'nar_ontario',
}


async def init_license_cache() -> None:
    """Load license/attribution info from database into cache at startup."""
    global _license_cache, _dataset_to_source_key

    try:
        results = await database.query('''
            SELECT source_key, source_name, status, dataset_mapping_key,
                   license_type, license_url, commercial_use_allowed,
                   required_attribution
            FROM data_sources
            WHERE license_type IS NOT NULL OR license_url IS NOT NULL
        ''')

        _license_cache.clear()
        _dataset_to_source_key.clear()

        for row in results:
            _license_cache[row['source_key']] = {
                'license_type': row['license_type'],
                'license_url': row['license_url'],
                'commercial_use_allowed': row['commercial_use_allowed'],
                'required_attribution': row['required_attribution'],
                'source_name': row['source_name'],
                'status': row['status'],
            }

            if row['dataset_mapping_key']:
                _dataset_to_source_key[row['dataset_mapping_key']] = row['source_key']

        logger.info(f'License cache loaded with {len(_license_cache)} entries')

    except Exception as e:
        logger.error(f'Failed to load license cache: {e}')
        # Don't raise - service can still function without license info


def get_source_key(source: str) -> Optional[str]:
    """
    Map a geocode source value to data_sources.source_key.

    Args:
        source: The source value from geocoding result (e.g., "municipal_toronto", "nar_match")

    Returns:
        The corresponding source_key for database lookup, or None if not found.

    Examples:
        >>> get_source_key('municipal_toronto')
        'toronto_addresses'
        >>> get_source_key('nar_match')
        'nar_ontario'
    """
    if not source:
        return None

    # Handle municipal_* format (e.g., "municipal_toronto" -> "toronto")
    if source.startswith('municipal_'):
        dataset = source[len('municipal_'):]
        return _dataset_to_source_key.get(dataset)

    # Direct lookup for other sources
    return DIRECT_SOURCE_TO_KEY.get(source)


def get_license_info(source: str) -> Optional[Dict[str, Any]]:
    """
    Get license information for a geocode source.

    Args:
        source: The source value from geocoding result

    Returns:
        Dict with license_type, license_url, commercial_use_allowed,
        required_attribution, or None if not found.
    """
    if not source:
        return None

    # Map source to source_key
    source_key = get_source_key(source)
    if not source_key:
        logger.debug(f'No source_key mapping for source: {source}')
        return None

    # Look up in cache
    license_info = _license_cache.get(source_key)
    if not license_info:
        logger.debug(f'No license info cached for source_key: {source_key}')
        return None

    return license_info.copy()


def get_attribution(source: str) -> Optional[str]:
    """
    Get just the required attribution statement for a geocode source.

    Args:
        source: The source value from geocoding result

    Returns:
        The required_attribution statement, or None if not found/not required.
    """
    license_info = get_license_info(source)
    if not license_info:
        return None
    return license_info.get('required_attribution')


def get_all_attributions() -> List[Dict[str, Any]]:
    """
    Get required attribution statements for every active (status='confirmed')
    data source, for the /v1/attributions endpoint.

    Returns:
        List of dicts with source_key, source_name, required_attribution,
        license_type, license_url -- one per confirmed source that has a
        required_attribution statement, sorted by source_key.
    """
    attributions = [
        {
            'source_key': source_key,
            'source_name': info.get('source_name'),
            'required_attribution': info.get('required_attribution'),
            'license_type': info.get('license_type'),
            'license_url': info.get('license_url'),
        }
        for source_key, info in _license_cache.items()
        if info.get('status') == 'confirmed' and info.get('required_attribution')
    ]
    return sorted(attributions, key=lambda a: a['source_key'])


def get_cache_stats() -> Dict[str, Any]:
    """Get statistics about the license cache."""
    return {
        'entries': len(_license_cache),
        'source_keys': list(_license_cache.keys()),
    }
