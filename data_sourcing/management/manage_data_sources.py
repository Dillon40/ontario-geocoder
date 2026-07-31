#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "asyncpg",
#   "python-dotenv",
# ]
# ///

"""
CLI for managing data sources in the geocoder database.
Provides commands for adding, updating, and querying data sources.
"""
import os
import sys
import json
import argparse
import asyncio
import subprocess
from pathlib import Path
from dotenv import load_dotenv
import asyncpg

# Add project root to path for db_utils import
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from data_sourcing.db_utils import get_db_pool

# Load environment variables
load_dotenv()


def regenerate_docs():
    """Regenerate data source documentation from database."""
    try:
        script_path = Path(__file__).parent / 'generate_data_source_docs.py'
        result = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            check=True,
            env=os.environ.copy()
        )
        print("\n" + "="*60)
        print("Documentation regenerated:")
        print("="*60)
        print(result.stdout)
        return True
    except subprocess.CalledProcessError as e:
        print(f"\n⚠ Warning: Failed to regenerate docs: {e.stderr}")
        return False


async def get_pool():
    """Create database connection pool."""
    return await get_db_pool(min_size=1, max_size=5)


# =============================================================================
# DATA SOURCE COMMANDS
# =============================================================================

async def cmd_add_source(args):
    """Add a new data source."""
    pool = await get_pool()
    try:
        # Parse regions as array
        regions = [r.strip() for r in args.regions.split(',')]

        # Parse optional arrays
        postal_prefixes = None
        if args.postal_prefixes:
            postal_prefixes = [p.strip() for p in args.postal_prefixes.split(',')]

        query = '''
            INSERT INTO data_sources (
                source_key, source_name, status, regions_covered, region_type,
                postal_prefixes, portal_url, scraping_notes, api_endpoint,
                api_type, api_key_required, api_rate_limit, api_notes,
                license_type, license_url, commercial_use_allowed, license_notes, notes
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18
            )
            RETURNING id
        '''

        result = await pool.fetchval(
            query,
            args.key,
            args.name,
            args.status,
            regions,
            args.region_type,
            postal_prefixes,
            args.portal_url,
            args.scraping_notes,
            args.api_endpoint,
            args.api_type,
            args.api_key_required,
            args.api_rate_limit,
            args.api_notes,
            args.license_type,
            args.license_url,
            args.commercial_use,
            args.license_notes,
            args.notes
        )

        print(f"✓ Added data source '{args.name}' with ID {result}")

        # Add data types if specified
        if args.data_types:
            types = [t.strip() for t in args.data_types.split(',')]
            await add_types_to_source(pool, result, types)

        # Auto-regenerate documentation
        if not hasattr(args, 'no_regenerate') or not args.no_regenerate:
            regenerate_docs()

    except asyncpg.UniqueViolationError:
        print(f"[FAIL] Source with key '{args.key}' already exists")
        sys.exit(1)
    finally:
        await pool.close()


async def cmd_update_source(args):
    """Update an existing data source."""
    pool = await get_pool()
    try:
        # Build dynamic update query based on provided arguments
        updates = []
        values = []
        param_num = 1

        update_fields = {
            'name': 'source_name',
            'status': 'status',
            'region_type': 'region_type',
            'portal_url': 'portal_url',
            'scraping_notes': 'scraping_notes',
            'api_endpoint': 'api_endpoint',
            'api_type': 'api_type',
            'api_key_required': 'api_key_required',
            'api_rate_limit': 'api_rate_limit',
            'api_notes': 'api_notes',
            'license_type': 'license_type',
            'license_url': 'license_url',
            'commercial_use': 'commercial_use_allowed',
            'license_notes': 'license_notes',
            'notes': 'notes',
            'record_count': 'record_count',
        }

        for arg_name, db_field in update_fields.items():
            value = getattr(args, arg_name, None)
            if value is not None:
                updates.append(f"{db_field} = ${param_num}")
                values.append(value)
                param_num += 1

        # Handle array fields
        if args.regions:
            regions = [r.strip() for r in args.regions.split(',')]
            updates.append(f"regions_covered = ${param_num}")
            values.append(regions)
            param_num += 1

        if args.postal_prefixes:
            prefixes = [p.strip() for p in args.postal_prefixes.split(',')]
            updates.append(f"postal_prefixes = ${param_num}")
            values.append(prefixes)
            param_num += 1

        if not updates:
            print("No updates specified")
            return

        # Add the key for WHERE clause
        values.append(args.key)
        where_param = param_num

        query = f'''
            UPDATE data_sources
            SET {', '.join(updates)}
            WHERE source_key = ${where_param}
            RETURNING id
        '''

        result = await pool.fetchval(query, *values)

        if result:
            print(f"✓ Updated data source '{args.key}'")
        else:
            print(f"[FAIL] Source with key '{args.key}' not found")
            sys.exit(1)

    finally:
        await pool.close()


