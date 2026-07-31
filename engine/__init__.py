"""
Geocoder Engine Package

Core geocoding logic with 3-tier cascading strategy:
- Tier 1: PIN lookup
- Tier 2: Municipal dataset matching
- Tier 3: National Address Register (NAR)

Services:
- SourceHealthService: Data source health checking and change detection
"""
from . import database, cache, geocoder, services

__all__ = [
    'database',
    'cache',
    'geocoder',
    'services',
]
