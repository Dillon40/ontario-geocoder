-- Migration script for tiered geocoding system enhancements
-- Run this after initial schema.sql to add multi-dataset and NAR support

-- ============================================================================
-- MIGRATION 1: Add dataset tracking to properties table
-- ============================================================================

BEGIN;

-- Add source tracking columns
ALTER TABLE properties ADD COLUMN IF NOT EXISTS source_dataset VARCHAR(100);
ALTER TABLE properties ADD COLUMN IF NOT EXISTS import_date TIMESTAMP DEFAULT NOW();

-- Create index for dataset filtering
CREATE INDEX IF NOT EXISTS idx_properties_source ON properties(source_dataset);

-- Backfill existing data (mark as 'guelph' if exists)
UPDATE properties
SET source_dataset = 'guelph',
    import_date = NOW()
WHERE source_dataset IS NULL;

COMMIT;

-- ============================================================================
-- MIGRATION 2: Create dataset metadata table
-- ============================================================================

BEGIN;

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

-- Insert Guelph metadata (update counts as needed)
INSERT INTO dataset_metadata (
  dataset_name, municipality_name, postal_code_prefixes, record_count, last_updated
)
VALUES (
  'guelph', 'City of Guelph', ARRAY['N1E', 'N1G', 'N1H', 'N1K', 'N1L'],
  (SELECT COUNT(*) FROM properties WHERE source_dataset = 'guelph'), NOW()
)
ON CONFLICT (dataset_name) DO UPDATE
SET record_count = EXCLUDED.record_count,
    last_updated = NOW();

COMMIT;

-- ============================================================================
-- MIGRATION 3: Create NAR (National Address Register) tables
-- ============================================================================

BEGIN;

-- NAR Addresses table
CREATE TABLE IF NOT EXISTS nar_addresses (
  id SERIAL PRIMARY KEY,
  loc_guid UUID,
  addr_guid UUID UNIQUE,
  civic_no VARCHAR(20),
  civic_no_suffix VARCHAR(10),
  apt_no_label VARCHAR(20),
  official_street_name VARCHAR(100),
  official_street_type VARCHAR(20),
  official_street_dir VARCHAR(10),
  csd_eng_name VARCHAR(100),
  prov_code INTEGER,
  mail_postal_code VARCHAR(10),
  bg_x DECIMAL,
  bg_y DECIMAL
);

-- NAR Locations table
CREATE TABLE IF NOT EXISTS nar_locations (
  id SERIAL PRIMARY KEY,
  loc_guid UUID UNIQUE,
  bg_latitude DECIMAL(10, 8),
  bg_longitude DECIMAL(11, 8),
  csd_code INTEGER
);

-- Create indexes for NAR tables
CREATE INDEX IF NOT EXISTS idx_nar_addr_postal_civic ON nar_addresses(mail_postal_code, civic_no);
CREATE INDEX IF NOT EXISTS idx_nar_addr_loc_guid ON nar_addresses(loc_guid);
CREATE INDEX IF NOT EXISTS idx_nar_loc_guid ON nar_locations(loc_guid);
CREATE INDEX IF NOT EXISTS idx_nar_prov_code ON nar_addresses(prov_code);
CREATE INDEX IF NOT EXISTS idx_nar_addr_postal ON nar_addresses(mail_postal_code);
CREATE INDEX IF NOT EXISTS idx_nar_addr_components ON nar_addresses(civic_no, official_street_name);  -- Optimized for component fuzzy matching

COMMIT;

-- ============================================================================
-- MIGRATION 4: Create API usage tracking table
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS api_usage_log (
  id SERIAL PRIMARY KEY,
  api_source VARCHAR(50),
  request_count INTEGER DEFAULT 1,
  date DATE DEFAULT CURRENT_DATE,
  estimated_cost DECIMAL(10, 4),
  UNIQUE(api_source, date)
);

-- Create index on date for fast lookups
CREATE INDEX IF NOT EXISTS idx_api_usage_date ON api_usage_log(date);

COMMIT;

-- ============================================================================
-- VERIFICATION QUERIES
-- ============================================================================

-- Verify properties table columns
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'properties'
  AND column_name IN ('source_dataset', 'import_date')
ORDER BY column_name;

-- Verify dataset_metadata table exists
SELECT table_name
FROM information_schema.tables
WHERE table_name = 'dataset_metadata';

-- Verify NAR tables exist
SELECT table_name
FROM information_schema.tables
WHERE table_name IN ('nar_addresses', 'nar_locations')
ORDER BY table_name;

-- Check counts
SELECT
  (SELECT COUNT(*) FROM properties) as properties_count,
  (SELECT COUNT(*) FROM properties WHERE source_dataset = 'guelph') as guelph_count,
  (SELECT COUNT(*) FROM nar_addresses) as nar_addresses_count,
  (SELECT COUNT(*) FROM nar_locations) as nar_locations_count;

-- Display dataset metadata
SELECT * FROM dataset_metadata;

-- ============================================================================
-- MIGRATION 5: Add FSA (Forward Sortation Area) code to NAR addresses
-- ============================================================================

BEGIN;

-- Add fsa_code column to nar_addresses
ALTER TABLE nar_addresses ADD COLUMN IF NOT EXISTS fsa_code VARCHAR(3);

-- Populate fsa_code with first 3 characters from mail_postal_code
UPDATE nar_addresses
SET fsa_code = LEFT(mail_postal_code, 3)
WHERE mail_postal_code IS NOT NULL AND mail_postal_code != '';

-- Create index for fast FSA lookups
CREATE INDEX IF NOT EXISTS idx_nar_addr_fsa_code ON nar_addresses(fsa_code);

COMMIT;

-- ============================================================================
-- MIGRATION 6: Create FSA-Municipality mapping infrastructure
-- ============================================================================

BEGIN;

-- Table: FSA to Municipality mapping (many-to-many relationship)
-- Maps FSAs to municipalities based on geographic intersection
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

-- Table: Municipality aliases for name normalization
-- Handles pre-amalgamation names (Toronto), bilingual names, variations
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

-- Indexes for fsa_municipality_mappings
CREATE INDEX IF NOT EXISTS idx_fsa_muni_fsa_code
  ON fsa_municipality_mappings(fsa_code);

CREATE INDEX IF NOT EXISTS idx_fsa_muni_municipal_id
  ON fsa_municipality_mappings(municipal_id);

CREATE INDEX IF NOT EXISTS idx_fsa_muni_name_short
  ON fsa_municipality_mappings(municipality_name_short);

CREATE UNIQUE INDEX IF NOT EXISTS idx_fsa_muni_unique
  ON fsa_municipality_mappings(fsa_code, municipal_id)
  WHERE municipal_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_fsa_muni_primary
  ON fsa_municipality_mappings(fsa_code, is_primary)
  WHERE is_primary = true;

-- Indexes for municipality_aliases
CREATE INDEX IF NOT EXISTS idx_muni_alias_normalized
  ON municipality_aliases(normalized_alias);

CREATE INDEX IF NOT EXISTS idx_muni_alias_canonical
  ON municipality_aliases(canonical_name);

CREATE UNIQUE INDEX IF NOT EXISTS idx_muni_alias_unique
  ON municipality_aliases(normalized_alias, canonical_name);

-- Function: Normalize municipality names for matching
-- Handles bilingual suffixes, county variations, whitespace
CREATE OR REPLACE FUNCTION normalize_municipality_name(name TEXT)
RETURNS TEXT AS $$
BEGIN
  -- Return NULL for NULL input
  IF name IS NULL OR TRIM(name) = '' THEN
    RETURN NULL;
  END IF;

  -- Remove bilingual suffix (e.g., "Greater Sudbury / Grand Sudbury" -> "Greater Sudbury")
  name := REGEXP_REPLACE(name, ' / .*$', '', 'g');

  -- Remove (Part) suffix (e.g., "Algoma, Unorganized, North Part" -> "Algoma, Unorganized, North")
  name := REGEXP_REPLACE(name, ' \(Part\)', '', 'g');

  -- Trim whitespace and convert to uppercase
  name := UPPER(TRIM(name));

  RETURN name;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

COMMIT;

-- Verification queries for Migration 6
SELECT 'Migration 6: FSA-Municipality mapping tables created' AS status;

