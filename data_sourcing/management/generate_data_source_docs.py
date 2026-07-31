#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "asyncpg",
#   "python-dotenv",
# ]
# ///

"""
Generate markdown documentation from data_sources database table.
Creates separate files for confirmed and potential data sources.
Outputs in markdown table format.
"""
import sys
import asyncio
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
import asyncpg

# Add project root to path for db_utils import
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from data_sourcing.db_utils import get_db_pool

# Load environment variables
load_dotenv()

OUTPUT_DIR = Path(__file__).parent.parent / 'docs'


async def get_data_sources(pool: asyncpg.Pool) -> list:
    """Fetch all data sources with their data types."""
    query = '''
        SELECT
            ds.id,
            ds.source_key,
            ds.source_name,
            ds.status,
            ds.regions_covered,
            ds.region_type,
            ds.postal_prefixes,
            ds.portal_url,
            ds.scraping_notes,
            ds.api_endpoint,
            ds.api_type,
            ds.api_key_required,
            ds.api_rate_limit,
            ds.api_notes,
            ds.license_type,
            ds.license_url,
            ds.commercial_use_allowed,
            ds.license_notes,
            ds.last_data_refresh,
            ds.record_count,
            ds.notes,
            ds.updated_at,
            COALESCE(
                array_agg(dt.display_name ORDER BY dt.display_name)
                FILTER (WHERE dt.display_name IS NOT NULL),
                ARRAY[]::TEXT[]
            ) as data_types
        FROM data_sources ds
        LEFT JOIN data_source_types dst ON ds.id = dst.data_source_id
        LEFT JOIN data_types dt ON dst.data_type_id = dt.id
        GROUP BY ds.id
        ORDER BY ds.source_name
    '''
    return await pool.fetch(query)


def escape_pipe(text: str) -> str:
    """Escape pipe characters for markdown tables."""
    if text is None:
        return ''
    return str(text).replace('|', '\\|').replace('\n', ' ')


def truncate(text: str, max_len: int = 50) -> str:
    """Truncate text to max length with ellipsis."""
    if text is None:
        return ''
    text = str(text).replace('\n', ' ')
    if len(text) <= max_len:
        return text
    return text[:max_len-3] + '...'


def format_url_link(url: str, label: str = 'Link') -> str:
    """Format URL as markdown link."""
    if not url:
        return ''
    return f'[{label}]({url})'


def generate_overview_table(sources: list) -> str:
    """Generate the main overview table."""
    lines = []

    # Table header
    lines.append("## Overview")
    lines.append("")
    lines.append("| Source Name | Key | Region Type | Regions | Data Types | Records | Last Refresh |")
    lines.append("|-------------|-----|-------------|---------|------------|---------|--------------|")

    # Table rows
    for source in sources:
        name = escape_pipe(source['source_name'])
        key = f"`{escape_pipe(source['source_key'])}`"
        region_type = escape_pipe(source['region_type']) or '-'

        regions = source['regions_covered'] or []
        if len(regions) > 3:
            regions_str = f"{', '.join(regions[:3])} (+{len(regions)-3})"
        else:
            regions_str = ', '.join(regions) if regions else '-'
        regions_str = escape_pipe(regions_str)

        data_types = source['data_types'] or []
        if len(data_types) > 2:
            types_str = f"{', '.join(data_types[:2])} (+{len(data_types)-2})"
        else:
            types_str = ', '.join(data_types) if data_types else '-'
        types_str = escape_pipe(types_str)

        record_count = f"{source['record_count']:,}" if source['record_count'] else '-'
        last_refresh = source['last_data_refresh'].strftime('%Y-%m-%d') if source['last_data_refresh'] else '-'

        lines.append(f"| {name} | {key} | {region_type} | {regions_str} | {types_str} | {record_count} | {last_refresh} |")

    lines.append("")
    return '\n'.join(lines)


def generate_access_table(sources: list) -> str:
    """Generate table showing portal and API access information."""
    lines = []

    lines.append("## Access Information")
    lines.append("")
    lines.append("| Source Name | Portal | API Type | API Endpoint | API Key | Rate Limit |")
    lines.append("|-------------|--------|----------|--------------|---------|------------|")

    for source in sources:
        name = escape_pipe(source['source_name'])
        portal = format_url_link(source['portal_url'], 'Portal') if source['portal_url'] else '-'
        api_type = escape_pipe(source['api_type']) or '-'

        if source['api_endpoint']:
            # Truncate long endpoints
            endpoint = truncate(source['api_endpoint'], 40)
            endpoint = f"`{escape_pipe(endpoint)}`"
        else:
            endpoint = '-'

        api_key = 'Yes' if source['api_key_required'] else 'No' if source['api_key_required'] is False else '-'
        rate_limit = escape_pipe(source['api_rate_limit']) or '-'

        lines.append(f"| {name} | {portal} | {api_type} | {endpoint} | {api_key} | {rate_limit} |")

    lines.append("")
    return '\n'.join(lines)


