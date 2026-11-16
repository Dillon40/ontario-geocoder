"""Offline safety coverage for the operator ETL verification CLI."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import sys

import pytest

from data_sourcing.management import verify_etl_pipeline as verifier


@pytest.fixture(autouse=True)
def isolated_services(monkeypatch):
    monkeypatch.setattr(verifier, 'get_db_config', lambda: {
        'database': 'geocoder', 'host': 'localhost', 'port': 5432,
        'user': 'geocoder', 'password': 'test_only',
    })
    monkeypatch.setattr(verifier, 'get_psql_env', lambda: {'PGPASSWORD': 'test_only'})
    # No test in this module may contact real services or run PostgreSQL tools.
    monkeypatch.setattr(verifier.asyncpg, 'connect', AsyncMock(
        side_effect=AssertionError('Unexpected real database access')))
    monkeypatch.setattr(verifier.asyncpg, 'create_pool', AsyncMock(
        side_effect=AssertionError('Unexpected real pool access')))
    run = MagicMock(return_value=SimpleNamespace(returncode=0, stdout='ok', stderr=''))
    monkeypatch.setattr(verifier.subprocess, 'run', run)
    return run


@pytest.mark.parametrize('name', [
    'geocoder', 'postgres', 'template0', 'template1', '', '-geocoder_verify',
    'postgresql://localhost/geocoder', 'name with spaces', 'a' * 64, 'Geocoder',
])
def test_rejects_production_system_and_ambiguous_targets(name):
    with pytest.raises(ValueError, match='distinct, disposable'):
        verifier.validate_verify_db(name)


@pytest.mark.parametrize('name', ['geocoder_verify', 'disposable_1', 'a' * 63])
def test_accepts_distinct_disposable_targets(name):
    verifier.validate_verify_db(name)


@pytest.mark.parametrize('function,args', [
    ('create_verify_db', ('geocoder',)),
    ('init_verify_db', ('geocoder',)),
    ('run_etl', (SimpleNamespace(), 'geocoder')),
    ('run_postal_codes_import', ('geocoder',)),
    ('copy_dependency_tables', ('geocoder',)),
    ('run_etl_single_source', ('nar_ontario', 'geocoder', False)),
    ('cleanup_verify_db', ('geocoder',)),
])
def test_sync_entrypoints_guard_production_even_without_main(function, args, isolated_services):
    with pytest.raises(ValueError):
        getattr(verifier, function)(*args)
    isolated_services.assert_not_called()


@pytest.mark.parametrize('function,args', [
    ('ensure_boundary_tables', ('geocoder',)),
    ('sync_data_sources', ('geocoder',)),
    ('run_setup_transformers', ('geocoder',)),
    ('run_etl_resume', (SimpleNamespace(), 'geocoder')),
    ('compare_databases', (SimpleNamespace(verify_db_name='geocoder'), verifier.TimingInfo())),
])
async def test_async_entrypoints_guard_production(function, args, isolated_services):
    with pytest.raises(ValueError):
        await getattr(verifier, function)(*args)
    verifier.asyncpg.connect.assert_not_awaited()
    verifier.asyncpg.create_pool.assert_not_awaited()
    isolated_services.assert_not_called()


async def test_preflight_rejects_production_before_connecting(monkeypatch):
    connect = AsyncMock()
    monkeypatch.setattr(verifier, 'connect_to_db', connect)
    assert not await verifier.preflight_checks(SimpleNamespace(verify_db_name='geocoder'))
    connect.assert_not_awaited()


async def test_preflight_rejects_unknown_or_blocked_source_and_closes_connection(monkeypatch):
    conn = AsyncMock()
    conn.fetchval.side_effect = [42, 'blocked']
    monkeypatch.setattr(verifier, 'connect_to_db', AsyncMock(return_value=conn))
    exists = AsyncMock()
    monkeypatch.setattr(verifier, 'db_exists', exists)
    args = SimpleNamespace(verify_db_name='geocoder_verify', source='middlesex_county_addresses')
    assert not await verifier.preflight_checks(args)
    conn.close.assert_awaited_once()
    exists.assert_not_awaited()


def test_initializer_uses_tracked_runner_and_fail_fast_sql(isolated_services):
    assert verifier.init_verify_db('geocoder_verify')
    calls = isolated_services.call_args_list
    psql_calls = [c for c in calls if c.args[0][0] == 'psql']
    assert all('ON_ERROR_STOP=1' in c.args[0] for c in psql_calls)
    runner = next(c for c in calls if 'run_migrations.py' in ' '.join(c.args[0]))
    assert runner.args[0][0] == sys.executable
    assert runner.args[0][-2:] == ['--initialize', '--replay-unverified']
    assert runner.kwargs['env']['DB_NAME'] == 'geocoder_verify'
    assert not any('-f' in c.args[0] for c in psql_calls)


def test_initializer_fails_on_runner_error_without_error_keyword(isolated_services):
    isolated_services.side_effect = [
        SimpleNamespace(returncode=0, stdout='', stderr=''),
        SimpleNamespace(returncode=2, stdout='', stderr='connection refused'),
    ]
    assert not verifier.init_verify_db('geocoder_verify')
    assert isolated_services.call_count == 2


@pytest.mark.parametrize('stage', ['dump', 'restore'])
def test_dependency_copy_does_not_swallow_failures(stage, isolated_services):
    ok = SimpleNamespace(returncode=0, stdout='table dump', stderr='')
    bad = SimpleNamespace(returncode=1, stdout='', stderr='connection refused')
    isolated_services.side_effect = [bad] if stage == 'dump' else [ok, bad]
    with pytest.raises(RuntimeError, match=stage):
        verifier.copy_dependency_tables('geocoder_verify')


def test_cleanup_does_not_report_success_on_drop_failure(isolated_services):
    isolated_services.return_value = SimpleNamespace(returncode=1, stderr='permission denied')
    with pytest.raises(RuntimeError, match='Failed to drop'):
        verifier.cleanup_verify_db('geocoder_verify')


@pytest.mark.parametrize('function', ['run_etl', 'run_etl_single_source', 'run_postal_codes_import'])
def test_child_imports_use_active_interpreter(function, monkeypatch, tmp_path, isolated_services):
    args = SimpleNamespace(source=None, full_download=False)
    if function == 'run_etl':
        assert verifier.run_etl(args, 'geocoder_verify')
    elif function == 'run_etl_single_source':
        assert verifier.run_etl_single_source('nar_ontario', 'geocoder_verify', False)
    else:
        postal_file = tmp_path / 'postal.sql'
        postal_file.write_text('-- test')
        monkeypatch.setattr(verifier, 'POSTAL_DATA_FILE', postal_file)
        assert verifier.run_postal_codes_import('geocoder_verify')
    call = isolated_services.call_args
    assert call.args[0][0] == sys.executable
    assert call.kwargs['env']['DB_NAME'] == 'geocoder_verify'


@pytest.mark.parametrize('failure', ['missing_fsa', 'missing_municipalities', 'fsa_setup', 'alias_setup'])
async def test_setup_transformer_failures_are_fatal_and_close_pool(failure, monkeypatch):
    from data_sourcing.transformers.fsa_municipality_setup import FSAMunicipalitySetup
    from data_sourcing.transformers.municipality_alias_setup import MunicipalityAliasSetup
    conn = AsyncMock()
    conn.fetchval.side_effect = [int(failure != 'missing_fsa'), int(failure != 'missing_municipalities')]
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.close = AsyncMock()
    monkeypatch.setattr(verifier.asyncpg, 'create_pool', AsyncMock(return_value=pool))
    for cls, label in [(FSAMunicipalitySetup, 'fsa_setup'), (MunicipalityAliasSetup, 'alias_setup')]:
        result = SimpleNamespace(status='error' if failure == label else 'success',
                                 reason='test failure', rows_affected=0, duration_seconds=0)
        monkeypatch.setattr(cls, 'ensure_setup', AsyncMock(return_value=result))
    with pytest.raises(RuntimeError):
        await verifier.run_setup_transformers('geocoder_verify')
    pool.close.assert_awaited_once()


async def test_resume_replays_all_confirmed_sources_without_blanket_deletes(monkeypatch):
    monkeypatch.setattr(verifier, 'get_all_confirmed_sources', AsyncMock(
        return_value=['nar_ontario', 'guelph_addresses']))
    run = MagicMock(return_value=True)
    monkeypatch.setattr(verifier, 'run_etl_single_source', run)
    assert await verifier.run_etl_resume(
        SimpleNamespace(source=None, full_download=False), 'geocoder_verify')
    assert [c.args[0] for c in run.call_args_list] == ['nar_ontario', 'guelph_addresses']
    verifier.asyncpg.connect.assert_not_awaited()


@pytest.mark.parametrize('sources,source', [([], None), (['guelph_addresses'], 'blocked_source')])
async def test_resume_rejects_empty_or_ineligible_sources(sources, source, monkeypatch):
    monkeypatch.setattr(verifier, 'get_all_confirmed_sources', AsyncMock(return_value=sources))
    run = MagicMock()
    monkeypatch.setattr(verifier, 'run_etl_single_source', run)
    assert not await verifier.run_etl_resume(
        SimpleNamespace(source=source, full_download=False), 'geocoder_verify')
    run.assert_not_called()


@pytest.mark.parametrize('quick', [False, True])
@pytest.mark.parametrize('exists', [[False], [True, False]])
async def test_missing_required_table_is_a_failure(quick, exists, monkeypatch):
    monkeypatch.setattr(verifier, 'table_exists', AsyncMock(side_effect=exists))
    config = verifier.TableConfig('properties', ['property_id'], set())
    if quick:
        result = await verifier.compare_table_quick(AsyncMock(), AsyncMock(), config, None)
    else:
        result = await verifier.compare_table_full(AsyncMock(), AsyncMock(), config, None, False)
    assert not result.passed and not result.skipped
    assert 'Table missing' in result.error


@pytest.mark.parametrize('quick', [False, True])
@pytest.mark.parametrize('columns,only_prod', [([], []), ([('property_id', 'text')], ['missing_column'])])
async def test_absent_or_differing_schema_cannot_pass(quick, columns, only_prod, monkeypatch):
    monkeypatch.setattr(verifier, 'table_exists', AsyncMock(return_value=True))
    monkeypatch.setattr(verifier, 'get_common_data_columns', AsyncMock(
        return_value=(columns, only_prod, [])))
    prod = AsyncMock()
    verify = AsyncMock()
    for conn in (prod, verify):
        conn.fetch.return_value = [{'row_count': 0, 'data_hash': None}]
    config = verifier.TableConfig('properties', ['property_id'], set())
    if quick:
        result = await verifier.compare_table_quick(prod, verify, config, None)
    else:
        result = await verifier.compare_table_full(prod, verify, config, None, False)
    assert not result.passed and not result.skipped and result.error


async def test_mismatched_column_types_cannot_be_silently_compared(monkeypatch):
    monkeypatch.setattr(verifier, 'get_data_columns', AsyncMock(side_effect=[
        [('property_id', 'text')], [('property_id', 'int8')],
    ]))
    with pytest.raises(ValueError, match='types differ'):
        await verifier.get_common_data_columns(AsyncMock(), AsyncMock(), 'properties', set())


async def test_single_nar_source_cannot_skip_its_empty_required_tables(monkeypatch):
    prod = AsyncMock()
    prod.fetchval.return_value = 'nar_ontario'
    verify = AsyncMock()
    verify.fetchval.return_value = 0
    monkeypatch.setattr(verifier, 'connect_to_db', AsyncMock(side_effect=[prod, verify]))
    monkeypatch.setattr(verifier, 'table_exists', AsyncMock(return_value=True))
    compare = AsyncMock(side_effect=lambda p, v, config, *a, **kw: verifier.TableResult(
        config.name, passed=config.name != 'nar_addresses'))
    monkeypatch.setattr(verifier, 'compare_table_full', compare)
    args = SimpleNamespace(verify_db_name='geocoder_verify', source='nar_ontario',
                           quick=False, verbose=False, diff=False)
    results = await verifier.compare_databases(args, verifier.TimingInfo())
    assert {c.args[2].name for c in compare.call_args_list} == {
        'nar_addresses', 'nar_locations', 'data_sources'}
    assert any(r.table_name == 'nar_addresses' and not r.passed and not r.skipped for r in results)
    prod.close.assert_awaited_once()
    verify.close.assert_awaited_once()


@pytest.mark.parametrize('function', ['sync_data_sources', 'compare_databases'])
async def test_production_read_connection_closes_if_verify_connection_fails(function, monkeypatch):
    prod = AsyncMock()
    monkeypatch.setattr(verifier, 'connect_to_db', AsyncMock(
        side_effect=[prod, RuntimeError('verify connection failed')]))
    with pytest.raises(RuntimeError, match='verify connection failed'):
        if function == 'sync_data_sources':
            await verifier.sync_data_sources('geocoder_verify')
        else:
            await verifier.compare_databases(
                SimpleNamespace(verify_db_name='geocoder_verify'), verifier.TimingInfo())
    prod.close.assert_awaited_once()


@pytest.fixture
def cli(monkeypatch):
    replacements = {
        'preflight_checks': AsyncMock(return_value=True),
        'create_verify_db': MagicMock(return_value=True),
        'init_verify_db': MagicMock(return_value=True),
        'sync_data_sources': AsyncMock(),
        'copy_dependency_tables': MagicMock(),
        'ensure_boundary_tables': AsyncMock(),
        'run_etl': MagicMock(return_value=True),
        'run_etl_resume': AsyncMock(return_value=True),
        'run_postal_codes_import': MagicMock(return_value=True),
        'run_setup_transformers': AsyncMock(),
        'compare_databases': AsyncMock(return_value=[verifier.TableResult('properties', True)]),
        'cleanup_verify_db': MagicMock(),
    }
    for name, mock in replacements.items():
        monkeypatch.setattr(verifier, name, mock)
    monkeypatch.setattr(sys, 'argv', ['verify_etl_pipeline'])
    return replacements


@pytest.mark.parametrize('mode', [[], ['--resume']])
@pytest.mark.parametrize('failed_stage', ['etl', 'postal'])
async def test_cli_reports_pipeline_failures_even_when_comparison_passes(mode, failed_stage, cli, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['verify_etl_pipeline', *mode])
    target = ('run_etl_resume' if mode else 'run_etl') if failed_stage == 'etl' else 'run_postal_codes_import'
    cli[target].return_value = False
    with pytest.raises(SystemExit) as error:
        await verifier.main()
    assert error.value.code == 1
    cli['cleanup_verify_db'].assert_not_called()
    output = capsys.readouterr().out
    assert '[FAIL]' in output and 'retained' in output


@pytest.mark.parametrize('results', [[], [verifier.TableResult('unused', True, skipped=True)]])
async def test_cli_fails_when_no_tables_were_compared(results, cli, capsys):
    cli['compare_databases'].return_value = results
    with pytest.raises(SystemExit) as error:
        await verifier.main()
    assert error.value.code == 1
    output = capsys.readouterr().out
    assert 'No tables were compared' in output and '[FAIL]' in output
    assert '0/0 tables PASS' not in output
    cli['cleanup_verify_db'].assert_not_called()


async def test_cli_retains_database_on_table_mismatch(cli):
    cli['compare_databases'].return_value = [verifier.TableResult('properties', False)]
    with pytest.raises(SystemExit) as error:
        await verifier.main()
    assert error.value.code == 1
    cli['cleanup_verify_db'].assert_not_called()


@pytest.mark.parametrize('mode,should_cleanup', [([], True), (['--resume'], False), (['--skip-etl'], False), (['--keep-verify-db'], False)])
async def test_cli_success_and_cleanup_policy(mode, should_cleanup, cli, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['verify_etl_pipeline', *mode])
    with pytest.raises(SystemExit) as error:
        await verifier.main()
    assert error.value.code == 0
    assert cli['cleanup_verify_db'].called is should_cleanup
    if '--skip-etl' in mode:
        cli['run_etl'].assert_not_called()
        cli['run_etl_resume'].assert_not_awaited()


@pytest.mark.parametrize('flags', [
    ['--resume', '--verify-db-name', 'geocoder'],
    ['--skip-etl', '--verify-db-name', 'geocoder'],
    ['--resume', '--skip-etl'],
])
async def test_cli_rejects_unsafe_targets_and_conflicting_modes_before_work(flags, cli, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['verify_etl_pipeline', *flags])
    with pytest.raises(SystemExit) as error:
        await verifier.main()
    assert error.value.code == 2
    cli['preflight_checks'].assert_not_awaited()
    cli['cleanup_verify_db'].assert_not_called()
