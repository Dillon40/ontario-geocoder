#!/usr/bin/env python3
"""
Generate suggested dataset-mappings.json configs for sources that don't have them.

Usage:
    DB_HOST=localhost uv run python data_sourcing/scripts/generate_missing_mappings.py
"""
import argparse
import subprocess
import asyncio
import os
import sys
from pathlib import Path

# Add project root to path for db_utils import
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from data_sourcing.db_utils import get_db_connection

async def main():
    conn = await get_db_connection()

    rows = await conn.fetch('''
        SELECT source_key FROM data_sources
        WHERE status = 'confirmed'
        AND import_script = 'import_production_data.py'
        AND dataset_mapping_key IS NULL
        ORDER BY source_key
    ''')
    await conn.close()

    if not rows:
        print("All confirmed sources have dataset mappings configured!")
        return

    print(f"Found {len(rows)} sources without dataset mappings:\n")
    for i, row in enumerate(rows, 1):
        print(f"  {i}. {row['source_key']}")

    print(f"\n{'='*60}")
    print("Running dry-run import wizard for each source...")
    print("Copy the suggested configs into config/dataset-mappings.json")
    print('='*60)

    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    for row in rows:
        source_key = row['source_key']
        print(f'\n{"="*60}')
        print(f'Source: {source_key}')
        print('='*60)

        result = subprocess.run(
            [
                'uv', 'run', 'python',
                'data_sourcing/importers/import_production_data.py',
                '--from-source', source_key, '--dry-run'
            ],
            cwd=project_root,
            env={**os.environ, 'DB_HOST': os.environ.get('DB_HOST', 'localhost')}
        )

        if result.returncode != 0:
            print(f"  [Warning] Failed to analyze {source_key}")

        # Pause between sources for readability
        print("\n" + "-"*60)
        response = input("Press Enter to continue to next source (or 'q' to quit): ")
        if response.lower() == 'q':
            print("Exiting...")
            sys.exit(0)

def parse_args():
    parser = argparse.ArgumentParser(
        description='Generate suggested dataset-mappings.json configs for confirmed '
                     'data sources that do not have one yet, by running the import '
                     'wizard in dry-run mode for each. Connects to the database.'
    )
    return parser.parse_args()


if __name__ == '__main__':
    parse_args()
    asyncio.run(main())
