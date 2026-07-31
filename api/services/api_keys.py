"""
API Key management service.
Provides CRUD operations for API keys with secure hashing.
"""
import os
import secrets
import hashlib
import hmac
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any

from engine import database, cache

logger = logging.getLogger('geocoder.api_keys')


# Configuration
API_KEY_PREFIX = os.getenv('API_KEY_PREFIX', 'gc_')
API_KEY_CACHE_TTL = int(os.getenv('API_KEY_CACHE_TTL', 300))  # 5 minutes

# Server-side secret mixed into key hashes (HMAC-SHA256). With a pepper set,
# a leaked database dump alone is not enough to forge or brute-force keys.
API_KEY_PEPPER = os.getenv('API_KEY_PEPPER', '')


def _generate_key() -> str:
    """
    Generate a secure API key with prefix.

    Returns:
        API key string (e.g., gc_abc123...)
    """
    random_bytes = secrets.token_hex(24)  # 48 character hex string
    return f"{API_KEY_PREFIX}{random_bytes}"


def _hash_key(key: str) -> str:
    """
    Hash API key for storage. Uses HMAC-SHA256 with the API_KEY_PEPPER
    secret when set; falls back to plain SHA-256 when no pepper is
    configured (legacy behavior).

    Args:
        key: Plain text API key

    Returns:
        Hexadecimal hash string
    """
    if API_KEY_PEPPER:
        return hmac.new(API_KEY_PEPPER.encode(), key.encode(), hashlib.sha256).hexdigest()
    return _legacy_hash_key(key)


def _legacy_hash_key(key: str) -> str:
    """
    Legacy plain SHA-256 hash. Kept as a validation fallback so keys
    created before the HMAC pepper was introduced keep working.

    Args:
        key: Plain text API key

    Returns:
        Hexadecimal hash string
    """
    return hashlib.sha256(key.encode()).hexdigest()


def _candidate_hashes(key: str) -> List[str]:
    """
    All hashes a key may be stored under: HMAC (current) first, then
    legacy SHA-256 if a pepper is configured.

    Args:
        key: Plain text API key

    Returns:
        List of hexadecimal hash strings (1 or 2 entries)
    """
    hashes = [_hash_key(key)]
    legacy = _legacy_hash_key(key)
    if legacy not in hashes:
        hashes.append(legacy)
    return hashes


def _get_key_prefix(key: str) -> str:
    """
    Extract a short prefix for identification: 'gc_' + 4 characters
    (API_KEY_PREFIX length + 4). Kept short so the prefix leaks as
    little of the key as possible.

    Args:
        key: Plain text API key

    Returns:
        Key prefix string
    """
    prefix_len = len(API_KEY_PREFIX) + 4
    return key[:prefix_len] if len(key) >= prefix_len else key


async def create_key(
    name: str,
    owner: Optional[str] = None,
    expires_at: Optional[datetime] = None,
    rate_limit_override: Optional[int] = None,
    allowed_ips: Optional[List[str]] = None,
    permissions: Optional[Dict[str, bool]] = None
) -> Dict[str, Any]:
    """
    Create a new API key.

    Args:
        name: Human-readable name for the key
        owner: Owner identifier (email, username, etc.)
        expires_at: Optional expiration datetime
        rate_limit_override: Optional custom rate limit
        allowed_ips: Optional list of allowed IP addresses/CIDRs
        permissions: Optional permissions dict

    Returns:
        Dictionary with key details including the plain key (only returned once)
    """
    # Generate key
    plain_key = _generate_key()
    key_hash = _hash_key(plain_key)
    key_prefix = _get_key_prefix(plain_key)

    # Default permissions
    default_permissions = {
        'geocode': True,
        'batch': True,
        'stats': False,
        'admin': False
    }

    if permissions:
        default_permissions.update(permissions)

    # Insert into database
    import json

    query = '''
        INSERT INTO api_keys (
            key_hash, key_prefix, name, owner, expires_at,
            rate_limit_override, allowed_ips, permissions
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id, created_at
    '''

    params = [
        key_hash,
        key_prefix,
        name,
        owner,
        expires_at,
        rate_limit_override,
        allowed_ips,
        json.dumps(default_permissions)
    ]

    result = await database.query(query, params)

    return {
        'id': result[0]['id'],
        'key': plain_key,  # Only returned at creation time
        'key_prefix': key_prefix,
        'name': name,
        'owner': owner,
        'created_at': result[0]['created_at'].isoformat(),
        'expires_at': expires_at.isoformat() if expires_at else None,
        'rate_limit_override': rate_limit_override,
        'allowed_ips': allowed_ips or [],
        'permissions': default_permissions,
        'is_active': True
    }


