#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "asyncpg>=0.29.0",
# ]
# ///
"""
Backfill unsplit civic numbers in the properties table.

Some sources (brantford_addresses, huron_county) were imported with
letter-suffixed civic numbers left unsplit in street_number (e.g. '68A')
because the source also supplied civic_no_suffix directly and the
CivicNumberParser transform runs with passthrough_suffix=true, which skips
parsing whenever civic_no_suffix is already populated. The query-time parser
now splits input '68A' into street_number='68' + civic_no_suffix='A', so the
unsplit rows can no longer match. This script rewrites them to the split
form using the exact same parse function the ETL pipeline uses
(data_sourcing.transformers.civic_number_parser.parse_civic_number).

Safety properties:
  - Idempotent: candidates are selected with street_number !~ '^[0-9]+$';
    once split, a row is plain digits and never selected again. Each UPDATE
    is also guarded with "AND street_number = <original>".
  - Suffix conflicts: if a row already has a civic_no_suffix that differs
    from what parse_civic_number extracts, the row is skipped and logged.
  - Collision-safe: if the split form would duplicate another existing row
    on (source_dataset, street_name, city, unit_no, street_number,
    civic_no_suffix), the row is skipped and logged, not updated.
  - Batched: updates run in batches, each in its own transaction.
  - Scope: only the properties table, only the requested source_datasets.

Usage:
    # Dry run: report what would change, write nothing
    ./backfill_unsplit_civic_numbers.py --dry-run

    # Real run
    ./backfill_unsplit_civic_numbers.py

    # Different sources / connection
    ./backfill_unsplit_civic_numbers.py --sources brantford_addresses huron_county \
        --db geocoder --host localhost --port 5432 --user geocoder
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

import asyncpg

# Make the project root importable so we reuse the ETL transformer verbatim.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from data_sourcing.transformers.civic_number_parser import parse_civic_number  # noqa: E402

DEFAULT_SOURCES = ["brantford_addresses", "huron_county"]

# Candidate filter: anything that is not a plain digit string. parse_civic_number
# decides what actually gets split; ranges like '100-102' come back unchanged.
CANDIDATE_SQL = """
    SELECT id, source_dataset, street_number, civic_no_suffix,
           street_name, city, unit_no
    FROM properties
    WHERE source_dataset = ANY($1)
      AND street_number IS NOT NULL
      AND street_number !~ '^[0-9]+$'
    ORDER BY id
"""

# All rows in the affected sources, for in-memory collision detection.
EXISTING_KEYS_SQL = """
    SELECT id, source_dataset, street_number, civic_no_suffix,
           street_name, city, unit_no
    FROM properties
    WHERE source_dataset = ANY($1)
"""

# Guard on the original street_number so a concurrent change makes the
# update a no-op instead of clobbering.
UPDATE_SQL = """
    UPDATE properties
    SET street_number = $2,
        civic_no_suffix = $3,
        updated_at = now()
    WHERE id = $1
      AND street_number = $4
"""

SURVEY_SQL = """
    SELECT source_dataset,
           COUNT(*) FILTER (WHERE street_number ~ '^[0-9]+[A-Za-z]$') AS attached_letter,
           COUNT(*) FILTER (WHERE street_number ~ '^[0-9]+ [0-9]+/[0-9]+$') AS fraction,
           COUNT(*) FILTER (WHERE street_number !~ '^[0-9]+$') AS any_non_digit
    FROM properties
    WHERE source_dataset = ANY($1)
    GROUP BY source_dataset
    ORDER BY source_dataset
