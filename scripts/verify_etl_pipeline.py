#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "asyncpg",
#   "python-dotenv",
# ]
# ///
"""
ETL Pipeline Verification

Creates a fresh database, runs the full import pipeline, and compares
table-by-table against the production database using server-side MD5 hashes.

Usage:
    uv run scripts/verify_etl_pipeline.py
    uv run scripts/verify_etl_pipeline.py --source guelph_addresses --keep-verify-db
    uv run scripts/verify_etl_pipeline.py --full-download
    uv run scripts/verify_etl_pipeline.py --skip-etl --verbose
"""

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# Project paths
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()

from data_sourcing.db_utils import get_db_config, get_psql_env
import asyncpg


# ============================================================================
# Configuration
# ============================================================================

@dataclass
class TableConfig:
    """Configuration for comparing a single table."""
    name: str
    natural_key: list[str]
    excluded_columns: set[str]
    has_is_deleted: bool = False
    group_by: Optional[str] = None


TABLE_CONFIGS = [
    TableConfig(
        name='properties',
        natural_key=['property_id'],
        excluded_columns={'id', 'created_at', 'updated_at', 'import_date',
                          'last_seen_import_id', 'deleted_by_import_id', 'deleted_at'},
        has_is_deleted=True,
        group_by='source_dataset',
    ),
    TableConfig(
        name='nar_addresses',
        natural_key=['addr_guid'],
        excluded_columns={'id', 'enrichment_date', 'last_seen_import_id',
                          'deleted_by_import_id', 'deleted_at'},
        has_is_deleted=True,
    ),
    TableConfig(
        name='nar_locations',
        natural_key=['loc_guid'],
        excluded_columns={'id', 'last_seen_import_id', 'deleted_by_import_id', 'deleted_at'},
        has_is_deleted=True,
    ),
    TableConfig(
        name='fsa_boundaries',
        natural_key=['fsa_code'],
        excluded_columns={'id', 'created_at', 'updated_at'},
    ),
    TableConfig(
        name='lower_municipal_boundaries',
        natural_key=['municipal_id'],
        excluded_columns={'id', 'created_at', 'updated_at'},
    ),
    TableConfig(
        name='fsa_municipality_mappings',
        natural_key=['fsa_code', 'municipal_id'],
        excluded_columns={'id', 'created_at', 'updated_at'},
    ),
    TableConfig(
        name='municipality_aliases',
        natural_key=['normalized_alias', 'canonical_name'],
        excluded_columns={'id', 'created_at'},
    ),
    TableConfig(
        name='data_sources',
        natural_key=['source_key'],
        excluded_columns={'id', 'created_at', 'updated_at', 'last_data_refresh',
                          'last_sync_check'},
    ),
    TableConfig(
        name='commercial_postal_codes',
        natural_key=['postal_code'],
        excluded_columns={'id', 'created_at', 'updated_at', 'municipality_assigned_at'},
    ),
]

POSTAL_DATA_FILE = PROJECT_ROOT / 'data_sourcing' / 'data_sources' / 'postal_db' / 'cdn_db.sql'


# ============================================================================
# Result types
# ============================================================================

@dataclass
class GroupResult:
    """Comparison result for a single group (e.g., one source_dataset)."""
    group_name: str
    prod_count: int
    verify_count: int
    prod_hash: Optional[str]
    verify_hash: Optional[str]

    @property
    def passed(self) -> bool:
        return (self.prod_count == self.verify_count
                and self.prod_hash == self.verify_hash)

    @property
    def count_match(self) -> bool:
        return self.prod_count == self.verify_count


@dataclass
class TableResult:
    """Comparison result for an entire table."""
    table_name: str
    passed: bool
    prod_total: int = 0
    verify_total: int = 0
    num_groups: int = 0
    groups: list[GroupResult] = field(default_factory=list)
    error: Optional[str] = None
    skipped: bool = False
    skip_reason: Optional[str] = None


@dataclass
class TimingInfo:
    db_init: float = 0
    etl: float = 0
    comparison: float = 0
    total: float = 0


# ============================================================================
# Database helpers
# ============================================================================

def get_psql_args_for_db(db_name: str) -> list[str]:
    """Get psql CLI args targeting a specific database."""
    config = get_db_config()
    return ['-h', config['host'], '-p', str(config['port']),
            '-U', config['user'], '-d', db_name]


async def connect_to_db(db_name: str) -> asyncpg.Connection:
    """Create asyncpg connection to a specific database."""
    config = get_db_config()
    return await asyncpg.connect(
        host=config['host'], port=config['port'],
        database=db_name, user=config['user'], password=config['password']
    )


async def db_exists(db_name: str) -> bool:
    """Check if a PostgreSQL database exists."""
    config = get_db_config()
    conn = await asyncpg.connect(
        host=config['host'], port=config['port'],
        database='postgres', user=config['user'], password=config['password']
    )
    try:
        result = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", db_name
        )
        return result is not None
    finally:
        await conn.close()


async def table_exists(conn, table_name: str) -> bool:
    """Check if a table exists in the public schema."""
    return await conn.fetchval("""
        SELECT EXISTS (
            SELECT FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = $1
        )
    """, table_name)


async def get_data_columns(
    conn, table_name: str, excluded: set[str]
) -> list[tuple[str, str]]:
    """Get (column_name, udt_name) pairs for hash computation."""
    rows = await conn.fetch("""
        SELECT column_name, udt_name
        FROM information_schema.columns
        WHERE table_name = $1
          AND table_schema = 'public'
          AND column_name != ALL($2::text[])
        ORDER BY ordinal_position
    """, table_name, list(excluded))
    return [(r['column_name'], r['udt_name']) for r in rows]


async def get_common_data_columns(
    prod_conn, verify_conn, table_name: str, excluded: set[str]
) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """
    Get columns present in BOTH databases for safe comparison.

    Returns: (common_columns, only_prod_columns, only_verify_columns)
    """
    prod_columns = await get_data_columns(prod_conn, table_name, excluded)
    verify_columns = await get_data_columns(verify_conn, table_name, excluded)

    prod_map = {name: udt for name, udt in prod_columns}
    verify_set = {name for name, _ in verify_columns}

    common = [(name, udt) for name, udt in prod_columns if name in verify_set]
    only_prod = sorted(set(prod_map) - verify_set)
    only_verify = sorted(verify_set - set(prod_map))

    return common, only_prod, only_verify


