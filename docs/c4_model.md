# Ontario Geocoder — C4 review diagrams

These diagrams describe the current implementation, not a proposed service split.
They use Mermaid flowcharts with explicit C4 boundaries and element types.
Arrows describe calls/dependencies; responses return over the same relationship
unless stated otherwise. External data providers remain outside the system boundary.

## Level 1 — System context

```mermaid
flowchart LR
    consumer["Consuming system<br/>[External software system]<br/>Needs coordinates for existing addresses"]
    operator["Operator<br/>[Person]<br/>Deploys the service and runs data imports"]
    geocoder["Ontario Geocoder<br/>[Software system]<br/>Geocodes Ontario addresses using local reference data"]
    municipal["Municipal ArcGIS services<br/>[External software systems]<br/>Municipal address datasets"]
    toronto["Toronto Open Data / CKAN<br/>[External software system]<br/>Toronto reference datasets"]
    statscan["Statistics Canada<br/>[External software system]<br/>NAR and geographic reference datasets"]

    consumer -->|Requests coordinates for addresses| geocoder
    operator -->|Configures, initializes, and refreshes reference data| geocoder
    geocoder -->|Downloads reference data| municipal
    geocoder -->|Downloads reference data| toronto
    geocoder -->|Downloads reference data| statscan
```

- The consumer receives geocoding results and is responsible for updating its own records.
- The geocoder pulls source data; providers do not push updates into it.
- Provider boxes group representative integrations, rather than enumerate every source.

## Level 2 — Containers

```mermaid
flowchart TB
    consumer["Consuming system<br/>[External software system]"]
    operator["Operator<br/>[Person]"]
    providers["Reference data providers<br/>[External software systems]<br/>Municipal ArcGIS, Toronto CKAN, Statistics Canada"]

    subgraph system["Ontario Geocoder — software system boundary"]
        api["Geocoding API<br/>[Container: Python / FastAPI]<br/>HTTP endpoints, authentication, rate limiting,<br/>address parsing, matching, confidence and attribution"]
        etl["ETL application<br/>[Container: Python batch jobs]<br/>Download, transform, validate and import reference data"]
        init["Database initializer<br/>[Container: Python batch job]<br/>Apply tracked schema migrations and seed metadata"]
        files[("Raw source file store<br/>[Data store: filesystem]<br/>Downloaded datasets for import and reprocessing")]
        db[("Reference and application database<br/>[Container: PostgreSQL / PostGIS]<br/>Addresses, geography, source metadata,<br/>API keys and import tracking")]
        redis[("Cache and rate-limit store<br/>[Container: Redis]<br/>Geocoding results, API-key cache and rate-limit state")]
    end

    consumer -->|Requests single or batch geocoding — HTTP/JSON| api
    operator -->|Runs sourcing and import commands — CLI| etl
    operator -->|Runs initialization, directly or through deployment tooling| init
    etl -->|Downloads datasets — HTTP/HTTPS| providers
    etl -->|Writes downloads and reads files for import| files
    etl -->|Reads metadata and geography; writes imported data and tracking — SQL| db
    init -->|Initializes schema and seeds metadata — SQL| db
    api -->|Queries reference data; reads/writes API-key records — SQL| db
    api -->|Reads/writes cached results, validated keys and rate limits — Redis protocol| redis
```

### Container interpretation

- **The matching engine is part of the API container.** It is not a separate service or network hop.
- ETL is an explicitly invoked batch application, not an always-running ingestion service. No automatic refresh schedule is implied.
- Raw files are stored under `data_sourcing/data_sources/`; production Compose bind-mounts this directory into ETL.
- ETL writes the database; the API does not read raw source files or query providers during geocoding.
- The initializer is shown for completeness. It is a one-shot deployment application, separate from reference-data imports.
- A C4 container is an application or data store, not necessarily a Docker container. These logical boundaries also apply to the non-Docker VM deployment.

## Level 3 — Geocoding API components

Scope: expands **only the Geocoding API container** from Level 2. ETL and the database initializer are not components of this container.

