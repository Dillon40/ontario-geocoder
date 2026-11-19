"""Exercise the live probe over HTTP, without real credentials or services."""
import json

import httpx
import pytest

from api.scripts import verify_deployment as probe


class Deployment:
    def __init__(self, nar=0, fault=None):
        self.nar = nar
        self.fault = fault
        self.requests = []
        self.keys = {}
        self.cached = set()

    def match(self, payload):
        address = payload.get('Address', payload)
        city = address.get('City', address.get('city', ''))
        tier = 3 if city in ('Huntsville', 'Middlesex Centre') else 2
        if 'pin' in payload:
            tier = 1
        layer = 0
        score = (1.0 if tier == 1 else
                 probe.MUNICIPAL_LAYER_CONFIDENCE[layer] if tier == 2 else probe.NAR_LAYER_CONFIDENCE[layer])
        cap = {1: 1.0, 2: .99, 3: .90}[tier]
        confidence = min(score, cap)
        label, latitude, longitude, source = 'An address', 44.0, -79.0, 'municipal_example'
        if 'Address' in payload or 'pin' in payload:
            label, latitude, longitude, source = '51 Cork Street West', 43.543327, -80.253393, 'municipal_guelph'
        if city == 'Hamilton':
            source = 'municipal_hamilton'
            if address.get('unit_no'):
                label = f"Unit {address['unit_no']}, 154 Gage Avenue North, Hamilton, ON L8L 7A4"
            else:
                label, latitude, longitude = '681 Hall Road, Hamilton, ON L0R 1C0', 43.07161804, -79.77144980
        if tier == 3:
            source = 'nar_match'
        if city == 'Middlesex Centre':
            latitude, longitude = 43.06061380, -81.26875973
        cache_key = json.dumps(payload, sort_keys=True)
        cached = cache_key in self.cached
        self.cached.add(cache_key)
        return {'success': True, 'match_tier': tier, 'match_layer': layer, 'confidence': confidence,
                'matched_address': label, 'latitude': latitude, 'longitude': longitude, 'source': source,
                'attribution': 'Publisher credit', 'license_info': {'commercial_use_allowed': True},
                'pin': '712880109', 'gpid': '2000001751', 'cached': cached,
                'confidence_factors': {'confidence_cap': cap, 'final_confidence': confidence,
                    'base_confidence': score, 'geography_adjustment': 0,
                    'unclamped_confidence': score, 'clamped': confidence != score}}

    def handle(self, request):
        self.requests.append(request)
        method, path = request.method, request.url.path
        headers = {'x-request-id': 'test-request', 'x-content-type-options': 'nosniff'}

        def response(status=200, body=None):
            return httpx.Response(status, headers=headers, json=body if body is not None else {})

        auth = request.headers.get('X-API-Key')
        consumer = self.keys.get(auth)
        if method == 'OPTIONS':
            if request.headers['origin'] == 'https://allowed.invalid':
                headers['access-control-allow-origin'] = 'https://allowed.invalid'
                return response()
            return response(400)
        if path in ('/v1/health', '/openapi.json'):
            return response()
        if auth != 'test-admin':
            if not consumer or not consumer['active']:
                return response(401)
            if consumer.get('allowed_ips'):
                return response(403)
            if path.startswith('/v1/admin'):
                return response(403)
            consumer['requests'] += 1
            if consumer['requests'] > consumer['limit']:
                return response(429)
        if path == '/v1/stats':
            if self.fault == 'stats':
                return response(500)
            return response(body={'database': {'nar_addresses_count': self.nar,
                'datasets': {'guelph': 55908, 'hamilton': 274779, 'toronto': 525061, 'ottawa': 404807}}})
        if path == '/v1/attributions':
            return response(body={'attributions': []})
        if path == '/v1/admin/keys' and method == 'POST':
            body = json.loads(request.content)
            identifier = len(self.keys) + 1
            secret = f'temporary-secret-{identifier}'
            self.keys[secret] = {'id': identifier, 'active': True, 'requests': 0,
                                 'limit': body.get('rate_limit_override', 100),
                                 'allowed_ips': body.get('allowed_ips')}
            if self.fault == 'creation_headers' and identifier == 1:
                del headers['x-request-id']
            data = {'id': identifier, 'key': secret}
            if self.fault == 'creation_credentials' and identifier == 1:
                del data['key']
            return response(body={'data': data})
        if path.startswith('/v1/admin/keys/') and method == 'DELETE':
            identifier = int(path.rsplit('/', 1)[1])
            if self.fault == 'revocation' and identifier == 1:
                return response(500)
            for key in self.keys.values():
                if key['id'] == identifier:
                    key['active'] = False
            return response()
        if path == '/v1/admin/keys':
            return response()
        if path.startswith('/v1/geocode'):
            try:
                body = json.loads(request.content)
            except ValueError:
                return response(400)
            if path.endswith('/batch'):
                addresses = body['addresses']
                if not addresses or len(addresses) > 1000:
                    return response(422)
                results = [self.match(p) for p in addresses[:-1]] + [{'success': False}]
                return response(body={'results': results, 'summary': {'successful': len(results) - 1, 'failed': 1}})
            if not body:
                return response(400)
            if 'streat_name' in body:
                return response(422)
            if body.get('street_number') == '99999999':
                return response(404)
            if self.fault == 'municipal_match' and 'Address' in body:
                return response(404)
            if self.fault == 'nar_match' and body.get('city') == 'Huntsville':
                return response(404)
            result = self.match(body)
            if self.fault == 'municipal_contract' and 'Address' in body:
                result['matched_address'] = 'Wrong direction'
            return response(body=result)
        raise AssertionError(f'Unexpected test route: {method} {path}')


