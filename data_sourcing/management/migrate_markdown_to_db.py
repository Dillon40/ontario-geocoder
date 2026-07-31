#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "asyncpg",
#   "python-dotenv",
# ]
# ///

"""
Migrate existing markdown data source documentation into the database.
Parses confirmed_sources.md and potential_sources.md from data_sourcing/docs/.
"""
import re
import sys
import asyncio
from pathlib import Path
from dotenv import load_dotenv

# Add project root to path for db_utils import
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from data_sourcing.db_utils import get_db_pool

# Load environment variables
load_dotenv()

REGIONAL_DIR = Path(__file__).parent.parent / 'docs'


def parse_potential_sources(content: str) -> list:
    """Parse potential_ontario_gis_data_sources.md"""
    sources = []

    # Split by source headers (## or ###)
    sections = re.split(r'\n(?=##+ )', content)

    for section in sections:
        if not section.strip():
            continue

        # Extract header - handle both formats
        # Format 1: ### Source Name [ ] or ### Source Name [x]
        # Format 2: ## Source Name **BLOCKED**
        header_match = re.match(
            r'^##+ ([^*\[\]]+?)(?:\s*\*\*([^*]+)\*\*)?\s*(?:\[([x ]?)\])?\s*$',
            section.split('\n')[0].strip()
        )

        if not header_match:
            # Try alternative format: ### Source Name
            # or: ### Source Name [ ]
            alt_match = re.match(
                r'^##+ (.+?)(?:\s*\[([x ]?)\])?\s*$',
                section.split('\n')[0].strip()
            )
            if alt_match:
                source_name = alt_match.group(1).strip()
                status_marker = alt_match.group(2)
            else:
                continue
        else:
            source_name = header_match.group(1).strip()
            special_status = header_match.group(2)  # e.g., "BLOCKED"
            status_marker = header_match.group(3)

            if special_status == 'BLOCKED':
                status = 'blocked'
            elif status_marker == 'x':
                status = 'confirmed'
            else:
                status = 'potential'

        # Skip technical notes section
        if source_name in ['Technical Notes', 'Common REST Endpoint Patterns', 'Data Typically Available']:
            continue

        # Skip province-wide entries that are informational
        if 'Province-Wide' in source_name and 'TERANET' not in source_name:
            continue

        # Generate source key
        source_key = re.sub(r'[^a-z0-9]+', '_', source_name.lower()).strip('_')
        source_key = source_key[:50]  # Truncate to max length

        # Extract portal URL
        portal_url = None
        url_match = re.search(r'\*\*Data Portal Link\(s\):\*\*\s*([^\n]+)', section)
        if url_match:
            # Extract first URL
            url_text = url_match.group(1)
            urls = re.findall(r'https?://[^\s\|\]]+', url_text)
            if urls:
                portal_url = urls[0].rstrip(')')

        # Extract data available (maps to data types)
        data_available = []
        data_match = re.search(r'\*\*Data Available:\*\*\s*([^\n]+)', section)
        if data_match:
            data_text = data_match.group(1).lower()
            if any(word in data_text for word in ['address', 'civic']):
                data_available.append('addresses')
            if any(word in data_text for word in ['parcel', 'property']):
                data_available.append('parcels')
            if 'zoning' in data_text:
                data_available.append('zoning')
            if 'building' in data_text or 'footprint' in data_text:
                data_available.append('building_footprints')
            if 'road' in data_text or 'street' in data_text:
                data_available.append('roads')
            if 'boundar' in data_text:
                data_available.append('boundaries')

        # Extract notes
        notes = None
        notes_match = re.search(r'\*\*Notes:\*\*\s*([^\n]+)', section)
        if notes_match:
            notes = notes_match.group(1).strip()

        # Determine API info from notes
        api_type = None
        if notes:
            if 'REST' in notes.upper():
                api_type = 'REST/FeatureServer'

        # Determine status
        if 'status' not in locals():
            if status_marker == 'x':
                status = 'confirmed'
            else:
                status = 'potential'

        # Determine region type
        region_type = None
        name_lower = source_name.lower()
        if 'region' in name_lower:
            region_type = 'region'
        elif 'county' in name_lower or 'counties' in name_lower:
            region_type = 'county'
        elif any(word in name_lower for word in ['city', 'town', 'township']):
            region_type = 'city'
        elif 'province' in name_lower or 'ontario' in name_lower:
            region_type = 'province'
        else:
            region_type = 'city'  # Default for individual municipalities

        sources.append({
            'source_key': source_key,
            'source_name': source_name,
            'status': status,
            'regions_covered': [source_name.replace(' Region', '').replace(' County', '')],
            'region_type': region_type,
            'portal_url': portal_url,
            'api_type': api_type,
            'notes': notes,
            'data_types': data_available
        })

    return sources