"""


def norm(value):
    """Normalize a value for collision-key comparison (NULL-safe, case-insensitive)."""
    if value is None:
        return ""
    return str(value).strip().upper()


def collision_key(source, street_name, city, unit_no, street_number, suffix):
    return (
        norm(source), norm(street_name), norm(city),
        norm(unit_no), norm(street_number), norm(suffix),
    )


async def survey(conn, sources, label):
    rows = await conn.fetch(SURVEY_SQL, sources)
    print(f"\n{label}:")
    print(f"  {'source_dataset':<28} {'attached':>9} {'fraction':>9} {'non_digit':>10}")
    for r in rows:
        print(f"  {r['source_dataset']:<28} {r['attached_letter']:>9} "
              f"{r['fraction']:>9} {r['any_non_digit']:>10}")


async def main():
    parser = argparse.ArgumentParser(description="Backfill unsplit civic numbers")
    parser.add_argument("--db", default="geocoder")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--user", default="geocoder")
    parser.add_argument("--password", default=None,
                         help="DB password (default: $PGPASSWORD, then $DB_PASSWORD; "
                              "one of these or --password is required)")
    parser.add_argument("--sources", nargs="+", default=DEFAULT_SOURCES)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    password = args.password or os.environ.get("PGPASSWORD") or os.environ.get("DB_PASSWORD")
    if not password:
        parser.error(
            "no password available: pass --password, or set PGPASSWORD or "
            "DB_PASSWORD in the environment (source .env) - there is no default password."
        )

    conn = await asyncpg.connect(
        host=args.host, port=args.port, user=args.user,
        password=password, database=args.db,
    )
    try:
        await survey(conn, args.sources, "Before")

        candidates = await conn.fetch(CANDIDATE_SQL, args.sources)
        print(f"\nCandidate rows (non-digit street_number): {len(candidates)}")

        # Collision map: split-form key -> set of row ids already occupying it.
        existing = await conn.fetch(EXISTING_KEYS_SQL, args.sources)
        occupied = {}
        for r in existing:
            key = collision_key(r["source_dataset"], r["street_name"], r["city"],
                                r["unit_no"], r["street_number"], r["civic_no_suffix"])
            occupied.setdefault(key, set()).add(r["id"])

        updates = []          # (id, base, suffix, original_street_number)
        stats = {"planned": 0, "no_suffix_pattern": 0,
                 "suffix_conflict": 0, "collision": 0}

        for r in candidates:
            base, suffix = parse_civic_number(r["street_number"])
            if not suffix or base == r["street_number"]:
                # Ranges ('100-102'), empty, or otherwise unparseable: leave as-is.
                stats["no_suffix_pattern"] += 1
                continue

            if r["civic_no_suffix"] is not None and norm(r["civic_no_suffix"]) != norm(suffix):
                stats["suffix_conflict"] += 1
                print(f"  SKIP suffix-conflict id={r['id']} "
                      f"street_number={r['street_number']!r} "
                      f"stored civic_no_suffix={r['civic_no_suffix']!r} parsed={suffix!r}")
                continue

            key = collision_key(r["source_dataset"], r["street_name"], r["city"],
                                r["unit_no"], base, suffix)
            holders = occupied.get(key, set())
            if holders - {r["id"]}:
                stats["collision"] += 1
                print(f"  SKIP collision id={r['id']} "
                      f"{r['street_number']!r} -> ({base!r}, {suffix!r}) "
                      f"duplicates id(s) {sorted(holders - {r['id']})} "
                      f"on {r['street_name']!r} / {r['city']!r}")
                continue

            # Reserve the key so two candidates cannot split onto the same row.
            occupied.setdefault(key, set()).add(r["id"])
            updates.append((r["id"], base, suffix, r["street_number"]))
            stats["planned"] += 1

        print(f"\nPlanned updates:   {stats['planned']}")
        print(f"Left as-is (no suffix pattern, e.g. ranges): {stats['no_suffix_pattern']}")
        print(f"Skipped (suffix conflict): {stats['suffix_conflict']}")
        print(f"Skipped (collision):       {stats['collision']}")

        if args.dry_run:
            print("\nDry run: no changes written.")
            for row in updates[:10]:
                print(f"  would update id={row[0]}: {row[3]!r} -> ({row[1]!r}, {row[2]!r})")
            return

        updated = 0
        for i in range(0, len(updates), args.batch_size):
            batch = updates[i:i + args.batch_size]
            async with conn.transaction():
                await conn.executemany(UPDATE_SQL, batch)
            updated += len(batch)
            print(f"  batch {i // args.batch_size + 1}: {updated}/{len(updates)} rows")

        print(f"\nUpdated rows: {updated}")
        await survey(conn, args.sources, "After")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