SELECT table_name,
       (SELECT COUNT(*) FROM information_schema.columns WHERE table_name = t.table_name) as column_count
FROM information_schema.tables t
WHERE table_name IN ('fsa_municipality_mappings', 'municipality_aliases')
ORDER BY table_name;

-- Test normalization function
SELECT
  normalize_municipality_name('Greater Sudbury / Grand Sudbury') as test1_expected_greater_sudbury,
  normalize_municipality_name('Prince Edward County') as test2_expected_prince_edward_county,
  normalize_municipality_name('  Toronto  ') as test3_expected_toronto,
  normalize_municipality_name(NULL) as test4_expected_null;

-- ============================================================================
-- MIGRATION 7: Create data sources tracking system
-- ============================================================================

BEGIN;

-- Reference table for standardized data types
-- This becomes the truth source for data type naming across all datasets
CREATE TABLE IF NOT EXISTS data_types (
    id SERIAL PRIMARY KEY,
    type_key VARCHAR(50) UNIQUE NOT NULL,       -- e.g., 'addresses', 'parcels', 'zoning'
    display_name VARCHAR(100) NOT NULL,
    description TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Seed with initial data types
INSERT INTO data_types (type_key, display_name, description) VALUES
    ('addresses', 'Address Points', 'Street addresses with coordinates'),
    ('parcels', 'Property Parcels', 'Land parcel boundaries'),
    ('zoning', 'Zoning Data', 'Zoning classifications and boundaries'),
    ('building_footprints', 'Building Footprints', 'Building outline polygons'),
    ('roads', 'Road Network', 'Street centerlines and attributes'),
    ('boundaries', 'Administrative Boundaries', 'Municipal, ward, or other administrative boundaries'),
    ('postal_codes', 'Postal Code Areas', 'FSA or full postal code boundaries')
ON CONFLICT (type_key) DO NOTHING;

-- Main data sources table
CREATE TABLE IF NOT EXISTS data_sources (
    id SERIAL PRIMARY KEY,
    source_key VARCHAR(50) UNIQUE NOT NULL,     -- matches dataset-mappings.json key
    source_name VARCHAR(200) NOT NULL,          -- e.g., "City of Toronto Open Data Portal"
    status VARCHAR(20) DEFAULT 'potential',     -- potential/confirmed/rejected/blocked

    -- Coverage
    regions_covered TEXT[] NOT NULL,            -- array: ['Toronto', 'East York', 'Scarborough']
    region_type VARCHAR(50),                    -- city/county/region/province
    postal_prefixes TEXT[],
    bbox GEOMETRY(Polygon, 4326),

    -- Manual scraping
    portal_url TEXT,
    scraping_notes TEXT,

    -- Automated access
    api_endpoint TEXT,
    api_type VARCHAR(50),                       -- REST, FeatureServer, MapServer, etc.
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

    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Junction table for many-to-many relationship between sources and data types
CREATE TABLE IF NOT EXISTS data_source_types (
    data_source_id INTEGER REFERENCES data_sources(id) ON DELETE CASCADE,
    data_type_id INTEGER REFERENCES data_types(id) ON DELETE CASCADE,
    PRIMARY KEY (data_source_id, data_type_id)
);

-- Indexes for efficient querying
CREATE INDEX IF NOT EXISTS idx_data_sources_status ON data_sources(status);
CREATE INDEX IF NOT EXISTS idx_data_sources_regions ON data_sources USING GIN(regions_covered);
CREATE INDEX IF NOT EXISTS idx_data_sources_postal ON data_sources USING GIN(postal_prefixes);

-- Trigger to update updated_at timestamp
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

COMMIT;

-- Verification queries for Migration 7
SELECT 'Migration 7: Data sources tracking system created' AS status;

SELECT table_name
FROM information_schema.tables
WHERE table_name IN ('data_types', 'data_sources', 'data_source_types')
ORDER BY table_name;

SELECT * FROM data_types ORDER BY type_key;

-- ============================================================================
-- MIGRATION 8: API Keys and Security Infrastructure
-- ============================================================================

BEGIN;

-- Table: API Keys for authentication
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

-- Indexes for api_keys
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_active ON api_keys(is_active) WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_api_keys_prefix ON api_keys(key_prefix);

-- Add api_key_id reference to api_usage_log for tracking per-key usage
ALTER TABLE api_usage_log ADD COLUMN IF NOT EXISTS api_key_id INTEGER REFERENCES api_keys(id);
ALTER TABLE api_usage_log ADD COLUMN IF NOT EXISTS endpoint VARCHAR(100);
ALTER TABLE api_usage_log ADD COLUMN IF NOT EXISTS response_time_ms INTEGER;
ALTER TABLE api_usage_log ADD COLUMN IF NOT EXISTS status_code INTEGER;
ALTER TABLE api_usage_log ADD COLUMN IF NOT EXISTS client_ip INET;

-- Create index for per-key usage lookups
CREATE INDEX IF NOT EXISTS idx_api_usage_key_id ON api_usage_log(api_key_id);
CREATE INDEX IF NOT EXISTS idx_api_usage_key_date ON api_usage_log(api_key_id, date);

-- Table: Rate limit tracking (for Redis backup/fallback)
CREATE TABLE IF NOT EXISTS rate_limit_buckets (
  id SERIAL PRIMARY KEY,
  api_key_id INTEGER REFERENCES api_keys(id),
  bucket_key VARCHAR(100) NOT NULL,
  request_count INTEGER DEFAULT 0,
  window_start TIMESTAMP DEFAULT NOW(),
  UNIQUE(api_key_id, bucket_key)
);

CREATE INDEX IF NOT EXISTS idx_rate_limit_key ON rate_limit_buckets(api_key_id, bucket_key);

COMMIT;

-- Verification queries for Migration 8
SELECT 'Migration 8: API Keys security infrastructure created' AS status;

SELECT table_name,
       (SELECT COUNT(*) FROM information_schema.columns WHERE table_name = t.table_name) as column_count
FROM information_schema.tables t
WHERE table_name IN ('api_keys', 'rate_limit_buckets')
ORDER BY table_name;

-- ============================================================================
-- MIGRATION 9: Admin Audit Logging
-- ============================================================================

BEGIN;

-- Table: Admin audit log for tracking administrative operations
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

-- Indexes for audit log queries
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON admin_audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_admin_key ON admin_audit_log(admin_key_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON admin_audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_target ON admin_audit_log(target_type, target_id);

COMMIT;

-- Verification queries for Migration 9
SELECT 'Migration 9: Admin audit logging created' AS status;

SELECT table_name,
       (SELECT COUNT(*) FROM information_schema.columns WHERE table_name = t.table_name) as column_count
FROM information_schema.tables t
WHERE table_name = 'admin_audit_log';

-- ============================================================================
-- MIGRATION 10: Add FSA Code to Properties Table
-- ============================================================================

BEGIN;

-- Add fsa_code column to properties table for faster FSA-based lookups
ALTER TABLE properties ADD COLUMN IF NOT EXISTS fsa_code VARCHAR(3);

-- Populate fsa_code with first 3 characters from postal_code
UPDATE properties
SET fsa_code = LEFT(postal_code, 3)
WHERE postal_code IS NOT NULL AND postal_code != '' AND fsa_code IS NULL;

-- Create index for fast FSA lookups on properties
CREATE INDEX IF NOT EXISTS idx_properties_fsa_code ON properties(fsa_code) WHERE fsa_code IS NOT NULL;

COMMIT;

-- Verification queries for Migration 10
SELECT 'Migration 10: FSA code added to properties table' AS status;

SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'properties' AND column_name = 'fsa_code';

SELECT COUNT(*) as properties_with_fsa,
       COUNT(DISTINCT fsa_code) as unique_fsa_codes
FROM properties
WHERE fsa_code IS NOT NULL;

-- ============================================================================
-- MIGRATION 11: NAR Enrichment and Municipal Boundary Infrastructure
-- ============================================================================

BEGIN;

-- Add enrichment tracking columns to NAR addresses
-- These track when NAR records are enriched with municipal open data
ALTER TABLE nar_addresses ADD COLUMN IF NOT EXISTS enriched_from_source VARCHAR(100);
ALTER TABLE nar_addresses ADD COLUMN IF NOT EXISTS enrichment_confidence NUMERIC(4,3);
ALTER TABLE nar_addresses ADD COLUMN IF NOT EXISTS enrichment_date TIMESTAMP;

-- Create index for enriched addresses lookups
CREATE INDEX IF NOT EXISTS idx_nar_addr_enriched ON nar_addresses(enriched_from_source);

-- Table: NAR Enrichment Log
-- Tracks all enrichment operations for auditing and quality control
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

-- Index for enrichment log queries
CREATE INDEX IF NOT EXISTS idx_nar_enrichment_date ON nar_enrichment_log(enrichment_date);
CREATE INDEX IF NOT EXISTS idx_nar_enrichment_source ON nar_enrichment_log(source_dataset);

-- Table: NAR Match Conflicts
-- Stores cases where NAR addresses have conflicting matches from different sources
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

-- Indexes for conflict tracking
CREATE INDEX IF NOT EXISTS idx_nar_conflicts_addr ON nar_match_conflicts(addr_guid);
CREATE INDEX IF NOT EXISTS idx_nar_conflicts_unresolved ON nar_match_conflicts(resolved) WHERE resolved = FALSE;

-- Table: FSA Boundaries
-- Geometries for Forward Sortation Areas (first 3 digits of postal code)
-- NOTE: this table is importer-managed. data_sourcing/importers/import_fsa_boundaries.py
-- reloads it wholesale from a StatCan FSA shapefile via geopandas
-- to_postgis(if_exists='replace'), which bypasses migrations.sql entirely. The
-- columns/constraints below reflect that live/raw-shapefile shape (captured from
-- the live DB) rather than a hand-designed one — see schema.sql's header comment
-- for the full history of this drift. PK constraint name
-- (fsa_boundaries_staging_pkey) preserved as-is from the live DB — a leftover
-- from when this table was created as a staging table.
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

-- Table: Lower-tier Municipal Boundaries
-- Geometries for cities, towns, townships, etc.
-- NOTE: same caveat as fsa_boundaries above — this table is importer-managed.
-- data_sourcing/importers/import_municipal_boundaries.py reloads it wholesale
-- via geopandas to_postgis(if_exists='replace') from an Ontario municipal
-- boundary shapefile. Columns below (captured from the live DB) are raw,
-- truncated shapefile DBF field names preserved as-is; the quoted mixed-case
-- identifiers reflect that.
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

COMMIT;

-- Verification queries for Migration 11
SELECT 'Migration 11: NAR enrichment and boundary infrastructure created' AS status;

-- Check NAR address enrichment columns
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'nar_addresses'
  AND column_name IN ('enriched_from_source', 'enrichment_confidence', 'enrichment_date')
ORDER BY column_name;

-- Check new tables
SELECT table_name,
       (SELECT COUNT(*) FROM information_schema.columns WHERE table_name = t.table_name) as column_count
FROM information_schema.tables t
WHERE table_name IN ('nar_enrichment_log', 'nar_match_conflicts', 'fsa_boundaries', 'lower_municipal_boundaries')
ORDER BY table_name;

-- ============================================================================
-- MIGRATION 12: Data Sources Download Automation
-- ============================================================================

BEGIN;

-- Add columns for download automation
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS download_url TEXT;
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS download_format VARCHAR(20);  -- geojson, csv, shp, zip
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS import_script VARCHAR(100);   -- e.g., import_nar_data.py
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS dataset_mapping_key VARCHAR(50);  -- links to dataset-mappings.json
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS local_file_path TEXT;         -- expected path after download
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS requires_manual_download BOOLEAN DEFAULT FALSE;
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS manual_download_instructions TEXT;

-- Index for finding sources that can be auto-downloaded
CREATE INDEX IF NOT EXISTS idx_data_sources_download ON data_sources(status, download_url)
  WHERE download_url IS NOT NULL;

COMMIT;

-- Verification queries for Migration 12
SELECT 'Migration 12: Data sources download automation columns added' AS status;

SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'data_sources'
  AND column_name IN ('download_url', 'download_format', 'import_script', 'dataset_mapping_key', 'local_file_path')
ORDER BY column_name;

-- ============================================================================
-- MIGRATION 13: Schema Migration Tracking
-- ============================================================================

BEGIN;

-- Track which migrations have been applied
-- This enables intelligent migration running and prevents re-execution
CREATE TABLE IF NOT EXISTS schema_migrations (
    id SERIAL PRIMARY KEY,
    migration_number INTEGER UNIQUE NOT NULL,
    migration_name VARCHAR(200) NOT NULL,
    applied_at TIMESTAMP DEFAULT NOW(),
    checksum VARCHAR(64),                    -- SHA256 of migration content for integrity
    execution_time_ms INTEGER,
    applied_by VARCHAR(100) DEFAULT CURRENT_USER,
    rollback_sql TEXT                        -- Optional: for future rollback support
);

-- Index for fast lookups
CREATE INDEX IF NOT EXISTS idx_schema_migrations_number ON schema_migrations(migration_number);

-- Seed with existing migrations (mark 1-13 as already applied)
-- This maintains backwards compatibility with existing installations
INSERT INTO schema_migrations (migration_number, migration_name, applied_at, checksum)
VALUES
    (1, 'Add dataset tracking to properties table', NOW(), NULL),
    (2, 'Create dataset metadata table', NOW(), NULL),
    (3, 'Create NAR tables', NOW(), NULL),
    (4, 'Create API usage tracking table', NOW(), NULL),
    (5, 'Add FSA code to NAR addresses', NOW(), NULL),
    (6, 'Create FSA-Municipality mapping infrastructure', NOW(), NULL),
    (7, 'Create data sources tracking system', NOW(), NULL),
    (8, 'API Keys and Security Infrastructure', NOW(), NULL),
    (9, 'Admin Audit Logging', NOW(), NULL),
    (10, 'Add FSA Code to Properties Table', NOW(), NULL),
    (11, 'NAR Enrichment and Municipal Boundary Infrastructure', NOW(), NULL),
    (12, 'Data Sources Download Automation', NOW(), NULL),
    (13, 'Schema Migration Tracking', NOW(), NULL)
ON CONFLICT (migration_number) DO NOTHING;

COMMIT;

-- Verification queries for Migration 13
SELECT 'Migration 13: Schema migration tracking table created' AS status;

SELECT table_name,
       (SELECT COUNT(*) FROM information_schema.columns WHERE table_name = t.table_name) as column_count
FROM information_schema.tables t
WHERE table_name = 'schema_migrations';

SELECT COUNT(*) as migrations_seeded FROM schema_migrations;

-- ============================================================================
-- MIGRATION 14: Import History and Audit Trail
-- ============================================================================

BEGIN;

-- Import history - tracks each import operation with full audit trail
CREATE TABLE IF NOT EXISTS import_history (
    id SERIAL PRIMARY KEY,
    source_key VARCHAR(50) NOT NULL,
    import_type VARCHAR(20) NOT NULL DEFAULT 'full',  -- full, incremental, partial
    status VARCHAR(20) NOT NULL DEFAULT 'started',    -- started, completed, failed, rolled_back
    started_at TIMESTAMP DEFAULT NOW(),
    completed_at TIMESTAMP,
    duration_seconds INTEGER,

    -- Row counts
    rows_processed INTEGER DEFAULT 0,
    rows_inserted INTEGER DEFAULT 0,
    rows_updated INTEGER DEFAULT 0,
    rows_failed INTEGER DEFAULT 0,
    rows_skipped INTEGER DEFAULT 0,

    -- File tracking
    file_path TEXT,
    file_hash VARCHAR(64),              -- SHA256 of source file
    file_size_bytes BIGINT,

    -- Error handling
    error_message TEXT,
    error_traceback TEXT,

    -- Extensible metadata
    metadata JSONB DEFAULT '{}'::jsonb
);

-- Individual row-level errors during import
CREATE TABLE IF NOT EXISTS import_row_errors (
    id SERIAL PRIMARY KEY,
    import_id INTEGER NOT NULL REFERENCES import_history(id) ON DELETE CASCADE,
    row_number INTEGER,
    error_type VARCHAR(100),            -- validation_error, constraint_violation, parse_error
    error_message TEXT NOT NULL,
    row_data JSONB,                     -- Original row data for debugging
    created_at TIMESTAMP DEFAULT NOW()
);

-- Indexes for import_history
CREATE INDEX IF NOT EXISTS idx_import_history_source ON import_history(source_key);
CREATE INDEX IF NOT EXISTS idx_import_history_status ON import_history(status);
CREATE INDEX IF NOT EXISTS idx_import_history_started ON import_history(started_at);

-- Indexes for import_row_errors
CREATE INDEX IF NOT EXISTS idx_import_row_errors_import ON import_row_errors(import_id);
CREATE INDEX IF NOT EXISTS idx_import_row_errors_type ON import_row_errors(error_type);

-- View: Latest import per source
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

COMMIT;

-- Verification queries for Migration 14
SELECT 'Migration 14: Import history and audit trail created' AS status;

SELECT table_name,
       (SELECT COUNT(*) FROM information_schema.columns WHERE table_name = t.table_name) as column_count
FROM information_schema.tables t
WHERE table_name IN ('import_history', 'import_row_errors')
ORDER BY table_name;

-- ============================================================================
-- MIGRATION 15: Upstream Version Detection and Sync Tracking
-- ============================================================================

BEGIN;

-- Add sync tracking columns to data_sources
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS upstream_version TEXT;
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS upstream_etag TEXT;
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS upstream_hash VARCHAR(64);
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS upstream_last_modified TIMESTAMP;
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS sync_status VARCHAR(20) DEFAULT 'never_synced';
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS last_sync_check TIMESTAMP;
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS last_sync_error TEXT;

-- Constraint for sync_status values
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_sync_status'
    ) THEN
        ALTER TABLE data_sources ADD CONSTRAINT chk_sync_status
            CHECK (sync_status IN ('never_synced', 'synced', 'stale', 'checking', 'error'));
    END IF;
END $$;

-- Index for finding stale sources
CREATE INDEX IF NOT EXISTS idx_data_sources_sync_status ON data_sources(sync_status);

COMMIT;

-- Verification queries for Migration 15
SELECT 'Migration 15: Upstream version detection columns added' AS status;

SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'data_sources'
  AND column_name IN ('upstream_version', 'upstream_etag', 'upstream_hash', 'sync_status', 'last_sync_check')
ORDER BY column_name;

-- ============================================================================
-- MIGRATION 16: Add columns for transform pipeline
-- ============================================================================

BEGIN;

-- Add city_original column to preserve pre-canonical municipality names
-- Used when mapping pre-amalgamation cities (Etobicoke → Toronto, etc.)
ALTER TABLE properties ADD COLUMN IF NOT EXISTS city_original VARCHAR(100);

-- Index for querying by original city name
CREATE INDEX IF NOT EXISTS idx_properties_city_original
    ON properties(city_original) WHERE city_original IS NOT NULL;

COMMIT;

-- Verification queries for Migration 16
SELECT 'Migration 16: Transform pipeline columns added' AS status;

SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'properties'
  AND column_name = 'city_original';


-- ============================================================================
-- MIGRATION 17: Drop duplicate postal_prefix column (use fsa_code instead)
-- ============================================================================

BEGIN;

-- Drop the duplicate postal_prefix column if it exists
-- (The fsa_code column already exists and is the canonical column name)
ALTER TABLE properties DROP COLUMN IF EXISTS postal_prefix;

-- Drop the index if it exists
DROP INDEX IF EXISTS idx_properties_postal_prefix;

COMMIT;

-- Verification queries for Migration 17
SELECT 'Migration 17: Removed duplicate postal_prefix column' AS status;

SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'properties'
  AND column_name IN ('fsa_code', 'postal_prefix')
ORDER BY column_name;

-- ============================================================================
-- MIGRATION 18: Add unit_no and civic_no_suffix to properties table
-- ============================================================================

BEGIN;

-- Add unit number column
-- Stores apartment/unit/suite numbers (e.g., "5", "200", "3B")
ALTER TABLE properties ADD COLUMN IF NOT EXISTS unit_no VARCHAR(20);

-- Add civic number suffix column
-- Stores suffixes like A, B, 1/2 (e.g., "A" from "100A Main St")
ALTER TABLE properties ADD COLUMN IF NOT EXISTS civic_no_suffix VARCHAR(10);

-- Index for unit number lookups (useful for deduplication and matching)
CREATE INDEX IF NOT EXISTS idx_properties_unit_no
    ON properties(unit_no) WHERE unit_no IS NOT NULL;

-- Composite index for full address matching including unit and suffix
CREATE INDEX IF NOT EXISTS idx_properties_addr_with_unit
    ON properties(postal_code, street_number, civic_no_suffix, unit_no);

-- Record migration
INSERT INTO schema_migrations (migration_number, migration_name, applied_at)
VALUES (18, 'Add unit_no and civic_no_suffix to properties', NOW())
ON CONFLICT (migration_number) DO NOTHING;

COMMIT;

-- Verification queries for Migration 18
SELECT 'Migration 18: unit_no and civic_no_suffix columns added' AS status;

SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'properties'
  AND column_name IN ('unit_no', 'civic_no_suffix')
ORDER BY column_name;

-- ============================================================================
-- MIGRATION 19: Soft delete and import tracking columns
-- ============================================================================

BEGIN;

-- Adds columns for:
-- 1. Soft delete support (is_deleted, deleted_at, deleted_by_import_id)
-- 2. Import freshness tracking (last_seen_import_id)
-- 3. Extended import_history stats (rows_deleted)
-- This enables stale record detection when re-importing updated datasets.

-- Add soft delete columns to properties table
ALTER TABLE properties ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;
ALTER TABLE properties ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP;
ALTER TABLE properties ADD COLUMN IF NOT EXISTS deleted_by_import_id INTEGER;
ALTER TABLE properties ADD COLUMN IF NOT EXISTS last_seen_import_id INTEGER;

-- Partial index for active (non-deleted) records - optimizes most queries
CREATE INDEX IF NOT EXISTS idx_properties_active
    ON properties(property_id) WHERE is_deleted = FALSE;

-- Index for finding records by last import (useful for stale detection)
CREATE INDEX IF NOT EXISTS idx_properties_last_import
    ON properties(source_dataset, last_seen_import_id);

-- Add soft delete columns to nar_addresses table
ALTER TABLE nar_addresses ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;
ALTER TABLE nar_addresses ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP;
ALTER TABLE nar_addresses ADD COLUMN IF NOT EXISTS deleted_by_import_id INTEGER;
ALTER TABLE nar_addresses ADD COLUMN IF NOT EXISTS last_seen_import_id INTEGER;

-- Partial index for active NAR addresses
CREATE INDEX IF NOT EXISTS idx_nar_addresses_active
    ON nar_addresses(addr_guid) WHERE is_deleted = FALSE;

-- Add soft delete columns to nar_locations table
ALTER TABLE nar_locations ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;
ALTER TABLE nar_locations ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP;
ALTER TABLE nar_locations ADD COLUMN IF NOT EXISTS deleted_by_import_id INTEGER;
ALTER TABLE nar_locations ADD COLUMN IF NOT EXISTS last_seen_import_id INTEGER;

-- Add rows_deleted tracking to import_history
ALTER TABLE import_history ADD COLUMN IF NOT EXISTS rows_deleted INTEGER DEFAULT 0;

-- Record migration
INSERT INTO schema_migrations (migration_number, migration_name, applied_at)
VALUES (19, 'Soft delete and import tracking columns', NOW())
ON CONFLICT (migration_number) DO NOTHING;

COMMIT;

-- Verification queries for Migration 19
SELECT 'Migration 19: Soft delete and import tracking columns added' AS status;

SELECT column_name, data_type, column_default
FROM information_schema.columns
WHERE table_name = 'properties'
  AND column_name IN ('is_deleted', 'deleted_at', 'deleted_by_import_id', 'last_seen_import_id')
ORDER BY column_name;

SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'import_history'
  AND column_name = 'rows_deleted';

-- ============================================================================
-- MIGRATION 20: Fuzzy Street Name Matching with pg_trgm
-- ============================================================================

BEGIN;

-- Enables trigram-based similarity matching for street names to catch typos
-- like "Elizbeth" → "Elizabeth" or "Queenswei" → "Queensway"

-- Enable pg_trgm extension for trigram similarity functions
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- GIN index on properties.street_name for fuzzy matching
-- Uses gin_trgm_ops operator class for trigram-based similarity searches
CREATE INDEX IF NOT EXISTS idx_properties_street_name_trgm
    ON properties USING GIN (street_name gin_trgm_ops);

-- GIN index on nar_addresses.official_street_name for fuzzy matching
CREATE INDEX IF NOT EXISTS idx_nar_addr_street_name_trgm
    ON nar_addresses USING GIN (official_street_name gin_trgm_ops);

-- Record migration
INSERT INTO schema_migrations (migration_number, migration_name, applied_at)
VALUES (20, 'Fuzzy street name matching with pg_trgm', NOW())
ON CONFLICT (migration_number) DO NOTHING;

COMMIT;

-- Verification queries for Migration 20
SELECT 'Migration 20: Fuzzy street name matching enabled' AS status;

-- Verify extension is installed
SELECT extname, extversion FROM pg_extension WHERE extname = 'pg_trgm';

-- Verify indexes were created
SELECT indexname, indexdef
FROM pg_indexes
WHERE indexname LIKE '%trgm%'
ORDER BY indexname;

-- ============================================================================
-- MIGRATION 21: Data Source Health Check and Change Detection System
-- ============================================================================

BEGIN;

-- Adds fields for tracking source CRS, update frequency, schema, and provider
-- modification dates. Creates source_health_checks table for logging probe
-- results and v_sources_needing_attention view for computed status.

-- Add new tracking fields to data_sources
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS source_crs INTEGER;
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS expected_crs INTEGER DEFAULT 4326;
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS update_frequency VARCHAR(50);
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS schema_fields JSONB;
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS schema_hash VARCHAR(64);
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS provider_last_modified TIMESTAMP;
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS required_attribution TEXT;

-- Constraint for update_frequency values
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_update_frequency'
    ) THEN
        ALTER TABLE data_sources ADD CONSTRAINT chk_update_frequency
            CHECK (update_frequency IS NULL OR update_frequency IN (
                'daily', 'weekly', 'monthly', 'quarterly', 'annual', 'on_demand', 'unknown'
            ));
    END IF;
END $$;

-- Index for update_frequency queries
CREATE INDEX IF NOT EXISTS idx_data_sources_update_frequency
    ON data_sources(update_frequency) WHERE update_frequency IS NOT NULL;

-- Create source_health_checks table for logging probe results
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
    change_type VARCHAR(50),  -- content, schema, count, none, error

    -- ArcGIS-specific
    arcgis_last_edit_date TIMESTAMP,

    -- Error tracking
    error_type VARCHAR(100),
    error_message TEXT,

    -- Foreign key (allowing cascade delete if source removed)
    CONSTRAINT fk_source_health_checks_source
        FOREIGN KEY (source_key)
        REFERENCES data_sources(source_key)
        ON DELETE CASCADE
);

