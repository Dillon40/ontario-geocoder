### Quick Test Commands

Check if the API is running:
curl http://localhost:3000/

Health check:
curl http://localhost:3000/health

Statistics:
curl http://localhost:3000/stats

### Clear REDIS Commands
Clear cache and test property:

docker exec -it geocoder-redis redis-cli FLUSHDB && curl -i -X POST http://localhost:3000/geocode
-H "Content-Type: application/json" -d '{"pin":"212310074"}'

Or if the container has a different name:

First, find the Redis container name:
docker ps | grep redis

Then use it:
docker exec -it <redis-container-name> redis-cli FLUSHDB && curl -i -X POST
http://localhost:3000/geocode -H "Content-Type: application/json" -d '{"pin":"212310074"}'

Other useful Docker Redis commands:

# Check Redis is running
docker exec -it geocoder-redis redis-cli ping

# See all cache keys
docker exec -it geocoder-redis redis-cli --scan --pattern "geocode:*"

# Clear only geocode cache entries
docker exec -it geocoder-redis redis-cli --scan --pattern "geocode:*" | xargs docker exec -i
geocoder-redis redis-cli DEL

# Get cache stats
docker exec -it geocoder-redis redis-cli INFO stats

The container name is `geocoder-redis` in both `docker-compose.yml` and `docker-compose.prod.yml`
(pinned via `container_name`, so it won't get a numeric suffix). Use `docker ps` to confirm if
running under a different Compose project or override.

### Simple String Address (Easiest)

curl -X POST http://localhost:3000/geocode \
-H "Content-Type: application/json" \
-d '{
    "address": "123 Main Street, Toronto, ON M5H 2N2"
}'

### Structured Address (All Available Fields - snake_case)

curl -X POST http://localhost:3000/geocode \
-H "Content-Type: application/json" \
-d '{
    "street_number": "123",
    "street_name": "Main",
    "street_suffix": "Street",
    "street_direction": "E",
    "unit_no": "Suite 100",
    "city": "Toronto",
    "province": "Ontario",
    "postal_code": "M5H 2N2",
    "pin": "12345678"
}'

### Structured Address (camelCase format)

curl -X POST http://localhost:3000/geocode \
-H "Content-Type: application/json" \
-d '{
    "streetNumber": "123",
    "streetName": "Main",
    "streetSuffix": "Street",
    "streetDirection": "E",
    "unitNumber": "Suite 100",
    "city": "Toronto",
    "province": "Ontario",
    "postalCode": "M5H 2N2",
    "PIN": "12345678"
}'

### IDX Format (Nested Address Object)

curl -X POST http://localhost:3000/geocode \
-H "Content-Type: application/json" \
-d '{
    "Address": {
    "StreetNumber": "123",
    "StreetName": "Main",
    "StreetSuffix": "Street",
    "StreetDirection": "E",
    "UnitNumber": "Suite 100",
    "City": "Toronto",
    "Province": "Ontario",
    "PostalCode": "M5H 2N2",
    "Country": "Canada"
    }
}'

### PIN-Only Lookup (Tier 1 Direct)

curl -X POST http://localhost:3000/geocode \
-H "Content-Type: application/json" \
-d '{
    "pin": "12345678"
}'

### Batch Request

curl -X POST http://localhost:3000/geocode/batch \
-H "Content-Type: application/json" \
-d '{
    "addresses": [
    {
        "streetNumber": "123",
        "streetName": "Main",
        "streetSuffix": "Street",
        "city": "Toronto",
        "province": "Ontario",
        "postalCode": "M5H 2N2"
    },
    {
        "streetNumber": "456",
        "streetName": "Queen",
        "streetSuffix": "Street",
        "city": "Toronto",
        "province": "Ontario",
        "postalCode": "M4C 1S6"
    }
    ]
}'

**Or using simple string addresses:**

curl -X POST http://localhost:3000/geocode/batch \
-H "Content-Type: application/json" \
-d '{
    "addresses": [
    {
        "address": "123 Main Street, Toronto, ON M5H 2N2"
    },
    {
        "address": "456 Queen Street, Toronto, ON M4C 1S6"
    }
    ]
}'

**For batch geocoding via JSON file:**

curl -X POST http://localhost:3000/geocode/batch \
    -H "Content-Type: application/json" \
    -d @sample_input_data/toronto_sample_properties_nopin.json

Note: The batch endpoint expects the format {"addresses": [...]}, so the first
commands wrap your JSON array appropriately. 

**The batch endpoint supports up to 1000 addresses per request.**

