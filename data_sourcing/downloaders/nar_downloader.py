#!/usr/bin/env python3
"""
NAR Ontario downloader.

Downloads the National Address Register for Ontario from Statistics Canada
and extracts the CSV files for import_nar_data.py to process.

Note: This downloader does NOT convert to GeoJSON. The import_nar_data.py
script reads the CSV files directly.
"""

import argparse
import asyncio
import logging
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx

try:
    from .download_utils import make_result, print_result
except ImportError:  # Direct script entrypoint
    from download_utils import make_result, print_result

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

SOURCE_KEY = "nar_ontario"
NAR_PAGE_URL = "https://www150.statcan.gc.ca/n1/pub/46-26-0002/462600022022001-eng.htm"
NAR_BASE_URL = "https://www150.statcan.gc.ca/n1/pub/46-26-0002/"

SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR.parent / 'data_sources'

# Request timeout (longer for large files - NAR is ~2GB)
TIMEOUT = httpx.Timeout(1800.0, connect=60.0)


def check_download_response(response: httpx.Response) -> None:
    """Report upstream access controls without attempting to bypass them."""
    if response.headers.get('cf-mitigated') == 'challenge':
        raise RuntimeError(
            'Statistics Canada returned a Cloudflare security challenge. '
            'The headless HTTP downloader cannot complete it; repeated retries will not solve it. '
            'Ask Statistics Canada for an approved automated distribution/allowlisting, '
            'or provide an authorized ZIP using NAR_DOWNLOAD_URL or NAR_ARCHIVE_PATH. '
            'A direct URL may also be challenged. No browser or challenge solver is used.'
        )
    if response.status_code == 403:
        raise RuntimeError(
            'Statistics Canada refused the request (HTTP 403). '
            'Do not retry indefinitely. Request approved automated access or provide '
            'an authorized ZIP using NAR_DOWNLOAD_URL or NAR_ARCHIVE_PATH.'
        )
    response.raise_for_status()


def extract_ontario_csvs(zip_path: Path, output_dir: Path) -> int:
    """Stream Ontario CSVs only; validate in staging before replacing retained files."""
    with zipfile.ZipFile(zip_path) as archive:
        members = [member for member in archive.infolist()
                   if not member.is_dir()
                   and re.fullmatch(r'(Address|Location)_35[^/]*\.csv', Path(member.filename).name)]
        names = [Path(member.filename).name for member in members]
        if not any(name.startswith('Address_35') for name in names):
            raise ValueError('No Ontario address CSV files (Address_35*.csv) found in ZIP')
        if len(names) != len(set(names)):
            raise ValueError('Duplicate Ontario CSV filenames in ZIP')
        # Extract only Ontario basenames, not archive paths. Stream files to
        # bound memory usage; ZipFile validates their CRCs.
        with tempfile.TemporaryDirectory(dir=output_dir) as staging:
            for member, name in zip(members, names):
                with archive.open(member) as src, (Path(staging) / name).open('wb') as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
            for name in names:
                (Path(staging) / name).replace(output_dir / name)
            # Avoid mixing obsolete parts with a new release on the next import.
            for pattern in ('Address_35*.csv', 'Location_35*.csv'):
                for previous in output_dir.glob(pattern):
                    if previous.name not in names:
                        previous.unlink()
    return len(names)


async def get_latest_nar_url() -> tuple[str, str]:
    """
    Scrape the Statistics Canada NAR page to find the most recent data file.

    Returns: (download_url, reference_period)
    """
    logger.info("Checking Statistics Canada for latest NAR release...")

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(NAR_PAGE_URL)
        check_download_response(response)

        zip_links = re.findall(r'href="(2022001/[^"]+\.zip)"', response.text)

        if not zip_links:
            raise ValueError("No NAR download links found on page")

        # Releases use either 2022001/YYYYMM.zip or 2022001/YYYY.zip.
        def parse_date(filename):
            match = re.search(r'/(\d+)\.zip$', filename)
            if match:
                date_str = match.group(1)
                if len(date_str) == 6:  # YYYYMM format
                    return int(date_str)
                elif len(date_str) == 4:  # YYYY format
                    return int(date_str) * 100
            return 0

        sorted_links = sorted(zip_links, key=parse_date, reverse=True)
        most_recent = sorted_links[0]

        full_url = NAR_BASE_URL + most_recent

        match = re.search(r'/(\d+)\.zip$', most_recent)
        ref_period = match.group(1) if match else "unknown"

        logger.info(f"Found latest release: {ref_period}")
        return full_url, ref_period


