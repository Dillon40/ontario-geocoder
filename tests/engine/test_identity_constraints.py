"""Supplied units and civic suffixes constrain every matching tier before LIMIT."""
from unittest.mock import AsyncMock

import pytest

from engine import database, geocoder
from engine.layered_matcher import query_candidates, build_result_from_nar_row, MatchMetadata


@pytest.mark.parametrize('nar', [False, True])
@pytest.mark.parametrize('components,expected', [
    ({'unit_no': ' 99 '}, ['99']),
    ({'civic_no_suffix': ' b '}, ['b']),
    ({'unit_no': '99', 'civic_no_suffix': 'B'}, ['99', 'B']),
    ({'unit_no': '', 'civic_no_suffix': None}, []),
])
async def test_constraints_are_bound_before_limit(monkeypatch, nar, components, expected):
    query = AsyncMock(return_value=[])
    monkeypatch.setattr(database, 'query', query)
    table, prefix = ('nar_addresses a', 'a.') if nar else ('properties', '')
    await query_candidates(f'SELECT * FROM {table} WHERE TRUE ORDER BY {prefix}id LIMIT 10',
                           [], components)
    sql, params = query.call_args.args
    assert params == expected
    assert f'NOT {prefix}is_deleted' in sql
    if components.get('unit_no'):
        column = 'a.apt_no_label' if nar else 'unit_no'
        assert f'UPPER(BTRIM({column})) = UPPER($1)' in sql.split('LIMIT')[0]
    if components.get('civic_no_suffix'):
        assert f'UPPER(BTRIM({prefix}civic_no_suffix)) = UPPER(${len(params)})' in sql.split('LIMIT')[0]


async def test_pin_lookup_enforces_supplied_identity(monkeypatch):
    query = AsyncMock(return_value=[])
    monkeypatch.setattr(database, 'query', query)
    assert await geocoder.lookup_by_pin({'pin': '123', 'unit_no': '99', 'civic_no_suffix': 'B'}) is None
    sql, params = query.call_args.args
    assert params == [123, '99', 'B']
    assert 'UPPER(BTRIM(unit_no)) = UPPER($2)' in sql.split('LIMIT')[0]
    assert 'UPPER(BTRIM(civic_no_suffix)) = UPPER($3)' in sql.split('LIMIT')[0]


def test_nar_label_preserves_matched_unit():
    row = {'civic_no': '68', 'civic_no_suffix': 'B', 'apt_no_label': '99',
           'official_street_name': 'Main', 'csd_eng_name': 'Toronto',
           'mail_postal_code': 'M1A1A1', 'bg_latitude': 44, 'bg_longitude': -79}
    result = build_result_from_nar_row(row, 0, MatchMetadata())
    assert result['matched_address'].startswith('Unit 99, 68 B Main')
