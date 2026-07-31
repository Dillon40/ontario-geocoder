#!/usr/bin/env python3
"""
Toronto Open Data downloader using CKAN API.

Toronto provides address data directly as GeoJSON via their CKAN portal.
"""

import argparse
import asyncio
import json
import logging
import sys

import httpx

from download_utils import (
    ensure_output_dir,
    validate_fields,
    make_result,
    print_result,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Toronto configuration
SOURCE_KEY = "toronto_addresses"
CKAN_BASE = "https://ckan0.cf.opendata.inter.prod-toronto.ca"
PACKAGE_ID = "address-points-municipal-toronto-one-address-repository"

# Direct GeoJSON URL (WGS84/4326)
GEOJSON_URL = "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/abedd8bc-e3dd-4d45-8e69-79165a76e4fa/resource/b1c2ab72-dfe7-4b29-8550-6d1cfaa61733/download/address-points-4326.geojson"

# Request timeout (longer for large file)
TIMEOUT = httpx.Timeout(600.0, connect=60.0)


async def get_geojson_url(client: httpx.AsyncClient) -> str:
    """
    Get the GeoJSON resource URL from CKAN API.

    Falls back to hardcoded URL if API fails.
    """
    try:
        response = await client.get(
            f"{CKAN_BASE}/api/3/action/package_show",
            params={"id": PACKAGE_ID}
        )
        response.raise_for_status()
        data = response.json()

        if data.get("success"):
            resources = data.get("result", {}).get("resources", [])
            for resource in resources:
                # Look for GeoJSON in 4326 projection
                if resource.get("format", "").upper() == "GEOJSON":
                    name = resource.get("name", "").lower()
                    if "4326" in name:
                        logger.info(f"Found GeoJSON resource: {resource['name']}")
                        return resource["url"]

            # If no 4326 GeoJSON, try any GeoJSON
            for resource in resources:
                if resource.get("format", "").upper() == "GEOJSON":
                    logger.info(f"Found GeoJSON resource: {resource['name']}")
                    return resource["url"]

        logger.warning("Could not find GeoJSON in CKAN response, using fallback URL")

    except Exception as e:
        logger.warning(f"CKAN API error: {e}, using fallback URL")

    return GEOJSON_URL


async def download_toronto(force: bool = False) -> dict:
    """
    Download Toronto addresses from Open Data portal.

    Returns: {"success": bool, "source_key": str, "records": int, "path": str, "error": str|None}
    """
    logger.info(f"[{SOURCE_KEY}] Starting download from Toronto Open Data")

    output_path = ensure_output_dir(SOURCE_KEY)

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            # Step 1: Get the GeoJSON URL
            geojson_url = await get_geojson_url(client)
            logger.info(f"[{SOURCE_KEY}] Downloading from: {geojson_url}")

            # Step 2: Download GeoJSON with streaming and retry
            temp_path = output_path.with_suffix('.tmp')
            max_retries = 3

            for attempt in range(1, max_retries + 1):
                try:
                    async with client.stream("GET", geojson_url, follow_redirects=True) as response:
                        response.raise_for_status()

                        total = int(response.headers.get("content-length", 0))
                        downloaded = 0

                        with open(temp_path, "wb") as f:
                            async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                                f.write(chunk)
                                downloaded += len(chunk)

                                if total > 0 and downloaded % (50 * 1024 * 1024) == 0:
                                    pct = (downloaded / total) * 100
                                    logger.info(f"[{SOURCE_KEY}] Downloaded {downloaded / (1024*1024):.0f} MB ({pct:.0f}%)")

                    break  # Success
                except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.RemoteProtocolError) as e:
                    if temp_path.exists():
                        temp_path.unlink()
                    if attempt < max_retries:
                        wait = 30 * attempt
                        logger.warning(f"[{SOURCE_KEY}] Download failed ({e}), retrying in {wait}s ({attempt}/{max_retries})")
                        await asyncio.sleep(wait)
                    else:
                        raise

            temp_path.rename(output_path)
            size_mb = output_path.stat().st_size / (1024 * 1024)
            logger.info(f"[{SOURCE_KEY}] Download complete: {size_mb:.1f} MB")

            # Step 3: Count records and validate fields
            logger.info(f"[{SOURCE_KEY}] Counting records...")
            record_count = 0
            actual_fields = set()

            with open(output_path, "r") as f:
                data = json.load(f)
                features = data.get("features", [])
                record_count = len(features)

                # Collect fields from first few features
                for feature in features[:10]:
                    actual_fields.update(feature.get("properties", {}).keys())

            # Validate fields
            validate_fields(SOURCE_KEY, actual_fields)

            logger.info(f"[{SOURCE_KEY}] Complete: {record_count:,} records")
            return make_result(True, SOURCE_KEY, record_count, output_path)

        except Exception as e:
            logger.exception(f"[{SOURCE_KEY}] Download failed")
            return make_result(False, SOURCE_KEY, error=str(e))


async def main():
    parser = argparse.ArgumentParser(description="Download Toronto addresses from Open Data")
    parser.add_argument("--force", action="store_true", help="Force re-download")

    args = parser.parse_args()

    result = await download_toronto(args.force)
    print_result(result)
    sys.exit(0 if result["success"] else 1)


if __name__ == "__main__":
    asyncio.run(main())