async def download_file(url: str, dest_path: Path, client: httpx.AsyncClient) -> None:
    """Download a file with progress logging."""
    logger.info(f"Downloading from {url}")

    async with client.stream("GET", url, follow_redirects=True) as response:
        check_download_response(response)
        if 'text/html' in response.headers.get('content-type', '').lower():
            raise ValueError('Expected a NAR ZIP archive, received HTML instead. '
                             'Use an authorized archive URL or NAR_ARCHIVE_PATH.')
        total = int(response.headers.get("content-length", 0))
        downloaded = 0

        with open(dest_path, "wb") as f:
            async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                if total > 0 and downloaded % (100 * 1024 * 1024) == 0:
                    pct = (downloaded / total) * 100
                    logger.info(f"Downloaded {downloaded / (1024*1024):.0f} MB ({pct:.0f}%)")

    size_mb = dest_path.stat().st_size / (1024 * 1024)
    logger.info(f"Download complete: {size_mb:.1f} MB")


async def download_nar(force: bool = False) -> dict:
    """
    Download NAR Ontario and extract CSV files.

    The CSV files are extracted to data_sources/nar_ontario/ for
    import_nar_data.py to process directly.

    Returns: {"success": bool, "source_key": str, "records": int, "path": str, "error": str|None}
    """
    logger.info(f"[{SOURCE_KEY}] Starting NAR Ontario download")

    output_dir = DATA_DIR / SOURCE_KEY
    output_dir.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            download_url = os.getenv('NAR_DOWNLOAD_URL', '').strip()
            archive_path = os.getenv('NAR_ARCHIVE_PATH', '').strip()
            if download_url and archive_path:
                raise ValueError('Set only one of NAR_DOWNLOAD_URL and NAR_ARCHIVE_PATH')

            if archive_path:
                file_count = extract_ontario_csvs(Path(archive_path), output_dir)
                ref_period = 'operator-supplied archive'
            else:
                if download_url:
                    parsed = urlsplit(download_url)
                    if parsed.scheme != 'https' or not parsed.hostname:
                        raise ValueError('NAR_DOWNLOAD_URL must be an HTTPS archive URL')
                    ref_period = 'operator-supplied URL (not auto-discovered latest)'
                else:
                    download_url, ref_period = await get_latest_nar_url()
                with tempfile.TemporaryDirectory() as tmpdir:
                    zip_path = Path(tmpdir) / 'nar_ontario.zip'
                    await download_file(download_url, zip_path, client)
                    file_count = extract_ontario_csvs(zip_path, output_dir)

            logger.info(f"[{SOURCE_KEY}] Complete: {file_count} CSV files extracted (ref: {ref_period})")
            # Return file count as "records" - the actual record count will be
            # determined by import_nar_data.py when processing the CSVs
            return make_result(True, SOURCE_KEY, file_count, str(output_dir))

        except Exception as e:
            logger.exception(f"[{SOURCE_KEY}] Download failed")
            return make_result(False, SOURCE_KEY, error=str(e))


async def main():
    parser = argparse.ArgumentParser(description="Download NAR Ontario from Statistics Canada")
    parser.add_argument("--force", action="store_true", help="Force re-download")

    args = parser.parse_args()

    result = await download_nar(args.force)
    print_result(result)
    sys.exit(0 if result["success"] else 1)


if __name__ == "__main__":
    asyncio.run(main())
