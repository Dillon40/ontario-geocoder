"""Keep fresh seeds, upgrade migrations and downloader configuration in sync."""
import ast
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_RESTORED = {
    'barrie_addresses': 'barrie_addresses',
    'brantford': 'brantford_addresses',
    'burlington': 'burlington_addresses',
    'cornwall': 'cornwall',
    'kawartha_lakes_addresses': 'kawartha_lakes_addresses',
    'kingston_addresses': 'kingston_addresses',
    'london_addresses': 'london_addresses',
    'peterborough': 'peterborough',
    'sarnia': 'sarnia',
    'thunder_bay': 'thunder_bay',
    'windsor_addresses': 'windsor_addresses',
    'muskoka_addresses': 'muskoka_addresses',
    'dufferin_county_addresses': 'dufferin_county_addresses',
    'durham_region_addresses': 'durham_region_addresses',
    'elgin_county_addresses': 'elgin_county_addresses',
    'essex_county_tecumseh': 'essex_county_addresses',
    'frontenac_county': 'frontenac_county',
    'greater_sudbury': 'greater_sudbury',
    'haldimand_county_addresses': 'haldimand_county',
    'hastings_county': 'hastings_county',
    'huron_county': 'huron_county',
    'leeds_and_grenville': 'leeds_grenville_addresses',
    'niagara_region_addresses': 'niagara_region_addresses',
    'norfolk_county': 'norfolk_county_addresses',
    'peel_region_addresses': 'peel_region_addresses',
    'waterloo_region_addresses': 'waterloo_region_addresses',
    'milton': 'milton_addresses',
    'oakville': 'oakville_addresses',
    'parry_sound_district_addresses': 'parry_sound_district_addresses',
    'york_region_addresses': 'york_region_addresses',
}
BASE_KEYS = {
    'nar_ontario', 'toronto_addresses', 'guelph_addresses', 'ottawa_addresses',
    'hamilton_addresses', 'fsa_boundaries', 'ontario_municipal_boundaries',
    'bruce_county_addresses', 'middlesex_county_addresses',
}


def restored_block(sql):
    start = sql.index('-- BEGIN RESTORED MUNICIPAL SOURCES')
    end = sql.index('-- END RESTORED MUNICIPAL SOURCES', start)
    return sql[start:end]


def test_seed_and_upgrade_restore_the_same_complete_inventory():
    from database.run_migrations import parse_migrations
    seed = (ROOT / 'database/seed_data_sources.sql').read_text()
    migrations = parse_migrations((ROOT / 'database/migrations.sql').read_text())
    migration = next(m for m in migrations if m['number'] == 38)
    block = restored_block(seed)
    assert block == restored_block(migration['sql'])
    keys = set(re.findall(r"^    -- ([a-z_]+)$", block, re.M))
    assert keys == EXPECTED_RESTORED.keys()
    assert len(keys) == 30
    assert 'ON CONFLICT (source_key) DO NOTHING' in block
    assert 'ON CONFLICT DO NOTHING' in block  # address-type links
    assert 'UPDATE data_sources' not in block
    assert 'DELETE ' not in block
    assert not keys & BASE_KEYS
    inventory = (ROOT / 'data_sourcing/docs/confirmed_sources.md').read_text()
    documented = set(re.findall(r'`([a-z_]+)`', inventory.split('## Access Information')[0]))
    assert documented == keys | (BASE_KEYS - {'bruce_county_addresses', 'middlesex_county_addresses'})


def test_restored_sources_have_full_endpoints_mapping_aliases_and_matching_paths():
    block = restored_block((ROOT / 'database/seed_data_sources.sql').read_text())
    mappings = json.loads((ROOT / 'config/dataset-mappings.json').read_text())
    module = ast.parse((ROOT / 'data_sourcing/downloaders/arcgis_downloader.py').read_text())
    endpoints = next(ast.literal_eval(n.value) for n in module.body
                     if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == 'STANDARD_SOURCES'
                             for t in n.targets))
    download_aliases = {
        'brantford': 'brantford_addresses', 'burlington': 'burlington_addresses',
        'milton': 'milton_addresses', 'oakville': 'oakville_addresses',
    }
    for key, mapping in EXPECTED_RESTORED.items():
        row = block.split(f'    -- {key}\n', 1)[1].split('\n    -- ', 1)[0]
        endpoint = endpoints[download_aliases.get(key, key)]
        assert f"'{endpoint}'" in row
        assert f"'{mapping}', 'data_sourcing/data_sources/{key}/{key}.geojson', FALSE" in row
        assert "'import_production_data.py'" in row
        assert "'confirmed'" in row and 'TRUE' in row
        assert mappings[mapping]['field_mappings']
        assert ('pending per-licence review' in row or 'Contains information licensed' in row
                or 'City credit is optional, not mandatory' in row)


@pytest.mark.asyncio
async def test_initialize_seeds_base_rows_even_after_data_migration_inserted_sources():
    from database.run_migrations import seed_missing_base_sources
    conn = AsyncMock()
    conn.fetch.return_value = [{'source_key': key} for key in EXPECTED_RESTORED]
    await seed_missing_base_sources(conn)
    conn.execute.assert_awaited_once()
    seed = conn.execute.call_args.args[0]
    assert restored_block(seed)
    assert 'ON CONFLICT (source_key) DO UPDATE' not in seed
    updates = re.findall(r'UPDATE data_sources SET[^;]+;', seed)
    assert not any("WHERE source_key = 'fsa_boundaries'" in sql for sql in updates)
    assert not any("WHERE source_key = 'ontario_municipal_boundaries'" in sql for sql in updates)


@pytest.mark.asyncio
async def test_initialize_does_not_overwrite_an_already_seeded_registry():
    from database.run_migrations import seed_missing_base_sources
    conn = AsyncMock()
    conn.fetch.return_value = [{'source_key': key} for key in BASE_KEYS | EXPECTED_RESTORED.keys()]
    await seed_missing_base_sources(conn)
    conn.execute.assert_not_awaited()
