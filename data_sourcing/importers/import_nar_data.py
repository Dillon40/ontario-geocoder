#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "asyncpg",
#   "python-dotenv",
# ]
# ///

"""
Import National Address Register (NAR) CSV data into PostgreSQL database.
Processes addresses and locations for Ontario (province code 35).
"""
import sys
import csv
import argparse
import asyncio
from pathlib import Path
from typing import Dict
from dotenv import load_dotenv
import asyncpg

# Support both direct-script and package execution with the locked interpreter.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from data_sourcing.importers.import_tracker import ImportTracker
from data_sourcing.importers.import_utils import FileHashManager, StaleRecordDetector
from data_sourcing.db_utils import get_db_pool

# Load environment variables
load_dotenv()


async def load_locations(file_path: str) -> Dict[str, dict]:
    """Load locations into memory map."""
    print('\nLoading location data...')

    location_map = {}
    line_count = 0
    valid_count = 0

    # StatCan CSVs may start with a BOM; it must not become part of LOC_GUID.
    with open(file_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)

        for row in reader:
            line_count += 1

            try:
                loc_guid = row.get('LOC_GUID')
                lat = row.get('BG_LATITUDE')
                lng = row.get('BG_LONGITUDE')

                if loc_guid and lat and lng and lat != '' and lng != '':
                    location_map[loc_guid] = {
                        'loc_guid': loc_guid,
                        'bg_latitude': float(lat),
                        'bg_longitude': float(lng),
                        'csd_code': int(row['CSD_CODE']) if row.get('CSD_CODE') else None
                    }
                    valid_count += 1

                if line_count % 50000 == 0:
                    print(f'\rProcessed {line_count} location records, valid: {valid_count}', end='')

            except (ValueError, KeyError):
                # Skip malformed lines
                pass

    print(f'\nLoaded {valid_count} locations with coordinates from {line_count} total records')
    return location_map


async def insert_location_batch(batch: list, pool: asyncpg.Pool,
                                import_id: int = None) -> int:
    """Insert/update location batch with UPSERT.

    Args:
        batch: List of location records
        pool: Database connection pool
        import_id: Current import ID for tracking

    Returns:
        Number of records inserted/updated
    """
    if not batch:
        return 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            inserted_count = 0

            for location in batch:
                query = '''
                    INSERT INTO nar_locations (loc_guid, bg_latitude, bg_longitude, csd_code,
                                               last_seen_import_id)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (loc_guid) DO UPDATE SET
                        -- Never let a NULL incoming coordinate wipe an
                        -- existing good one (load_locations filters NULLs,
                        -- but keep the DB-side guard regardless).
                        bg_latitude = COALESCE(EXCLUDED.bg_latitude,
                                               nar_locations.bg_latitude),
                        bg_longitude = COALESCE(EXCLUDED.bg_longitude,
                                                nar_locations.bg_longitude),
                        csd_code = EXCLUDED.csd_code,
                        last_seen_import_id = EXCLUDED.last_seen_import_id,
                        is_deleted = FALSE,
                        deleted_at = NULL
                '''

                await conn.execute(
                    query,
                    location['loc_guid'],
                    location['bg_latitude'],
                    location['bg_longitude'],
                    location['csd_code'],
                    import_id
                )

                inserted_count += 1

            return inserted_count