async def cmd_list_sources(args):
    """List data sources with optional filtering."""
    pool = await get_pool()
    try:
        # Build query with optional filters
        conditions = []
        values = []
        param_num = 1

        if args.status:
            conditions.append(f"status = ${param_num}")
            values.append(args.status)
            param_num += 1

        if args.region:
            conditions.append(f"${param_num} = ANY(regions_covered)")
            values.append(args.region)
            param_num += 1

        if args.data_type:
            conditions.append(f"""
                id IN (
                    SELECT dst.data_source_id
                    FROM data_source_types dst
                    JOIN data_types dt ON dst.data_type_id = dt.id
                    WHERE dt.type_key = ${param_num}
                )
            """)
            values.append(args.data_type)
            param_num += 1

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        query = f'''
            SELECT
                ds.source_key,
                ds.source_name,
                ds.status,
                ds.regions_covered,
                ds.commercial_use_allowed,
                COALESCE(
                    array_agg(dt.type_key ORDER BY dt.type_key)
                    FILTER (WHERE dt.type_key IS NOT NULL),
                    ARRAY[]::TEXT[]
                ) as data_types
            FROM data_sources ds
            LEFT JOIN data_source_types dst ON ds.id = dst.data_source_id
            LEFT JOIN data_types dt ON dst.data_type_id = dt.id
            {where_clause}
            GROUP BY ds.id
            ORDER BY ds.source_name
        '''

        sources = await pool.fetch(query, *values)

        if not sources:
            print("No sources found matching criteria")
            return

        # Display results
        print(f"\n{'Source Key':<20} {'Status':<12} {'Commercial':<10} {'Data Types':<30} {'Regions'}")
        print("=" * 100)

        for source in sources:
            commercial = "Yes" if source['commercial_use_allowed'] else ("No" if source['commercial_use_allowed'] is False else "?")
            types = ', '.join(source['data_types'][:3])
            if len(source['data_types']) > 3:
                types += f" (+{len(source['data_types']) - 3})"
            regions = ', '.join(source['regions_covered'][:2])
            if len(source['regions_covered']) > 2:
                regions += f" (+{len(source['regions_covered']) - 2})"

            print(f"{source['source_key']:<20} {source['status']:<12} {commercial:<10} {types:<30} {regions}")

        print(f"\nTotal: {len(sources)} sources")

    finally:
        await pool.close()


async def cmd_show_source(args):
    """Show detailed info for a single source."""
    pool = await get_pool()
    try:
        query = '''
            SELECT
                ds.*,
                COALESCE(
                    array_agg(dt.display_name ORDER BY dt.display_name)
                    FILTER (WHERE dt.display_name IS NOT NULL),
                    ARRAY[]::TEXT[]
                ) as data_types
            FROM data_sources ds
            LEFT JOIN data_source_types dst ON ds.id = dst.data_source_id
            LEFT JOIN data_types dt ON dst.data_type_id = dt.id
            WHERE ds.source_key = $1
            GROUP BY ds.id
        '''

        source = await pool.fetchrow(query, args.key)

        if not source:
            print(f"[FAIL] Source '{args.key}' not found")
            sys.exit(1)

        # Display detailed info
        print(f"\n{'=' * 60}")
        print(f"DATA SOURCE: {source['source_name']}")
        print(f"{'=' * 60}")
        print(f"Key: {source['source_key']}")
        print(f"Status: {source['status']}")
        print(f"Regions: {', '.join(source['regions_covered'])}")
        if source['region_type']:
            print(f"Region Type: {source['region_type']}")
        if source['postal_prefixes']:
            print(f"Postal Prefixes: {', '.join(source['postal_prefixes'])}")
        if source['data_types']:
            print(f"Data Types: {', '.join(source['data_types'])}")

        print(f"\n--- Manual Scraping ---")
        print(f"Portal URL: {source['portal_url'] or 'N/A'}")
        if source['scraping_notes']:
            print(f"Notes: {source['scraping_notes']}")

        print(f"\n--- API Access ---")
        print(f"Endpoint: {source['api_endpoint'] or 'N/A'}")
        if source['api_type']:
            print(f"Type: {source['api_type']}")
        print(f"Key Required: {'Yes' if source['api_key_required'] else 'No'}")
        if source['api_rate_limit']:
            print(f"Rate Limit: {source['api_rate_limit']}")
        if source['api_notes']:
            print(f"Notes: {source['api_notes']}")

        print(f"\n--- Licensing ---")
        print(f"License: {source['license_type'] or 'N/A'}")
        if source['license_url']:
            print(f"URL: {source['license_url']}")
        commercial = "Yes" if source['commercial_use_allowed'] else ("No" if source['commercial_use_allowed'] is False else "Unknown")
        print(f"Commercial Use: {commercial}")
        if source['license_notes']:
            print(f"Notes: {source['license_notes']}")

        print(f"\n--- Import Status ---")
        print(f"Record Count: {source['record_count'] or 'N/A'}")
        print(f"Last Refresh: {source['last_data_refresh'] or 'N/A'}")

        if source['notes']:
            print(f"\n--- Notes ---")
            print(source['notes'])

        print(f"{'=' * 60}\n")

    finally:
        await pool.close()


