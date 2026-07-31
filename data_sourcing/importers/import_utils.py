#!/usr/bin/env python3
"""
Import utilities for geocoder data importers.

Provides shared functionality for:
- File hash checking to skip re-importing identical files
- Stale record detection and soft delete
- Consistent property ID generation across sources

Usage:
    from import_utils import FileHashManager, StaleRecordDetector, PropertyIdGenerator

    # Check if file should be skipped
    should_skip, reason = await FileHashManager.should_skip_file(pool, 'source_key', '/path/to/file')

    # Track and detect stale records
    detector = StaleRecordDetector(pool, import_id, 'source_key', 'properties', 'property_id')
    for record in records:
        detector.mark_seen(record['property_id'])
    stale_ids = await detector.detect_stale_records()
    deleted = await detector.soft_delete_stale_records(stale_ids)

    # Generate consistent property IDs
    property_id = PropertyIdGenerator.generate('toronto', record, row_number)
"""
import hashlib
import os
from typing import Optional
from pathlib import Path

import asyncpg

# Safety threshold for stale-record soft deletes. If the import file yields
# fewer seen IDs than this fraction of the source's currently-active records,
# the file is assumed to be truncated/partial and stale deletion is ABORTED
# (nothing is soft-deleted). Without this guard, a partial download would
# soft-delete most of a source's live rows and report success.
# Override via the STALE_DELETE_MIN_RATIO env var or the min_seen_ratio
# argument to StaleRecordDetector.
DEFAULT_STALE_DELETE_MIN_RATIO = 0.90


class StaleDeletionAbortedError(RuntimeError):
    """Raised when stale deletion is aborted by the safety threshold.

    Indicates the import file contained suspiciously few records compared
    to the database (likely a truncated or partial download). No records
    were soft-deleted. Callers should treat this as an import failure.
    """


class FileHashManager:
    """
    Manages file hash checking to skip re-importing identical files.

    Computes SHA256 hash of file and compares against the last successful
    import's hash stored in import_history table.
    """

    @staticmethod
    def compute_file_hash(file_path: str, chunk_size: int = 8192) -> str:
        """
        Compute SHA256 hash of a file.

        Args:
            file_path: Path to the file
            chunk_size: Size of chunks to read (default 8KB)

        Returns:
            SHA256 hex digest
        """
        sha256 = hashlib.sha256()
        path = Path(file_path)

        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        with open(path, 'rb') as f:
            while chunk := f.read(chunk_size):
                sha256.update(chunk)

        return sha256.hexdigest()

    @staticmethod
    async def should_skip_file(
        pool: asyncpg.Pool,
        source_key: str,
        file_path: str
    ) -> tuple[bool, Optional[str]]:
        """
        Check if file should be skipped based on hash comparison.

        Compares current file's hash against the hash from the last
        successful import for this source.

        Args:
            pool: Database connection pool
            source_key: Data source identifier (e.g., 'toronto_addresses')
            file_path: Path to the file being imported

        Returns:
            Tuple of (should_skip, reason)
            - (True, "File unchanged since...") if file hash matches last import
            - (False, None) if file should be imported
        """
        try:
            current_hash = FileHashManager.compute_file_hash(file_path)
        except FileNotFoundError:
            return False, None  # Can't skip if file doesn't exist

        # Get last successful import for this source
        async with pool.acquire() as conn:
            last_import = await conn.fetchrow("""
                SELECT id, file_hash, completed_at
                FROM import_history
                WHERE source_key = $1
                  AND status = 'completed'
                  AND file_hash IS NOT NULL
                ORDER BY completed_at DESC
                LIMIT 1
            """, source_key)

        if last_import and last_import['file_hash'] == current_hash:
            completed = last_import['completed_at']
            return True, f"File unchanged since {completed.strftime('%Y-%m-%d %H:%M:%S')}"

        return False, None

    @staticmethod
    async def get_file_size(file_path: str) -> int:
        """Get file size in bytes."""
        return Path(file_path).stat().st_size


