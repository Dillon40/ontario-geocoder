#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "asyncpg",
#   "python-dotenv",
# ]
# ///

"""
Generate markdown documentation from municipality_aliases database table.
Creates a reference document of municipality name mappings grouped by canonical name.
"""
import sys
import asyncio
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from dotenv import load_dotenv
import asyncpg

# Add project root to path for db_utils import
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from data_sourcing.db_utils import get_db_pool

# Load environment variables
load_dotenv()

OUTPUT_DIR = Path(__file__).parent.parent / 'docs'


async def get_municipality_aliases(pool: asyncpg.Pool) -> list:
    """Fetch all municipality aliases with address counts."""
    query = '''
        SELECT
            alias,
            normalized_alias,
            canonical_name,
            alias_type,
            address_count,
            notes
        FROM municipality_aliases
        WHERE canonical_name IS NOT NULL
        ORDER BY canonical_name, alias_type, alias
    '''
    return await pool.fetch(query)


async def get_alias_statistics(pool: asyncpg.Pool) -> dict:
    """Get summary statistics."""
    query = '''
        SELECT
            COUNT(*) as total_aliases,
            COUNT(DISTINCT canonical_name) as unique_municipalities,
            SUM(address_count) as total_addresses,
            COUNT(*) FILTER (WHERE alias_type = 'pre_amalgamation') as pre_amalgamation_count,
            COUNT(*) FILTER (WHERE alias_type = 'bilingual') as bilingual_count,
            COUNT(*) FILTER (WHERE alias_type = 'variation') as variation_count,
            COUNT(*) FILTER (WHERE alias_type = 'canonical') as canonical_count
        FROM municipality_aliases
        WHERE canonical_name IS NOT NULL
    '''
    return dict(await pool.fetchrow(query))


def format_alias_type(alias_type: str) -> str:
    """Format alias type for display."""
    type_labels = {
        'pre_amalgamation': 'Pre-Amalgamation',
        'bilingual': 'Bilingual',
        'variation': 'Variation',
        'canonical': 'Canonical',
    }
    return type_labels.get(alias_type, alias_type.title() if alias_type else 'Unknown')


def format_number(n) -> str:
    """Format number with commas."""
    if n is None:
        return '0'
    return f"{n:,}"


def generate_markdown(aliases: list, stats: dict) -> str:
    """Generate complete markdown documentation."""
    lines = []

    # Header
    lines.append("# Municipality Aliases Reference")
    lines.append("")
    lines.append(f"*Auto-generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}*")
    lines.append("")

    # Summary statistics
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- **Total Aliases:** {format_number(stats.get('total_aliases'))}")
    lines.append(f"- **Unique Municipalities:** {format_number(stats.get('unique_municipalities'))}")
    lines.append(f"- **Total Addresses Covered:** {format_number(stats.get('total_addresses'))}")
    lines.append("")
    lines.append("### Alias Types")
    lines.append("")
    lines.append(f"- Pre-Amalgamation: {format_number(stats.get('pre_amalgamation_count'))} (e.g., Etobicoke → Toronto)")
    lines.append(f"- Bilingual: {format_number(stats.get('bilingual_count'))} (e.g., Grand Sudbury → Greater Sudbury)")
    lines.append(f"- Variation: {format_number(stats.get('variation_count'))} (e.g., spelling variants)")
    lines.append(f"- Canonical: {format_number(stats.get('canonical_count'))} (base municipality names)")
    lines.append("")

    # About section
    lines.append("## About This Document")
    lines.append("")
    lines.append("This document lists municipality name mappings used by the geocoder transform pipeline.")
    lines.append("When importing address data, the pipeline maps variant municipality names to their canonical form.")
    lines.append("Original names are preserved in the `city_original` field for reference.")
    lines.append("")
    lines.append("Key examples:")
    lines.append("- **Toronto 1998 Amalgamation:** Etobicoke, Scarborough, North York, East York, York → Toronto")
    lines.append("- **Ottawa 2001 Amalgamation:** Nepean, Kanata, Gloucester, Vanier → Ottawa")
    lines.append("- **Hamilton 2001 Amalgamation:** Dundas, Ancaster, Stoney Creek, Flamborough → Hamilton")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Group aliases by canonical name
    by_canonical = defaultdict(list)
    for alias in aliases:
        canonical = alias['canonical_name']
        by_canonical[canonical].append(alias)

    # Sort canonicals by total address count (descending)
    sorted_canonicals = sorted(
        by_canonical.items(),
        key=lambda x: sum(a['address_count'] or 0 for a in x[1]),
        reverse=True
    )

    # Municipality sections
    lines.append("## Municipalities")
    lines.append("")

    for canonical, alias_list in sorted_canonicals:
        total_addresses = sum(a['address_count'] or 0 for a in alias_list)

        lines.append(f"### {canonical}")
        lines.append("")
        lines.append(f"**Total Addresses:** {format_number(total_addresses)}")
        lines.append("")

        # Table header
        lines.append("| Alias | Type | Address Count |")
        lines.append("|-------|------|---------------|")

        # Sort aliases: canonical first, then by address count
        sorted_aliases = sorted(
            alias_list,
            key=lambda a: (a['alias_type'] != 'canonical', -(a['address_count'] or 0))
        )

        for alias in sorted_aliases:
            alias_name = alias['alias']
            alias_type = format_alias_type(alias['alias_type'])
            count = format_number(alias['address_count'])

            # Mark canonical entry
            if alias['alias_type'] == 'canonical':
                alias_name = f"**{alias_name}** (canonical)"

            lines.append(f"| {alias_name} | {alias_type} | {count} |")

        lines.append("")

    return '\n'.join(lines)


async def main():
    """Main execution."""
    print("Generating municipality alias documentation...")
    print("=" * 60)

    # Connect to database
    pool = await get_db_pool(min_size=1, max_size=5)

    try:
        # Fetch aliases and statistics
        aliases = await get_municipality_aliases(pool)
        stats = await get_alias_statistics(pool)

        if not aliases:
            print("No municipality aliases found in database.")
            print("Run extract_municipality_aliases.py to populate the table first.")
            return

        # Ensure output directory exists
        OUTPUT_DIR.mkdir(exist_ok=True)

        # Generate documentation
        content = generate_markdown(aliases, stats)
        output_path = OUTPUT_DIR / 'municipality-aliases.md'
        with open(output_path, 'w') as f:
            f.write(content)

        print(f"Generated: {output_path}")
        print("")
        print("Summary:")
        print(f"  - Total aliases: {stats.get('total_aliases', 0)}")
        print(f"  - Unique municipalities: {stats.get('unique_municipalities', 0)}")
        print(f"  - Total addresses: {format_number(stats.get('total_addresses'))}")
        print("=" * 60)

    finally:
        await pool.close()


if __name__ == '__main__':
    asyncio.run(main())