@pytest.fixture
def verify(monkeypatch):
    real_client = httpx.Client

    def run(nar=0, fault=None, full_coverage=False):
        deployment = Deployment(nar, fault)
        monkeypatch.setattr(probe.httpx, 'Client', lambda **kwargs: real_client(
            **kwargs, transport=httpx.MockTransport(deployment.handle)))
        report = probe.verify('http://test', 'test-admin', 'https://allowed.invalid', full_coverage)
        return report, deployment
    return run


def names(items):
    return [item['check'] for item in items]


def test_empty_nar_verifies_municipal_and_security_checks(verify, capsys):
    report, deployment = verify()
    assert not report['failed']
    assert len(report['skipped']) == 7  # Three NAR fixtures + caches, NAR batch portion
    assert 'batch parity' in report['passed']
    assert 'rate exceeded' in report['passed'] and 'loopback spoof deny' in report['passed']
    assert all(not key['active'] for key in deployment.keys.values())
    assert not any(b'Huntsville' in r.content or b'Middlesex Centre' in r.content for r in deployment.requests)
    output = capsys.readouterr().out
    assert 'municipal operation verified; NAR fallback unavailable' in output
    assert '0 failed, 7 skipped' in output
    assert 'temporary-secret-' not in output


def test_strict_missing_nar_fails_but_runs_independent_checks(verify):
    report, deployment = verify(full_coverage=True)
    assert names(report['failed']) == ['required NAR coverage']
    assert 'README Guelph' in report['passed'] and 'batch parity' in report['passed']
    assert 'loopback spoof deny' in report['passed']
    assert len(report['skipped']) == 7
    assert all(not key['active'] for key in deployment.keys.values())


@pytest.mark.parametrize('strict', [False, True])
def test_populated_nar_runs_nar_single_cache_and_batch_checks(verify, strict):
    report, deployment = verify(nar=100, full_coverage=strict)
    assert not report['failed'] and not report['skipped']
    assert 'Huntsville NAR' in report['passed'] and 'Middlesex NAR cached' in report['passed']
    batch = next(json.loads(r.content) for r in deployment.requests if r.url.path.endswith('/batch')
                 and len(json.loads(r.content)['addresses']) == 7)
    assert len(batch['addresses']) == 7


@pytest.mark.parametrize('fault', ['municipal_match', 'municipal_contract'])
def test_failed_match_is_not_printed_as_pass_and_does_not_abort(verify, capsys, fault):
    report, deployment = verify(fault=fault)
    assert 'README Guelph' in names(report['failed'])
    assert 'README Guelph' not in report['passed']
    assert 'README Guelph cached' in names(report['skipped'])
    assert 'Hamilton' in report['passed'] and 'rate exceeded' in report['passed']
    assert 'loopback spoof deny' in report['passed']
    assert all(not key['active'] for key in deployment.keys.values())
    assert 'PASS README Guelph\n' not in capsys.readouterr().out


def test_populated_but_broken_nar_is_failure_not_optional_skip(verify):
    report, _ = verify(nar=100, fault='nar_match')
    assert 'Huntsville NAR' in names(report['failed'])
    assert 'Middlesex NAR' in report['passed'] and 'rate exceeded' in report['passed']
    assert 'NAR batch parity' not in names(report['skipped'])


def test_failed_stats_does_not_masquerade_as_municipal_success(verify, capsys):
    report, _ = verify(fault='stats')
    assert 'stats' in names(report['failed']) and report['coverage'] is None
    assert 'README Guelph' in report['passed'] and 'loopback spoof deny' in report['passed']
    assert 'source coverage' in names(report['skipped'])
    assert 'PASS municipal operation verified' not in capsys.readouterr().out


@pytest.mark.parametrize('fault', ['creation_headers', 'creation_credentials'])
def test_known_created_ids_are_revoked_even_if_creation_contract_fails(verify, fault):
    report, deployment = verify(fault=fault)
    assert report['failed']
    assert len(deployment.keys) == 2
    assert all(not key['active'] for key in deployment.keys.values())
    assert 'loopback spoof deny' in report['passed']


def test_revocation_failure_is_reported_and_other_key_checks_continue(verify):
    report, _ = verify(fault='revocation')
    assert 'revoke key' in names(report['failed'])
    assert 'key revoked denied' in names(report['failed'])
    assert 'loopback spoof deny' in report['passed'] and 'revoke IP key' in report['passed']


@pytest.mark.parametrize('failed,strict,expected', [(False, False, 0), (True, True, 1)])
def test_cli_exit_code_and_full_coverage_flag(monkeypatch, failed, strict, expected):
    from unittest.mock import MagicMock
    monkeypatch.setenv('API_KEY', 'dummy-admin')
    monkeypatch.setattr('sys.argv', ['verify_deployment'] + (['--full-coverage'] if strict else []))
    verify = MagicMock(return_value={'failed': ['failure'] if failed else []})
    monkeypatch.setattr(probe, 'verify', verify)
    with pytest.raises(SystemExit) as exc:
        probe.main()
    assert exc.value.code == expected
    assert verify.call_args.kwargs['full_coverage'] is strict
