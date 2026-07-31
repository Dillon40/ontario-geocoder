#!/usr/bin/env python3
"""
Parse MySQL dump file for commercial postal code data.

Converts MySQL INSERT statements to Python dictionaries for PostgreSQL import.

Handles:
- MySQL INSERT VALUES syntax parsing
- Batch processing for memory efficiency
- Postal code normalization (removes space)
- NULL value handling for optional fields

Usage:
    from parse_mysql_postal_dump import parse_mysql_dump

    for batch in parse_mysql_dump('/path/to/cdn_db.sql', batch_size=1000):
        for record in batch:
            print(record['postal_code'], record['city'])
"""

import argparse
import re
from pathlib import Path
from typing import Iterator, Dict, Any, List, Optional

# Pattern to match individual value tuples in INSERT statements
# Format: (id, 'postal', 'fsa', community, county, region, 'city', 'uri', latitude, longitude)
VALUE_PATTERN = re.compile(
    r"\((\d+),\s*"                           # id (integer)
    r"'([^']+)',\s*"                         # postal (string with space)
    r"'([^']+)',\s*"                         # fsa (3-char string)
    r"(\d+|NULL),\s*"                        # community (int or NULL)
    r"(\d+|NULL),\s*"                        # county (int or NULL)
    r"(\d+|NULL),\s*"                        # region (int or NULL)
    r"'([^']*)',\s*"                         # city (string, may be empty)
    r"'([^']*)',\s*"                         # uri (string, may be empty)
    r"([\d.-]+|NULL),\s*"                    # latitude (decimal or NULL)
    r"([\d.-]+|NULL)\)"                      # longitude (decimal or NULL)
)


def parse_value(value: str, as_type: str = 'str') -> Optional[Any]:
    """Parse a single value, handling NULL and type conversion."""
    if value == 'NULL' or value == '':
        return None

    if as_type == 'int':
        return int(value)
    elif as_type == 'float':
        return float(value)
    else:
        return value


def parse_record(match: re.Match) -> Dict[str, Any]:
    """
    Parse a regex match into a record dictionary.

    Returns dict with normalized keys matching PostgreSQL schema.
    """
    postal_raw = match.group(2)

    return {
        'source_id': int(match.group(1)),
        'postal_code_raw': postal_raw,
        'postal_code': postal_raw.replace(' ', ''),  # Normalize: remove space
        'fsa_code': match.group(3),
        'source_community_id': parse_value(match.group(4), 'int'),
        'source_county_id': parse_value(match.group(5), 'int'),
        'source_region_id': parse_value(match.group(6), 'int'),
        'city': parse_value(match.group(7)) or None,
        'uri': parse_value(match.group(8)) or None,
        'latitude': parse_value(match.group(9), 'float'),
        'longitude': parse_value(match.group(10), 'float'),
    }


def parse_mysql_dump(
    file_path: str,
    batch_size: int = 1000,
    skip_header: bool = True
) -> Iterator[List[Dict[str, Any]]]:
    """
    Parse MySQL dump file yielding batches of records.

    Args:
        file_path: Path to MySQL dump file (.sql)
        batch_size: Number of records per batch (default 1000)
        skip_header: Skip non-data lines at start (default True)

    Yields:
        List of record dictionaries, each batch up to batch_size

    Example:
        for batch in parse_mysql_dump('cdn_db.sql'):
            for record in batch:
                print(f"{record['postal_code']}: {record['city']}")
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"MySQL dump file not found: {file_path}")

    batch: List[Dict[str, Any]] = []
    total_parsed = 0

    with open(file_path, 'r', encoding='utf-8') as f:
        for line_num, line in enumerate(f, 1):
            # Skip empty lines and comments
            line = line.strip()
            if not line or line.startswith('--') or line.startswith('/*'):
                continue

            # Skip MySQL directives and non-data statements
            if line.startswith(('SET ', 'START ', 'COMMIT', 'CREATE ', 'ALTER ',
                               'INSERT INTO', '/*!', '`')):
                continue

            # Look for value tuples - lines starting with '(' contain data
            if not line.startswith('('):
                continue

            # Parse all value tuples in this line
            for match in VALUE_PATTERN.finditer(line):
                try:
                    record = parse_record(match)
                    batch.append(record)
                    total_parsed += 1

                    if len(batch) >= batch_size:
                        yield batch
                        batch = []

                except Exception as e:
                    # Log parse errors but continue
                    print(f"Warning: Parse error at line {line_num}: {e}")
                    continue

    # Yield remaining records
    if batch:
        yield batch

    print(f"Parsed {total_parsed:,} records from {file_path.name}")


def count_records(file_path: str) -> int:
    """Count total records in dump file without full parsing."""
    count = 0
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip().startswith('('):
                # Count parentheses pairs as rough record count
                count += line.count('),(') + 1 if '),(' in line else 1
    return count


def get_sample_records(file_path: str, n: int = 5) -> List[Dict[str, Any]]:
    """Get first n records for inspection."""
    records = []
    for batch in parse_mysql_dump(file_path, batch_size=n):
        records.extend(batch)
        if len(records) >= n:
            break
    return records[:n]


def parse_args():
    parser = argparse.ArgumentParser(
        description='Parse a MySQL dump file of commercial postal code data and '
                     'print sample records plus a total count. Pure file parsing - '
                     'does not touch the database.'
    )
    parser.add_argument('sql_file', help='Path to the MySQL dump .sql file')
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    file_path = args.sql_file

    print(f"Parsing MySQL dump: {file_path}")
    print("-" * 60)

    # Show sample records
    print("\nSample records:")
    for i, record in enumerate(get_sample_records(file_path, 5), 1):
        print(f"  {i}. {record['postal_code_raw']} -> {record['postal_code']} | "
              f"{record['city']} | ({record['latitude']}, {record['longitude']})")

    # Count total
    print(f"\nCounting records...")
    total_batches = 0
    total_records = 0
    for batch in parse_mysql_dump(file_path):
        total_batches += 1
        total_records += len(batch)

    print(f"\nSummary:")
    print(f"  Total records: {total_records:,}")
    print(f"  Total batches: {total_batches}")
    print(f"  Batch size: 1000")