async def cmd_delete_source(args):
    """Delete a data source."""
    pool = await get_pool()
    try:
        result = await pool.execute(
            "DELETE FROM data_sources WHERE source_key = $1",
            args.key
        )

        if result == "DELETE 1":
            print(f"✓ Deleted data source '{args.key}'")
        else:
            print(f"[FAIL] Source '{args.key}' not found")
            sys.exit(1)

    finally:
        await pool.close()


# =============================================================================
# DATA TYPE COMMANDS
# =============================================================================

async def cmd_list_types(args):
    """List all data types."""
    pool = await get_pool()
    try:
        query = '''
            SELECT
                dt.type_key,
                dt.display_name,
                dt.description,
                COUNT(dst.data_source_id) as source_count
            FROM data_types dt
            LEFT JOIN data_source_types dst ON dt.id = dst.data_type_id
            GROUP BY dt.id
            ORDER BY dt.type_key
        '''

        types = await pool.fetch(query)

        print(f"\n{'Type Key':<25} {'Display Name':<25} {'Sources':<10} {'Description'}")
        print("=" * 100)

        for t in types:
            desc = (t['description'][:35] + '...') if t['description'] and len(t['description']) > 35 else (t['description'] or '')
            print(f"{t['type_key']:<25} {t['display_name']:<25} {t['source_count']:<10} {desc}")

        print(f"\nTotal: {len(types)} data types")

    finally:
        await pool.close()


async def cmd_add_type(args):
    """Add a new data type."""
    pool = await get_pool()
    try:
        result = await pool.fetchval(
            '''
            INSERT INTO data_types (type_key, display_name, description)
            VALUES ($1, $2, $3)
            RETURNING id
            ''',
            args.key,
            args.display_name,
            args.description
        )

        print(f"✓ Added data type '{args.key}' with ID {result}")

    except asyncpg.UniqueViolationError:
        print(f"[FAIL] Data type '{args.key}' already exists")
        sys.exit(1)
    finally:
        await pool.close()


async def add_types_to_source(pool: asyncpg.Pool, source_id: int, type_keys: list):
    """Helper to add data types to a source."""
    for type_key in type_keys:
        # Get type ID
        type_id = await pool.fetchval(
            "SELECT id FROM data_types WHERE type_key = $1",
            type_key
        )

        if not type_id:
            print(f"  ⚠ Data type '{type_key}' not found, skipping")
            continue

        try:
            await pool.execute(
                '''
                INSERT INTO data_source_types (data_source_id, data_type_id)
                VALUES ($1, $2)
                ON CONFLICT DO NOTHING
                ''',
                source_id,
                type_id
            )
            print(f"  ✓ Linked data type '{type_key}'")
        except Exception as e:
            print(f"  ⚠ Error linking '{type_key}': {e}")


async def cmd_link_types(args):
    """Link data types to a source."""
    pool = await get_pool()
    try:
        # Get source ID
        source_id = await pool.fetchval(
            "SELECT id FROM data_sources WHERE source_key = $1",
            args.source_key
        )

        if not source_id:
            print(f"[FAIL] Source '{args.source_key}' not found")
            sys.exit(1)

        type_keys = [t.strip() for t in args.types.split(',')]
        await add_types_to_source(pool, source_id, type_keys)

    finally:
        await pool.close()


async def cmd_unlink_types(args):
    """Remove data types from a source."""
    pool = await get_pool()
    try:
        # Get source ID
        source_id = await pool.fetchval(
            "SELECT id FROM data_sources WHERE source_key = $1",
            args.source_key
        )

        if not source_id:
            print(f"[FAIL] Source '{args.source_key}' not found")
            sys.exit(1)

        type_keys = [t.strip() for t in args.types.split(',')]

        for type_key in type_keys:
            result = await pool.execute(
                '''
                DELETE FROM data_source_types
                WHERE data_source_id = $1
                AND data_type_id = (SELECT id FROM data_types WHERE type_key = $2)
                ''',
                source_id,
                type_key
            )

            if result == "DELETE 1":
                print(f"✓ Unlinked data type '{type_key}'")
            else:
                print(f"⚠ Data type '{type_key}' was not linked")

    finally:
        await pool.close()


# =============================================================================
# QUERY COMMANDS
# =============================================================================

async def cmd_sources_by_type(args):
    """Find all sources that have a specific data type."""
    pool = await get_pool()
    try:
        query = '''
            SELECT
                ds.source_key,
                ds.source_name,
                ds.status,
                ds.regions_covered,
                ds.commercial_use_allowed
            FROM data_sources ds
            JOIN data_source_types dst ON ds.id = dst.data_source_id
            JOIN data_types dt ON dst.data_type_id = dt.id
            WHERE dt.type_key = $1
            ORDER BY ds.source_name
        '''

        sources = await pool.fetch(query, args.type_key)

        if not sources:
            print(f"No sources found with data type '{args.type_key}'")
            return

        print(f"\nSources with '{args.type_key}' data:")
        print("=" * 80)

        for source in sources:
            commercial = "✓" if source['commercial_use_allowed'] else ("✗" if source['commercial_use_allowed'] is False else "?")
            regions = ', '.join(source['regions_covered'][:3])
            print(f"  [{source['status']:<10}] {source['source_key']:<20} {commercial} {regions}")

        print(f"\nTotal: {len(sources)} sources")

    finally:
        await pool.close()


