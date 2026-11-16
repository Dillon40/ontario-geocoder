"""
Shared database utilities for data sourcing scripts.

Provides centralized database connection management to avoid duplication
across downloaders, importers, and management scripts.

Usage:
    from data_sourcing.db_utils import get_db_pool, get_db_connection, get_db_config

    # Async pool (preferred for high-throughput operations)
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.fetch("SELECT * FROM data_sources")
    await pool.close()

    # Single connection (for simple scripts)
    conn = await get_db_connection()
    await conn.fetch("SELECT * FROM data_sources")
    await conn.close()

    # SQLAlchemy connection string
    config = get_db_config()
    engine = create_engine(config['connection_string'])
"""
import os
from typing import Optional

import asyncpg


def get_db_config() -> dict:
    """
    Get database configuration from environment variables.

    Returns:
        dict with keys: host, port, database, user, password, connection_string
    """
    host = os.environ.get('DB_HOST', 'localhost')
    port = int(os.environ.get('DB_PORT', 5432))
    database = os.environ.get('DB_NAME', 'geocoder')
    user = os.environ.get('DB_USER', 'geocoder')
    password = os.environ.get('DB_PASSWORD')
    if not password:
        raise RuntimeError(
            'DB_PASSWORD is not set. Source .env (or otherwise export '
            'DB_PASSWORD) before running database scripts - there is no '
            'default password.'
        )

    return {
        'host': host,
        'port': port,
        'database': database,
        'user': user,
        'password': password,
        'connection_string': f'postgresql://{user}:{password}@{host}:{port}/{database}'
    }


async def ensure_source_import_allowed(conn, source_name: str) -> None:
    """Do not let direct imports/--force revive an explicitly blocked dataset.

    Match both the registry key and importer mapping key. Unregistered sources
    remain available to the dataset wizard; blocked/rejected registrations win.
    """
    rows = await conn.fetch('''
        SELECT source_key, status FROM data_sources
        WHERE source_key = $1 OR dataset_mapping_key = $1
    ''', source_name)
    for row in rows:
        if row['status'] in ('blocked', 'rejected'):
            raise RuntimeError(
                f"Source {row['source_key']} is {row['status']}; import is prohibited. "
                "--force and licence-consent flags do not override source exclusion.")


async def get_db_connection() -> asyncpg.Connection:
    """
    Create a single database connection.

    Use for simple scripts that need one connection. Remember to close it:
        conn = await get_db_connection()
        try:
            await conn.fetch(...)
        finally:
            await conn.close()

    Returns:
        asyncpg.Connection instance
    """
    config = get_db_config()
    return await asyncpg.connect(
        host=config['host'],
        port=config['port'],
        database=config['database'],
        user=config['user'],
        password=config['password']
    )


async def get_db_pool(
    min_size: int = 1,
    max_size: int = 10,
    command_timeout: Optional[int] = None
) -> asyncpg.Pool:
    """
    Create a database connection pool.

    Use for high-throughput operations that benefit from connection reuse.
    Remember to close the pool when done:
        pool = await get_db_pool()
        try:
            async with pool.acquire() as conn:
                await conn.fetch(...)
        finally:
            await pool.close()

    Args:
        min_size: Minimum number of connections to keep open
        max_size: Maximum number of connections in the pool
        command_timeout: Optional timeout in seconds for commands

    Returns:
        asyncpg.Pool instance
    """
    config = get_db_config()
    kwargs = {
        'host': config['host'],
        'port': config['port'],
        'database': config['database'],
        'user': config['user'],
        'password': config['password'],
        'min_size': min_size,
        'max_size': max_size,
    }
    if command_timeout:
        kwargs['command_timeout'] = command_timeout

    return await asyncpg.create_pool(**kwargs)


def get_psql_env() -> dict:
    """
    Get environment variables for psql subprocess calls.

    Use when running psql as a subprocess:
        result = subprocess.run(
            ['psql', '-h', host, ...],
            env=get_psql_env()
        )

    Returns:
        dict with current env plus PGPASSWORD set
    """
    config = get_db_config()
    return {
        **os.environ,
        'PGPASSWORD': config['password']
    }


def get_psql_args() -> list:
    """
    Get common psql command-line arguments.

    Returns:
        List of args: ['-h', host, '-p', port, '-U', user, '-d', database]
    """
    config = get_db_config()
    return [
        '-h', config['host'],
        '-p', str(config['port']),
        '-U', config['user'],
        '-d', config['database']
    ]