```mermaid
flowchart TB
    consumer["Consuming system<br/>[External software system]"]
    db[("PostgreSQL / PostGIS<br/>[External container relative to this view]")]
    redis[("Redis<br/>[External container relative to this view]")]

    subgraph api_boundary["Geocoding API — container boundary: Python / FastAPI"]
        middleware["HTTP middleware<br/>[Component: api/middleware/]<br/>Authentication, rate limiting, IP filtering,<br/>request limits and response context"]
        endpoints["API endpoints and request/response models<br/>[Component: api/main.py, api/models.py]<br/>Single/batch geocoding, key management,<br/>health, statistics and attribution"]
        keys["API-key service<br/>[Component: api/services/api_keys.py]<br/>Validate and manage API keys"]
        orchestrator["Geocoding orchestrator<br/>[Component: engine/geocoder.py]<br/>Cache-first processing and cascading<br/>PIN → municipal → NAR lookup; batch execution"]
        parser["Address parser<br/>[Component: engine/address_parser.py]<br/>Parse, normalize and hash address identity"]
        geography["Geographic validator<br/>[Component: engine/geographic_validator.py]<br/>Enrich and validate municipality/postal geography"]
        matcher["Layered matchers<br/>[Component: engine/layered_matcher.py]<br/>Municipal and NAR candidate queries,<br/>exact/fuzzy matching and disambiguation"]
        scoring["Confidence and address formatting<br/>[Component: engine/confidence.py,<br/>engine/address_formatter.py]<br/>Tier/layer scoring and matched-address labels"]
        licenses["License and attribution service<br/>[Component: engine/license_service.py]<br/>Source-specific license metadata;<br/>in-memory cache populated at startup"]
        database["Database access<br/>[Component: engine/database.py]<br/>Async PostgreSQL connection pool and queries"]
        cache["Redis access<br/>[Component: engine/cache.py]<br/>JSON result/key caching, TTLs,<br/>connection management and raw client access"]
    end

    consumer -->|Sends HTTP/JSON requests| middleware
    middleware -->|Authenticates protected requests| keys
    middleware -->|Passes permitted requests| endpoints
    middleware -->|Uses raw client for atomic rate limiting| cache
    endpoints -->|Invokes single/batch geocoding| orchestrator
    endpoints -->|Manages API keys| keys
    endpoints -->|Adds source license and attribution| licenses
    endpoints -->|Checks health and reads statistics| database
    endpoints -->|Checks health and reads statistics| cache
    keys -->|Reads/writes key records| database
    keys -->|Caches validated keys; invalidates changes| cache
    orchestrator -->|Parses input and generates cache identity| parser
    orchestrator -->|Reads cached results; writes matches and misses| cache
    orchestrator -->|Queries PIN lookup on cache miss| database
    orchestrator -->|Enriches geography for municipal/NAR lookup| geography
    orchestrator -->|Invokes municipal then NAR matching as needed| matcher
    orchestrator -->|Finalizes confidence| scoring
    geography -->|Queries geographic reference data| database
    matcher -->|Queries reference address candidates| database
    matcher -->|Normalizes street suffixes and directions| parser
    matcher -->|Builds scored, formatted match results| scoring
    licenses -->|Loads source metadata at startup| database
    database -->|Executes SQL — PostgreSQL protocol| db
    cache -->|Reads/writes entries and rate-limit state — Redis protocol| redis
```

### Runtime behaviour

1. Middleware applies request controls and authenticates protected requests.
2. A geocoding endpoint calls the orchestrator in the same process.
3. The orchestrator parses the address and checks Redis before any matching tier.
4. On a cache miss, it tries PIN lookup, municipal matching, then NAR matching against PostgreSQL.
5. Successful results are cached (default 24 hours); not-found results are negatively cached (default 1 hour).
6. The endpoint adds response context and source attribution, then returns the result. Batch geocoding reuses the same orchestrator.

Component arrows are dependencies, not a strict sequence diagram. In particular,
PIN lookup bypasses the layered matchers, and a geocoding cache hit bypasses all
matching tiers. Geographic and license in-memory caches are not Redis caches.

## Implementation anchors

| Architectural element | Implementation |
|---|---|
| API and matching engine share one deployable application | `Dockerfile`; `api/main.py` imports and calls `engine.geocoder` |
| API, database, Redis, ETL and initializer runtime definitions | `docker-compose.prod.yml` |
| ETL entry points and source workflows | `data_sourcing/setup_data.py`, `data_sourcing/downloaders/`, `data_sourcing/importers/` |
| ETL transformation and validation | `data_sourcing/transformers/`, `data_sourcing/validators/` and importer-level validation |
| Raw file store mount | ETL volume in `docker-compose.prod.yml` |
| Database initialization | `database/run_migrations.py` |
| Cache-first geocoding and cascading tiers | `engine/geocoder.py` |
| Redis caching and TTL defaults | `engine/cache.py` |
| Authentication caching and rate limiting | `api/services/api_keys.py`, `api/middleware/rate_limiter.py` |
