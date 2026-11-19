"""Coverage-aware live regression probe. Run inside the API container after imports.

API_KEY (or --key-file JSON containing 'key') must be an admin key. Temporary
consumer keys are revoked in finally blocks. No secret values are printed.
Use --full-coverage to require NAR as well as the municipal regression fixtures.
"""
import argparse
import json
import os
from pathlib import Path
import httpx
from engine.confidence import MUNICIPAL_LAYER_CONFIDENCE, NAR_LAYER_CONFIDENCE


def assert_match_contract(body, tier):
    """Check label and score semantics, not just HTTP success."""
    assert body['success'] and body['match_tier'] == tier
    assert isinstance(body.get('matched_address'), str) and body['matched_address'].strip()
    ceiling = {1: 1.0, 2: 0.99, 3: 0.90}[tier]
    assert 0 <= body['confidence'] <= ceiling
    factors = body['confidence_factors']
    assert factors['confidence_cap'] == ceiling
    assert factors['final_confidence'] == body['confidence']
    unclamped = round(factors['base_confidence'] + factors['geography_adjustment'], 6)
    assert factors['unclamped_confidence'] == unclamped
    assert body['confidence'] == round(max(0, min(ceiling, unclamped)), 6)
    assert factors['clamped'] == (body['confidence'] != unclamped)
    if tier in (2, 3) and not body.get('fuzzy_match'):
        table = MUNICIPAL_LAYER_CONFIDENCE if tier == 2 else NAR_LAYER_CONFIDENCE
        assert factors['base_confidence'] == table[body['match_layer']]