def parse_confirmed_sources(content: str) -> list:
    """Parse confirmed_ontario_gis_data_sources.md"""
    sources = []

    # Split by headers
    sections = re.split(r'\n(?=###? )', content)

    current_status = 'confirmed'  # Default for workable section

    for section in sections:
        if not section.strip():
            continue

        # Check for section headers
        if section.strip().startswith('# Workable'):
            current_status = 'confirmed'
            continue
        elif section.strip().startswith('# Not Workable'):
            current_status = 'rejected'
            continue

        # Extract header
        header_match = re.match(
            r'^###? ([^*\[\]]+?)(?:\s*\*\*[^*]+\*\*)?\s*(?:\[([x ]?)\])?\s*$',
            section.split('\n')[0].strip()
        )

        if not header_match:
            continue

        source_name = header_match.group(1).strip()

        # Generate source key
        source_key = re.sub(r'[^a-z0-9]+', '_', source_name.lower()).strip('_')
        source_key = source_key[:50]

        # Extract portal URL
        portal_url = None
        url_match = re.search(r'\*\*Data Portal Link\(s\):\*\*\s*([^\n]+)', section)
        if url_match:
            url_text = url_match.group(1)
            urls = re.findall(r'https?://[^\s\|\]]+', url_text)
            if urls:
                portal_url = urls[0].rstrip(')')

        # Extract copyright/license info
        license_notes = None
        commercial_use = None
        license_url = None

        copyright_match = re.search(r'\*\*Copyright:\*\*\s*([^\n]+)', section)
        if copyright_match:
            license_text = copyright_match.group(1)
            license_notes = license_text.strip()

            if 'EXPLICIT COMMERCIAL USE' in license_text.upper():
                commercial_use = True

            # Extract license URL
            license_urls = re.findall(r'\[link\]\((https?://[^\)]+)\)', section)
            if license_urls:
                license_url = license_urls[0]

        # Extract notes from retrieval process
        notes = None
        if '### Retrieval Process:' in section:
            retrieval_match = re.search(r'### Retrieval Process:\n(.+?)(?=\n###|\Z)', section, re.DOTALL)
            if retrieval_match:
                notes = retrieval_match.group(1).strip()

        # Determine region type
        region_type = 'city'
        name_lower = source_name.lower()
        if 'county' in name_lower:
            region_type = 'county'
        elif 'province' in name_lower or 'teranet' in name_lower:
            region_type = 'province'

        # Determine status for TERANET
        status = current_status
        if 'TERANET' in source_name:
            status = 'blocked'
            commercial_use = False

        sources.append({
            'source_key': source_key,
            'source_name': source_name,
            'status': status,
            'regions_covered': [source_name.replace(' County', '')],
            'region_type': region_type,
            'portal_url': portal_url,
            'license_notes': license_notes,
            'license_url': license_url,
            'commercial_use_allowed': commercial_use,
            'notes': notes,
            'data_types': []
        })

    return sources