async def insert_address_batch(batch: list, pool: asyncpg.Pool,
                               import_id: int = None) -> tuple[int, int]:
    """Insert/update address batch with UPSERT.

    Args:
        batch: List of address records
        pool: Database connection pool
        import_id: Current import ID for tracking

    Returns:
        Tuple of actual (inserted, updated) counts, including conflict updates.
    """
    if not batch:
        return 0, 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            inserted_count = 0
            updated_count = 0

            for address in batch:
                query = '''
                    INSERT INTO nar_addresses (
                        loc_guid, addr_guid, civic_no, civic_no_suffix, apt_no_label,
                        official_street_name, official_street_type, official_street_dir,
                        csd_eng_name, prov_code, mail_postal_code, bg_x, bg_y,
                        last_seen_import_id
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                    ON CONFLICT (addr_guid) DO UPDATE SET
                        loc_guid = EXCLUDED.loc_guid,
                        civic_no = EXCLUDED.civic_no,
                        civic_no_suffix = EXCLUDED.civic_no_suffix,
                        apt_no_label = EXCLUDED.apt_no_label,
                        official_street_name = EXCLUDED.official_street_name,
                        official_street_type = EXCLUDED.official_street_type,
                        official_street_dir = EXCLUDED.official_street_dir,
                        csd_eng_name = EXCLUDED.csd_eng_name,
                        prov_code = EXCLUDED.prov_code,
                        mail_postal_code = EXCLUDED.mail_postal_code,
                        -- Recompute derived coordinates only when a new projected
                        -- pair actually changes. Missing coordinates never erase
                        -- existing geocodes; setup's backfill handles invalidation.
                        latitude = CASE WHEN EXCLUDED.bg_x IS NOT NULL AND EXCLUDED.bg_y IS NOT NULL
                            AND (EXCLUDED.bg_x, EXCLUDED.bg_y) IS DISTINCT FROM (nar_addresses.bg_x, nar_addresses.bg_y)
                            THEN NULL ELSE nar_addresses.latitude END,
                        longitude = CASE WHEN EXCLUDED.bg_x IS NOT NULL AND EXCLUDED.bg_y IS NOT NULL
                            AND (EXCLUDED.bg_x, EXCLUDED.bg_y) IS DISTINCT FROM (nar_addresses.bg_x, nar_addresses.bg_y)
                            THEN NULL ELSE nar_addresses.longitude END,
                        -- Coordinate-overwrite protection: a re-import from
                        -- a file without coordinates (e.g. no Location file)
                        -- must never wipe existing good coordinates. Updated
                        -- as a pair so x/y stay consistent.
                        bg_x = CASE
                            WHEN EXCLUDED.bg_x IS NOT NULL
                             AND EXCLUDED.bg_y IS NOT NULL
                            THEN EXCLUDED.bg_x
                            ELSE nar_addresses.bg_x END,
                        bg_y = CASE
                            WHEN EXCLUDED.bg_x IS NOT NULL
                             AND EXCLUDED.bg_y IS NOT NULL
                            THEN EXCLUDED.bg_y
                            ELSE nar_addresses.bg_y END,
                        last_seen_import_id = EXCLUDED.last_seen_import_id,
                        is_deleted = FALSE,
                        deleted_at = NULL
                '''

                created = await conn.fetchval(
                    query + ' RETURNING (xmax = 0)',
                    address['loc_guid'],
                    address['addr_guid'],
                    address['civic_no'],
                    address['civic_no_suffix'],
                    address['apt_no_label'],
                    address['official_street_name'],
                    address['official_street_type'],
                    address['official_street_dir'],
                    address['csd_eng_name'],
                    address['prov_code'],
                    address['mail_postal_code'],
                    address['bg_x'],
                    address['bg_y'],
                    import_id
                )

                inserted_count += int(created)
                updated_count += int(not created)

            return inserted_count, updated_count


