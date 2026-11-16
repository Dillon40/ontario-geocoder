#!/usr/bin/env python3
"""
Import history tracking utilities.

Provides an async context manager for recording import operations
with full audit trail including row counts, timing, and error logging.

Usage:
    async with ImportTracker(pool, 'toronto_addresses', file_path) as tracker:
        for batch in data:
            inserted = await import_batch(batch)
            tracker.stats.rows_inserted += inserted
            tracker.stats.rows_processed += len(batch)

        if error:
            await tracker.log_row_error(row_num, 'validation_error', str(e), row_data)
"""
import json
import traceback
from decimal import Decimal
from pathlib import Path
from datetime import datetime
from typing import Optional
from dataclasses import dataclass


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal types."""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)

import asyncpg

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from data_sourcing.db_utils import get_db_pool
from data_sourcing.importers.import_utils import FileHashManager


@dataclass
class ImportStats:
    """Accumulates import statistics during an import operation."""
    rows_processed: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_failed: int = 0
    rows_skipped: int = 0
    rows_deleted: int = 0  # Stale records soft-deleted during this import

    def to_dict(self) -> dict:
        return {
            'rows_processed': self.rows_processed,
            'rows_inserted': self.rows_inserted,
            'rows_updated': self.rows_updated,
            'rows_failed': self.rows_failed,
            'rows_skipped': self.rows_skipped,
            'rows_deleted': self.rows_deleted
        }

    def summary(self) -> str:
        """Return a human-readable summary of the import stats."""
        parts = []
        if self.rows_processed:
            parts.append(f"{self.rows_processed:,} processed")
        if self.rows_inserted:
            parts.append(f"{self.rows_inserted:,} inserted")
        if self.rows_updated:
            parts.append(f"{self.rows_updated:,} updated")
        if self.rows_deleted:
            parts.append(f"{self.rows_deleted:,} deleted")
        if self.rows_failed:
            parts.append(f"{self.rows_failed:,} failed")
        if self.rows_skipped:
            parts.append(f"{self.rows_skipped:,} skipped")
        return ", ".join(parts) if parts else "No rows processed"


class ImportTracker:
    """
    Async context manager for tracking import operations.

    Records import start/completion in import_history table,
    tracks row-level errors in import_row_errors table,
    and computes file hashes for integrity tracking.

    Args:
        pool: asyncpg connection pool
        source_key: identifier for the data source (e.g., 'toronto_addresses')
        file_path: path to the file being imported (optional)
        import_type: type of import - 'full', 'incremental', or 'partial'
        metadata: additional metadata to store as JSONB

    Attributes:
        stats: ImportStats instance for tracking row counts
        import_id: database ID of the import_history record
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        source_key: str,
        file_path: Optional[str] = None,
        import_type: str = 'full',
        metadata: Optional[dict] = None
    ):
        self.pool = pool
        self.source_key = source_key
        self.file_path = file_path
        self.import_type = import_type
        self.metadata = metadata or {}
        self.import_id: Optional[int] = None
        self.stats = ImportStats()
        self.start_time: Optional[datetime] = None
        self._error_batch: list = []
        self._error_batch_size = 100

    async def __aenter__(self) -> 'ImportTracker':
        self.start_time = datetime.now()

        # Calculate file hash if file provided
        file_hash = None
        file_size = None
        if self.file_path and Path(self.file_path).exists():
            file_size = Path(self.file_path).stat().st_size
            file_hash = FileHashManager.compute_file_hash(self.file_path)

        # Create import history record
        async with self.pool.acquire() as conn:
            self.import_id = await conn.fetchval("""
                INSERT INTO import_history
                    (source_key, import_type, status, file_path, file_hash, file_size_bytes, metadata)
                VALUES ($1, $2, 'started', $3, $4, $5, $6)
                RETURNING id
            """, self.source_key, self.import_type, self.file_path, file_hash, file_size,
               json.dumps(self.metadata))

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # Flush any remaining errors
        await self._flush_error_batch()

        duration = int((datetime.now() - self.start_time).total_seconds())

        if exc_type:
            status = 'failed'
            error_message = str(exc_val)
            error_tb = ''.join(traceback.format_tb(exc_tb))
        else:
            status = 'completed'
            error_message = None
            error_tb = None

        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE import_history SET
                    status = $2,
                    completed_at = NOW(),
                    duration_seconds = $3,
                    rows_processed = $4,
                    rows_inserted = $5,
                    rows_updated = $6,
                    rows_failed = $7,
                    rows_skipped = $8,
                    rows_deleted = $9,
                    error_message = $10,
                    error_traceback = $11
                WHERE id = $1
            """, self.import_id, status, duration,
               self.stats.rows_processed, self.stats.rows_inserted,
               self.stats.rows_updated, self.stats.rows_failed,
               self.stats.rows_skipped, self.stats.rows_deleted,
               error_message, error_tb)

        return False  # Don't suppress exceptions

    async def log_row_error(
        self,
        row_number: int,
        error_type: str,
        error_message: str,
        row_data: Optional[dict] = None,
        *, count_failure: bool = True
    ) -> None:
        """
        Log a row-level error (batched for performance).

        Args:
            row_number: line/row number in the source file
            error_type: category of error (e.g., 'validation_error', 'parse_error')
            error_message: description of the error
            row_data: original row data for debugging (optional)
            count_failure: False only for explicitly excluded non-address source features;
                           these remain audited and increment rows_skipped instead.
        """
        self._error_batch.append({
            'import_id': self.import_id,
            'row_number': row_number,
            'error_type': error_type,
            'error_message': error_message,
            'row_data': json.dumps(row_data, cls=DecimalEncoder) if row_data else None
        })
        if count_failure:
            self.stats.rows_failed += 1
        else:
            self.stats.rows_skipped += 1

        if len(self._error_batch) >= self._error_batch_size:
            await self._flush_error_batch()

    async def _flush_error_batch(self) -> None:
        """Write batched errors to database."""
        if not self._error_batch:
            return

        async with self.pool.acquire() as conn:
            await conn.executemany("""
                INSERT INTO import_row_errors
                    (import_id, row_number, error_type, error_message, row_data)
                VALUES ($1, $2, $3, $4, $5::jsonb)
            """, [(e['import_id'], e['row_number'], e['error_type'],
                  e['error_message'], e['row_data']) for e in self._error_batch])

        self._error_batch = []


async def get_latest_import(
    pool: asyncpg.Pool,
    source_key: str
) -> Optional[dict]:
    """
    Get the most recent import for a source.

    Returns dict with import details or None if no imports exist.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id, source_key, import_type, status,
                   started_at, completed_at, duration_seconds,
                   rows_processed, rows_inserted, rows_updated,
                   rows_failed, rows_skipped, rows_deleted,
                   file_path, file_hash, error_message
            FROM import_history
            WHERE source_key = $1
            ORDER BY started_at DESC
            LIMIT 1
        """, source_key)

    if row:
        return dict(row)
    return None


async def get_import_errors(
    pool: asyncpg.Pool,
    import_id: int,
    limit: int = 100
) -> list[dict]:
    """
    Get row-level errors for an import.

    Args:
        pool: database connection pool
        import_id: ID of the import_history record
        limit: maximum number of errors to return

    Returns list of error dictionaries.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT row_number, error_type, error_message, row_data, created_at
            FROM import_row_errors
            WHERE import_id = $1
            ORDER BY row_number
            LIMIT $2
        """, import_id, limit)

    return [dict(row) for row in rows]


class SyncImportTracker:
    """
    Synchronous context manager for import tracking.

    Use this for synchronous import scripts (e.g., geopandas/SQLAlchemy).
    Creates an asyncpg pool internally and wraps async ImportTracker.

    Usage:
        with SyncImportTracker('fsa_boundaries', file_path) as tracker:
            # Do import work
            tracker.rows_inserted = 100
            tracker.rows_processed = 100
    """

    def __init__(
        self,
        source_key: str,
        file_path: Optional[str] = None,
        import_type: str = 'full',
        metadata: Optional[dict] = None
    ):
        self.source_key = source_key
        self.file_path = file_path
        self.import_type = import_type
        self.metadata = metadata or {}
        self.import_id: Optional[int] = None
        self.rows_processed: int = 0
        self.rows_inserted: int = 0
        self.rows_updated: int = 0
        self.rows_failed: int = 0
        self.rows_skipped: int = 0
        self.rows_deleted: int = 0
        self._start_time: Optional[datetime] = None
        self._pool = None
        self._error: Optional[Exception] = None
        self._error_tb: Optional[str] = None

    def __enter__(self) -> 'SyncImportTracker':
        import asyncio
        self._start_time = datetime.now()

        # Create a single event loop to reuse in __exit__.
        # Using asyncio.run() in both __enter__ and __exit__ creates separate
        # loops, and the pool from __enter__'s loop fails in __exit__'s loop.
        self._loop = asyncio.new_event_loop()
        self._loop.run_until_complete(self._start())
        return self

    async def _start(self):
        self._pool = await get_db_pool(min_size=1, max_size=2)

        # Calculate file hash if file provided
        file_hash = None
        file_size = None
        if self.file_path and Path(self.file_path).exists():
            file_size = Path(self.file_path).stat().st_size
            file_hash = FileHashManager.compute_file_hash(self.file_path)

        async with self._pool.acquire() as conn:
            self.import_id = await conn.fetchval("""
                INSERT INTO import_history
                    (source_key, import_type, status, file_path, file_hash, file_size_bytes, metadata)
                VALUES ($1, $2, 'started', $3, $4, $5, $6)
                RETURNING id
            """, self.source_key, self.import_type, self.file_path, file_hash, file_size,
               json.dumps(self.metadata))

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration = int((datetime.now() - self._start_time).total_seconds())

        if exc_type:
            status = 'failed'
            error_message = str(exc_val)
            error_tb = ''.join(traceback.format_tb(exc_tb))
        else:
            status = 'completed'
            error_message = None
            error_tb = None

        try:
            self._loop.run_until_complete(
                self._finish(status, duration, error_message, error_tb)
            )
        finally:
            self._loop.close()
        return False  # Don't suppress exceptions

    async def _finish(self, status, duration, error_message, error_tb):
        async with self._pool.acquire() as conn:
            await conn.execute("""
                UPDATE import_history SET
                    status = $2,
                    completed_at = NOW(),
                    duration_seconds = $3,
                    rows_processed = $4,
                    rows_inserted = $5,
                    rows_updated = $6,
                    rows_failed = $7,
                    rows_skipped = $8,
                    rows_deleted = $9,
                    error_message = $10,
                    error_traceback = $11
                WHERE id = $1
            """, self.import_id, status, duration,
               self.rows_processed, self.rows_inserted,
               self.rows_updated, self.rows_failed,
               self.rows_skipped, self.rows_deleted,
               error_message, error_tb)
        await self._pool.close()