def build_col_expr(col_name: str, udt_name: str) -> str:
    """Build a SQL expression for a column, handling geometry and precision.

    Column names are double-quoted to handle case-sensitive identifiers
    (e.g. OGF_ID from shapefile imports).
    """
    qn = f'"{col_name}"'
    if udt_name in ('geometry', 'geography'):
        return f"COALESCE(ST_AsText({qn}, 6), '<NULL>')"
    elif col_name == 'coverage_pct':
        return f"COALESCE(ROUND({qn}, 1)::text, '<NULL>')"
    else:
        return f"COALESCE({qn}::text, '<NULL>')"


# ============================================================================
# Phase 0: Pre-flight checks
# ============================================================================

async def preflight_checks(args) -> bool:
    """Verify prerequisites before starting."""
    print("\n--- Phase 0: Pre-flight Checks ---")
    config = get_db_config()
    prod_db = config['database']
    verify_db = args.verify_db_name

    # Check production DB
    try:
        conn = await connect_to_db(prod_db)
        count = await conn.fetchval("SELECT COUNT(*) FROM properties")
        await conn.close()
        if count == 0:
            print(f"  [WARN] Production database '{prod_db}' has 0 properties")
        else:
            print(f"  [OK] Production database: {count:,} properties")
    except Exception as e:
        print(f"  [FAIL] Cannot connect to '{prod_db}': {e}")
        return False

    # Check verify DB state
    verify_exists = await db_exists(verify_db)
    if args.skip_etl or args.resume:
        if not verify_exists:
            flag = '--skip-etl' if args.skip_etl else '--resume'
            print(f"  [FAIL] {flag} requires '{verify_db}' to exist")
            return False
        print(f"  [OK] Verify database '{verify_db}' exists "
              f"({'--skip-etl' if args.skip_etl else '--resume'})")
    else:
        if verify_exists:
            print(f"  [FAIL] Database '{verify_db}' already exists. "
                  f"Drop it first, use --skip-etl, or use --resume")
            return False
        print(f"  [OK] Verify database '{verify_db}' does not exist")

    # Check cached data files
    if not args.full_download and not args.skip_etl:
        data_dir = PROJECT_ROOT / 'data_sourcing' / 'data_sources'
        if data_dir.exists():
            subdirs = [d.name for d in data_dir.iterdir() if d.is_dir()]
            print(f"  [OK] Data sources directory: {len(subdirs)} source(s) cached")
        else:
            print(f"  [WARN] Data sources directory not found: {data_dir}")

    # Check disk space
    try:
        usage = shutil.disk_usage(str(PROJECT_ROOT))
        free_gb = usage.free / (1024**3)
        if free_gb < 10:
            print(f"  [WARN] Low disk space: {free_gb:.1f} GB free (recommend >= 10 GB)")
        else:
            print(f"  [OK] Disk space: {free_gb:.1f} GB free")
    except OSError:
        pass

    return True


# ============================================================================
# Phase 1: Create & Initialize verify DB
# ============================================================================

def create_verify_db(db_name: str, admin_user: Optional[str] = None,
                     admin_password: Optional[str] = None) -> bool:
    """Create the empty verification database."""
    config = get_db_config()
    user = admin_user or config['user']
    password = admin_password or config['password']
    env = {**os.environ, 'PGPASSWORD': password}

    cmd = ['createdb', '-h', config['host'], '-p', str(config['port']),
           '-U', user]
    if admin_user:
        cmd.extend(['-O', config['user']])  # owned by the app user
    cmd.append(db_name)

    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  [FAIL] createdb: {result.stderr.strip()}")
        if 'permission denied' in result.stderr:
            print(f"  Hint: use --admin-user postgres --admin-password <pw>")
        return False
    print(f"  [OK] Created database '{db_name}'")
    return True


