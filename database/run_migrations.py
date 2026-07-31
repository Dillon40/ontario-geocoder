#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "asyncpg",
#   "python-dotenv",
# ]
# ///

"""
Database migration runner with version tracking.

Parses migrations.sql to extract individual migration blocks,
tracks applied migrations in schema_migrations table,
and applies only new migrations.

Usage:
    uv run database/run_migrations.py           # Apply pending migrations
    uv run database/run_migrations.py --status  # Show migration status
    uv run database/run_migrations.py --dry-run # Show what would be applied
"""
import os
import sys
import re
import hashlib
import argparse
import asyncio
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

import asyncpg

load_dotenv()

MIGRATIONS_FILE = Path(__file__).parent / 'migrations.sql'


async def get_db_connection() -> asyncpg.Connection:
    """Create database connection from environment variables."""
    password = os.environ.get('DB_PASSWORD')
    if not password:
        print("ERROR: DB_PASSWORD is not set. Source .env before running "
              "migrations - there is no default password.")
        sys.exit(1)

    return await asyncpg.connect(
        host=os.environ.get('DB_HOST', 'localhost'),
        port=int(os.environ.get('DB_PORT', 5432)),
        database=os.environ.get('DB_NAME', 'geocoder'),
        user=os.environ.get('DB_USER', 'geocoder'),
        password=password
    )


def parse_migrations(sql_content: str) -> list[dict]:
    """
    Parse migrations.sql into individual migration blocks.

    Extracts migration number, name, and SQL content between
    BEGIN; and COMMIT; for each numbered migration section.

    Raises ValueError if any `-- MIGRATION N:` header in the file was not
    captured as a runnable block (e.g. comment formatting drift), so a
    malformed block can never be silently skipped.
    """
    migrations = []

    # Pattern matches: -- MIGRATION N: Name followed by ====+ then BEGIN;...COMMIT;
    # Uses non-greedy matching to stop at first COMMIT;
    pattern = r'-- =+\n-- MIGRATION (\d+): (.+?)\n-- =+\n\n(BEGIN;.+?COMMIT;)'

    for match in re.finditer(pattern, sql_content, re.DOTALL):
        number = int(match.group(1))
        name = match.group(2).strip()
        sql = match.group(3).strip()
        checksum = hashlib.sha256(sql.encode()).hexdigest()

        migrations.append({
            'number': number,
            'name': name,
            'sql': sql,
            'checksum': checksum
        })

    # Cross-check: every migration header in the file must have produced a
    # runnable block. This is the exact failure mode that silently skipped
    # migration 25 in production (header present, block formatting off).
    header_numbers = {
        int(m.group(1))
        for m in re.finditer(r'^-- MIGRATION (\d+):', sql_content, re.MULTILINE)
    }
    captured_numbers = {m['number'] for m in migrations}
    missing = sorted(header_numbers - captured_numbers)
    if missing:
        raise ValueError(
            f"Migration header(s) {missing} found in migrations.sql but not "
            f"captured as runnable blocks. Check the comment/BEGIN...COMMIT "
            f"formatting of those blocks. Refusing to run a partial set."
        )

    return sorted(migrations, key=lambda m: m['number'])


async def ensure_migrations_table(conn: asyncpg.Connection) -> None:
    """Create schema_migrations table if it doesn't exist."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id SERIAL PRIMARY KEY,
            migration_number INTEGER UNIQUE NOT NULL,
            migration_name VARCHAR(200) NOT NULL,
            applied_at TIMESTAMP DEFAULT NOW(),
            checksum VARCHAR(64),
            execution_time_ms INTEGER,
            applied_by VARCHAR(100) DEFAULT CURRENT_USER,
            rollback_sql TEXT
        )
    """)


async def get_applied_migrations(conn: asyncpg.Connection) -> set[int]:
    """Get set of migration numbers already applied."""
    try:
        rows = await conn.fetch("SELECT migration_number FROM schema_migrations")
        return {row['migration_number'] for row in rows}
    except asyncpg.UndefinedTableError:
        return set()


async def apply_migration(conn: asyncpg.Connection, migration: dict) -> bool:
    """
    Apply a single migration and record it in schema_migrations.

    Returns True on success, False on failure.
    """
    start_time = datetime.now()

    try:
        # Execute the migration SQL
        await conn.execute(migration['sql'])

        elapsed_ms = int((datetime.now() - start_time).total_seconds() * 1000)

        # Record the applied migration
        await conn.execute("""
            INSERT INTO schema_migrations
                (migration_number, migration_name, checksum, execution_time_ms)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (migration_number) DO UPDATE SET
                checksum = EXCLUDED.checksum,
                execution_time_ms = EXCLUDED.execution_time_ms
        """, migration['number'], migration['name'], migration['checksum'], elapsed_ms)

        return True
    except Exception as e:
        print(f"  ERROR: {e}")
        return False