def verify(base_url, key, origin, full_coverage=False):
    report = {'passed': [], 'failed': [], 'skipped': [], 'coverage': None}

    def run(name, action):
        try:
            value = action()
        except (AssertionError, AttributeError, KeyError, TypeError, ValueError, httpx.HTTPError) as exc:
            # Never print response bodies, credentials or arbitrary exception values.
            reason = (str(exc) or 'contract assertion failed') if isinstance(exc, AssertionError) else type(exc).__name__
            report['failed'].append({'check': name, 'reason': reason})
            print(f'FAIL {name}: {reason}', flush=True)
            return None
        report['passed'].append(name)
        print(f'PASS {name}', flush=True)
        return value

    def skip(name, reason):
        report['skipped'].append({'check': name, 'reason': reason})
        print(f'SKIP {name}: {reason}', flush=True)

    def require(condition, message):
        assert condition, message

    nar_available = None
    with httpx.Client(base_url=base_url, timeout=120) as client:
        def check(name, method, path, status=200, auth=key, validate=None, capture=None, **kwargs):
            def request():
                headers = kwargs.pop('headers', {})
                if auth:
                    headers['X-API-Key'] = auth
                response = client.request(method, path, headers=headers, **kwargs)
                # Capture key IDs before assertions so cleanup can revoke them.
                if capture:
                    capture(response)
                assert response.status_code == status, f'expected HTTP {status}, got {response.status_code}'
                assert response.headers.get('x-request-id'), 'missing request ID'
                assert response.headers.get('x-content-type-options') == 'nosniff', 'missing security header'
                if validate:
                    validate(response)
                return response
            return run(name, request)

        check('health', 'GET', '/v1/health', auth=None)
        check('missing key', 'GET', '/v1/stats', 401, auth=None)
        check('invalid key', 'GET', '/v1/stats', 401, auth='gc_invalid')
        check('public schema', 'GET', '/openapi.json', auth=None)
        check('CORS preflight', 'OPTIONS', '/v1/geocode', auth=None, headers={
            'Origin': origin, 'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'x-api-key,content-type'},
            validate=lambda r: require(r.headers.get('access-control-allow-origin') == origin,
                                       'allowed origin missing from CORS response'))
        check('disallowed origin', 'OPTIONS', '/v1/geocode', 400, auth=None, headers={
            'Origin': 'https://not-allowed.invalid', 'Access-Control-Request-Method': 'POST'})

        def validate_stats(response):
            database = response.json()['database']
            assert isinstance(database['datasets'], dict), 'invalid municipal dataset counts'
            assert all(isinstance(n, int) and n >= 0 for n in database['datasets'].values()), 'invalid municipal row count'
            assert isinstance(database['nar_addresses_count'], int) and database['nar_addresses_count'] >= 0, 'invalid NAR row count'

        stats = check('stats', 'GET', '/v1/stats', validate=validate_stats)
        if stats is not None:
            database = stats.json()['database']
            report['coverage'] = database
            print('COVERAGE ' + json.dumps(database, sort_keys=True), flush=True)
            nar_available = database['nar_addresses_count'] > 0
            for source in ('guelph', 'toronto', 'ottawa', 'hamilton'):
                run(f'municipal coverage {source}', lambda s=source: require(
                    database['datasets'].get(s, 0) > 0, f'missing required municipal fixture source: {s}'))
            for excluded in ('middlesex_county', 'middlesex_county_addresses'):
                run(f'excluded source {excluded}', lambda s=excluded: require(
                    database['datasets'].get(s, 0) == 0, f'excluded municipal source is active: {s}'))
            if not nar_available:
                print('WARNING: NAR fallback unavailable; municipal operation can still be verified. '
                      'No province-wide coverage certification.', flush=True)
                if full_coverage:
                    run('required NAR coverage', lambda: require(False, 'full-coverage mode requires imported NAR addresses'))
        else:
            skip('source coverage', 'stats failed; source availability is unknown')

        check('attributions', 'GET', '/v1/attributions', validate=lambda r: require(
            all(a['source_key'] != 'middlesex_county_addresses' for a in r.json()['attributions']),
            'excluded Middlesex municipal attribution is active'))
        guelph = {'Address': {'StreetNumber': '51', 'StreetName': 'Cork', 'StreetSuffix': 'Street',
                             'StreetDirection': 'West', 'City': 'Guelph', 'Province': 'ON', 'PostalCode': 'N1H 2X3'}}
        hamilton = {'street_number': '681', 'street_name': 'Hall', 'street_suffix': 'Road', 'city': 'Hamilton', 'postal_code': 'L0R 1C0'}
        unit_1 = {'street_number': '154', 'street_name': 'Gage', 'street_suffix': 'Avenue',
                  'street_direction': 'North', 'city': 'Hamilton', 'postal_code': 'L8L7A4', 'unit_no': '1'}
        unit_2 = {**unit_1, 'unit_no': '2'}
        middlesex = {'street_number': '21605', 'street_name': 'Adelaide', 'street_direction': 'North',
                     'city': 'Middlesex Centre', 'postal_code': 'N0M1C0'}
        huntsville_postal = {'street_number': '55', 'street_name': 'Glen Acres', 'city': 'Huntsville', 'postal_code': 'P1H1N6'}
        cases = [
            ('README Guelph', guelph, 2), ('Hamilton', hamilton, 2),
            ('Hamilton unit 1', unit_1, 2), ('Hamilton unit 2', unit_2, 2),
            ('Toronto free text', {'address': '100 Queen Street West, Toronto, ON'}, 2),
            ('Ottawa', {'street_number': '110', 'street_name': 'Laurier', 'street_suffix': 'Avenue', 'street_direction': 'West', 'city': 'Ottawa'}, 2),
            ('Huntsville NAR', {'street_number': '55', 'street_name': 'Glen Acres', 'city': 'Huntsville'}, 3),
            ('Middlesex NAR', middlesex, 3), ('Huntsville postal NAR', huntsville_postal, 3),
        ]

        def validate_match(body, name, payload, tier):
            assert_match_contract(body, tier)
            assert body.get('attribution') and body.get('license_info'), 'missing licence/attribution'
            assert 41 < body['latitude'] < 57 and -96 < body['longitude'] < -73, 'coordinates outside Ontario bounds'
            if name.startswith('Hamilton unit '):
                assert body['source'] == 'municipal_hamilton'
                assert body['matched_address'] == f"Unit {payload['unit_no']}, 154 Gage Avenue North, Hamilton, ON L8L 7A4"
            if name == 'Hamilton':
                assert body['source'] == 'municipal_hamilton'
                assert body['matched_address'] == '681 Hall Road, Hamilton, ON L0R 1C0'
                assert abs(body['latitude'] - 43.07161804) < 0.000001
                assert abs(body['longitude'] - (-79.77144980)) < 0.000001
            if name == 'Middlesex NAR':
                assert body['source'] == 'nar_match'
                assert body['license_info']['commercial_use_allowed'] is True
                assert abs(body['latitude'] - 43.06061380) < 0.000001
                assert abs(body['longitude'] - (-81.26875973)) < 0.000001
            if name == 'README Guelph':
                assert body['matched_address'] == '51 Cork Street West'
                assert abs(body['latitude'] - 43.543327) < 0.0001
                assert isinstance(body['pin'], str) and isinstance(body['gpid'], str)

        single_results = {}
        for name, payload, tier in cases:
            if tier == 3 and not nar_available:
                reason = 'NAR has no imported addresses' if nar_available is False else 'NAR availability unknown: stats failed'
                skip(name, reason)
                skip(name + ' cached', reason)
                continue
            response = check(name, 'POST', '/v1/geocode', json=payload,
                             validate=lambda r, n=name, p=payload, t=tier: validate_match(r.json(), n, p, t))
            if response is None:
                skip(name + ' cached', 'initial match failed; cache parity cannot be established')
                continue
            body = response.json()
            single_results[name] = body

            def validate_cached(response, first=body, t=tier):
                cached = response.json()
                assert cached['cached'] is True, 'repeat lookup was not cached'
                assert_match_contract(cached, t)
                assert cached['matched_address'] == first['matched_address']
                assert cached['confidence_factors'] == first['confidence_factors']
            check(name + ' cached', 'POST', '/v1/geocode', json=payload, validate=validate_cached)

        def validate_pin(response):
            body = response.json()
            assert body['match_tier'] == 1 and body['pin'] == '712880109'
            assert_match_contract(body, 1)
        check('PIN lookup', 'POST', '/v1/geocode', json={'pin': '712880109'}, validate=validate_pin)
        missing = {'street_number': '99999999', 'street_name': 'ZZZNONEXISTENTAUDIT', 'city': 'Toronto'}
        check('no match', 'POST', '/v1/geocode', 404, json=missing)
        batch_names = ['README Guelph', 'Hamilton', 'Hamilton unit 1', 'Hamilton unit 2']
        if nar_available:
            batch_names += ['Middlesex NAR', 'Huntsville postal NAR']
        else:
            skip('NAR batch parity', 'NAR unavailable or unknown; municipal batch checks still run')
        batch_cases = [(n, p, t) for n, p, t in cases if n in batch_names]

        def validate_batch(response):
            body = response.json()
            assert body['summary']['successful'] == len(batch_cases) and body['summary']['failed'] == 1
            assert len(body['results']) == len(batch_cases) + 1
            for item, (name, payload, tier) in zip(body['results'], batch_cases):
                validate_match(item, name, payload, tier)
                if name in single_results:
                    assert item['matched_address'] == single_results[name]['matched_address']
                    assert item['confidence_factors'] == single_results[name]['confidence_factors']
            assert body['results'][-1]['success'] is False
        check('batch parity', 'POST', '/v1/geocode/batch',
              json={'addresses': [p for _, p, _ in batch_cases] + [missing]}, validate=validate_batch)
        check('empty input', 'POST', '/v1/geocode', 400, json={})
        check('invalid field', 'POST', '/v1/geocode', 422, json={'streat_name': 'Queen'})
        check('malformed JSON', 'POST', '/v1/geocode', 400, content='{', headers={'Content-Type': 'application/json'})
        check('empty batch', 'POST', '/v1/geocode/batch', 422, json={'addresses': []})
        check('oversized batch', 'POST', '/v1/geocode/batch', 422, json={'addresses': [{}] * 1001})
        check('admin list', 'GET', '/v1/admin/keys')

        def temporary_key(label, payload, exercise):
            created = {}
            try:
                response = check('create ' + label, 'POST', '/v1/admin/keys', json=payload,
                                 capture=lambda r: created.update(r.json().get('data') or {}))
                def validate_credentials():
                    assert isinstance(created.get('id'), int) and created['id'] > 0, 'missing temporary key ID'
                    assert isinstance(created.get('key'), str) and created['key'], 'missing temporary key secret'
                    return created
                credentials = run(label + ' credentials', validate_credentials) if response is not None else None
                if credentials is None:
                    skip(label + ' dependent checks', 'temporary key creation failed')
                else:
                    exercise(credentials['key'])
            finally:
                if isinstance(created.get('id'), int) and created['id'] > 0:
                    check('revoke ' + label, 'DELETE', f"/v1/admin/keys/{created['id']}")
            if isinstance(created.get('key'), str) and isinstance(created.get('id'), int):
                check(label + ' revoked denied', 'GET', '/v1/stats', 401, auth=created['key'])

        def exercise_rate(consumer_key):
            check('nonadmin denial', 'GET', '/v1/admin/keys', 403, auth=consumer_key)
            check('rate first', 'GET', '/v1/stats', auth=consumer_key)
            check('rate second', 'GET', '/v1/stats', auth=consumer_key)
            check('rate exceeded', 'GET', '/v1/stats', 429, auth=consumer_key)
        temporary_key('key', {'name': 'smoke-rate', 'rate_limit_override': 2,
                             'permissions': {'stats': True, 'admin': False}}, exercise_rate)

        def exercise_ip(consumer_key):
            check('IP deny', 'GET', '/v1/stats', 403, auth=consumer_key)
            check('loopback spoof deny', 'GET', '/v1/stats', 403, auth=consumer_key,
                  headers={'X-Forwarded-For': '192.0.2.1'})
        temporary_key('IP key', {'name': 'smoke-ip', 'allowed_ips': ['192.0.2.1'],
                                'permissions': {'stats': True}}, exercise_ip)

    print(f"SUMMARY: {len(report['passed'])} passed, {len(report['failed'])} failed, "
          f"{len(report['skipped'])} skipped", flush=True)
    if report['failed']:
        print('FAIL live regression verification; see named failures and skips above', flush=True)
    elif nar_available:
        print('PASS municipal and NAR regression checks; source counts are not a province-wide accuracy audit', flush=True)
    else:
        print('PASS municipal operation verified; NAR fallback unavailable (not province-wide coverage)', flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://localhost:3000')
    parser.add_argument('--key-file')
    parser.add_argument('--origin', default=os.getenv('ALLOWED_ORIGINS', 'http://localhost:13000').split(',')[0])
    parser.add_argument('--full-coverage', action='store_true',
                        help='Require imported NAR data; still run independent checks if it is missing')
    args = parser.parse_args()
    key = json.loads(Path(args.key_file).read_text())['key'] if args.key_file else os.getenv('API_KEY')
    if not key:
        parser.error('Set API_KEY or --key-file; never disable authentication to run smoke tests')
    report = verify(args.base_url, key, args.origin, full_coverage=args.full_coverage)
    raise SystemExit(1 if report['failed'] else 0)


if __name__ == '__main__':
    main()
