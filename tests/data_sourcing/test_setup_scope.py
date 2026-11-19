"""Explicit municipal-only ETL selection does not conceal requested failures."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from data_sourcing import setup_data


@pytest.fixture
def setup_run(monkeypatch):
    connection = AsyncMock()
    monkeypatch.setattr(setup_data, 'get_db_connection', AsyncMock(return_value=connection))
    monkeypatch.setattr(setup_data, 'check_data_sources_seeded', AsyncMock(return_value=True))
    sources = {
        'fsa_boundaries': {'import_script': 'import_fsa_boundaries.py'},
        'ontario_municipal_boundaries': {'import_script': 'import_municipal_boundaries.py'},
        'nar_ontario': {'import_script': 'import_nar_data.py'},
        'guelph_addresses': {'import_script': 'import_production_data.py'},
        'other_nonmunicipal': {'import_script': 'import_postal_codes.py'},
    }
    for key, value in sources.items():
        value.update(source_key=key, source_name=key, status='confirmed', commercial_use_allowed=True)
    monkeypatch.setattr(setup_data, 'get_import_order', AsyncMock(return_value=list(sources)))
    monkeypatch.setattr(setup_data, 'get_source_info', AsyncMock(side_effect=lambda conn, key: sources[key]))
    download = MagicMock(return_value=True)
    monkeypatch.setattr(setup_data, 'run_download_script', download)
    imported = AsyncMock(side_effect=lambda conn, source, **kwargs: setup_data.ImportResult(
        source_key=source['source_key'], success=True, rows_inserted=10))
    monkeypatch.setattr(setup_data, 'run_import_with_tracking', imported)
    monkeypatch.setattr(setup_data, 'validate_priority_source', AsyncMock(return_value=(True, 'populated')))

    async def run(*args):
        monkeypatch.setattr('sys.argv', ['setup_data', *args, '--commercial-only'])
        await setup_data.main()
    return run, connection, download, imported


@pytest.mark.asyncio
@pytest.mark.parametrize('import_only', [False, True])
async def test_municipal_only_includes_boundaries_and_never_requests_nar(setup_run, capsys, import_only):
    run, connection, download, imported = setup_run
    await run('--municipal-only', *(['--import-only'] if import_only else []))
    expected = ['fsa_boundaries', 'ontario_municipal_boundaries', 'guelph_addresses']
    assert [c.args[1]['source_key'] for c in imported.call_args_list] == expected
    assert [c.args[0] for c in download.call_args_list] == ([] if import_only else expected)
    connection.close.assert_awaited_once()
    output = capsys.readouterr().out
    assert 'NAR is intentionally excluded' in output
    assert 'nar_ontario: excluded by --municipal-only' in output
    assert 'other_nonmunicipal: excluded by --municipal-only' in output


@pytest.mark.asyncio
@pytest.mark.parametrize('selection', [['--all-confirmed'], ['--source', 'nar_ontario']])
async def test_requested_nar_failure_still_exits_nonzero(setup_run, selection):
    run, connection, download, imported = setup_run
    download.side_effect = lambda key, **kwargs: key != 'nar_ontario'
    with pytest.raises(SystemExit) as exc:
        await run(*selection)
    assert exc.value.code == 1
    assert 'nar_ontario' in [c.args[0] for c in download.call_args_list]
    processed = [c.args[1]['source_key'] for c in imported.call_args_list]
    assert 'nar_ontario' not in processed
    if '--all-confirmed' in selection:
        assert 'guelph_addresses' in processed  # Independent imports still proceed
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_municipal_download_failure_is_not_downgraded(setup_run):
    run, _, download, _ = setup_run
    download.side_effect = lambda key, **kwargs: key != 'guelph_addresses'
    with pytest.raises(SystemExit) as exc:
        await run('--municipal-only')
    assert exc.value.code == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('other', [['--all-confirmed'], ['--source', 'nar_ontario'], ['--list']])
async def test_municipal_selection_cannot_silently_override_explicit_source(monkeypatch, other):
    connection = AsyncMock()
    monkeypatch.setattr(setup_data, 'get_db_connection', connection)
    monkeypatch.setattr('sys.argv', ['setup_data', '--municipal-only', *other])
    with pytest.raises(SystemExit) as exc:
        await setup_data.main()
    assert exc.value.code == 2
    connection.assert_not_awaited()
