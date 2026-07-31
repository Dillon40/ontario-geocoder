#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "asyncpg",
#   "python-dotenv",
#   "httpx",
# ]
# ///

"""
Download data sources from configured URLs and APIs.

Routes to specialized downloaders:
- arcgis_downloader.py: Standard ArcGIS FeatureServer sources
- hamilton_downloader.py: Hamilton addresses (with rate limiting)
- toronto_downloader.py: Toronto CKAN API
- nar_downloader.py: NAR Ontario (CSV to GeoJSON)
- fsa_downloader.py: FSA boundaries (shapefile extraction only)
"""
import sys
import json
import argparse
import asyncio
import hashlib
import zipfile
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
import httpx

from download_utils import calculate_file_hash

# Load environment variables
load_dotenv()

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from data_sourcing.db_utils import get_db_connection

# Constants
DATA_DIR = Path(__file__).parent.parent / 'data_sources'


async def get_data_sources(conn, source_key: str = None, status: str = 'confirmed'):
    """Fetch data sources from database."""
    if source_key:
        return await conn.fetch("""
            SELECT * FROM data_sources WHERE source_key = $1
        """, source_key)
    else:
        return await conn.fetch("""
            SELECT * FROM data_sources WHERE status = $1
            ORDER BY source_key
        """, status)


async def update_source_refresh(conn, source_key: str, record_count: int = None):
    """Update last_data_refresh timestamp for a source."""
    if record_count:
        await conn.execute("""
            UPDATE data_sources
            SET last_data_refresh = NOW(), record_count = $2, updated_at = NOW()
            WHERE source_key = $1
        """, source_key, record_count)
    else:
        await conn.execute("""
            UPDATE data_sources
            SET last_data_refresh = NOW(), updated_at = NOW()
            WHERE source_key = $1
        """, source_key)


async def update_sync_status(conn, source_key: str, status: str,
                             etag: str = None, file_hash: str = None,
                             error: str = None):
    """Update sync tracking columns for a source."""
    await conn.execute("""
        UPDATE data_sources
        SET sync_status = $2,
            upstream_etag = COALESCE($3, upstream_etag),
            upstream_hash = COALESCE($4, upstream_hash),
            last_sync_check = NOW(),
            last_sync_error = $5,
            updated_at = NOW()
        WHERE source_key = $1
    """, source_key, status, etag, file_hash, error)


def extract_geojson_schema(file_path: Path) -> tuple:
    """
    Extract schema (field names and types) from a GeoJSON file.
    Returns (schema_fields, schema_hash) tuple.
    """
    try:
        with open(file_path, 'r') as f:
            data = json.load(f)

        if 'features' not in data or not data['features']:
            return None, None

        # Get properties from first feature
        first_feature = data['features'][0]
        properties = first_feature.get('properties', {})

        # Build schema
        schema_fields = []
        for key, value in properties.items():
            field_type = type(value).__name__ if value is not None else 'unknown'
            schema_fields.append({'name': key, 'type': field_type})

        # Sort for consistent hashing
        schema_fields.sort(key=lambda x: x['name'])

        # Compute hash
        field_names = [f['name'] for f in schema_fields]
        schema_hash = hashlib.sha256(json.dumps(field_names).encode()).hexdigest()

        return schema_fields, schema_hash

    except Exception as e:
        print(f"  Warning: Could not extract schema: {e}")
        return None, None


async def update_schema_info(conn, source_key: str, schema_fields: list, schema_hash: str):
    """Update schema tracking columns for a source."""
    await conn.execute("""
        UPDATE data_sources
        SET schema_fields = $2::jsonb,
            schema_hash = $3,
            updated_at = NOW()
        WHERE source_key = $1
    """, source_key, json.dumps(schema_fields), schema_hash)


