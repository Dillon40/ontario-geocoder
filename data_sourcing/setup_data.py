#!/usr/bin/env python3
# Run in the project's locked environment (uv run python ...), not an isolated
# inline-script environment: child importers share this interpreter.

"""
Data Setup Orchestrator

Orchestrates the complete data setup process:
1. Seeds the data_sources table (if empty)
2. Downloads data sources (boundaries first, then addresses)
3. Imports data into the database
4. Updates record counts

Usage:
    # Full setup with all confirmed sources
    ./setup_data.py --all-confirmed

    # Download only (no import)
    ./setup_data.py --all-confirmed --download-only

    # Import only (assumes data already downloaded)
    ./setup_data.py --all-confirmed --import-only

    # Specific source
    ./setup_data.py --source guelph_addresses
"""
import sys
import os
import argparse
import asyncio
import subprocess
from pathlib import Path
from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
from data_sourcing.db_utils import get_db_connection, get_db_config

# Paths
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
DATA_DIR = SCRIPT_DIR / 'data_sources'
DOWNLOADERS_DIR = SCRIPT_DIR / 'downloaders'
IMPORTERS_DIR = SCRIPT_DIR / 'importers'

# Priority sources: must be imported first in this exact order
# These have dependencies that other sources rely on
PRIORITY_SOURCES = [
    'fsa_boundaries',           # Required for FSA spatial lookups
    'ontario_municipal_boundaries',  # Required for municipality spatial lookups
    'nar_ontario',              # Required for NAR enrichment
]

# Validation rules for priority sources — checked after import
PRIORITY_VALIDATION = {
    'fsa_boundaries': {
        'table': 'fsa_boundaries',
        'min_count': 1,
        'description': 'FSA boundary polygons (needed for spatial FSA lookups)',
    },
    'ontario_municipal_boundaries': {
        'table': 'lower_municipal_boundaries',
        'min_count': 1,
        'description': 'Municipal boundary polygons (needed for municipality spatial lookups)',
    },
}


async def get_import_order(conn) -> list[str]:
    """
    Get dynamic import order from database.

    Returns priority sources first (in fixed order), then remaining
    confirmed sources from the database.

    Args:
        conn: Database connection

    Returns:
        List of source_key strings in import order
    """
    # Get all confirmed sources from database
    rows = await conn.fetch("""
        SELECT source_key
        FROM data_sources
        WHERE status = 'confirmed'
        ORDER BY source_key ASC
    """)

    all_sources = [row['source_key'] for row in rows]

    # Build order: priority sources first, then remaining
    order = []
    for source in PRIORITY_SOURCES:
        if source in all_sources:
            order.append(source)

    # Add remaining sources (not in priority list)
    for source in all_sources:
        if source not in PRIORITY_SOURCES:
            order.append(source)

    return order


async def validate_priority_source(conn, source_key: str) -> tuple[bool, str]:
    """
    Validate that a priority source has sufficient data after import.

    Args:
        conn: Database connection
        source_key: The source_key to validate

    Returns:
        Tuple of (passed, message)
    """
    validation = PRIORITY_VALIDATION.get(source_key)
    if not validation:
        return True, f"{source_key}: no validation rule defined (OK)"

    table = validation['table']
    min_count = validation['min_count']
    description = validation['description']

    # Use a safe constant table name (from our own dict, not user input)
    count = await conn.fetchval(f"SELECT COUNT(*) FROM {table}")

    if count >= min_count:
        return True, f"{table}: {count:,} rows — {description}"
    else:
        return False, (
            f"CRITICAL: {table} has {count} rows (minimum {min_count} required) — "
            f"{description}"
        )


@dataclass
class ImportResult:
    """Holds results from an import operation."""
    source_key: str
    success: bool
    rows_processed: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_deleted: int = 0
    rows_failed: int = 0
    rows_skipped: int = 0
    duration_seconds: int = 0
    error_message: Optional[str] = None

    def summary_line(self) -> str:
        """Return a single-line summary of the import result."""
        if not self.success:
            return f"FAILED: {self.error_message or 'Unknown error'}"

        parts = []
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

        if parts:
            return f"{', '.join(parts)} ({self.duration_seconds}s)"
        elif self.rows_processed:
            return f"{self.rows_processed:,} processed ({self.duration_seconds}s)"
        return "Unchanged: no rows processed in this run"


