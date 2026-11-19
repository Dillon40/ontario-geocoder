"""Municipal quality/completeness gates, using real audit and stale-ID logic."""
import json
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from data_sourcing.importers import import_production_data as importer
from data_sourcing.importers.import_tracker import ImportTracker
from data_sourcing.importers.import_utils import (
    StaleDeletionAbortedError, validate_import_quality,
)
from data_sourcing.transformers.base import BaseTransformer, TransformContext
from data_sourcing.transformers.pipeline import TransformPipeline
from data_sourcing.transformers.record_validator import RecordValidator


def feature(identifier, longitude=-77.5, address=True):
    properties = {'LAT': 44.7, 'LONG': longitude, 'NUMBER': '175' if address else None}
    if identifier is not None:
        properties['ID'] = str(identifier)
    # Valid geometry must NOT recover a malformed attribute pair.
    return {'type': 'Feature', 'properties': properties,
            'geometry': {'type': 'Point', 'coordinates': [-77.5, 44.7]}}


@pytest.fixture
def import_run(tmp_path, monkeypatch):
    monkeypatch.delenv('IMPORT_MIN_ACCEPT_RATIO', raising=False)
    monkeypatch.delenv('STALE_DELETE_MIN_RATIO', raising=False)
    connection = AsyncMock()
    connection.fetch.return_value = []
    connection.fetchval.return_value = 1
    connection.execute.return_value = 'UPDATE 1'
    connection.transaction = MagicMock()
    connection.transaction.return_value.__aenter__ = AsyncMock()
    connection.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=connection)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    pool.close = AsyncMock()
    pool.execute = AsyncMock()
    monkeypatch.setattr(importer, 'get_db_pool', AsyncMock(return_value=pool))
    monkeypatch.setattr(importer, 'get_db_connection', AsyncMock(return_value=connection))
    monkeypatch.setattr(importer, 'ensure_source_import_allowed', AsyncMock())
    monkeypatch.setitem(importer.dataset_mappings, 'test_source', {'field_mappings': {
        'property_id': 'ID', 'latitude': 'LAT', 'longitude': 'LONG', 'street_number': 'NUMBER'}})
    pipeline = TransformPipeline('test_source', [RecordValidator()])
    monkeypatch.setattr(importer.TransformPipeline, 'for_source_async',
                        AsyncMock(return_value=pipeline))
    def inserted(batch, *args):
        assert connection.fetch.await_count == 1  # Baseline captured before upserts
        return len(batch), 0

    insert = AsyncMock(side_effect=inserted)
    monkeypatch.setattr(importer, 'insert_batch', insert)
    tracker = ImportTracker(pool, 'test_source')
    monkeypatch.setattr(importer, 'ImportTracker', lambda *args: tracker)

    async def run(features, existing=(), strategy='soft_delete', streaming=False):
        data = {'type': 'FeatureCollection', 'features': features}
        path = tmp_path / 'addresses.geojson'
        path.write_text(json.dumps(data))
        monkeypatch.setattr(importer, 'load_spatial_file', lambda path: data)
        if streaming:
            monkeypatch.setattr(importer, 'STREAMING_THRESHOLD_MB', 0)
        connection.fetch.return_value = [{'property_id': f'TES-ID-{i}'} for i in existing]
        await importer.import_geojson(str(path), 'test_source', 50, force=True,
                                      stale_strategy=strategy)

    return run, tracker, connection, insert, pipeline


def delete_calls(connection):
    return [c for c in connection.execute.call_args_list if 'UPDATE properties' in c.args[0]]


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_quarantine_completes_with_warnings_and_preserves_old_id(import_run, streaming):
    run, tracker, connection, insert, _ = import_run
    # ID 99 is present but malformed; only truly missing ID 100 may retire.
    await run([feature(i) for i in range(99)] + [feature(99, 4954859.4366)],
              range(101), streaming=streaming)
    assert tracker.stats.rows_inserted == 99
    assert tracker.stats.rows_processed == 100
    assert tracker.stats.rows_quarantined == tracker.stats.rows_skipped == 1
    assert tracker.stats.rows_failed == 0
    assert tracker.metadata['accepted_ratio'] == .99
    assert tracker.metadata['outcome'] == 'completed_with_warnings'
    assert tracker.metadata['rows_quarantined'] == 1
    deletes = delete_calls(connection)
    assert len(deletes) == 1 and deletes[0].args[2] == ['TES-ID-100']
    assert all(row['longitude'] == -77.5 for c in insert.call_args_list for row in c.args[0])
    audit = [r for c in connection.executemany.call_args_list for r in c.args[1]]
    assert audit[0][2] == 'validation_quarantine'
    original = json.loads(audit[0][4])
    assert original['property_id'] == 'TES-ID-99'
    assert original['feature']['properties']['LONG'] == 4954859.4366
    # Persisted metadata and compatibility status, not stdout alone.
    update = [c for c in connection.execute.call_args_list if 'UPDATE import_history' in c.args[0]][0]
    assert update.args[2] == 'completed'
    assert json.loads(update.args[12])['outcome'] == 'completed_with_warnings'


