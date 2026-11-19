"""Opt-in migration checks against a disposable PostGIS server.

Set MIGRATION_TEST_DSN to an admin connection on a TEST server, then run:
  pytest -m integration tests/test_database_upgrade.py
Only randomly named databases created by these tests are migrated/dropped.
"""
import asyncio
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg
import pytest

from database.run_migrations import apply_migration, parse_migrations

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.integration


@pytest.fixture
async def migration_db():
    dsn = os.getenv('MIGRATION_TEST_DSN')
    if not dsn:
        pytest.skip('MIGRATION_TEST_DSN must point to a disposable PostGIS server')
    admin = await asyncpg.connect(dsn)
    name = 'migration_test_' + uuid.uuid4().hex
    await admin.execute(f'CREATE DATABASE {name}')
    parsed = urlsplit(dsn)
    env = {
        **os.environ, 'DB_HOST': parsed.hostname, 'DB_PORT': str(parsed.port or 5432),
        'DB_USER': parsed.username, 'DB_PASSWORD': parsed.password or 'test-only',
        'DB_NAME': name,
    }
    conn = await asyncpg.connect(**{
        'host': env['DB_HOST'], 'port': int(env['DB_PORT']),
        'user': env['DB_USER'], 'password': env['DB_PASSWORD'], 'database': name,
    })
    try:
        yield conn, env
    finally:
        await conn.close()
        await admin.execute(f'DROP DATABASE {name} WITH (FORCE)')
        await admin.close()


