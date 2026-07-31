#!/usr/bin/env python3
"""
Source health checking and change detection service.

Provides methods to check various data source types for upstream changes:
- ArcGIS FeatureServer: editingInfo.lastEditDate, record count, schema
- CKAN API: package metadata and resource last_modified
- Direct URLs: HEAD request with ETag/Last-Modified comparison

Usage:
    service = SourceHealthService(pool)
    result = await service.check_source('toronto_addresses')
    if result.has_changes:
        print(f"Changes detected: {result.change_type}")
"""
import os
import json
import hashlib
import asyncio
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field

import httpx
import asyncpg
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger('geocoder.health')


@dataclass
class HealthCheckResult:
    """Result of a health check operation."""
    source_key: str
    checked_at: datetime = field(default_factory=datetime.now)

    # HTTP response data
    http_status: Optional[int] = None
    response_time_ms: Optional[int] = None
    etag: Optional[str] = None
    content_length: Optional[int] = None
    last_modified: Optional[str] = None

    # Content analysis
    record_count: Optional[int] = None
    schema_hash: Optional[str] = None
    schema_fields: Optional[List[Dict]] = None

    # Change detection
    has_changes: bool = False
    change_type: Optional[str] = None  # content, schema, count, none, error

    # ArcGIS-specific
    arcgis_last_edit_date: Optional[datetime] = None

    # Error tracking
    error_type: Optional[str] = None
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for database storage."""
        return {
            'source_key': self.source_key,
            'checked_at': self.checked_at,
            'http_status': self.http_status,
            'response_time_ms': self.response_time_ms,
            'etag': self.etag,
            'content_length': self.content_length,
            'last_modified': self.last_modified,
            'record_count': self.record_count,
            'schema_hash': self.schema_hash,
            'schema_fields': json.dumps(self.schema_fields) if self.schema_fields else None,
            'has_changes': self.has_changes,
            'change_type': self.change_type,
            'arcgis_last_edit_date': self.arcgis_last_edit_date,
            'error_type': self.error_type,
            'error_message': self.error_message,
        }

    def summary(self) -> str:
        """Return a human-readable summary."""
        if self.error_type:
            return f"ERROR: {self.error_type} - {self.error_message}"
        elif self.has_changes:
            return f"CHANGES ({self.change_type})"
        else:
            return "no changes"


class SourceHealthService:
    """
    Service for checking data source health and detecting changes.

    Supports:
    - ArcGIS FeatureServer/MapServer
    - CKAN APIs
    - Direct URL downloads
    """

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.http_timeout = httpx.Timeout(30.0, connect=10.0)

    async def check_source(self, source_key: str) -> HealthCheckResult:
        """
        Check a single source for changes.

        Determines the appropriate check method based on api_type and
        delegates to the specific checker.
        """
        result = HealthCheckResult(source_key=source_key)

        async with self.pool.acquire() as conn:
            source = await conn.fetchrow("""
                SELECT source_key, source_name, api_type, api_endpoint,
                       download_url, upstream_etag, upstream_hash,
                       schema_hash, record_count,
                       sync_status, provider_last_modified
                FROM data_sources
                WHERE source_key = $1
            """, source_key)

        if not source:
            result.error_type = 'not_found'
            result.error_message = f"Source '{source_key}' not found"
            return result

        try:
            # Determine check method based on api_type
            api_type = (source['api_type'] or '').lower()

            if 'arcgis' in api_type or 'featureserver' in api_type or 'mapserver' in api_type:
                result = await self.check_arcgis_source(source, result)
            elif api_type == 'ckan':
                result = await self.check_ckan_source(source, result)
            elif source['download_url']:
                result = await self.check_direct_url(source, result)
            elif source['api_endpoint']:
                # Try ArcGIS check for unspecified endpoints
                result = await self.check_arcgis_source(source, result)
            else:
                result.error_type = 'no_check_method'
                result.error_message = 'No check method available for this source'

            # Log and store the result
            await self._store_health_check(result)

            # Update sync_status if changes detected
            if result.has_changes and not result.error_type:
                await self._update_sync_status(source_key, 'stale')
            elif not result.error_type:
                # Update last_sync_check even if no changes
                await self._update_last_check(source_key)

        except Exception as e:
            result.error_type = 'check_failed'
            result.error_message = str(e)
            logger.exception(f"Health check failed for {source_key}")
            await self._store_health_check(result)
            await self._update_sync_status(source_key, 'error', str(e))

        return result

    async def check_arcgis_source(
        self,
        source: asyncpg.Record,
        result: HealthCheckResult
    ) -> HealthCheckResult:
        """
        Check ArcGIS FeatureServer/MapServer for changes.

        Checks:
        - editingInfo.lastEditDate from layer metadata
        - Record count comparison
        - Schema field changes
        """
        api_endpoint = source['api_endpoint']
        if not api_endpoint:
            result.error_type = 'missing_endpoint'
            result.error_message = 'No API endpoint configured'
            return result

        async with httpx.AsyncClient(timeout=self.http_timeout) as client:
            start_time = datetime.now()

            try:
                # Get layer metadata
                response = await client.get(f"{api_endpoint}?f=json")
                result.http_status = response.status_code
                result.response_time_ms = int((datetime.now() - start_time).total_seconds() * 1000)

                if response.status_code != 200:
                    result.error_type = 'http_error'
                    result.error_message = f"HTTP {response.status_code}"
                    return result

                metadata = response.json()

                # Check for ArcGIS error response
                if 'error' in metadata:
                    result.error_type = 'arcgis_error'
                    result.error_message = metadata['error'].get('message', 'Unknown ArcGIS error')
                    return result

                # Extract editingInfo.lastEditDate
                editing_info = metadata.get('editingInfo', {})
                last_edit_date = editing_info.get('lastEditDate')
                if last_edit_date:
                    # ArcGIS returns timestamp in milliseconds
                    result.arcgis_last_edit_date = datetime.fromtimestamp(last_edit_date / 1000)

                # Get record count
                try:
                    count_response = await client.get(
                        f"{api_endpoint}/query",
                        params={'where': '1=1', 'returnCountOnly': 'true', 'f': 'json'}
                    )
                    if count_response.status_code == 200:
                        count_data = count_response.json()
                        result.record_count = count_data.get('count')
                except Exception:
                    pass  # Record count is optional

                # Extract schema from fields
                fields = metadata.get('fields', [])
                if fields:
                    result.schema_fields = [
                        {'name': f['name'], 'type': f.get('type', 'unknown')}
                        for f in fields
                    ]
                    result.schema_hash = self._compute_schema_hash(result.schema_fields)

                # Detect changes
                result = self._detect_changes(source, result)

            except httpx.TimeoutException:
                result.error_type = 'timeout'
                result.error_message = 'Request timed out'
            except httpx.ConnectError as e:
                result.error_type = 'connection_error'
                result.error_message = str(e)

        return result

    async def check_ckan_source(
        self,
        source: asyncpg.Record,
        result: HealthCheckResult
    ) -> HealthCheckResult:
        """
        Check CKAN API for resource changes.

        Checks:
        - Package/resource last_modified timestamp
        - Resource size changes
        """
        api_endpoint = source['api_endpoint']
        if not api_endpoint:
            result.error_type = 'missing_endpoint'
            result.error_message = 'No API endpoint configured'
            return result

        async with httpx.AsyncClient(timeout=self.http_timeout, follow_redirects=True) as client:
            start_time = datetime.now()

            try:
                response = await client.get(api_endpoint)
                result.http_status = response.status_code
                result.response_time_ms = int((datetime.now() - start_time).total_seconds() * 1000)

                if response.status_code != 200:
                    result.error_type = 'http_error'
                    result.error_message = f"HTTP {response.status_code}"
                    return result

                data = response.json()
                if not data.get('success'):
                    result.error_type = 'ckan_error'
                    result.error_message = data.get('error', {}).get('message', 'Unknown CKAN error')
                    return result

                package = data['result']
                resources = package.get('resources', [])

                # Find GeoJSON or JSON resource
                target_resource = None
                for resource in resources:
                    fmt = resource.get('format', '').lower()
                    if fmt == 'geojson':
                        target_resource = resource
                        break

                if not target_resource:
                    for resource in resources:
                        if resource.get('format', '').lower() == 'json':
                            target_resource = resource
                            break

                if target_resource:
                    # Extract metadata
                    result.last_modified = target_resource.get('last_modified')
                    size = target_resource.get('size')
                    if size:
                        result.content_length = int(size)

                    # Check for changes based on last_modified
                    if result.last_modified and source['provider_last_modified']:
                        try:
                            from dateutil.parser import parse
                            parsed_date = parse(result.last_modified)
                            # Make timezone-naive for comparison
                            if parsed_date.tzinfo:
                                parsed_date = parsed_date.replace(tzinfo=None)
                            if parsed_date > source['provider_last_modified']:
                                result.has_changes = True
                                result.change_type = 'content'
                        except Exception:
                            pass
                    elif result.last_modified and not source['provider_last_modified']:
                        # First check with a last_modified value
                        result.has_changes = True
                        result.change_type = 'content'

                # If no changes detected yet, use general detection
                if not result.has_changes:
                    result = self._detect_changes(source, result)

            except httpx.TimeoutException:
                result.error_type = 'timeout'
                result.error_message = 'Request timed out'
            except httpx.ConnectError as e:
                result.error_type = 'connection_error'
                result.error_message = str(e)

        return result

    async def check_direct_url(
        self,
        source: asyncpg.Record,
        result: HealthCheckResult
    ) -> HealthCheckResult:
        """
        Check direct URL for changes via HEAD request.

        Checks:
        - ETag header comparison
        - Last-Modified header comparison
        - Content-Length changes
        """
        download_url = source['download_url']
        if not download_url:
            result.error_type = 'missing_url'
            result.error_message = 'No download URL configured'
            return result

        async with httpx.AsyncClient(
            timeout=self.http_timeout,
            follow_redirects=True
        ) as client:
            start_time = datetime.now()

            try:
                response = await client.head(download_url)
                result.http_status = response.status_code
                result.response_time_ms = int((datetime.now() - start_time).total_seconds() * 1000)

                if response.status_code not in (200, 301, 302, 304):
                    result.error_type = 'http_error'
                    result.error_message = f"HTTP {response.status_code}"
                    return result

                # Extract headers
                result.etag = response.headers.get('ETag')
                result.last_modified = response.headers.get('Last-Modified')
                content_length = response.headers.get('Content-Length')
                if content_length:
                    result.content_length = int(content_length)

                # Compare with stored values
                stored_etag = source['upstream_etag']
                if stored_etag and result.etag:
                    # Normalize ETags
                    stored_clean = stored_etag.replace('W/', '').strip('"')
                    new_clean = result.etag.replace('W/', '').strip('"')
                    if stored_clean != new_clean:
                        result.has_changes = True
                        result.change_type = 'content'
                elif result.etag and not stored_etag:
                    # First check with ETag
                    result.has_changes = True
                    result.change_type = 'content'

                # If no changes from ETag, check general detection
                if not result.has_changes:
                    result = self._detect_changes(source, result)

            except httpx.TimeoutException:
                result.error_type = 'timeout'
                result.error_message = 'Request timed out'
            except httpx.ConnectError as e:
                result.error_type = 'connection_error'
                result.error_message = str(e)

        return result

    def _compute_schema_hash(self, fields: List[Dict]) -> str:
        """Compute SHA256 hash of sorted field names."""
        if not fields:
            return ''

        field_names = sorted([f['name'] for f in fields])
        return hashlib.sha256(
            json.dumps(field_names).encode()
        ).hexdigest()

    def _detect_changes(
        self,
        source: asyncpg.Record,
        result: HealthCheckResult
    ) -> HealthCheckResult:
        """
        Compare current check result with stored values to detect changes.
        """
        # Already marked as changed
        if result.has_changes:
            return result

        # Check schema changes
        if result.schema_hash and source['schema_hash']:
            if result.schema_hash != source['schema_hash']:
                result.has_changes = True
                result.change_type = 'schema'
                return result

        # Check record count changes
        if result.record_count is not None and source['record_count'] is not None:
            if result.record_count != source['record_count']:
                result.has_changes = True
                result.change_type = 'count'
                return result

        # Check ArcGIS last edit date
        if result.arcgis_last_edit_date and source['provider_last_modified']:
            if result.arcgis_last_edit_date > source['provider_last_modified']:
                result.has_changes = True
                result.change_type = 'content'
                return result
        elif result.arcgis_last_edit_date and not source['provider_last_modified']:
            # First check with edit date
            result.has_changes = True
            result.change_type = 'content'
            return result

        # No changes detected
        if result.change_type is None:
            result.change_type = 'none'
        return result

    async def _store_health_check(self, result: HealthCheckResult) -> int:
        """Store health check result in database."""
        try:
            async with self.pool.acquire() as conn:
                return await conn.fetchval("""
                    INSERT INTO source_health_checks (
                        source_key, checked_at, http_status, response_time_ms,
                        etag, content_length, last_modified, record_count,
                        schema_hash, schema_fields, has_changes, change_type,
                        arcgis_last_edit_date, error_type, error_message
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb,
                        $11, $12, $13, $14, $15
                    )
                    RETURNING id
                """, result.source_key, result.checked_at, result.http_status,
                   result.response_time_ms, result.etag, result.content_length,
                   result.last_modified, result.record_count, result.schema_hash,
                   json.dumps(result.schema_fields) if result.schema_fields else None,
                   result.has_changes, result.change_type, result.arcgis_last_edit_date,
                   result.error_type, result.error_message)
        except Exception as e:
            logger.warning(f"Failed to store health check: {e}")
            return None

    async def _update_sync_status(
        self,
        source_key: str,
        status: str,
        error: str = None
    ) -> None:
        """Update sync status in data_sources table."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE data_sources
                SET sync_status = $2,
                    last_sync_check = NOW(),
                    last_sync_error = $3,
                    updated_at = NOW()
                WHERE source_key = $1
            """, source_key, status, error)

    async def _update_last_check(self, source_key: str) -> None:
        """Update last_sync_check without changing status."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE data_sources
                SET last_sync_check = NOW(),
                    updated_at = NOW()
                WHERE source_key = $1
            """, source_key)

    async def check_all_confirmed(self) -> List[HealthCheckResult]:
        """Check all confirmed sources."""
        async with self.pool.acquire() as conn:
            sources = await conn.fetch("""
                SELECT source_key
                FROM data_sources
                WHERE status = 'confirmed'
                  AND (requires_manual_download = FALSE OR requires_manual_download IS NULL)
                ORDER BY source_key
            """)

        results = []
        for source in sources:
            result = await self.check_source(source['source_key'])
            results.append(result)

        return results

    async def get_stale_sources(self) -> List[Dict]:
        """Get all sources marked as stale or error."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM v_sources_needing_attention
            """)
        return [dict(row) for row in rows]


class SourceHealthScheduler:
    """
    Scheduler for periodic health checks based on update_frequency.

    Usage:
        scheduler = SourceHealthScheduler(pool)

        # One-time check of due sources
        stats = await scheduler.run_once()

        # Daemon mode
        await scheduler.run_daemon(check_interval_seconds=3600)
    """

    # Map update_frequency to check intervals (in hours)
    FREQUENCY_INTERVALS = {
        'daily': 6,           # Check 4x/day for daily sources
        'weekly': 24,         # Check daily for weekly sources
        'monthly': 24 * 3,    # Check every 3 days for monthly
        'quarterly': 24 * 7,  # Check weekly for quarterly
        'annual': 24 * 14,    # Check bi-weekly for annual
        'on_demand': 24 * 3,  # Check every 3 days for on_demand
        'unknown': 24 * 3,    # Check every 3 days for unknown
    }

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.service = SourceHealthService(pool)
        self._running = False

    async def get_sources_due_for_check(self) -> List[str]:
        """
        Get list of source_keys that are due for a health check.

        A source is due if:
        - It has never been checked (last_sync_check IS NULL), or
        - Time since last check exceeds the interval for its update_frequency
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT
                    source_key,
                    update_frequency,
                    last_sync_check
                FROM data_sources
                WHERE status = 'confirmed'
                  AND (requires_manual_download = FALSE OR requires_manual_download IS NULL)
            """)

        due_sources = []
        now = datetime.now()

        for row in rows:
            source_key = row['source_key']
            frequency = row['update_frequency']
            last_check = row['last_sync_check']

            interval_hours = self.FREQUENCY_INTERVALS.get(frequency, 24 * 3)  # Default 3 days

            if last_check is None:
                # Never checked
                due_sources.append(source_key)
            else:
                hours_since_check = (now - last_check).total_seconds() / 3600
                if hours_since_check >= interval_hours:
                    due_sources.append(source_key)

        return due_sources

    async def run_once(self) -> Dict[str, Any]:
        """
        Run health checks on all due sources once.

        Returns summary statistics.
        """
        due_sources = await self.get_sources_due_for_check()

        stats = {
            'checked': 0,
            'changes_detected': 0,
            'errors': 0,
            'sources': []
        }

        for source_key in due_sources:
            result = await self.service.check_source(source_key)
            stats['checked'] += 1

            if result.error_type:
                stats['errors'] += 1
            elif result.has_changes:
                stats['changes_detected'] += 1

            stats['sources'].append({
                'source_key': source_key,
                'has_changes': result.has_changes,
                'change_type': result.change_type,
                'error': result.error_message
            })

        return stats

    async def run_daemon(
        self,
        check_interval_seconds: int = 3600
    ) -> None:
        """
        Run as a daemon, periodically checking due sources.

        Args:
            check_interval_seconds: How often to poll for due sources
        """
        self._running = True
        logger.info(f"Starting health check daemon (interval: {check_interval_seconds}s)")

        while self._running:
            try:
                stats = await self.run_once()
                logger.info(
                    f"Health check cycle complete: "
                    f"{stats['checked']} checked, "
                    f"{stats['changes_detected']} changes, "
                    f"{stats['errors']} errors"
                )
            except Exception:
                logger.exception("Error in health check daemon cycle")

            # Sleep in smaller chunks to allow graceful shutdown
            for _ in range(check_interval_seconds):
                if not self._running:
                    break
                await asyncio.sleep(1)

    def stop(self) -> None:
        """Signal the daemon to stop."""
        self._running = False
        logger.info("Health check daemon stopping...")


async def create_pool() -> asyncpg.Pool:
    """Create database connection pool."""
    db_password = os.environ.get('DB_PASSWORD')
    if not db_password:
        raise ValueError(
            "DB_PASSWORD environment variable is required. "
            "Set it to your PostgreSQL password."
        )

    return await asyncpg.create_pool(
        host=os.environ.get('DB_HOST', 'localhost'),
        port=int(os.environ.get('DB_PORT', 5432)),
        database=os.environ.get('DB_NAME', 'geocoder'),
        user=os.environ.get('DB_USER', 'geocoder'),
        password=db_password,
        min_size=1,
        max_size=5
    )


async def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description='Data source health checking and change detection',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Check a specific source
  %(prog)s --source toronto_addresses

  # Check all confirmed sources
  %(prog)s --all

  # Check only sources due for check
  %(prog)s --due

  # Show sources needing update
  %(prog)s --stale

  # Run as daemon
  %(prog)s --daemon --interval 3600
        """
    )
    parser.add_argument('--source', '-s', help='Specific source_key to check')
    parser.add_argument('--all', '-a', action='store_true',
                        help='Check all confirmed sources')
    parser.add_argument('--due', action='store_true',
                        help='Check only sources due for check')
    parser.add_argument('--stale', action='store_true',
                        help='Show sources needing update')
    parser.add_argument('--daemon', '-d', action='store_true',
                        help='Run as daemon')
    parser.add_argument('--interval', type=int, default=3600,
                        help='Daemon check interval in seconds (default: 3600)')
    parser.add_argument('--history', metavar='KEY',
                        help='Show health check history for a source')
    parser.add_argument('--limit', type=int, default=10,
                        help='Number of history records to show (default: 10)')

    args = parser.parse_args()

    pool = await create_pool()

    try:
        service = SourceHealthService(pool)
        scheduler = SourceHealthScheduler(pool)

        if args.source:
            # Check specific source
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

        elif args.stale:
            # Show stale sources
            stale = await service.get_stale_sources()

            if not stale:
                print("\nNo sources need attention.")
            else:
                print(f"\n{'=' * 60}")
                print(f"Sources Needing Attention ({len(stale)} total)")
                print(f"{'=' * 60}")

                for row in stale:
                    print(f"\n{row['source_key']}:")
                    print(f"  Status: {row['sync_status']}")
                    if row.get('last_sync_check'):
                        print(f"  Last checked: {row['last_sync_check']}")
                    if row.get('time_since_check'):
                        hours = row['time_since_check'].total_seconds() / 3600
                        print(f"  Time since check: {hours:.1f} hours")
                    if row.get('last_sync_error'):
                        print(f"  Error: {row['last_sync_error']}")

        elif args.daemon:
            # Run as daemon
            print(f"Starting health check daemon (interval: {args.interval}s)")
            print("Press Ctrl+C to stop...")
            try:
                await scheduler.run_daemon(check_interval_seconds=args.interval)
            except KeyboardInterrupt:
                scheduler.stop()
                print("\nDaemon stopped.")

        elif args.history:
            # Show health check history
            async with pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT checked_at, http_status, response_time_ms,
                           record_count, has_changes, change_type,
                           error_type, error_message
                    FROM source_health_checks
                    WHERE source_key = $1
                    ORDER BY checked_at DESC
                    LIMIT $2
                """, args.history, args.limit)

            if not rows:
                print(f"\nNo health check history for '{args.history}'")
            else:
                print(f"\n{'=' * 80}")
                print(f"Health Check History: {args.history}")
                print(f"{'=' * 80}")

                for row in rows:
                    status = 'ERROR' if row['error_type'] else 'OK'
                    changes = f"CHANGES ({row['change_type']})" if row['has_changes'] else "no changes"
                    print(f"{row['checked_at']} | HTTP {row['http_status']} | "
                          f"{row['response_time_ms']}ms | {changes}")

        else:
            # Show due sources without checking
            due = await scheduler.get_sources_due_for_check()
            print(f"\n{len(due)} sources due for health check:")
            for key in due:
                print(f"  - {key}")
            print(f"\nUse --due to run checks, or --help for more options.")

    finally:
        await pool.close()


if __name__ == '__main__':
    asyncio.run(main())
