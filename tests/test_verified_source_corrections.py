"""Regression tests for the four publisher-supplied endpoint/licence updates."""
import ast
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENDPOINTS = {
    'windsor_addresses': 'https://mappmycity.ca/arcgis/rest/services/open_data_services/propertyparcels/MapServer/0',
    'sarnia': 'https://services1.arcgis.com/ICybsLmBXrZCZV3x/arcgis/rest/services/Addresses_Open_Data_AGOL/FeatureServer/0',
    'london_addresses': 'https://maps.london.ca/server/rest/services/OpenData/OpenData_Community/MapServer/0',
    'elgin_county_addresses': 'https://services6.arcgis.com/TCQ78XR5HqDZ1SDc/arcgis/rest/services/Elgin_Civic_Numbers_2026/FeatureServer/0',
}


def test_verified_corrections_remain_in_migration_38():
    from database.run_migrations import parse_migrations
    migrations = parse_migrations((ROOT / 'database/migrations.sql').read_text())
    sql = next(m['sql'] for m in migrations if m['number'] == 38)
    seed = (ROOT / 'database/seed_data_sources.sql').read_text()
    marker = '-- BEGIN VERIFIED SOURCE CORRECTIONS'
    end = '-- END VERIFIED SOURCE CORRECTIONS'
    assert sql.split(marker)[1].split(end)[0] == seed.split(marker)[1].split(end)[0]
    module = ast.parse((ROOT / 'data_sourcing/downloaders/arcgis_downloader.py').read_text())
    endpoints = next(ast.literal_eval(n.value) for n in module.body if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == 'STANDARD_SOURCES' for t in n.targets))
    for key, endpoint in ENDPOINTS.items():
        assert endpoints[key] == endpoint
        assert endpoint in seed and endpoint in sql
        assert '/query?' not in endpoints[key]
    corrections = sql.split(marker)[1].split(end)[0]
    assert 'ds.api_endpoint = fixed.old_endpoint' in corrections
    assert 'ds.required_attribution IS NOT DISTINCT FROM fixed.old_attribution' in corrections
    assert 'status =' not in corrections
    assert 'is_deleted' not in corrections


def test_verified_default_attributions_and_london_redistribution_terms():
    seed = (ROOT / 'database/seed_data_sources.sql').read_text()
    assert 'Contains information licensed under the Open Government Licence – The Corporation of the City of Windsor.' in seed
    assert 'Contains information licensed under the Open Government Licence – City of Sarnia' in seed
    assert 'Contains information licensed under the Open Government Licence – County of Elgin.' in seed
    assert 'City credit is optional, not mandatory' in seed
    assert 'Redistribution must include the terms or their URL' in seed
    for name in ('windsor', 'elgin', 'london'):
        assert (ROOT / f'docs/licences/{name}_open_data_terms.pdf').read_bytes().startswith(b'%PDF')
    sarnia = (ROOT / 'docs/licences/sarnia_open_data_terms.json').read_text()
    assert 'commercial purposes' in sarnia


@pytest.mark.parametrize('key,properties,expected', [
    ('windsor_addresses',
     {'OBJECTID': 1, 'address_number': 3648, 'street_name': 'KING',
      'street_suffix': 'ST', 'street_direction': '', 'unit_number': '',
      'property_address': '3648 KING ST', 'ward_number': 3},
     {'property_id': '1', 'street_number': '3648', 'full_address': '3648 KING ST', 'ward': '3'}),
    ('elgin_county_addresses',
     {'GlobalID': 'example-id', 'Civic': '12953', 'Address': '12953 IONA ROAD',
      'StrFULL': 'IONA ROAD', 'StrUNIT': 'N/A', 'MUNI': 'DUTTON/DUNWICH',
      'ARN': '342900000603301', 'x': -81.47049585, 'y': 42.75444243},
     {'street_number': '12953', 'full_address': '12953 IONA ROAD', 'city': 'DUTTON/DUNWICH'}),
])
def test_current_schemas_map_address_identity_and_optional_fields(key, properties, expected):
    from data_sourcing.importers.import_production_data import map_fields, validate_field_mappings
    mappings = json.loads((ROOT / 'config/dataset-mappings.json').read_text())
    fields = mappings[key]['field_mappings']
    assert not set(fields.values()) - properties.keys()
    assert validate_field_mappings([{'properties': properties}], fields, key) == (True, [])
    record = map_fields(properties, fields)
    if key == 'windsor_addresses':
        assert 'assessment_roll_number' not in record
    for target, value in expected.items():
        assert str(record[target]) == value
