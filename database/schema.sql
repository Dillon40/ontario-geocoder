-- Geocoder database schema
-- Regenerated 2026-07-31 from the live database (pg_dump --schema-only, cleaned up)
-- Reflects migrations 1-32.
-- IMPORTANT: schema.sql must be kept in lockstep with migrations.sql — update both together.
-- All objects use IF NOT EXISTS for idempotency
--
-- Bootstrap contract (unchanged from prior versions of this file):
--   schema.sql creates the final-state schema, including an EMPTY
--   schema_migrations table. It does NOT seed any migration rows. Row
--   1-13 are back-seeded by migration 13's own INSERT, and later
--   migrations record their own row, when migrations.sql is run
--   immediately afterward (setup_database.sh and docker-entrypoint-initdb.d
--   both run schema.sql then migrations.sql, in that order). This lets
--   run_migrations.py correctly treat 1-32 as already applied on a
--   freshly bootstrapped database without re-running them.
--
-- KNOWN DRIFT (see regeneration notes): fsa_boundaries and
-- lower_municipal_boundaries below reflect the live database, which no
-- longer matches the CREATE TABLE statements inside migrations.sql's
-- MIGRATION 11 block. Both tables are dropped and recreated from source
-- shapefiles by data_sourcing/importers/import_fsa_boundaries.py and
-- import_municipal_boundaries.py via geopandas to_postgis(if_exists=
-- 'replace'), which bypasses the migration system entirely. Application
-- code (fsa_municipality_setup.py, populate_fsa_municipality_mappings.py,
-- import_municipal_boundaries.py, validate_postal_codes.py) already
-- depends on the raw/live column names captured here, not the ones in
-- migration 11. Running migrations.sql against a database bootstrapped
-- from this schema.sql WILL fail at migration 11's
-- idx_lower_muni_name / idx_lower_muni_province indexes, which reference
-- columns (name_en, province) that do not exist on the live table shape.
-- This is a pre-existing inconsistency, not introduced by this
-- regeneration — migrations.sql was intentionally left untouched per the
-- scope of this change. A follow-up migration should either formalize
-- the current raw-shapefile shape or make the importers additive
-- (ALTER/UPSERT) instead of destructive.

-- Extensions
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================================================
-- CORE TABLES
-- ============================================================================

