# Deployment Architecture

This document clarifies the production deployment architecture for the Property Geocoder service.

## Authoritative Deployment: Docker Compose

**ALL COMPONENTS RUN AS DOCKER SERVICES**, defined in `docker-compose.prod.yml`. There is no
LXC container in the deployment — API, PostgreSQL, and Redis are all containers on the same
Docker Compose network (`geocoder-network`).

### Quick Reference

| Component | Location | How to Access |
|-----------|----------|----------------|
| **Production Database** | Docker container `geocoder-postgres` | Docker network hostname `postgres` only — **not exposed on the host** |
| **Production API** | Docker container `geocoder-api` | Host port `${API_PORT:-3000}` → reverse proxy / tunnel for external access |
| **Redis Cache** | Docker container `geocoder-redis` | Docker network hostname `redis` only — **not exposed on the host**, password-protected |
| **Import/ETL Scripts** | Docker container `geocoder-etl` (on-demand profile) or run on the host with `DB_HOST` pointed at the exposed dev port | See "Running Imports" below |
| **Your Dev Machine** | Remote | SSH tunnel, or a separate local dev database (Postgres is not reachable from outside the Docker network in prod) |

**Key Facts:**
- All three core services (`api`, `postgres`, `redis`) are Docker containers defined in `docker-compose.prod.yml`.
- PostgreSQL and Redis have **no host port mappings** in production — they are reachable only via
  the internal `geocoder-network`, by hostname (`postgres`, `redis`).
- The API container **does** publish a host port (`${API_PORT:-3000}:3000`) so a reverse proxy or
  tunnel in front of it can reach it.
- `DB_PASSWORD` and `REDIS_PASSWORD` have no defaults — Compose fails to start (`:?...required`
  interpolation) if they are unset. There is no hardcoded fallback password anywhere in prod.
- The `etl` service is not started by `docker compose up`; it runs on-demand via
  `docker compose run --rm etl ...` and shares the same network, so it reaches Postgres by hostname
  too.

## Current Production Setup

### Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    SINGLE PRODUCTION SERVER                              │
│              (docker-compose.prod.yml — all services)                    │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────┐    │
│  │              External Traffic (Internet)                        │    │
│  └──────────────────────────┬─────────────────────────────────────┘    │
│                             │                                           │
│                             ▼                                           │
│                  ┌──────────────────────┐                               │
│                  │ Reverse proxy /      │                               │
│                  │ tunnel (Cloudflare,  │                               │
│                  │ nginx, etc.)         │                               │
│                  └──────────┬───────────┘                               │
│                             │                                           │
│                             ▼                                           │
│                  ┌─────────────────────────────┐                        │
│                  │   Docker container: api      │                       │
│                  │   container_name:            │                       │
│                  │     geocoder-api             │                       │
│                  │  ┌────────────────────┐     │                        │
│                  │  │  FastAPI (gunicorn) │     │                       │
│                  │  │  Host port:         │     │                       │
│                  │  │  ${API_PORT:-3000}  │     │                       │
│                  │  └─────────┬──────────┘     │                        │
│                  └────────────┼────────────────┘                        │
│                               │ DB_HOST=postgres, REDIS_URL=redis://redis:6379│
│                               │ (Docker network: geocoder-network)       │
│                               ▼                                         │
│                ┌───────────────┴───────────────┐                        │
│                │                               │                        │
│                ▼                               ▼                        │
│      ┌──────────────────┐           ┌──────────────────┐               │
│      │   PostgreSQL     │           │     Redis        │               │
│      │  (postgis:15-3.3)│           │   (redis:7-alpine)│              │
│      │                  │           │                  │               │
│      │ container_name:  │           │ container_name:  │               │
│      │ geocoder-postgres│           │ geocoder-redis   │               │
│      │                  │           │                  │               │
│      │ NOT exposed on   │           │ NOT exposed on   │               │
│      │ the host — Docker│           │ the host —       │               │
│      │ network only     │           │ requirepass set  │               │
│      │                  │           │ via REDIS_PASSWORD│              │
│      │ Volume:          │           │                  │               │
│      │ postgres_data    │           │ Volume:          │               │
│      │                  │           │ redis_data (AOF) │               │
│      └──────────────────┘           └──────────────────┘               │
│                                                                          │
│  ETL container (geocoder-etl, `etl` profile — run on demand):           │
│  → docker compose -f docker-compose.prod.yml run --rm etl ...          │
│  → DB_HOST=postgres (same Docker network, not host localhost)          │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

## Component Details

### 1. Production API (`geocoder-api` container)