@pytest.mark.asyncio
@pytest.mark.parametrize('existing', [(), range(100)])
async def test_excessive_quarantine_fails_before_stale_deletion(import_run, existing):
    run, tracker, connection, _, _ = import_run
    with pytest.raises(RuntimeError, match='IMPORT QUALITY FAILED'):
        await run([feature(i) for i in range(98)] + [feature(98, 999), feature(99, 999)], existing)
    assert tracker.metadata['outcome'] == 'failed'
    assert tracker.stats.rows_quarantined == 2
    assert not delete_calls(connection)


@pytest.mark.asyncio
async def test_nonaddresses_do_not_dilute_quality_denominator(import_run):
    run, tracker, connection, _, _ = import_run
    with pytest.raises(RuntimeError, match='1/2 eligible rows'):
        await run([feature(1), feature(2, 999)] + [feature(i, address=False) for i in range(3, 103)])
    assert tracker.stats.rows_skipped == 101
    assert tracker.stats.rows_quarantined == 1
    assert not delete_calls(connection)


@pytest.mark.asyncio
async def test_unreliable_rejected_id_suppresses_all_retirement(import_run):
    run, tracker, connection, _, _ = import_run
    await run([feature(i) for i in range(99)] + [feature(None, 999)], range(100))
    assert tracker.metadata['stale_deletion_suppressed'] is True
    assert tracker.metadata['outcome'] == 'completed_with_warnings'
    assert not delete_calls(connection)


@pytest.mark.asyncio
@pytest.mark.parametrize('strategy', ['soft_delete', 'report_only', 'skip'])
async def test_new_ids_cannot_mask_truncation_even_without_deletion(import_run, strategy):
    run, tracker, connection, _, _ = import_run
    # 100 usable rows, but only 80 of the original 100 identities remain.
    with pytest.raises(StaleDeletionAbortedError):
        await run([feature(i) for i in range(80)] + [feature(i) for i in range(1000, 1020)],
                  range(100), strategy)
    assert tracker.metadata['outcome'] == 'failed'
    assert not delete_calls(connection)


class BrokenTransformer(BaseTransformer):
    @property
    def name(self):
        return 'BrokenTransformer'

    def transform(self, context):
        raise RuntimeError('unexpected code failure')


@pytest.mark.asyncio
async def test_unexpected_transformer_exception_is_fatal(import_run):
    run, tracker, connection, _, pipeline = import_run
    pipeline.transformers = [BrokenTransformer()]
    with pytest.raises(RuntimeError, match='Processing failure.*unexpected code failure'):
        await run([feature(1)])
    assert tracker.stats.rows_failed == 1
    assert tracker.stats.rows_quarantined == 0
    assert tracker.metadata['outcome'] == 'failed'
    assert not delete_calls(connection)


@pytest.mark.asyncio
async def test_sql_failure_is_fatal_and_does_not_retire_stale(import_run):
    run, tracker, connection, insert, _ = import_run
    insert.side_effect = asyncpg.PostgresError('database write failure')
    with pytest.raises(asyncpg.PostgresError):
        await run([feature(i) for i in range(100)], range(101))
    assert tracker.metadata['outcome'] == 'failed'
    assert not delete_calls(connection)


@pytest.mark.asyncio
async def test_critical_mapping_loss_remains_fatal(import_run):
    run, tracker, connection, _, _ = import_run
    data = feature(1)
    del data['properties']['NUMBER']
    with pytest.raises(ValueError, match='Field mapping validation failed'):
        await run([data])
    assert tracker.import_id is None
    assert not delete_calls(connection)