async def verify_applied_checksums(conn: asyncpg.Connection,
                                    migrations: list[dict]) -> list[str]:
    """
    Compare the stored checksum of each already-applied migration against a
    freshly recomputed checksum of its block text in migrations.sql.

    Returns a list of mismatch descriptions (empty list = all good).
    Rows with NULL checksums (migration 13 back-seed of 1-13) are skipped,
    as are applied migrations no longer present in the file.
    """
    rows = await conn.fetch(
        "SELECT migration_number, checksum FROM schema_migrations"
    )
    by_number = {m['number']: m for m in migrations}
    mismatches = []

    for row in rows:
        stored = row['checksum']
        if stored is None:
            # Back-seeded by migration 13 with NULL checksum; nothing to compare
            continue
        migration = by_number.get(row['migration_number'])
        if migration is None:
            # Applied migration not present in file; cannot recompute
            continue
        if stored != migration['checksum']:
            mismatches.append(
                f"Migration {row['migration_number']} ({migration['name']}): "
                f"stored {stored[:12]}... != recomputed {migration['checksum'][:12]}..."
            )

    return mismatches


async def show_status(conn: asyncpg.Connection, migrations: list[dict]) -> None:
    """Display current migration status."""
    applied = await get_applied_migrations(conn)
    pending = [m for m in migrations if m['number'] not in applied]

    print(f"\nMigration Status")
    print("=" * 50)
    print(f"Total migrations in file: {len(migrations)}")
    print(f"Applied migrations:       {len(applied)}")
    print(f"Pending migrations:       {len(pending)}")

    if applied:
        print(f"\nApplied ({len(applied)}):")
        for m in migrations:
            if m['number'] in applied:
                print(f"  [✓] Migration {m['number']:2d}: {m['name']}")

    if pending:
        print(f"\nPending ({len(pending)}):")
        for m in pending:
            print(f"  [ ] Migration {m['number']:2d}: {m['name']}")
    else:
        print("\nAll migrations are up to date.")


async def main():
    parser = argparse.ArgumentParser(
        description='Database migration runner with version tracking'
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Show pending migrations without applying'
    )
    parser.add_argument(
        '--status', action='store_true',
        help='Show current migration status'
    )
    parser.add_argument(
        '--force', type=int, metavar='N',
        help='Force re-apply migration N (use with caution)'
    )
    args = parser.parse_args()

    # Parse migrations from file
    if not MIGRATIONS_FILE.exists():
        print(f"ERROR: Migrations file not found: {MIGRATIONS_FILE}")
        sys.exit(1)

    sql_content = MIGRATIONS_FILE.read_text()
    try:
        migrations = parse_migrations(sql_content)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    if not migrations:
        print("WARNING: No migrations found in file")
        print("Expected format: -- MIGRATION N: Name")
        sys.exit(1)

    print(f"Found {len(migrations)} migrations in {MIGRATIONS_FILE.name}")

    # Connect to database
    try:
        conn = await get_db_connection()
    except Exception as e:
        print(f"ERROR: Cannot connect to database: {e}")
        sys.exit(1)

    try:
        # Ensure tracking table exists
        await ensure_migrations_table(conn)

        # Status mode
        if args.status:
            await show_status(conn, migrations)
            return

        # Get pending migrations
        applied = await get_applied_migrations(conn)
        pending = [m for m in migrations if m['number'] not in applied]

        # Force mode - re-apply specific migration
        if args.force:
            target = next((m for m in migrations if m['number'] == args.force), None)
            if not target:
                print(f"ERROR: Migration {args.force} not found")
                sys.exit(1)
            pending = [target]
            print(f"Force re-applying Migration {args.force}")

        if not pending:
            print("All migrations already applied.")
            mismatches = await verify_applied_checksums(conn, migrations)
            if mismatches:
                print("\nERROR: Checksum mismatch on applied migration(s) "
                      "(file drift or tampering):")
                for mismatch in mismatches:
                    print(f"  - {mismatch}")
                sys.exit(1)
            return

        print(f"\n{len(pending)} pending migration(s):")
        for m in pending:
            print(f"  - Migration {m['number']}: {m['name']}")

        # Dry run mode
        if args.dry_run:
            print("\n[DRY RUN] No changes made")
            return

        # Apply pending migrations
        print()
        failed = False
        for m in pending:
            print(f"Applying Migration {m['number']}: {m['name']}...")
            if await apply_migration(conn, m):
                print(f"  Applied successfully")
            else:
                print(f"  FAILED - stopping migration run")
                failed = True
                break

        if failed:
            sys.exit(1)

        print(f"\nAll {len(pending)} migration(s) applied successfully")

        # Verify stored checksums of all applied migrations against freshly
        # recomputed ones (tampering/drift detection)
        mismatches = await verify_applied_checksums(conn, migrations)
        if mismatches:
            print("\nERROR: Checksum mismatch on applied migration(s) "
                  "(file drift or tampering):")
            for mismatch in mismatches:
                print(f"  - {mismatch}")
            sys.exit(1)

    finally:
        await conn.close()


if __name__ == '__main__':
    asyncio.run(main())