async def validate_key(key: str) -> Optional[Dict[str, Any]]:
    """
    Validate an API key and return its details.
    Uses Redis cache for performance.

    Args:
        key: Plain text API key

    Returns:
        Key details dict if valid, None otherwise
    """
    # Try current (HMAC) hash first, then legacy SHA-256 fallback
    candidate_hashes = _candidate_hashes(key)

    # Check cache first
    for key_hash in candidate_hashes:
        cache_key = f"api_key:{key_hash}"
        cached = await cache.get(cache_key)
        if cached:
            # Verify key is still active
            if cached.get('is_active') and not _is_expired(cached.get('expires_at')):
                return cached
            else:
                # Invalidate cache if key is no longer valid
                await cache.delete(cache_key)
                return None

    # Query database
    query = '''
        SELECT id, key_hash, key_prefix, name, owner, created_at, expires_at,
               last_used_at, is_active, rate_limit_override, allowed_ips, permissions
        FROM api_keys
        WHERE key_hash = $1
    '''

    row = None
    matched_hash = None
    for key_hash in candidate_hashes:
        result = await database.query(query, [key_hash])
        # Constant-time comparison against the stored hash
        if result and hmac.compare_digest(result[0]['key_hash'], key_hash):
            row = result[0]
            matched_hash = key_hash
            break

    if row is None:
        return None

    # Check if active and not expired
    if not row['is_active']:
        return None

    if row['expires_at'] and datetime.now() > row['expires_at']:
        return None

    # Build response
    import json

    key_data = {
        'id': row['id'],
        'key_prefix': row['key_prefix'],
        'name': row['name'],
        'owner': row['owner'],
        'created_at': row['created_at'].isoformat() if row['created_at'] else None,
        'expires_at': row['expires_at'].isoformat() if row['expires_at'] else None,
        'last_used_at': row['last_used_at'].isoformat() if row['last_used_at'] else None,
        'is_active': row['is_active'],
        'rate_limit_override': row['rate_limit_override'],
        'allowed_ips': row['allowed_ips'] or [],
        'permissions': json.loads(row['permissions']) if isinstance(row['permissions'], str) else row['permissions']
    }

    # Cache the result under the stored hash (matches revoke/update invalidation)
    await cache.set(f"api_key:{matched_hash}", key_data, API_KEY_CACHE_TTL)

    # Update last_used_at (fire and forget)
    await _update_last_used(row['id'])

    return key_data


def _is_expired(expires_at: Optional[str]) -> bool:
    """Check if a key is expired based on ISO format string."""
    if not expires_at:
        return False

    try:
        expiry = datetime.fromisoformat(expires_at)
        return datetime.now() > expiry
    except Exception:
        return False


async def _update_last_used(key_id: int):
    """Update the last_used_at timestamp for a key."""
    try:
        await database.execute(
            'UPDATE api_keys SET last_used_at = NOW() WHERE id = $1',
            [key_id]
        )
    except Exception as error:
        # Non-critical, just log
        logger.warning(f'Failed to update last_used_at for key {key_id}: {error}')


