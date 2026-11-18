"""Haldimand must not be selected by normal ETL until parcel handling is approved."""
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]


def exclusion(sql):
    return sql.split('-- BEGIN HALDIMAND DOWNLOAD EXCLUSION')[1].split('-- END HALDIMAND DOWNLOAD EXCLUSION')[0]


def test_fresh_seed_and_upgrade_disable_haldimand_without_changing_licence_or_audit():
    from database.run_migrations import parse_migrations
    seed = (ROOT / 'database/seed_data_sources.sql').read_text()
    migrations = parse_migrations((ROOT / 'database/migrations.sql').read_text())
    migration = next(m for m in migrations if m['number'] == 39)
    assert exclusion(seed) == exclusion(migration['sql'])
    assert "status = 'blocked'" in exclusion(seed)
    assert "source_key = 'haldimand_county_addresses'" in exclusion(seed)
    assert 'commercial_use_allowed' not in exclusion(seed)
    assert 'DELETE' not in exclusion(seed)
    assert 'import_history' not in exclusion(seed)
    assert seed.index('-- BEGIN HALDIMAND DOWNLOAD EXCLUSION') > seed.index('-- END RESTORED MUNICIPAL SOURCES')


def test_upgrade_retires_existing_haldimand_rows_and_cached_matches():
    from database.run_migrations import parse_migrations
    migrations = parse_migrations((ROOT / 'database/migrations.sql').read_text())
    sql = next(m['sql'] for m in migrations if m['number'] == 40)
    assert "('haldimand_county', 'haldimand_county_addresses')" in sql
    assert 'UPDATE properties SET is_deleted = TRUE, deleted_at = NOW()' in sql
    assert 'AND is_deleted IS NOT TRUE' in sql
    assert 'UPDATE dataset_metadata SET active = FALSE, record_count = 0' in sql
    assert 'DELETE FROM geocode_cache WHERE matched_property_id IN' in sql
    assert 'DELETE FROM properties' not in sql
    assert 'import_history' not in sql


@pytest.mark.asyncio
async def test_import_order_selects_only_confirmed_sources():
    from data_sourcing.setup_data import get_import_order
    conn = AsyncMock()
    conn.fetch.return_value = [{'source_key': 'guelph_addresses'}]
    assert await get_import_order(conn) == ['guelph_addresses']
    assert "WHERE status = 'confirmed'" in conn.fetch.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['haldimand_county', 'haldimand_county_addresses'])
async def test_direct_import_rejects_both_haldimand_keys(source):
    from data_sourcing.db_utils import ensure_source_import_allowed
    conn = AsyncMock()
    conn.fetch.return_value = [{'source_key': 'haldimand_county_addresses', 'status': 'blocked'}]
    with pytest.raises(RuntimeError, match='import is prohibited'):
        await ensure_source_import_allowed(conn, source)


@pytest.mark.asyncio
async def test_explicit_orchestrated_download_cannot_override_block():
    from data_sourcing import setup_data
    conn = AsyncMock()
    source = {'source_key': 'haldimand_county_addresses', 'status': 'blocked'}
    with patch('sys.argv', ['setup_data', '--source', source['source_key'], '--force', '--accept-non-commercial']), \
         patch.object(setup_data, 'get_db_connection', AsyncMock(return_value=conn)), \
         patch.object(setup_data, 'check_data_sources_seeded', AsyncMock(return_value=True)), \
         patch.object(setup_data, 'get_source_info', AsyncMock(return_value=source)), \
         patch.object(setup_data, 'run_download_script') as download:
        with pytest.raises(SystemExit) as error:
            await setup_data.main()
        assert error.value.code == 1
        download.assert_not_called()
