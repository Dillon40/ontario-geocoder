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
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import httpx

from download_utils import (
    make_result,
    print_result,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# NAR configuration
SOURCE_KEY = "nar_ontario"
NAR_PAGE_URL = "https://www150.statcan.gc.ca/n1/pub/46-26-0002/462600022022001-eng.htm"
NAR_BASE_URL = "https://www150.statcan.gc.ca/n1/pub/46-26-0002/"

# Output directory for NAR CSV files
SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR.parent / 'data_sources'

# Request timeout (longer for large files - NAR is ~2GB)
TIMEOUT = httpx.Timeout(1800.0, connect=60.0)


async def get_latest_nar_url() -> tuple[str, str]:
    """
    Scrape the Statistics Canada NAR page to find the most recent data file.

    Returns: (download_url, reference_period)
    """
    logger.info("Checking Statistics Canada for latest NAR release...")

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(NAR_PAGE_URL)
        response.raise_for_status()

        # Extract all ZIP file links
        zip_links = re.findall(r'href="(2022001/[^"]+\.zip)"', response.text)

        if not zip_links:
            raise ValueError("No NAR download links found on page")

        # Parse dates from filenames and find most recent
        # Format: 2022001/YYYYMM.zip or 2022001/YYYY.zip
        def parse_date(filename):
            match = re.search(r'/(\d+)\.zip$', filename)
            if match:
                date_str = match.group(1)
                if len(date_str) == 6:  # YYYYMM format
                    return int(date_str)
                elif len(date_str) == 4:  # YYYY format
                    return int(date_str) * 100
            return 0

        # Sort by date (descending) to get most recent first
        sorted_links = sorted(zip_links, key=parse_date, reverse=True)
        most_recent = sorted_links[0]

        full_url = NAR_BASE_URL + most_recent

        # Extract reference period for display
        match = re.search(r'/(\d+)\.zip$', most_recent)
        ref_period = match.group(1) if match else "unknown"

        logger.info(f"Found latest release: {ref_period}")
        return full_url, ref_period


async def download_file(url: str, dest_path: Path, client: httpx.AsyncClient) -> None:
    """Download a file with progress logging."""
    logger.info(f"Downloading from {url}")

    async with client.stream("GET", url, follow_redirects=True) as response:
        response.raise_for_status()
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

    # Create output directory
    output_dir = DATA_DIR / SOURCE_KEY
    output_dir.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            # Step 1: Find latest NAR URL
            download_url, ref_period = await get_latest_nar_url()

            with tempfile.TemporaryDirectory() as tmpdir:
                tmpdir = Path(tmpdir)

                # Step 2: Download ZIP
                zip_path = tmpdir / "nar_ontario.zip"
                await download_file(download_url, zip_path, client)

                # Step 3: Extract
                logger.info("Extracting ZIP...")
                extract_dir = tmpdir / "extracted"
                extract_dir.mkdir()

                with zipfile.ZipFile(zip_path, 'r') as zf:
                    zf.extractall(extract_dir)

                # Step 4: Find Ontario CSV files (province code 35)
                # NAR contains multiple provinces - we only want Ontario
                # Look for Address_35*.csv and Location_35*.csv files
                ontario_csv_files = sorted(extract_dir.rglob("*_35*.csv"))
                if not ontario_csv_files:
                    # Fallback: list all CSV files for debugging
                    all_csv = list(extract_dir.rglob("*.csv"))
                    logger.error(f"No Ontario CSV files found. Available: {[f.name for f in all_csv[:10]]}")
                    return make_result(False, SOURCE_KEY, error="No Ontario CSV files (*_35*.csv) found in ZIP")

                logger.info(f"Found {len(ontario_csv_files)} Ontario CSV files")
                for f in ontario_csv_files:
                    logger.info(f"  - {f.name}")

                # Step 5: Copy Ontario CSV files to output directory
                for csv_file in ontario_csv_files:
                    dest_file = output_dir / csv_file.name
                    logger.info(f"Copying {csv_file.name} to {output_dir}")
                    shutil.copy2(csv_file, dest_file)

                file_count = len(ontario_csv_files)

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