class StaleRecordDetector:
    """
    Detects records in the database that are missing from a new import file.

    Tracks which record IDs are seen during import processing, then compares
    against existing records in the database to find stale ones.

    Supports soft delete: marking records as is_deleted=True rather than
    removing them, preserving history and allowing recovery.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        import_id: int,
        source_key: str,
        table_name: str,
        id_column: str,
        min_seen_ratio: Optional[float] = None
    ):
        """
        Initialize the stale record detector.

        Args:
            pool: Database connection pool
            import_id: Current import's ID from import_history
            source_key: Data source identifier (used to filter existing records)
            table_name: Database table name (e.g., 'properties', 'nar_addresses')
            id_column: Column name for record ID (e.g., 'property_id', 'addr_guid')
            min_seen_ratio: Safety threshold for soft deletes. If the import
                file yields fewer seen IDs than this fraction of the current
                active count, soft_delete_stale_records raises
                StaleDeletionAbortedError instead of deleting. Defaults to
                the STALE_DELETE_MIN_RATIO env var, falling back to
                DEFAULT_STALE_DELETE_MIN_RATIO (0.90).
        """
        self.pool = pool
        self.import_id = import_id
        self.source_key = source_key
        self.table_name = table_name
        self.id_column = id_column
        self._seen_ids: set[str] = set()
        self._active_count: Optional[int] = None
        if min_seen_ratio is None:
            min_seen_ratio = float(os.getenv(
                'STALE_DELETE_MIN_RATIO', DEFAULT_STALE_DELETE_MIN_RATIO))
        self.min_seen_ratio = min_seen_ratio

    def mark_seen(self, record_id: str) -> None:
        """
        Mark a record as seen in the current import.

        Call this for each record successfully processed from the import file.

        Args:
            record_id: The unique identifier of the record
        """
        if record_id:
            self._seen_ids.add(str(record_id))

    @property
    def seen_count(self) -> int:
        """Return count of records seen in this import."""
        return len(self._seen_ids)

    async def detect_stale_records(self) -> list[str]:
        """
        Find records that exist in DB but weren't seen in this import.

        Queries the database for all active (non-deleted) records for this
        source, then returns those not in the seen set.

        Returns:
            List of record IDs that are stale (exist in DB but not in import)
        """
        # Build query based on table type
        if self.table_name == 'properties':
            query = f"""
                SELECT {self.id_column}
                FROM {self.table_name}
                WHERE source_dataset = $1
                  AND (is_deleted = FALSE OR is_deleted IS NULL)
            """
            args = [self.source_key]
        elif self.table_name == 'nar_addresses':
            # NAR doesn't use source_dataset, all NAR records are from same source
            query = f"""
                SELECT {self.id_column}
                FROM {self.table_name}
                WHERE (is_deleted = FALSE OR is_deleted IS NULL)
            """
            args = []
        elif self.table_name == 'nar_locations':
            query = f"""
                SELECT {self.id_column}
                FROM {self.table_name}
                WHERE (is_deleted = FALSE OR is_deleted IS NULL)
            """
            args = []
        else:
            # Generic fallback
            query = f"""
                SELECT {self.id_column}
                FROM {self.table_name}
                WHERE (is_deleted = FALSE OR is_deleted IS NULL)
            """
            args = []

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *args)

        existing_ids = {str(row[self.id_column]) for row in rows}
        self._active_count = len(existing_ids)
        stale_ids = existing_ids - self._seen_ids

        return list(stale_ids)

    async def soft_delete_stale_records(
        self,
        stale_ids: list[str],
        batch_size: int = 1000
    ) -> int:
        """
        Mark stale records as soft-deleted.

        Sets is_deleted=True, deleted_at=NOW(), and deleted_by_import_id
        for all stale records. Processes in batches for large datasets.

        Safety guard: if the number of IDs seen in the import file is less
        than min_seen_ratio of the source's active record count, the file
        is assumed to be truncated/partial and NOTHING is deleted — a
        StaleDeletionAbortedError is raised instead so the import is
        surfaced as a failure rather than silently nuking live data.

        Args:
            stale_ids: List of record IDs to soft delete
            batch_size: Number of records to delete per batch

        Returns:
            Total number of records soft-deleted

        Raises:
            StaleDeletionAbortedError: if the seen count is below the
                safety threshold (likely truncated/partial import file)
        """
        if not stale_ids:
            return 0

        active_count = self._active_count
        if active_count is None:
            # detect_stale_records() was not called first; seen + stale is
            # the best available estimate of the active count (they are
            # disjoint once seen records have been upserted).
            active_count = self.seen_count + len(stale_ids)

        min_required = active_count * self.min_seen_ratio
        if active_count > 0 and self.seen_count < min_required:
            message = (
                f"STALE DELETION ABORTED for {self.table_name} "
                f"(source: {self.source_key}): import file contained only "
                f"{self.seen_count:,} records but the database has "
                f"{active_count:,} active records "
                f"({self.seen_count / active_count:.1%} < threshold "
                f"{self.min_seen_ratio:.0%}). The file is likely truncated "
                f"or partial. NO records were soft-deleted. If this "
                f"shrinkage is genuine, re-run with a lower "
                f"STALE_DELETE_MIN_RATIO."
            )
            print(f"\n{'!' * 70}\nERROR: {message}\n{'!' * 70}")
            raise StaleDeletionAbortedError(message)

        deleted_count = 0

        async with self.pool.acquire() as conn:
            # Process in batches
            for i in range(0, len(stale_ids), batch_size):
                batch = stale_ids[i:i + batch_size]

                async with conn.transaction():
                    result = await conn.execute(f"""
                        UPDATE {self.table_name}
                        SET is_deleted = TRUE,
                            deleted_at = NOW(),
                            deleted_by_import_id = $1
                        WHERE {self.id_column}::text = ANY($2::text[])
                          AND (is_deleted = FALSE OR is_deleted IS NULL)
                    """, self.import_id, batch)

                    # Parse affected row count from result string "UPDATE N"
                    if result:
                        parts = result.split()
                        if len(parts) >= 2:
                            deleted_count += int(parts[1])

        return deleted_count

    async def report_stale_records(
        self,
        stale_ids: list[str],
        limit: int = 20
    ) -> None:
        """
        Print a report of stale records without modifying them.

        Args:
            stale_ids: List of stale record IDs
            limit: Maximum number of samples to display
        """
        print(f"\nStale Record Report for {self.table_name}")
        print("=" * 60)
        print(f"Source: {self.source_key}")
        print(f"Records in import file: {self.seen_count:,}")
        print(f"Stale records detected: {len(stale_ids):,}")

        if stale_ids:
            sample = stale_ids[:limit]
            print(f"\nSample stale IDs (first {len(sample)}):")
            for sid in sample:
                print(f"  - {sid}")

            if len(stale_ids) > limit:
                print(f"  ... and {len(stale_ids) - limit:,} more")


class PropertyIdGenerator:
    """
    Generates consistent property IDs across different data sources.

    Uses a priority-based approach to generate IDs:
    1. Use existing property_id if present and valid
    2. Use PIN with source prefix
    3. Use assessment roll number with source prefix
    4. Generate from source + coordinate hash
    5. Fall back to source + row number

    Format: {PREFIX}-{TYPE}-{VALUE}
    Examples: TOR-PIN-123456, OTT-ARN-9876543, GUE-GEO-a1b2c3d4e5f6
    """

    # Source name to prefix mapping
    SOURCE_PREFIXES = {
        'toronto': 'TOR',
        'toronto_addresses': 'TOR',
        'ottawa': 'OTT',
        'ottawa_addresses': 'OTT',
        'guelph': 'GUE',
        'guelph_addresses': 'GUE',
        'hamilton': 'HAM',
        'hamilton_addresses': 'HAM',
        'london': 'LON',
        'london_addresses': 'LON',
        'brampton': 'BRM',
        'mississauga': 'MIS',
        'markham': 'MRK',
        'vaughan': 'VGH',
        'bruce_county': 'BRC',
        'bruce_county_parcels': 'BRC',
    }

    @classmethod
    def get_prefix(cls, source_name: str) -> str:
        """
        Get the standardized prefix for a source.

        Args:
            source_name: Data source name

        Returns:
            3-character prefix (uppercase)
        """
        # Normalize source name
        normalized = source_name.lower().strip()

        # Check explicit mapping
        if normalized in cls.SOURCE_PREFIXES:
            return cls.SOURCE_PREFIXES[normalized]

        # Generate from first 3 characters
        return normalized[:3].upper()

    @classmethod
    def generate(
        cls,
        source_name: str,
        record: dict,
        row_number: int
    ) -> str:
        """
        Generate a standardized property ID.

        Args:
            source_name: Data source name for prefix
            record: Record dictionary with potential ID fields
            row_number: Row number in import file (fallback)

        Returns:
            Standardized property ID string
        """
        prefix = cls.get_prefix(source_name)

        # Check for existing valid property_id
        existing = record.get('property_id')
        if existing and str(existing).strip():
            existing_str = str(existing).strip()
            # If it already has our format with matching prefix, use as-is
            if existing_str.startswith(f"{prefix}-"):
                return existing_str
            # Otherwise, prefix it to avoid cross-source collisions
            # (many sources use OBJECTID 1,2,3... which would collide)
            return f"{prefix}-ID-{existing_str}"

        # Priority 1: Use PIN
        pin = record.get('pin') or record.get('PIN')
        if pin and str(pin).strip():
            return f"{prefix}-PIN-{str(pin).strip()}"

        # Priority 2: Use assessment roll number
        arn = (
            record.get('assessment_roll_number') or
            record.get('roll_no') or
            record.get('ROLL_NO')
        )
        if arn and str(arn).strip():
            return f"{prefix}-ARN-{str(arn).strip()}"

        # Priority 3: Use existing property_id with prefix
        if existing and str(existing).strip():
            return f"{prefix}-ID-{str(existing).strip()}"

        # Priority 4: Generate from coordinates hash
        lat = record.get('latitude') or record.get('lat') or record.get('LAT')
        lon = record.get('longitude') or record.get('long') or record.get('lng') or record.get('LONG')

        if lat is not None and lon is not None:
            try:
                lat_f = float(lat)
                lon_f = float(lon)
                coord_str = f"{lat_f:.8f},{lon_f:.8f}"
                coord_hash = hashlib.md5(coord_str.encode()).hexdigest()[:12]
                return f"{prefix}-GEO-{coord_hash}"
            except (ValueError, TypeError):
                pass

        # Priority 5: Fall back to row number
        return f"{prefix}-ROW-{row_number:08d}"

    @classmethod
    def validate(cls, property_id: str) -> bool:
        """
        Validate a property ID format.

        Args:
            property_id: ID to validate

        Returns:
            True if valid format, False otherwise
        """
        if not property_id or not isinstance(property_id, str):
            return False

        parts = property_id.split('-')
        if len(parts) < 2:
            return False

        # Check prefix is uppercase letters
        if not parts[0].isalpha() or not parts[0].isupper():
            return False

        # Check type is valid
        valid_types = {'PIN', 'ARN', 'ID', 'GEO', 'ROW'}
        if len(parts) >= 3 and parts[1] not in valid_types:
            # Legacy format without type is also valid
            pass

        return True


def atomic_table_swap(engine, table_name: str, staging_table: str) -> None:
    """
    Atomically swap a staging table into production position.

    This enables zero-downtime imports by:
    1. Building the new table with all data and indexes (staging)
    2. Atomically renaming the current production table to _old
    3. Atomically renaming staging to production
    4. Dropping the old table

    The rename operations are near-instant regardless of table size,
    so queries experience no interruption.

    Args:
        engine: SQLAlchemy engine
        table_name: Production table name (e.g., 'fsa_boundaries')
        staging_table: Staging table name (e.g., 'fsa_boundaries_staging')

    Usage:
        # Import to staging table
        gdf.to_postgis(name='fsa_boundaries_staging', con=engine, ...)

        # Create indexes on staging table
        with engine.connect() as conn:
            conn.execute(text("CREATE INDEX ... ON fsa_boundaries_staging ..."))
            conn.commit()

        # Atomic swap
        atomic_table_swap(engine, 'fsa_boundaries', 'fsa_boundaries_staging')
    """
    from sqlalchemy import text

    old_table = f"{table_name}_old"

    with engine.connect() as conn:
        # Perform atomic rename within a transaction
        # First, clean up any leftover _old table from failed previous runs
        conn.execute(text(f"DROP TABLE IF EXISTS {old_table} CASCADE"))

        # Rename current production table to _old (if it exists)
        conn.execute(text(f"ALTER TABLE IF EXISTS {table_name} RENAME TO {old_table}"))

        # Rename staging table to production name
        conn.execute(text(f"ALTER TABLE {staging_table} RENAME TO {table_name}"))

        conn.commit()

        # Cleanup: drop the old table (outside atomic section, safe to fail)
        conn.execute(text(f"DROP TABLE IF EXISTS {old_table} CASCADE"))
        conn.commit()

    print(f"✓ Atomically swapped {staging_table} → {table_name}")