def init_verify_db(db_name: str, admin_user: Optional[str] = None,
                   admin_password: Optional[str] = None) -> bool:
    """Apply schema and migrations to the verify database."""
    config = get_db_config()
    env = get_psql_env()
    psql_args = get_psql_args_for_db(db_name)

    # PostGIS extension requires superuser — try admin user first, fall back to app user
    postgis_sql = "CREATE EXTENSION IF NOT EXISTS postgis; CREATE EXTENSION IF NOT EXISTS postgis_topology;"
    postgis_ok = False

    if admin_user:
        admin_env = {**os.environ, 'PGPASSWORD': admin_password or config['password']}
        admin_args = ['-h', config['host'], '-p', str(config['port']),
                      '-U', admin_user, '-d', db_name]
        result = subprocess.run(
            ['psql'] + admin_args + ['-c', postgis_sql],
            env=admin_env, capture_output=True, text=True
        )
        postgis_ok = result.returncode == 0

    if not postgis_ok:
        result = subprocess.run(
            ['psql'] + psql_args + ['-c', postgis_sql],
            env=env, capture_output=True, text=True
        )
        postgis_ok = result.returncode == 0

    if not postgis_ok:
        print(f"  [FAIL] Cannot create PostGIS extension (requires superuser)")
        print(f"  Hint: use --admin-user postgres --admin-password <pw>")
        return False
    print(f"  [OK] PostGIS extension enabled")

    # Apply schema via psql
    schema_path = PROJECT_ROOT / 'database' / 'schema.sql'
    if not schema_path.exists():
        print(f"  [FAIL] schema.sql not found: {schema_path}")
        return False

    result = subprocess.run(
        ['psql'] + psql_args + ['-f', str(schema_path)],
        env=env, capture_output=True, text=True
    )
    if result.returncode != 0 and 'ERROR' in result.stderr:
        print(f"  [FAIL] schema.sql: {result.stderr[:300]}")
        return False
    print(f"  [OK] Applied schema.sql")

    # Apply migrations via run_migrations.py — the same code path production
    # uses (raw `psql -f migrations.sql` bypasses the parser and tracking)
    runner_path = PROJECT_ROOT / 'database' / 'run_migrations.py'
    if not runner_path.exists():
        print(f"  [FAIL] run_migrations.py not found: {runner_path}")
        return False

    runner_env = {
        **os.environ,
        'DB_HOST': config['host'],
        'DB_PORT': str(config['port']),
        'DB_NAME': db_name,
        'DB_USER': config['user'],
        'DB_PASSWORD': config['password'],
    }
    result = subprocess.run(
        [sys.executable, str(runner_path)],
        env=runner_env, capture_output=True, text=True
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        print(f"  [FAIL] run_migrations.py: {detail[:300]}")
        return False
    print(f"  [OK] Applied migrations via run_migrations.py")

    # Seed file may be in database/ or data_sourcing/
    for candidate in [PROJECT_ROOT / 'database' / 'seed_data_sources.sql',
                      PROJECT_ROOT / 'data_sourcing' / 'seed_data_sources.sql']:
        if candidate.exists():
            result = subprocess.run(
                ['psql'] + psql_args + ['-f', str(candidate)],
                env=env, capture_output=True, text=True
            )
            if result.returncode != 0 and 'ERROR' in result.stderr:
                print(f"  [FAIL] seed_data_sources.sql: {result.stderr[:300]}")
                return False
            print(f"  [OK] Applied seed_data_sources.sql")
            break

    # Verify PostGIS is functional
    result = subprocess.run(
        ['psql'] + psql_args + ['-c', 'SELECT PostGIS_version();'],
        env=env, capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  [FAIL] PostGIS not functional in '{db_name}'")
        return False
    print(f"  [OK] PostGIS verified")

    return True


# ============================================================================
# Phase 2: Run ETL pipeline
# ============================================================================

def run_etl(args, verify_db_name: str) -> bool:
    """Run the ETL pipeline against the verify database."""
    config = get_db_config()
    run_env = {
        **os.environ,
        'DB_NAME': verify_db_name,
        'DB_HOST': config['host'],
        'DB_PORT': str(config['port']),
        'DB_USER': config['user'],
        'DB_PASSWORD': config['password'],
    }

    cmd = ['uv', 'run', str(PROJECT_ROOT / 'data_sourcing' / 'setup_data.py')]

    if args.source:
        cmd.extend(['--source', args.source])
    else:
        cmd.append('--all-confirmed')

    if not args.full_download:
        cmd.append('--import-only')

    cmd.append('--accept-non-commercial')

    print(f"  Running: {' '.join(cmd)}")
    print(f"  DB_NAME={verify_db_name}")
    result = subprocess.run(cmd, env=run_env)

    if result.returncode != 0:
        print(f"  [WARN] ETL exited with code {result.returncode}")
        return False

    print(f"  [OK] ETL completed successfully")
    return True


def run_postal_codes_import(verify_db_name: str) -> bool:
    """Import commercial postal codes if source data exists."""
    if not POSTAL_DATA_FILE.exists():
        print(f"  [SKIP] Postal codes: data file not found")
        return True

    script_path = PROJECT_ROOT / 'data_sourcing' / 'importers' / 'import_postal_codes.py'
    if not script_path.exists():
        print(f"  [SKIP] Postal codes: import script not found")
        return True

    config = get_db_config()
    run_env = {
        **os.environ,
        'DB_NAME': verify_db_name,
        'DB_HOST': config['host'],
        'DB_PORT': str(config['port']),
        'DB_USER': config['user'],
        'DB_PASSWORD': config['password'],
    }

    cmd = ['uv', 'run', str(script_path), '--file', str(POSTAL_DATA_FILE)]
    print(f"  Importing postal codes...")
    result = subprocess.run(cmd, env=run_env, cwd=str(script_path.parent))

    if result.returncode != 0:
        print(f"  [WARN] Postal codes import failed (exit code {result.returncode})")
        return False

    print(f"  [OK] Postal codes imported")
    return True


def copy_dependency_tables(verify_db_name: str) -> None:
    """Copy boundary tables from production for single-source verification.

    The FSA and municipal boundary tables are prerequisites for the transform
    pipeline. Their schema differs between migrations (which create placeholder
    columns) and the actual importers (which swap in tables with different
    columns like province_id). Copying from production ensures the verify DB
    has the correct structure and data.
    """
    config = get_db_config()
    env = get_psql_env()

    tables = ['fsa_boundaries', 'lower_municipal_boundaries']
    verify_args = get_psql_args_for_db(verify_db_name)

    for table in tables:
        dump_cmd = [
            'pg_dump', '-h', config['host'], '-p', str(config['port']),
            '-U', config['user'], config['database'],
            '-t', table, '--clean', '--if-exists',
            '--no-owner', '--no-privileges',
        ]
        restore_cmd = ['psql'] + verify_args

        dump = subprocess.run(dump_cmd, env=env, capture_output=True, text=True)
        if dump.returncode != 0 or not dump.stdout:
            print(f"  [WARN] Could not dump {table}: {dump.stderr[:200]}")
            continue

        restore = subprocess.run(
            restore_cmd, input=dump.stdout, env=env,
            capture_output=True, text=True
        )
        if restore.returncode == 0:
            print(f"  [OK] Copied {table} from production")
        else:
            # pg_restore may warn but still succeed
            if 'ERROR' in restore.stderr:
                print(f"  [WARN] Errors restoring {table}: {restore.stderr[:200]}")
            else:
                print(f"  [OK] Copied {table} from production")


async def ensure_boundary_tables(verify_db_name: str) -> None:
    """Copy boundary tables from production if empty in verify DB."""
    conn = await connect_to_db(verify_db_name)
    try:
        fsa = await conn.fetchval("SELECT COUNT(*) FROM fsa_boundaries")
        muni = await conn.fetchval("SELECT COUNT(*) FROM lower_municipal_boundaries")
    finally:
        await conn.close()

    if fsa == 0 or muni == 0:
        print("  Boundary tables empty, copying from production...")
        copy_dependency_tables(verify_db_name)


async def sync_data_sources(verify_db_name: str) -> None:
    """Sync data_sources from production to verify DB.

    The seed script may have different local_file_path, dataset_mapping_key,
    etc. than what the download scripts set in production. For --import-only
    mode, these must match so the importer can find cached files.
    """
    config = get_db_config()
    prod_conn = await connect_to_db(config['database'])
    verify_conn = await connect_to_db(verify_db_name)
    try:
        # Get updatable columns (skip id and timestamps)
        cols_rows = await prod_conn.fetch("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'data_sources' AND table_schema = 'public'
              AND column_name NOT IN ('id', 'created_at', 'updated_at')
            ORDER BY ordinal_position
        """)
        col_names = [c['column_name'] for c in cols_rows]

        rows = await prod_conn.fetch(
            f"SELECT {', '.join(col_names)} FROM data_sources"
        )

        for row in rows:
            set_clause = ", ".join(
                f"{c} = EXCLUDED.{c}" for c in col_names if c != 'source_key'
            )
            placeholders = ", ".join(f"${i+1}" for i in range(len(col_names)))
            await verify_conn.execute(f"""
                INSERT INTO data_sources ({', '.join(col_names)})
                VALUES ({placeholders})
                ON CONFLICT (source_key) DO UPDATE SET {set_clause}
            """, *[row[c] for c in col_names])

        print(f"  [OK] Synced {len(rows)} data sources from production")
    finally:
        await prod_conn.close()
        await verify_conn.close()


async def run_setup_transformers(verify_db_name: str) -> None:
    """Re-run setup transformers to populate derived tables."""
    config = get_db_config()
    pool = await asyncpg.create_pool(
        host=config['host'], port=config['port'],
        database=verify_db_name, user=config['user'], password=config['password'],
        min_size=1, max_size=3
    )
    try:
        async with pool.acquire() as conn:
            fsa_count = await conn.fetchval(
                "SELECT COUNT(*) FROM fsa_boundaries"
            )
            muni_count = await conn.fetchval(
                "SELECT COUNT(*) FROM lower_municipal_boundaries"
            )

        if fsa_count > 0 and muni_count > 0:
            from data_sourcing.transformers.fsa_municipality_setup import (
                FSAMunicipalitySetup,
            )
            fsa_setup = FSAMunicipalitySetup()
            result = await fsa_setup.ensure_setup(pool, force=True)
            print(f"  FSAMunicipalitySetup: {result.status} "
                  f"({result.rows_affected} rows, {result.duration_seconds:.1f}s)")
        else:
            print(f"  [SKIP] FSAMunicipalitySetup: missing boundary data "
                  f"(fsa={fsa_count}, muni={muni_count})")

        from data_sourcing.transformers.municipality_alias_setup import (
            MunicipalityAliasSetup,
        )
        alias_setup = MunicipalityAliasSetup()
        result = await alias_setup.ensure_setup(pool, force=True)
        print(f"  MunicipalityAliasSetup: {result.status} "
              f"({result.rows_affected} rows, {result.duration_seconds:.1f}s)")
    finally:
        await pool.close()


async def get_completed_sources(verify_db_name: str) -> set[str]:
    """
    Query import_history in the verify DB to find sources that completed successfully.

    import_history.source_key stores the dataset_mapping_key (e.g., 'brantford_addresses'),
    while data_sources.source_key is what setup_data.py expects (e.g., 'brantford').
    This function maps back through data_sources to return data_sources.source_key values.

    Returns set of data_sources.source_key values with at least one completed import.
    """
    try:
        conn = await connect_to_db(verify_db_name)
        try:
            # Get raw completed keys from import_history
            rows = await conn.fetch("""
                SELECT DISTINCT source_key
                FROM import_history
                WHERE status = 'completed'
            """)
            completed_import_keys = {r['source_key'] for r in rows}

            # Map back to data_sources.source_key via dataset_mapping_key
            # A source is complete if its source_key OR dataset_mapping_key
            # appears in the completed import_history entries
            ds_rows = await conn.fetch("""
                SELECT source_key, dataset_mapping_key, local_file_path
                FROM data_sources
                WHERE status = 'confirmed'
            """)
            completed = set()
            for r in ds_rows:
                sk = r['source_key']
                dmk = r['dataset_mapping_key']
                if sk in completed_import_keys or (dmk and dmk in completed_import_keys):
                    # NAR has multiple parts — only mark complete if all parts imported
                    if sk == 'nar_ontario':
                        nar_dir = PROJECT_ROOT / (
                            r['local_file_path'] or f'data_sourcing/data_sources/{sk}'
                        )
                        expected_parts = (
                            len(list(nar_dir.glob('**/Address_35*.csv')))
                            if nar_dir.exists() else 0
                        )
                        actual_completed = await conn.fetchval("""
                            SELECT COUNT(*) FROM import_history
                            WHERE source_key = 'nar_ontario' AND status = 'completed'
                        """)
                        if expected_parts > 0 and actual_completed >= expected_parts:
                            completed.add(sk)
                        # else: NAR is NOT complete, needs re-import
                    else:
                        completed.add(sk)

            return completed
        finally:
            await conn.close()
    except Exception as e:
        print(f"  [WARN] Could not query import_history: {e}")
        return set()


async def get_all_confirmed_sources(prod_db_name: str) -> list[str]:
    """Get all confirmed source_keys from the production data_sources table."""
    conn = await connect_to_db(prod_db_name)
    try:
        rows = await conn.fetch("""
            SELECT source_key FROM data_sources
            WHERE status = 'confirmed'
            ORDER BY source_key
        """)
        return [r['source_key'] for r in rows]
    finally:
        await conn.close()


async def clean_nar_for_reimport(verify_db_name: str) -> bool:
    """Remove old NAR data if corrupted by stale-deletion bug.

    Only cleans if soft-deleted rows exist (indicating the old stale-detection
    bug damaged the data). If no soft-deleted rows, returns False (no cleanup needed).

    Returns True if cleanup was performed, False if data was already clean.
    """
    conn = await connect_to_db(verify_db_name)
    try:
        deleted_count = await conn.fetchval(
            "SELECT COUNT(*) FROM nar_addresses WHERE is_deleted = TRUE"
        )
        if deleted_count == 0:
            return False

        addr_count = await conn.fetchval("SELECT COUNT(*) FROM nar_addresses")
        await conn.execute("DELETE FROM nar_addresses")
        await conn.execute("DELETE FROM nar_locations")
        await conn.execute(
            "DELETE FROM import_history WHERE source_key = 'nar_ontario'"
        )
        print(f"  [OK] Cleared corrupted NAR data for clean re-import "
              f"({addr_count:,} addresses, {deleted_count:,} were soft-deleted)")
        return True
    finally:
        await conn.close()


def run_etl_single_source(source_key: str, verify_db_name: str,
                          full_download: bool) -> bool:
    """Run ETL for a single source against the verify database."""
    config = get_db_config()
    run_env = {
        **os.environ,
        'DB_NAME': verify_db_name,
        'DB_HOST': config['host'],
        'DB_PORT': str(config['port']),
        'DB_USER': config['user'],
        'DB_PASSWORD': config['password'],
    }

    cmd = ['uv', 'run', str(PROJECT_ROOT / 'data_sourcing' / 'setup_data.py'),
           '--source', source_key, '--accept-non-commercial']

    if not full_download:
        cmd.append('--import-only')

    result = subprocess.run(cmd, env=run_env)
    return result.returncode == 0


async def run_etl_resume(args, verify_db_name: str) -> bool:
    """
    Resume ETL by running only sources that haven't completed yet.

    Queries import_history in the verify DB, compares against all confirmed
    sources in production, and runs setup_data.py --source for each missing one.
    """
    config = get_db_config()
    prod_db = config['database']

    # Find what's already done and what needs doing
    completed = await get_completed_sources(verify_db_name)
    all_sources = await get_all_confirmed_sources(prod_db)

    if args.source:
        # Single source mode — just check if that one is done
        pending = [args.source] if args.source not in completed else []
    else:
        pending = [s for s in all_sources if s not in completed]

    if not pending:
        print(f"  [OK] All {len(all_sources)} sources already imported, nothing to resume")
        return True

    print(f"  Already completed: {len(completed)} source(s)")
    print(f"  Remaining: {len(pending)} source(s)")
    for s in pending:
        print(f"    - {s}")

    # Run each pending source
    success_count = 0
    fail_count = 0
    for i, source_key in enumerate(pending, 1):
        print(f"\n  [{i}/{len(pending)}] Importing {source_key}...")
        if source_key == 'nar_ontario':
            cleaned = await clean_nar_for_reimport(verify_db_name)
            if not cleaned:
                print(f"  [OK] NAR data is clean, no cleanup needed")
        if run_etl_single_source(source_key, verify_db_name, args.full_download):
            success_count += 1
            print(f"  [OK] {source_key}")
        else:
            fail_count += 1
            print(f"  [WARN] {source_key} failed, continuing...")

    print(f"\n  Resume complete: {success_count} succeeded, {fail_count} failed")
    return fail_count == 0


# ============================================================================
# Phase 3: Compare databases
# ============================================================================

async def compare_table_full(
    prod_conn, verify_conn, config: TableConfig,
    source_filter: Optional[str], verbose: bool,
    diff: bool = False,
) -> TableResult:
    """Full MD5 hash comparison for a table."""
    table = config.name

    # Check table exists in both databases
    for label, conn in [("production", prod_conn), ("verify", verify_conn)]:
        if not await table_exists(conn, table):
            return TableResult(
                table_name=table, passed=True, skipped=True,
                skip_reason=f"Table missing in {label}"
            )

    # Discover data columns (intersection of both DBs)
    columns, only_prod_cols, only_verify_cols = await get_common_data_columns(
        prod_conn, verify_conn, table, config.excluded_columns
    )
    schema_note = ""
    if only_prod_cols or only_verify_cols:
        parts = []
        if only_prod_cols:
            parts.append(f"only in prod: {', '.join(only_prod_cols)}")
        if only_verify_cols:
            parts.append(f"only in verify: {', '.join(only_verify_cols)}")
        schema_note = f"Schema diff: {'; '.join(parts)}"
    if not columns:
        return TableResult(
            table_name=table, passed=True, skipped=True,
            skip_reason="No common data columns",
            error=schema_note or None,
        )
    if schema_note:
        print(f"    [NOTE] {table}: {schema_note} — comparing {len(columns)} common columns")

    col_exprs = ", ".join(build_col_expr(n, u) for n, u in columns)

    # WHERE clause with parameterized source filter
    where_parts = []
    params = []
    if config.has_is_deleted:
        where_parts.append("(is_deleted = FALSE OR is_deleted IS NULL)")
    if source_filter and config.group_by == 'source_dataset':
        params.append(source_filter)
        where_parts.append(f"source_dataset = ${len(params)}")
    where = " AND ".join(where_parts) if where_parts else "TRUE"

    # Build hash query using CTE for efficiency
    if config.group_by and not source_filter:
        query = f"""
            WITH row_hashes AS (
                SELECT {config.group_by},
                       md5(concat_ws('|', {col_exprs})) AS rh
                FROM {table}
                WHERE {where}
            )
            SELECT {config.group_by} AS group_name,
                   COUNT(*) AS row_count,
                   md5(string_agg(rh, '' ORDER BY rh)) AS data_hash
            FROM row_hashes
            GROUP BY {config.group_by}
            ORDER BY {config.group_by}
        """
    else:
        query = f"""
            WITH row_hashes AS (
                SELECT md5(concat_ws('|', {col_exprs})) AS rh
                FROM {table}
                WHERE {where}
            )
            SELECT COUNT(*) AS row_count,
                   md5(string_agg(rh, '' ORDER BY rh)) AS data_hash
            FROM row_hashes
        """

    prod_rows = await prod_conn.fetch(query, *params)
    verify_rows = await verify_conn.fetch(query, *params)

    # Build group results
    groups = []

    if config.group_by and not source_filter:
        prod_map = {r['group_name']: r for r in prod_rows}
        verify_map = {r['group_name']: r for r in verify_rows}
        all_groups = sorted(
            set(prod_map.keys()) | set(verify_map.keys()),
            key=lambda x: x or ''
        )

        for gn in all_groups:
            pr = prod_map.get(gn)
            vr = verify_map.get(gn)
            groups.append(GroupResult(
                group_name=gn or '(null)',
                prod_count=pr['row_count'] if pr else 0,
                verify_count=vr['row_count'] if vr else 0,
                prod_hash=pr['data_hash'] if pr else None,
                verify_hash=vr['data_hash'] if vr else None,
            ))
    else:
        label = source_filter or '(all)'
        pc = prod_rows[0]['row_count'] if prod_rows else 0
        vc = verify_rows[0]['row_count'] if verify_rows else 0
        ph = prod_rows[0]['data_hash'] if prod_rows else None
        vh = verify_rows[0]['data_hash'] if verify_rows else None
        groups.append(GroupResult(
            group_name=label, prod_count=pc, verify_count=vc,
            prod_hash=ph, verify_hash=vh,
        ))

    prod_total = sum(g.prod_count for g in groups)
    verify_total = sum(g.verify_count for g in groups)
    all_passed = all(g.passed for g in groups)

    result = TableResult(
        table_name=table, passed=all_passed,
        prod_total=prod_total, verify_total=verify_total,
        num_groups=len(groups), groups=groups,
    )

    # Verbose: row-level diff for failed groups
    if verbose and not all_passed:
        for g in groups:
            if not g.passed:
                group_filter = g.group_name if (
                    config.group_by and g.group_name != '(all)'
                ) else None
                await show_verbose_diff(
                    prod_conn, verify_conn, config, group_filter
                )

    # Diff mode: detailed row-level breakdown for failed groups
    if diff and not all_passed:
        for g in groups:
            if not g.passed:
                gf = g.group_name if (
                    config.group_by and g.group_name != '(all)'
                ) else None
                print(f"       {g.group_name} ({g.prod_count:,} prod vs {g.verify_count:,} verify) HASH MISMATCH")
                await show_diff_summary(
                    prod_conn, verify_conn, config,
                    g.group_name, gf,
                    g.prod_count, g.verify_count,
                )

    return result


async def compare_table_quick(
    prod_conn, verify_conn, config: TableConfig,
    source_filter: Optional[str]
) -> TableResult:
    """Quick count + sample comparison (no full hash)."""
    table = config.name

    for label, conn in [("production", prod_conn), ("verify", verify_conn)]:
        if not await table_exists(conn, table):
            return TableResult(
                table_name=table, passed=True, skipped=True,
                skip_reason=f"Table missing in {label}"
            )

    where_parts = []
    params = []
    if config.has_is_deleted:
        where_parts.append("(is_deleted = FALSE OR is_deleted IS NULL)")
    if source_filter and config.group_by == 'source_dataset':
        params.append(source_filter)
        where_parts.append(f"source_dataset = ${len(params)}")
    where = " AND ".join(where_parts) if where_parts else "TRUE"

    # Count comparison
    if config.group_by and not source_filter:
        count_query = f"""
            SELECT {config.group_by} AS group_name, COUNT(*) AS row_count
            FROM {table} WHERE {where}
            GROUP BY {config.group_by}
            ORDER BY {config.group_by}
        """
    else:
        count_query = f"SELECT COUNT(*) AS row_count FROM {table} WHERE {where}"

    prod_rows = await prod_conn.fetch(count_query, *params)
    verify_rows = await verify_conn.fetch(count_query, *params)

    groups = []
    if config.group_by and not source_filter:
        prod_map = {r['group_name']: r['row_count'] for r in prod_rows}
        verify_map = {r['group_name']: r['row_count'] for r in verify_rows}
        all_groups = sorted(
            set(prod_map.keys()) | set(verify_map.keys()),
            key=lambda x: x or ''
        )
        for gn in all_groups:
            pc = prod_map.get(gn, 0)
            vc = verify_map.get(gn, 0)
            groups.append(GroupResult(
                group_name=gn or '(null)', prod_count=pc, verify_count=vc,
                prod_hash='(quick)', verify_hash='(quick)' if pc == vc else '(differs)',
            ))
    else:
        pc = prod_rows[0]['row_count'] if prod_rows else 0
        vc = verify_rows[0]['row_count'] if verify_rows else 0
        groups.append(GroupResult(
            group_name=source_filter or '(all)', prod_count=pc, verify_count=vc,
            prod_hash='(quick)', verify_hash='(quick)' if pc == vc else '(differs)',
        ))

    # Sample comparison: hash first 1000 rows by natural key
    columns, _, _ = await get_common_data_columns(
        prod_conn, verify_conn, table, config.excluded_columns
    )
    sample_mismatch = False
    if columns:
        col_exprs = ", ".join(build_col_expr(n, u) for n, u in columns)
        key_cols = ", ".join(config.natural_key)
        sample_query = f"""
            SELECT md5(concat_ws('|', {col_exprs})) AS rh
            FROM {table}
            WHERE {where}
            ORDER BY {key_cols}
            LIMIT 1000
        """
        prod_sample = [r['rh'] for r in await prod_conn.fetch(sample_query, *params)]
        verify_sample = [r['rh'] for r in await verify_conn.fetch(sample_query, *params)]
        if prod_sample != verify_sample:
            sample_mismatch = True

    # In quick mode, pass = counts match and sample matches
    for g in groups:
        if g.count_match and sample_mismatch:
            g.verify_hash = '(sample differs)'

    prod_total = sum(g.prod_count for g in groups)
    verify_total = sum(g.verify_count for g in groups)
    all_passed = all(g.count_match for g in groups) and not sample_mismatch

    return TableResult(
        table_name=table, passed=all_passed,
        prod_total=prod_total, verify_total=verify_total,
        num_groups=len(groups), groups=groups,
    )


async def show_verbose_diff(
    prod_conn, verify_conn, config: TableConfig,
    group_filter: Optional[str] = None
):
    """Show row-level differences for a mismatched table/group."""
    table = config.name
    key_cols = ", ".join(config.natural_key)

    where_parts = []
    if config.has_is_deleted:
        where_parts.append("(is_deleted = FALSE OR is_deleted IS NULL)")
    if group_filter and config.group_by:
        where_parts.append(f"{config.group_by} = $1")
        params = [group_filter]
    else:
        params = []
    where = " AND ".join(where_parts) if where_parts else "TRUE"

    LIMIT = 50000

    query = f"SELECT {key_cols} FROM {table} WHERE {where} ORDER BY {key_cols} LIMIT {LIMIT}"
    if params:
        prod_raw = await prod_conn.fetch(query, *params)
        verify_raw = await verify_conn.fetch(query, *params)
    else:
        prod_raw = await prod_conn.fetch(query)
        verify_raw = await verify_conn.fetch(query)

    prod_keys = {
        tuple(r[k] for k in config.natural_key) for r in prod_raw
    }
    verify_keys = {
        tuple(r[k] for k in config.natural_key) for r in verify_raw
    }

    only_prod = sorted(prod_keys - verify_keys)[:10]
    only_verify = sorted(verify_keys - prod_keys)[:10]

    if only_prod:
        total = len(prod_keys - verify_keys)
        keys_str = ", ".join(
            str(k[0]) if len(k) == 1 else str(k) for k in only_prod
        )
        more = f" (+{total - 10} more)" if total > 10 else ""
        print(f"       Missing in verify: {keys_str}{more}")

    if only_verify:
        total = len(verify_keys - prod_keys)
        keys_str = ", ".join(
            str(k[0]) if len(k) == 1 else str(k) for k in only_verify
        )
        more = f" (+{total - 10} more)" if total > 10 else ""
        print(f"       Extra in verify: {keys_str}{more}")

    if not only_prod and not only_verify:
        print(f"       Same keys, different data values")


async def show_diff_summary(
    prod_conn, verify_conn, config: TableConfig,
    group_name: str, group_filter: Optional[str],
    prod_count: int, verify_count: int,
):
    """
    Show row-level breakdown for a group with hash mismatch.

    Fetches (natural_key, md5_hash) from both databases and computes:
    - Identical rows (same key, same data)
    - Changed rows (same key, different data)
    - Only in prod (key missing from verify)
    - Only in verify (key missing from prod)
    """
    table = config.name
    key_cols = ", ".join(config.natural_key)
    natural_key_label = ", ".join(config.natural_key)

    # Safety limit: if either side exceeds 1M rows, sample instead
    max_rows = max(prod_count, verify_count)
    sample_note = ""
    limit_clause = ""
    if max_rows > 1_000_000:
        sample_note = " (sampled first 1M by natural key)"
        limit_clause = f" ORDER BY {key_cols} LIMIT 1000000"

    # Build WHERE clause
    where_parts = []
    params = []
    if config.has_is_deleted:
        where_parts.append("(is_deleted = FALSE OR is_deleted IS NULL)")
    if group_filter and config.group_by:
        params.append(group_filter)
        where_parts.append(f"{config.group_by} = ${len(params)}")
    where = " AND ".join(where_parts) if where_parts else "TRUE"

    # Get data columns for hashing (intersection of both DBs)
    columns, _, _ = await get_common_data_columns(
        prod_conn, verify_conn, table, config.excluded_columns
    )
    if not columns:
        print(f"         (no common data columns to diff)")
        return

    col_exprs = ", ".join(build_col_expr(n, u) for n, u in columns)

    # Fetch (natural_key, row_hash) from both databases
    query = f"""
        SELECT {key_cols}, md5(concat_ws('|', {col_exprs})) AS row_hash
        FROM {table}
        WHERE {where}
        {limit_clause}
    """

    prod_rows = await prod_conn.fetch(query, *params)
    verify_rows = await verify_conn.fetch(query, *params)

    # Build dicts keyed by natural key tuple
    def make_key(row):
        if len(config.natural_key) == 1:
            return row[config.natural_key[0]]
        return tuple(row[k] for k in config.natural_key)

    prod_map = {make_key(r): r['row_hash'] for r in prod_rows}
    verify_map = {make_key(r): r['row_hash'] for r in verify_rows}

    prod_keys = set(prod_map.keys())
    verify_keys = set(verify_map.keys())

    only_prod = prod_keys - verify_keys
    only_verify = verify_keys - prod_keys
    common = prod_keys & verify_keys
    changed = {k for k in common if prod_map[k] != verify_map[k]}
    identical = common - changed

    print(f"         Identical rows: {len(identical):,} (by {natural_key_label}){sample_note}")
    print(f"         Changed rows:   {len(changed):,}")
    print(f"         Only in prod:   {len(only_prod):,}")
    print(f"         Only in verify: {len(only_verify):,}")


async def compare_databases(args, timing: TimingInfo) -> list[TableResult]:
    """Run Phase 3: compare production vs verify database."""
    print("\n--- Phase 3: Compare Databases ---")
    start = time.time()

    config = get_db_config()
    prod_db = config['database']
    verify_db = args.verify_db_name

    prod_conn = await connect_to_db(prod_db)
    verify_conn = await connect_to_db(verify_db)

    # Resolve source_key to the source_dataset name used in properties table
    source_filter = args.source
    if source_filter:
        mapping = await prod_conn.fetchval(
            "SELECT dataset_mapping_key FROM data_sources WHERE source_key = $1",
            source_filter
        )
        if mapping:
            source_filter = mapping
            print(f"  Source filter: {args.source} -> source_dataset='{source_filter}'")

    results = []
    try:
        for tc in TABLE_CONFIGS:
            # When --source is set, skip unrelated tables that are empty in verify
            if args.source:
                is_properties = tc.group_by == 'source_dataset'
                is_data_sources = tc.name == 'data_sources'
                if not is_properties and not is_data_sources:
                    if await table_exists(verify_conn, tc.name):
                        vc = await verify_conn.fetchval(
                            f"SELECT COUNT(*) FROM {tc.name}"
                        )
                        if vc == 0:
                            results.append(TableResult(
                                table_name=tc.name, passed=True, skipped=True,
                                skip_reason=f"Empty (--source {args.source})"
                            ))
                            continue

            print(f"  Comparing {tc.name}...", end="", flush=True)
            t0 = time.time()

            try:
                if args.quick:
                    result = await compare_table_quick(
                        prod_conn, verify_conn, tc, source_filter
                    )
                else:
                    result = await compare_table_full(
                        prod_conn, verify_conn, tc, source_filter,
                        args.verbose, diff=args.diff,
                    )
            except Exception as e:
                result = TableResult(
                    table_name=tc.name, passed=False, error=str(e)
                )

            elapsed = time.time() - t0
            if result.skipped:
                status = "SKIP"
            elif result.passed:
                status = "PASS"
            else:
                status = "FAIL"
            print(f" [{status}] ({elapsed:.1f}s)")
            results.append(result)

    finally:
        await prod_conn.close()
        await verify_conn.close()

    timing.comparison = time.time() - start
    return results


# ============================================================================
# Phase 4: Report
# ============================================================================

def format_duration(seconds: float) -> str:
    """Format seconds as human-readable duration."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    mins, secs = divmod(int(seconds), 60)
    if mins < 60:
        return f"{mins}m {secs}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins}m {secs}s"


def generate_report(
    results: list[TableResult], timing: TimingInfo, args
) -> str:
    """Generate the verification report as a string."""
    config = get_db_config()
    lines = []

    lines.append("=" * 80)
    lines.append("ETL PIPELINE VERIFICATION REPORT")
    lines.append(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(
        f"Production DB: {config['database']} @ {config['host']}:{config['port']}"
    )
    lines.append(
        f"Verification DB: {args.verify_db_name} @ {config['host']}:{config['port']}"
    )

    mode = "full-download" if args.full_download else "import-only"
    if args.source:
        mode += f" (source: {args.source})"
    if args.quick:
        mode += " [QUICK]"
    if args.skip_etl:
        mode += " [SKIP-ETL]"
    if args.resume:
        mode += " [RESUME]"
    lines.append(f"Mode: {mode}")
    lines.append("=" * 80)

    # Summary
    compared = [r for r in results if not r.skipped]
    passed = [r for r in compared if r.passed]
    failed = [r for r in compared if not r.passed]
    skipped = [r for r in results if r.skipped]

    summary_parts = [f"{len(passed)}/{len(compared)} tables PASS"]
    if failed:
        summary_parts.append(f"{len(failed)} FAIL")
    if skipped:
        summary_parts.append(f"{len(skipped)} SKIP")
    lines.append(f"\nSUMMARY: {', '.join(summary_parts)}")

    # Detailed results
    lines.append(f"\nTABLE RESULTS")
    lines.append("-" * 60)

    for r in results:
        if r.skipped:
            lines.append(
                f"[SKIP] {r.table_name:.<40s} {r.skip_reason}"
            )
            continue

        if r.error:
            lines.append(
                f"[FAIL] {r.table_name:.<40s} ERROR: {r.error}"
            )
            continue

        status = "PASS" if r.passed else "FAIL"
        count_str = f"{r.prod_total:,} rows"
        if r.num_groups > 1:
            count_str += f" ({r.num_groups} sources)"
        lines.append(f"[{status}] {r.table_name:.<40s} {count_str}")

        # Group details for multi-group tables or failed tables
        if r.groups and (r.num_groups > 1 or not r.passed):
            for g in r.groups:
                if g.passed:
                    lines.append(
                        f"       {g.group_name:.<35s} "
                        f"{g.prod_count:>10,} rows  hash match"
                    )
                elif g.count_match:
                    lines.append(
                        f"       {g.group_name:.<35s} "
                        f"{g.prod_count:>10,} rows  HASH MISMATCH"
                    )
                else:
                    lines.append(
                        f"       {g.group_name:.<35s} "
                        f"{g.prod_count:>10,} prod vs "
                        f"{g.verify_count:>10,} verify"
                    )

    # Timing
    lines.append(
        f"\nTIMING: DB init {format_duration(timing.db_init)} | "
        f"ETL {format_duration(timing.etl)} | "
        f"Comparison {format_duration(timing.comparison)} | "
        f"Total {format_duration(timing.total)}"
    )

    return "\n".join(lines)


# ============================================================================
# Phase 5: Cleanup
# ============================================================================

def cleanup_verify_db(db_name: str, admin_user: Optional[str] = None,
                      admin_password: Optional[str] = None):
    """Drop the verification database."""
    config = get_db_config()
    user = admin_user or config['user']
    password = admin_password or config['password']
    env = {**os.environ, 'PGPASSWORD': password}

    result = subprocess.run(
        ['dropdb', '-h', config['host'], '-p', str(config['port']),
         '-U', user, '--if-exists', db_name],
        env=env, capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  [WARN] Failed to drop '{db_name}': {result.stderr.strip()}")
    else:
        print(f"  [OK] Dropped database '{db_name}'")


# ============================================================================
# Main
# ============================================================================

async def main():
    parser = argparse.ArgumentParser(
        description='ETL Pipeline Verification - compare fresh import vs production',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Smoke test with one source
  uv run scripts/verify_etl_pipeline.py --source guelph_addresses --keep-verify-db

  # Full verification (all sources, import-only)
  uv run scripts/verify_etl_pipeline.py

  # Resume after a partial run (imports only sources not yet completed)
  uv run scripts/verify_etl_pipeline.py --resume

  # Re-compare without re-running ETL
  uv run scripts/verify_etl_pipeline.py --skip-etl --verbose

  # Quick count-only check
  uv run scripts/verify_etl_pipeline.py --quick
        """
    )
    parser.add_argument(
        '--full-download', action='store_true',
        help='Re-download from live APIs (default: import-only from cached files)')
    parser.add_argument(
        '--keep-verify-db', action='store_true',
        help="Don't drop verify DB after comparison")
    parser.add_argument(
        '--verify-db-name', default='geocoder_verify',
        help='Verification database name (default: geocoder_verify)')
    parser.add_argument(
        '--source',
        help='Only verify a specific source_key')
    parser.add_argument(
        '--quick', action='store_true',
        help='Count + sample comparison only (skip full hash)')
    parser.add_argument(
        '--report',
        help='Write report to file (default: stdout only)')
    parser.add_argument(
        '--skip-etl', action='store_true',
        help='Skip ETL run, compare existing verify DB')
    parser.add_argument(
        '--resume', action='store_true',
        help='Resume a partial ETL run: reuse existing verify DB, import only sources not yet completed')
    parser.add_argument(
        '--verbose', action='store_true',
        help='Show per-row diffs for mismatched tables')
    parser.add_argument(
        '--diff', action='store_true',
        help='Show row-level breakdown for hash mismatches (identical/changed/only-in-prod/only-in-verify)')
    parser.add_argument(
        '--admin-user',
        help='PostgreSQL superuser for createdb/dropdb (default: same as DB_USER)')
    parser.add_argument(
        '--admin-password',
        help='Password for admin user (default: same as DB_PASSWORD)')

    args = parser.parse_args()
    timing = TimingInfo()
    total_start = time.time()

    # Phase 0: Pre-flight
    if not await preflight_checks(args):
        sys.exit(1)

    if args.resume:
        # Resume mode: reuse existing verify DB, run only missing sources
        print("\n--- Phase 1: Skipped (--resume, reusing existing verify DB) ---")

        print("\n--- Phase 2: Resume ETL Pipeline ---")
        t2 = time.time()

        # Sync data_sources so file paths match
        await sync_data_sources(args.verify_db_name)

        # Ensure boundary tables have data for transformers
        await ensure_boundary_tables(args.verify_db_name)

        etl_ok = await run_etl_resume(args, args.verify_db_name)
        if not etl_ok:
            print("  [WARN] ETL had errors, continuing with comparison...")

        # Import postal codes (separate from main ETL)
        run_postal_codes_import(args.verify_db_name)

        # Re-run setup transformers for derived tables
        print("  Running setup transformers...")
        await run_setup_transformers(args.verify_db_name)

        timing.etl = time.time() - t2

    elif not args.skip_etl:
        # Fresh run: create DB and run full ETL
        print("\n--- Phase 1: Create & Initialize Verify DB ---")
        t1 = time.time()

        if not create_verify_db(args.verify_db_name, args.admin_user,
                               args.admin_password):
            sys.exit(1)
        if not init_verify_db(args.verify_db_name, args.admin_user,
                              args.admin_password):
            print("  Cleaning up after failed init...")
            cleanup_verify_db(args.verify_db_name, args.admin_user,
                              args.admin_password)
            sys.exit(1)

        timing.db_init = time.time() - t1

        # Phase 2: Run ETL
        print("\n--- Phase 2: Run ETL Pipeline ---")
        t2 = time.time()

        # Sync data_sources from production so file paths match for --import-only
        await sync_data_sources(args.verify_db_name)

        # For single-source verification, copy boundary dependency tables
        if args.source:
            copy_dependency_tables(args.verify_db_name)

        etl_ok = run_etl(args, args.verify_db_name)
        if not etl_ok:
            print("  [WARN] ETL had errors, continuing with comparison...")

        # Import postal codes (separate from main ETL)
        run_postal_codes_import(args.verify_db_name)

        # Re-run setup transformers for derived tables
        print("  Running setup transformers...")
        await run_setup_transformers(args.verify_db_name)

        timing.etl = time.time() - t2
    else:
        print("\n--- Skipping Phases 1 & 2 (--skip-etl) ---")

    # Phase 3: Compare databases
    results = await compare_databases(args, timing)

    timing.total = time.time() - total_start

    # Phase 4: Report
    report = generate_report(results, timing, args)
    print()
    print(report)

    if args.report:
        with open(args.report, 'w') as f:
            f.write(report + "\n")
        print(f"\nReport written to: {args.report}")

    # Phase 5: Cleanup
    # --resume and --skip-etl default to keeping the DB (user may resume again)
    if not args.keep_verify_db and not args.skip_etl and not args.resume:
        print("\n--- Phase 5: Cleanup ---")
        cleanup_verify_db(args.verify_db_name, args.admin_user,
                          args.admin_password)
    elif args.keep_verify_db or args.resume:
        print(f"\n--- Verify DB retained: {args.verify_db_name} ---")

    # Exit code based on comparison results
    compared = [r for r in results if not r.skipped]
    all_passed = all(r.passed for r in compared) if compared else True
    sys.exit(0 if all_passed else 1)


if __name__ == '__main__':
    asyncio.run(main())