async def update_provider_modified(conn, source_key: str, provider_last_modified: datetime):
    """Update provider_last_modified timestamp for a source."""
    await conn.execute("""
        UPDATE data_sources
        SET provider_last_modified = $2,
            updated_at = NOW()
        WHERE source_key = $1
    """, source_key, provider_last_modified)


async def check_upstream_version(url: str, stored_etag: str = None) -> dict:
    """
    Check upstream for version changes using HEAD request.
    Returns dict with: changed, etag, last_modified, content_length
    """
    result = {
        'changed': True,  # Assume changed by default
        'etag': None,
        'last_modified': None,
        'content_length': None,
        'error': None
    }

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            response = await client.head(url)
            result['etag'] = response.headers.get('ETag')
            result['last_modified'] = response.headers.get('Last-Modified')
            result['content_length'] = response.headers.get('Content-Length')

            # Compare ETags if we have a stored one
            if stored_etag and result['etag']:
                # Normalize ETags (remove weak prefix if present)
                stored_clean = stored_etag.replace('W/', '').strip('"')
                new_clean = result['etag'].replace('W/', '').strip('"')
                result['changed'] = stored_clean != new_clean

    except Exception as e:
        result['error'] = str(e)

    return result


async def download_with_version_check(
    conn, source: dict, url: str, dest_path: Path,
    extract_zip: bool = True, force: bool = False
) -> tuple[bool, str]:
    """
    Download with ETag/hash version tracking.
    Returns: (downloaded: bool, file_hash: str)
    """
    source_key = source['source_key']
    stored_etag = source.get('upstream_etag')
    stored_hash = source.get('upstream_hash')

    # Use longer timeout for large downloads (e.g., NAR at 1.9GB)
    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(1800.0, connect=60.0)) as client:
        headers = {}

        # Use If-None-Match for conditional GET if we have stored ETag
        if stored_etag and not force:
            headers['If-None-Match'] = stored_etag

        print(f"  Downloading from: {url}")

        # For large files, use streaming download with progress
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        async with client.stream('GET', url, headers=headers) as response:
            # Handle 304 Not Modified
            if response.status_code == 304:
                print(f"  Not modified (ETag match)")
                await update_sync_status(conn, source_key, 'synced')
                return False, stored_hash

            response.raise_for_status()

            # Capture ETag before context closes
            new_etag = response.headers.get('ETag')

            # Determine if ZIP from Content-Type or Content-Disposition header
            content_type = response.headers.get('content-type', '').lower()
            content_disp = response.headers.get('content-disposition', '').lower()
            download_format = source.get('download_format', '').lower()

            is_zip = (
                'application/zip' in content_type or
                'application/x-zip' in content_type or
                download_format == 'zip' or
                url.endswith('.zip') or
                '.zip' in content_disp
            )

            # Determine temp file path
            temp_file = dest_path.parent / 'download.zip' if is_zip else dest_path

            # Get content length for progress
            total_size = int(response.headers.get('content-length', 0))
            downloaded_size = 0
            last_progress = 0

            with open(temp_file, 'wb') as f:
                async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):  # 1MB chunks
                    f.write(chunk)
                    downloaded_size += len(chunk)

                    # Progress every 50MB
                    if total_size > 0:
                        progress_mb = downloaded_size // (50 * 1024 * 1024)
                        if progress_mb > last_progress:
                            pct = (downloaded_size / total_size) * 100
                            print(f"  Downloaded {downloaded_size / 1024 / 1024:.0f} / {total_size / 1024 / 1024:.0f} MB ({pct:.0f}%)...")
                            last_progress = progress_mb

        # Compute hash from downloaded file
        file_hash = calculate_file_hash(temp_file)

        if is_zip:
            print(f"  Downloaded ZIP: {downloaded_size / 1024 / 1024:.1f} MB")

            if extract_zip:
                print(f"  Extracting to: {dest_path.parent}")
                with zipfile.ZipFile(temp_file, 'r') as zf:
                    zf.extractall(dest_path.parent)
                temp_file.unlink()
        else:
            # For non-ZIP files, temp_file is already dest_path
            print(f"  Downloaded: {downloaded_size / 1024 / 1024:.1f} MB")

        # Check if content actually changed (hash comparison)
        if not force and stored_hash and file_hash == stored_hash:
            print(f"  Content unchanged (hash match)")
            await update_sync_status(conn, source_key, 'synced', etag=new_etag)
            return False, file_hash

        # Update tracking columns
        await update_sync_status(conn, source_key, 'synced', etag=new_etag, file_hash=file_hash)

        return True, file_hash


