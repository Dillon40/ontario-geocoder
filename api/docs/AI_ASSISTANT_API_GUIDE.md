# Guide for AI Assistants Using This API

This guide helps AI assistants (like Claude) understand the deployment architecture to avoid common misunderstandings.

## Key Architecture Facts

### Single-Server Co-Located Deployment

**Everything runs on ONE production server:**

```
Production Server (one machine)
├── PostgreSQL (Docker, geocoder-postgres) ← Docker network only, no host port mapping
├── Redis (Docker, geocoder-redis)         ← Docker network only, no host port mapping
├── FastAPI (Docker, geocoder-api)         ← same Docker Compose network as above
└── Cloudflare Tunnel / reverse proxy      ← routes external traffic to the published API port
```

All three services (`api`, `postgres`, `redis`) are Docker containers defined in
`docker-compose.prod.yml`, on the same `geocoder-network`. There is no LXC container in the
deployment — see `DEPLOYMENT.md` for the authoritative architecture reference.

### Critical Clarifications

| Statement | Truth |
|-----------|-------|
| "Database is at localhost:5432 on the server" | No - `postgres` has **no host port mapping** in production; it's reachable only from other containers on the `geocoder-network`, by hostname `postgres` |
| "Database is remote at some IP" | No - database only accessible from other containers on the Docker network |
| "I need to connect to my server's IP for database" | No - that's just for Cloudflare routing, not database access |
| "API and database are on different machines" | No - they're on the same server, same Docker Compose network |
| "Import scripts run from my laptop" | No - they run via the `etl` Compose service (or `docker exec`), on the server |

## API Access

### External API Endpoint

- **URL**: `https://geocoder.yourdomain.com`
- **Access**: Via Cloudflare Tunnel (internet → server)
- **Method**: POST to `/v1/geocode` or `/v1/geocode/batch` (unversioned `/geocode` and
  `/geocode/batch` still work as DEPRECATED aliases)
- **Auth**: Requires `X-API-Key` header

### Request Example

```bash
curl -X POST https://geocoder.yourdomain.com/v1/geocode \
  -H "Content-Type: application/json" \
  -H "X-API-Key: gc_your_api_key_here" \
  -d '{
    "Address": {
      "StreetNumber": "123",
      "StreetName": "Main",
      "StreetSuffix": "Street",
      "City": "Toronto",
      "Province": "ON",
      "PostalCode": "M5H 2N2"
    }
  }'
```

### Response Example

```json
{
  "latitude": 43.6532,
  "longitude": -79.3832,
  "source": "municipal_data",
  "confidence": 0.95,
  "property_id": "ADDR-00123456",
  "matched_address": "123 Main Street, Toronto, ON M5H 2N2"
}
```

## Common Mistakes to Avoid

### Mistake 1: Assuming Remote Database

**Wrong Assumption:**
> "The production database is at your server's IP, so I need to connect there to import data"

**Reality:**
- The database is in a Docker container (`geocoder-postgres`) on the production server
- It has **no host port mapping** — not even on the server's own `localhost:5432`
- It's only reachable from other containers on the `geocoder-network`, by hostname `postgres`
- IP addresses mentioned are for Cloudflare tunnel routing, not database access
- Import scripts must run **as (or alongside) a container on `geocoder-network`** — see the `etl`
  Compose service below

### Mistake 2: Assuming `localhost:5432` Works on the Server

**Wrong Assumption:**
> "I'll SSH into the server and connect to localhost:5432 to reach the database"

**Reality:**
- `postgres` publishes no host port in production, so `localhost:5432` doesn't work even
  from the server itself
- Reach it via the `etl` Compose service (`docker compose run --rm etl ...`, `DB_HOST=postgres`)
  or by execing into the container (`docker exec -it geocoder-postgres psql ...`)
- A bare `DB_HOST=localhost` script invocation on the server host will get connection refused

### Mistake 3: Thinking Components are Distributed

**Wrong Assumption:**
> "The docs mention different container names, so the API and database must be on different servers"

**Reality:**
- `geocoder-api`, `geocoder-postgres`, and `geocoder-redis` are all Docker containers on the
  **same physical/virtual server**, on the same `geocoder-network`
- They communicate via Docker network hostnames (`postgres`, `redis`), not IPs
- No network latency between them - it's all local

### Mistake 4: Trying to Import Data Remotely

**Wrong Assumption:**
> "I'll run the import script from my laptop with DB_HOST=production-server-ip"

