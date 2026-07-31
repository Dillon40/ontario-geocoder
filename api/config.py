"""
Configuration module for loading JSON configs and security settings.
"""
import os
import json
from pathlib import Path
from typing import Dict, Any, List


# Get config directory
CONFIG_DIR = Path(__file__).parent.parent / 'config'


def load_json_config(filename: str) -> Dict[str, Any]:
    """
    Load a JSON configuration file.

    Args:
        filename: Name of the config file

    Returns:
        Configuration dictionary
    """
    config_path = CONFIG_DIR / filename
    with open(config_path, 'r') as f:
        return json.load(f)


# Load all configuration files
dataset_mappings = load_json_config('dataset-mappings.json')
street_type_mappings = load_json_config('street-type-mappings.json')
direction_mappings = load_json_config('direction-mappings.json')


# =============================================================================
# Security Configuration
# =============================================================================

class SecurityConfig:
    """Security-related configuration settings."""

    # Authentication
    API_KEY_REQUIRED: bool = os.getenv('API_KEY_REQUIRED', 'true').lower() == 'true'
    API_KEY_HEADER: str = os.getenv('API_KEY_HEADER', 'X-API-Key')
    API_KEY_PREFIX: str = os.getenv('API_KEY_PREFIX', 'gc_')
    API_KEY_CACHE_TTL: int = int(os.getenv('API_KEY_CACHE_TTL', 300))

    # Rate Limiting
    RATE_LIMIT_ENABLED: bool = os.getenv('RATE_LIMIT_ENABLED', 'true').lower() == 'true'
    RATE_LIMIT_REQUESTS: int = int(os.getenv('RATE_LIMIT_REQUESTS', 100))
    RATE_LIMIT_WINDOW: int = int(os.getenv('RATE_LIMIT_WINDOW', 60))
    RATE_LIMIT_BURST: int = int(os.getenv('RATE_LIMIT_BURST', 20))

    # IP Filtering
    IP_FILTER_ENABLED: bool = os.getenv('IP_FILTER_ENABLED', 'true').lower() == 'true'

    # CORS
    @staticmethod
    def get_allowed_origins() -> List[str]:
        """
        Get list of allowed CORS origins.

        SECURITY: Defaults to empty list (no origins allowed).
        Set ALLOWED_ORIGINS environment variable for production.
        Use '*' only for development/testing.

        Examples:
            ALLOWED_ORIGINS=https://example.com,https://app.example.com
            ALLOWED_ORIGINS=*  (allows all - development only!)
        """
        origins = os.getenv('ALLOWED_ORIGINS', '')
        if not origins:
            return []
        if origins == '*':
            return ['*']
        return [o.strip() for o in origins.split(',') if o.strip()]

    # Security Headers
    SECURE_HEADERS_ENABLED: bool = os.getenv('SECURE_HEADERS_ENABLED', 'true').lower() == 'true'

    # Request ID
    REQUEST_ID_HEADER: str = os.getenv('REQUEST_ID_HEADER', 'X-Request-ID')


# Create singleton instance
security_config = SecurityConfig()