async def migrate_to_database(sources: list, dry_run: bool = False):
    """Insert sources into database."""
    pool = await get_db_pool(min_size=1, max_size=5)

    try:
        # Get existing data types
        data_type_ids = {}
        types = await pool.fetch("SELECT id, type_key FROM data_types")
        for t in types:
            data_type_ids[t['type_key']] = t['id']

        inserted = 0
        skipped = 0

        for source in sources:
            if dry_run:
                print(f"[DRY RUN] Would insert: {source['source_key']} - {source['source_name']}")
                inserted += 1
                continue

            try:
                # Insert source
                result = await pool.fetchval(
                    '''
                    INSERT INTO data_sources (
                        source_key, source_name, status, regions_covered, region_type,
                        portal_url, license_notes, license_url, commercial_use_allowed,
                        notes, api_type
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    ON CONFLICT (source_key) DO UPDATE SET
                        source_name = EXCLUDED.source_name,
                        status = EXCLUDED.status,
                        regions_covered = EXCLUDED.regions_covered,
                        portal_url = COALESCE(EXCLUDED.portal_url, data_sources.portal_url),
                        license_notes = COALESCE(EXCLUDED.license_notes, data_sources.license_notes),
                        license_url = COALESCE(EXCLUDED.license_url, data_sources.license_url),
                        commercial_use_allowed = COALESCE(EXCLUDED.commercial_use_allowed, data_sources.commercial_use_allowed),
                        notes = COALESCE(EXCLUDED.notes, data_sources.notes),
                        api_type = COALESCE(EXCLUDED.api_type, data_sources.api_type)
                    RETURNING id
                    ''',
                    source['source_key'],
                    source['source_name'],
                    source['status'],
                    source['regions_covered'],
                    source.get('region_type'),
                    source.get('portal_url'),
                    source.get('license_notes'),
                    source.get('license_url'),
                    source.get('commercial_use_allowed'),
                    source.get('notes'),
                    source.get('api_type')
                )

                # Link data types
                for type_key in source.get('data_types', []):
                    if type_key in data_type_ids:
                        await pool.execute(
                            '''
                            INSERT INTO data_source_types (data_source_id, data_type_id)
                            VALUES ($1, $2)
                            ON CONFLICT DO NOTHING
                            ''',
                            result,
                            data_type_ids[type_key]
                        )

                print(f"✓ Inserted: {source['source_key']}")
                inserted += 1

            except Exception as e:
                print(f"⚠ Error inserting {source['source_key']}: {e}")
                skipped += 1

        return inserted, skipped

    finally:
        await pool.close()


async def main():
    """Main execution."""
    import argparse

    parser = argparse.ArgumentParser(description='Migrate markdown data sources to database')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be migrated without inserting')
    args = parser.parse_args()

    print("=" * 60)
    print("Migrating Markdown Data Sources to Database")
    print("=" * 60)

    # Read potential sources
    potential_file = REGIONAL_DIR / 'potential_sources.md'
    if potential_file.exists():
        print(f"\nParsing: {potential_file}")
        content = potential_file.read_text()
        potential_sources = parse_potential_sources(content)
        print(f"  Found {len(potential_sources)} potential sources")
    else:
        print(f"⚠ File not found: {potential_file}")
        potential_sources = []

    # Read confirmed sources
    confirmed_file = REGIONAL_DIR / 'confirmed_sources.md'
    if confirmed_file.exists():
        print(f"\nParsing: {confirmed_file}")
        content = confirmed_file.read_text()
        confirmed_sources = parse_confirmed_sources(content)
        print(f"  Found {len(confirmed_sources)} confirmed sources")
    else:
        print(f"⚠ File not found: {confirmed_file}")
        confirmed_sources = []

    # Merge sources (confirmed takes precedence)
    all_sources = {}

    # Add potential first
    for source in potential_sources:
        all_sources[source['source_key']] = source

    # Override with confirmed (more detailed info)
    for source in confirmed_sources:
        key = source['source_key']
        if key in all_sources:
            # Merge data
            existing = all_sources[key]
            for field, value in source.items():
                if value and not existing.get(field):
                    existing[field] = value
            # Update status from confirmed file
            existing['status'] = source['status']
        else:
            all_sources[key] = source

    sources_list = list(all_sources.values())

    print(f"\nTotal unique sources to migrate: {len(sources_list)}")
    print("=" * 60)

    if args.dry_run:
        print("\n[DRY RUN MODE - No changes will be made]\n")

    # Migrate to database
    inserted, skipped = await migrate_to_database(sources_list, dry_run=args.dry_run)

    print("\n" + "=" * 60)
    print("Migration Summary")
    print("=" * 60)
    print(f"Inserted/Updated: {inserted}")
    print(f"Skipped (errors): {skipped}")

    if not args.dry_run:
        print("\n✓ Migration complete!")
        print("Run 'uv run generate_data_source_docs.py' to generate updated markdown.")
    else:
        print("\nTo perform actual migration, run without --dry-run")

    print("=" * 60)


if __name__ == '__main__':
    asyncio.run(main())
