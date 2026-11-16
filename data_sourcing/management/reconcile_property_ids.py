"""Safely retire legacy IDs after switching a source to stable address-level IDs.

Normal fresh installs never need this. Existing deployments must first force-import
with the new mapping. This tool verifies the exact source file hash, every new ID
and its address before retiring any old IDs. It never lowers stale-delete guards.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from data_sourcing.db_utils import get_db_connection
from data_sourcing.importers.import_utils import PropertyIdGenerator

ROOT = Path(__file__).resolve().parents[2]

def expected_ids(source, file_path):
    config = json.loads((ROOT / 'config/dataset-mappings.json').read_text())[source]
    mapping = config['field_mappings']
    if not mapping.get('property_id'):
        raise ValueError('A stable upstream property_id mapping is required')
    features = json.loads(Path(file_path).read_text())['features']
    expected = {}
    for i, feature in enumerate(features, 1):
        props = feature['properties']
        raw_id = props.get(mapping['property_id'])
        if raw_id is None or str(raw_id).strip() == '':
            raise ValueError('Every source feature must have an ID')
        key = PropertyIdGenerator.generate(source, {'property_id': raw_id}, i)
        if key in expected:
            raise ValueError(f'Duplicate upstream ID: {key}')
        address = props.get(mapping.get('full_address'))
        expected[key] = address
    if not expected:
        raise ValueError('Refusing an empty input')
    return expected

async def reconcile(source, file_path, apply=False):
    expected = expected_ids(source, file_path)
    digest = hashlib.sha256(Path(file_path).read_bytes()).hexdigest()
    conn = await get_db_connection()
    try:
        async with conn.transaction():
            # Block concurrent property writes during verification + retirement.
            await conn.execute('LOCK TABLE properties IN SHARE ROW EXCLUSIVE MODE')
            source_count = await conn.fetchval('''SELECT record_count FROM data_sources
                WHERE source_key=$1 OR dataset_mapping_key=$1''', source)
            if source_count != len(expected):
                raise ValueError('File count must match verified download metadata; re-download with --force first')
            history = await conn.fetchrow('''SELECT * FROM import_history
                WHERE source_key=$1 ORDER BY id DESC LIMIT 1''', source)
            if not history or history['file_hash'] != digest or history['rows_processed'] != len(expected) or history['rows_failed']:
                raise ValueError('Latest import must match this complete file with zero failed rows')
            await conn.execute('CREATE TEMP TABLE expected_property_ids (property_id text PRIMARY KEY, address text) ON COMMIT DROP')
            await conn.copy_records_to_table('expected_property_ids', records=list(expected.items()))
            matched = await conn.fetchval('''SELECT count(*) FROM expected_property_ids e
                JOIN properties p USING (property_id)
                WHERE p.source_dataset=$1 AND NOT p.is_deleted
                  AND (e.address IS NULL OR p.full_address=e.address)''', source)
            if matched != len(expected):
                raise ValueError(f'Only {matched}/{len(expected)} new address identities verified; nothing retired')
            stale = await conn.fetchval('''SELECT count(*) FROM properties p
                WHERE source_dataset=$1 AND NOT is_deleted
                  AND NOT EXISTS (SELECT 1 FROM expected_property_ids e WHERE e.property_id=p.property_id)''', source)
            print(f'Verified {matched} new identities; {stale} legacy IDs to retire; apply={apply}')
            if apply:
                await conn.execute('''UPDATE properties p SET is_deleted=true, deleted_at=now(), deleted_by_import_id=$2
                    WHERE source_dataset=$1 AND NOT is_deleted
                      AND NOT EXISTS (SELECT 1 FROM expected_property_ids e WHERE e.property_id=p.property_id)''', source, history['id'])
    finally:
        await conn.close()

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True)
    parser.add_argument('--file',required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    asyncio.run(reconcile(args.source,args.file,args.apply))