@pytest.mark.asyncio
async def test_no_accepted_rows_cannot_complete(import_run):
    run, tracker, connection, _, _ = import_run
    with pytest.raises(RuntimeError, match='IMPORT QUALITY FAILED'):
        await run([feature(1, address=False)])
    assert not delete_calls(connection)


@pytest.mark.parametrize('value', ['0', '-1', '1.1', 'nan', 'inf', 'invalid'])
def test_invalid_quality_threshold_fails_closed(monkeypatch, value):
    monkeypatch.setenv('IMPORT_MIN_ACCEPT_RATIO', value)
    with pytest.raises(ValueError):
        validate_import_quality(100, 0)


def test_quality_threshold_is_independent_of_deletion_threshold(monkeypatch):
    monkeypatch.delenv('IMPORT_MIN_ACCEPT_RATIO', raising=False)
    monkeypatch.setenv('STALE_DELETE_MIN_RATIO', '0.5')
    assert validate_import_quality(31150, 2) > .99
    with pytest.raises(RuntimeError):
        validate_import_quality(98, 2)
    monkeypatch.setenv('IMPORT_MIN_ACCEPT_RATIO', '1')
    with pytest.raises(RuntimeError):
        validate_import_quality(99, 1)


@pytest.mark.parametrize('latitude,longitude', [
    (None, -77.5), (44.7, None), ('', -77.5),
    (float('nan'), -77.5), (44.7, float('inf')), ('not-a-number', -77.5),
])
def test_bad_coordinate_pairs_are_explicit_data_defects(latitude, longitude):
    context = TransformContext({'latitude': latitude, 'longitude': longitude,
                                'street_number': '175'}, 'test_source')
    RecordValidator().transform(context)
    assert context.has_errors
    assert all(e.data_error for e in context.errors)


@pytest.mark.asyncio
async def test_orchestrator_surfaces_quarantine_warnings():
    from data_sourcing import setup_data
    from unittest.mock import patch
    connection = AsyncMock()
    connection.fetchval.return_value = 1
    stats = {'status': 'completed', 'rows_failed': 0, 'rows_skipped': 2,
             'rows_quarantined': 2, 'has_warnings': True, 'rows_inserted': 31150}
    with patch.object(setup_data, 'run_import_script', return_value=True), \
         patch.object(setup_data, 'get_latest_import_stats', AsyncMock(return_value=stats)):
        result = await setup_data.run_import_with_tracking(connection, {'source_key': 'test_source'})
    assert result.success
    assert 'COMPLETED WITH WARNINGS' in result.summary_line()
    assert '2 quarantined' in result.summary_line()


@pytest.mark.asyncio
async def test_changed_stream_count_fails_before_retirement(import_run, monkeypatch):
    run, tracker, connection, _, _ = import_run
    monkeypatch.setattr(importer, 'count_features_streaming', lambda path: 101)
    with pytest.raises(RuntimeError, match='Input feature count changed'):
        await run([feature(i) for i in range(100)], range(101), streaming=True)
    assert tracker.metadata['outcome'] == 'failed'
    assert not delete_calls(connection)


@pytest.mark.asyncio
async def test_corrupt_stream_fails_closed(import_run, monkeypatch):
    run, tracker, connection, insert, _ = import_run
    def corrupt(path, transformer=None):
        yield from (feature(i) for i in range(50))
        raise ValueError('truncated GeoJSON stream')
    monkeypatch.setattr(importer, 'stream_geojson_features', corrupt)
    with pytest.raises(ValueError, match='truncated GeoJSON'):
        await run([feature(i) for i in range(100)], range(101), streaming=True)
    assert insert.await_count == 1  # Prior batch committed, still no retirement
    assert tracker.metadata['outcome'] == 'failed'
    assert not delete_calls(connection)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['audit', 'metadata', 'metadata_table'])
async def test_diagnostic_or_metadata_sql_failure_precedes_retirement(import_run, monkeypatch, failure):
    run, tracker, connection, _, _ = import_run
    error = asyncpg.PostgresError('database failure')
    if failure == 'audit':
        connection.executemany.side_effect = error
    elif failure == 'metadata_table':
        importer.get_db_pool.return_value.execute.side_effect = asyncpg.UndefinedTableError(
            'dataset_metadata missing')
    else:
        monkeypatch.setattr(importer, 'update_dataset_metadata', AsyncMock(side_effect=error))
    with pytest.raises(asyncpg.PostgresError):
        await run([feature(i) for i in range(99)] + [feature(99, 999)], range(101))
    assert not delete_calls(connection)


