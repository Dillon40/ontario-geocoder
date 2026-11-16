"""Live, assertive smoke test. Run inside the API container after a full import.

API_KEY (or --key-file JSON containing 'key') must be an admin key. Temporary
consumer keys are revoked in finally blocks. No secret values are printed.
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


def verify(base_url, key, origin):
    count=0
    with httpx.Client(base_url=base_url,timeout=120) as client:
        def check(name, method, path, status=200, auth=key, **kwargs):
            nonlocal count
            headers=kwargs.pop('headers',{})
            if auth: headers['X-API-Key']=auth
            response=client.request(method,path,headers=headers,**kwargs)
            assert response.status_code==status, f'{name}: expected {status}, got {response.status_code}: {response.text[:200]}'
            assert response.headers.get('x-request-id'), f'{name}: missing request ID'
            assert response.headers.get('x-content-type-options')=='nosniff', f'{name}: missing security header'
            count+=1
            print(f'PASS {name}',flush=True)
            return response
        check('health','GET','/v1/health',auth=None)
        check('missing key','GET','/v1/stats',401,auth=None)
        check('invalid key','GET','/v1/stats',401,auth='gc_invalid')
        check('public schema','GET','/openapi.json',auth=None)
        preflight=check('CORS preflight','OPTIONS','/v1/geocode',auth=None,headers={
            'Origin':origin,'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'x-api-key,content-type'})
        assert preflight.headers['access-control-allow-origin']==origin
        check('disallowed origin','OPTIONS','/v1/geocode',400,auth=None,headers={
            'Origin':'https://not-allowed.invalid','Access-Control-Request-Method':'POST'})
        stats=check('stats','GET','/v1/stats').json()
        assert stats['database']['nar_addresses_count']>0
        for source in ('guelph','toronto','ottawa','hamilton'):
            assert stats['database']['datasets'].get(source,0)>0, f'Missing source: {source}'
        for excluded in ('middlesex_county', 'middlesex_county_addresses'):
            assert stats['database']['datasets'].get(excluded,0)==0, f'Excluded source active: {excluded}'
        attributions=check('attributions','GET','/v1/attributions').json()['attributions']
        assert all(a['source_key']!='middlesex_county_addresses' for a in attributions)
        guelph={'Address':{'StreetNumber':'51','StreetName':'Cork','StreetSuffix':'Street','StreetDirection':'West','City':'Guelph','Province':'ON','PostalCode':'N1H 2X3'}}
        hamilton={'street_number':'681','street_name':'Hall','street_suffix':'Road','city':'Hamilton','postal_code':'L0R 1C0'}
        hamilton_unit_1={'street_number':'154','street_name':'Gage','street_suffix':'Avenue','street_direction':'North','city':'Hamilton','postal_code':'L8L7A4','unit_no':'1'}
        hamilton_unit_2={**hamilton_unit_1,'unit_no':'2'}
        middlesex={'street_number':'21605','street_name':'Adelaide','street_direction':'North','city':'Middlesex Centre','postal_code':'N0M1C0'}
        huntsville_postal={'street_number':'55','street_name':'Glen Acres','city':'Huntsville','postal_code':'P1H1N6'}
        for name,payload,tier in [
            ('README Guelph',guelph,2),
            ('Hamilton',hamilton,2),
            ('Hamilton unit 1',hamilton_unit_1,2),
            ('Hamilton unit 2',hamilton_unit_2,2),
            ('Toronto free text',{'address':'100 Queen Street West, Toronto, ON'},2),
            ('Ottawa',{'street_number':'110','street_name':'Laurier','street_suffix':'Avenue','street_direction':'West','city':'Ottawa'},2),
            ('Huntsville NAR',{'street_number':'55','street_name':'Glen Acres','city':'Huntsville'},3),
            ('Middlesex NAR',middlesex,3),
            ('Huntsville postal NAR',huntsville_postal,3)]:
            body=check(name,'POST','/v1/geocode',json=payload).json()
            assert_match_contract(body, tier)
            if name.startswith('Hamilton unit '):
                unit=payload['unit_no']
                assert body['source']=='municipal_hamilton'
                assert body['matched_address']==f'Unit {unit}, 154 Gage Avenue North, Hamilton, ON L8L 7A4'
            if name=='Hamilton':
                assert body['source']=='municipal_hamilton'
                assert body['matched_address']=='681 Hall Road, Hamilton, ON L0R 1C0'
                assert abs(body['latitude']-43.07161804)<0.000001
                assert abs(body['longitude']-(-79.77144980))<0.000001
            assert body.get('attribution') and body.get('license_info')
            assert 41 < body['latitude'] < 57 and -96 < body['longitude'] < -73
            if name=='Middlesex NAR':
                assert body['source']=='nar_match'
                assert body['license_info']['commercial_use_allowed'] is True
                assert abs(body['latitude']-43.06061380)<0.000001
                assert abs(body['longitude']-(-81.26875973))<0.000001
            if name=='README Guelph':
                assert body['matched_address']=='51 Cork Street West'
                assert abs(body['latitude']-43.543327)<0.0001
                assert isinstance(body['pin'],str) and isinstance(body['gpid'],str)
            cached=check(name+' cached','POST','/v1/geocode',json=payload).json()
            assert cached['cached'] is True
            assert_match_contract(cached, tier)
            assert cached['matched_address']==body['matched_address']
            assert cached['confidence_factors']==body['confidence_factors']
        pin=check('PIN lookup','POST','/v1/geocode',json={'pin':'712880109'}).json()
        assert pin['match_tier']==1 and pin['pin']=='712880109'
        assert_match_contract(pin, 1)
        missing={'street_number':'99999999','street_name':'ZZZNONEXISTENTAUDIT','city':'Toronto'}
        check('no match','POST','/v1/geocode',404,json=missing)
        batch=check('batch parity','POST','/v1/geocode/batch',json={'addresses':[guelph,hamilton,middlesex,huntsville_postal,hamilton_unit_1,hamilton_unit_2,missing]}).json()
        assert batch['summary']['successful']==6 and batch['summary']['failed']==1
        for item,tier in zip(batch['results'][:6],(2,2,3,3,2,2)):
            assert_match_contract(item, tier)
        assert batch['results'][1]['matched_address']=='681 Hall Road, Hamilton, ON L0R 1C0'
        for index,unit in ((4,'1'),(5,'2')):
            assert batch['results'][index]['matched_address']==f'Unit {unit}, 154 Gage Avenue North, Hamilton, ON L8L 7A4'
        assert batch['results'][0]['matched_address']=='51 Cork Street West'
        assert isinstance(batch['results'][0]['pin'],str)
        check('empty input','POST','/v1/geocode',400,json={})
        check('invalid field','POST','/v1/geocode',422,json={'streat_name':'Queen'})
        check('malformed JSON','POST','/v1/geocode',400,content='{',headers={'Content-Type':'application/json'})
        check('empty batch','POST','/v1/geocode/batch',422,json={'addresses':[]})
        check('oversized batch','POST','/v1/geocode/batch',422,json={'addresses':[{}]*1001})
        check('admin list','GET','/v1/admin/keys')
        limited=check('create key','POST','/v1/admin/keys',json={'name':'smoke-rate','rate_limit_override':2,'permissions':{'stats':True,'admin':False}}).json()['data']
        try:
            check('nonadmin denial','GET','/v1/admin/keys',403,auth=limited['key'])
            check('rate first','GET','/v1/stats',auth=limited['key'])
            check('rate second','GET','/v1/stats',auth=limited['key'])
            check('rate exceeded','GET','/v1/stats',429,auth=limited['key'])
        finally:
            check('revoke key','DELETE',f"/v1/admin/keys/{limited['id']}")
        check('revoked denied','GET','/v1/stats',401,auth=limited['key'])
        ip=check('create IP key','POST','/v1/admin/keys',json={'name':'smoke-ip','allowed_ips':['192.0.2.1'],'permissions':{'stats':True}}).json()['data']
        try:
            check('IP deny','GET','/v1/stats',403,auth=ip['key'])
            check('loopback spoof deny','GET','/v1/stats',403,auth=ip['key'],headers={'X-Forwarded-For':'192.0.2.1'})
        finally:
            check('revoke IP key','DELETE',f"/v1/admin/keys/{ip['id']}")
    print(f'PASS all {count} live checks')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default='http://localhost:3000')
    parser.add_argument('--key-file')
    parser.add_argument('--origin',default=os.getenv('ALLOWED_ORIGINS','http://localhost:13000').split(',')[0])
    args=parser.parse_args()
    key=json.loads(Path(args.key_file).read_text())['key'] if args.key_file else os.getenv('API_KEY')
    if not key: parser.error('Set API_KEY or --key-file; never disable authentication to run smoke tests')
    verify(args.base_url,key,args.origin)
