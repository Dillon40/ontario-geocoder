#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "asyncpg>=0.29.0",
# ]
# ///
"""
Add FSA Codes to nar_addresses

Populates the fsa_code column (added in migration 5) on the nar_addresses table
in two passes:

  Pass 1 (postal): fsa_code = first 3 chars of mail_postal_code, uppercased,
          validated against the Canadian FSA format (letter-digit-letter).
          Set-based batched UPDATE — no row-by-row round trips.

  Pass 2 (spatial): for rows still NULL that have coordinates, reverse-geocode
          bg_x/bg_y against fsa_boundaries with ST_Contains().

IMPORTANT — coordinate system:
  bg_x/bg_y are NOT lon/lat degrees. They are projected coordinates in
  EPSG:3347 (NAD83 / Statistics Canada Lambert), the native CRS of the
  StatCan National Address Register. They are transformed to EPSG:4326
  (the SRID of fsa_boundaries.geometry) inside the spatial query.
  Verified empirically: 15/15 sampled rows land in the FSA polygon matching
  their own postal code after ST_Transform(..., 3347 -> 4326).

Updates run in modest id-range batches, each in its own transaction, so we
never hold a single 6M-row transaction (long lock hold, WAL/IO spike, bloat).

Usage:
    # Dry run: report projected coverage, write nothing
    ./add_fsa_to_nar.py --db geocoder_verify --dry-run

    # Real run
    ./add_fsa_to_nar.py --db geocoder_verify

    # Spot check only (postal-derived vs spatial FSA agreement on N rows)
    ./add_fsa_to_nar.py --db geocoder_verify --spot-check-only
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
FSA_REGEX = '^[A-Z][0-9][A-Z]$'

# Rough bounding box for Canada in lon/lat, used as a startup sanity check
# that the SRID assumption for bg_x/bg_y is correct.
CANADA_LON = (-141.5, -52.0)
CANADA_LAT = (41.0, 84.0)


def make_point_sql(x_expr: str = 'n.bg_x', y_expr: str = 'n.bg_y') -> str:
    """SQL fragment: bg_x/bg_y (EPSG:3347) -> 4326 point."""
    return (
        f"ST_Transform(ST_SetSRID(ST_MakePoint({x_expr}::float8, "
        f"{y_expr}::float8), {NAR_SRID}), 4326)"
    )


async def sanity_check_srid(conn: asyncpg.Connection) -> None:
    """Transform a handful of sample points and confirm they land inside Canada."""
    rows = await conn.fetch(f"""
        SELECT ST_X(pt) AS lon, ST_Y(pt) AS lat
        FROM (
            SELECT {make_point_sql()} AS pt
            FROM nar_addresses n
            WHERE n.bg_x IS NOT NULL AND n.bg_y IS NOT NULL
            LIMIT 25
        ) s
    """)
    if not rows:
        raise SystemExit("SRID sanity check: no rows with coordinates found")
    for r in rows:
        if not (CANADA_LON[0] <= r['lon'] <= CANADA_LON[1]
                and CANADA_LAT[0] <= r['lat'] <= CANADA_LAT[1]):
            raise SystemExit(
                f"SRID sanity check FAILED: transformed point "
                f"({r['lon']:.4f}, {r['lat']:.4f}) is outside Canada. "
                f"bg_x/bg_y may not be EPSG:{NAR_SRID}, or x/y are swapped."
            )
    print(f"✓ SRID sanity check passed: {len(rows)} sample points transform "
          f"from EPSG:{NAR_SRID} into plausible Canadian lon/lat")


async def dry_run(pool: asyncpg.Pool, spatial_sample: int) -> None:
    """Report projected coverage without writing anything."""
    async with pool.acquire() as conn:
        print("Counting projected coverage (read-only)...")
        row = await conn.fetchrow(f"""
            SELECT
                count(*) AS total,
                count(*) FILTER (WHERE fsa_code IS NOT NULL) AS already_set,
                count(*) FILTER (
                    WHERE fsa_code IS NULL
                      AND upper(left(mail_postal_code, 3)) ~ '{FSA_REGEX}'
                ) AS postal_derivable,
                count(*) FILTER (
                    WHERE fsa_code IS NULL
                      AND NOT (upper(left(coalesce(mail_postal_code, ''), 3)) ~ '{FSA_REGEX}')
                      AND bg_x IS NOT NULL AND bg_y IS NOT NULL
                ) AS spatial_candidates,
                count(*) FILTER (
                    WHERE fsa_code IS NULL
                      AND NOT (upper(left(coalesce(mail_postal_code, ''), 3)) ~ '{FSA_REGEX}')
                      AND (bg_x IS NULL OR bg_y IS NULL)
                ) AS no_data
            FROM nar_addresses
        """)

        # Estimate the spatial hit rate on a random sample of candidates
        # (some points fall outside every FSA polygon: bad coords, water, etc.)
        hit = await conn.fetchrow(f"""
            WITH sample AS (
                SELECT n.bg_x, n.bg_y
                FROM nar_addresses n
                WHERE n.fsa_code IS NULL
                  AND NOT (upper(left(coalesce(n.mail_postal_code, ''), 3)) ~ '{FSA_REGEX}')
                  AND n.bg_x IS NOT NULL AND n.bg_y IS NOT NULL
                ORDER BY random()
                LIMIT {spatial_sample}
            )
            SELECT count(*) AS sampled,
                   count(*) FILTER (WHERE EXISTS (
                       SELECT 1 FROM fsa_boundaries f
                       WHERE ST_Contains(f.geometry, {make_point_sql('n.bg_x', 'n.bg_y')})
                   )) AS hits
            FROM sample n
        """)

    total = row['total']
    hit_rate = (hit['hits'] / hit['sampled']) if hit['sampled'] else 0.0
    projected_spatial = round(row['spatial_candidates'] * hit_rate)
    projected_covered = row['already_set'] + row['postal_derivable'] + projected_spatial

    print()
    print("=" * 70)
    print("DRY RUN — PROJECTED COVERAGE")
    print("=" * 70)
    print(f"Total rows:                      {total:>12,}")
    print(f"Already has fsa_code:            {row['already_set']:>12,}")
    print(f"Pass 1 — postal-derivable:       {row['postal_derivable']:>12,}")
    print(f"Pass 2 — spatial candidates:     {row['spatial_candidates']:>12,}")
    print(f"    sampled hit rate:            {hit_rate * 100:>11.1f}%  "
          f"({hit['hits']:,}/{hit['sampled']:,})")
    print(f"    projected spatial assigns:   {projected_spatial:>12,}")
    print(f"No postal + no coords (stays NULL): {row['no_data']:>9,}")
    print("-" * 70)
    print(f"Bucket sum (postal + spatial-cand + no-data + already): "
          f"{row['postal_derivable'] + row['spatial_candidates'] + row['no_data'] + row['already_set']:,}")
    print(f"Projected coverage:              {projected_covered:>12,} "
          f"({projected_covered / total * 100:.1f}%)")
    print(f"Projected still NULL:            {total - projected_covered:>12,} "
          f"({(total - projected_covered) / total * 100:.1f}%)")


async def run_pass1_postal(pool: asyncpg.Pool, batch_size: int) -> int:
    """Batched set-based UPDATE from mail_postal_code."""
    async with pool.acquire() as conn:
        lo, hi = await conn.fetchrow("SELECT min(id), max(id) FROM nar_addresses")
        total_updated = 0
        batch_start = lo
        while batch_start <= hi:
            batch_end = batch_start + batch_size
            t0 = time.monotonic()
            result = await conn.execute(f"""
                UPDATE nar_addresses
                SET fsa_code = upper(left(mail_postal_code, 3)),
                    enriched_from_source = 'postal_code',
                    enrichment_date = now()
                WHERE id >= $1 AND id < $2
                  AND fsa_code IS NULL
                  AND upper(left(mail_postal_code, 3)) ~ '{FSA_REGEX}'
            """, batch_start, batch_end)
            n = int(result.split()[-1])
            total_updated += n
            pct = min(100.0, (batch_end - lo) / (hi - lo + 1) * 100)
            print(f"  pass1 ids [{batch_start:,} .. {batch_end:,}) "
                  f"updated {n:>7,}  ({pct:5.1f}% scanned, "
                  f"{time.monotonic() - t0:.1f}s)")
            batch_start = batch_end
        return total_updated


async def run_pass2_spatial(pool: asyncpg.Pool, batch_size: int) -> int:
    """Batched spatial UPDATE joining fsa_boundaries via ST_Contains."""
    async with pool.acquire() as conn:
        lo, hi = await conn.fetchrow("SELECT min(id), max(id) FROM nar_addresses")
        total_updated = 0
        batch_start = lo
        while batch_start <= hi:
            batch_end = batch_start + batch_size
            t0 = time.monotonic()
            result = await conn.execute(f"""
                UPDATE nar_addresses n
                SET fsa_code = f.fsa_code,
                    enriched_from_source = 'spatial_fsa',
                    enrichment_date = now()
                FROM fsa_boundaries f
                WHERE n.id >= $1 AND n.id < $2
                  AND n.fsa_code IS NULL
                  AND n.bg_x IS NOT NULL AND n.bg_y IS NOT NULL
                  AND ST_Contains(f.geometry, {make_point_sql()})
            """, batch_start, batch_end)
            n = int(result.split()[-1])
            total_updated += n
            pct = min(100.0, (batch_end - lo) / (hi - lo + 1) * 100)
            print(f"  pass2 ids [{batch_start:,} .. {batch_end:,}) "
                  f"updated {n:>7,}  ({pct:5.1f}% scanned, "
                  f"{time.monotonic() - t0:.1f}s)")
            batch_start = batch_end
        return total_updated


async def report_final_stats(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT
                count(*) AS total,
                count(*) FILTER (WHERE fsa_code IS NOT NULL) AS covered,
                count(*) FILTER (WHERE enriched_from_source = 'postal_code') AS from_postal,
                count(*) FILTER (WHERE enriched_from_source = 'spatial_fsa') AS from_spatial,
                count(*) FILTER (WHERE fsa_code IS NULL) AS still_null,
                count(DISTINCT fsa_code) AS unique_fsas
            FROM nar_addresses
        """)
    print()
    print("=" * 70)
    print("FINAL COVERAGE")
    print("=" * 70)
    print(f"Total rows:          {row['total']:>12,}")
    print(f"fsa_code populated:  {row['covered']:>12,} "
          f"({row['covered'] / row['total'] * 100:.1f}%)")
    print(f"  from postal code:  {row['from_postal']:>12,}")
    print(f"  from spatial:      {row['from_spatial']:>12,}")
    print(f"Still NULL:          {row['still_null']:>12,} "
          f"({row['still_null'] / row['total'] * 100:.1f}%)")
    print(f"Unique FSA codes:    {row['unique_fsas']:>12,}")