async def download_direct_url(url: str, dest_path: Path, extract_zip: bool = True) -> Path:
    """Download file from direct URL."""
    print(f"  Downloading from: {url}")
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(follow_redirects=True, timeout=300.0) as client:
        response = await client.get(url)
        response.raise_for_status()

        # Determine filename
        if url.endswith('.zip'):
            temp_file = dest_path.parent / 'download.zip'
            temp_file.write_bytes(response.content)
            print(f"  Downloaded ZIP: {len(response.content) / 1024 / 1024:.1f} MB")

            if extract_zip:
                print(f"  Extracting to: {dest_path.parent}")
                with zipfile.ZipFile(temp_file, 'r') as zf:
                    zf.extractall(dest_path.parent)
                temp_file.unlink()
                return dest_path.parent
            return temp_file
        else:
            dest_path.write_bytes(response.content)
            print(f"  Downloaded: {len(response.content) / 1024 / 1024:.1f} MB")
            return dest_path


async def handle_manual_download(source: dict) -> bool:
    """
    Handle sources that require manual download.
    Prompts user and verifies files exist.
    Returns: True if files are ready, False otherwise
    """
    source_key = source['source_key']
    instructions = source.get('manual_download_instructions', 'No instructions provided.')
    local_path = source.get('local_file_path')

    print(f"\n{'='*60}")
    print(f"MANUAL DOWNLOAD REQUIRED: {source['source_name']}")
    print('='*60)
    print(f"\nInstructions:")
    print(instructions)
    print(f"\nExpected location: {local_path}")
    print('='*60)

    # Check if files already exist
    if local_path:
        check_path = DATA_DIR.parent / local_path
        if check_path.exists():
            if check_path.is_dir():
                files = list(check_path.glob('*'))
                if files:
                    print(f"\n✓ Found {len(files)} files in {check_path}")
                    return True
            else:
                print(f"\n✓ Found file: {check_path}")
                return True

    # Prompt user
    print("\nHave you completed the manual download? (y/n/skip): ", end='')
    response = input().strip().lower()

    if response == 'skip':
        print(f"Skipping {source_key}")
        return False
    elif response == 'y':
        # Verify files exist
        if local_path:
            check_path = DATA_DIR.parent / local_path
            if check_path.exists():
                print(f"✓ Files verified at {check_path}")
                return True
            else:
                print(f"✗ Files not found at expected location: {check_path}")
                return False
        return True
    else:
        print(f"Skipping {source_key} - please complete manual download first")
        return False


async def check_source_updates(conn, source: dict) -> dict:
    """
    Check if a source has upstream updates without downloading.
    Returns dict with: needs_update, etag, last_modified, error
    """
    source_key = source['source_key']
    download_url = source.get('download_url')
    api_endpoint = source.get('api_endpoint')
    stored_etag = source.get('upstream_etag')

    result = {
        'source_key': source_key,
        'needs_update': None,
        'etag': None,
        'last_modified': None,
        'error': None
    }

    # Check URL-based sources
    url = download_url or api_endpoint
    if url:
        version_info = await check_upstream_version(url, stored_etag)
        result.update({
            'needs_update': version_info['changed'],
            'etag': version_info['etag'],
            'last_modified': version_info['last_modified'],
            'error': version_info['error']
        })

        # Update last_sync_check
        await conn.execute("""
            UPDATE data_sources SET last_sync_check = NOW() WHERE source_key = $1
        """, source_key)

    return result


