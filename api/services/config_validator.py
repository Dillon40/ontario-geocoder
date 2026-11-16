"""
Configuration validator for startup checks.
Validates required environment variables and warns about insecure configurations.
"""
import os
import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)


class ConfigurationError(Exception):
    """Raised when required configuration is missing or invalid."""
    pass


def validate_environment() -> Tuple[bool, List[str], List[str]]:
    """
    Validate environment configuration at startup.

    Returns:
        Tuple of (is_valid, errors, warnings)
    """
    errors = []
    warnings = []

    # Environment mode (used by several checks below)
    env = os.getenv('ENV', 'production')

    # ==========================================================================
    # Required Environment Variables
    # ==========================================================================

    # Database password is required (no default)
    if not os.getenv('DB_PASSWORD'):
        errors.append(
            "DB_PASSWORD environment variable is required. "
            "Set it to your PostgreSQL password."
        )

    # Redis URL should be set for production
    redis_url = os.getenv('REDIS_URL')
    if not redis_url:
        warnings.append(
            "REDIS_URL not set. Using default redis://localhost:6379. "
            "Set this explicitly for production."
        )

    # ==========================================================================
    # Security Configuration Warnings
    # ==========================================================================

    # Check if API key authentication is disabled
    api_key_required = os.getenv('API_KEY_REQUIRED', 'true').lower()
    if api_key_required == 'false':
        warnings.append(
            "API_KEY_REQUIRED=false - API authentication is DISABLED. "
            "This should only be used for development/testing."
        )

    # Check CORS configuration
    allowed_origins = os.getenv('ALLOWED_ORIGINS', '')
    origins_list = [origin.strip() for origin in allowed_origins.split(',') if origin.strip()]
    wildcard_origins = not origins_list or '*' in origins_list

    if wildcard_origins:
        if env == 'production':
            # Hard startup error: wildcard CORS in production
            errors.append(
                "ALLOWED_ORIGINS is set to '*' or not configured while ENV=production. "
                "Wildcard CORS is not allowed in production. "
                "Set specific origins (e.g., 'https://example.com')."
            )
        else:
            warnings.append(
                "ALLOWED_ORIGINS is set to '*' or not configured. "
                "This allows requests from any origin. "
                "Set specific origins for production (e.g., 'https://example.com')."
            )

        # The API enables allow_credentials on its CORS middleware. Combined
        # with wildcard origins, any site could make credentialed requests.
        warnings.append(
            "Wildcard ALLOWED_ORIGINS combined with allow_credentials=True is "
            "an insecure CORS configuration: any origin can send credentialed "
            "requests. Set explicit origins."
        )

    # Check rate limiting
    rate_limit_enabled = os.getenv('RATE_LIMIT_ENABLED', 'true').lower()
    if rate_limit_enabled == 'false':
        warnings.append(
            "RATE_LIMIT_ENABLED=false - Rate limiting is DISABLED. "
            "This may expose the API to abuse."
        )

    # Check IP filtering
    ip_filter_enabled = os.getenv('IP_FILTER_ENABLED', 'true').lower()
    if ip_filter_enabled == 'false':
        warnings.append(
            "IP_FILTER_ENABLED=false - IP filtering is DISABLED. "
            "Per-key IP restrictions will not be enforced."
        )

    # Check fail-closed setting for rate limiting
    fail_closed = os.getenv('RATE_LIMIT_FAIL_CLOSED', 'true').lower()
    if fail_closed == 'false':
        warnings.append(
            "RATE_LIMIT_FAIL_CLOSED=false - Rate limiting will fail-open. "
            "If Redis is unavailable, all requests will be allowed."
        )

    # Check trusted proxy configuration (X-Forwarded-For handling)
    trusted_proxies = os.getenv('TRUSTED_PROXIES', '')
    if trusted_proxies:
        import ipaddress
        for entry in trusted_proxies.split(','):
            entry = entry.strip()
            if not entry:
                continue
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError:
                warnings.append(
                    f"TRUSTED_PROXIES contains invalid IP/CIDR '{entry}'. "
                    "It will be ignored by the IP filter."
                )
    else:
        warnings.append(
            "TRUSTED_PROXIES not set. X-Forwarded-For / X-Real-IP headers will "
            "be ignored and the direct socket peer address will be used. If the "
            "API sits behind a reverse proxy, set TRUSTED_PROXIES to the proxy's "
            "IP/CIDR so real client IPs are resolved."
        )

    # Check API key pepper (HMAC secret for key hashing)
    if not os.getenv('API_KEY_PEPPER'):
        if env == 'production':
            warnings.append(
                "API_KEY_PEPPER not set in production. New API keys will be "
                "hashed with plain SHA-256 instead of HMAC-SHA256. Set a strong "
                "random secret to protect key hashes if the database leaks."
            )
        else:
            warnings.append(
                "API_KEY_PEPPER not set. New API keys will be hashed with "
                "plain SHA-256 instead of HMAC-SHA256."
            )

    # ==========================================================================
    # Optional Checks
    # ==========================================================================

    # Check if running in development mode
    if env == 'development':
        warnings.append(
            f"ENV={env} - Running in development mode. "
            "Ensure this is not used in production."
        )

    # Determine validity
    is_valid = len(errors) == 0

    return is_valid, errors, warnings


