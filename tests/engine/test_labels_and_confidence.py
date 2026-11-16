"""Contract tests for source-derived labels and tier-specific confidence."""
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest

from engine.address_formatter import format_property_address
from engine.layered_matcher import (
    MatchMetadata, LayeredNARMatcher, build_result_from_properties_row,
    build_result_from_nar_row,
)

HAMILTON = {
    'property_id': 'HAM-ID-1', 'source_dataset': 'hamilton',
    'street_number': '681', 'street_name': 'Hall', 'street_suffix': 'Road',
    'street_direction': None, 'civic_no_suffix': None, 'unit_no': None,
    'city': 'Hamilton', 'province': 'ON', 'postal_code': 'L0R1C0',
    'full_address': None, 'latitude': 43.07161804, 'longitude': -79.77144980,
    'assessment_roll_number': None, 'pin': None, 'gpid': None,
}
NAR = {
    'civic_no': '21605', 'official_street_name': 'Adelaide',
    'official_street_type': 'ST', 'official_street_dir': 'N',
    'csd_eng_name': 'Middlesex Centre', 'mail_postal_code': 'N0M1C0',
    'bg_latitude': 43.06061380, 'bg_longitude': -81.26875973,
    'street_similarity': 0.8,
}


def test_hamilton_label_comes_from_stored_components_without_mutation():
    original = deepcopy(HAMILTON)
    result = build_result_from_properties_row(HAMILTON, 0, MatchMetadata())
    assert result['matched_address'] == '681 Hall Road, Hamilton, ON L0R 1C0'
    assert HAMILTON == original


@pytest.mark.parametrize('full_address', ['51 Cork Street West', '  Source-provided label  '])
def test_existing_source_label_is_preserved(full_address):
    assert format_property_address({**HAMILTON, 'full_address': full_address}) == full_address


@pytest.mark.parametrize('unit,suffix,expected', [
    ('2', 'A', 'Unit 2, 681A Hall Road, Hamilton, ON L0R 1C0'),
    ('Suite 2', '1/2', 'Suite 2, 681 1/2 Hall Road, Hamilton, ON L0R 1C0'),
    ('#2', '-A', '#2, 681-A Hall Road, Hamilton, ON L0R 1C0'),
    (0, None, 'Unit 0, 681 Hall Road, Hamilton, ON L0R 1C0'),
])
def test_generated_labels_preserve_unit_and_civic_suffix(unit, suffix, expected):
    assert format_property_address({**HAMILTON, 'unit_no': unit, 'civic_no_suffix': suffix}) == expected


def test_empty_source_label_and_absent_optional_fields():
    row = {'full_address': ' ', 'street_number': 12, 'street_name': 'Queen',
           'street_suffix': 'ST', 'street_direction': 'W'}
    assert format_property_address(row) == '12 Queen Street West'
    assert format_property_address({'city': 'Hamilton', 'province': 'ON'}) is None


@pytest.mark.parametrize('table_type,unit_field', [('properties', 'unit_no'), ('nar', 'apt_no_label')])
def test_requested_unit_disambiguates_using_real_database_columns(table_type, unit_field):
    from engine.layered_matcher import disambiguate
    # The civic suffix must never be mistaken for an apartment number.
    wrong = {unit_field: '1', 'civic_no_suffix': '2'}
    right = {unit_field: '2', 'civic_no_suffix': None}
    row, used, margin = disambiguate([wrong, right], {'unit_no': '2'}, table_type)
    assert row is right and used and margin == 15


def test_unit_specific_labels_have_distinct_cache_keys():
    from engine.address_parser import parse_address, generate_address_hash
    base = {'street_number': '681', 'street_name': 'Hall', 'city': 'Hamilton'}
    hashes = {generate_address_hash(parse_address({**base, 'unit_no': unit}))
              for unit in (None, '1', '2')}
    assert len(hashes) == 3
    assert generate_address_hash(parse_address({**base, 'unit_no': '2'})) == generate_address_hash(
        parse_address({'Address': {'StreetNumber': '681', 'StreetName': 'Hall',
                                  'City': 'Hamilton', 'UnitNumber': '2'}}))


@pytest.mark.asyncio
async def test_different_units_do_not_reuse_a_cached_matched_label():
    from engine import geocoder
    entries = {}
    async def get(key):
        return deepcopy(entries.get(key))
    async def put(key, result):
        entries[key] = deepcopy(result)
        return True
    async def match(components):
        row = {**HAMILTON, 'unit_no': components['unit_no']}
        return build_result_from_properties_row(row, 4, MatchMetadata(matched_layer_name='city_core'))
    payload = {'street_number': '681', 'street_name': 'Hall', 'city': 'Hamilton'}
    with patch.object(geocoder.cache, 'get', side_effect=get), \
         patch.object(geocoder.cache, 'set', side_effect=put), \
         patch.object(geocoder, 'lookup_by_pin', AsyncMock(return_value=None)), \
         patch.object(geocoder, 'match_municipal_data', side_effect=match):
        first = await geocoder.geocode({**payload, 'unit_no': '1'})
        second = await geocoder.geocode({**payload, 'unit_no': '2'})
        repeated = await geocoder.geocode({**payload, 'unit_no': '2'})
    assert first['matched_address'].startswith('Unit 1,')
    assert second['matched_address'].startswith('Unit 2,') and not second['cached']
    assert repeated['matched_address'] == second['matched_address'] and repeated['cached']


@pytest.mark.asyncio
async def test_pin_lookup_uses_same_label_fallback():
    from engine import geocoder
    with patch.object(geocoder.database, 'query', AsyncMock(return_value=[{**HAMILTON, 'pin': 123}])):
        result = await geocoder.lookup_by_pin({'pin': '123'})
    assert result['matched_address'] == '681 Hall Road, Hamilton, ON L0R 1C0'
    assert result['match_tier'] == 1