async def spot_check(pool: asyncpg.Pool, sample_size: int) -> None:
    """
    For rows that have BOTH a valid postal code and coordinates, compare the
    postal-derived FSA with the spatially derived FSA. Low agreement would
    indicate an SRID mismatch or swapped x/y.
    """
    print()
    print(f"Spot check: postal FSA vs spatial FSA on {sample_size:,} random rows...")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f"""
            WITH sample AS (
                SELECT upper(left(n.mail_postal_code, 3)) AS postal_fsa,
                       n.bg_x, n.bg_y
                FROM nar_addresses n
                WHERE upper(left(n.mail_postal_code, 3)) ~ '{FSA_REGEX}'
                  AND n.bg_x IS NOT NULL AND n.bg_y IS NOT NULL
                ORDER BY random()
                LIMIT {sample_size}
            ),
            resolved AS (
                SELECT s.postal_fsa,
                       (SELECT f.fsa_code FROM fsa_boundaries f
                        WHERE ST_Contains(f.geometry, {make_point_sql('s.bg_x', 's.bg_y')})
                        LIMIT 1) AS spatial_fsa
                FROM sample s
            )
            SELECT count(*) AS sampled,
                   count(*) FILTER (WHERE spatial_fsa IS NOT NULL) AS resolved,
                   count(*) FILTER (WHERE spatial_fsa = postal_fsa) AS agree
            FROM resolved
        """)
    resolved = row['resolved']
    agree_rate = (row['agree'] / resolved * 100) if resolved else 0.0
    print(f"  sampled:                 {row['sampled']:,}")
    print(f"  spatially resolved:      {resolved:,}")
    print(f"  postal == spatial:       {row['agree']:,}  ({agree_rate:.1f}%)")
    if agree_rate < 95.0:
        print("  ⚠ WARNING: agreement below 95% — investigate SRID / x-y order "
              "before trusting spatial results")
    else:
        print("  ✓ agreement above 95% threshold")


