#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "asyncpg",
#   "python-dotenv",
# ]
# ///

"""
Import potential data sources from markdown file into database.
Avoids duplicates by checking if source_key already exists or if a more
specific version exists (e.g., skips 'barrie' if 'barrie_addresses' exists).
"""
import argparse
import re
import sys
import asyncio
from pathlib import Path
from dotenv import load_dotenv
import asyncpg

# Add project root to path for db_utils import
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from data_sourcing.db_utils import get_db_pool

load_dotenv()

MARKDOWN_FILE = Path(__file__).parent.parent / 'docs' / 'potential_sources.md'

# Map markdown data type names to database type_keys
DATA_TYPE_MAP = {
    'Address Points': 'addresses',
    'Administrative Boundaries': 'boundaries',
    'Building Footprints': 'building_footprints',
    'Postal Code Areas': 'postal_codes',
    'Property Parcels': 'parcels',
    'Road Network': 'roads',
    'Zoning Data': 'zoning',
}


def parse_markdown_sources(content: str) -> list[dict]:
    """Parse the markdown file and extract source records."""
    sources = []

    # Split by source sections (## headers followed by ---)
    sections = re.split(r'\n---\n', content)

    for section in sections:
        section = section.strip()
        if not section or not section.startswith('##'):
            continue

        # Skip the header section
        if section.startswith('# '):
            continue

        source = {}

        # Extract source name from ## header
        name_match = re.search(r'^## (.+)$', section, re.MULTILINE)
        if name_match:
            source['source_name'] = name_match.group(1).strip()
        else:
            continue

        # Extract key
        key_match = re.search(r'\*\*Key:\*\* `([^`]+)`', section)
        if key_match:
            source['source_key'] = key_match.group(1).strip()
        else:
            continue

        # Extract regions covered
        regions_match = re.search(r'\*\*Regions Covered:\*\* (.+)$', section, re.MULTILINE)
        if regions_match:
            regions_str = regions_match.group(1).strip()
            # Handle comma-separated regions
            source['regions_covered'] = [r.strip() for r in regions_str.split(',')]

        # Extract region type
        type_match = re.search(r'\*\*Region Type:\*\* (.+)$', section, re.MULTILINE)
        if type_match:
            source['region_type'] = type_match.group(1).strip()

        # Extract data types
        data_types_match = re.search(r'\*\*Data Types Available:\*\* (.+)$', section, re.MULTILINE)
        if data_types_match:
            types_str = data_types_match.group(1).strip()
            source['data_types'] = [t.strip() for t in types_str.split(',')]

        # Extract portal URL
        portal_match = re.search(r'\*\*Portal URL:\*\* (.+)$', section, re.MULTILINE)
        if portal_match:
            source['portal_url'] = portal_match.group(1).strip()

        # Extract API type - must be on a single line after "### API Access"
        api_type_match = re.search(r'- \*\*Type:\*\* (.+)$', section, re.MULTILINE)
        if api_type_match:
            source['api_type'] = api_type_match.group(1).strip()

        # Extract notes (text after ### Notes)
        notes_match = re.search(r'### Notes\n(.+?)(?=\n###|\n---|\Z)', section, re.DOTALL)
        if notes_match:
            notes = notes_match.group(1).strip()
            # Clean up the notes - remove markdown artifacts
            notes = re.sub(r'^\s*-\s*', '', notes)  # Remove leading bullet
            if notes and notes != '*No licensing information recorded*':
                source['notes'] = notes

        sources.append(source)

    return sources


async def get_existing_keys(pool: asyncpg.Pool) -> set[str]:
    """Get all existing source keys from database."""
    rows = await pool.fetch("SELECT source_key FROM data_sources")
    return {row['source_key'] for row in rows}


async def get_data_type_ids(pool: asyncpg.Pool) -> dict[str, int]:
    """Get mapping of type_key to id."""
    rows = await pool.fetch("SELECT id, type_key FROM data_types")
    return {row['type_key']: row['id'] for row in rows}


def should_skip_source(source_key: str, existing_keys: set[str]) -> bool:
    """
    Check if source should be skipped.
    Returns True if:
    - Exact key exists
    - A more specific version exists (e.g., barrie_addresses for barrie)
    """
    # Exact match
    if source_key in existing_keys:
        return True

    # Check if a more specific version exists
    # e.g., 'barrie' should be skipped if 'barrie_addresses' exists
    for existing_key in existing_keys:
        if existing_key.startswith(f"{source_key}_"):
            return True

    return False


async def import_source(pool: asyncpg.Pool, source: dict, data_type_ids: dict[str, int]) -> bool:
    """Import a single source into the database."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Insert the source
            source_id = await conn.fetchval('''
                INSERT INTO data_sources (
                    source_key, source_name, status, regions_covered,
                    region_type, portal_url, api_type, notes
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id
            ''',
                source.get('source_key'),
                source.get('source_name'),
                'potential',
                source.get('regions_covered'),
                source.get('region_type'),
                source.get('portal_url'),
                source.get('api_type'),
                source.get('notes')
            )

            # Insert data type associations
            data_types = source.get('data_types', [])
            for dt_name in data_types:
                type_key = DATA_TYPE_MAP.get(dt_name)
                if type_key and type_key in data_type_ids:
                    await conn.execute('''
                        INSERT INTO data_source_types (data_source_id, data_type_id)
                        VALUES ($1, $2)
                        ON CONFLICT DO NOTHING
                    ''', source_id, data_type_ids[type_key])

            return True


async def main():
    """Main execution."""
    print("Importing potential sources from markdown...")
    print("=" * 60)

    # Read and parse markdown
    if not MARKDOWN_FILE.exists():
        print(f"Error: Markdown file not found: {MARKDOWN_FILE}")
        return

    content = MARKDOWN_FILE.read_text()
    sources = parse_markdown_sources(content)
    print(f"Parsed {len(sources)} sources from markdown")

    # Connect to database
    pool = await get_db_pool(min_size=1, max_size=5)

    try:
        existing_keys = await get_existing_keys(pool)
        data_type_ids = await get_data_type_ids(pool)

        print(f"Found {len(existing_keys)} existing sources in database")
        print("-" * 60)

        imported = 0
        skipped = 0
        skipped_sources = []

        for source in sources:
            source_key = source.get('source_key')
            source_name = source.get('source_name')

            if should_skip_source(source_key, existing_keys):
                skipped += 1
                # Find which key caused the skip
                matched_key = source_key if source_key in existing_keys else None
                if not matched_key:
                    for ek in existing_keys:
                        if ek.startswith(f"{source_key}_"):
                            matched_key = ek
                            break
                skipped_sources.append((source_name, source_key, matched_key))
                continue

            try:
                await import_source(pool, source, data_type_ids)
                imported += 1
                print(f"  Imported: {source_name} ({source_key})")
            except Exception as e:
                print(f"  Error importing {source_name}: {e}")

        print("-" * 60)
        print(f"Imported: {imported}")
        print(f"Skipped (already exist): {skipped}")

        if skipped_sources:
            print("\nSkipped sources (matched existing):")
            for name, key, matched in skipped_sources:
                print(f"  - {name} ({key}) -> matched: {matched}")

        print("=" * 60)

    finally:
        await pool.close()


def parse_args():
    parser = argparse.ArgumentParser(
        description='Import potential data sources from docs/potential_sources.md '
                     'into the database, skipping duplicates. Connects to the database.'
    )
    return parser.parse_args()


if __name__ == '__main__':
    parse_args()
    asyncio.run(main())
