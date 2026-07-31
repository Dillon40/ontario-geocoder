#!/usr/bin/env python3
"""
FSA Boundaries downloader.

Downloads Forward Sortation Area boundary shapefiles from Statistics Canada.
Extracts the shapefile without conversion - the import_fsa_boundaries.py
importer reads shapefiles directly.
"""

import argparse
import asyncio
import logging
import sys
import tempfile
import zipfile
from pathlib import Path

import httpx

from download_utils import (
    make_result,
    print_result,
    DATA_DIR,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# FSA configuration
SOURCE_KEY = "fsa_boundaries"
FSA_URL = "https://www12.statcan.gc.ca/census-recensement/2021/geo/sip-pis/boundary-limites/files-fichiers/lfsa000a21a_e.zip"

# Request timeout
TIMEOUT = httpx.Timeout(300.0, connect=60.0)


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
                if total > 0 and downloaded % (10 * 1024 * 1024) == 0:
                    pct = (downloaded / total) * 100
                    logger.info(f"Downloaded {downloaded / (1024*1024):.0f} MB ({pct:.0f}%)")

    size_mb = dest_path.stat().st_size / (1024 * 1024)
    logger.info(f"Download complete: {size_mb:.1f} MB")


async def download_fsa(force: bool = False) -> dict:
    """
    Download FSA boundaries shapefile.

    Extracts to: data_sources/fsa_boundaries/lfsa000a21a_e/
    The import_fsa_boundaries.py importer reads the .shp file directly.

    Returns: {"success": bool, "source_key": str, "records": int, "path": str, "error": str|None}
    """
    logger.info(f"[{SOURCE_KEY}] Starting FSA boundaries download")

    # Output directory for extracted shapefile
    output_dir = DATA_DIR / SOURCE_KEY
    output_dir.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmpdir = Path(tmpdir)

                # Step 1: Download ZIP
                zip_path = tmpdir / "fsa_boundaries.zip"
                await download_file(FSA_URL, zip_path, client)

                # Step 2: Extract to output directory
                logger.info(f"Extracting to {output_dir}")

                with zipfile.ZipFile(zip_path, 'r') as zf:
                    zf.extractall(output_dir)

                # Step 3: Find the shapefile
                shp_files = list(output_dir.rglob("*.shp"))
                if not shp_files:
                    return make_result(False, SOURCE_KEY, error="No .shp file found in ZIP")

                shp_path = shp_files[0]
                logger.info(f"Extracted shapefile: {shp_path}")

                # Count features (optional, for reporting)
                feature_count = 0
                try:
                    import geopandas as gpd
                    gdf = gpd.read_file(shp_path)
                    feature_count = len(gdf)
                    logger.info(f"Shapefile contains {feature_count:,} FSA boundaries")
                except ImportError:
                    logger.info("geopandas not available, skipping feature count")
                except Exception as e:
                    logger.warning(f"Could not count features: {e}")

            logger.info(f"[{SOURCE_KEY}] Complete: shapefile extracted to {shp_path.parent}")
            return make_result(True, SOURCE_KEY, feature_count, shp_path)

        except Exception as e:
            logger.exception(f"[{SOURCE_KEY}] Download failed")
            return make_result(False, SOURCE_KEY, error=str(e))


async def main():
    parser = argparse.ArgumentParser(description="Download FSA boundaries from Statistics Canada")
    parser.add_argument("--force", action="store_true", help="Force re-download")

    args = parser.parse_args()

    result = await download_fsa(args.force)
    print_result(result)
    sys.exit(0 if result["success"] else 1)


if __name__ == "__main__":
    asyncio.run(main())