async def get_latest_import_stats(conn, source_key: str, after_id: int = 0) -> Optional[dict]:
    """
    Query the most recent import stats for a source from import_history.

    Returns dict with import details or None if no imports exist.
    """
    row = await conn.fetchrow("""
        SELECT
            CASE WHEN bool_and(status = 'completed') THEN 'completed' ELSE 'failed' END AS status,
            sum(rows_processed) AS rows_processed, sum(rows_inserted) AS rows_inserted,
            sum(rows_updated) AS rows_updated, sum(rows_deleted) AS rows_deleted,
            sum(rows_failed) AS rows_failed, sum(rows_skipped) AS rows_skipped,
            sum(duration_seconds) AS duration_seconds,
            string_agg(error_message, '; ') AS error_message
        FROM import_history
        WHERE source_key = $1 AND id > $2
        HAVING count(*) > 0
    """, source_key, after_id)

    if row:
        return dict(row)
    return None


async def check_data_sources_seeded(conn) -> bool:
    """Check if data_sources table has been seeded."""
    count = await conn.fetchval("SELECT COUNT(*) FROM data_sources")
    return count > 0


async def run_seed_script(conn):
    """Run the seed_data_sources.sql script."""
    seed_file = PROJECT_ROOT / 'database' / 'seed_data_sources.sql'
    if not seed_file.exists():
        print(f"Error: Seed file not found: {seed_file}")
        return False

    print("Seeding data_sources table...")
    try:
        await conn.execute(seed_file.read_text())
    except Exception as error:
        print(f"Error seeding data_sources: {error}")
        return False

    print("✓ Data sources seeded successfully")
    return True


async def get_source_info(conn, source_key: str) -> dict:
    """Get source information from database."""
    row = await conn.fetchrow(
        "SELECT * FROM data_sources WHERE source_key = $1",
        source_key
    )
    return dict(row) if row else None


def run_download_script(source_key: str = None, all_confirmed: bool = False,
                        force: bool = False, skip_manual: bool = False) -> bool:
    """Run the download_data_sources.py script."""
    cmd = [sys.executable, str(DOWNLOADERS_DIR / 'download_data_sources.py')]

    if source_key:
        cmd.extend(['--source', source_key])
    elif all_confirmed:
        cmd.append('--all-confirmed')

    if force:
        cmd.append('--force')
    if skip_manual:
        cmd.append('--skip-manual')

    config = get_db_config()
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, env={**os.environ, 'DB_HOST': config['host']})
    return result.returncode == 0