-- Constraint for change_type values
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_health_change_type'
    ) THEN
        ALTER TABLE source_health_checks ADD CONSTRAINT chk_health_change_type
            CHECK (change_type IS NULL OR change_type IN (
                'content', 'schema', 'count', 'none', 'error'
            ));
    END IF;
END $$;

-- Indexes for source_health_checks
CREATE INDEX IF NOT EXISTS idx_health_checks_source_key
    ON source_health_checks(source_key);
CREATE INDEX IF NOT EXISTS idx_health_checks_checked_at
    ON source_health_checks(checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_health_checks_source_time
    ON source_health_checks(source_key, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_health_checks_has_changes
    ON source_health_checks(source_key, has_changes)
    WHERE has_changes = TRUE;

-- Create view for sources needing attention (computed status)
-- Shows sources where status='confirmed' AND sync_status IN ('stale', 'error')
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
    -- Calculate staleness duration
    CASE
        WHEN ds.last_sync_check IS NULL THEN NULL
        ELSE NOW() - ds.last_sync_check
    END as time_since_check,
    -- Get most recent health check with changes
    hc.checked_at as last_change_detected,
    hc.change_type,
    hc.record_count as latest_record_count,
    hc.error_type as latest_error_type,
    hc.error_message as latest_error_message,
    -- Calculate expected check interval based on update_frequency
    CASE ds.update_frequency
        WHEN 'daily' THEN INTERVAL '1 day'
        WHEN 'weekly' THEN INTERVAL '7 days'
        WHEN 'monthly' THEN INTERVAL '30 days'
        WHEN 'quarterly' THEN INTERVAL '90 days'
        WHEN 'annual' THEN INTERVAL '365 days'
        ELSE INTERVAL '7 days'  -- default for unknown/on_demand/NULL
    END as expected_check_interval,
    -- Calculate if overdue for check
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

-- Record migration
INSERT INTO schema_migrations (migration_number, migration_name, applied_at)
VALUES (21, 'Data source health check and change detection system', NOW())
ON CONFLICT (migration_number) DO NOTHING;

COMMIT;

-- Verification queries for Migration 21
SELECT 'Migration 21: Health check system created' AS status;

-- Check new columns on data_sources
SELECT column_name, data_type, column_default
FROM information_schema.columns
WHERE table_name = 'data_sources'
  AND column_name IN ('source_crs', 'expected_crs', 'update_frequency',
                      'schema_fields', 'schema_hash', 'provider_last_modified',
                      'required_attribution')
ORDER BY column_name;

-- Check source_health_checks table
SELECT table_name,
       (SELECT COUNT(*) FROM information_schema.columns WHERE table_name = t.table_name) as column_count
FROM information_schema.tables t
WHERE table_name = 'source_health_checks';

-- Check view exists
SELECT viewname
FROM pg_views
WHERE viewname = 'v_sources_needing_attention';

-- ============================================================================
-- MIGRATION 22: Unified Download System with Adaptive Rate Limiting
-- ============================================================================

BEGIN;

-- Adds columns for:
-- 1. Download configuration (page size, retry settings, delays)
-- 2. Server capability caching (maxRecordCount, geometryType, formats)
-- 3. Adaptive rate limiting state (token bucket, backoff)
-- 4. Resume checkpoints for interrupted downloads
-- 5. Multi-endpoint aggregation config (for Middlesex-style sources)
-- 6. Schema auto-discovery suggestions for review

-- Download configuration (page size, retry settings, request delays)
-- Example: {"page_size": 500, "request_delay_seconds": 6.0, "max_retries": 10}
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS download_config JSONB DEFAULT '{}';

-- Cached server capabilities (from probing ArcGIS metadata)
-- Example: {"max_record_count": 2000, "geometry_type": "esriGeometryPoint", "supports_geojson": true}
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS server_capabilities JSONB;

-- Rate limiting configuration
-- Example: {"strategy": "adaptive", "min_delay_seconds": 0, "backoff_base": 30}
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS rate_limit_config JSONB DEFAULT '{"strategy": "adaptive"}';

-- Resume checkpoint for interrupted downloads
-- Example: {"last_objectid": 50000, "records_written": 50000, "timestamp": "..."}
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS resume_checkpoint JSONB;

-- Multi-endpoint configuration for aggregate sources like Middlesex County
-- Example: {"endpoints": [{"name": "Newbury", "item_id": "...", "is_condo": false}], ...}
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS multi_endpoint_config JSONB;

-- Auto-discovered field mapping suggestions (pending human review)
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS suggested_mappings JSONB;

-- Status of suggested mappings: 'none', 'pending_review', 'approved', 'rejected'
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS suggested_mappings_status VARCHAR(20) DEFAULT 'none';

-- Constraint for suggested_mappings_status values
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_suggested_mappings_status'
    ) THEN
        ALTER TABLE data_sources ADD CONSTRAINT chk_suggested_mappings_status
            CHECK (suggested_mappings_status IN ('none', 'pending_review', 'approved', 'rejected'));
    END IF;
END $$;

-- Index for finding sources with pending mapping reviews
CREATE INDEX IF NOT EXISTS idx_data_sources_pending_mappings
    ON data_sources(suggested_mappings_status)
    WHERE suggested_mappings_status = 'pending_review';

-- Index for finding sources with resume checkpoints
CREATE INDEX IF NOT EXISTS idx_data_sources_has_checkpoint
    ON data_sources(source_key)
    WHERE resume_checkpoint IS NOT NULL;

-- Create download_runs table for tracking each download operation
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

    -- Server capabilities discovered during download
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

    -- Foreign key
    CONSTRAINT fk_download_runs_source
        FOREIGN KEY (source_key)
        REFERENCES data_sources(source_key)
        ON DELETE CASCADE
);

