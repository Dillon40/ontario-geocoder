#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "asyncpg>=0.29.0",
# ]
# ///
"""
Add latitude/longitude to nar_addresses

Populates the latitude/longitude columns (added in migration 30) on
nar_addresses from the projected bg_x/bg_y coordinates.

Why this exists:
  Tier 3 (NAR) geocoding never matched anything. The layered matcher joined
  nar_locations to read bg_latitude/bg_longitude, but nar_locations is empty
  and nar_addresses.loc_guid is NULL on every row, so every NAR candidate was
  filtered out. The real coordinates live in nar_addresses.bg_x/bg_y.

IMPORTANT — coordinate system:
  bg_x/bg_y are NOT lon/lat degrees. They are projected coordinates in
  EPSG:3347 (NAD83 / Statistics Canada Lambert), the native CRS of the
  StatCan National Address Register. They are transformed to EPSG:4326
  (WGS84 lon/lat degrees) inside the UPDATE:

      ST_Transform(ST_SetSRID(ST_MakePoint(bg_x::float8, bg_y::float8),
                              3347), 4326)

  latitude  = ST_Y(point)   longitude = ST_X(point)

  Verified empirically: 55 Glen Acres, Huntsville -> (45.3443, -79.2545),
  and a prior enrichment spot-check found 97.4% postal agreement.

Updates run in modest id-range batches, each in its own transaction, so we
never hold a single 5.4M-row transaction (long lock hold, WAL/IO spike, bloat).

Idempotent: only rows WHERE latitude IS NULL AND bg_x IS NOT NULL are touched,
so a re-run resumes where an interrupted run left off.

Usage:
    # Dry run: report projected coverage, write nothing
    ./add_latlong_to_nar.py --dry-run

    # Real run
    ./add_latlong_to_nar.py
"""

import asyncio
import asyncpg
import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

# Add project root to path for db_utils import
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from data_sourcing.db_utils import get_db_config

NAR_SRID = 3347          # NAD83 / Statistics Canada Lambert (native NAR CRS)

# Ontario bounding box in lon/lat, used as a startup sanity check that the
# SRID assumption for bg_x/bg_y is correct (the NAR load here is Ontario-only,
# prov_code = 35). A transformed point outside this box means bg_x/bg_y are
# not EPSG:3347 or x/y are swapped -> abort rather than write garbage.
ONTARIO_LON = (-96.0, -73.0)
ONTARIO_LAT = (41.0, 57.0)


def make_point_sql(x_expr: str = 'bg_x', y_expr: str = 'bg_y') -> str:
    """SQL fragment: bg_x/bg_y (EPSG:3347) -> 4326 point."""
    return (
        f"ST_Transform(ST_SetSRID(ST_MakePoint({x_expr}::float8, "
        f"{y_expr}::float8), {NAR_SRID}), 4326)"
    )


async def sanity_check_srid(conn: asyncpg.Connection) -> None:
    """Transform ~25 sample points and confirm they land inside Ontario."""
    rows = await conn.fetch(f"""
        SELECT ST_X(pt) AS lon, ST_Y(pt) AS lat
        FROM (
            SELECT {make_point_sql('n.bg_x', 'n.bg_y')} AS pt
            FROM nar_addresses n
            WHERE n.bg_x IS NOT NULL AND n.bg_y IS NOT NULL
            LIMIT 25
        ) s
    """)
    if not rows:
        raise SystemExit("SRID sanity check: no rows with coordinates found")
    for r in rows:
        if not (ONTARIO_LON[0] <= r['lon'] <= ONTARIO_LON[1]
                and ONTARIO_LAT[0] <= r['lat'] <= ONTARIO_LAT[1]):
            raise SystemExit(
                f"SRID sanity check FAILED: transformed point "
                f"({r['lon']:.4f}, {r['lat']:.4f}) is outside Ontario bounds "
                f"lon {ONTARIO_LON} / lat {ONTARIO_LAT}. bg_x/bg_y may not be "
                f"EPSG:{NAR_SRID}, or x/y are swapped. Aborting."
            )
    print(f"✓ SRID sanity check passed: {len(rows)} sample points transform "
          f"from EPSG:{NAR_SRID} into plausible Ontario lon/lat")


async def dry_run(pool: asyncpg.Pool) -> None:
    """Report projected coverage without writing anything."""
    async with pool.acquire() as conn:
        print("Counting projected coverage (read-only)...")
        row = await conn.fetchrow("""
            SELECT
                count(*) AS total,
                count(*) FILTER (WHERE latitude IS NOT NULL) AS already_set,
                count(*) FILTER (
                    WHERE latitude IS NULL
                      AND bg_x IS NOT NULL AND bg_y IS NOT NULL
                ) AS fillable,
                count(*) FILTER (
                    WHERE latitude IS NULL
                      AND (bg_x IS NULL OR bg_y IS NULL)
                ) AS no_coords
            FROM nar_addresses
        """)
    total = row['total']
    projected = row['already_set'] + row['fillable']
    print()
    print("=" * 70)
    print("DRY RUN — PROJECTED COVERAGE")
    print("=" * 70)
    print(f"Total rows:                    {total:>12,}")
    print(f"Already has latitude:          {row['already_set']:>12,}")
    print(f"Fillable (lat NULL + coords):  {row['fillable']:>12,}")
    print(f"No coords (stays NULL):        {row['no_coords']:>12,}")
    print("-" * 70)
    print(f"Projected coverage:            {projected:>12,} "
          f"({projected / total * 100:.1f}%)")