async def cmd_types_for_region(args):
    """Find all data types available for a region."""
    pool = await get_pool()
    try:
        query = '''
            SELECT DISTINCT dt.type_key, dt.display_name
            FROM data_types dt
            JOIN data_source_types dst ON dt.id = dst.data_type_id
            JOIN data_sources ds ON ds.id = dst.data_source_id
            WHERE $1 = ANY(ds.regions_covered)
            ORDER BY dt.type_key
        '''

        types = await pool.fetch(query, args.region)

        if not types:
            print(f"No data types found for region '{args.region}'")
            return

        print(f"\nData types available for '{args.region}':")
        print("=" * 50)

        for t in types:
            print(f"  - {t['type_key']}: {t['display_name']}")

        print(f"\nTotal: {len(types)} data types")

    finally:
        await pool.close()


async def cmd_common_types(args):
    """Find data types common across multiple sources."""
    pool = await get_pool()
    try:
        source_keys = [s.strip() for s in args.sources.split(',')]

        # Get source IDs
        source_ids = await pool.fetch(
            "SELECT id FROM data_sources WHERE source_key = ANY($1)",
            source_keys
        )

        if len(source_ids) != len(source_keys):
            print("⚠ Some source keys not found")

        if not source_ids:
            print("[FAIL] No valid sources found")
            sys.exit(1)

        ids = [s['id'] for s in source_ids]

        query = '''
            SELECT dt.type_key, dt.display_name, COUNT(*) as source_count
            FROM data_types dt
            JOIN data_source_types dst ON dt.id = dst.data_type_id
            WHERE dst.data_source_id = ANY($1)
            GROUP BY dt.id
            HAVING COUNT(*) = $2
            ORDER BY dt.type_key
        '''

        types = await pool.fetch(query, ids, len(ids))

        print(f"\nData types common to all {len(ids)} sources:")
        print("=" * 50)

        if types:
            for t in types:
                print(f"  - {t['type_key']}: {t['display_name']}")
            print(f"\nTotal: {len(types)} common data types")
        else:
            print("  No common data types found")

    finally:
        await pool.close()


async def cmd_export_json(args):
    """Export all sources as JSON."""
    pool = await get_pool()
    try:
        query = '''
            SELECT
                ds.*,
                COALESCE(
                    array_agg(dt.type_key ORDER BY dt.type_key)
                    FILTER (WHERE dt.type_key IS NOT NULL),
                    ARRAY[]::TEXT[]
                ) as data_types
            FROM data_sources ds
            LEFT JOIN data_source_types dst ON ds.id = dst.data_source_id
            LEFT JOIN data_types dt ON dst.data_type_id = dt.id
            GROUP BY ds.id
            ORDER BY ds.source_name
        '''

        sources = await pool.fetch(query)

        # Convert to JSON-serializable format
        output = []
        for source in sources:
            item = dict(source)
            # Convert special types
            item['regions_covered'] = list(item['regions_covered']) if item['regions_covered'] else []
            item['postal_prefixes'] = list(item['postal_prefixes']) if item['postal_prefixes'] else []
            item['data_types'] = list(item['data_types']) if item['data_types'] else []
            # Remove non-serializable fields
            for key in ['bbox', 'created_at', 'updated_at', 'last_data_refresh']:
                if item.get(key):
                    item[key] = str(item[key])
            output.append(item)

        print(json.dumps(output, indent=2))

    finally:
        await pool.close()


# =============================================================================
# HEALTH CHECK COMMANDS
# =============================================================================

