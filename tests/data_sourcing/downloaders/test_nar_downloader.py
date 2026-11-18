"""Headless NAR inputs and upstream access-control diagnostics."""
import zipfile
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from data_sourcing.downloaders import nar_downloader as nar


def archive_at(path, files):
    with zipfile.ZipFile(path, 'w') as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


@pytest.mark.parametrize('status', [200, 403])
def test_cloudflare_challenge_has_actionable_non_browser_diagnostics(status):
    response = httpx.Response(status, headers={'cf-mitigated': 'challenge'},
                              request=httpx.Request('GET', nar.NAR_PAGE_URL))
    with pytest.raises(RuntimeError, match='Cloudflare') as error:
        nar.check_download_response(response)
    assert 'NAR_DOWNLOAD_URL' in str(error.value)
    assert 'NAR_ARCHIVE_PATH' in str(error.value)
    assert 'repeated retries will not solve it' in str(error.value)


def test_other_forbidden_response_is_not_misdiagnosed_as_cloudflare():
    response = httpx.Response(403, request=httpx.Request('GET', nar.NAR_PAGE_URL))
    with pytest.raises(RuntimeError, match='HTTP 403') as error:
        nar.check_download_response(response)
    assert 'Cloudflare' not in str(error.value)


def test_extracts_only_ontario_and_never_archive_paths(tmp_path):
    archive = archive_at(tmp_path / 'release.zip', {
        '../../Address_35_part1.csv': 'Ontario addresses',
        'nested/Location_35_part1.csv': 'Ontario locations',
        'Address_24.csv': 'Quebec',
        '../../outside.txt': 'never extract',
    })
    output = tmp_path / 'ontario'
    output.mkdir()
    (output / 'Address_35_old.csv').write_text('old release')
    assert nar.extract_ontario_csvs(archive, output) == 2
    assert {p.name for p in output.iterdir()} == {'Address_35_part1.csv', 'Location_35_part1.csv'}
    assert (output / 'Address_35_part1.csv').read_text() == 'Ontario addresses'
    assert not (tmp_path / 'outside.txt').exists()


@pytest.mark.parametrize('files', [
    {'Address_24.csv': 'wrong province'},
    {'Location_35.csv': 'no addresses'},
    {'a/Address_35.csv': 'one', 'b/Address_35.csv': 'duplicate basename'},
])
def test_invalid_archive_preserves_retained_csvs(tmp_path, files):
    archive = archive_at(tmp_path / 'invalid.zip', files)
    output = tmp_path / 'ontario'
    output.mkdir()
    retained = output / 'Address_35_old.csv'
    retained.write_text('retained')
    with pytest.raises(ValueError):
        nar.extract_ontario_csvs(archive, output)
    assert retained.read_text() == 'retained'
    assert list(output.iterdir()) == [retained]


@pytest.mark.asyncio
async def test_offline_archive_does_not_make_http_requests(tmp_path, monkeypatch):
    archive = archive_at(tmp_path / 'release.zip', {'folder/Address_35.csv': 'Ontario'})
    monkeypatch.setattr(nar, 'DATA_DIR', tmp_path / 'data')
    monkeypatch.setenv('NAR_ARCHIVE_PATH', str(archive))
    monkeypatch.delenv('NAR_DOWNLOAD_URL', raising=False)
    with patch.object(nar, 'get_latest_nar_url', AsyncMock()) as discover, \
         patch.object(nar, 'download_file', AsyncMock()) as download:
        result = await nar.download_nar()
    assert result['success']
    discover.assert_not_awaited()
    download.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_url_skips_release_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(nar, 'DATA_DIR', tmp_path / 'data')
    monkeypatch.setenv('NAR_DOWNLOAD_URL', 'https://publisher.example/202606.zip')
    monkeypatch.delenv('NAR_ARCHIVE_PATH', raising=False)

    async def download(url, path, client):
        assert url == 'https://publisher.example/202606.zip'
        archive_at(path, {'Address_35.csv': 'Ontario'})

    with patch.object(nar, 'get_latest_nar_url', AsyncMock()) as discover, \
         patch.object(nar, 'download_file', side_effect=download):
        result = await nar.download_nar()
    assert result['success']
    discover.assert_not_awaited()


@pytest.mark.asyncio
async def test_conflicting_inputs_fail_without_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(nar, 'DATA_DIR', tmp_path / 'data')
    monkeypatch.setenv('NAR_DOWNLOAD_URL', 'https://publisher.example/release.zip')
    monkeypatch.setenv('NAR_ARCHIVE_PATH', '/some/archive.zip')
    with patch.object(nar, 'get_latest_nar_url', AsyncMock()) as discover:
        result = await nar.download_nar()
    assert not result['success']
    assert 'only one' in result['error']
    discover.assert_not_awaited()


@pytest.mark.asyncio
async def test_html_response_is_not_saved_as_archive(tmp_path):
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, headers={'content-type': 'text/html'}, text='<html>Security check</html>'))
    async with httpx.AsyncClient(transport=transport) as client:
        dest = tmp_path / 'release.zip'
        with pytest.raises(ValueError, match='received HTML'):
            await nar.download_file('https://publisher.example/release.zip', dest, client)
    assert not dest.exists()