def run_import_script(source_info: dict, force: bool = False) -> bool:
    """Run the appropriate import script for a source."""
    import_script = source_info.get('import_script')
    source_key = source_info['source_key']
    local_file_path = source_info.get('local_file_path')
    dataset_mapping_key = source_info.get('dataset_mapping_key')

    if not import_script:
        print(f"  No import_script configured for {source_key}")
        return False

    # Import scripts are in the importers/ subdirectory
    script_path = IMPORTERS_DIR / import_script

    if not script_path.exists():
        print(f"  Import script not found: {script_path}")
        return False

    # Get DB config for subprocess environment
    config = get_db_config()
    run_env = {**os.environ, 'DB_HOST': config['host']}

    # Build command based on import script type
    if 'nar' in import_script.lower():
        # NAR import needs special handling - find CSV files
        nar_dir = PROJECT_ROOT / (local_file_path or f'data_sources/{source_key}')

        # Find address and location files for Ontario (province 35)
        addr_files = sorted(nar_dir.glob('**/Address_35*.csv'))
        loc_files = sorted(nar_dir.glob('**/Location_35*.csv'))

        if not addr_files:
            print(f"  No NAR address files found in {nar_dir}")
            return False

        success = True
        # Build list of (addr_file, loc_file_or_None) pairs
        file_pairs = []
        for addr_file in addr_files:
            # Find matching location file
            part_suffix = addr_file.stem.replace('Address_', 'Location_')
            loc_file = None
            for lf in loc_files:
                if part_suffix in lf.stem:
                    loc_file = lf
                    break

            if not loc_file:
                print(f"  Note: No location file for {addr_file.name} (addresses imported without coords)")

            file_pairs.append((addr_file, loc_file))

        for i, (addr_file, loc_file) in enumerate(file_pairs):
            cmd = [
                sys.executable, str(script_path),
                '--addresses', str(addr_file),
                '--province', '35',
                '--batch-size', '5000'
            ]
            if force:
                cmd.append('--force')
            if loc_file:
                cmd.extend(['--locations', str(loc_file)])
            # Stale detection is only valid when a single file covers the
            # whole source. When NAR ships as multiple part files, each part
            # only sees its own records, so per-part stale detection would
            # flag the other parts' rows (~5.3M) as stale — skip it.
            # Single-file imports run full stale detection, now protected by
            # the STALE_DELETE_MIN_RATIO truncation guard in import_utils.
            if len(file_pairs) > 1:
                cmd.extend(['--stale-strategy', 'skip'])
            print(f"  Importing: {addr_file.name} ({i+1}/{len(file_pairs)})")
            result = subprocess.run(cmd, env=run_env)
            if result.returncode != 0:
                success = False

        if success:
            # Address CSVs carry EPSG:3347 coordinates even when location files
            # have no usable coordinates. Make Tier 3 usable on first setup.
            for module in ('add_latlong_to_nar', 'add_fsa_to_nar'):
                result = subprocess.run([sys.executable, '-m',
                    f'data_sourcing.enrichment.{module}', '--db', config['database']], env=run_env)
                if result.returncode:
                    return False
        return success

    elif 'boundaries' in import_script.lower():
        data_path = PROJECT_ROOT / (local_file_path or f'data_sourcing/data_sources/{source_key}')
        candidates = ([data_path] if data_path.is_file() else
                      sorted(data_path.glob('**/*.geojson')) + sorted(data_path.glob('**/*.shp')))
        if not candidates:
            print(f"  No boundary GeoJSON/shapefile found in {data_path}")
            return False
        cmd = [sys.executable, str(script_path), '--shapefile', str(candidates[0])]
        print(f"  Importing: {candidates[0].name}")
        result = subprocess.run(cmd, env=run_env)
        return result.returncode == 0

    else:
        # Production data import (GeoJSON)
        if local_file_path:
            file_path = PROJECT_ROOT / local_file_path
        else:
            file_path = DATA_DIR / source_key / f"{source_key}.geojson"

        if not file_path.exists():
            print(f"  Data file not found: {file_path}")
            return False

        source_name = dataset_mapping_key or source_key
        cmd = [
            sys.executable, str(script_path),
            '--file', str(file_path),
            '--source', source_name,
            '--batch-size', '1000'
        ]
        if force:
            cmd.append('--force')
        print(f"  Importing: {file_path.name}")
        result = subprocess.run(cmd, env=run_env)
        return result.returncode == 0


async def run_import_with_tracking(conn, source_info: dict, force: bool = False) -> ImportResult:
    """
    Run import script and query import_history for detailed stats.

    Args:
        conn: Database connection (for querying import_history)
        source_info: Source configuration dict

    Returns:
        ImportResult with detailed statistics
    """
    source_key = source_info['source_key']

    # Get the mapping key used for import_history lookup
    # Some sources use dataset_mapping_key, others use source_key
    mapping_key = source_info.get('dataset_mapping_key') or source_key

    # Run the import
    before_id = await conn.fetchval('SELECT COALESCE(max(id), 0) FROM import_history')
    success = run_import_script(source_info, force=force)

    # Only this invocation's records; aggregate every NAR part, including failures.
    stats = await get_latest_import_stats(conn, mapping_key, before_id)

    if stats:
        return ImportResult(
            source_key=source_key,
            success=success and stats.get('status') == 'completed' and not stats.get('rows_failed'),
            rows_processed=stats.get('rows_processed') or 0,
            rows_inserted=stats.get('rows_inserted') or 0,
            rows_updated=stats.get('rows_updated') or 0,
            rows_deleted=stats.get('rows_deleted') or 0,
            rows_failed=stats.get('rows_failed') or 0,
            rows_skipped=stats.get('rows_skipped') or 0,
            duration_seconds=stats.get('duration_seconds') or 0,
            error_message=stats.get('error_message') or (
                f"{stats['rows_failed']} rows failed; see import_row_errors" if stats.get('rows_failed') else
                None if success else 'Importer or enrichment subprocess failed; see output above')
        )

    # No stats found - return basic result
    return ImportResult(
        source_key=source_key,
        success=success,
        error_message="No import history recorded" if not success else None
    )


