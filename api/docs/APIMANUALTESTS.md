# Manual deployment checks

Follow [deployment setup and key creation](../../docs/DEPLOYMENT.md) first.
Set your actual port and a valid key (do not commit or paste the key in reports):

```bash
export BASE_URL=http://localhost:13000
read -rs API_KEY; export API_KEY
curl "$BASE_URL/v1/health"             # public: 200 when DB + Redis work
curl "$BASE_URL/v1/stats"              # no key: 401
curl -H "X-API-Key: $API_KEY" "$BASE_URL/v1/stats"
curl -H "X-API-Key: $API_KEY" "$BASE_URL/v1/attributions"
```

## Geocoding formats and validation

```bash
# Free-text input
curl "$BASE_URL/v1/geocode" -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"address":"100 Queen Street West, Toronto, ON"}'
# Structured snake_case (camelCase aliases streetNumber/streetName/etc. also work)
curl "$BASE_URL/v1/geocode" -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"street_number":"100","street_name":"Queen","street_suffix":"Street","street_direction":"West","city":"Toronto","province":"ON"}'
# Nested IDX format
curl "$BASE_URL/v1/geocode" -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"Address":{"StreetNumber":"51","StreetName":"Cork","StreetSuffix":"Street","StreetDirection":"West","City":"Guelph","Province":"ON","PostalCode":"N1H 2X3"}}'
# Hamilton: matched_address must be "681 Hall Road, Hamilton, ON L0R 1C0", not null
curl "$BASE_URL/v1/geocode" -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"street_number":"681","street_name":"Hall","street_suffix":"Road","city":"Hamilton","postal_code":"L0R 1C0"}'
# Middlesex: NAR (tier 3), confidence <= 0.90, never municipal_middlesex_county
curl "$BASE_URL/v1/geocode" -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"street_number":"21605","street_name":"Adelaide","street_direction":"North","city":"Middlesex Centre","postal_code":"N0M1C0"}'
# PIN-only (a parcel may have several addresses; inspect the matched address)
curl "$BASE_URL/v1/geocode" -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"pin":"712880109"}'
# Batch: 200 with independent per-item success, maximum 1000 items
curl "$BASE_URL/v1/geocode/batch" -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"addresses":[{"address":"100 Queen Street West, Toronto, ON"},{"street_number":"99999999","street_name":"ZZZNONEXISTENT","city":"Toronto"}]}'
# Empty body 400; malformed JSON 400; unknown fields 422
curl -i "$BASE_URL/v1/geocode" -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' -d '{}'
curl -i "$BASE_URL/v1/geocode" -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' -d '{'
curl -i "$BASE_URL/v1/geocode" -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' -d '{"streat_name":"Queen"}'
```

Check coordinates, direction, populated matched label, source and licence attribution. NAR
`confidence` must not exceed 0.90. Its `confidence_factors` must preserve the pre-geography base,
show the geographic adjustment and cap, and agree with the final score. Check the same fields
in batch responses. See the [confidence policy](../../docs/NAR_QUERY_CONFIDENCE_RATINGS.md).
Repeat a valid request to check
`cached=true`. A 404 means no match; it is not a server error. Missing reference data must not be
misreported as proof that a real city/FSA combination is geographically invalid.

## Browser preflight

Use the exact origin configured in `ALLOWED_ORIGINS`. Preflight has no API key:

```bash
curl -i -X OPTIONS "$BASE_URL/v1/geocode" \
  -H "Origin: $BASE_URL" -H 'Access-Control-Request-Method: POST' \
  -H 'Access-Control-Request-Headers: x-api-key,content-type'
```

Expect 200 and the allowed origin. Disallowed origins must not receive an allow-origin header.
Actual requests still require a valid key. `/docs` is a public UI with **Authorize** for operations.
Check that even 401/403/500 responses include `X-Request-ID` and security headers.

## Redis checks (production authentication required)

```bash
docker compose -f docker-compose.prod.yml exec redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli ping'
docker compose -f docker-compose.prod.yml exec redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli INFO stats'
```

Do not flush a shared/production Redis while testing. This project's container has a dedicated
volume. Rebuild/recreate the API after code changes; response cache versioning invalidates old
geocoding results when result semantics change. API-key/rate-limit state must not be cleared
merely to make a failing test pass.