-- Constraint for download_runs status values
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_download_run_status'
    ) THEN
        ALTER TABLE download_runs ADD CONSTRAINT chk_download_run_status
            CHECK (status IN ('running', 'completed', 'failed', 'interrupted', 'cancelled'));
    END IF;
END $$;

-- Indexes for download_runs
CREATE INDEX IF NOT EXISTS idx_download_runs_source ON download_runs(source_key, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_download_runs_status ON download_runs(status);
CREATE INDEX IF NOT EXISTS idx_download_runs_running ON download_runs(source_key) WHERE status = 'running';

-- Create field_mapping_suggestions table for schema auto-discovery review workflow
CREATE TABLE IF NOT EXISTS field_mapping_suggestions (
    id SERIAL PRIMARY KEY,
    source_key VARCHAR(50) NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    reviewed_at TIMESTAMP,
    reviewed_by VARCHAR(100),
    status VARCHAR(20) DEFAULT 'pending',

    -- Schema information from source
    source_fields JSONB NOT NULL,  -- [{name, type, sample_values, null_count}]

    -- Auto-generated mapping suggestions with confidence
    suggested_mappings JSONB NOT NULL,  -- {street_number: "STREETNO", ...}
    confidence_scores JSONB,  -- {street_number: 0.95, street_name: 0.87, ...}

    -- Human review results
    approved_mappings JSONB,  -- Final approved mappings (may differ from suggestions)
    rejection_notes TEXT,

    -- Foreign key
    CONSTRAINT fk_field_mapping_suggestions_source
        FOREIGN KEY (source_key)
        REFERENCES data_sources(source_key)
        ON DELETE CASCADE
);

-- Constraint for field_mapping_suggestions status
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_mapping_suggestion_status'
    ) THEN
        ALTER TABLE field_mapping_suggestions ADD CONSTRAINT chk_mapping_suggestion_status
            CHECK (status IN ('pending', 'approved', 'rejected', 'modified'));
    END IF;