def check_and_report() -> bool:
    """
    Validate configuration and report results.
    Exits the application if critical errors are found.

    Returns:
        True if configuration is valid
    """
    is_valid, errors, warnings = validate_environment()

    # Report warnings
    for warning in warnings:
        logger.warning(f"CONFIG WARNING: {warning}")

    # Report errors
    for error in errors:
        logger.error(f"CONFIG ERROR: {error}")

    if not is_valid:
        logger.critical(
            "Configuration validation failed. "
            "Please fix the errors above before starting the service."
        )
        return False

    if warnings:
        logger.info(
            f"Configuration validation passed with {len(warnings)} warning(s)."
        )
    else:
        logger.info("Configuration validation passed.")

    return True


async def bootstrap_initial_admin_key():
    """
    Create initial admin API key from environment variable if specified.
    This allows automated deployments to bootstrap without manual intervention.
    """
    initial_key = os.getenv('INITIAL_ADMIN_KEY')
    initial_key_name = os.getenv('INITIAL_ADMIN_KEY_NAME', 'Bootstrap Admin')
    initial_key_owner = os.getenv('INITIAL_ADMIN_KEY_OWNER', 'system')

    if not initial_key:
        return

    # Import here to avoid circular imports
    from engine import database
    from . import api_keys
    import json

    logger.info("INITIAL_ADMIN_KEY detected. Checking for existing admin keys...")

    try:
        # Check if any admin keys exist
        existing = await database.query(
            "SELECT COUNT(*) as count FROM api_keys WHERE permissions->>'admin' = 'true' AND is_active = true"
        )

        if existing[0]['count'] > 0:
            logger.info(
                f"Found {existing[0]['count']} existing admin key(s). "
                "Skipping initial key creation."
            )
            return

        # Validate key format
        if len(initial_key) < 32:
            logger.error(
                "INITIAL_ADMIN_KEY must be at least 32 characters. "
                "Skipping key creation."
            )
            return

        # Hash the key (same scheme as api_keys: HMAC-SHA256 when pepper is set)
        key_hash = api_keys._hash_key(initial_key)
        key_prefix = api_keys._get_key_prefix(initial_key)

        # Admin permissions
        permissions = {
            'geocode': True,
            'batch': True,
            'stats': True,
            'admin': True
        }

        # Check if this key already exists
        exists = await database.query(
            "SELECT id FROM api_keys WHERE key_hash = $1",
            [key_hash]
        )

        if exists:
            logger.info("Initial admin key already exists in database.")
            return

        # Insert the key
        await database.execute(
            '''
            INSERT INTO api_keys (
                key_hash, key_prefix, name, owner, permissions
            )
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (key_hash) DO NOTHING
            ''',
            [key_hash, key_prefix, initial_key_name, initial_key_owner, json.dumps(permissions)]
        )

        logger.info(
            f"Created initial admin API key '{initial_key_name}' "
            f"(prefix: {key_prefix})"
        )

        # Security: Clear the environment variable after use
        # Note: This only affects the current process, not the actual environment
        if 'INITIAL_ADMIN_KEY' in os.environ:
            del os.environ['INITIAL_ADMIN_KEY']
            logger.info("Cleared INITIAL_ADMIN_KEY from process environment.")

    except Exception as e:
        logger.error(f"Failed to create initial admin key: {e}")
        # Don't raise - this is a non-critical bootstrap operation