async def get_key_by_id(key_id: int) -> Optional[Dict[str, Any]]:
    """
    Get API key details by ID (without the actual key).

    Args:
        key_id: Key database ID

    Returns:
        Key details or None
    """
    query = '''
        SELECT id, key_prefix, name, owner, created_at, expires_at,
               last_used_at, is_active, rate_limit_override, allowed_ips, permissions
        FROM api_keys
        WHERE id = $1
    '''

    result = await database.query(query, [key_id])

    if not result:
        return None

    row = result[0]
    import json

    return {
        'id': row['id'],
        'key_prefix': row['key_prefix'],
        'name': row['name'],
        'owner': row['owner'],
        'created_at': row['created_at'].isoformat() if row['created_at'] else None,
        'expires_at': row['expires_at'].isoformat() if row['expires_at'] else None,
        'last_used_at': row['last_used_at'].isoformat() if row['last_used_at'] else None,
        'is_active': row['is_active'],
        'rate_limit_override': row['rate_limit_override'],
        'allowed_ips': row['allowed_ips'] or [],
        'permissions': json.loads(row['permissions']) if isinstance(row['permissions'], str) else row['permissions']
    }


async def list_keys(
    include_inactive: bool = False,
    limit: int = 100,
    offset: int = 0
) -> List[Dict[str, Any]]:
    """
    List all API keys (without actual key values).

    Args:
        include_inactive: Whether to include inactive keys
        limit: Maximum number of results
        offset: Pagination offset

    Returns:
        List of key details
    """
    if include_inactive:
        query = '''
            SELECT id, key_prefix, name, owner, created_at, expires_at,
                   last_used_at, is_active, rate_limit_override, allowed_ips, permissions
            FROM api_keys
            ORDER BY created_at DESC
            LIMIT $1 OFFSET $2
        '''
        params = [limit, offset]
    else:
        query = '''
            SELECT id, key_prefix, name, owner, created_at, expires_at,
                   last_used_at, is_active, rate_limit_override, allowed_ips, permissions
            FROM api_keys
            WHERE is_active = true
            ORDER BY created_at DESC
            LIMIT $1 OFFSET $2
        '''
        params = [limit, offset]

    result = await database.query(query, params)

    import json
    keys = []
    for row in result:
        keys.append({
            'id': row['id'],
            'key_prefix': row['key_prefix'],
            'name': row['name'],
            'owner': row['owner'],
            'created_at': row['created_at'].isoformat() if row['created_at'] else None,
            'expires_at': row['expires_at'].isoformat() if row['expires_at'] else None,
            'last_used_at': row['last_used_at'].isoformat() if row['last_used_at'] else None,
            'is_active': row['is_active'],
            'rate_limit_override': row['rate_limit_override'],
            'allowed_ips': row['allowed_ips'] or [],
            'permissions': json.loads(row['permissions']) if isinstance(row['permissions'], str) else row['permissions']
        })

    return keys


async def revoke_key(key_id: int) -> bool:
    """
    Revoke (deactivate) an API key.

    Args:
        key_id: Key database ID

    Returns:
        True if revoked, False if not found
    """
    # Get key hash first to invalidate cache
    result = await database.query(
        'SELECT key_hash FROM api_keys WHERE id = $1',
        [key_id]
    )

    if not result:
        return False

    key_hash = result[0]['key_hash']

    # Deactivate
    await database.execute(
        'UPDATE api_keys SET is_active = false WHERE id = $1',
        [key_id]
    )

    # Invalidate cache
    await cache.delete(f"api_key:{key_hash}")

    return True