**Reality:**
- `postgres` has no host port mapping at all - not from the server, and definitely not from
  the internet
- Import scripts must run **as a container on `geocoder-network`**, on the production server
- Process: SSH into server → `docker compose -f docker-compose.prod.yml run --rm etl ...`

## Correct Understanding

### For API Testing/Usage

```bash
# CORRECT: Use the external API endpoint
curl -X POST https://geocoder.yourdomain.com/v1/geocode \
  -H "X-API-Key: your_key" \
  -d '{"Address": {...}}'

# WRONG: Try to connect to database directly
# The database is not exposed externally!
```

### For Data Import

```bash
# CORRECT: SSH into server, then import via the etl Compose service
ssh user@production-server
cd /path/to/geocoder
docker compose -f docker-compose.prod.yml run --rm etl \
  data_sourcing.importers.import_production_data --file data.geojson --source city

# WRONG: Try to import from your laptop, or with a bare DB_HOST=localhost on the server
DB_HOST=production-server-ip uv run data_sourcing/importers/import_production_data.py ...
DB_HOST=localhost uv run data_sourcing/importers/import_production_data.py ...
# Neither works - postgres has no host port mapping, on the server or off it
```

### For Local Development

```bash
# CORRECT: Run your own local Postgres - neither docker-compose.yml nor
# docker-compose.prod.yml publishes a Postgres host port, so run one yourself
docker run -d -p 5433:5432 -e POSTGRES_PASSWORD=devpass -e POSTGRES_DB=geocoder postgis/postgis:15-3.3
DB_HOST=localhost DB_PORT=5433 uv run uvicorn api.main:app --reload

# WRONG: Try to connect to the production server's database
DB_HOST=203.0.113.10 DB_PORT=5432 ...
DB_HOST=production-server-ip DB_PORT=5432 ...
# postgres has no host port mapping in docker-compose.prod.yml - not on localhost,
# and definitely not externally
```

## Current Database Status

The production database (`geocoder-postgres` container) currently contains:

- **Properties**: 563,011 records
  - Toronto: 525,409
  - Guelph: 37,602
- **NAR Addresses**: 795,039 records
- **NAR Locations**: 734,119 records
- **FSA Boundaries**: 1,643 polygons
- **Municipal Boundaries**: 685 polygons
- **FSA-Municipality Mappings**: 1,850 records
- **Municipality Aliases**: 11 records

**All data is already present** - no imports needed for basic operation.

## Quick Reference

| What You Want to Do | How to Do It |
|---------------------|--------------|
| Test the geocoding API | Use the Cloudflare Tunnel URL with X-API-Key header |
| Import new data | SSH into server, run the `etl` Compose service |
| Develop locally | Run your own local Postgres/Redis (see "For Local Development" above) |
| Check database status | SSH into server, `docker exec -it geocoder-postgres psql -U "$DB_USER" -d "$DB_NAME"` |
| View API logs | SSH into server, `docker compose -f docker-compose.prod.yml logs -f api` |
| Restart API | SSH into server, `docker compose -f docker-compose.prod.yml restart api` |
| Backup database | SSH into server, run pg_dump from the `geocoder-postgres` container |

## Troubleshooting

### "Connection refused to database"

- **If from an import script**: Are you running it through the `etl` Compose service (on
  `geocoder-network`)? A bare `DB_HOST=localhost`, even run on the server itself, will not reach
  `postgres` - it has no host port mapping.
- **If from your dev machine**: Run your own local Postgres and point `DB_HOST`/`DB_PORT` at it.

### "API returns errors"

- **Check API logs**: SSH into server, `docker compose -f docker-compose.prod.yml logs -f api`
- **Check API health**: `curl https://geocoder.yourdomain.com/v1/health`
- **Verify database**: On server, `docker ps | grep geocoder-postgres`

### "Import script can't find data files"

- **Data files must be ON the server**: SCP/upload them first
- **Use full paths**: `/home/user/geocoder/regional_data_sources/file.geojson`

## Additional Documentation

- **DEPLOYMENT.md**: Detailed architecture with diagrams (the authoritative reference for this guide)
- **README.md**: General setup and API usage
- **docker-compose.prod.yml**: Production container configuration (with helpful comments)

---

**Remember**: When in doubt, assume everything is **co-located on one server**. The only external-facing component is the API via Cloudflare Tunnel - everything else is server-local.