END $$;

-- Indexes for field_mapping_suggestions
CREATE INDEX IF NOT EXISTS idx_mapping_suggestions_source
    ON field_mapping_suggestions(source_key);
CREATE INDEX IF NOT EXISTS idx_mapping_suggestions_pending
    ON field_mapping_suggestions(source_key)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_mapping_suggestions_status
    ON field_mapping_suggestions(status);

-- Ensure only one pending suggestion per source
CREATE UNIQUE INDEX IF NOT EXISTS idx_mapping_suggestions_one_pending
    ON field_mapping_suggestions(source_key)
    WHERE status = 'pending';

-- View: Download run statistics per source
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

-- View: Sources with pending field mapping suggestions
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
    -- Count high-confidence suggestions (>= 0.8)
    (SELECT COUNT(*)
     FROM jsonb_each_text(fms.confidence_scores) c
     WHERE c.value::numeric >= 0.8) as high_confidence_count
FROM data_sources ds
JOIN field_mapping_suggestions fms ON ds.source_key = fms.source_key
WHERE fms.status = 'pending'
ORDER BY fms.created_at ASC;

-- Record migration
INSERT INTO schema_migrations (migration_number, migration_name, applied_at)
VALUES (22, 'Unified download system with adaptive rate limiting', NOW())
ON CONFLICT (migration_number) DO NOTHING;

