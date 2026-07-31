#!/usr/bin/env python3
"""
Hamilton-specific downloader with rate limiting.

Hamilton's ArcGIS server enforces strict rate limits.
Uses 6-second delays between requests and exponential backoff on errors.
"""

import argparse
import asyncio
import logging
import sys

import httpx

from download_utils import (
    ensure_output_dir,
    stream_geojson_start,
    stream_geojson_feature,
    stream_geojson_end,
    validate_fields,
    make_result,
    print_result,
    get_server_info,
    get_record_count,
    get_resume_oid,
    fetch_arcgis_page_with_retry,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Hamilton configuration
HAMILTON_ENDPOINT = "https://services.arcgis.com/rYz782eMbySr2srL/arcgis/rest/services/Addresses/FeatureServer/1"
SOURCE_KEY = "hamilton_addresses"

# Rate limiting settings
REQUEST_DELAY = 6.0       # Seconds between requests
BACKOFF_BASE = 90.0       # Initial backoff on rate limit
BACKOFF_MAX = 600.0       # Max backoff (10 minutes)
MAX_RETRIES = 5           # Max retries per page

# Request timeout
TIMEOUT = httpx.Timeout(60.0, connect=30.0)


async def download_hamilton(force: bool = False) -> dict:
    """
    Download Hamilton addresses with rate limiting.

    Returns: {"success": bool, "source_key": str, "records": int, "path": str, "error": str|None}
    """
    logger.info(f"[{SOURCE_KEY}] Starting download from {HAMILTON_ENDPOINT}")
    logger.info(f"[{SOURCE_KEY}] Rate limit: {REQUEST_DELAY}s between requests")

    output_path = ensure_output_dir(SOURCE_KEY)

    # Check for resume from partial download
    last_oid = 0
    records_written = 0
    is_first = True
    resuming = False

    if not force:
        resume_oid = get_resume_oid(output_path)
        if resume_oid is not None:
            last_oid = resume_oid
            resuming = True
            logger.info(f"[{SOURCE_KEY}] Resuming from OID {last_oid}")

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            # Step 1: Get server info
            info = await get_server_info(HAMILTON_ENDPOINT, client)
            oid_field = info["oid_field"]
            # Use smaller page size for Hamilton to reduce request time
            page_size = min(info["max_record_count"], 1000)
            output_format = "geojson" if info["supports_geojson"] else "json"

            logger.info(f"[{SOURCE_KEY}] OID field: {oid_field}, page size: {page_size}, format: {output_format}")

            # Step 2: Get total count
            total_count = await get_record_count(HAMILTON_ENDPOINT, client)
            logger.info(f"[{SOURCE_KEY}] Total records: {total_count:,}")

            # Estimate time
            pages = (total_count // page_size) + 1
            est_time = pages * REQUEST_DELAY / 60
            logger.info(f"[{SOURCE_KEY}] Estimated time: ~{est_time:.0f} minutes ({pages} pages)")

            # Step 3: Stream paginated results to file
            actual_fields = set()
            request_count = 0

            file_mode = "a" if resuming else "w"
            with open(output_path, file_mode) as f:
                if not resuming:
                    stream_geojson_start(f)
                else:
                    is_first = False

                while True:
                    # Rate limit: wait before each request (except first)
                    if request_count > 0:
                        logger.debug(f"[{SOURCE_KEY}] Waiting {REQUEST_DELAY}s...")
                        await asyncio.sleep(REQUEST_DELAY)

                    features, new_last_oid = await fetch_arcgis_page_with_retry(
                        HAMILTON_ENDPOINT, oid_field, last_oid, page_size,
                        output_format, client,
                        max_retries=MAX_RETRIES, backoff_base=BACKOFF_BASE,
                        backoff_max=BACKOFF_MAX, source_key=SOURCE_KEY,
                    )
                    request_count += 1

                    if not features:
                        break

                    for feature in features:
                        stream_geojson_feature(f, feature, is_first)
                        is_first = False
                        records_written += 1

                        # Collect field names from first few features
                        if records_written <= 10:
                            actual_fields.update(feature.get("properties", {}).keys())

                    last_oid = new_last_oid

                    # Progress logging
                    if records_written % 5000 == 0:
                        pct = (records_written / total_count * 100) if total_count else 0
                        logger.info(f"[{SOURCE_KEY}] Progress: {records_written:,} / {total_count:,} ({pct:.0f}%)")

                stream_geojson_end(f)

            # Step 4: Validate fields
            validate_fields(SOURCE_KEY, actual_fields)

            logger.info(f"[{SOURCE_KEY}] Complete: {records_written:,} records")
            return make_result(True, SOURCE_KEY, records_written, output_path)

        except Exception as e:
            logger.exception(f"[{SOURCE_KEY}] Download failed")
            return make_result(False, SOURCE_KEY, error=str(e))


async def main():
    parser = argparse.ArgumentParser(description="Download Hamilton addresses with rate limiting")
    parser.add_argument("--force", action="store_true", help="Force re-download")

    args = parser.parse_args()

    result = await download_hamilton(args.force)
    print_result(result)
    sys.exit(0 if result["success"] else 1)


if __name__ == "__main__":
    asyncio.run(main())