async def download_source(conn, source: dict, force: bool = False) -> bool:
    """
    Download a single data source using modular downloaders.
    Returns: True on success, False on failure/skip
    """
    # Import modular downloaders
    try:
        from arcgis_downloader import download_arcgis, STANDARD_SOURCES
        from hamilton_downloader import download_hamilton
        from toronto_downloader import download_toronto
        from nar_downloader import download_nar
        from fsa_downloader import download_fsa
    except ImportError:
        from .arcgis_downloader import download_arcgis, STANDARD_SOURCES
        from .hamilton_downloader import download_hamilton
        from .toronto_downloader import download_toronto
        from .nar_downloader import download_nar
        from .fsa_downloader import download_fsa

    source_key = source['source_key']
    source_name = source['source_name']
    api_type = source.get('api_type')
    api_endpoint = source.get('api_endpoint')
    download_url = source.get('download_url')
    download_format = source.get('download_format', 'geojson')
    local_file_path = source.get('local_file_path')
    requires_manual = source.get('requires_manual_download', False)

    print(f"\n--- {source_name} ({source_key}) ---")

    # Determine destination path
    if local_file_path:
        dest_path = DATA_DIR.parent / local_file_path
    else:
        dest_path = DATA_DIR / source_key / f"{source_key}.{download_format}"

    # Check if already downloaded (unless force)
    if not force and dest_path.exists():
        # Validate GeoJSON files aren't truncated from a previous crashed download
        if dest_path.suffix.lower() in ('.geojson', '.json'):
            try:
                with open(dest_path, 'rb') as f:
                    f.seek(max(0, dest_path.stat().st_size - 10))
                    tail = f.read().decode('utf-8', errors='ignore').strip()
                    if not tail.endswith(']}'):
                        print(f"  Incomplete download detected, re-downloading: {dest_path}")
                        # Fall through to re-download
                    else:
                        print(f"  Already exists: {dest_path}")
                        print(f"  Use --force to re-download")
                        return True
            except Exception:
                pass  # Fall through to re-download
        else:
            print(f"  Already exists: {dest_path}")
            print(f"  Use --force to re-download")
            return True

    try:
        record_count = None
        downloaded = True
        result = None

        # Handle manual downloads
        if requires_manual:
            if await handle_manual_download(source):
                await update_source_refresh(conn, source_key)
                return True
            return False

        # Route to specialized modular downloaders
        if source_key == 'hamilton_addresses':
            result = await download_hamilton(force)
        elif source_key == 'toronto_addresses':
            result = await download_toronto(force)
        elif source_key == 'nar_ontario':
            result = await download_nar(force)
        elif source_key == 'fsa_boundaries':
            result = await download_fsa(force)
        elif source_key in STANDARD_SOURCES:
            # Use modular ArcGIS downloader for standard sources
            endpoint = STANDARD_SOURCES[source_key]
            result = await download_arcgis(source_key, endpoint, force)
        elif api_type and 'arcgis' in api_type.lower() and api_endpoint:
            # ArcGIS source not in STANDARD_SOURCES - use endpoint from database
            result = await download_arcgis(source_key, api_endpoint, force)
        # ArcGIS Hub direct download (preferred for polygon data with complex geometry)
        elif api_type and api_type.lower() == 'arcgis/hub' and download_url:
            # Hub downloads are typically ZIP files containing shapefiles
            downloaded, file_hash = await download_with_version_check(
                conn, source, download_url, dest_path, extract_zip=True, force=force
            )
            if not downloaded:
                print(f"  ✓ Already up to date")
                return True
        # Direct URL download (with version tracking)
        elif download_url:
            downloaded, file_hash = await download_with_version_check(
                conn, source, download_url, dest_path, force=force
            )
            if not downloaded:
                print(f"  ✓ Already up to date")
                return True
        # No download method available
        else:
            print(f"  No download method configured for {source_key}")
            print(f"  api_type: {api_type}, api_endpoint: {api_endpoint}, download_url: {download_url}")
            await update_sync_status(conn, source_key, 'error', error='No download method configured')
            return False

        # Handle modular downloader result
        if result:
            if not result['success']:
                print(f"  ✗ {result.get('error', 'Unknown error')}")
                await update_sync_status(conn, source_key, 'error', error=result.get('error'))
                return False
            record_count = result.get('record_count')
            dest_path = result.get('file_path', dest_path)

        # Update database
        await update_source_refresh(conn, source_key, record_count)

        # Extract and save schema information from downloaded file
        if dest_path and Path(dest_path).exists() and Path(dest_path).suffix.lower() in ('.geojson', '.json'):
            schema_fields, schema_hash = extract_geojson_schema(Path(dest_path))
            if schema_fields:
                await update_schema_info(conn, source_key, schema_fields, schema_hash)
                print(f"  Schema: {len(schema_fields)} fields tracked")

        # Mark as synced
        await update_sync_status(conn, source_key, 'synced')
        print(f"  ✓ Download complete")
        return True

    except Exception as e:
        print(f"  ✗ Error downloading {source_key}: {e}")
        await update_sync_status(conn, source_key, 'error', error=str(e))
        return False