COMMIT;

-- Verification queries for Migration 22
SELECT 'Migration 22: Unified download system created' AS status;

-- Check new columns on data_sources
SELECT column_name, data_type, column_default
FROM information_schema.columns
WHERE table_name = 'data_sources'
  AND column_name IN ('download_config', 'server_capabilities', 'rate_limit_config',
                      'resume_checkpoint', 'multi_endpoint_config', 'suggested_mappings',
                      'suggested_mappings_status')
ORDER BY column_name;

-- Check download_runs table
SELECT table_name,
       (SELECT COUNT(*) FROM information_schema.columns WHERE table_name = t.table_name) as column_count
FROM information_schema.tables t
WHERE table_name = 'download_runs';

-- Check field_mapping_suggestions table
SELECT table_name,
       (SELECT COUNT(*) FROM information_schema.columns WHERE table_name = t.table_name) as column_count
FROM information_schema.tables t
WHERE table_name = 'field_mapping_suggestions';

-- Check views exist
SELECT viewname
FROM pg_views
WHERE viewname IN ('v_download_stats', 'v_pending_mapping_reviews')
ORDER BY viewname;

-- ============================================================================
-- MIGRATION 23: Commercial Postal Code Database
-- ============================================================================

BEGIN;

-- Purpose: Store commercial postal code data for validation against existing
--          geocoder reference data (FSA boundaries, municipality mappings)
-- Note: header must be exactly "rule / MIGRATION N: title / rule / blank /
-- BEGIN;" or run_migrations.py cannot parse this block (see migration 25 note).
-- This block previously had a "Purpose" comment sandwiched between two rule
-- lines before BEGIN;, which made the non-greedy name capture swallow it and
-- overflow schema_migrations.migration_name (VARCHAR(200)) on a fresh install.