async def cmd_check_health(args):
    """Check health of data sources."""
    # Import health service
    sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'engine'))
    from services.source_health_service import SourceHealthService, SourceHealthScheduler

    pool = await get_pool()
    try:
        service = SourceHealthService(pool)
        scheduler = SourceHealthScheduler(pool)

        if args.source:
            # Check specific source
            print(f"\nChecking health of '{args.source}'...")
            result = await service.check_source(args.source)

            print(f"\n{'=' * 60}")
            print(f"Health Check: {args.source}")
            print(f"{'=' * 60}")
            print(f"Checked at: {result.checked_at}")
            print(f"HTTP Status: {result.http_status}")
            print(f"Response Time: {result.response_time_ms}ms")
            if result.record_count:
                print(f"Record Count: {result.record_count:,}")
            if result.arcgis_last_edit_date:
                print(f"ArcGIS Last Edit: {result.arcgis_last_edit_date}")
            if result.schema_hash:
                print(f"Schema Hash: {result.schema_hash[:16]}...")
            if result.has_changes:
                print(f"Changes Detected: YES ({result.change_type})")
            else:
                print(f"Changes Detected: No")
            if result.error_message:
                print(f"Error: {result.error_type} - {result.error_message}")

        elif args.all:
            # Check all confirmed sources
            print("\nChecking all confirmed sources...")
            results = await service.check_all_confirmed()

            changes = [r for r in results if r.has_changes]
            errors = [r for r in results if r.error_type]
            ok = [r for r in results if not r.has_changes and not r.error_type]

            print(f"\n{'=' * 60}")
            print(f"Health Check Summary")
            print(f"{'=' * 60}")
            print(f"Total checked: {len(results)}")
            print(f"  OK (no changes): {len(ok)}")
            print(f"  Changes detected: {len(changes)}")
            print(f"  Errors: {len(errors)}")

            if changes:
                print(f"\nSources with changes:")
                for r in changes:
                    print(f"  - {r.source_key} ({r.change_type})")

            if errors:
                print(f"\nSources with errors:")
                for r in errors:
                    print(f"  - {r.source_key}: {r.error_type}")

        elif args.due:
            # Check only due sources
            print("\nChecking sources due for health check...")
            stats = await scheduler.run_once()

            print(f"\n{'=' * 60}")
            print(f"Health Check Summary")
            print(f"{'=' * 60}")
            print(f"Sources checked: {stats['checked']}")
            print(f"Changes detected: {stats['changes_detected']}")
            print(f"Errors: {stats['errors']}")

            if stats['sources']:
                print(f"\nDetails:")
                for s in stats['sources']:
                    status = 'ERROR' if s['error'] else ('CHANGES' if s['has_changes'] else 'OK')
                    print(f"  - {s['source_key']}: {status}")

        else:
            # Show due sources without checking
            due = await scheduler.get_sources_due_for_check()
            print(f"\n{len(due)} sources due for health check:")
            for key in due:
                print(f"  - {key}")
            print(f"\nUse --due to run checks, or --all to check all sources.")

    finally:
        await pool.close()


async def cmd_show_stale(args):
    """Show sources needing attention (stale or error)."""
    pool = await get_pool()
    try:
        rows = await pool.fetch("""
            SELECT * FROM v_sources_needing_attention
        """)

        if not rows:
            print("\nNo sources need attention. All confirmed sources are synced.")
            return

        print(f"\n{'=' * 80}")
        print(f"Sources Needing Attention ({len(rows)} total)")
        print(f"{'=' * 80}")

        for row in rows:
            print(f"\n{row['source_key']}:")
            print(f"  Status: {row['sync_status']}")
            if row['last_sync_check']:
                print(f"  Last checked: {row['last_sync_check']}")
            if row['time_since_check']:
                hours = row['time_since_check'].total_seconds() / 3600
                days = hours / 24
                if days >= 1:
                    print(f"  Time since check: {days:.1f} days")
                else:
                    print(f"  Time since check: {hours:.1f} hours")
            if row['last_sync_error']:
                print(f"  Error: {row['last_sync_error']}")
            if args.verbose:
                if row['change_type']:
                    print(f"  Last change type: {row['change_type']}")
                if row['stored_record_count']:
                    print(f"  Stored record count: {row['stored_record_count']:,}")
                if row['latest_record_count']:
                    print(f"  Latest record count: {row['latest_record_count']:,}")

    finally:
        await pool.close()


async def cmd_health_daemon(args):
    """Run health check daemon."""
    sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'engine'))
    from services.source_health_service import SourceHealthScheduler

    pool = await get_pool()
    try:
        scheduler = SourceHealthScheduler(pool)
        print(f"Starting health check daemon (interval: {args.interval}s)")
        print("Press Ctrl+C to stop...")
        try:
            await scheduler.run_daemon(check_interval_seconds=args.interval)
        except KeyboardInterrupt:
            scheduler.stop()
            print("\nDaemon stopped.")
    finally:
        await pool.close()


async def cmd_health_history(args):
    """Show health check history for a source."""
    pool = await get_pool()
    try:
        rows = await pool.fetch("""
            SELECT checked_at, http_status, response_time_ms,
                   record_count, has_changes, change_type,
                   error_type, error_message
            FROM source_health_checks
            WHERE source_key = $1
            ORDER BY checked_at DESC
            LIMIT $2
        """, args.source_key, args.limit)

        if not rows:
            print(f"\nNo health check history for '{args.source_key}'")
            return

        print(f"\n{'=' * 80}")
        print(f"Health Check History: {args.source_key}")
        print(f"{'=' * 80}")

        for row in rows:
            status = 'ERROR' if row['error_type'] else 'OK'
            changes = f"CHANGES ({row['change_type']})" if row['has_changes'] else "no changes"
            count = f"{row['record_count']:,}" if row['record_count'] else "N/A"
            print(f"{row['checked_at']} | {status:<5} | HTTP {row['http_status'] or 'N/A':>3} | "
                  f"{row['response_time_ms'] or 0:>4}ms | {count:>10} records | {changes}")
            if row['error_message']:
                print(f"    Error: {row['error_message']}")

    finally:
        await pool.close()


# =============================================================================
# FIELD MAPPING REVIEW COMMANDS
# =============================================================================