-- Properties table (multi-source address database)
CREATE TABLE IF NOT EXISTS properties (
    id SERIAL PRIMARY KEY,
    property_id VARCHAR(50) UNIQUE,
    assessment_roll_number VARCHAR(50),
    street_number VARCHAR(20),
    street_name VARCHAR(100),
    street_suffix VARCHAR(20),
    street_direction VARCHAR(20),
    unit_no VARCHAR(20),
    civic_no_suffix VARCHAR(50), -- widened from VARCHAR(10) on live DB; not tracked by a numbered migration
    city VARCHAR(100),
    city_original VARCHAR(100),
    province VARCHAR(50),
    postal_code VARCHAR(10),
    fsa_code VARCHAR(3),
    full_address TEXT,
    latitude DECIMAL(10, 8),
    longitude DECIMAL(11, 8),
    location GEOGRAPHY(Point, 4326),
    pin BIGINT,
    gpid BIGINT,
    source_dataset VARCHAR(100),
    import_date TIMESTAMP DEFAULT NOW(),
    -- Soft delete support
    is_deleted BOOLEAN DEFAULT FALSE,
    deleted_at TIMESTAMP,
    deleted_by_import_id INTEGER,
    last_seen_import_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Properties indexes
CREATE INDEX IF NOT EXISTS idx_properties_full_address ON properties(full_address);
CREATE INDEX IF NOT EXISTS idx_properties_composite ON properties(street_number, street_name, city, postal_code);
CREATE INDEX IF NOT EXISTS idx_properties_postal ON properties(postal_code);
CREATE INDEX IF NOT EXISTS idx_properties_postal_civic ON properties(postal_code, street_number);
CREATE INDEX IF NOT EXISTS idx_properties_property_id ON properties(property_id);
CREATE INDEX IF NOT EXISTS idx_properties_assessment ON properties(assessment_roll_number);
CREATE INDEX IF NOT EXISTS idx_properties_location ON properties USING GIST(location);
CREATE INDEX IF NOT EXISTS idx_properties_pin ON properties(pin);
CREATE INDEX IF NOT EXISTS idx_properties_gpid ON properties(gpid);
CREATE INDEX IF NOT EXISTS idx_properties_source ON properties(source_dataset);
CREATE INDEX IF NOT EXISTS idx_properties_normalized ON properties(
    UPPER(TRIM(street_number)),
    UPPER(TRIM(street_name)),
    UPPER(TRIM(city))
);
CREATE INDEX IF NOT EXISTS idx_properties_fsa_code ON properties(fsa_code) WHERE fsa_code IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_properties_city_original ON properties(city_original) WHERE city_original IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_properties_unit_no ON properties(unit_no) WHERE unit_no IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_properties_addr_with_unit ON properties(postal_code, street_number, civic_no_suffix, unit_no);
CREATE INDEX IF NOT EXISTS idx_properties_active ON properties(property_id) WHERE is_deleted = FALSE;
CREATE INDEX IF NOT EXISTS idx_properties_last_import ON properties(source_dataset, last_seen_import_id);
CREATE INDEX IF NOT EXISTS idx_properties_street_name_trgm ON properties USING GIN (street_name gin_trgm_ops);
-- Expression index for case-insensitive civic+street lookups (migration 32)
CREATE INDEX IF NOT EXISTS idx_properties_upper_civic_street ON properties (UPPER(street_number), UPPER(street_name));

-- Geocode cache table
CREATE TABLE IF NOT EXISTS geocode_cache (
    id SERIAL PRIMARY KEY,
    address_hash VARCHAR(64) UNIQUE,
    input_address JSONB,
    latitude DECIMAL(10, 8),
    longitude DECIMAL(11, 8),
    source VARCHAR(20),
    confidence DECIMAL(3, 2),
    matched_property_id VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_cache_hash ON geocode_cache(address_hash);

-- Dataset metadata
CREATE TABLE IF NOT EXISTS dataset_metadata (
    id SERIAL PRIMARY KEY,
    dataset_name VARCHAR(100) UNIQUE NOT NULL,
    municipality_name VARCHAR(100),
    postal_code_prefixes TEXT[],
    bbox GEOGRAPHY(Polygon, 4326),
    record_count INTEGER,
    last_updated TIMESTAMP,
    active BOOLEAN DEFAULT TRUE
);

-- ============================================================================
-- NAR (National Address Register) TABLES
-- ============================================================================

CREATE TABLE IF NOT EXISTS nar_addresses (
    id SERIAL PRIMARY KEY,
    loc_guid UUID,
    addr_guid UUID UNIQUE,
    civic_no VARCHAR(20),
    civic_no_suffix VARCHAR(10),
    apt_no_label VARCHAR(20),
    official_street_name VARCHAR(100),
    official_street_type VARCHAR(20),
    official_street_dir VARCHAR(20), -- widened from VARCHAR(10) by migration 26
    csd_eng_name VARCHAR(100),
    prov_code INTEGER,
    mail_postal_code VARCHAR(10),
    bg_x DECIMAL,
    bg_y DECIMAL,
    fsa_code VARCHAR(3),
    -- Enrichment tracking
    enriched_from_source VARCHAR(100),
    enrichment_confidence NUMERIC(4,3),
    enrichment_date TIMESTAMP,
    -- Soft delete support
    is_deleted BOOLEAN DEFAULT FALSE,
    deleted_at TIMESTAMP,
    deleted_by_import_id INTEGER,
    last_seen_import_id INTEGER,
    -- WGS84 lat/lon derived from bg_x/bg_y, EPSG:3347 (migration 30)
    latitude NUMERIC(10,8),
    longitude NUMERIC(11,8)
);

CREATE INDEX IF NOT EXISTS idx_nar_addr_postal_civic ON nar_addresses(mail_postal_code, civic_no);
CREATE INDEX IF NOT EXISTS idx_nar_addr_loc_guid ON nar_addresses(loc_guid);
CREATE INDEX IF NOT EXISTS idx_nar_prov_code ON nar_addresses(prov_code);
CREATE INDEX IF NOT EXISTS idx_nar_addr_postal ON nar_addresses(mail_postal_code);
CREATE INDEX IF NOT EXISTS idx_nar_addr_components ON nar_addresses(civic_no, official_street_name);
CREATE INDEX IF NOT EXISTS idx_nar_addr_fsa_code ON nar_addresses(fsa_code);
CREATE INDEX IF NOT EXISTS idx_nar_addr_enriched ON nar_addresses(enriched_from_source);
CREATE INDEX IF NOT EXISTS idx_nar_addresses_active ON nar_addresses(addr_guid) WHERE is_deleted = FALSE;
CREATE INDEX IF NOT EXISTS idx_nar_addr_street_name_trgm ON nar_addresses USING GIN (official_street_name gin_trgm_ops);
-- Expression index for case-insensitive civic+street lookups (migration 31)
CREATE INDEX IF NOT EXISTS idx_nar_addr_upper_civic_street ON nar_addresses (UPPER(civic_no), UPPER(official_street_name));

CREATE TABLE IF NOT EXISTS nar_locations (
    id SERIAL PRIMARY KEY,
    loc_guid UUID UNIQUE,
    bg_latitude DECIMAL(10, 8),
    bg_longitude DECIMAL(11, 8),
    csd_code INTEGER,
    -- Soft delete support
    is_deleted BOOLEAN DEFAULT FALSE,
    deleted_at TIMESTAMP,
    deleted_by_import_id INTEGER,
    last_seen_import_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_nar_loc_guid ON nar_locations(loc_guid);

-- NAR Enrichment Log
CREATE TABLE IF NOT EXISTS nar_enrichment_log (
    id SERIAL PRIMARY KEY,
    enrichment_date TIMESTAMP DEFAULT NOW(),
    source_dataset VARCHAR(100) NOT NULL,
    addresses_matched INTEGER DEFAULT 0,
    addresses_enriched INTEGER DEFAULT 0,
    addresses_skipped INTEGER DEFAULT 0,
    avg_confidence NUMERIC(4,3),
    processing_time_seconds INTEGER,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_nar_enrichment_date ON nar_enrichment_log(enrichment_date);
CREATE INDEX IF NOT EXISTS idx_nar_enrichment_source ON nar_enrichment_log(source_dataset);

-- NAR Match Conflicts
CREATE TABLE IF NOT EXISTS nar_match_conflicts (
    id SERIAL PRIMARY KEY,
    addr_guid UUID REFERENCES nar_addresses(addr_guid),
    conflict_type VARCHAR(50),
    source_a VARCHAR(100),
    source_b VARCHAR(100),
    coordinates_a GEOGRAPHY(Point, 4326),
    coordinates_b GEOGRAPHY(Point, 4326),
    distance_meters NUMERIC(10,2),
    detected_date TIMESTAMP DEFAULT NOW(),
    resolved BOOLEAN DEFAULT FALSE,
    resolution_notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_nar_conflicts_addr ON nar_match_conflicts(addr_guid);
CREATE INDEX IF NOT EXISTS idx_nar_conflicts_unresolved ON nar_match_conflicts(resolved) WHERE resolved = FALSE;

-- ============================================================================
-- API & SECURITY TABLES
-- ============================================================================

CREATE TABLE IF NOT EXISTS api_usage_log (
    id SERIAL PRIMARY KEY,
    api_source VARCHAR(50),
    request_count INTEGER DEFAULT 1,
    date DATE DEFAULT CURRENT_DATE,
    estimated_cost DECIMAL(10, 4),
    api_key_id INTEGER,
    endpoint VARCHAR(100),
    response_time_ms INTEGER,
    status_code INTEGER,
    client_ip INET,
    UNIQUE(api_source, date)
);

CREATE INDEX IF NOT EXISTS idx_api_usage_date ON api_usage_log(date);
CREATE INDEX IF NOT EXISTS idx_api_usage_key_id ON api_usage_log(api_key_id);
CREATE INDEX IF NOT EXISTS idx_api_usage_key_date ON api_usage_log(api_key_id, date);

CREATE TABLE IF NOT EXISTS api_keys (
    id SERIAL PRIMARY KEY,
    key_hash VARCHAR(64) UNIQUE NOT NULL,
    key_prefix VARCHAR(20) NOT NULL,
    name VARCHAR(100) NOT NULL,
    owner VARCHAR(200),
    created_at TIMESTAMP DEFAULT NOW(),
    expires_at TIMESTAMP,
    last_used_at TIMESTAMP,
    is_active BOOLEAN DEFAULT true,
    rate_limit_override INTEGER,
    allowed_ips TEXT[],
    permissions JSONB DEFAULT '{"geocode": true, "batch": true, "stats": false, "admin": false}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_active ON api_keys(is_active) WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_api_keys_prefix ON api_keys(key_prefix);

-- Add FK after api_keys exists
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'api_usage_log_api_key_id_fkey'
    ) THEN
        ALTER TABLE api_usage_log ADD CONSTRAINT api_usage_log_api_key_id_fkey
            FOREIGN KEY (api_key_id) REFERENCES api_keys(id);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS rate_limit_buckets (
    id SERIAL PRIMARY KEY,
    api_key_id INTEGER REFERENCES api_keys(id),
    bucket_key VARCHAR(100) NOT NULL,
    request_count INTEGER DEFAULT 0,
    window_start TIMESTAMP DEFAULT NOW(),
    UNIQUE(api_key_id, bucket_key)
);

CREATE INDEX IF NOT EXISTS idx_rate_limit_key ON rate_limit_buckets(api_key_id, bucket_key);

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMP DEFAULT NOW(),
    admin_key_id INTEGER REFERENCES api_keys(id),
    admin_key_name VARCHAR(100),
    action VARCHAR(50) NOT NULL,
    target_type VARCHAR(50),
    target_id INTEGER,
    target_name VARCHAR(100),
    details JSONB,
    client_ip INET,
    request_id VARCHAR(100)
);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON admin_audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_admin_key ON admin_audit_log(admin_key_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON admin_audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_target ON admin_audit_log(target_type, target_id);

-- ============================================================================
-- FSA & MUNICIPALITY TABLES
-- ============================================================================

-- NOTE: this table's real shape comes from data_sourcing/importers/import_fsa_boundaries.py,
-- which reloads it wholesale from a StatCan FSA shapefile via geopandas
-- to_postgis(if_exists='replace'). That bypasses migrations.sql entirely, so the
-- columns/constraints below (captured from the live DB) do NOT match migration 11's
-- CREATE TABLE fsa_boundaries. See the header comment at the top of this file.
-- PK constraint name (fsa_boundaries_staging_pkey) preserved as-is from the live
-- DB — a leftover from when this table was created as a staging table.
CREATE TABLE IF NOT EXISTS fsa_boundaries (
    id SERIAL,
    fsa_code TEXT,
    dissemination_guid TEXT,
    province_id TEXT,
    province_name TEXT,
    land_area_km2 DOUBLE PRECISION,
    geometry GEOMETRY(Geometry, 4326),
    CONSTRAINT fsa_boundaries_staging_pkey PRIMARY KEY (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_fsa_boundaries_fsa_code ON fsa_boundaries(fsa_code);
CREATE INDEX IF NOT EXISTS idx_fsa_boundaries_geometry ON fsa_boundaries USING GIST(geometry);
CREATE INDEX IF NOT EXISTS idx_fsa_boundaries_province ON fsa_boundaries(province_id);

-- NOTE: same caveat as fsa_boundaries above — reloaded wholesale by
-- data_sourcing/importers/import_municipal_boundaries.py via geopandas
-- to_postgis(if_exists='replace') from an Ontario municipal boundary shapefile.
-- Columns (captured from the live DB) do NOT match migration 11's
-- CREATE TABLE lower_municipal_boundaries. Quoted mixed-case columns below are
-- raw, truncated shapefile DBF field names preserved as-is.
CREATE TABLE IF NOT EXISTS lower_municipal_boundaries (
    "OGF_ID" INTEGER,
    tier_type TEXT,
    "MUNICIPA_1" TEXT,
    name TEXT,
    "MUNICIPA_3" TEXT,
    municipal_id TEXT,
    "MAH_CODE" TEXT,
    "MUNICIPA_4" TEXT,
    "MUNICIPA_5" TEXT,
    "ASSESSMENT" TEXT,
    "MUNICIPA_6" TEXT,
    "MUNICIPA_7" TEXT,
    short_name TEXT,
    "MUNICIPA_9" TEXT,
    "UPPER_TIER" TEXT,
    upper_tier_name TEXT,
    "UPPER_TI_2" TEXT,
    "EFFECTIVE_" TIMESTAMP,
    "SYSTEM_DAT" TIMESTAMP,
    "OBJECTID" INTEGER,
    "SHAPEAREA" INTEGER,
    "SHAPELEN" INTEGER,
    geometry GEOMETRY(Geometry, 4326),
    id SERIAL PRIMARY KEY
);

CREATE INDEX IF NOT EXISTS idx_lower_municipal_boundaries_geometry ON lower_municipal_boundaries USING GIST(geometry);
CREATE INDEX IF NOT EXISTS idx_lower_municipal_boundaries_municipal_id ON lower_municipal_boundaries(municipal_id);
CREATE INDEX IF NOT EXISTS idx_lower_municipal_boundaries_name ON lower_municipal_boundaries(name);
CREATE INDEX IF NOT EXISTS idx_lower_municipal_boundaries_tier_type ON lower_municipal_boundaries(tier_type);

CREATE TABLE IF NOT EXISTS fsa_municipality_mappings (
    id SERIAL PRIMARY KEY,
    fsa_code VARCHAR(3) NOT NULL,
    municipal_id TEXT,
    municipality_name_short TEXT NOT NULL,
    municipality_name_full TEXT,
    is_primary BOOLEAN DEFAULT false,
    coverage_pct DECIMAL(5,2),
    address_count INTEGER DEFAULT 0,
    geometry_intersection GEOMETRY(Geometry, 4326),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fsa_muni_fsa_code ON fsa_municipality_mappings(fsa_code);
CREATE INDEX IF NOT EXISTS idx_fsa_muni_municipal_id ON fsa_municipality_mappings(municipal_id);
CREATE INDEX IF NOT EXISTS idx_fsa_muni_name_short ON fsa_municipality_mappings(municipality_name_short);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fsa_muni_unique ON fsa_municipality_mappings(fsa_code, municipal_id) WHERE municipal_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_fsa_muni_primary ON fsa_municipality_mappings(fsa_code, is_primary) WHERE is_primary = true;

CREATE TABLE IF NOT EXISTS municipality_aliases (
    id SERIAL PRIMARY KEY,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    municipal_id TEXT,
    alias_type VARCHAR(50),
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_muni_alias_normalized ON municipality_aliases(normalized_alias);
CREATE INDEX IF NOT EXISTS idx_muni_alias_canonical ON municipality_aliases(canonical_name);
CREATE UNIQUE INDEX IF NOT EXISTS idx_muni_alias_unique ON municipality_aliases(normalized_alias, canonical_name);

-- ============================================================================
-- DATA SOURCES TRACKING
-- ============================================================================

CREATE TABLE IF NOT EXISTS data_types (
    id SERIAL PRIMARY KEY,
    type_key VARCHAR(50) UNIQUE NOT NULL,
    display_name VARCHAR(100) NOT NULL,
    description TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS data_sources (
    id SERIAL PRIMARY KEY,
    source_key VARCHAR(50) UNIQUE NOT NULL,
    source_name VARCHAR(200) NOT NULL,
    status VARCHAR(20) DEFAULT 'potential',

    -- Coverage
    regions_covered TEXT[] NOT NULL,
    region_type VARCHAR(50),
    postal_prefixes TEXT[],
    bbox GEOMETRY(Polygon, 4326),

    -- Manual scraping
    portal_url TEXT,
    scraping_notes TEXT,

    -- Automated access
    api_endpoint TEXT,
    api_type VARCHAR(50),
    api_key_required BOOLEAN DEFAULT FALSE,
    api_rate_limit TEXT,
    api_notes TEXT,

    -- Licensing
    license_type VARCHAR(100),
    license_url TEXT,
    commercial_use_allowed BOOLEAN,
    license_notes TEXT,

    -- Tracking
    last_data_refresh TIMESTAMP,
    record_count INTEGER,
    notes TEXT,

    -- Download automation (migration 12)
    download_url TEXT,
    download_format VARCHAR(20),
    import_script VARCHAR(100),
    dataset_mapping_key VARCHAR(50),
    local_file_path TEXT,
    requires_manual_download BOOLEAN DEFAULT FALSE,
    manual_download_instructions TEXT,

    -- Upstream sync (migration 15)
    upstream_version TEXT,
    upstream_etag TEXT,
    upstream_hash VARCHAR(64),
    upstream_last_modified TIMESTAMP,
    sync_status VARCHAR(20) DEFAULT 'never_synced',
    last_sync_check TIMESTAMP,
    last_sync_error TEXT,

    -- Health check metadata (migration 21)
    source_crs INTEGER,
    expected_crs INTEGER DEFAULT 4326,
    update_frequency VARCHAR(50),
    schema_fields JSONB,
    schema_hash VARCHAR(64),
    provider_last_modified TIMESTAMP,
    required_attribution TEXT,

    -- Download config (migration 22)
    download_config JSONB DEFAULT '{}',
    server_capabilities JSONB,
    rate_limit_config JSONB DEFAULT '{"strategy": "adaptive"}',
    resume_checkpoint JSONB,
    multi_endpoint_config JSONB,
    suggested_mappings JSONB,
    suggested_mappings_status VARCHAR(20) DEFAULT 'none',

    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- data_sources constraints
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_sync_status') THEN
        ALTER TABLE data_sources ADD CONSTRAINT chk_sync_status
            CHECK (sync_status IN ('never_synced', 'synced', 'stale', 'checking', 'error'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_update_frequency') THEN
        ALTER TABLE data_sources ADD CONSTRAINT chk_update_frequency
            CHECK (update_frequency IS NULL OR update_frequency IN (
                'daily', 'weekly', 'monthly', 'quarterly', 'annual', 'on_demand', 'unknown'
            ));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_suggested_mappings_status') THEN
        ALTER TABLE data_sources ADD CONSTRAINT chk_suggested_mappings_status
            CHECK (suggested_mappings_status IN ('none', 'pending_review', 'approved', 'rejected'));
    END IF;
END $$;

-- data_sources indexes
CREATE INDEX IF NOT EXISTS idx_data_sources_status ON data_sources(status);
CREATE INDEX IF NOT EXISTS idx_data_sources_regions ON data_sources USING GIN(regions_covered);
CREATE INDEX IF NOT EXISTS idx_data_sources_postal ON data_sources USING GIN(postal_prefixes);
CREATE INDEX IF NOT EXISTS idx_data_sources_download ON data_sources(status, download_url) WHERE download_url IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_data_sources_sync_status ON data_sources(sync_status);
CREATE INDEX IF NOT EXISTS idx_data_sources_update_frequency ON data_sources(update_frequency) WHERE update_frequency IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_data_sources_pending_mappings ON data_sources(suggested_mappings_status) WHERE suggested_mappings_status = 'pending_review';
CREATE INDEX IF NOT EXISTS idx_data_sources_has_checkpoint ON data_sources(source_key) WHERE resume_checkpoint IS NOT NULL;

-- data_sources updated_at trigger
CREATE OR REPLACE FUNCTION update_data_sources_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS data_sources_updated_at ON data_sources;
CREATE TRIGGER data_sources_updated_at
    BEFORE UPDATE ON data_sources
    FOR EACH ROW
    EXECUTE FUNCTION update_data_sources_timestamp();

CREATE TABLE IF NOT EXISTS data_source_types (
    data_source_id INTEGER REFERENCES data_sources(id) ON DELETE CASCADE,
    data_type_id INTEGER REFERENCES data_types(id) ON DELETE CASCADE,
    PRIMARY KEY (data_source_id, data_type_id)
);

-- ============================================================================
-- IMPORT & DOWNLOAD TRACKING
-- ============================================================================

CREATE TABLE IF NOT EXISTS schema_migrations (
    id SERIAL PRIMARY KEY,
    migration_number INTEGER UNIQUE NOT NULL,
    migration_name VARCHAR(200) NOT NULL,
    applied_at TIMESTAMP DEFAULT NOW(),
    checksum VARCHAR(64),
    execution_time_ms INTEGER,
    applied_by VARCHAR(100) DEFAULT CURRENT_USER,
    rollback_sql TEXT
);

CREATE INDEX IF NOT EXISTS idx_schema_migrations_number ON schema_migrations(migration_number);

CREATE TABLE IF NOT EXISTS import_history (
    id SERIAL PRIMARY KEY,
    source_key VARCHAR(50) NOT NULL,
    import_type VARCHAR(20) NOT NULL DEFAULT 'full',
    status VARCHAR(20) NOT NULL DEFAULT 'started',
    started_at TIMESTAMP DEFAULT NOW(),
    completed_at TIMESTAMP,
    duration_seconds INTEGER,

    -- Row counts
    rows_processed INTEGER DEFAULT 0,
    rows_inserted INTEGER DEFAULT 0,
    rows_updated INTEGER DEFAULT 0,
    rows_deleted INTEGER DEFAULT 0,
    rows_failed INTEGER DEFAULT 0,
    rows_skipped INTEGER DEFAULT 0,

    -- File tracking
    file_path TEXT,
    file_hash VARCHAR(64),
    file_size_bytes BIGINT,

    -- Error handling
    error_message TEXT,
    error_traceback TEXT,

    -- Extensible metadata
    metadata JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_import_history_source ON import_history(source_key);
CREATE INDEX IF NOT EXISTS idx_import_history_status ON import_history(status);
CREATE INDEX IF NOT EXISTS idx_import_history_started ON import_history(started_at);

CREATE TABLE IF NOT EXISTS import_row_errors (
    id SERIAL PRIMARY KEY,
    import_id INTEGER NOT NULL REFERENCES import_history(id) ON DELETE CASCADE,
    row_number INTEGER,
    error_type VARCHAR(100),
    error_message TEXT NOT NULL,
    row_data JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_import_row_errors_import ON import_row_errors(import_id);
CREATE INDEX IF NOT EXISTS idx_import_row_errors_type ON import_row_errors(error_type);

CREATE TABLE IF NOT EXISTS source_health_checks (
    id SERIAL PRIMARY KEY,
    source_key VARCHAR(50) NOT NULL,
    checked_at TIMESTAMP DEFAULT NOW(),

    -- HTTP response data
    http_status INTEGER,
    response_time_ms INTEGER,
    etag TEXT,
    content_length BIGINT,
    last_modified TEXT,

    -- Content analysis
    record_count INTEGER,
    schema_hash VARCHAR(64),
    schema_fields JSONB,

    -- Change detection
    has_changes BOOLEAN DEFAULT FALSE,
    change_type VARCHAR(50),

    -- ArcGIS-specific
    arcgis_last_edit_date TIMESTAMP,

    -- Error tracking
    error_type VARCHAR(100),
    error_message TEXT,

    CONSTRAINT fk_source_health_checks_source
        FOREIGN KEY (source_key) REFERENCES data_sources(source_key) ON DELETE CASCADE
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_health_change_type') THEN
        ALTER TABLE source_health_checks ADD CONSTRAINT chk_health_change_type
            CHECK (change_type IS NULL OR change_type IN ('content', 'schema', 'count', 'none', 'error'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_health_checks_source_key ON source_health_checks(source_key);
CREATE INDEX IF NOT EXISTS idx_health_checks_checked_at ON source_health_checks(checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_health_checks_source_time ON source_health_checks(source_key, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_health_checks_has_changes ON source_health_checks(source_key, has_changes) WHERE has_changes = TRUE;

CREATE TABLE IF NOT EXISTS download_runs (
    id SERIAL PRIMARY KEY,
    source_key VARCHAR(50) NOT NULL,
    started_at TIMESTAMP DEFAULT NOW(),
    completed_at TIMESTAMP,
    status VARCHAR(20) DEFAULT 'running',

    -- Progress tracking
    records_downloaded INTEGER DEFAULT 0,
    records_total INTEGER,
    last_objectid BIGINT,
    pages_fetched INTEGER DEFAULT 0,

    -- Rate limiting statistics
    rate_limit_hits INTEGER DEFAULT 0,
    total_wait_time_seconds INTEGER DEFAULT 0,
    avg_response_time_ms INTEGER,

    -- Server capabilities
    server_max_records INTEGER,
    geometry_type VARCHAR(50),
    supports_geojson BOOLEAN,

    -- Error tracking
    error_count INTEGER DEFAULT 0,
    last_error TEXT,

    -- File output
    output_path TEXT,
    file_size_bytes BIGINT,
    file_hash VARCHAR(64),

    -- Resume support
    is_resumable BOOLEAN DEFAULT TRUE,
    resume_data JSONB,

    CONSTRAINT fk_download_runs_source
        FOREIGN KEY (source_key) REFERENCES data_sources(source_key) ON DELETE CASCADE
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_download_run_status') THEN
        ALTER TABLE download_runs ADD CONSTRAINT chk_download_run_status
            CHECK (status IN ('running', 'completed', 'failed', 'interrupted', 'cancelled'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_download_runs_source ON download_runs(source_key, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_download_runs_status ON download_runs(status);
CREATE INDEX IF NOT EXISTS idx_download_runs_running ON download_runs(source_key) WHERE status = 'running';

CREATE TABLE IF NOT EXISTS field_mapping_suggestions (
    id SERIAL PRIMARY KEY,
    source_key VARCHAR(50) NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    reviewed_at TIMESTAMP,
    reviewed_by VARCHAR(100),
    status VARCHAR(20) DEFAULT 'pending',

    -- Schema information
    source_fields JSONB NOT NULL,
    suggested_mappings JSONB NOT NULL,
    confidence_scores JSONB,

    -- Review results
    approved_mappings JSONB,
    rejection_notes TEXT,

    CONSTRAINT fk_field_mapping_suggestions_source
        FOREIGN KEY (source_key) REFERENCES data_sources(source_key) ON DELETE CASCADE
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_mapping_suggestion_status') THEN
        ALTER TABLE field_mapping_suggestions ADD CONSTRAINT chk_mapping_suggestion_status
            CHECK (status IN ('pending', 'approved', 'rejected', 'modified'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_mapping_suggestions_source ON field_mapping_suggestions(source_key);
CREATE INDEX IF NOT EXISTS idx_mapping_suggestions_pending ON field_mapping_suggestions(source_key) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_mapping_suggestions_status ON field_mapping_suggestions(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mapping_suggestions_one_pending ON field_mapping_suggestions(source_key) WHERE status = 'pending';

-- ============================================================================
-- COMMERCIAL POSTAL CODES
-- ============================================================================

CREATE TABLE IF NOT EXISTS commercial_postal_codes (
    id SERIAL PRIMARY KEY,
    postal_code_raw VARCHAR(7) NOT NULL,
    postal_code VARCHAR(6) NOT NULL,
    fsa_code VARCHAR(3) NOT NULL,
    city VARCHAR(100),
    city_normalized VARCHAR(100),
    uri VARCHAR(100),
    latitude DECIMAL(12,8),
    longitude DECIMAL(12,8),
    location GEOGRAPHY(Point, 4326),
    source_community_id INTEGER,
    source_county_id INTEGER,
    source_region_id INTEGER,
    -- Municipality assignment (migration 24)
    official_municipality VARCHAR(100),
    official_municipal_id TEXT,
    municipality_assigned_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_comm_postal_code ON commercial_postal_codes(postal_code);
CREATE INDEX IF NOT EXISTS idx_comm_postal_fsa ON commercial_postal_codes(fsa_code);
CREATE INDEX IF NOT EXISTS idx_comm_postal_city_norm ON commercial_postal_codes(city_normalized);
CREATE INDEX IF NOT EXISTS idx_comm_postal_location ON commercial_postal_codes USING GIST(location);
CREATE INDEX IF NOT EXISTS idx_comm_postal_raw ON commercial_postal_codes(postal_code_raw);
CREATE INDEX IF NOT EXISTS idx_comm_postal_official_muni ON commercial_postal_codes(official_municipality);

-- ============================================================================
-- FUNCTIONS
-- ============================================================================

CREATE OR REPLACE FUNCTION normalize_address(addr TEXT)
RETURNS TEXT AS $$
BEGIN
    RETURN UPPER(TRIM(REGEXP_REPLACE(addr, '\s+', ' ', 'g')));
END;
$$ LANGUAGE plpgsql IMMUTABLE;

CREATE OR REPLACE FUNCTION normalize_municipality_name(name TEXT)
RETURNS TEXT AS $$
BEGIN
    IF name IS NULL OR TRIM(name) = '' THEN
        RETURN NULL;
    END IF;
    -- Remove bilingual suffix
    name := REGEXP_REPLACE(name, ' / .*$', '', 'g');
    -- Remove (Part) suffix
    name := REGEXP_REPLACE(name, ' \(Part\)', '', 'g');
    -- Trim and uppercase
    name := UPPER(TRIM(name));
    RETURN name;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- ============================================================================
-- VIEWS
-- ============================================================================

CREATE OR REPLACE VIEW v_latest_imports AS
SELECT DISTINCT ON (source_key)
    source_key,
    id as import_id,
    import_type,
    status,
    started_at,
    completed_at,
    duration_seconds,
    rows_inserted,
    rows_failed,
    error_message
FROM import_history
ORDER BY source_key, started_at DESC;

CREATE OR REPLACE VIEW v_sources_needing_attention AS
SELECT
    ds.source_key,
    ds.source_name,
    ds.sync_status,
    ds.last_sync_check,
    ds.last_sync_error,
    ds.update_frequency,
    ds.upstream_last_modified,
    ds.provider_last_modified,
    ds.record_count as stored_record_count,
    CASE
        WHEN ds.last_sync_check IS NULL THEN NULL
        ELSE NOW() - ds.last_sync_check
    END as time_since_check,
    hc.checked_at as last_change_detected,
    hc.change_type,
    hc.record_count as latest_record_count,
    hc.error_type as latest_error_type,
    hc.error_message as latest_error_message,
    CASE ds.update_frequency
        WHEN 'daily' THEN INTERVAL '1 day'
        WHEN 'weekly' THEN INTERVAL '7 days'
        WHEN 'monthly' THEN INTERVAL '30 days'
        WHEN 'quarterly' THEN INTERVAL '90 days'
        WHEN 'annual' THEN INTERVAL '365 days'
        ELSE INTERVAL '7 days'
    END as expected_check_interval,
    CASE
        WHEN ds.last_sync_check IS NULL THEN TRUE
        WHEN ds.update_frequency = 'daily' AND (NOW() - ds.last_sync_check) > INTERVAL '1 day' THEN TRUE
        WHEN ds.update_frequency = 'weekly' AND (NOW() - ds.last_sync_check) > INTERVAL '7 days' THEN TRUE
        WHEN ds.update_frequency = 'monthly' AND (NOW() - ds.last_sync_check) > INTERVAL '30 days' THEN TRUE
        WHEN ds.update_frequency IS NULL AND (NOW() - ds.last_sync_check) > INTERVAL '7 days' THEN TRUE
        ELSE FALSE
    END as is_overdue
FROM data_sources ds
LEFT JOIN LATERAL (
    SELECT checked_at, change_type, record_count, error_type, error_message
    FROM source_health_checks
    WHERE source_key = ds.source_key
    ORDER BY checked_at DESC
    LIMIT 1
) hc ON TRUE
WHERE ds.status = 'confirmed'
  AND ds.sync_status IN ('stale', 'error')
ORDER BY
    CASE ds.sync_status
        WHEN 'error' THEN 1
        WHEN 'stale' THEN 2
    END,
    ds.last_sync_check ASC NULLS FIRST;

CREATE OR REPLACE VIEW v_download_stats AS
SELECT
    source_key,
    COUNT(*) as total_runs,
    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as successful_runs,
    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_runs,
    SUM(CASE WHEN status = 'interrupted' THEN 1 ELSE 0 END) as interrupted_runs,
    ROUND(AVG(records_downloaded)) as avg_records,
    ROUND(AVG(rate_limit_hits)) as avg_rate_limits,
    ROUND(AVG(avg_response_time_ms)) as avg_response_time,
    MAX(completed_at) as last_successful_download,
    SUM(total_wait_time_seconds) as total_rate_limit_wait_seconds
FROM download_runs
GROUP BY source_key;

CREATE OR REPLACE VIEW v_pending_mapping_reviews AS
SELECT
    ds.source_key,
    ds.source_name,
    ds.record_count,
    ds.last_data_refresh,
    fms.created_at as suggestion_created,
    fms.suggested_mappings,
    fms.confidence_scores,
    jsonb_array_length(fms.source_fields) as field_count,
    (SELECT COUNT(*)
     FROM jsonb_each_text(fms.confidence_scores) c
     WHERE c.value::numeric >= 0.8) as high_confidence_count
FROM data_sources ds
JOIN field_mapping_suggestions fms ON ds.source_key = fms.source_key
WHERE fms.status = 'pending'
ORDER BY fms.created_at ASC;