async def main():
    parser = argparse.ArgumentParser(
        description='Populate nar_addresses.fsa_code from postal codes + PostGIS spatial lookup'
    )
    parser.add_argument('--db', default='geocoder',
                        help='Target database name (default: geocoder)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Report projected coverage without writing')
    parser.add_argument('--batch-size-postal', type=int, default=250_000,
                        help='Id-range width per pass-1 batch (default: 250000)')
    parser.add_argument('--batch-size-spatial', type=int, default=100_000,
                        help='Id-range width per pass-2 batch (default: 100000)')
    parser.add_argument('--spot-check-size', type=int, default=1000,
                        help='Rows for postal-vs-spatial agreement check (default: 1000)')
    parser.add_argument('--spot-check-only', action='store_true',
                        help='Only run the agreement spot check, no updates')
    args = parser.parse_args()

    print("=" * 70)
    print("ADD FSA CODES TO nar_addresses")
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

        if args.spot_check_only:
            await spot_check(pool, args.spot_check_size)
            return

        if args.dry_run:
            await dry_run(pool, spatial_sample=5000)
            return

        print("Pass 1: deriving fsa_code from mail_postal_code (batched UPDATE)...")
        n1 = await run_pass1_postal(pool, args.batch_size_postal)
        print(f"✓ Pass 1 complete: {n1:,} rows updated")
        print()

        print("Pass 2: spatial lookup for remaining rows with coordinates...")
        n2 = await run_pass2_spatial(pool, args.batch_size_spatial)
        print(f"✓ Pass 2 complete: {n2:,} rows updated")

        await report_final_stats(pool)
        await spot_check(pool, args.spot_check_size)
    finally:
        await pool.close()
        elapsed = time.monotonic() - t_start
        print()
        print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
              f"(elapsed {elapsed / 60:.1f} min)")


if __name__ == '__main__':
    asyncio.run(main())
