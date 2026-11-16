"""Regression coverage for deployment, ingestion and API contracts."""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]

def test_mapping_identity_and_confirmed_sources():
    mappings = json.loads((ROOT / 'config/dataset-mappings.json').read_text())
    assert mappings['guelph']['field_mappings']['property_id'] == 'ADDID'
    assert mappings['hamilton']['field_mappings']['street_number'] == 'NUMBER_COMPLETE'
    assert mappings['middlesex_county']['field_mappings']['property_id'] == 'GlobalID'

@pytest.mark.asyncio
async def test_migrations_verify_null_unknown_and_changed_checksums():
    from database.run_migrations import verify_applied_checksums,parse_migrations
    migrations=parse_migrations((ROOT/'database/migrations.sql').read_text())
    conn=AsyncMock()
    conn.fetch.return_value=[{'migration_number':1,'checksum':None}, {'migration_number':2,'checksum':'wrong'}, {'migration_number':999,'checksum':'x'}]
    errors=await verify_applied_checksums(conn,migrations)
    assert len(errors)==3
    assert any('unverified' in e for e in errors)
    assert any('missing' in e for e in errors)

@pytest.mark.asyncio
async def test_current_import_stats_not_historical_and_all_parts_counted():
    from data_sourcing.setup_data import run_import_with_tracking
    conn=AsyncMock()
    conn.fetchval.return_value=42
    with patch('data_sourcing.setup_data.run_import_script',return_value=True), \
         patch('data_sourcing.setup_data.get_latest_import_stats',AsyncMock(return_value=None)) as stats:
        result=await run_import_with_tracking(conn,{'source_key':'guelph_addresses','dataset_mapping_key':'guelph'})
        stats.assert_awaited_once_with(conn,'guelph',42)
        assert result.success and result.rows_inserted==0
        assert 'Unchanged' in result.summary_line()
    conn.fetchrow.return_value={'status':'failed','error_message':'part 2 overflow'}
    from data_sourcing.setup_data import get_latest_import_stats
    await get_latest_import_stats(conn,'nar_ontario',42)
    sql=conn.fetchrow.call_args.args[0]
    assert 'bool_and' in sql and 'id > $2' in sql and 'sum(rows_processed)' in sql


def test_etl_uses_active_interpreter_not_inline_uv_environments():
    source=(ROOT/'data_sourcing/setup_data.py').read_text()
    assert "['uv', 'run'" not in source
    assert "'uv', 'run', str(script_path)" not in source
    assert 'sys.executable' in source
    assert 'input("  Continue with remaining sources?' not in source

@pytest.mark.asyncio
async def test_arcgis_invalid_json_is_retried_not_empty_success():
    from data_sourcing.downloaders.download_utils import fetch_arcgis_page_with_retry
    client=AsyncMock()
    bad=MagicMock(); bad.status_code=200; bad.json.side_effect=ValueError('HTML gateway response')
    good=MagicMock(); good.status_code=200; good.json.return_value={'features':[{'properties':{'OBJECTID':1}}]}
    client.get.side_effect=[bad,good]
    with patch('data_sourcing.downloaders.download_utils.asyncio.sleep',AsyncMock()):
        features,oid=await fetch_arcgis_page_with_retry('https://example.test/0','OBJECTID',0,10,'geojson',client)
    assert len(features)==1 and oid==1
    assert client.get.await_count==2

@pytest.mark.asyncio
async def test_nar_upsert_counts_inserts_and_updates_separately():
    from data_sourcing.importers.import_nar_data import insert_address_batch
    conn=MagicMock()
    conn.transaction.return_value.__aenter__=AsyncMock()
    conn.transaction.return_value.__aexit__=AsyncMock(return_value=False)
    conn.fetchval=AsyncMock(side_effect=[True,False])
    pool=MagicMock()
    pool.acquire.return_value.__aenter__=AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__=AsyncMock(return_value=False)
    fields=('loc_guid','addr_guid','civic_no','civic_no_suffix','apt_no_label',
            'official_street_name','official_street_type','official_street_dir',
            'csd_eng_name','prov_code','mail_postal_code','bg_x','bg_y')
    row=dict.fromkeys(fields)
    assert await insert_address_batch([row,row],pool)==(1,1)
    assert await insert_address_batch([],pool)==(0,0)