async def run_simple_mode(source_key: str, sources: list, force: bool = False):
    """
    Run downloads using the simplified modular downloaders.

    Routes sources to:
    - arcgis_downloader.py: Standard ArcGIS sources (34)
    - hamilton_downloader.py: Hamilton (rate-limited)
    - toronto_downloader.py: Toronto CKAN
    - nar_downloader.py: NAR Ontario (CSV to GeoJSON)
    - fsa_downloader.py: FSA boundaries (shapefile only)
    """
    # Import modular downloaders
    try:
        from arcgis_downloader import download_arcgis, STANDARD_SOURCES
        from hamilton_downloader import download_hamilton
        from toronto_downloader import download_toronto
        from nar_downloader import download_nar
        from fsa_downloader import download_fsa
        from download_utils import print_result
    except ImportError:
        from .arcgis_downloader import download_arcgis, STANDARD_SOURCES
        from .hamilton_downloader import download_hamilton
        from .toronto_downloader import download_toronto
        from .nar_downloader import download_nar
        from .fsa_downloader import download_fsa
        from .download_utils import print_result

    results = []

    # Single source mode
    if source_key:
        source_keys = [source_key]
    else:
        source_keys = [s['source_key'] for s in sources if not s.get('requires_manual_download')]

    print(f"\nDownloading {len(source_keys)} sources using simplified downloaders...")
    print("=" * 60)

    for key in source_keys:
        # Route to appropriate downloader
        if key == 'hamilton_addresses':
            result = await download_hamilton(force)
        elif key == 'toronto_addresses':
            result = await download_toronto(force)
        elif key == 'nar_ontario':
            result = await download_nar(force)
        elif key == 'fsa_boundaries':
            result = await download_fsa(force)
        elif key in STANDARD_SOURCES:
            endpoint = STANDARD_SOURCES[key]
            result = await download_arcgis(key, endpoint, force)
        else:
            print(f"\n⚠ Unknown source: {key} (not in any modular downloader)")
            continue

        results.append(result)
        print_result(result)

    # Summary
    success = sum(1 for r in results if r['success'])
    print(f"\n{'='*60}")
    print(f"Download Summary:")
    print(f"  Successful: {success}")
    print(f"  Failed:     {len(results) - success}")
    print(f"{'='*60}")