async def update_key(
    key_id: int,
    name: Optional[str] = None,
    owner: Optional[str] = None,
    expires_at: Optional[datetime] = None,
    rate_limit_override: Optional[int] = None,
    allowed_ips: Optional[List[str]] = None,
    permissions: Optional[Dict[str, bool]] = None,
    is_active: Optional[bool] = None
) -> Optional[Dict[str, Any]]:
    """
    Update an API key's settings.

    Args:
        key_id: Key database ID
        Other args: Fields to update (None means no change)

    Returns:
        Updated key details or None if not found
    """
    # Get current key to find hash for cache invalidation
    result = await database.query(
        'SELECT key_hash FROM api_keys WHERE id = $1',
        [key_id]
    )

    if not result:
        return None

    key_hash = result[0]['key_hash']

    # Build dynamic UPDATE query
    updates = []
    params = []
    param_count = 0

    if name is not None:
        param_count += 1
        updates.append(f'name = ${param_count}')
        params.append(name)

    if owner is not None:
        param_count += 1
        updates.append(f'owner = ${param_count}')
        params.append(owner)

    if expires_at is not None:
        param_count += 1
        updates.append(f'expires_at = ${param_count}')
        params.append(expires_at)

    if rate_limit_override is not None:
        param_count += 1
        updates.append(f'rate_limit_override = ${param_count}')
        params.append(rate_limit_override)

    if allowed_ips is not None:
        param_count += 1
        updates.append(f'allowed_ips = ${param_count}')
        params.append(allowed_ips)

    if permissions is not None:
        param_count += 1
        updates.append(f'permissions = ${param_count}')
        import json
        params.append(json.dumps(permissions))

    if is_active is not None:
        param_count += 1
        updates.append(f'is_active = ${param_count}')
        params.append(is_active)

    if not updates:
        return await get_key_by_id(key_id)

    param_count += 1
    params.append(key_id)

    query = f'''
        UPDATE api_keys
        SET {', '.join(updates)}
        WHERE id = ${param_count}
    '''

    await database.execute(query, params)

    # Invalidate cache
    await cache.delete(f"api_key:{key_hash}")

    return await get_key_by_id(key_id)


async def log_admin_action(
    admin_key_id: int,
    admin_key_name: str,
    action: str,
    target_type: str = None,
    target_id: int = None,
    target_name: str = None,
    details: Dict[str, Any] = None,
    client_ip: str = None,
    request_id: str = None
):
    """
    Log an administrative action for audit purposes.

    Args:
        admin_key_id: ID of the admin API key performing the action
        admin_key_name: Name of the admin API key
        action: Action performed (e.g., 'create_key', 'revoke_key', 'update_key')
        target_type: Type of target (e.g., 'api_key')
        target_id: ID of the target
        target_name: Name of the target
        details: Additional details as JSON
        client_ip: Client IP address
        request_id: Request ID for correlation
    """
    try:
        import json

        query = '''
            INSERT INTO admin_audit_log (
                admin_key_id, admin_key_name, action, target_type,
                target_id, target_name, details, client_ip, request_id
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::inet, $9)
        '''

        await database.execute(
            query,
            [
                admin_key_id,
                admin_key_name,
                action,
                target_type,
                target_id,
                target_name,
                json.dumps(details) if details else None,
                client_ip,
                request_id
            ]
        )

        logger.info(
            f"Audit: {action} by {admin_key_name} (ID: {admin_key_id}) "
            f"on {target_type}:{target_id}"
        )

    except Exception as error:
        # Non-critical - don't fail the operation, just log the error
        logger.error(f"Failed to log admin action: {error}")


async def get_usage_stats(key_id: int, days: int = 30) -> Dict[str, Any]:
    """
    Get usage statistics for an API key.

    Args:
        key_id: Key database ID
        days: Number of days to look back

    Returns:
        Usage statistics
    """
    query = '''
        SELECT
            COUNT(*) as total_requests,
            SUM(request_count) as total_geocodes,
            AVG(response_time_ms) as avg_response_time,
            MIN(date) as first_use,
            MAX(date) as last_use
        FROM api_usage_log
        WHERE api_key_id = $1 AND date >= CURRENT_DATE - $2
    '''

    result = await database.query(query, [key_id, days])

    if not result or not result[0]['total_requests']:
        return {
            'total_requests': 0,
            'total_geocodes': 0,
            'avg_response_time_ms': 0,
            'period_days': days
        }

    row = result[0]
    return {
        'total_requests': row['total_requests'],
        'total_geocodes': row['total_geocodes'] or 0,
        'avg_response_time_ms': round(float(row['avg_response_time'] or 0), 2),
        'first_use': row['first_use'].isoformat() if row['first_use'] else None,
        'last_use': row['last_use'].isoformat() if row['last_use'] else None,
        'period_days': days
    }