@pytest.mark.asyncio
async def test_async_enrichment_connection_failure_is_not_quarantined(import_run):
    from data_sourcing.transformers.base import AsyncTransformer
    class BrokenLookup(AsyncTransformer):
        @property
        def name(self):
            return 'BrokenLookup'

        async def transform_async(self, context):
            raise ConnectionError('reference database unavailable')

    run, tracker, connection, _, pipeline = import_run
    pipeline.transformers = [BrokenLookup()]
    with pytest.raises(RuntimeError, match='Processing failure.*reference database unavailable'):
        await run([feature(1)])
    assert tracker.stats.rows_failed == 1
    assert not tracker.stats.rows_quarantined
    assert not delete_calls(connection)


@pytest.mark.asyncio
async def test_unchanged_file_keeps_previous_quarantine_warning(import_run, tmp_path):
    from datetime import datetime
    from data_sourcing.importers.import_utils import FileHashManager
    _, _, connection, _, _ = import_run
    path = tmp_path / 'unchanged.geojson'
    path.write_text('{}')
    connection.fetchrow.return_value = {
        'file_hash': FileHashManager.compute_file_hash(str(path)),
        'completed_at': datetime(2026, 1, 1),
        'metadata': json.dumps({'outcome': 'completed_with_warnings', 'rows_quarantined': 2}),
    }
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=connection)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    skip, reason = await FileHashManager.should_skip_file(pool, 'test_source', str(path))
    assert skip and 'WARNING' in reason and '2 rows' in reason


@pytest.mark.asyncio
@pytest.mark.parametrize('lookup', ['fsa', 'municipality'])
@pytest.mark.parametrize('error_type', [ConnectionError, ValueError])
async def test_real_lookup_exhausted_retries_fail_import(import_run, monkeypatch, lookup, error_type):
    from data_sourcing.transformers import db_retry
    from data_sourcing.transformers.fsa_enricher import FSAEnricher
    from data_sourcing.transformers.municipality_mapper import MunicipalityMapper
    run, tracker, connection, _, pipeline = import_run
    monkeypatch.setattr(db_retry.asyncio, 'sleep', AsyncMock())
    connection.fetchrow.side_effect = error_type('reference database offline')
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=connection)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    transformer = (FSAEnricher(pool=pool) if lookup == 'fsa' else
                   MunicipalityMapper({'derive_from_coordinates': True}, pool=pool))
    pipeline.transformers = [transformer, RecordValidator()]
    with pytest.raises(RuntimeError, match='Processing failure.*reference database offline'):
        await run([feature(1)])
    assert connection.fetchrow.await_count == (4 if error_type is ConnectionError else 1)
    assert tracker.stats.rows_failed == 1 and tracker.stats.rows_quarantined == 0
    assert not delete_calls(connection)


@pytest.mark.asyncio
async def test_batch_fsa_database_error_is_not_an_empty_match(import_run, monkeypatch):
    from data_sourcing.transformers import db_retry
    from data_sourcing.transformers.fsa_enricher import FSAEnricher
    _, _, connection, _, _ = import_run
    monkeypatch.setattr(db_retry.asyncio, 'sleep', AsyncMock())
    connection.fetch.side_effect = ConnectionError('batch database offline')
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=connection)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    with pytest.raises(ConnectionError, match='batch database offline'):
        await FSAEnricher(pool=pool).lookup_fsa_batch([(0, -77.5, 44.7)])
    assert connection.fetch.await_count == 4


@pytest.mark.asyncio
async def test_fsa_no_spatial_match_remains_a_warning(import_run):
    from data_sourcing.transformers.fsa_enricher import FSAEnricher
    _, _, connection, _, _ = import_run
    connection.fetchrow.return_value = None
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=connection)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    context = TransformContext({'latitude': 44.7, 'longitude': -77.5}, 'test_source')
    await FSAEnricher(pool=pool).transform_async(context)
    assert context.has_warnings and not context.has_errors


@pytest.mark.asyncio
async def test_reference_setup_error_prevents_pipeline_creation(monkeypatch):
    from data_sourcing.transformers.base import SetupResult
    monkeypatch.setattr(TransformPipeline, '_run_setup_transformers',
                        AsyncMock(return_value=[SetupResult('error', 'reference setup failed')]))
    with pytest.raises(RuntimeError, match='Transform setup failed: reference setup failed'):
        await TransformPipeline.for_source_async('test_source', MagicMock())