async def main():
    parser = argparse.ArgumentParser(
        description='Download data sources from configured URLs and APIs',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Download a specific source
  %(prog)s --source toronto_addresses

  # Download all confirmed sources
  %(prog)s --all-confirmed

  # Force re-download of existing files
  %(prog)s --source guelph_addresses --force

  # Check for updates without downloading
  %(prog)s --all-confirmed --check-only

  # List available sources without downloading
  %(prog)s --list
        """
    )
    parser.add_argument('--source', '-s', help='Specific source_key to download')
    parser.add_argument('--all-confirmed', '-a', action='store_true',
                        help='Download all confirmed sources')
    parser.add_argument('--force', '-f', action='store_true',
                        help='Re-download even if files exist')
    parser.add_argument('--check-only', '-c', action='store_true',
                        help='Check for updates without downloading')
    parser.add_argument('--list', '-l', action='store_true',
                        help='List available sources and exit')
    parser.add_argument('--skip-manual', action='store_true',
                        help='Skip sources that require manual download')
    parser.add_argument('--simple', action='store_true',
                        help='Use the simplified modular downloaders (arcgis, hamilton, toronto, zip)')

    args = parser.parse_args()

    if not args.source and not args.all_confirmed and not args.list:
        parser.print_help()
        sys.exit(1)

    # Ensure data directory exists
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    conn = await get_db_connection()

    try:
        # List mode
        if args.list:
            sources = await get_data_sources(conn, status='confirmed')
            print("\nConfirmed Data Sources:")
            print("-" * 80)
            for src in sources:
                method = 'Manual' if src['requires_manual_download'] else \
                         'API' if src['api_endpoint'] else \
                         'URL' if src['download_url'] else 'Unknown'
                refreshed = src['last_data_refresh'].strftime('%Y-%m-%d') if src['last_data_refresh'] else 'Never'
                print(f"  {src['source_key']:30} {method:8} Last: {refreshed}")
            return

        # Get sources to download
        if args.source:
            sources = await get_data_sources(conn, source_key=args.source)
            if not sources:
                print(f"Error: Source '{args.source}' not found in data_sources table")
                sys.exit(1)
        else:
            sources = await get_data_sources(conn, status='confirmed')

        # Check-only mode
        if args.check_only:
            print("\nChecking for updates...")
            print("-" * 60)

            needs_update = []
            up_to_date = []
            errors = []

            for source in sources:
                if source.get('requires_manual_download'):
                    continue

                result = await check_source_updates(conn, dict(source))
                if result['error']:
                    errors.append((source['source_key'], result['error']))
                elif result['needs_update']:
                    needs_update.append(source['source_key'])
                elif result['needs_update'] is False:
                    up_to_date.append(source['source_key'])

            print(f"\n{'='*60}")
            print("Update Check Summary:")
            print(f"{'='*60}")
            if needs_update:
                print(f"\nNeeds update ({len(needs_update)}):")
                for key in needs_update:
                    print(f"  - {key}")
            if up_to_date:
                print(f"\nUp to date ({len(up_to_date)}):")
                for key in up_to_date:
                    print(f"  - {key}")
            if errors:
                print(f"\nErrors ({len(errors)}):")
                for key, error in errors:
                    print(f"  - {key}: {error}")
            print(f"{'='*60}")
            return

        # Simplified modular downloader mode
        if args.simple:
            await run_simple_mode(args.source, sources, args.force)
            return

        # Download (legacy mode)
        success_count = 0
        skip_count = 0
        fail_count = 0

        for source in sources:
            # Skip manual downloads if requested
            if args.skip_manual and source.get('requires_manual_download'):
                print(f"\nSkipping manual download: {source['source_key']}")
                skip_count += 1
                continue

            if await download_source(conn, dict(source), force=args.force):
                success_count += 1
            else:
                fail_count += 1

        # Summary
        print(f"\n{'='*60}")
        print(f"Download Summary:")
        print(f"  Successful: {success_count}")
        print(f"  Skipped:    {skip_count}")
        print(f"  Failed:     {fail_count}")
        print(f"{'='*60}")

    finally:
        await conn.close()


if __name__ == '__main__':
    asyncio.run(main())
