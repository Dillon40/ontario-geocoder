#!/usr/bin/env python3
"""
Parcel-to-Address downloader for sources that only provide parcel polygons.

Downloads parcel polygons from ArcGIS, extracts centroids, and converts
to address points in a single step.

Use for sources like Haldimand County that don't have dedicated address point layers.
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
    fetch_arcgis_page_with_retry,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Parcel sources that need centroid extraction
PARCEL_SOURCES = {
    "haldimand_county_addresses": {
        "endpoint": "https://gis.haldimandcounty.ca/server/rest/services/Topo/ParcelsOnlinePublic/FeatureServer/0",
        "address_field": "StreetAddress",  # Field containing the street address
        "street_number_field": "StreetNumber",
        "street_name_field": "StreetName",
        "filter_empty_addresses": True,  # Skip parcels without addresses
    },
}

TIMEOUT = httpx.Timeout(60.0, connect=30.0)


def extract_centroid(geometry: dict) -> tuple[float, float] | None:
    """
    Extract centroid from a polygon or multipolygon geometry.

    Returns (longitude, latitude) or None if invalid.
    """
    if not geometry:
        return None

    geom_type = geometry.get("type", "")
    coords = geometry.get("coordinates", [])

    if not coords:
        return None

    try:
        if geom_type == "Polygon":
            # Use the exterior ring (first ring)
            ring = coords[0] if coords else []
            return _ring_centroid(ring)

        elif geom_type == "MultiPolygon":
            # Find centroid of the largest polygon
            max_area = 0
            best_centroid = None

            for polygon_coords in coords:
                ring = polygon_coords[0] if polygon_coords else []
                area = _ring_area(ring)
                if area > max_area:
                    max_area = area
                    best_centroid = _ring_centroid(ring)

            return best_centroid

        elif geom_type == "Point":
            # Already a point
            return (coords[0], coords[1]) if len(coords) >= 2 else None

        else:
            logger.warning(f"Unsupported geometry type: {geom_type}")
            return None

    except (IndexError, TypeError) as e:
        logger.debug(f"Centroid extraction error: {e}")
        return None


def _ring_centroid(ring: list) -> tuple[float, float] | None:
    """Calculate centroid of a polygon ring using simple average."""
    if not ring or len(ring) < 3:
        return None

    # Simple centroid: average of all points
    sum_x = sum(p[0] for p in ring)
    sum_y = sum(p[1] for p in ring)
    n = len(ring)

    return (sum_x / n, sum_y / n)


def _ring_area(ring: list) -> float:
    """Calculate approximate area of a polygon ring (shoelace formula)."""
    if not ring or len(ring) < 3:
        return 0

    n = len(ring)
    area = 0
    for i in range(n):
        j = (i + 1) % n
        area += ring[i][0] * ring[j][1]
        area -= ring[j][0] * ring[i][1]

    return abs(area) / 2


def convert_parcel_to_address(feature: dict, config: dict) -> dict | None:
    """
    Convert a parcel polygon feature to an address point feature.

    - Extracts centroid from polygon geometry
    - Filters out parcels without addresses (if configured)
    - Returns a Point feature with properties
    """
    props = feature.get("properties", {})
    geometry = feature.get("geometry")

    # Check if parcel has an address
    if config.get("filter_empty_addresses"):
        addr_field = config.get("address_field", "StreetAddress")
        address = props.get(addr_field, "")
        if not address or not str(address).strip():
            return None

        # Also check street number if available
        num_field = config.get("street_number_field")
        if num_field:
            street_num = props.get(num_field, "")
            if not street_num or not str(street_num).strip():
                return None

    # Extract centroid
    centroid = extract_centroid(geometry)
    if not centroid:
        return None

    lon, lat = centroid

    # Validate coordinates are in Ontario region (rough bounds)
    if not (-95 <= lon <= -74 and 41 <= lat <= 57):
        logger.debug(f"Coordinates outside Ontario bounds: {lon}, {lat}")
        return None

    # Create point feature
    return {
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [lon, lat]
        },
        "properties": props
    }


async def download_parcels(source_key: str, config: dict, force: bool = False) -> dict:
    """
    Download parcel polygons and convert to address points.

    Returns: {"success": bool, "source_key": str, "records": int, "path": str, "error": str|None}
    """
    endpoint = config["endpoint"]
    logger.info(f"[{source_key}] Starting parcel download from {endpoint}")

    output_path = ensure_output_dir(source_key)

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            # Step 1: Get server info (uses shared function with retry-enabled fetcher)
            info = await get_server_info(endpoint, client)
            oid_field = info["oid_field"]
            page_size = min(info["max_record_count"], 2000)
            geom_type = info.get("geometry_type", "")

            logger.info(f"[{source_key}] OID field: {oid_field}, geometry: {geom_type}, page size: {page_size}")

            if "Polygon" not in geom_type:
                logger.warning(f"[{source_key}] Expected polygon geometry, got: {geom_type}")

            # Step 2: Get total count
            total_count = await get_record_count(endpoint, client)
            logger.info(f"[{source_key}] Total parcels: {total_count:,}")

            # Step 3: Stream paginated results, converting polygons to points
            last_oid = 0
            parcels_processed = 0
            addresses_written = 0
            is_first = True
            actual_fields = set()

            with open(output_path, "w") as f:
                stream_geojson_start(f)

                while True:
                    features, new_last_oid = await fetch_arcgis_page_with_retry(
                        endpoint, oid_field, last_oid, page_size,
                        "geojson", client, source_key=source_key,
                    )

                    if not features:
                        break

                    for feature in features:
                        parcels_processed += 1

                        # Convert parcel to address point
                        address_feature = convert_parcel_to_address(feature, config)

                        if address_feature:
                            stream_geojson_feature(f, address_feature, is_first)
                            is_first = False
                            addresses_written += 1

                            # Collect field names from first few features
                            if addresses_written <= 10:
                                actual_fields.update(address_feature.get("properties", {}).keys())

                    last_oid = new_last_oid

                    # Progress logging
                    if parcels_processed % 5000 == 0:
                        pct = (parcels_processed / total_count * 100) if total_count else 0
                        logger.info(
                            f"[{source_key}] Progress: {parcels_processed:,} parcels processed, "
                            f"{addresses_written:,} addresses extracted ({pct:.0f}%)"
                        )

                stream_geojson_end(f)

            # Step 4: Validate fields
            validate_fields(source_key, actual_fields)

            logger.info(
                f"[{source_key}] Complete: {parcels_processed:,} parcels -> {addresses_written:,} addresses "
                f"({addresses_written/parcels_processed*100:.1f}% conversion rate)"
            )
            return make_result(True, source_key, addresses_written, output_path)

        except Exception as e:
            logger.exception(f"[{source_key}] Download failed")
            return make_result(False, source_key, error=str(e))


async def download_all(force: bool = False) -> list[dict]:
    """Download all parcel sources sequentially."""
    results = []
    for source_key, config in PARCEL_SOURCES.items():
        result = await download_parcels(source_key, config, force)
        results.append(result)
        print_result(result)
    return results


async def main():
    parser = argparse.ArgumentParser(
        description="Download parcel data and convert to address points"
    )
    parser.add_argument("--source", help="Source key to download")
    parser.add_argument("--all", action="store_true", help="Download all parcel sources")
    parser.add_argument("--force", action="store_true", help="Force re-download")
    parser.add_argument("--list", action="store_true", help="List available sources")

    args = parser.parse_args()

    if args.list:
        print("Parcel sources (polygon -> address point conversion):")
        for key, config in PARCEL_SOURCES.items():
            print(f"  {key}")
            print(f"    Endpoint: {config['endpoint']}")
            print(f"    Address field: {config.get('address_field', 'N/A')}")
        return

    if args.all:
        results = await download_all(args.force)
        success = sum(1 for r in results if r["success"])
        print(f"\nSummary: {success}/{len(results)} sources downloaded successfully")
        sys.exit(0 if success == len(results) else 1)

    if args.source:
        if args.source not in PARCEL_SOURCES:
            print(f"Error: Unknown source '{args.source}'")
            print(f"Use --list to see available sources")
            sys.exit(1)

        config = PARCEL_SOURCES[args.source]
        result = await download_parcels(args.source, config, args.force)
        print_result(result)
        sys.exit(0 if result["success"] else 1)

    parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())