async def import_addresses(file_path: str, location_map: Dict[str, dict],
                          province_code: str, batch_size: int, pool: asyncpg.Pool,
                          tracker: ImportTracker = None,
                          stale_detector: StaleRecordDetector = None) -> dict:
    """Import NAR addresses with UPSERT and stale detection.

    Args:
        file_path: Path to Address CSV file
        location_map: Dict mapping loc_guid to location data
        province_code: Province filter (e.g., '35' for Ontario)
        batch_size: Number of records per batch
        pool: Database connection pool
        tracker: ImportTracker for statistics
        stale_detector: StaleRecordDetector for tracking seen records

    Returns:
        Dict with import statistics
    """
    print(f'\nImporting addresses from {file_path}...')
    print(f'Filtering for province code: {province_code} (Ontario)')

    line_count = 0
    batch = []
    batch_addr_guids = []  # Track addr_guids for stale detection
    inserted_count = 0
    updated_count = 0
    skipped_count = 0
    with_coords_count = 0
    import_id = tracker.import_id if tracker else None

    with open(file_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)

        for row in reader:
            line_count += 1

            try:
                # Filter by province
                if row.get('PROV_CODE') != province_code:
                    skipped_count += 1
                    if tracker:
                        tracker.stats.rows_skipped += 1
                    continue

                loc_guid = row.get('LOC_GUID')
                addr_guid = row['ADDR_GUID']

                # Check if we have coordinates for this location
                has_coords = loc_guid in location_map

                # Normalize postal code
                postal_code = row.get('MAIL_POSTAL_CODE')
                if postal_code:
                    postal_code = postal_code.replace(' ', '').upper()

                address = {
                    'loc_guid': loc_guid,
                    'addr_guid': addr_guid,
                    'civic_no': row.get('CIVIC_NO'),
                    'civic_no_suffix': row.get('CIVIC_NO_SUFFIX'),
                    'apt_no_label': row.get('APT_NO_LABEL'),
                    'official_street_name': row.get('OFFICIAL_STREET_NAME'),
                    'official_street_type': row.get('OFFICIAL_STREET_TYPE'),
                    'official_street_dir': row.get('OFFICIAL_STREET_DIR'),
                    'csd_eng_name': row.get('CSD_ENG_NAME'),
                    'prov_code': int(row['PROV_CODE']),
                    'mail_postal_code': postal_code,
                    'bg_x': float(row['BG_X']) if row.get('BG_X') else None,
                    'bg_y': float(row['BG_Y']) if row.get('BG_Y') else None
                }

                batch.append(address)
                batch_addr_guids.append(addr_guid)

                if has_coords:
                    with_coords_count += 1

                # Insert batch when it reaches batch size
                if len(batch) >= batch_size:
                    inserted, updated = await insert_address_batch(batch, pool, import_id)
                    inserted_count += inserted
                    updated_count += updated
                    if tracker:
                        tracker.stats.rows_inserted += inserted
                        tracker.stats.rows_updated += updated
                        tracker.stats.rows_processed += len(batch)

                    # Mark addr_guids as seen for stale detection
                    if stale_detector:
                        for guid in batch_addr_guids:
                            stale_detector.mark_seen(guid)

                    batch = []
                    batch_addr_guids = []

                    print(f'\rProcessed: {line_count}, Inserted: {inserted_count}, '
                          f'With coords: {with_coords_count}, Skipped: {skipped_count}', end='')

            except (ValueError, KeyError) as e:
                # Log parsing errors
                skipped_count += 1
                if tracker:
                    await tracker.log_row_error(line_count, 'parse_error', str(e))

        # Insert remaining records
        if batch:
            inserted, updated = await insert_address_batch(batch, pool, import_id)
            inserted_count += inserted
            updated_count += updated
            # Mark remaining addr_guids as seen
            if stale_detector:
                for guid in batch_addr_guids:
                    stale_detector.mark_seen(guid)
            if tracker:
                tracker.stats.rows_inserted += inserted
                tracker.stats.rows_updated += updated
                tracker.stats.rows_processed += len(batch)

    print(f'\n\nAddress import complete!')
    print(f'Total processed: {line_count}')
    print(f'Successfully inserted: {inserted_count}; updated: {updated_count}')
    print(f'Addresses with coordinates: {with_coords_count}')
    print(f'Skipped: {skipped_count}')

    return {
        'inserted_count': inserted_count,
        'updated_count': updated_count,
        'with_coords_count': with_coords_count
    }


