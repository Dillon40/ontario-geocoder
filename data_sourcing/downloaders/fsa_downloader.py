#!/usr/bin/env python3
"""2021 digital FSA boundaries from Statistics Canada's public ArcGIS service.

This is the same digital boundary product as lfsa000a21a_e.zip, not a third-party
mirror or a cartographic/generalized substitute. The ZIP host requires browser
security verification on some networks; the official query service is public.
GeoJSON preserves full geometry and is read directly by the boundary importer.
"""
import argparse
import asyncio
import sys
try:
    from .arcgis_downloader import download_arcgis
    from .download_utils import print_result
except ImportError:
    from arcgis_downloader import download_arcgis
    from download_utils import print_result

SOURCE_KEY = 'fsa_boundaries'
FSA_URL = 'https://geo.statcan.gc.ca/geo_wa/rest/services/2021/Digital_boundary_files/MapServer/14'

async def download_fsa(force: bool = False) -> dict:
    return await download_arcgis(SOURCE_KEY, FSA_URL, force)

async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    result = await download_fsa(args.force)
    print_result(result)
    return 0 if result['success'] else 1

if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
