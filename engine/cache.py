"""
Redis cache service module.
Provides caching functionality with TTL support and statistics tracking.
Includes automatic reconnection and startup retry logic.
"""
import os
import json
import asyncio
import logging
import time
from typing import Optional, Dict, Any
from urllib.parse import urlsplit
from redis import asyncio as aioredis

logger = logging.getLogger('geocoder.cache')


# Cache configuration
CACHE_TTL_SECONDS = int(os.getenv('CACHE_TTL_SECONDS', 86400))  # 24 hours
NEGATIVE_CACHE_TTL_SECONDS = int(os.getenv('NEGATIVE_CACHE_TTL_SECONDS', 3600))  # 1 hour
REDIS_STARTUP_RETRIES = int(os.getenv('REDIS_STARTUP_RETRIES', 5))
REDIS_RETRY_DELAY = float(os.getenv('REDIS_RETRY_DELAY', 1.0))  # seconds

# Redis client
_client: Optional[aioredis.Redis] = None
_is_connected = False
# REDIS_URL supports redis:// and TLS rediss:// schemes, with or without
# inline credentials (e.g. rediss://:password@host:6380).
_redis_url = os.getenv('REDIS_URL', 'redis://redis:6379')
# REDIS_PASSWORD (if set) overrides any password embedded in REDIS_URL,
# so the URL can stay secret-free in compose files and logs.
_redis_password = os.getenv('REDIS_PASSWORD') or None
_last_reconnect_attempt = 0.0
_reconnect_cooldown = 5.0  # seconds


def _redacted_redis_url() -> str:
    """
    Return the Redis URL safe for logging: scheme, host and port only.
    Credentials (inline or via REDIS_PASSWORD) are shown as '***'.
    """
    try:
        parts = urlsplit(_redis_url)
        auth = '***@' if (parts.username or parts.password or _redis_password) else ''
        host = parts.hostname or ''
        port = f':{parts.port}' if parts.port else ''
        return f'{parts.scheme}://{auth}{host}{port}'
    except ValueError:
        return '<unparseable redis url>'

# Statistics
_stats = {
    'hits': 0,
    'misses': 0,
    'sets': 0,
    'errors': 0,
    'reconnections': 0
}


async def _try_connect() -> bool:
    """
    Attempt to establish Redis connection (single attempt).

    Returns:
        True if successful, False otherwise
    """
    global _client, _is_connected

    try:
        connect_kwargs = {
            'encoding': 'utf-8',
            'decode_responses': True,
            'socket_connect_timeout': 5,
            'socket_timeout': 5
        }

        # Explicit password wins over any credential embedded in the URL
        if _redis_password:
            connect_kwargs['password'] = _redis_password

        _client = aioredis.from_url(_redis_url, **connect_kwargs)

        # Test connection
        await _client.ping()
        _is_connected = True
        return True

    except Exception as error:
        _is_connected = False
        if _client:
            try:
                await _client.close()
            except Exception:
                pass
            _client = None
        raise error


async def connect():
    """
    Initialize Redis connection with retry logic and exponential backoff.

    Returns:
        True if connection successful, False otherwise
    """
    logger.info(f'Connecting to Redis: {_redacted_redis_url()}')

    for attempt in range(1, REDIS_STARTUP_RETRIES + 1):
        try:
            await _try_connect()
            logger.info(f'Redis connection established: {_redacted_redis_url()}')
            return True

        except Exception as error:
            if attempt < REDIS_STARTUP_RETRIES:
                delay = REDIS_RETRY_DELAY * (2 ** (attempt - 1))  # Exponential backoff
                logger.warning(f'Redis connection attempt {attempt}/{REDIS_STARTUP_RETRIES} failed: {error}')
                logger.info(f'Retrying in {delay:.1f}s...')
                await asyncio.sleep(delay)
            else:
                logger.error(f'Failed to connect to Redis after {REDIS_STARTUP_RETRIES} attempts: {error}')
                logger.warning('Service will continue without caching')

    return False


async def _ensure_connection() -> bool:
    """
    Ensure Redis connection is established, attempt reconnection if needed.
    Implements cooldown to avoid hammering Redis with reconnect attempts.

    Returns:
        True if connected, False otherwise
    """
    global _last_reconnect_attempt, _stats

    if _is_connected and _client:
        return True

    # Check cooldown period
    current_time = time.time()
    if current_time - _last_reconnect_attempt < _reconnect_cooldown:
        return False

    _last_reconnect_attempt = current_time

    # Attempt reconnection
    try:
        await _try_connect()
        _stats['reconnections'] += 1
        logger.info(f'Redis reconnected successfully: {_redacted_redis_url()}')
        return True
    except Exception as error:
        logger.warning(f'Redis reconnection failed: {error}')
        return False