@pytest.mark.asyncio
async def test_orchestrator_download_failure_exits_nonzero_without_import():
    from data_sourcing import setup_data
    source={'source_key':'guelph_addresses','source_name':'Guelph','status':'confirmed','commercial_use_allowed':True}
    conn=AsyncMock()
    with patch('sys.argv',['setup_data','--source','guelph_addresses']), \
         patch.object(setup_data,'get_db_connection',AsyncMock(return_value=conn)), \
         patch.object(setup_data,'check_data_sources_seeded',AsyncMock(return_value=True)), \
         patch.object(setup_data,'get_source_info',AsyncMock(return_value=source)), \
         patch.object(setup_data,'run_download_script',return_value=False), \
         patch.object(setup_data,'run_import_with_tracking',AsyncMock()) as importer:
        with pytest.raises(SystemExit) as error:
            await setup_data.main()
        assert error.value.code==1
        importer.assert_not_called()
        conn.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_nonaddress_quarantine_does_not_hide_real_errors():
    from data_sourcing.importers.import_tracker import ImportTracker
    tracker=ImportTracker(None,'test_source')
    await tracker.log_row_error(1,'not_address_record','No identity',{},count_failure=False)
    await tracker.log_row_error(2,'transform_error','Invalid coordinates',{})
    assert tracker.stats.rows_skipped==1 and tracker.stats.rows_failed==1
    assert len(tracker._error_batch)==2

@pytest.mark.asyncio
async def test_row_failures_are_not_success_even_for_legacy_completed_history():
    from data_sourcing import setup_data
    conn=AsyncMock(); conn.fetchval.return_value=1
    stats={'status':'completed','rows_failed':1}
    with patch.object(setup_data,'run_import_script',return_value=True), \
         patch.object(setup_data,'get_latest_import_stats',AsyncMock(return_value=stats)):
        result=await setup_data.run_import_with_tracking(conn,{'source_key':'test'})
        assert not result.success
        assert '1 rows failed' in result.error_message


def test_identity_reconciliation_refuses_duplicate_ids(tmp_path):
    from data_sourcing.management.reconcile_property_ids import expected_ids
    f=tmp_path/'addresses.json'
    f.write_text(json.dumps({'features':[{'properties':{'ADDID':1,'ADDRESS':'51 Cork Street West'}}]}))
    assert expected_ids('guelph',f)=={'GUE-ID-1':'51 Cork Street West'}
    f.write_text(json.dumps({'features':[{'properties':{'ADDID':1}},{'properties':{'ADDID':1}}]}))
    with pytest.raises(ValueError,match='Duplicate upstream ID'):
        expected_ids('guelph',f)


def test_native_env_keeps_credentials_private_and_existing_file(tmp_path):
    import os, shutil, subprocess
    scripts=tmp_path/'dependency_setup'; scripts.mkdir()
    script=scripts/'configure_env.sh'
    shutil.copy(ROOT/'dependency_setup/configure_env.sh',script)
    shutil.copy(ROOT/'.env.example',tmp_path/'.env.example')
    env={**os.environ,'DB_PASSWORD':'test_only_hex_password','DB_HOST':'localhost','DB_NAME':'geocoder','DB_USER':'geocoder'}
    run=subprocess.run(['bash',str(script)],env=env,capture_output=True,text=True)
    assert run.returncode==0,run.stderr
    output=(tmp_path/'.env').read_text()
    assert 'DB_PASSWORD=test_only_hex_password' in output
    assert 'INITIAL_ADMIN_KEY=' in output
    assert 'test_only_hex_password' not in run.stdout
    assert (tmp_path/'.env').stat().st_mode & 0o777 == 0o600
    subprocess.run(['bash',str(script)],env=env,check=True,capture_output=True)
    assert (tmp_path/'.env').read_text()==output


def test_make_etl_command_has_only_module_entrypoint(tmp_path):
    import shutil,subprocess
    shutil.copy(ROOT/'Makefile',tmp_path/'Makefile')
    run=subprocess.run(['make','-n','docker-etl','ETL_ARGS=--help'],cwd=tmp_path,capture_output=True,text=True,check=True)
    assert 'data_sourcing.setup_data --help' in run.stdout
    assert 'python -m data_sourcing.setup_data' not in run.stdout
