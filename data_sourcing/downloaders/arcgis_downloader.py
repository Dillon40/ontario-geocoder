#!/usr/bin/env python3
"""
Standard ArcGIS downloader for 34 Ontario municipal data sources.

Simple, single-purpose: paginate by OID, stream to GeoJSON file.
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
    get_adaptive_timeout,
    get_resume_oid,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Standard ArcGIS sources (34 total, excludes Hamilton which needs rate limiting)
STANDARD_SOURCES = {
    "barrie_addresses": "https://gispublic.barrie.ca/arcgis/rest/services/Open_Data/AddressVW/MapServer/0",
    "brantford_addresses": "https://services.arcgis.com/aji2lCmjXt0KpGzI/arcgis/rest/services/OP_Site_Addresses2/FeatureServer/0",
    "burlington_addresses": "https://mapping.burlington.ca/arcgisweb/rest/services/COB/AddressPoints/MapServer/0",
    "cochrane_addresses": "https://services5.arcgis.com/M1SNYuFIW9v2gSO7/arcgis/rest/services/Address_Points/FeatureServer/4",
    "cornwall": "https://services2.arcgis.com/tPtW2sjE3XOR63A7/arcgis/rest/services/AddressPoints/FeatureServer/34",
    "dufferin_county_addresses": "https://services5.arcgis.com/C0BEdgagVaXzcw0W/arcgis/rest/services/Address_Points_DufferinCounty_Open_Data/FeatureServer/0",
    "durham_region_addresses": "https://maps.durham.ca/arcgis/rest/services/Open_Data/Durham_OpenData/MapServer/0",
    "elgin_county_addresses": "https://services6.arcgis.com/TCQ78XR5HqDZ1SDc/arcgis/rest/services/Elgin_Civic_Numbers_2026/FeatureServer/0",
    "essex_county_tecumseh": "https://gisweb.tecumseh.ca/arcgis/rest/services/OpenDataMaps/OpenDataMAP_InfraZonePropBound_Layers/MapServer/33",
    "frontenac_county": "https://services2.arcgis.com/vJlmDE1PmdTcJF0N/arcgis/rest/services/Civic_Address_Points_View_2/FeatureServer/0",
    "greater_sudbury": "https://services.arcgis.com/q3mIlR87lZlZsds3/arcgis/rest/services/Address_and_Building_Roofline/FeatureServer/2",
    "guelph_addresses": "https://gismaps.guelph.ca/hosting/rest/services/AddressPoint/FeatureServer/0",
    "haldimand_county_addresses": "https://gis.haldimandcounty.ca/server/rest/services/Topo/ParcelsOnlinePublic/FeatureServer/0",
    "hastings_county": "https://services1.arcgis.com/1xcPmb5WGexkkJ9w/arcgis/rest/services/CivicAdd_OpenData/FeatureServer/0",
    "huron_county": "https://services1.arcgis.com/Nl1VlZHemqspPW1n/arcgis/rest/services/Municipal_911_Address/FeatureServer/0",
    "kawartha_lakes_addresses": "https://services3.arcgis.com/RQBDTPtsbT0jebs7/arcgis/rest/services/CKL_Address_Point_View/FeatureServer/0",
    "kingston_addresses": "https://utility.arcgis.com/usrsvcs/servers/d76e1fe447e84cb1983c76e1cb9dadb4/rest/services/Planning/Civic_Address_Point_Public/FeatureServer/0",
    "leeds_and_grenville": "https://services3.arcgis.com/KQcdsE7S72bcD7R8/arcgis/rest/services/LG_CivicAddress_OpenData/FeatureServer/0",
    "london_addresses": "https://maps.london.ca/server/rest/services/OpenData/OpenData_Community/MapServer/0",
    "milton_addresses": "https://api.milton.ca/arcgis/rest/services/Datasets/Address_Pts/MapServer/0",
    "muskoka_addresses": "https://utility.arcgis.com/usrsvcs/servers/7dbdeb9bc94b45d4a3296f73fba38a3c/rest/services/Addressing/Addressing_Service_Public/FeatureServer/0",
    "niagara_region_addresses": "https://services1.arcgis.com/WxiLK82TWf8W3O3f/arcgis/rest/services/OpenData_Address_Points/FeatureServer/43",
    "norfolk_county": "https://services1.arcgis.com/mSGV2hCLXHNsfQqK/arcgis/rest/services/Civic_Addresses/FeatureServer/0",
    "oakville_addresses": "https://maps.oakville.ca/oakgis/rest/services/ArcGISOnline/AGO_Oakville_Misc_Layers/MapServer/15",
    "ontario_municipal_boundaries": "https://ws.lioservices.lrc.gov.on.ca/arcgis2/rest/services/LIO_OPEN_DATA/LIO_Open03/MapServer/14",
    "ottawa_addresses": "https://maps.ottawa.ca/arcgis/rest/services/Municipal_Address/MapServer/0",
    "parry_sound_district_addresses": "https://www.wpsgn.ca/s22/rest/services/NG911/NG911/FeatureServer/9112",
    "peel_region_addresses": "https://services6.arcgis.com/ONZht79c8QWuX759/arcgis/rest/services/Address_Points/FeatureServer/0",
    "peterborough": "https://services1.arcgis.com/JOTjMnANji9YTrZ2/arcgis/rest/services/Address/FeatureServer/0",
    "sarnia": "https://services1.arcgis.com/ICybsLmBXrZCZV3x/arcgis/rest/services/Addresses_Open_Data_AGOL/FeatureServer/0",
    "thunder_bay": "https://services5.arcgis.com/h9xShea49ZANgOtx/arcgis/rest/services/Municipal_Address_Feature_Layer/FeatureServer/0",
    "waterloo_region_addresses": "https://utility.arcgis.com/usrsvcs/servers/d4d0c6dedec14e59930688f3830776c6/rest/services/OpenData/OpenData/MapServer/1",
    "windsor_addresses": "https://mappmycity.ca/arcgis/rest/services/open_data_services/propertyparcels/MapServer/0",
    "york_region_addresses": "https://ww8.yorkmaps.ca/arcgis/rest/services/OpenData/Location/MapServer/0",
}


async def download_arcgis(source_key: str, endpoint: str, force: bool = False) -> dict:
    """
    Download from ArcGIS FeatureServer/MapServer with retry and resume support.

    Returns: {"success": bool, "source_key": str, "records": int, "path": str, "error": str|None}
    """
    logger.info(f"[{source_key}] Starting download from {endpoint}")

    final_path = ensure_output_dir(source_key)
    output_path = final_path.with_suffix('.geojson.part')

    # Use a temporary client to get server info for adaptive timeout
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=30.0)) as probe_client:
        try:
            info = await get_server_info(endpoint, probe_client)
        except Exception as e:
            logger.exception(f"[{source_key}] Failed to get server info")
            return make_result(False, source_key, error=str(e))

    oid_field = info["oid_field"]
    max_record_count = info["max_record_count"]
    # Polygon vertices, not feature count, dominate response size. The
    # advertised limit is unsafe for province-wide detailed boundary layers.
    page_size = min(max_record_count, 5) if source_key in (
        'fsa_boundaries', 'ontario_municipal_boundaries') else max_record_count
    output_format = "geojson" if info["supports_geojson"] else "json"
    timeout = get_adaptive_timeout(max_record_count)

    logger.info(f"[{source_key}] OID field: {oid_field}, page size: {page_size}, "
                f"format: {output_format}, read timeout: {timeout.read}s")

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            # Step 1: Get total count
            total_count = await get_record_count(endpoint, client)
            logger.info(f"[{source_key}] Total records: {total_count:,}")

            # Restart into staging; incomplete GeoJSON is not resumable.
            last_oid = 0
            records_written = 0
            is_first = True
            resuming = False

            # Replace the previous download only after validating the record count.

            actual_fields = set()

            # Open file in append mode if resuming, write mode otherwise
            file_mode = "a" if resuming else "w"
            with open(output_path, file_mode) as f:
                if not resuming:
                    stream_geojson_start(f)
                else:
                    is_first = False  # Not the first feature when resuming

                while True:
                    features, new_last_oid = await fetch_arcgis_page_with_retry(
                        endpoint, oid_field, last_oid, page_size,
                        output_format, client, source_key=source_key,
                    )

                    if not features:
                        break

                    for feature in features:
                        stream_geojson_feature(f, feature, is_first)
                        is_first = False
                        records_written += 1

                        if records_written <= 10:
                            actual_fields.update(feature.get("properties", {}).keys())

                    if new_last_oid <= last_oid:
                        raise RuntimeError('ArcGIS pagination did not advance')
                    last_oid = new_last_oid

                    if records_written % 10000 == 0:
                        pct = (records_written / total_count * 100) if total_count else 0
                        logger.info(f"[{source_key}] Progress: {records_written:,} / {total_count:,} ({pct:.0f}%)")

                stream_geojson_end(f)

            if records_written != total_count or not records_written:
                raise RuntimeError(f'Incomplete download: {records_written}/{total_count} records')
            output_path.replace(final_path)
            # Step 3: Validate fields
            validate_fields(source_key, actual_fields)

            logger.info(f"[{source_key}] Complete: {records_written:,} records")
            return make_result(True, source_key, records_written, final_path)

        except Exception as e:
            logger.exception(f"[{source_key}] Download failed")
            return make_result(False, source_key, error=str(e))


async def download_all(force: bool = False) -> list[dict]:
    """Download all standard ArcGIS sources sequentially."""
    results = []
    for source_key, endpoint in STANDARD_SOURCES.items():
        result = await download_arcgis(source_key, endpoint, force)
        results.append(result)
        print_result(result)
    return results


async def main():
    parser = argparse.ArgumentParser(description="Download ArcGIS data sources")
    parser.add_argument("--source", help="Source key to download")
    parser.add_argument("--all", action="store_true", help="Download all standard sources")
    parser.add_argument("--force", action="store_true", help="Force re-download")
    parser.add_argument("--list", action="store_true", help="List available sources")

    args = parser.parse_args()

    if args.list:
        print("Standard ArcGIS sources:")
        for key in sorted(STANDARD_SOURCES.keys()):
            print(f"  {key}")
        return

    if args.all:
        results = await download_all(args.force)
        success = sum(1 for r in results if r["success"])
        print(f"\nSummary: {success}/{len(results)} sources downloaded successfully")
        sys.exit(0 if success == len(results) else 1)

    if args.source:
        if args.source not in STANDARD_SOURCES:
            print(f"Error: Unknown source '{args.source}'")
            print(f"Use --list to see available sources")
            sys.exit(1)

        endpoint = STANDARD_SOURCES[args.source]
        result = await download_arcgis(args.source, endpoint, args.force)
        print_result(result)
        sys.exit(0 if result["success"] else 1)

    parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())