async def run_backfill(pool: asyncpg.Pool, batch_size: int) -> int:
    """Batched set-based UPDATE deriving latitude/longitude from bg_x/bg_y."""
    async with pool.acquire() as conn:
        lo, hi = await conn.fetchrow("SELECT min(id), max(id) FROM nar_addresses")
    if lo is None:
        print("  nar_addresses is empty; nothing to do")
        return 0

    total_updated = 0
    batch_start = lo
    while batch_start <= hi:
        batch_end = batch_start + batch_size
        t0 = time.monotonic()
        async with pool.acquire() as conn:
            result = await conn.execute(f"""
                UPDATE nar_addresses n
                SET latitude = ST_Y(p.pt),
                    longitude = ST_X(p.pt)
                FROM (
                    SELECT id, {make_point_sql('bg_x', 'bg_y')} AS pt
                    FROM nar_addresses
                    WHERE id >= $1 AND id < $2
                      AND latitude IS NULL
                      AND bg_x IS NOT NULL
                      AND bg_y IS NOT NULL
                ) p
                WHERE n.id = p.id
            """, batch_start, batch_end)
        n = int(result.split()[-1])
        total_updated += n
        pct = min(100.0, (batch_end - lo) / (hi - lo + 1) * 100)
        print(f"  ids [{batch_start:,} .. {batch_end:,}) "
              f"updated {n:>7,}  ({pct:5.1f}% scanned, "
              f"{time.monotonic() - t0:.1f}s)")
        batch_start = batch_end
    return total_updated


async def report_final_stats(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT
                count(*) AS total,
                count(*) FILTER (WHERE latitude IS NOT NULL) AS covered,
                count(*) FILTER (WHERE latitude IS NULL) AS still_null,
                min(latitude) AS min_lat, max(latitude) AS max_lat,
                min(longitude) AS min_lon, max(longitude) AS max_lon
            FROM nar_addresses
        """)
    print()
    print("=" * 70)
    print("FINAL COVERAGE")
    print("=" * 70)
    print(f"Total rows:            {row['total']:>12,}")
    print(f"latitude populated:    {row['covered']:>12,} "
          f"({row['covered'] / row['total'] * 100:.1f}%)")
    print(f"Still NULL:            {row['still_null']:>12,} "
          f"({row['still_null'] / row['total'] * 100:.1f}%)")
    print(f"lat range:  {float(row['min_lat']):.4f} .. {float(row['max_lat']):.4f}")
    print(f"lon range:  {float(row['min_lon']):.4f} .. {float(row['max_lon']):.4f}")


async def main():
    parser = argparse.ArgumentParser(
        description='Populate nar_addresses.latitude/longitude from bg_x/bg_y'
    )
    parser.add_argument('--db', default='geocoder',
                        help='Target database name (default: geocoder)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Report projected coverage without writing')
    parser.add_argument('--batch-size', type=int, default=250_000,
                        help='Id-range width per batch (default: 250000)')
    parser.add_argument('--no-analyze', action='store_true',
                        help='Skip ANALYZE nar_addresses after the backfill')
    args = parser.parse_args()

    print("=" * 70)
    print("ADD LATITUDE/LONGITUDE TO nar_addresses")
    print("=" * 70)
    print(f"Started:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Database: {args.db}")
    print(f"Mode:     {'DRY RUN' if args.dry_run else 'LIVE'}")
    print()

    db_config = get_db_config()
    db_config['database'] = args.db  # allow --db to override the configured database
    pool = await asyncpg.create_pool(
        host=db_config['host'],
        port=db_config['port'],
        database=db_config['database'],
        user=db_config['user'],
        password=db_config['password'],
        min_size=1, max_size=3,
        command_timeout=1800)
    t_start = time.monotonic()
    try:
        async with pool.acquire() as conn:
            await sanity_check_srid(conn)
        print()

        if args.dry_run:
            await dry_run(pool)
            return

        print("Backfilling latitude/longitude from bg_x/bg_y (batched UPDATE)...")
        n = await run_backfill(pool, args.batch_size)
        print(f"✓ Backfill complete: {n:,} rows updated")

        if not args.no_analyze:
            print()
            print("Running ANALYZE nar_addresses...")
            async with pool.acquire() as conn:
                await conn.execute("ANALYZE nar_addresses")
            print("✓ ANALYZE complete")

        await report_final_stats(pool)
    finally:
        await pool.close()
        elapsed = time.monotonic() - t_start
        print()
        print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
              f"(elapsed {elapsed / 60:.1f} min)")


if __name__ == '__main__':
    asyncio.run(main())