**Location:** Docker container, built from `Dockerfile`, defined under `api:` in `docker-compose.prod.yml`
**Service:** FastAPI with Gunicorn + Uvicorn workers
**Port:** `3000` internally, published to the host as `${API_PORT:-3000}`
**Access:** Via a reverse proxy / tunnel in front of the published host port

**Environment Variables (from `docker-compose.prod.yml`):**
```bash
DB_HOST=postgres              # Docker network hostname
DB_PORT=5432
DB_NAME=${DB_NAME:-geocoder}
DB_USER=${DB_USER:-geocoder}
DB_PASSWORD=${DB_PASSWORD:?DB_PASSWORD is required}   # no default — Compose refuses to start without it
REDIS_URL=redis://redis:6379
REDIS_PASSWORD=${REDIS_PASSWORD:?REDIS_PASSWORD is required}
TRUSTED_PROXIES=${TRUSTED_PROXIES:-}                  # set to your reverse proxy's IP/CIDR
API_KEY_PEPPER=${API_KEY_PEPPER:-}                    # recommended: HMAC secret for new key hashes
ALLOWED_ORIGINS=${ALLOWED_ORIGINS:?ALLOWED_ORIGINS is required - set explicit allowed origins, not *}
```

**Key Points:**
- Runs as a Docker container, same as Postgres and Redis — not LXC.
- Connects to the database via the Docker network hostname `postgres`.
- Connects to Redis via hostname `redis`, authenticating with `REDIS_PASSWORD`.
- `ALLOWED_ORIGINS` and a wildcard `*` origin combined with production `ENV` is a hard startup
  error (see `api/services/config_validator.py`) — the app will not boot with permissive CORS in prod.
- `TRUSTED_PROXIES` should be set to the reverse proxy's IP/CIDR whenever the API sits behind one;
  without it, `X-Forwarded-For`/`X-Real-IP` are ignored and per-key IP allowlisting sees the proxy's
  IP instead of the real client IP.
- Gunicorn workers configurable via `GUNICORN_WORKERS` (default 4).

### 2. Production Database (`geocoder-postgres` container)

**Container Name:** `geocoder-postgres`
**Image:** `postgis/postgis:15-3.3`
**Exposed Port:** **None** — no host port mapping in `docker-compose.prod.yml`. Reachable only from
other containers on `geocoder-network`, by hostname `postgres`.
**Network:** `geocoder-network`
**Volume:** `postgres_data` (persistent)

**Purpose:**
- **Production Data**: Single source of truth for all address data.
- **Schema init**: `database/schema.sql`, `database/migrations.sql`, and
  `database/seed_data_sources.sql` are mounted into `/docker-entrypoint-initdb.d/` and run
  automatically on first container start (empty data volume only).
- **Access for imports/migrations**: Run the `etl` service (`docker compose run --rm etl ...`), or
  exec into the running container (`docker exec -it geocoder-postgres psql ...`). There is no
  `localhost:5432` route into this database from the host in production.

### 3. Redis Cache (`geocoder-redis` container)

**Container Name:** `geocoder-redis`
**Image:** `redis:7-alpine`
**Exposed Port:** **None** — no host port mapping. Reachable only from other containers, by hostname `redis`.
**Network:** `geocoder-network`
**Auth:** `--requirepass "${REDIS_PASSWORD:?REDIS_PASSWORD is required}"` — no password, no start.

**Purpose:**
- API response caching (configurable TTL via `CACHE_TTL_SECONDS`, default 24h)
- Negative-result caching (`NEGATIVE_CACHE_TTL_SECONDS`)
- Rate limiting (per-key sliding window)

## Running Imports Against Production

Because Postgres has no host port mapping in production, import scripts don't run against
`localhost:5432` on the server the way older setups did. Use one of:

**Option A — the `etl` Compose service (recommended):**
```bash
docker compose -f docker-compose.prod.yml run --rm etl \
  data_sourcing.importers.import_production_data \
  --file regional_data_sources/toronto_address_dataset.geojson \
  --source toronto
```
The `etl` container joins `geocoder-network` and reaches Postgres at `DB_HOST=postgres`, same as the API.

**Option B — exec into a running container:**
```bash
docker exec -it geocoder-postgres psql -U "$DB_USER" -d "$DB_NAME"
```

**Option C — local development database:**
For iterating on import logic without touching production data, run a separate local Postgres
(e.g. `docker run -d -p 5433:5432 -e POSTGRES_PASSWORD=... postgis/postgis:15-3.3`, or a native
install per `VM_SETUP.md`) and point scripts at it with `DB_HOST`/`DB_PORT` env vars. Neither
`docker-compose.yml` nor `docker-compose.prod.yml` publishes a host port for Postgres —
`docker-compose.yml` instead uses `network_mode: host` and expects Postgres already running on
that host.

## Network Configuration