def generate_licensing_table(sources: list) -> str:
    """Generate table showing licensing information."""
    lines = []

    lines.append("## Licensing Information")
    lines.append("")
    lines.append("| Source Name | License Type | Commercial Use | License URL | Notes |")
    lines.append("|-------------|--------------|----------------|-------------|-------|")

    for source in sources:
        name = escape_pipe(source['source_name'])
        license_type = escape_pipe(source['license_type']) or '-'

        if source['commercial_use_allowed'] is None:
            commercial = '-'
        elif source['commercial_use_allowed']:
            commercial = 'Yes'
        else:
            commercial = 'No'

        license_url = format_url_link(source['license_url'], 'View') if source['license_url'] else '-'
        notes = truncate(escape_pipe(source['license_notes']), 40) or '-'

        lines.append(f"| {name} | {license_type} | {commercial} | {license_url} | {notes} |")

    lines.append("")
    return '\n'.join(lines)


def generate_details_section(sources: list) -> str:
    """Generate detailed notes section for sources with additional info."""
    lines = []

    # Only include sources that have notes, scraping notes, or API notes
    detailed_sources = [
        s for s in sources
        if s['notes'] or s['scraping_notes'] or s['api_notes']
    ]

    if not detailed_sources:
        return ''

    lines.append("## Additional Notes")
    lines.append("")

    for source in detailed_sources:
        lines.append(f"### {source['source_name']}")
        lines.append("")

        if source['scraping_notes']:
            lines.append(f"**Scraping Notes:** {source['scraping_notes']}")
            lines.append("")

        if source['api_notes']:
            lines.append(f"**API Notes:** {source['api_notes']}")
            lines.append("")

        if source['notes']:
            lines.append(f"**General Notes:** {source['notes']}")
            lines.append("")

    return '\n'.join(lines)


def generate_markdown_file(sources: list, status: str, title: str) -> str:
    """Generate complete markdown file for sources with given status."""
    lines = []

    # Header
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"*Auto-generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}*")
    lines.append("")
    lines.append(f"**Total Sources:** {len(sources)}")
    lines.append("")

    # Summary of data types
    all_types = set()
    for source in sources:
        all_types.update(source['data_types'] or [])
    if all_types:
        lines.append(f"**Data Types Across All Sources:** {', '.join(sorted(all_types))}")
        lines.append("")

    # Summary stats
    total_records = sum(s['record_count'] or 0 for s in sources)
    if total_records:
        lines.append(f"**Total Records:** {total_records:,}")
        lines.append("")

    lines.append("---")
    lines.append("")

    # Generate tables
    lines.append(generate_overview_table(sources))
    lines.append(generate_access_table(sources))
    lines.append(generate_licensing_table(sources))

    # Add detailed notes section
    details = generate_details_section(sources)
    if details:
        lines.append(details)

    return '\n'.join(lines)


async def main():
    """Main execution."""
    print("Generating data source documentation (table format)...")
    print("=" * 60)

    # Connect to database
    pool = await get_db_pool(min_size=1, max_size=5)

    try:
        # Fetch all sources
        sources = await get_data_sources(pool)

        if not sources:
            print("No data sources found in database.")
            print("Use manage_data_sources.py to add sources first.")
            return

        # Group by status
        confirmed = [s for s in sources if s['status'] == 'confirmed']
        potential = [s for s in sources if s['status'] == 'potential']
        rejected = [s for s in sources if s['status'] == 'rejected']
        blocked = [s for s in sources if s['status'] == 'blocked']

        # Ensure output directory exists
        OUTPUT_DIR.mkdir(exist_ok=True)

        # Generate confirmed sources file
        if confirmed:
            content = generate_markdown_file(
                confirmed,
                'confirmed',
                'Confirmed Ontario GIS Data Sources'
            )
            output_path = OUTPUT_DIR / 'confirmed_sources.md'
            with open(output_path, 'w') as f:
                f.write(content)
            print(f"Generated: {output_path} ({len(confirmed)} sources)")

        # Generate potential sources file
        if potential:
            content = generate_markdown_file(
                potential,
                'potential',
                'Potential Ontario GIS Data Sources'
            )
            output_path = OUTPUT_DIR / 'potential_sources.md'
            with open(output_path, 'w') as f:
                f.write(content)
            print(f"Generated: {output_path} ({len(potential)} sources)")

        # Generate rejected/blocked sources file if any exist
        excluded = rejected + blocked
        if excluded:
            content = generate_markdown_file(
                excluded,
                'excluded',
                'Excluded Ontario GIS Data Sources'
            )
            output_path = OUTPUT_DIR / 'excluded_sources.md'
            with open(output_path, 'w') as f:
                f.write(content)
            print(f"Generated: {output_path} ({len(excluded)} sources)")

        # Summary
        print("=" * 60)
        print(f"Total sources: {len(sources)}")
        print(f"  - Confirmed: {len(confirmed)}")
        print(f"  - Potential: {len(potential)}")
        print(f"  - Rejected/Blocked: {len(excluded)}")
        print("=" * 60)

    finally:
        await pool.close()


if __name__ == '__main__':
    asyncio.run(main())