async def cmd_review_mappings(args):
    """Review and manage suggested field mappings."""
    pool = await get_pool()
    try:
        if args.list:
            # List all pending mapping suggestions
            rows = await pool.fetch("""
                SELECT
                    ds.source_key,
                    ds.source_name,
                    ds.record_count,
                    fms.created_at,
                    fms.suggested_mappings,
                    fms.confidence_scores
                FROM data_sources ds
                JOIN field_mapping_suggestions fms
                    ON ds.source_key = fms.source_key
                WHERE fms.status = 'pending'
                ORDER BY fms.created_at ASC
            """)

            if not rows:
                print("\nNo pending field mapping suggestions.")
                return

            print(f"\n{'=' * 80}")
            print(f"Pending Field Mapping Suggestions ({len(rows)})")
            print(f"{'=' * 80}")

            for row in rows:
                mappings = row['suggested_mappings']
                if isinstance(mappings, str):
                    mappings = json.loads(mappings)
                scores = row['confidence_scores']
                if isinstance(scores, str):
                    scores = json.loads(scores)

                print(f"\n{row['source_key']}")
                print(f"  Source: {row['source_name']}")
                print(f"  Records: {row['record_count']:,}" if row['record_count'] else "  Records: Unknown")
                print(f"  Created: {row['created_at']}")
                print(f"  Mappings ({len(mappings)}):")
                for target, source_field in mappings.items():
                    conf = scores.get(target, 0) if scores else 0
                    bar = "█" * int(conf * 10) + "░" * (10 - int(conf * 10))
                    print(f"    {target:20} ← {source_field:20} [{bar}] {conf:.0%}")

        elif args.source:
            # Show detailed suggestions for a specific source
            row = await pool.fetchrow("""
                SELECT
                    ds.source_key, ds.source_name, ds.record_count,
                    ds.dataset_mapping_key,
                    fms.created_at, fms.source_fields,
                    fms.suggested_mappings, fms.confidence_scores,
                    fms.status
                FROM data_sources ds
                LEFT JOIN field_mapping_suggestions fms
                    ON ds.source_key = fms.source_key AND fms.status = 'pending'
                WHERE ds.source_key = $1
            """, args.source)

            if not row:
                print(f"\nSource not found: {args.source}")
                return

            print(f"\n{'=' * 80}")
            print(f"Field Mapping Suggestions: {row['source_key']}")
            print(f"{'=' * 80}")
            print(f"Source: {row['source_name']}")
            print(f"Records: {row['record_count']:,}" if row['record_count'] else "Records: Unknown")
            print(f"Current mapping key: {row['dataset_mapping_key'] or 'None'}")

            if not row['suggested_mappings']:
                print("\nNo pending suggestions for this source.")
                return

            mappings = row['suggested_mappings']
            if isinstance(mappings, str):
                mappings = json.loads(mappings)
            scores = row['confidence_scores']
            if isinstance(scores, str):
                scores = json.loads(scores)
            fields = row['source_fields']
            if isinstance(fields, str):
                fields = json.loads(fields)

            print(f"\nSource Fields ({len(fields)}):")
            for field in fields:
                null_rate = field.get('null_rate', 0)
                samples = ', '.join(str(s)[:20] for s in field.get('sample_values', [])[:3])
                print(f"  {field['name']:30} ({field['type']:10}) null:{null_rate:.0%}  samples: {samples}")

            print(f"\nSuggested Mappings ({len(mappings)}):")
            for target, source_field in mappings.items():
                conf = scores.get(target, 0) if scores else 0
                bar = "█" * int(conf * 10) + "░" * (10 - int(conf * 10))
                print(f"  {target:20} ← {source_field:25} [{bar}] {conf:.0%}")

            if args.approve:
                await _approve_mappings(pool, args.source, mappings, force=getattr(args, 'force', False))
            elif args.reject:
                await _reject_mappings(pool, args.source, args.reject)

        else:
            print("Use --list to see all pending suggestions, or --source <key> for details")

    finally:
        await pool.close()