def collect_commercial_consents(sources: list) -> set:
    """
    Collect user consent for sources without explicit commercial use permission.
    Returns set of source_keys that user has approved.
    """
    # Find sources needing consent
    needs_consent = []
    for src in sources:
        commercial = src.get('commercial_use_allowed')
        if commercial is not True:  # FALSE or NULL both need consent
            needs_consent.append(src)

    if not needs_consent:
        return set()  # All sources allow commercial use

    print(f"\n{'='*60}")
    print("COMMERCIAL USE CONSENT REQUIRED")
    print(f"{'='*60}")
    print(f"\nThe following {len(needs_consent)} source(s) do not have explicit")
    print("commercial use permission. You must acknowledge this before importing:\n")

    for i, src in enumerate(needs_consent, 1):
        commercial = src.get('commercial_use_allowed')
        status = "NOT ALLOWED" if commercial is False else "UNKNOWN (not specified)"
        license_info = src.get('license_type') or 'Not specified'
        license_url = src.get('license_url') or ''

        print(f"  {i}. {src['source_name']}")
        print(f"     Source key: {src['source_key']}")
        print(f"     Commercial use: {status}")
        print(f"     License: {license_info}")
        if license_url:
            print(f"     License URL: {license_url}")
        print()

    print("Options:")
    print("  [y] Accept ALL sources above (proceed with import)")
    print("  [n] Skip ALL sources above (only import commercial-allowed sources)")
    print("  [q] Quit (do not import anything)")
    print()

    while True:
        response = input("Your choice (y/n/q): ").strip().lower()
        if response == 'y':
            print("\n✓ Consent recorded for non-commercial sources")
            return {src['source_key'] for src in needs_consent}
        elif response == 'n':
            print("\n→ Skipping non-commercial sources")
            return set()
        elif response == 'q':
            print("\nExiting without importing.")
            sys.exit(0)
        else:
            print("Please enter 'y', 'n', or 'q'")


def sort_sources_by_commercial(sources: list, import_order: list) -> list:
    """
    Sort sources: commercial_use_allowed=TRUE first, then others.
    Within each group, maintain the import_order sequence.
    """
    # Create order index map
    order_map = {key: i for i, key in enumerate(import_order)}

    def sort_key(src):
        commercial = src.get('commercial_use_allowed')
        # Commercial allowed = 0 (first), others = 1 (second)
        commercial_priority = 0 if commercial is True else 1
        # Then sort by import order
        order_priority = order_map.get(src['source_key'], 999)
        return (commercial_priority, order_priority)

    return sorted(sources, key=sort_key)