async def main():
    """Main execution."""
    parser = argparse.ArgumentParser(
        description='Import NAR CSV data into PostgreSQL database'
    )
    parser.add_argument('--addresses', required=True, help='Path to Address_10.csv file')
    parser.add_argument('--locations', required=False, default=None,
                       help='Path to Location CSV file (optional — addresses imported without coords if omitted)')
    parser.add_argument('--province', default='35', help='Province code (default: 35 - Ontario)')
    parser.add_argument('--batch-size', type=int, default=5000, help='Batch size (default: 5000)')
    parser.add_argument('--force', action='store_true',
                       help='Force import even if file hash unchanged from last import')
    parser.add_argument('--stale-strategy', choices=['soft_delete', 'report_only', 'skip'],
                       default='soft_delete',
                       help='How to handle stale records (default: soft_delete)')

    args = parser.parse_args()

    try:
        print('\n' + '=' * 60)
        print('NAR Data Import')
        print('=' * 60)

        # Connect to database
        pool = await get_db_pool(min_size=1, max_size=10)

        try:
            # Check file hash and skip if unchanged (unless force)
            if not args.force:
                should_skip, reason = await FileHashManager.should_skip_file(
                    pool, 'nar_ontario', args.addresses)
                if should_skip:
                    print(f'\n⏭️  Skipping import: {reason}')
                    print('   Use --force to override file hash check.')
                    await pool.close()
                    return

            # Use ImportTracker to record this import operation
            async with ImportTracker(
                pool,
                'nar_ontario',
                args.addresses,
                'full',
                {'locations_file': args.locations, 'province_code': args.province}
            ) as tracker:
                # Initialize stale record detectors
                addr_stale_detector = None
                loc_stale_detector = None
                if args.stale_strategy != 'skip':
                    addr_stale_detector = StaleRecordDetector(
                        pool, tracker.import_id, 'nar_ontario',
                        'nar_addresses', 'addr_guid'
                    )
                    loc_stale_detector = StaleRecordDetector(
                        pool, tracker.import_id, 'nar_ontario',
                        'nar_locations', 'loc_guid'
                    )
                    print(f'Stale record detection: {args.stale_strategy}')

                # Step 1: Load locations (if file provided)
                inserted_locations = 0
                if args.locations:
                    location_map = await load_locations(args.locations)

                    # Step 2: Insert locations into database
                    print('\nInserting locations into database...')
                    location_batch = list(location_map.values())

                    for i in range(0, len(location_batch), args.batch_size):
                        chunk = location_batch[i:i + args.batch_size]
                        inserted = await insert_location_batch(chunk, pool, tracker.import_id)
                        inserted_locations += inserted

                        # Mark loc_guids as seen for stale detection
                        if loc_stale_detector:
                            for loc in chunk:
                                loc_stale_detector.mark_seen(loc['loc_guid'])

                        print(f'\rInserted {inserted_locations}/{len(location_batch)} locations', end='')

                    print(f'\nLocation import complete: {inserted_locations} records')
                else:
                    location_map = {}
                    print('\nNo locations file — importing addresses without coordinates')

                # Step 3: Import addresses (with tracker and stale detector)
                address_stats = await import_addresses(
                    args.addresses,
                    location_map,
                    args.province,
                    args.batch_size,
                    pool,
                    tracker,
                    addr_stale_detector
                )

                if tracker.stats.rows_failed:
                    raise RuntimeError(f'{tracker.stats.rows_failed} rows failed; see import_row_errors')

                # Step 4: Handle stale records
                if addr_stale_detector:
                    print(f'\nDetecting stale records...')

                    # Detect stale addresses
                    stale_addr_ids = await addr_stale_detector.detect_stale_records()
                    if stale_addr_ids:
                        print(f'Found {len(stale_addr_ids):,} stale addresses')
                        if args.stale_strategy == 'soft_delete':
                            deleted = await addr_stale_detector.soft_delete_stale_records(stale_addr_ids)
                            tracker.stats.rows_deleted += deleted
                            print(f'Soft-deleted {deleted:,} stale addresses')
                        elif args.stale_strategy == 'report_only':
                            await addr_stale_detector.report_stale_records(stale_addr_ids, limit=10)

                    # Detect stale locations (only if locations were imported)
                    stale_loc_ids = await loc_stale_detector.detect_stale_records() if args.locations else []
                    if stale_loc_ids:
                        print(f'Found {len(stale_loc_ids):,} stale locations')
                        if args.stale_strategy == 'soft_delete':
                            deleted = await loc_stale_detector.soft_delete_stale_records(stale_loc_ids)
                            print(f'Soft-deleted {deleted:,} stale locations')
                        elif args.stale_strategy == 'report_only':
                            await loc_stale_detector.report_stale_records(stale_loc_ids, limit=10)

                    if not stale_addr_ids and not stale_loc_ids:
                        print('No stale records found')

                print('\n' + '=' * 60)
                print('NAR Import Summary')
                print('=' * 60)
                print(f'Locations imported: {inserted_locations}')
                total_upserted = address_stats['inserted_count'] + address_stats['updated_count']
                print(f'Addresses upserted: {total_upserted} (inserted {address_stats["inserted_count"]}, updated {address_stats["updated_count"]})')
                print(f'Addresses with location-file coordinates: {address_stats["with_coords_count"]}')
                if total_upserted > 0:
                    coverage = round((address_stats['with_coords_count'] / total_upserted) * 100)
                    print(f'Location-file coordinate coverage: {coverage}% (projected-address backfill runs in setup)')
                if tracker.stats.rows_deleted > 0:
                    print(f'Stale records deleted: {tracker.stats.rows_deleted}')
                print(f'Import tracked with ID: {tracker.import_id}')
                print(f'\nFinal Import Statistics:')
                print(f'  {tracker.stats.summary()}')
                print('=' * 60 + '\n')

        finally:
            await pool.close()

    except Exception as error:
        print(f'\nNAR import failed: {error}')
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())