-- Table: commercial_postal_codes
-- Reference postal code data with geocoordinates from commercial source
CREATE TABLE IF NOT EXISTS commercial_postal_codes (
    id SERIAL PRIMARY KEY,
    postal_code_raw VARCHAR(7) NOT NULL,        -- Original format with space 'L7J 2L7'
    postal_code VARCHAR(6) NOT NULL,            -- Normalized without space 'L7J2L7'
    fsa_code VARCHAR(3) NOT NULL,               -- FSA extracted from postal code
    city VARCHAR(100),                          -- City name from source
    city_normalized VARCHAR(100),               -- Normalized via normalize_municipality_name()
    uri VARCHAR(100),                           -- URL slug from source
    latitude DECIMAL(12,8),                     -- Coordinate
    longitude DECIMAL(12,8),                    -- Coordinate
    location GEOGRAPHY(Point, 4326),            -- PostGIS point for spatial queries
    -- Original foreign key values preserved for potential future use
    source_community_id INTEGER,
    source_county_id INTEGER,
    source_region_id INTEGER,
    -- Audit fields
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Indexes for efficient lookup and validation queries
CREATE INDEX IF NOT EXISTS idx_comm_postal_code ON commercial_postal_codes(postal_code);
CREATE INDEX IF NOT EXISTS idx_comm_postal_fsa ON commercial_postal_codes(fsa_code);
CREATE INDEX IF NOT EXISTS idx_comm_postal_city_norm ON commercial_postal_codes(city_normalized);
CREATE INDEX IF NOT EXISTS idx_comm_postal_location ON commercial_postal_codes USING GIST(location);
CREATE INDEX IF NOT EXISTS idx_comm_postal_raw ON commercial_postal_codes(postal_code_raw);
-- Note: postal_code_raw is not unique - source data contains duplicates

-- Record migration
INSERT INTO schema_migrations (migration_number, migration_name, applied_at)
VALUES (23, 'Commercial postal code database', NOW())
ON CONFLICT (migration_number) DO NOTHING;

COMMIT;

-- Verification queries for Migration 23
SELECT 'Migration 23: Commercial postal code database created' AS status;

-- Check table structure
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'commercial_postal_codes'
ORDER BY ordinal_position;

-- Check indexes
SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename = 'commercial_postal_codes'
ORDER BY indexname;

-- ============================================================================
-- MIGRATION 24: Postal Code Municipality Assignment
-- ============================================================================

BEGIN;

-- Purpose: Add columns for spatial municipality assignment to postal codes
--          Enables community→municipality alias extraction
-- Note: header must be exactly "rule / MIGRATION N: title / rule / blank /
-- BEGIN;" or run_migrations.py cannot parse this block (see migration 25 note).
-- This block previously had a "Purpose" comment sandwiched between two rule
-- lines before BEGIN;, which made the non-greedy name capture swallow it and
-- overflow schema_migrations.migration_name (VARCHAR(200)) on a fresh install.

-- Add columns to track official municipality assignment via spatial join
ALTER TABLE commercial_postal_codes
ADD COLUMN IF NOT EXISTS official_municipality VARCHAR(100),
ADD COLUMN IF NOT EXISTS official_municipal_id TEXT,
ADD COLUMN IF NOT EXISTS municipality_assigned_at TIMESTAMP;

-- Index for querying by official municipality
CREATE INDEX IF NOT EXISTS idx_comm_postal_official_muni
ON commercial_postal_codes(official_municipality);

-- Record migration
INSERT INTO schema_migrations (migration_number, migration_name, applied_at)
VALUES (24, 'Postal code municipality assignment columns', NOW())
ON CONFLICT (migration_number) DO NOTHING;

COMMIT;

-- Verification queries for Migration 24
SELECT 'Migration 24: Postal code municipality assignment columns added' AS status;

-- Check new columns
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'commercial_postal_codes'
  AND column_name IN ('official_municipality', 'official_municipal_id', 'municipality_assigned_at')
ORDER BY column_name;

-- ============================================================================
-- MIGRATION 25: Widen street_direction column
-- ============================================================================

BEGIN;

-- Purpose: Some source data contains street direction values longer than 10
-- characters, causing INSERT failures ("value too long for type character
-- varying(10)"). Widen to VARCHAR(20) to match street_suffix.
-- Note: the header above must be exactly "rule / MIGRATION N: title / rule /
-- blank / BEGIN;" or run_migrations.py cannot parse this block (the original
-- format with comments between the rule and BEGIN; was invisible to the
-- runner, which is why this migration was never actually applied).

ALTER TABLE properties ALTER COLUMN street_direction TYPE VARCHAR(20);

INSERT INTO schema_migrations (migration_number, migration_name)
VALUES (25, 'Widen street_direction from VARCHAR(10) to VARCHAR(20)')
ON CONFLICT (migration_number) DO NOTHING;

COMMIT;

SELECT 'Migration 25: street_direction widened to VARCHAR(20)' AS status;

-- ============================================================================
-- MIGRATION 26: Widen nar_addresses.official_street_dir column
-- ============================================================================

BEGIN;

-- Purpose: same truncation class migration 25 fixed for properties. NAR
-- street direction values can exceed 10 characters; widen to VARCHAR(20)
-- to match properties.street_direction so imports neither fail nor
-- silently truncate.

ALTER TABLE nar_addresses ALTER COLUMN official_street_dir TYPE VARCHAR(20);

INSERT INTO schema_migrations (migration_number, migration_name)
VALUES (26, 'Widen nar_addresses.official_street_dir from VARCHAR(10) to VARCHAR(20)')
ON CONFLICT (migration_number) DO NOTHING;

COMMIT;

SELECT 'Migration 26: official_street_dir widened to VARCHAR(20)' AS status;

-- ============================================================================
-- MIGRATION 27: Backfill data_sources.required_attribution
-- ============================================================================

BEGIN;

-- Purpose: database/seed_data_sources.sql now populates required_attribution
-- on INSERT, but a database seeded before that change (or one whose seed
-- script has not been re-run) still has NULL in this column for every row.
-- Backfill the same attribution wording the seed file now uses so licence
-- compliance does not depend on re-running the seed script in a live DB.
--
-- bruce_county_addresses is intentionally NOT covered here: its licence
-- (Bruce County Open Data Licence) is unconfirmed for commercial use and
-- the source status is 'blocked' (see license_notes), so no attribution
-- statement can be mandated yet.

UPDATE data_sources SET required_attribution =
    'Contains information licensed under the Open Government Licence – Canada.'
    WHERE source_key = 'nar_ontario';

UPDATE data_sources SET required_attribution =
    'Contains information licensed under the Open Government Licence – Toronto.'
    WHERE source_key = 'toronto_addresses';

UPDATE data_sources SET required_attribution =
    'Contains information licensed under the Open Government Licence – Guelph.'
    WHERE source_key = 'guelph_addresses';

UPDATE data_sources SET required_attribution =
    'Contains information licensed under the Open Government Licence – Ottawa.'
    WHERE source_key = 'ottawa_addresses';

UPDATE data_sources SET required_attribution =
    'Contains information licensed under the Open Government Licence – Canada.'
    WHERE source_key = 'fsa_boundaries';

UPDATE data_sources SET required_attribution =
    'Contains information licensed under the Open Government Licence – Ontario.'
    WHERE source_key = 'ontario_municipal_boundaries';

UPDATE data_sources SET required_attribution =
    'Contains information licensed under the Open Government Licence – Hamilton.'
    WHERE source_key = 'hamilton_addresses';

UPDATE data_sources SET required_attribution =
    'Contains information licensed under the Open Government Licence – Middlesex County.'
    WHERE source_key = 'middlesex_county_addresses';

INSERT INTO schema_migrations (migration_number, migration_name)
VALUES (27, 'Backfill required_attribution on data_sources')
ON CONFLICT (migration_number) DO NOTHING;

COMMIT;

SELECT 'Migration 27: required_attribution backfilled' AS status;

-- Verification: every confirmed source except the unconfirmed-licence
-- bruce_county_addresses should now have required_attribution set.
SELECT source_key, required_attribution
FROM data_sources
WHERE status = 'confirmed'
ORDER BY source_key;

-- ============================================================================
-- MIGRATION 28: Fix county source licence URLs and Elgin attribution
-- ============================================================================

BEGIN;

-- Purpose: the Elgin County and Frontenac County data_sources rows carried a
-- license_url pointing at a PDF on a developer's local machine, and Elgin
-- had no required_attribution.
-- These sources are added via management tooling (not seed_data_sources.sql),
-- so this migration keeps the fix reproducible for any DB holding these rows.
-- Idempotent: UPDATE ... WHERE source_key = ... no-ops on a DB without the rows.
--
-- Note: the county Open Data Licence PDFs are not yet confirmed at a public
-- URL; license_url points at the county open-data portal and the PDF must be
-- archived under docs/licences/ before commercial launch.

UPDATE data_sources SET
    license_url = 'https://geohub.elgin.ca',
    required_attribution = 'Contains information licensed under the Open Government Licence – Elgin County.',
    license_notes = 'Based on Open Government License. Commercial use allowed with attribution required. Licence PDF pending archival under docs/licences/ before commercial launch.'
    WHERE source_key = 'elgin_county_addresses';

UPDATE data_sources SET
    license_url = 'https://frontenacmaps.ca',
    license_notes = 'Commercial use allowed, requires attribution, Open Government License adapted. Licence PDF pending archival under docs/licences/ before commercial launch.'
    WHERE source_key = 'frontenac_county';

COMMIT;

-- ============================================================================
-- MIGRATION 29: Attribution for OGL county/region sources; flag bespoke licences
-- ============================================================================

BEGIN;

-- Purpose: 14 confirmed, commercial-use sources in the live DB (added via
-- management tooling, not seed_data_sources.sql) still had NULL
-- required_attribution after migrations 27/28. Three carry a true Open
-- Government Licence, whose mandated attribution wording is known and applied
-- here. The other eleven use bespoke municipal open-data licences / terms of
-- use whose exact required attribution text must be read from each licence;
-- they are left NULL (so the API returns no attribution rather than an
-- incorrect one) and flagged in license_notes for per-licence review before
-- commercial launch. Idempotent.

UPDATE data_sources SET required_attribution =
    'Contains information licensed under the Open Government Licence – Niagara Region.'
    WHERE source_key = 'niagara_region_addresses';

UPDATE data_sources SET required_attribution =
    'Contains information licensed under the Open Government Licence – Dufferin County.'
    WHERE source_key = 'dufferin_county_addresses';

UPDATE data_sources SET required_attribution =
    'Contains information licensed under the Open Government Licence – Haldimand County.'
    WHERE source_key = 'haldimand_county_addresses';

UPDATE data_sources
SET license_notes = COALESCE(license_notes || ' ', '')
    || 'Attribution text pending per-licence review before commercial launch.'
    WHERE source_key IN (
        'barrie_addresses', 'burlington', 'kingston_addresses', 'london_addresses',
        'windsor_addresses', 'kawartha_lakes_addresses', 'peel_region_addresses',
        'norfolk_county', 'durham_region_addresses', 'waterloo_region_addresses',
        'york_region_addresses'
    )
    AND (license_notes IS NULL
         OR license_notes NOT LIKE '%Attribution text pending per-licence review%');

COMMIT;

-- ============================================================================
-- MIGRATION 30: Add latitude/longitude to nar_addresses
-- ============================================================================

BEGIN;

-- Purpose: Tier 3 (NAR) geocoding never matched because the layered matcher
-- joined nar_locations for coordinates, but nar_locations is empty and
-- nar_addresses.loc_guid is NULL on every row. The real coordinates live in
-- nar_addresses.bg_x/bg_y (EPSG:3347, Statistics Canada Lambert). These two
-- columns hold the WGS84 (EPSG:4326) lat/lon derived from bg_x/bg_y so the
-- matcher can read coordinates straight off nar_addresses. Populated by
-- data_sourcing/enrichment/add_latlong_to_nar.py.
-- Note: header must be exactly "rule / MIGRATION N: title / rule / blank /
-- BEGIN;" or run_migrations.py cannot parse this block (see migration 25 note).

ALTER TABLE nar_addresses
    ADD COLUMN IF NOT EXISTS latitude numeric(10,8),
    ADD COLUMN IF NOT EXISTS longitude numeric(11,8);

INSERT INTO schema_migrations (migration_number, migration_name)
VALUES (30, 'Add latitude/longitude to nar_addresses')
ON CONFLICT (migration_number) DO NOTHING;

COMMIT;

SELECT 'Migration 30: latitude/longitude columns added to nar_addresses' AS status;

-- ============================================================================
-- MIGRATION 31: Expression index for case-insensitive NAR civic+street lookup
-- ============================================================================

BEGIN;

-- Purpose: the Tier-3 NAR fallback layers filter on UPPER(civic_no) and
-- UPPER(official_street_name), but the only civic+street index
-- (idx_nar_addr_components) is on the RAW columns, so it cannot be used. The
-- city+core layer (nar_city_core) therefore sequentially scans all ~6.24M rows
-- (~8.9s/call, EXPLAIN: "Rows Removed by Filter: 6245550"). This expression
-- index matches the UPPER(...) predicates so those lookups use an index scan;
-- the leading UPPER(civic_no) column also prefilters the fuzzy layers (5/6),
-- whose street candidates already come from idx_nar_addr_street_name_trgm.
-- Read-only: an index changes query speed, never match results.
-- Note: header must be exactly "rule / MIGRATION N: title / rule / blank /
-- BEGIN;" or run_migrations.py cannot parse this block (see migration 25 note).

CREATE INDEX IF NOT EXISTS idx_nar_addr_upper_civic_street
    ON nar_addresses (UPPER(civic_no), UPPER(official_street_name));

INSERT INTO schema_migrations (migration_number, migration_name)
VALUES (31, 'Expression index on UPPER(civic_no), UPPER(official_street_name) for NAR')
ON CONFLICT (migration_number) DO NOTHING;

COMMIT;

SELECT 'Migration 31: expression index idx_nar_addr_upper_civic_street created' AS status;

-- ============================================================================
-- MIGRATION 32: Expression index for case-insensitive municipal civic+street lookup
-- ============================================================================

BEGIN;

-- Purpose: the Tier-2 municipal matcher filters on UPPER(street_number) and
-- similarity(UPPER(street_name), ...), but no index matches those expressions:
-- idx_properties_composite is on the RAW columns, idx_properties_normalized
-- wraps them in TRIM() (so UPPER(street_number) misses it), and
-- idx_properties_street_name_trgm is on RAW street_name (so
-- similarity(UPPER(street_name), ...) misses it). The municipal fuzzy layers
-- therefore parallel-seq-scan all ~4.1M properties (~11.2s/call, EXPLAIN:
-- "Rows Removed by Filter: 4107644"). This mirrors migration 31 for NAR: the
-- leading UPPER(street_number) column lets the fuzzy layers bitmap-index-scan
-- to the handful of rows for that civic number before the similarity filter,
-- and it also serves the exact municipal layers (UPPER(street_number) =
-- UPPER($2) AND UPPER(street_name) = UPPER($3)). Read-only: speeds queries,
-- never changes match results.
-- Note: header must be exactly "rule / MIGRATION N: title / rule / blank /
-- BEGIN;" or run_migrations.py cannot parse this block (see migration 25 note).

CREATE INDEX IF NOT EXISTS idx_properties_upper_civic_street
    ON properties (UPPER(street_number), UPPER(street_name));

INSERT INTO schema_migrations (migration_number, migration_name)
VALUES (32, 'Expression index on UPPER(street_number), UPPER(street_name) for properties')
ON CONFLICT (migration_number) DO NOTHING;

COMMIT;

SELECT 'Migration 32: expression index idx_properties_upper_civic_street created' AS status;

-- ============================================================================
-- MIGRATION 33: Widen properties.civic_no_suffix to VARCHAR(50)
-- ============================================================================

BEGIN;

-- Purpose: formalizes pre-existing drift between migration 18 (which defined
-- civic_no_suffix as VARCHAR(10)) and the live database (VARCHAR(50)). The
-- live column was widened at some point without a tracked migration; the
-- fresh-install schema.sql regenerated from the live DB on 2026-07-31
-- captures VARCHAR(50), and this migration brings migrations.sql in line so a
-- fresh install and a migrated-up existing install produce the same shape.
-- Idempotent: ALTER COLUMN ... TYPE is a no-op if the column is already
-- VARCHAR(50) or wider (Postgres will error only if narrowing would lose
-- data, which does not apply here).
-- Note: header must be exactly "rule / MIGRATION N: title / rule / blank /
-- BEGIN;" or run_migrations.py cannot parse this block (see migration 25 note).

ALTER TABLE properties ALTER COLUMN civic_no_suffix TYPE VARCHAR(50);

INSERT INTO schema_migrations (migration_number, migration_name)
VALUES (33, 'Widen properties.civic_no_suffix to VARCHAR(50)')
ON CONFLICT (migration_number) DO NOTHING;

COMMIT;

SELECT 'Migration 33: properties.civic_no_suffix widened to VARCHAR(50)' AS status;

-- ============================================================================
-- MIGRATION 34: Preserve upstream unit labels without truncation
-- ============================================================================

BEGIN;

-- NAR has address-like unit labels longer than 20 characters. Preserve them
-- verbatim instead of aborting a whole CSV part or silently truncating data.
ALTER TABLE nar_addresses ALTER COLUMN apt_no_label TYPE TEXT;
ALTER TABLE properties ALTER COLUMN unit_no TYPE TEXT;

COMMIT;

-- ============================================================================
-- MIGRATION 35: Repair confirmed source endpoints and boundary formats
-- ============================================================================

BEGIN;

UPDATE data_sources SET
    api_endpoint = 'https://services.arcgis.com/vhh9rYcMiBEBPGYb/arcgis/rest/services/MiddlesexCountyAddressPoints/FeatureServer/0',
    api_type = 'ArcGIS/FeatureServer', dataset_mapping_key = 'middlesex_county',
    multi_endpoint_config = NULL,
    notes = 'County-published consolidated address layer, August 2022 snapshot. Municipality resolved from official boundaries. ArcGIS item 928ed74edad64e4aa65170fb49c46476.'
WHERE source_key = 'middlesex_county_addresses';
UPDATE data_sources SET
    api_endpoint = 'https://geo.statcan.gc.ca/geo_wa/rest/services/2021/Digital_boundary_files/MapServer/14',
    api_type = 'ArcGIS/MapServer', download_format = 'geojson',
    local_file_path = 'data_sourcing/data_sources/fsa_boundaries/fsa_boundaries.geojson'
WHERE source_key = 'fsa_boundaries';
UPDATE data_sources SET
    api_endpoint = 'https://ws.lioservices.lrc.gov.on.ca/arcgis2/rest/services/LIO_OPEN_DATA/LIO_Open03/MapServer/14',
    api_type = 'ArcGIS/MapServer', download_format = 'geojson',
    local_file_path = 'data_sourcing/data_sources/ontario_municipal_boundaries/ontario_municipal_boundaries.geojson'
WHERE source_key = 'ontario_municipal_boundaries';

COMMIT;

-- ============================================================================
-- END OF MIGRATIONS
-- ============================================================================