async def get(key: str) -> Optional[Dict[str, Any]]:
    """
    Get value from cache with automatic reconnection.

    Args:
        key: Cache key

    Returns:
        Cached value or None
    """
    global _stats

    # Ensure connection (attempt reconnect if needed)
    if not await _ensure_connection():
        _stats['misses'] += 1
        return None

    try:
        value = await _client.get(key)
        if value:
            _stats['hits'] += 1
            return json.loads(value)
        else:
            _stats['misses'] += 1
            return None

    except Exception as error:
        logger.error(f'Cache get error: {error}')
        _stats['errors'] += 1

        # Mark as disconnected on error
        global _is_connected
        _is_connected = False

        return None


async def set(key: str, value: Dict[str, Any], ttl: int = CACHE_TTL_SECONDS) -> bool:
    """
    Set value in cache with automatic reconnection.

    Args:
        key: Cache key
        value: Value to cache
        ttl: Time to live in seconds (optional)

    Returns:
        Success status
    """
    global _stats

    # Ensure connection (attempt reconnect if needed)
    if not await _ensure_connection():
        return False

    try:
        serialized = json.dumps(value)
        await _client.setex(key, ttl, serialized)
        _stats['sets'] += 1
        return True

    except Exception as error:
        logger.error(f'Cache set error: {error}')
        _stats['errors'] += 1

        # Mark as disconnected on error
        global _is_connected
        _is_connected = False

        return False


async def set_negative(key: str) -> bool:
    """
    Set negative cache entry (address not found).

    Args:
        key: Cache key

    Returns:
        Success status
    """
    return await set(key, {'success': False, 'cached': True}, NEGATIVE_CACHE_TTL_SECONDS)


async def delete(key: str) -> bool:
    """
    Delete key from cache with automatic reconnection.

    Args:
        key: Cache key

    Returns:
        Success status
    """
    # Ensure connection (attempt reconnect if needed)
    if not await _ensure_connection():
        return False

    try:
        await _client.delete(key)
        return True

    except Exception as error:
        logger.error(f'Cache delete error: {error}')
        _stats['errors'] += 1

        # Mark as disconnected on error
        global _is_connected
        _is_connected = False

        return False


async def clear_pattern(pattern: str) -> int:
    """
    Clear all cache entries matching a pattern with automatic reconnection.

    Args:
        pattern: Key pattern (e.g., "geocode:*")

    Returns:
        Number of keys deleted
    """
    # Ensure connection (attempt reconnect if needed)
    if not await _ensure_connection():
        return 0

    try:
        deleted_count = 0
        cursor = 0

        while True:
            cursor, keys = await _client.scan(cursor, match=pattern, count=100)

            if keys:
                await _client.delete(*keys)
                deleted_count += len(keys)

            if cursor == 0:
                break

        return deleted_count

    except Exception as error:
        logger.error(f'Cache clear pattern error: {error}')
        _stats['errors'] += 1

        # Mark as disconnected on error
        global _is_connected
        _is_connected = False

        return 0


def get_stats() -> Dict[str, Any]:
    """
    Get cache statistics including reconnection attempts.

    Returns:
        Cache stats
    """
    total = _stats['hits'] + _stats['misses']
    hit_rate = _stats['hits'] / total if total > 0 else 0

    return {
        'connected': _is_connected,
        'hit_rate': round(hit_rate, 2),
        'total_hits': _stats['hits'],
        'total_misses': _stats['misses'],
        'total_sets': _stats['sets'],
        'total_errors': _stats['errors'],
        'total_reconnections': _stats['reconnections']
    }


async def test_connection() -> bool:
    """
    Test cache connection with automatic reconnection attempt.

    Returns:
        Connection status
    """
    # Try to ensure connection first
    if not await _ensure_connection():
        logger.warning('Redis not connected')
        return False

    try:
        await _client.ping()
        logger.info('Redis connection successful')
        return True

    except Exception as error:
        logger.error(f'Redis connection test failed: {error}')

        # Mark as disconnected on error
        global _is_connected
        _is_connected = False

        return False


def get_raw_client() -> Optional[aioredis.Redis]:
    """
    Get the underlying Redis client for callers that need direct access
    (e.g. rate limiting). Returns None if not currently connected.

    Returns:
        The Redis client, or None if not connected.
    """
    if not _is_connected:
        return None
    return _client


async def close():
    """Close Redis connection."""
    global _client, _is_connected

    if _client:
        await _client.close()
        logger.info('Redis connection closed')
        _client = None
        _is_connected = False
