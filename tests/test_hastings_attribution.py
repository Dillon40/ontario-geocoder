"""Verified Hastings item terms must reach fresh seeds and existing deployments."""
import hashlib
import html
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from database.run_migrations import parse_migrations

ROOT = Path(__file__).resolve().parents[1]
ITEM_ID = 'd1edbea63ca24e5bbd2363c3015bfdb0'
ITEM_URL = f'https://www.arcgis.com/sharing/rest/content/items/{ITEM_ID}?f=pjson'
ATTRIBUTION = 'Contains information licensed under the Open Government License – The Corporation of the County of Hastings.'
LICENSE_TYPE = 'Open Government License – Corporation of the County of Hastings'


def correction(sql):
    return sql.split('-- BEGIN HASTINGS ITEM LICENCE CORRECTION', 1)[1].split(
        '-- END HASTINGS ITEM LICENCE CORRECTION', 1)[0]


def test_archived_item_confirms_commercial_permission_and_exact_attribution():
    archive = ROOT / 'docs/licences/hastings_open_data_terms.json'
    metadata = json.loads(archive.read_text())
    assert metadata['id'] == ITEM_ID
    assert metadata['accessInformation'] == 'Hastings County 2026'
    assert metadata['url'].endswith('/CivicAdd_OpenData/FeatureServer')
    terms = ' '.join(html.unescape(re.sub(r'<[^>]+>', ' ', metadata['licenseInfo'])).split())
    assert 'including for commercial purposes' in terms
    assert ATTRIBUTION in terms
    assert hashlib.sha256(archive.read_bytes()).hexdigest() in (
        ROOT / 'docs/licences/README.md').read_text()


def test_fresh_seed_and_append_only_migration_apply_same_guarded_correction():
    seed = (ROOT / 'database/seed_data_sources.sql').read_text()
    migrations = parse_migrations((ROOT / 'database/migrations.sql').read_text())
    assert migrations[-1]['number'] == 41
    sql = correction(migrations[-1]['sql'])
    assert sql == correction(seed)
    assert ATTRIBUTION in sql and ITEM_URL in sql and LICENSE_TYPE in sql
    assert "WHERE source_key = 'hastings_county' AND (" in sql
    assert 'WHEN required_attribution IS NULL OR btrim(required_attribution)' in sql
    assert 'ELSE required_attribution END' in sql
    assert 'ELSE license_url END' in sql and 'ELSE license_type END' in sql
    assert 'ELSE license_notes END' in sql
    for protected in ('status', 'commercial_use_allowed', 'api_endpoint', 'record_count', 'last_data_refresh'):
        assert not re.search(rf'\b{protected}\s*=', sql)
    assert 'DELETE ' not in sql and 'TRUNCATE ' not in sql


@pytest.mark.asyncio
async def test_hastings_licence_cache_and_attribution_listing_use_verified_statement(monkeypatch):
    from engine import license_service
    monkeypatch.setattr(license_service, '_license_cache', {})
    monkeypatch.setattr(license_service, '_dataset_to_source_key', {})
    monkeypatch.setattr(license_service.database, 'query', AsyncMock(return_value=[{
        'source_key': 'hastings_county', 'source_name': 'Hastings County',
        'status': 'confirmed', 'dataset_mapping_key': 'hastings_county',
        'license_type': LICENSE_TYPE, 'license_url': ITEM_URL,
        'commercial_use_allowed': True, 'required_attribution': ATTRIBUTION,
    }]))
    await license_service.init_license_cache()
    assert license_service.get_attribution('municipal_hastings_county') == ATTRIBUTION
    assert license_service.get_license_info('municipal_hastings_county')['license_url'] == ITEM_URL
    assert license_service.get_all_attributions() == [{
        'source_key': 'hastings_county', 'source_name': 'Hastings County',
        'required_attribution': ATTRIBUTION, 'license_type': LICENSE_TYPE, 'license_url': ITEM_URL,
    }]
