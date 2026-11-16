"""
Database service module for PostgreSQL + PostGIS.
Provides connection pool management and query execution.
"""
import os
import logging
import asyncpg
from datetime import datetime
from typing import Optional, List, Dict, Any

logger = logging.getLogger('geocoder.database')


# Database connection pool
_pool: Optional[asyncpg.Pool] = None


async def init_pool():
    """Initialize the database connection pool."""
    global _pool

    # Get password - required, no default for security
    db_password = os.getenv('DB_PASSWORD')
    if not db_password:
        raise ValueError(
            "DB_PASSWORD environment variable is required. "
            "Set it to your PostgreSQL password."
        )

    pool_config = {
        'host': os.getenv('DB_HOST', 'postgres'),
        'port': int(os.getenv('DB_PORT', 5432)),
        'database': os.getenv('DB_NAME', 'geocoder'),
        'user': os.getenv('DB_USER', 'geocoder'),
        'password': db_password,
        'min_size': int(os.getenv('MIN_DB_CONNECTIONS', 5)),
        'max_size': int(os.getenv('MAX_DB_CONNECTIONS', 20)),
        'command_timeout': 60
    }

    _pool = await asyncpg.create_pool(**pool_config)
    logger.info(f'Database connection pool established (host: {pool_config["host"]}:{pool_config["port"]})')
    return _pool


def get_pool() -> Optional[asyncpg.Pool]:
    """Get the current database pool."""
    return _pool


async def query(text: str, params: List[Any] = None) -> List[asyncpg.Record]:
    """
    Execute a query and return results.

    Args:
        text: SQL query string
        params: Query parameters

    Returns:
        List of records
    """
    if _pool is None:
        raise RuntimeError('Database pool not initialized')

    start = datetime.now()

    try:
        async with _pool.acquire() as conn:
            if params:
                result = await conn.fetch(text, *params)
            else:
                result = await conn.fetch(text)

            duration = (datetime.now() - start).total_seconds() * 1000

            if os.getenv('LOG_SQL_QUERIES', 'false').lower() == 'true':
                logger.debug(f'Executed query in {duration:.2f}ms, rows: {len(result)}')

            return result

    except Exception as error:
        logger.error(f'Database query error: {error}')
        raise


async def execute(text: str, params: List[Any] = None) -> str:
    """
    Execute a command (INSERT, UPDATE, DELETE) and return status.

    Args:
        text: SQL command string
        params: Command parameters

    Returns:
        Command status string
    """
    if _pool is None:
        raise RuntimeError('Database pool not initialized')

    try:
        async with _pool.acquire() as conn:
            if params:
                result = await conn.execute(text, *params)
            else:
                result = await conn.execute(text)
            return result

    except Exception as error:
        logger.error(f'Database execute error: {error}')
        raise


async def get_connection():
    """
    Get a connection from the pool for transactions.

    Returns:
        Database connection
    """
    if _pool is None:
        raise RuntimeError('Database pool not initialized')

    return await _pool.acquire()


async def release_connection(conn):
    """Release a connection back to the pool."""
    await _pool.release(conn)


async def test_connection() -> bool:
    """
    Test database connection.

    Returns:
        Connection status
    """
    try:
        result = await query('SELECT NOW() as current_time, version()')
        logger.info(f'Database connection successful: {result[0]["current_time"]}')
        return True
    except Exception as error:
        logger.error(f'Database connection failed: {error}')
        return False


async def get_stats() -> Dict[str, Any]:
    """
    Get database statistics.

    Returns:
        Database stats dictionary
    """
    try:
        # Get properties count
        properties_result = await query('SELECT COUNT(*) as count FROM properties WHERE NOT is_deleted')
        properties_count = properties_result[0]['count']

        # Get cache count
        cache_result = await query('SELECT COUNT(*) as count FROM geocode_cache')
        cache_count = cache_result[0]['count']

        # Get dataset counts
        dataset_result = await query('''
            SELECT source_dataset, COUNT(*) as count
            FROM properties
            WHERE source_dataset IS NOT NULL AND NOT is_deleted
            GROUP BY source_dataset
        ''')

        datasets = {row['source_dataset'] or 'unknown': row['count'] for row in dataset_result}

        # Check for NAR tables
        nar_address_count = 0
        nar_location_count = 0

        try:
            nar_addr = await query('SELECT COUNT(*) as count FROM nar_addresses WHERE NOT is_deleted')
            nar_address_count = nar_addr[0]['count']
        except Exception:
            # NAR tables don't exist yet
            pass

        try:
            nar_loc = await query('SELECT COUNT(*) as count FROM nar_locations')
            nar_location_count = nar_loc[0]['count']
        except Exception:
            # NAR tables don't exist yet
            pass

        return {
            'properties_total': properties_count,
            'cache_entries': cache_count,
            'datasets': datasets,
            'nar_addresses': nar_address_count,
            'nar_locations': nar_location_count
        }

    except Exception as error:
        logger.error(f'Error getting database stats: {error}')
        raise


async def close():
    """Close database pool (for graceful shutdown)."""
    global _pool

    if _pool:
        await _pool.close()
        logger.info('Database pool closed')
        _pool = None
