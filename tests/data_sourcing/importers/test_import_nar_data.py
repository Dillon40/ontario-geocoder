"""Publisher CSV encoding must preserve address/location linkage."""
import csv
from unittest.mock import AsyncMock

import pytest

from data_sourcing.importers import import_nar_data as importer


@pytest.mark.asyncio
@pytest.mark.parametrize('encoding', ['utf-8', 'utf-8-sig'])
async def test_location_csv_preserves_first_header_with_or_without_bom(tmp_path, encoding):
    path = tmp_path / 'location.csv'
    with path.open('w', encoding=encoding, newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['LOC_GUID', 'BG_LATITUDE', 'BG_LONGITUDE', 'CSD_CODE'])
        writer.writerow(['location-one', '44.7', '-77.5', '3512001'])
    locations = await importer.load_locations(str(path))
    assert locations == {'location-one': {
        'loc_guid': 'location-one', 'bg_latitude': 44.7,
        'bg_longitude': -77.5, 'csd_code': 3512001,
    }}


@pytest.mark.asyncio
@pytest.mark.parametrize('encoding', ['utf-8', 'utf-8-sig'])
async def test_address_csv_keeps_location_guid_and_linkage(tmp_path, monkeypatch, encoding):
    path = tmp_path / 'address.csv'
    with path.open('w', encoding=encoding, newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['LOC_GUID', 'ADDR_GUID', 'PROV_CODE', 'CIVIC_NO', 'MAIL_POSTAL_CODE'])
        writer.writerow(['location-one', 'address-one', '35', '175', 'K0K 1A0'])
    insert = AsyncMock(return_value=(1, 0))
    monkeypatch.setattr(importer, 'insert_address_batch', insert)
    stats = await importer.import_addresses(str(path), {'location-one': {}}, '35', 100, None)
    record = insert.call_args.args[0][0]
    assert record['loc_guid'] == 'location-one'
    assert record['addr_guid'] == 'address-one'
    assert record['mail_postal_code'] == 'K0K1A0'
    assert stats == {'inserted_count': 1, 'updated_count': 0, 'with_coords_count': 1}