async def _approve_mappings(pool, source_key: str, mappings: dict, force: bool = False):
    """Approve mappings and add to dataset-mappings.json."""
    # Load current dataset-mappings.json
    config_path = Path(__file__).parent.parent.parent / 'config' / 'dataset-mappings.json'

    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
    except FileNotFoundError:
        config = {}

    # Create mapping entry
    mapping_key = source_key.replace('_addresses', '').replace('_', '_')

    if mapping_key in config:
        print(f"\nMapping key '{mapping_key}' already exists. Use update-mappings to modify.")
        return

    # Build field_mappings from approved suggestions
    field_mappings = {}
    for target, source_field in mappings.items():
        field_mappings[target] = source_field

    # Show confirmation
    print(f"\n{'=' * 60}")
    print(f"APPROVAL CONFIRMATION: {source_key}")
    print(f"{'=' * 60}")
    print(f"\nMapping key: {mapping_key}")
    print(f"Config file: {config_path}")
    print(f"\nField mappings to be added:")
    for target, source_field in field_mappings.items():
        print(f"  {target:20} <- {source_field}")

    print(f"\nDefault transforms:")
    print(f"  parse_street_name:  disabled")
    print(f"  parse_civic_number: enabled")
    print(f"  normalize_fields:   enabled")
    print(f"  map_municipality:   disabled")
    print(f"  enrich_fsa:         disabled")
    print(f"  validate:           enabled (strict_bounds: ontario)")

    print(f"\n⚠️  NOTE: After approval, you must manually edit the config to set:")
    print(f"  - municipality: The official municipality name")
    print(f"  - postal_prefixes: Array of FSA codes")
    print(f"  - transforms: Adjust based on source data format")

    if not force:
        confirm = input("\nType 'APPROVE' to confirm: ").strip()
        if confirm != 'APPROVE':
            print("\n✗ Approval cancelled.")
            return

    config[mapping_key] = {
        'municipality': 'TODO: Set municipality name',
        'province': 'ON',
        'postal_prefixes': [],
        'field_mappings': field_mappings,
        'transforms': {
            'parse_street_name': {'enabled': False},
            'parse_civic_number': {'enabled': True, 'passthrough_suffix': True},
            'normalize_fields': {'enabled': True},
            'map_municipality': {'enabled': False},
            'enrich_fsa': {'enabled': False},
            'validate': {'enabled': True, 'strict_bounds': 'ontario'}
        }
    }

    # Save updated config
    with open(config_path, 'w') as f:
        json.dump(config, f, indent=2)

    # Update database
    await pool.execute("""
        UPDATE field_mapping_suggestions
        SET status = 'approved', reviewed_at = NOW(), approved_mappings = $2
        WHERE source_key = $1 AND status = 'pending'
    """, source_key, json.dumps(mappings))

    await pool.execute("""
        UPDATE data_sources
        SET dataset_mapping_key = $2, suggested_mappings_status = 'approved'
        WHERE source_key = $1
    """, source_key, mapping_key)

    print(f"\n✓ Mappings approved for {source_key}")
    print(f"  Added to: {config_path}")
    print(f"  Mapping key: {mapping_key}")
    print(f"  NOTE: Edit the config to set municipality name and postal_prefixes")


async def _reject_mappings(pool, source_key: str, reason: str):
    """Reject suggested mappings."""
    await pool.execute("""
        UPDATE field_mapping_suggestions
        SET status = 'rejected', reviewed_at = NOW(), rejection_notes = $2
        WHERE source_key = $1 AND status = 'pending'
    """, source_key, reason)

    await pool.execute("""
        UPDATE data_sources
        SET suggested_mappings_status = 'rejected'
        WHERE source_key = $1
    """, source_key)

    print(f"\n✓ Mappings rejected for {source_key}: {reason}")


# =============================================================================
# MAIN
# =============================================================================