async def main():
    parser = argparse.ArgumentParser(
        description='Data Setup Orchestrator - Downloads and imports geocoding data',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full setup with all confirmed sources
  DB_HOST=localhost %(prog)s --all-confirmed

  # Download only (no import)
  DB_HOST=localhost %(prog)s --all-confirmed --download-only

  # Import only (assumes data already downloaded)
  DB_HOST=localhost %(prog)s --all-confirmed --import-only

  # Specific source
  DB_HOST=localhost %(prog)s --source guelph_addresses

  # Skip sources explicitly marked manual (NAR is automatic)
  DB_HOST=localhost %(prog)s --all-confirmed --skip-manual

  # Accept non-commercial sources without prompting
  DB_HOST=localhost %(prog)s --all-confirmed --accept-non-commercial
        """
    )
    parser.add_argument('--source', '-s', help='Specific source_key to process')
    parser.add_argument('--all-confirmed', '-a', action='store_true',
                        help='Process all confirmed sources')
    parser.add_argument('--download-only', '-d', action='store_true',
                        help='Only download, do not import')
    parser.add_argument('--import-only', '-i', action='store_true',
                        help='Only import (assumes data downloaded)')
    parser.add_argument('--force', '-f', action='store_true',
                        help='Force re-download and re-import (with --import-only, only re-import)')
    parser.add_argument('--skip-manual', action='store_true',
                        help='Skip sources explicitly marked manual (not NAR)')
    parser.add_argument('--accept-non-commercial', action='store_true',
                        help='Accept sources without commercial use permission (no prompt)')
    parser.add_argument('--commercial-only', action='store_true',
                        help='Only process sources with explicit commercial use permission')
    parser.add_argument('--list', '-l', action='store_true',
                        help='List sources and their status')

    args = parser.parse_args()

    if not args.source and not args.all_confirmed and not args.list:
        parser.print_help()
        sys.exit(1)

    conn = await get_db_connection()

    try:
        # Check and seed data_sources if needed
        if not await check_data_sources_seeded(conn):
            print("Data sources table is empty. Running seed script...")
            if not await run_seed_script(conn):
                print("Failed to seed data sources. Exiting.")
                sys.exit(1)

        # List mode
        if args.list:
            sources = await conn.fetch("""
                SELECT source_key, source_name, status, import_script,
                       requires_manual_download, commercial_use_allowed,
                       last_data_refresh, record_count
                FROM data_sources
                WHERE status = 'confirmed'
                ORDER BY
                    CASE WHEN commercial_use_allowed = TRUE THEN 0 ELSE 1 END,
                    source_key
            """)
            print("\nConfirmed Data Sources:")
            print("-" * 115)
            print(f"{'Source Key':<30} {'Commercial':<12} {'Manual':<8} {'Records':<12} {'Last Refresh'}")
            print("-" * 115)
            for src in sources:
                commercial = '✓ Yes' if src['commercial_use_allowed'] is True else \
                             '✗ No' if src['commercial_use_allowed'] is False else '? Unknown'
                manual = 'Yes' if src['requires_manual_download'] else 'No'
                records = f"{src['record_count']:,}" if src['record_count'] else '-'
                refreshed = src['last_data_refresh'].strftime('%Y-%m-%d') if src['last_data_refresh'] else 'Never'
                print(f"{src['source_key']:<30} {commercial:<12} {manual:<8} {records:<12} {refreshed}")
            return

        # Get all source info from database
        if args.source:
            source_keys = [args.source]
        else:
            source_keys = await get_import_order(conn)

        # Fetch full source info for all sources
        all_sources = []
        for source_key in source_keys:
            source_info = await get_source_info(conn, source_key)
            if source_info and source_info['status'] == 'confirmed':
                all_sources.append(source_info)

        if not all_sources:
            print("No confirmed sources found to process.")
            sys.exit(0)

        # Filter by skip conditions
        sources_to_process = []
        skipped = []

        for source_info in all_sources:
            source_key = source_info['source_key']

            # Skip manual downloads if requested
            if args.skip_manual and source_info.get('requires_manual_download'):
                skipped.append((source_key, 'requires manual download'))
                continue

            # Skip non-commercial if requested
            if args.commercial_only and source_info.get('commercial_use_allowed') is not True:
                skipped.append((source_key, 'commercial use not allowed'))
                continue

            sources_to_process.append(source_info)

        if not sources_to_process:
            print("No sources to process after filtering.")
            if skipped:
                print("\nSkipped sources:")
                for key, reason in skipped:
                    print(f"  - {key}: {reason}")
            sys.exit(0)

        # Sort: commercial-allowed first, then maintain import order
        sources_to_process = sort_sources_by_commercial(sources_to_process, source_keys)

        # Collect consents UPFRONT for non-commercial sources
        if not args.accept_non_commercial and not args.commercial_only:
            approved_non_commercial = collect_commercial_consents(sources_to_process)
        else:
            # Auto-approve all if flag set
            approved_non_commercial = {src['source_key'] for src in sources_to_process
                                       if src.get('commercial_use_allowed') is not True}

        # Process sources
        download_success = []
        download_failed = []
        import_results: list[ImportResult] = []

        for source_info in sources_to_process:
            source_key = source_info['source_key']
            commercial = source_info.get('commercial_use_allowed')

            # Check if we have consent for non-commercial sources
            if commercial is not True and source_key not in approved_non_commercial:
                skipped.append((source_key, 'commercial use consent not given'))
                continue

            print(f"\n{'='*60}")
            commercial_status = "✓ Commercial use allowed" if commercial is True else "⚠ Non-commercial (consent given)"
            print(f"Processing: {source_info['source_name']}")
            print(f"            {commercial_status}")
            print(f"{'='*60}")

            # Download phase
            if not args.import_only:
                print(f"\n[1/2] Downloading {source_key}...")
                if run_download_script(source_key, force=args.force):
                    download_success.append(source_key)
                else:
                    download_failed.append(source_key)
                    if not args.download_only:
                        print(f"  Skipping import due to download failure")
                    continue

            # Import phase
            if not args.download_only:
                print(f"\n[2/2] Importing {source_key}...")
                result = await run_import_with_tracking(conn, source_info, force=args.force)
                import_results.append(result)
                if result.success:
                    print(f"  ✓ {result.summary_line()}")
                else:
                    print(f"  ✗ {result.summary_line()}")

                # Validate priority sources after import
                if source_key in PRIORITY_VALIDATION:
                    passed, msg = await validate_priority_source(conn, source_key)
                    if passed:
                        print(f"  ✓ Validation: {msg}")
                    else:
                        print(f"  ⚠ Validation: {msg}")
                        result.success = False
                        result.error_message = msg
                        # Continue independent sources, report all failures, and
                        # exit nonzero. Never block unattended jobs on stdin.

        # Summary
        print(f"\n{'='*60}")
        print("SETUP SUMMARY")
        print(f"{'='*60}")

        if not args.import_only:
            print(f"\nDownloads:")
            print(f"  Successful: {len(download_success)}")
            if download_success:
                for s in download_success:
                    print(f"    ✓ {s}")
            print(f"  Failed:     {len(download_failed)}")
            if download_failed:
                for s in download_failed:
                    print(f"    ✗ {s}")

        if not args.download_only and import_results:
            successful = [r for r in import_results if r.success]
            failed = [r for r in import_results if not r.success]

            # Calculate totals
            total_inserted = sum(r.rows_inserted for r in successful)
            total_updated = sum(r.rows_updated for r in successful)
            total_deleted = sum(r.rows_deleted for r in successful)
            total_failed = sum(r.rows_failed for r in import_results)
            total_duration = sum(r.duration_seconds for r in import_results)

            print(f"\nImports:")
            print(f"  Successful: {len(successful)}")
            if successful:
                for r in successful:
                    print(f"    ✓ {r.source_key}: {r.summary_line()}")
            print(f"  Failed:     {len(failed)}")
            if failed:
                for r in failed:
                    print(f"    ✗ {r.source_key}: {r.summary_line()}")

            # Print totals
            print(f"\n  Totals:")
            if total_inserted:
                print(f"    Rows inserted: {total_inserted:,}")
            if total_updated:
                print(f"    Rows updated:  {total_updated:,}")
            if total_deleted:
                print(f"    Rows deleted:  {total_deleted:,}")
            if total_failed:
                print(f"    Rows failed:   {total_failed:,}")
            if total_duration:
                mins, secs = divmod(total_duration, 60)
                print(f"    Total time:    {mins}m {secs}s")

        if skipped:
            print(f"\nSkipped: {len(skipped)}")
            for item in skipped:
                if isinstance(item, tuple):
                    key, reason = item
                    print(f"    - {key}: {reason}")
                else:
                    print(f"    - {item}")

        print(f"\n{'='*60}")
        if download_failed or any(not r.success for r in import_results):
            sys.exit(1)

    finally:
        await conn.close()


if __name__ == '__main__':
    asyncio.run(main())