### Docker Network

All core services share the `geocoder-network` Docker network defined in `docker-compose.prod.yml`:

```bash
$ docker network inspect geocoder-network

# Shows geocoder-api, geocoder-postgres, geocoder-redis (and geocoder-etl when run) on the same network
```

### Port Exposure

| Service | Container Port | Host Port | Purpose |
|---------|---------------|-----------|---------|
| API (`geocoder-api`) | 3000 | `${API_PORT:-3000}` | Behind reverse proxy / tunnel |
| PostgreSQL (`geocoder-postgres`) | 5432 | *(none)* | Docker network only |
| Redis (`geocoder-redis`) | 6379 | *(none)* | Docker network only |

## Important Notes

### Secrets

- `DB_PASSWORD`, `REDIS_PASSWORD`, and `ALLOWED_ORIGINS` are required — Compose will refuse to
  start any service that needs them if they are unset in `.env`. There is no built-in fallback
  password.
- `API_KEY_PEPPER` is recommended (not enforced by Compose) — without it, new API keys are hashed
  with plain SHA-256 instead of HMAC-SHA256; `config_validator.py` logs a startup warning if it's
  unset.
- Never commit a populated `.env` file. Use `.env.example` as the template.

### Backups

Create backups before major operations:

```bash
# Backup entire database
docker exec geocoder-postgres pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc -f /tmp/backup.dump

# Copy backup to host
docker cp geocoder-postgres:/tmp/backup.dump ./backups/geocoder-$(date +%Y%m%d).dump
```

(Automated/scheduled backups, WAL/PITR, and offsite copies are not yet implemented.)

### Monitoring

Check service health:

```bash
# Container status (includes healthchecks defined in docker-compose.prod.yml)
docker compose -f docker-compose.prod.yml ps

# Database connection test (from inside the container, or via the etl service)
docker exec -it geocoder-postgres psql -U "$DB_USER" -d "$DB_NAME" -c "SELECT COUNT(*) FROM properties;"

# API health (through your reverse proxy / tunnel)
curl https://geocoder.yourdomain.com/v1/health
```

## Troubleshooting

### Issue: API can't connect to database

**Symptoms:** API logs show connection refused to `postgres:5432`

**Solutions:**
1. Verify database is running and healthy: `docker compose -f docker-compose.prod.yml ps postgres`
2. Check Docker network: `docker network inspect geocoder-network`
3. Confirm `DB_HOST=postgres` (not `localhost`) in the API container's environment
4. Confirm `DB_PASSWORD` matches between the `api` and `postgres` service environments

### Issue: Import/ETL scripts can't connect

**Symptoms:** Connection refused when running an import

**Solutions:**
1. Run imports through the `etl` Compose service so they're on `geocoder-network` — a bare
   `DB_HOST=localhost` from the host will not reach Postgres in production (no host port mapping).
2. Verify the `postgres` service is healthy: `docker compose -f docker-compose.prod.yml ps postgres`

### Issue: Data missing or incorrect

**Symptoms:** API returns no results or wrong data

**Solutions:**
1. Verify data was imported:
   ```bash
   docker exec -it geocoder-postgres psql -U "$DB_USER" -d "$DB_NAME" -c "
   SELECT source_dataset, COUNT(*) FROM properties GROUP BY source_dataset;
   "
   ```
2. Check migrations ran: `uv run database/run_migrations.py --status` (or query
   `schema_migrations` directly). The runner now hard-errors on any migration header it fails to
   parse and verifies stored checksums against the current `migrations.sql` on every run — a
   mismatch or an unparsed migration stops the run rather than silently skipping it.
3. Review import logs / `import_history` and `import_row_errors` for errors.

## Future Improvements

### Separate Dev/Prod Databases

Neither compose file currently publishes a Postgres dev port: `docker-compose.yml` uses
`network_mode: host` and expects a Postgres/Redis already running on that host (see
`VM_SETUP.md`), and `docker-compose.prod.yml` deliberately keeps `postgres` off the host network
entirely. To separate environments, run a dedicated dev Postgres container/volume (see Option C
above) rather than pointing dev tooling at prod.

### High Availability

For production HA:
- PostgreSQL replication (primary/replica)
- Redis cluster or Sentinel
- Load balancer for multiple API instances
- Health checks and automatic failover

Not yet implemented.

### Monitoring

Consider adding:
- Prometheus for metrics collection
- Grafana for dashboards
- pgAdmin for database management
- Redis monitoring (`redis-cli -a "$REDIS_PASSWORD" INFO`)

Not yet implemented.

## Contact

For deployment questions or issues, refer to:
- `README.md` for general setup
- `api/` directory for API documentation
- `database/` directory for import scripts