def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Manage geocoder data sources',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    subparsers = parser.add_subparsers(dest='command', help='Commands')

    # --- Source commands ---

    # add-source
    add_parser = subparsers.add_parser('add-source', help='Add a new data source')
    add_parser.add_argument('--key', required=True, help='Unique source key (e.g., toronto)')
    add_parser.add_argument('--name', required=True, help='Display name')
    add_parser.add_argument('--regions', required=True, help='Comma-separated regions covered')
    add_parser.add_argument('--status', default='potential', choices=['potential', 'confirmed', 'rejected', 'blocked'])
    add_parser.add_argument('--region-type', help='Type: city, county, region, province')
    add_parser.add_argument('--postal-prefixes', help='Comma-separated postal prefixes')
    add_parser.add_argument('--portal-url', help='Portal URL for manual download')
    add_parser.add_argument('--scraping-notes', help='Notes for manual scraping')
    add_parser.add_argument('--api-endpoint', help='API endpoint URL')
    add_parser.add_argument('--api-type', help='API type: REST, FeatureServer, etc.')
    add_parser.add_argument('--api-key-required', action='store_true', help='Requires API key')
    add_parser.add_argument('--api-rate-limit', help='Rate limit info')
    add_parser.add_argument('--api-notes', help='API notes')
    add_parser.add_argument('--license-type', help='License type')
    add_parser.add_argument('--license-url', help='License URL')
    add_parser.add_argument('--commercial-use', type=lambda x: x.lower() == 'true', help='Commercial use allowed: true/false')
    add_parser.add_argument('--license-notes', help='License notes')
    add_parser.add_argument('--notes', help='General notes')
    add_parser.add_argument('--data-types', help='Comma-separated data type keys to link')
    add_parser.add_argument('--no-regenerate', action='store_true', help='Skip automatic doc regeneration after adding source')

    # update-source
    update_parser = subparsers.add_parser('update-source', help='Update a data source')
    update_parser.add_argument('key', help='Source key to update')
    update_parser.add_argument('--name', help='Display name')
    update_parser.add_argument('--regions', help='Comma-separated regions covered')
    update_parser.add_argument('--status', choices=['potential', 'confirmed', 'rejected', 'blocked'])
    update_parser.add_argument('--region-type', help='Type: city, county, region, province')
    update_parser.add_argument('--postal-prefixes', help='Comma-separated postal prefixes')
    update_parser.add_argument('--portal-url', help='Portal URL')
    update_parser.add_argument('--scraping-notes', help='Scraping notes')
    update_parser.add_argument('--api-endpoint', help='API endpoint')
    update_parser.add_argument('--api-type', help='API type')
    update_parser.add_argument('--api-key-required', type=lambda x: x.lower() == 'true')
    update_parser.add_argument('--api-rate-limit', help='Rate limit')
    update_parser.add_argument('--api-notes', help='API notes')
    update_parser.add_argument('--license-type', help='License type')
    update_parser.add_argument('--license-url', help='License URL')
    update_parser.add_argument('--commercial-use', type=lambda x: x.lower() == 'true')
    update_parser.add_argument('--license-notes', help='License notes')
    update_parser.add_argument('--notes', help='General notes')
    update_parser.add_argument('--record-count', type=int, help='Record count')

    # list-sources
    list_parser = subparsers.add_parser('list-sources', help='List data sources')
    list_parser.add_argument('--status', help='Filter by status')
    list_parser.add_argument('--region', help='Filter by region')
    list_parser.add_argument('--data-type', help='Filter by data type')

    # show-source
    show_parser = subparsers.add_parser('show-source', help='Show detailed source info')
    show_parser.add_argument('key', help='Source key')

    # delete-source
    delete_parser = subparsers.add_parser('delete-source', help='Delete a data source')
    delete_parser.add_argument('key', help='Source key to delete')

    # --- Data type commands ---

    # list-types
    subparsers.add_parser('list-types', help='List all data types')

    # add-type
    add_type_parser = subparsers.add_parser('add-type', help='Add a new data type')
    add_type_parser.add_argument('--key', required=True, help='Type key (e.g., addresses)')
    add_type_parser.add_argument('--display-name', required=True, help='Display name')
    add_type_parser.add_argument('--description', help='Description')

    # link-types
    link_parser = subparsers.add_parser('link-types', help='Link data types to a source')
    link_parser.add_argument('source_key', help='Source key')
    link_parser.add_argument('types', help='Comma-separated type keys')

    # unlink-types
    unlink_parser = subparsers.add_parser('unlink-types', help='Remove data types from a source')
    unlink_parser.add_argument('source_key', help='Source key')
    unlink_parser.add_argument('types', help='Comma-separated type keys')

    # --- Query commands ---

    # sources-by-type
    by_type_parser = subparsers.add_parser('sources-by-type', help='Find sources with a data type')
    by_type_parser.add_argument('type_key', help='Data type key')

    # types-for-region
    for_region_parser = subparsers.add_parser('types-for-region', help='Find data types for a region')
    for_region_parser.add_argument('region', help='Region name')

    # common-types
    common_parser = subparsers.add_parser('common-types', help='Find common data types across sources')
    common_parser.add_argument('sources', help='Comma-separated source keys')

    # export-json
    subparsers.add_parser('export-json', help='Export all sources as JSON')

    # --- Health check commands ---

    # check-health
    check_parser = subparsers.add_parser('check-health', help='Check source health and detect changes')
    check_parser.add_argument('--source', '-s', help='Specific source_key to check')
    check_parser.add_argument('--all', '-a', action='store_true', help='Check all confirmed sources')
    check_parser.add_argument('--due', action='store_true', help='Check only sources due for check')

    # show-stale
    stale_parser = subparsers.add_parser('show-stale', help='Show sources needing attention (stale/error)')
    stale_parser.add_argument('--verbose', '-v', action='store_true', help='Show detailed info')

    # health-daemon
    daemon_parser = subparsers.add_parser('health-daemon', help='Run health check daemon')
    daemon_parser.add_argument('--interval', type=int, default=3600,
                              help='Check interval in seconds (default: 3600)')

    # health-history
    history_parser = subparsers.add_parser('health-history', help='Show health check history')
    history_parser.add_argument('source_key', help='Source key to show history for')
    history_parser.add_argument('--limit', type=int, default=10, help='Number of records (default: 10)')

    # --- Field mapping commands ---

    # review-mappings
    review_parser = subparsers.add_parser('review-mappings', help='Review suggested field mappings')
    review_parser.add_argument('--list', '-l', action='store_true', help='List all pending suggestions')
    review_parser.add_argument('--source', '-s', help='Show suggestions for specific source')
    review_parser.add_argument('--approve', action='store_true', help='Approve and save mappings')
    review_parser.add_argument('--reject', metavar='REASON', help='Reject with reason')
    review_parser.add_argument('--force', '-f', action='store_true', help='Skip confirmation prompt for approval')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Map commands to functions
    commands = {
        'add-source': cmd_add_source,
        'update-source': cmd_update_source,
        'list-sources': cmd_list_sources,
        'show-source': cmd_show_source,
        'delete-source': cmd_delete_source,
        'list-types': cmd_list_types,
        'add-type': cmd_add_type,
        'link-types': cmd_link_types,
        'unlink-types': cmd_unlink_types,
        'sources-by-type': cmd_sources_by_type,
        'types-for-region': cmd_types_for_region,
        'common-types': cmd_common_types,
        'export-json': cmd_export_json,
        'check-health': cmd_check_health,
        'show-stale': cmd_show_stale,
        'health-daemon': cmd_health_daemon,
        'health-history': cmd_health_history,
        'review-mappings': cmd_review_mappings,
    }

    asyncio.run(commands[args.command](args))


if __name__ == '__main__':
    main()