async def initialize(env):
    process = await asyncio.create_subprocess_exec(
        sys.executable, 'database/run_migrations.py', '--initialize',
        '--replay-unverified', cwd=ROOT, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    assert process.returncode == 0, output.decode()


@pytest.mark.asyncio
async def test_fresh_initialization_and_repeat(migration_db):
    conn, env = migration_db
    await initialize(env)
    assert await conn.fetchval("SELECT count(*) FROM data_sources WHERE status = 'confirmed'") == 36
    assert await conn.fetchval('SELECT max(migration_number) FROM schema_migrations') == 41
    hastings = await conn.fetchrow("SELECT required_attribution, license_url FROM data_sources WHERE source_key = 'hastings_county'")
    assert hastings['required_attribution'] == 'Contains information licensed under the Open Government License – The Corporation of the County of Hastings.'
    assert hastings['license_url'] == 'https://www.arcgis.com/sharing/rest/content/items/d1edbea63ca24e5bbd2363c3015bfdb0?f=pjson'
    assert await conn.fetchval('SELECT count(*) FROM schema_migrations WHERE checksum IS NULL') == 0
    for key in ('haldimand_county_addresses', 'middlesex_county_addresses', 'bruce_county_addresses'):
        assert await conn.fetchval('SELECT status FROM data_sources WHERE source_key = $1', key) == 'blocked'
    for key in ('fsa_boundaries', 'ontario_municipal_boundaries'):
        row = await conn.fetchrow('SELECT api_endpoint, download_format FROM data_sources WHERE source_key = $1', key)
        assert '/MapServer/14' in row['api_endpoint']
        assert row['download_format'] == 'geojson'
    before = await conn.fetch('SELECT * FROM data_sources ORDER BY source_key')
    await initialize(env)
    assert await conn.fetch('SELECT * FROM data_sources ORDER BY source_key') == before


@pytest.mark.asyncio
@pytest.mark.parametrize('previous_version', [37, 39])
async def test_upgrade_preserves_custom_sources_and_retires_haldimand(migration_db, previous_version):
    conn, env = migration_db
    await conn.execute((ROOT / 'database/schema.sql').read_text())
    migrations = parse_migrations((ROOT / 'database/migrations.sql').read_text())
    # Simulate a pre-restoration registry with tracked migrations through 37.
    for migration in migrations:
        if migration['number'] <= 37:
            assert await apply_migration(conn, migration)
    seed = (ROOT / 'database/seed_data_sources.sql').read_text()
    await conn.execute(seed.split('-- BEGIN RESTORED MUNICIPAL SOURCES')[0] + '\nCOMMIT;')
    for migration in migrations:
        if 37 < migration['number'] <= previous_version:
            assert await apply_migration(conn, migration)
    await conn.execute("""
        UPDATE data_sources SET api_endpoint = 'https://operator.example/custom',
            required_attribution = 'Operator attribution', license_url = 'https://operator.example/licence',
            status = 'blocked', record_count = 42, last_data_refresh = '2025-01-01'
        WHERE source_key IN ('toronto_addresses', 'fsa_boundaries', 'ontario_municipal_boundaries');
        INSERT INTO data_sources (source_key, source_name, status, license_type, license_url,
            required_attribution, license_notes, commercial_use_allowed, record_count, regions_covered)
        VALUES ('hastings_county', 'Hastings County', 'blocked', 'Operator licence',
            'https://operator.example/hastings-licence', 'Operator Hastings attribution',
            'Operator Hastings notes', FALSE, 42, ARRAY['Hastings'])
        ON CONFLICT (source_key) DO UPDATE SET status = 'blocked', license_type = 'Operator licence',
            license_url = 'https://operator.example/hastings-licence',
            required_attribution = 'Operator Hastings attribution', license_notes = 'Operator Hastings notes',
            commercial_use_allowed = FALSE, record_count = 42;
        DELETE FROM data_source_types WHERE data_source_id IN
            (SELECT id FROM data_sources WHERE source_key = 'guelph_addresses');
        DELETE FROM data_sources WHERE source_key = 'guelph_addresses';
        INSERT INTO properties (property_id, source_dataset, is_deleted, deleted_at) VALUES
            ('review-haldimand-1', 'haldimand_county', FALSE, NULL),
            ('review-haldimand-2', 'haldimand_county_addresses', FALSE, NULL),
            ('review-haldimand-old', 'haldimand_county', TRUE, '2025-01-01'),
            ('review-other', 'toronto', FALSE, NULL);
        INSERT INTO dataset_metadata (dataset_name, record_count) VALUES
            ('haldimand_county', 2), ('haldimand_county_addresses', 1);
        INSERT INTO geocode_cache (address_hash, matched_property_id) VALUES
            ('review-haldimand', 'review-haldimand-1'), ('review-other', 'review-other');
    """)
    custom_keys = ['toronto_addresses', 'fsa_boundaries', 'ontario_municipal_boundaries', 'hastings_county']
    query = 'SELECT * FROM data_sources WHERE source_key = ANY($1::text[]) ORDER BY source_key'
    before = await conn.fetch(query, custom_keys)
    await initialize(env)
    assert await conn.fetch(query, custom_keys) == before
    assert await conn.fetchval("SELECT count(*) FROM data_sources WHERE source_key = 'guelph_addresses'") == 1
    assert await conn.fetchval("SELECT count(*) FROM properties WHERE source_dataset LIKE 'haldimand%' AND NOT is_deleted") == 0
    assert await conn.fetchval("SELECT count(*) FROM properties WHERE source_dataset LIKE 'haldimand%'") == 3
    assert str(await conn.fetchval("SELECT deleted_at FROM properties WHERE property_id = 'review-haldimand-old'")) == '2025-01-01 00:00:00'
    assert await conn.fetchval("SELECT count(*) FROM dataset_metadata WHERE dataset_name LIKE 'haldimand%' AND (active OR record_count <> 0)") == 0
    assert await conn.fetchval("SELECT count(*) FROM geocode_cache WHERE address_hash = 'review-haldimand'") == 0
    assert await conn.fetchval("SELECT count(*) FROM geocode_cache WHERE address_hash = 'review-other'") == 1
    assert await conn.fetchval("SELECT is_deleted FROM properties WHERE property_id = 'review-other'") is False
    await initialize(env)
    assert await conn.fetchval('SELECT max(migration_number) FROM schema_migrations') == 41


@pytest.mark.parametrize('nar', [False, True])
async def test_address_identity_with_real_sql_before_limit(migration_db, monkeypatch, nar):
    """Single wrong candidate, absent values and >10 candidates in both tiers."""
    from engine import database, geocoder
    from engine.layered_matcher import LayeredMatcher, LayeredNARMatcher
    conn, env = migration_db
    await initialize(env)

    async def query(sql, params=None):
        return await conn.fetch(sql, *(params or []))
    monkeypatch.setattr(database, 'query', query)
    matcher = LayeredNARMatcher() if nar else LayeredMatcher()
    components = {'street_number': '68', 'street_name': 'Main',
                  'postal_code': 'M1A1A1', 'city': 'Toronto',
                  'unit_no': '99', 'civic_no_suffix': 'B'}

    async def insert(unit, suffix):
        if nar:
            await conn.execute('''
                INSERT INTO nar_addresses (addr_guid, civic_no, civic_no_suffix,
                    apt_no_label, official_street_name, csd_eng_name, prov_code,
                    mail_postal_code, latitude, longitude)
                VALUES ($1, '68', $2, $3, 'Main', 'Toronto', 35, 'M1A1A1', 44, -79)
            ''', uuid.uuid4(), suffix, unit)
        else:
            await conn.execute('''
                INSERT INTO properties (property_id, street_number, civic_no_suffix,
                    unit_no, street_name, city, postal_code, fsa_code,
                    latitude, longitude, source_dataset, pin)
                VALUES ($1, '68', $2, $3, 'Main', 'Toronto', 'M1A1A1', 'M1A',
                        44, -79, 'toronto', 123)
            ''', str(uuid.uuid4()), suffix, unit)

    # A single contradictory row must not become an exact or fallback match.
    await insert('1', 'A')
    assert await matcher.match(components) is None
    if not nar:
        assert await geocoder.lookup_by_pin({**components, 'pin': '123'}) is None
    # Missing identity is not evidence of the supplied unit/suffix either.
    await insert(None, None)
    assert await matcher.match(components) is None
    # Neither independently matching field may compensate for the other conflict.
    await insert('99', 'A')
    await insert('1', 'B')
    assert await matcher.match(components) is None
    for i in range(20):
        await insert(str(i), 'A')
    # The right row is beyond the old LIMIT, and normalization is intentional.
    await insert(' 99 ', ' b ')
    result = await matcher.match(components)
    assert result is not None
    assert result['match_layer'] == 0
    assert '99' in result['matched_address']
    if not nar:
        result = await geocoder.lookup_by_pin({**components, 'pin': '123'})
        assert result is not None and '99' in result['matched_address']
