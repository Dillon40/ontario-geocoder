-- Seed data for data_sources table
-- Run after migrations.sql to populate known data sources.
-- Existing base rows are insert-only; explicit exclusions below remain mandatory.
-- Usage: PGPASSWORD=$DB_PASSWORD psql -h localhost -U geocoder -d geocoder -f seed_data_sources.sql

BEGIN;

-- ============================================================================
-- CONFIRMED DATA SOURCES (status = 'confirmed')
-- ============================================================================

-- National Address Register (NAR) - Statistics Canada
INSERT INTO data_sources (
    source_key, source_name, status,
    regions_covered, region_type, postal_prefixes,
    portal_url, api_endpoint, api_type,
    license_type, license_url, commercial_use_allowed, required_attribution,
    download_format, import_script, dataset_mapping_key,
    local_file_path, requires_manual_download, manual_download_instructions,
    notes
) VALUES (
    'nar_ontario',
    'Statistics Canada - National Address Register (Ontario)',
    'confirmed',
    ARRAY['Ontario'],
    'province',
    NULL,  -- NAR covers all Ontario postal codes
    'https://www150.statcan.gc.ca/n1/en/catalogue/46200001',
    NULL,  -- URL dynamically scraped from StatsCan page by downloader
    NULL,
    'Statistics Canada Open Licence',
    'https://www.statcan.gc.ca/en/reference/licence',
    TRUE,
    'Contains information licensed under the Open Government Licence – Canada.',
    'csv',
    'import_nar_data.py',
    NULL,  -- NAR uses its own schema, not dataset-mappings.json
    'data_sourcing/data_sources/nar_ontario/',
    FALSE,  -- Automated download via get_latest_nar_url() in download_data_sources.py
    NULL,   -- No manual instructions needed - download is automated
    'National Address Register contains ~15M addresses for Canada. Ontario (province code 35) has ~5M addresses. Download URL is dynamically scraped from StatsCan page to get the latest version.'
) ON CONFLICT (source_key) DO NOTHING;

-- Toronto Address Points
INSERT INTO data_sources (
    source_key, source_name, status,
    regions_covered, region_type, postal_prefixes,
    portal_url, api_endpoint, api_type,
    license_type, license_url, commercial_use_allowed, required_attribution,
    download_format, import_script, dataset_mapping_key,
    local_file_path, requires_manual_download,
    notes
) VALUES (
    'toronto_addresses',
    'City of Toronto - Address Points (MTOR)',
    'confirmed',
    ARRAY['Toronto', 'East York', 'North York', 'Scarborough', 'York', 'Etobicoke'],
    'city',
    ARRAY['M1A', 'M1B', 'M1C', 'M1E', 'M1G', 'M1H', 'M1J', 'M1K', 'M1L', 'M1M', 'M1N', 'M1P', 'M1R', 'M1S', 'M1T', 'M1V', 'M1W', 'M1X', 'M2H', 'M2J', 'M2K', 'M2L', 'M2M', 'M2N', 'M2P', 'M2R', 'M3A', 'M3B', 'M3C', 'M3H', 'M3J', 'M3K', 'M3L', 'M3M', 'M3N', 'M4A', 'M4B', 'M4C', 'M4E', 'M4G', 'M4H', 'M4J', 'M4K', 'M4L', 'M4M', 'M4N', 'M4P', 'M4R', 'M4S', 'M4T', 'M4V', 'M4W', 'M4X', 'M4Y', 'M5A', 'M5B', 'M5C', 'M5E', 'M5G', 'M5H', 'M5J', 'M5K', 'M5L', 'M5M', 'M5N', 'M5P', 'M5R', 'M5S', 'M5T', 'M5V', 'M5W', 'M5X', 'M6A', 'M6B', 'M6C', 'M6E', 'M6G', 'M6H', 'M6J', 'M6K', 'M6L', 'M6M', 'M6N', 'M6P', 'M6R', 'M6S', 'M8V', 'M8W', 'M8X', 'M8Y', 'M8Z', 'M9A', 'M9B', 'M9C', 'M9L', 'M9M', 'M9N', 'M9P', 'M9R', 'M9V', 'M9W'],
    'https://open.toronto.ca/dataset/address-points-municipal-toronto-one-address-repository/',
    'https://ckan0.cf.opendata.inter.prod-toronto.ca/api/3/action/package_show?id=address-points-municipal-toronto-one-address-repository',
    'CKAN',
    'Open Government Licence - Toronto',
    'https://open.toronto.ca/open-data-licence/',
    TRUE,
    'Contains information licensed under the Open Government Licence – Toronto.',
    'geojson',
    'import_production_data.py',
    'toronto',
    'data_sourcing/data_sources/toronto_addresses/toronto_addresses.geojson',
    FALSE,
    'Toronto Open Data - Municipal Toronto One Address Repository (MTOR). ~800K address points. Explicit commercial use allowed.'
) ON CONFLICT (source_key) DO NOTHING;

-- Guelph Address Points
INSERT INTO data_sources (
    source_key, source_name, status,
    regions_covered, region_type, postal_prefixes,
    portal_url, api_endpoint, api_type,
    license_type, license_url, commercial_use_allowed, required_attribution,
    download_format, import_script, dataset_mapping_key,
    local_file_path, requires_manual_download,
    notes
) VALUES (
    'guelph_addresses',
    'City of Guelph - Address Points',
    'confirmed',
    ARRAY['Guelph'],
    'city',
    ARRAY['N1E', 'N1G', 'N1H', 'N1K', 'N1L'],
    'https://data.open.guelph.ca/',
    'https://maps.guelph.ca/arcgis/rest/services/OpenData/MapServer/12',
    'ArcGIS/FeatureServer',
    'Open Government Licence - Guelph',
    'https://data.open.guelph.ca/pages/open-government-licence',
    TRUE,
    'Contains information licensed under the Open Government Licence – Guelph.',
    'geojson',
    'import_production_data.py',
    'guelph',
    'data_sourcing/data_sources/guelph_addresses/guelph_addresses.geojson',
    FALSE,
    'City of Guelph Open Data - Address points with coordinates, postal codes, and property IDs. ~50K addresses.'
) ON CONFLICT (source_key) DO NOTHING;

-- Ottawa Address Points
INSERT INTO data_sources (
    source_key, source_name, status,
    regions_covered, region_type, postal_prefixes,
    portal_url, api_endpoint, api_type,
    license_type, license_url, commercial_use_allowed, required_attribution,
    download_format, import_script, dataset_mapping_key,
    local_file_path, requires_manual_download,
    notes
) VALUES (
    'ottawa_addresses',
    'City of Ottawa - Address Points',
    'confirmed',
    ARRAY['Ottawa', 'Nepean', 'Kanata', 'Orleans', 'Gloucester', 'Vanier'],
    'city',
    ARRAY['K1A', 'K1B', 'K1C', 'K1E', 'K1G', 'K1H', 'K1J', 'K1K', 'K1L', 'K1M', 'K1N', 'K1P', 'K1R', 'K1S', 'K1T', 'K1V', 'K1W', 'K1X', 'K1Y', 'K1Z', 'K2A', 'K2B', 'K2C', 'K2E', 'K2G', 'K2H', 'K2J', 'K2K', 'K2L', 'K2M', 'K2P', 'K2R', 'K2S', 'K2T', 'K2V', 'K2W', 'K4A', 'K4B', 'K4C', 'K4K', 'K4M', 'K4P', 'K4R'],
    'https://open.ottawa.ca/',
    'https://maps.ottawa.ca/arcgis/rest/services/Address_Points/MapServer/0',
    'ArcGIS/FeatureServer',
    'Open Government Licence - Ottawa',
    'https://ottawa.ca/en/city-hall/open-transparent-and-accountable-government/open-data',
    TRUE,
    'Contains information licensed under the Open Government Licence – Ottawa.',
    'geojson',
    'import_production_data.py',
    'ottawa',
    'data_sourcing/data_sources/ottawa_addresses/ottawa_addresses.geojson',
    FALSE,
    'City of Ottawa Open Data - Address points. ~350K addresses.'
) ON CONFLICT (source_key) DO NOTHING;

-- Bruce County Addresses
INSERT INTO data_sources (
    source_key, source_name, status,
    regions_covered, region_type, postal_prefixes,
    portal_url, api_endpoint, api_type,
    license_type, license_url, commercial_use_allowed, required_attribution,
    download_format, import_script, dataset_mapping_key,
    local_file_path, requires_manual_download,
    license_notes, notes
) VALUES (
    'bruce_county_addresses',
    'Bruce County - Address Points',
    'blocked',
    ARRAY['Bruce County', 'Kincardine', 'Saugeen Shores', 'South Bruce', 'South Bruce Peninsula', 'Arran-Elderslie', 'Brockton', 'Huron-Kinloss', 'Northern Bruce Peninsula'],
    'county',
    ARRAY['N0G', 'N0H'],
    'https://bruce-county-open-spatial-data-brucecounty.hub.arcgis.com/',
    'https://maps.brucecounty.on.ca/arcgisserver/rest/services/public/bruce_open_data/MapServer/3',
    'ArcGIS/MapServer',
    'Bruce County Open Data Licence',
    'https://bruce-county-open-spatial-data-brucecounty.hub.arcgis.com/',
    NULL,  -- Commercial use not explicitly stated, contact gis@brucecounty.on.ca
    NULL,  -- No required_attribution wording: licence terms unconfirmed (see license_notes) and
           -- status is 'blocked', so no attribution statement can be mandated yet.
    'geojson',
    'import_production_data.py',
    'bruce_county',
    'data_sourcing/data_sources/bruce_county_addresses/bruce_county_addresses.geojson',
    FALSE,
    'License not yet confirmed - needs verification before commercial use',
    'Bruce County address points. Commercial use not explicitly stated - contact gis@brucecounty.on.ca for clarification.'
) ON CONFLICT (source_key) DO NOTHING;

-- ============================================================================
-- BOUNDARY FILES (status = 'confirmed')
-- ============================================================================

-- FSA Boundaries (Statistics Canada)
INSERT INTO data_sources (
    source_key, source_name, status,
    regions_covered, region_type,
    portal_url, download_url, api_endpoint, api_type,
    license_type, license_url, commercial_use_allowed, required_attribution,
    download_format, import_script,
    local_file_path, requires_manual_download,
    notes
) VALUES (
    'fsa_boundaries',
    'Statistics Canada - Forward Sortation Area Boundaries',
    'confirmed',
    ARRAY['Canada'],
    'country',
    'https://www12.statcan.gc.ca/census-recensement/2021/geo/sip-pis/boundary-limites/index2021-eng.cfm',
    'https://www12.statcan.gc.ca/census-recensement/2021/geo/sip-pis/boundary-limites/files-fichiers/lfsa000a21a_e.zip',
    'https://geo.statcan.gc.ca/geo_wa/rest/services/2021/Digital_boundary_files/MapServer/14',
    'ArcGIS/MapServer',
    'Statistics Canada Open Licence',
    'https://www.statcan.gc.ca/en/reference/licence',
    TRUE,
    'Contains information licensed under the Open Government Licence – Canada.',
    'geojson',
    'import_fsa_boundaries.py',
    'data_sourcing/data_sources/fsa_boundaries/fsa_boundaries.geojson',
    FALSE,
    'Forward Sortation Area (FSA) boundary polygons for all of Canada. Used for postal code geographic filtering.'
) ON CONFLICT (source_key) DO NOTHING;

-- Ontario Municipal Boundaries
INSERT INTO data_sources (
    source_key, source_name, status,
    regions_covered, region_type,
    portal_url, download_url, api_endpoint, api_type,
    license_type, license_url, commercial_use_allowed, required_attribution,
    download_format, import_script,
    local_file_path, requires_manual_download,
    notes
) VALUES (
    'ontario_municipal_boundaries',
    'Ontario GeoHub - Lower and Single Tier Municipal Boundaries',
    'confirmed',
    ARRAY['Ontario'],
    'province',
    'https://geohub.lio.gov.on.ca/datasets/ontario::lower-and-single-tier-municipality/about',
    'https://www.arcgis.com/sharing/rest/content/items/41c9ee71e7f34c5dbf6e9b5c5ff85d32/data',
    'https://ws.lioservices.lrc.gov.on.ca/arcgis2/rest/services/LIO_OPEN_DATA/LIO_Open03/MapServer/14',
    'ArcGIS/MapServer',
    'Open Government Licence - Ontario',
    'https://www.ontario.ca/page/open-government-licence-ontario',
    TRUE,
    'Contains information licensed under the Open Government Licence – Ontario.',
    'geojson',
    'import_municipal_boundaries.py',
    'data_sourcing/data_sources/ontario_municipal_boundaries/ontario_municipal_boundaries.geojson',
    FALSE,
    'Ontario lower-tier and single-tier municipal boundaries. Used for FSA-to-municipality mapping.'
) ON CONFLICT (source_key) DO NOTHING;

-- ============================================================================
-- LINK DATA SOURCES TO DATA TYPES
-- ============================================================================

-- Helper function to link source to type
DO $$
DECLARE
    v_source_id INTEGER;
    v_type_id INTEGER;
BEGIN
    -- NAR -> addresses
    SELECT id INTO v_source_id FROM data_sources WHERE source_key = 'nar_ontario';
    SELECT id INTO v_type_id FROM data_types WHERE type_key = 'addresses';
    IF v_source_id IS NOT NULL AND v_type_id IS NOT NULL THEN
        INSERT INTO data_source_types (data_source_id, data_type_id)
        VALUES (v_source_id, v_type_id)
        ON CONFLICT DO NOTHING;
    END IF;

    -- Toronto -> addresses
    SELECT id INTO v_source_id FROM data_sources WHERE source_key = 'toronto_addresses';
    SELECT id INTO v_type_id FROM data_types WHERE type_key = 'addresses';
    IF v_source_id IS NOT NULL AND v_type_id IS NOT NULL THEN
        INSERT INTO data_source_types (data_source_id, data_type_id)
        VALUES (v_source_id, v_type_id)
        ON CONFLICT DO NOTHING;
    END IF;

    -- Guelph -> addresses
    SELECT id INTO v_source_id FROM data_sources WHERE source_key = 'guelph_addresses';
    SELECT id INTO v_type_id FROM data_types WHERE type_key = 'addresses';
    IF v_source_id IS NOT NULL AND v_type_id IS NOT NULL THEN
        INSERT INTO data_source_types (data_source_id, data_type_id)
        VALUES (v_source_id, v_type_id)
        ON CONFLICT DO NOTHING;
    END IF;

    -- Ottawa -> addresses
    SELECT id INTO v_source_id FROM data_sources WHERE source_key = 'ottawa_addresses';
    SELECT id INTO v_type_id FROM data_types WHERE type_key = 'addresses';
    IF v_source_id IS NOT NULL AND v_type_id IS NOT NULL THEN
        INSERT INTO data_source_types (data_source_id, data_type_id)
        VALUES (v_source_id, v_type_id)
        ON CONFLICT DO NOTHING;
    END IF;

    -- Bruce County -> addresses
    SELECT id INTO v_source_id FROM data_sources WHERE source_key = 'bruce_county_addresses';
    SELECT id INTO v_type_id FROM data_types WHERE type_key = 'addresses';
    IF v_source_id IS NOT NULL AND v_type_id IS NOT NULL THEN
        INSERT INTO data_source_types (data_source_id, data_type_id)
        VALUES (v_source_id, v_type_id)
        ON CONFLICT DO NOTHING;
    END IF;

    -- FSA -> postal_codes
    SELECT id INTO v_source_id FROM data_sources WHERE source_key = 'fsa_boundaries';
    SELECT id INTO v_type_id FROM data_types WHERE type_key = 'postal_codes';
    IF v_source_id IS NOT NULL AND v_type_id IS NOT NULL THEN
        INSERT INTO data_source_types (data_source_id, data_type_id)
        VALUES (v_source_id, v_type_id)
        ON CONFLICT DO NOTHING;
    END IF;

    -- Municipal boundaries -> boundaries
    SELECT id INTO v_source_id FROM data_sources WHERE source_key = 'ontario_municipal_boundaries';
    SELECT id INTO v_type_id FROM data_types WHERE type_key = 'boundaries';
    IF v_source_id IS NOT NULL AND v_type_id IS NOT NULL THEN
        INSERT INTO data_source_types (data_source_id, data_type_id)
        VALUES (v_source_id, v_type_id)
        ON CONFLICT DO NOTHING;
    END IF;
END $$;

-- ============================================================================
-- ADDITIONAL CONFIRMED SOURCES WITH DOWNLOAD CONFIG
-- ============================================================================

-- Hamilton Addresses (known rate-limited server)
INSERT INTO data_sources (
    source_key, source_name, status,
    regions_covered, region_type, postal_prefixes,
    portal_url, api_endpoint, api_type,
    license_type, license_url, commercial_use_allowed, required_attribution,
    download_format, import_script, dataset_mapping_key,
    local_file_path, requires_manual_download,
    download_config, rate_limit_config,
    notes
) VALUES (
    'hamilton_addresses',
    'City of Hamilton - Address Points',
    'confirmed',
    ARRAY['Hamilton', 'Ancaster', 'Dundas', 'Flamborough', 'Glanbrook', 'Stoney Creek'],
    'city',
    ARRAY['L0R', 'L8E', 'L8G', 'L8H', 'L8J', 'L8K', 'L8L', 'L8M', 'L8N', 'L8P', 'L8R', 'L8S', 'L8T', 'L8V', 'L8W', 'L9A', 'L9B', 'L9C', 'L9G', 'L9H', 'L9K'],
    'https://open.hamilton.ca/',
    'https://services.arcgis.com/rYz782eMbySr2srL/arcgis/rest/services/Addresses/FeatureServer/1',
    'ArcGIS/FeatureServer',
    'Open Government Licence - Hamilton',
    'https://www.hamilton.ca/open-data-terms-use',
    TRUE,
    'Contains information licensed under the Open Government Licence – Hamilton.',
    'geojson',
    'import_production_data.py',
    'hamilton',
    'data_sourcing/data_sources/hamilton_addresses/hamilton_addresses.geojson',
    FALSE,
    '{"page_size": 500, "request_delay_seconds": 6.0, "max_retries": 10}'::jsonb,
    '{"strategy": "fixed", "min_delay_seconds": 6.0, "backoff_base": 90, "backoff_multiplier": 1.5, "backoff_max": 600}'::jsonb,
    'Hamilton Open Data - ~270K address points. Server is rate-limited, requires 6-second delays between requests and careful backoff handling.'
) ON CONFLICT (source_key) DO NOTHING;

-- Middlesex County Addresses (blocked; endpoint retained for provenance)
INSERT INTO data_sources (
    source_key, source_name, status,
    regions_covered, region_type, postal_prefixes,
    portal_url, api_endpoint, api_type,
    license_type, license_url, commercial_use_allowed, required_attribution,
    download_format, import_script, dataset_mapping_key,
    local_file_path, requires_manual_download,
    multi_endpoint_config,
    notes
) VALUES (
    'middlesex_county_addresses',
    'Middlesex County - Municipal Address Points',
    'blocked',
    ARRAY['Middlesex Centre', 'Strathroy-Caradoc', 'Thames Centre', 'Adelaide Metcalfe', 'Lucan Biddulph', 'North Middlesex', 'Southwest Middlesex', 'Newbury'],
    'county',
    ARRAY['N0L', 'N0M', 'N6G', 'N6H', 'N6J', 'N6K', 'N6L', 'N6M', 'N6N', 'N6P'],
    'https://data-middlesex.opendata.arcgis.com/',
    'https://services.arcgis.com/vhh9rYcMiBEBPGYb/arcgis/rest/services/MiddlesexCountyAddressPoints/FeatureServer/0',
    'ArcGIS/FeatureServer',
    'Custom Middlesex County terms (commercial permission unverified)',
    'https://www.arcgis.com/home/item.html?id=928ed74edad64e4aa65170fb49c46476',
    NULL,
    'Address data provided by the County of Middlesex (August 2022 snapshot). Review source terms before reuse.',
    'geojson',
    'import_production_data.py',
    'middlesex_county',
    'data_sourcing/data_sources/middlesex_county_addresses/middlesex_county_addresses.geojson',
    FALSE,
    NULL,
    'Excluded by operator: no verified permissive municipal licence. Use National Address Register fallback. Endpoints retained for research only.'
) ON CONFLICT (source_key) DO NOTHING;

-- Link new sources to data types
DO $$
DECLARE
    v_source_id INTEGER;
    v_type_id INTEGER;
BEGIN
    -- Hamilton -> addresses
    SELECT id INTO v_source_id FROM data_sources WHERE source_key = 'hamilton_addresses';
    SELECT id INTO v_type_id FROM data_types WHERE type_key = 'addresses';
    IF v_source_id IS NOT NULL AND v_type_id IS NOT NULL THEN
        INSERT INTO data_source_types (data_source_id, data_type_id)
        VALUES (v_source_id, v_type_id)
        ON CONFLICT DO NOTHING;
    END IF;

    -- Middlesex -> addresses
    SELECT id INTO v_source_id FROM data_sources WHERE source_key = 'middlesex_county_addresses';
    SELECT id INTO v_type_id FROM data_types WHERE type_key = 'addresses';
    IF v_source_id IS NOT NULL AND v_type_id IS NOT NULL THEN
        INSERT INTO data_source_types (data_source_id, data_type_id)
        VALUES (v_source_id, v_type_id)
        ON CONFLICT DO NOTHING;
    END IF;
END $$;

-- Preserve existing source metadata; numbered migrations handle upgrades.

-- BEGIN RESTORED MUNICIPAL SOURCES
-- Canonical keys/licences: data_sourcing/docs/confirmed_sources.md (2026-07-23).
-- Full endpoints: arcgis_downloader.STANDARD_SOURCES. Mapping aliases are explicit.
-- Do not overwrite operator settings, exclusions, or upstream/import audit state.
INSERT INTO data_sources (
    source_key, source_name, status, regions_covered, region_type, postal_prefixes,
    portal_url, api_endpoint, api_type, license_type, license_url,
    commercial_use_allowed, required_attribution, download_format, import_script,
    dataset_mapping_key, local_file_path, requires_manual_download, license_notes, notes
) VALUES
    -- barrie_addresses
    ('barrie_addresses', 'City of Barrie - Address Points', 'confirmed', ARRAY['Barrie']::text[], 'city', ARRAY[]::text[],
     'https://public-barrie.opendata.arcgis.com/', 'https://gispublic.barrie.ca/arcgis/rest/services/Open_Data/AddressVW/MapServer/0', 'ArcGIS/MapServer', 'City of Barrie Open Data License', 'https://www.barrie.ca/Online%20Services/PublishingImages/OpenData_Images/COB_DataLicense.pdf',
     TRUE, NULL, 'geojson', 'import_production_data.py', 'barrie_addresses', 'data_sourcing/data_sources/barrie_addresses/barrie_addresses.geojson', FALSE,
     'Commercial permission retained from the confirmed inventory. Attribution text pending per-licence review before commercial launch.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- brantford
    ('brantford', 'City of Brantford - Site Addresses', 'confirmed', ARRAY['Brantford']::text[], 'city', ARRAY['N3P', 'N3R', 'N3S', 'N3T', 'N3V']::text[],
     'https://data-brantford.opendata.arcgis.com/', 'https://services.arcgis.com/aji2lCmjXt0KpGzI/arcgis/rest/services/OP_Site_Addresses2/FeatureServer/0', 'ArcGIS/FeatureServer', 'Open Government License - Brantford', 'https://data-brantford.opendata.arcgis.com/pages/licence',
     TRUE, NULL, 'geojson', 'import_production_data.py', 'brantford_addresses', 'data_sourcing/data_sources/brantford/brantford.geojson', FALSE,
     'Commercial permission retained from the confirmed inventory. Attribution text pending per-licence review before commercial launch.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- burlington
    ('burlington', 'City of Burlington - Address Points', 'confirmed', ARRAY['Burlington']::text[], 'city', ARRAY['L7L', 'L7M', 'L7N', 'L7P', 'L7R', 'L7S', 'L7T']::text[],
     'https://www.burlington.ca/en/services-for-you/Open-Data.asp', 'https://mapping.burlington.ca/arcgisweb/rest/services/COB/AddressPoints/MapServer/0', 'ArcGIS/MapServer', 'City of Burlington Open Data Terms of Use', 'https://opendata.burlington.ca/opendata-terms-of-use/City%20of%20Burlington%20-%20Open%20Data%20Terms%20of%20Use.pdf',
     TRUE, NULL, 'geojson', 'import_production_data.py', 'burlington_addresses', 'data_sourcing/data_sources/burlington/burlington.geojson', FALSE,
     'Commercial permission retained from the confirmed inventory. Attribution text pending per-licence review before commercial launch.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- cornwall
    ('cornwall', 'City of Cornwall - Address Points', 'confirmed', ARRAY['Cornwall']::text[], 'city', ARRAY['K0C', 'K6H', 'K6J', 'K6K']::text[],
     'https://data-cornwallcity.opendata.arcgis.com/', 'https://services2.arcgis.com/tPtW2sjE3XOR63A7/arcgis/rest/services/AddressPoints/FeatureServer/34', 'ArcGIS/FeatureServer', 'Open Government License', 'https://www.cornwall.ca/en/play-here/resources/Maps/City-of-Cornwall-Open-Government-Licence.pdf',
     TRUE, NULL, 'geojson', 'import_production_data.py', 'cornwall', 'data_sourcing/data_sources/cornwall/cornwall.geojson', FALSE,
     'Commercial permission retained from the confirmed inventory. Attribution text pending per-licence review before commercial launch.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- kawartha_lakes_addresses
    ('kawartha_lakes_addresses', 'City of Kawartha Lakes Address Points', 'confirmed', ARRAY['Kawartha Lakes']::text[], 'single_tier', ARRAY[]::text[],
     'https://open-data-kawartha.hub.arcgis.com/', 'https://services3.arcgis.com/RQBDTPtsbT0jebs7/arcgis/rest/services/CKL_Address_Point_View/FeatureServer/0', 'ArcGIS/FeatureServer', 'Open Data', 'https://open-data-kawartha.hub.arcgis.com/pages/termsofservice',
     TRUE, NULL, 'geojson', 'import_production_data.py', 'kawartha_lakes_addresses', 'data_sourcing/data_sources/kawartha_lakes_addresses/kawartha_lakes_addresses.geojson', FALSE,
     'Commercial permission retained from the confirmed inventory. Attribution text pending per-licence review before commercial launch.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- kingston_addresses
    ('kingston_addresses', 'City of Kingston - Address Points', 'confirmed', ARRAY['Kingston']::text[], 'city', ARRAY['K0H', 'K7G', 'K7K', 'K7L', 'K7M', 'K7P']::text[],
     'https://maps-cityofkingston.hub.arcgis.com/', 'https://utility.arcgis.com/usrsvcs/servers/d76e1fe447e84cb1983c76e1cb9dadb4/rest/services/Planning/Civic_Address_Point_Public/FeatureServer/0', 'ArcGIS/FeatureServer', 'City of Kingston Open Data Licence', 'https://www.cityofkingston.ca/media/wtpgpkb0/gis_license_opendata.pdf',
     TRUE, NULL, 'geojson', 'import_production_data.py', 'kingston_addresses', 'data_sourcing/data_sources/kingston_addresses/kingston_addresses.geojson', FALSE,
     'Commercial permission retained from the confirmed inventory. Attribution text pending per-licence review before commercial launch.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- london_addresses
    ('london_addresses', 'City of London - Address Points', 'confirmed', ARRAY['London']::text[], 'city', ARRAY['N5V', 'N5W', 'N5X', 'N5Y', 'N5Z', 'N6A', 'N6B', 'N6C', 'N6E', 'N6G', 'N6H', 'N6J', 'N6K', 'N6L', 'N6M', 'N6N', 'N6P']::text[],
     'https://london.ca/living-london/maps', 'https://maps.london.ca/server/rest/services/OpenData/OpenData_Community/MapServer/0', 'ArcGIS/MapServer', 'Terms of Use – City of London Open Data', 'https://london.maps.arcgis.com/sharing/rest/content/items/e31458fd0c7e41dd9f93144a9550781d/data',
     TRUE, NULL, 'geojson', 'import_production_data.py', 'london_addresses', 'data_sourcing/data_sources/london_addresses/london_addresses.geojson', FALSE,
     'Lawful-purpose reuse permitted. City credit is optional, not mandatory. Redistribution must include the terms or their URL and bind recipients without further restrictions. Publisher PDF archived at docs/licences/london_open_data_terms.pdf.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- peterborough
    ('peterborough', 'City of Peterborough Addresses', 'confirmed', ARRAY['Peterborough']::text[], 'city', ARRAY['K9H', 'K9J', 'K9K', 'K9L']::text[],
     'https://data-ptbo.opendata.arcgis.com', 'https://services1.arcgis.com/JOTjMnANji9YTrZ2/arcgis/rest/services/Address/FeatureServer/0', 'ArcGIS/FeatureServer', 'Open Government License - Permissive', 'https://data-ptbo.opendata.arcgis.com/pages/terms-of-use',
     TRUE, NULL, 'geojson', 'import_production_data.py', 'peterborough', 'data_sourcing/data_sources/peterborough/peterborough.geojson', FALSE,
     'Commercial permission retained from the confirmed inventory. Attribution text pending per-licence review before commercial launch.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- sarnia
    ('sarnia', 'City of Sarnia - Address Points', 'confirmed', ARRAY['Sarnia']::text[], 'city', ARRAY[]::text[],
     'https://city-of-sarnia.hub.arcgis.com/', 'https://services1.arcgis.com/ICybsLmBXrZCZV3x/arcgis/rest/services/Addresses_Open_Data_AGOL/FeatureServer/0', 'ArcGIS/FeatureServer', 'Open Government Licence – City of Sarnia', 'https://city-of-sarnia.hub.arcgis.com/pages/terms-of-use',
     TRUE, 'Contains information licensed under the Open Government Licence – City of Sarnia', 'geojson', 'import_production_data.py', 'sarnia', 'data_sourcing/data_sources/sarnia/sarnia.geojson', FALSE,
     'Commercial reuse and default attribution verified from the official Hub page data archived at docs/licences/sarnia_open_data_terms.json.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- thunder_bay
    ('thunder_bay', 'City of Thunder Bay - Municipal Address Points', 'confirmed', ARRAY['Thunder Bay']::text[], 'city', ARRAY[]::text[],
     'https://www.arcgis.com/apps/webappviewer/index.html?id=30777acab44b4a9485833d175a254286', 'https://services5.arcgis.com/h9xShea49ZANgOtx/arcgis/rest/services/Municipal_Address_Feature_Layer/FeatureServer/0', 'ArcGIS/FeatureServer', 'Open Government License', 'https://www.thunderbay.ca/en/city-hall/open-data-licence.aspx',
     TRUE, NULL, 'geojson', 'import_production_data.py', 'thunder_bay', 'data_sourcing/data_sources/thunder_bay/thunder_bay.geojson', FALSE,
     'Commercial permission retained from the confirmed inventory. Attribution text pending per-licence review before commercial launch.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- windsor_addresses
    ('windsor_addresses', 'City of Windsor - Address Points', 'confirmed', ARRAY['Windsor']::text[], 'city', ARRAY['N8H', 'N8N', 'N8P', 'N8R', 'N8S', 'N8T', 'N8V', 'N8W', 'N8X', 'N8Y', 'N9A', 'N9B', 'N9C', 'N9E', 'N9G', 'N9H', 'N9J', 'N9K', 'N9V', 'N9Y']::text[],
     'https://opendata.citywindsor.ca/', 'https://mappmycity.ca/arcgis/rest/services/open_data_services/propertyparcels/MapServer/0', 'ArcGIS/MapServer', 'Open Government Licence – The Corporation of the City of Windsor', 'https://opendata.citywindsor.ca/Documents/OpenDataTermsofUse.pdf',
     TRUE, 'Contains information licensed under the Open Government Licence – The Corporation of the City of Windsor.', 'geojson', 'import_production_data.py', 'windsor_addresses', 'data_sourcing/data_sources/windsor_addresses/windsor_addresses.geojson', FALSE,
     'Commercial reuse and default attribution verified from the publisher PDF archived at docs/licences/windsor_open_data_terms.pdf.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- muskoka_addresses
    ('muskoka_addresses', 'District Municipality of Muskoka - Address Points', 'confirmed', ARRAY['District Municipality of Muskoka']::text[], 'region', ARRAY['P0B', 'P0C', 'P0E', 'P1H', 'P1L', 'P1P']::text[],
     'https://map.muskoka.on.ca/', 'https://utility.arcgis.com/usrsvcs/servers/7dbdeb9bc94b45d4a3296f73fba38a3c/rest/services/Addressing/Addressing_Service_Public/FeatureServer/0', 'ArcGIS/FeatureServer', 'Open Data License - Muskoka', 'https://map.muskoka.on.ca/pages/terms-of-use',
     TRUE, NULL, 'geojson', 'import_production_data.py', 'muskoka_addresses', 'data_sourcing/data_sources/muskoka_addresses/muskoka_addresses.geojson', FALSE,
     'Commercial permission retained from the confirmed inventory. Attribution text pending per-licence review before commercial launch.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- dufferin_county_addresses
    ('dufferin_county_addresses', 'Dufferin County - Address Points', 'confirmed', ARRAY['Dufferin County']::text[], 'county', ARRAY['L9V', 'L9W', 'N0C']::text[],
     'https://data-dufferincounty.opendata.arcgis.com/', 'https://services5.arcgis.com/C0BEdgagVaXzcw0W/arcgis/rest/services/Address_Points_DufferinCounty_Open_Data/FeatureServer/0', 'ArcGIS/FeatureServer', 'Open Government Licence - Dufferin County', 'https://www.dufferincounty.ca/wp-content/uploads/2024/03/Dufferin-Open-Government-License.pdf',
     TRUE, 'Contains information licensed under the Open Government Licence – Dufferin County.', 'geojson', 'import_production_data.py', 'dufferin_county_addresses', 'data_sourcing/data_sources/dufferin_county_addresses/dufferin_county_addresses.geojson', FALSE,
     'Commercial permission retained from the confirmed inventory.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- durham_region_addresses
    ('durham_region_addresses', 'Durham Region - Address Points', 'confirmed', ARRAY['Durham Region']::text[], 'region', ARRAY['L1B', 'L1C', 'L1E', 'L1G', 'L1H', 'L1J', 'L1K', 'L1L', 'L1M', 'L1N', 'L1P', 'L1R', 'L1S', 'L1T', 'L1V', 'L1W', 'L1X', 'L1Y', 'L1Z']::text[],
     'https://opendata.durham.ca/', 'https://maps.durham.ca/arcgis/rest/services/Open_Data/Durham_OpenData/MapServer/0', 'ArcGIS/MapServer', 'Region of Durham Open Data Licence v.1.0', 'https://www.durham.ca/en/regional-government/resources/Documents/OpenDataLicenceAgreement.pdf',
     TRUE, NULL, 'geojson', 'import_production_data.py', 'durham_region_addresses', 'data_sourcing/data_sources/durham_region_addresses/durham_region_addresses.geojson', FALSE,
     'Commercial permission retained from the confirmed inventory. Attribution text pending per-licence review before commercial launch.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- elgin_county_addresses
    ('elgin_county_addresses', 'Elgin County - Address Points', 'confirmed', ARRAY['Elgin County']::text[], 'county', ARRAY[]::text[],
     'https://geohub.elgin.ca', 'https://services6.arcgis.com/TCQ78XR5HqDZ1SDc/arcgis/rest/services/Elgin_Civic_Numbers_2026/FeatureServer/0', 'ArcGIS/FeatureServer', 'Open Government Licence – County of Elgin', 'https://elgin-county.maps.arcgis.com/sharing/rest/content/items/d4f8cfbd8aa64adaa3cd983abf258419/data',
     TRUE, 'Contains information licensed under the Open Government Licence – County of Elgin.', 'geojson', 'import_production_data.py', 'elgin_county_addresses', 'data_sourcing/data_sources/elgin_county_addresses/elgin_county_addresses.geojson', FALSE,
     'Commercial reuse and default attribution verified from the publisher PDF archived at docs/licences/elgin_open_data_terms.pdf (version 1.0, modified 2024-03-12).', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- essex_county_tecumseh
    ('essex_county_tecumseh', 'Essex County / Tecumseh', 'confirmed', ARRAY['Tecumseh']::text[], 'county', ARRAY['N8N', 'N0R']::text[],
     'https://opendata.countyofessex.ca/', 'https://gisweb.tecumseh.ca/arcgis/rest/services/OpenDataMaps/OpenDataMAP_InfraZonePropBound_Layers/MapServer/33', 'ArcGIS/MapServer', 'Open Government Licence', 'https://gisweb.countyofessex.ca/OpenData/County%20of%20Essex%20-%20Open%20Government%20Licence.pdf',
     TRUE, NULL, 'geojson', 'import_production_data.py', 'essex_county_addresses', 'data_sourcing/data_sources/essex_county_tecumseh/essex_county_tecumseh.geojson', FALSE,
     'Commercial permission retained from the confirmed inventory. Attribution text pending per-licence review before commercial launch.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import. Covers Tecumseh only, not all of Essex County.'),
    -- frontenac_county
    ('frontenac_county', 'Frontenac County', 'confirmed', ARRAY['Frontenac County']::text[], 'county', ARRAY[]::text[],
     'https://frontenacmaps.ca/', 'https://services2.arcgis.com/vJlmDE1PmdTcJF0N/arcgis/rest/services/Civic_Address_Points_View_2/FeatureServer/0', 'ArcGIS/FeatureServer', 'Frontenac Open Data Licence (Open Government License adapted)', 'https://frontenacmaps.ca',
     TRUE, NULL, 'geojson', 'import_production_data.py', 'frontenac_county', 'data_sourcing/data_sources/frontenac_county/frontenac_county.geojson', FALSE,
     'Commercial permission retained from the confirmed inventory. Attribution text pending per-licence review before commercial launch. Licence PDF pending archival under docs/licences/ before commercial launch (migration 28).', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- greater_sudbury
    ('greater_sudbury', 'Greater Sudbury', 'confirmed', ARRAY['Greater Sudbury']::text[], 'city', ARRAY[]::text[],
     'https://www.greatersudbury.ca/city-hall/open-government/open-data/', 'https://services.arcgis.com/q3mIlR87lZlZsds3/arcgis/rest/services/Address_and_Building_Roofline/FeatureServer/2', 'ArcGIS/FeatureServer', 'commercial_with_attribution', 'https://www.greatersudbury.ca/city-hall/open-government/open-data/licence/',
     TRUE, NULL, 'geojson', 'import_production_data.py', 'greater_sudbury', 'data_sourcing/data_sources/greater_sudbury/greater_sudbury.geojson', FALSE,
     'Commercial permission retained from the confirmed inventory. Attribution text pending per-licence review before commercial launch.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- haldimand_county_addresses
    ('haldimand_county_addresses', 'Haldimand County - Address Points', 'confirmed', ARRAY['Haldimand County']::text[], 'county', ARRAY['N0A', 'N3W']::text[],
     'https://opendata-haldimand.hub.arcgis.com/', 'https://gis.haldimandcounty.ca/server/rest/services/Topo/ParcelsOnlinePublic/FeatureServer/0', 'ArcGIS/FeatureServer', 'Open Government Licence - Haldimand County (Version 1.0)', 'https://opendata-haldimand.hub.arcgis.com/',
     TRUE, 'Contains information licensed under the Open Government Licence – Haldimand County.', 'geojson', 'import_production_data.py', 'haldimand_county', 'data_sourcing/data_sources/haldimand_county_addresses/haldimand_county_addresses.geojson', FALSE,
     'Commercial permission retained from the confirmed inventory.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- hastings_county
    ('hastings_county', 'Hastings County', 'confirmed', ARRAY['Hastings County']::text[], 'county', ARRAY[]::text[],
     'https://www.arcgis.com/home/item.html?id=d1edbea63ca24e5bbd2363c3015bfdb0', 'https://services1.arcgis.com/1xcPmb5WGexkkJ9w/arcgis/rest/services/CivicAdd_OpenData/FeatureServer/0', 'ArcGIS/FeatureServer', 'Open Government License', 'https://ago-item-storage.s3.amazonaws.com/e2f14950bfbd4c138da4b60c71f3a134/County_GIS_Open_Data_Initiative_Agreement_2017.pdf',
     TRUE, NULL, 'geojson', 'import_production_data.py', 'hastings_county', 'data_sourcing/data_sources/hastings_county/hastings_county.geojson', FALSE,
     'Commercial permission retained from the confirmed inventory. Attribution text pending per-licence review before commercial launch.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- huron_county
    ('huron_county', 'Huron County - Municipal 911 Addresses', 'confirmed', ARRAY['Huron County']::text[], 'county', ARRAY[]::text[],
     'https://data-huron.opendata.arcgis.com/', 'https://services1.arcgis.com/Nl1VlZHemqspPW1n/arcgis/rest/services/Municipal_911_Address/FeatureServer/0', 'ArcGIS/FeatureServer', 'Open Government License', 'https://data-huron.opendata.arcgis.com/datasets/55ae362c840344cdb3422d496db10d64_0/about',
     TRUE, NULL, 'geojson', 'import_production_data.py', 'huron_county', 'data_sourcing/data_sources/huron_county/huron_county.geojson', FALSE,
     'Commercial permission retained from the confirmed inventory. Attribution text pending per-licence review before commercial launch.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- leeds_and_grenville
    ('leeds_and_grenville', 'Leeds and Grenville', 'confirmed', ARRAY['United Counties of Leeds and Grenville']::text[], 'county', ARRAY['K0E', 'K0G', 'K7G', 'K7H', 'K7K']::text[],
     'https://leedsgrenville.com/', 'https://services3.arcgis.com/KQcdsE7S72bcD7R8/arcgis/rest/services/LG_CivicAddress_OpenData/FeatureServer/0', 'ArcGIS/FeatureServer', 'Open Government License (Adapted)', 'https://geohub-uclg.hub.arcgis.com/pages/terms-of-use',
     TRUE, NULL, 'geojson', 'import_production_data.py', 'leeds_grenville_addresses', 'data_sourcing/data_sources/leeds_and_grenville/leeds_and_grenville.geojson', FALSE,
     'Commercial permission retained from the confirmed inventory. Attribution text pending per-licence review before commercial launch.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- niagara_region_addresses
    ('niagara_region_addresses', 'Niagara Region - Address Points', 'confirmed', ARRAY['Niagara Region']::text[], 'region', ARRAY['L0R', 'L0S', 'L2A', 'L2E', 'L2G', 'L2H', 'L2J', 'L2M', 'L2N', 'L2P', 'L2R', 'L2S', 'L2T', 'L2V', 'L2W', 'L3B', 'L3C', 'L3K', 'L3M']::text[],
     'https://niagaraopendata.ca/', 'https://services1.arcgis.com/WxiLK82TWf8W3O3f/arcgis/rest/services/OpenData_Address_Points/FeatureServer/43', 'ArcGIS/FeatureServer', 'Open Government Licence 2.0 - Niagara Region', 'https://www.niagaraopendata.ca/dataset/address-points',
     TRUE, 'Contains information licensed under the Open Government Licence – Niagara Region.', 'geojson', 'import_production_data.py', 'niagara_region_addresses', 'data_sourcing/data_sources/niagara_region_addresses/niagara_region_addresses.geojson', FALSE,
     'Commercial permission retained from the confirmed inventory.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- norfolk_county
    ('norfolk_county', 'Norfolk County Civic Addresses', 'confirmed', ARRAY['Norfolk County']::text[], 'county', ARRAY['N0E', 'N0J', 'N3Y', 'N4S', 'N4G']::text[],
     'https://data-norfolk.opendata.arcgis.com', 'https://services1.arcgis.com/mSGV2hCLXHNsfQqK/arcgis/rest/services/Civic_Addresses/FeatureServer/0', 'ArcGIS/FeatureServer', 'Open Data License', 'https://data-norfolk.opendata.arcgis.com/pages/terms-of-use',
     TRUE, NULL, 'geojson', 'import_production_data.py', 'norfolk_county_addresses', 'data_sourcing/data_sources/norfolk_county/norfolk_county.geojson', FALSE,
     'Commercial permission retained from the confirmed inventory. Attribution text pending per-licence review before commercial launch.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- peel_region_addresses
    ('peel_region_addresses', 'Region of Peel - Address Points', 'confirmed', ARRAY['Peel Region']::text[], 'region', ARRAY['L4T', 'L4V', 'L4W', 'L4X', 'L4Y', 'L4Z', 'L5A', 'L5B', 'L5C', 'L5E', 'L5G', 'L5H', 'L5J', 'L5K', 'L5L', 'L5M', 'L5N', 'L5P', 'L5R', 'L5S', 'L5T', 'L5V', 'L5W', 'L6P', 'L6R', 'L6S', 'L6T', 'L6V', 'L6W', 'L6X', 'L6Y', 'L6Z', 'L7A', 'L7C', 'L7E', 'L7G']::text[],
     'https://data.peelregion.ca/', 'https://services6.arcgis.com/ONZht79c8QWuX759/arcgis/rest/services/Address_Points/FeatureServer/0', 'ArcGIS/FeatureServer', 'Open Data Licence for The Regional Municipality of Peel (Version 1.0)', 'https://data.peelregion.ca/',
     TRUE, NULL, 'geojson', 'import_production_data.py', 'peel_region_addresses', 'data_sourcing/data_sources/peel_region_addresses/peel_region_addresses.geojson', FALSE,
     'Commercial permission retained from the confirmed inventory. Attribution text pending per-licence review before commercial launch.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- waterloo_region_addresses
    ('waterloo_region_addresses', 'Region of Waterloo - Address Points', 'confirmed', ARRAY['Waterloo Region']::text[], 'region', ARRAY['N0B', 'N1R', 'N2A', 'N2B', 'N2C', 'N2E', 'N2G', 'N2H', 'N2J', 'N2K', 'N2L', 'N2M', 'N2N', 'N2P', 'N2R', 'N2T', 'N2V', 'N3A', 'N3B', 'N3C', 'N3E', 'N3H']::text[],
     'https://region-of-waterloo-geohub-rmw.hub.arcgis.com/', 'https://utility.arcgis.com/usrsvcs/servers/d4d0c6dedec14e59930688f3830776c6/rest/services/OpenData/OpenData/MapServer/1', 'ArcGIS/MapServer', 'Region of Waterloo Open Data Licence', 'https://www.regionofwaterloo.ca/en/regional-government/open-data.aspx#Open-Data-Licence',
     TRUE, NULL, 'geojson', 'import_production_data.py', 'waterloo_region_addresses', 'data_sourcing/data_sources/waterloo_region_addresses/waterloo_region_addresses.geojson', FALSE,
     'Commercial permission retained from the confirmed inventory. Attribution text pending per-licence review before commercial launch.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- milton
    ('milton', 'Town of Milton - Address Points', 'confirmed', ARRAY['Milton']::text[], 'city', ARRAY['L9E', 'L9T']::text[],
     'https://discover-milton.hub.arcgis.com/', 'https://api.milton.ca/arcgis/rest/services/Datasets/Address_Pts/MapServer/0', 'ArcGIS/MapServer', 'Open Government License - Milton', 'https://discover-milton.hub.arcgis.com/pages/disclaimer-and-terms-of-use',
     TRUE, NULL, 'geojson', 'import_production_data.py', 'milton_addresses', 'data_sourcing/data_sources/milton/milton.geojson', FALSE,
     'Commercial permission retained from the confirmed inventory. Attribution text pending per-licence review before commercial launch.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- oakville
    ('oakville', 'Town of Oakville - Address Points', 'confirmed', ARRAY['Oakville']::text[], 'city', ARRAY['L6H', 'L6J', 'L6K', 'L6L', 'L6M']::text[],
     'https://www.oakville.ca/business/gis-maps.html', 'https://maps.oakville.ca/oakgis/rest/services/ArcGISOnline/AGO_Oakville_Misc_Layers/MapServer/15', 'ArcGIS/MapServer', 'Open Government License - Oakville', 'https://www.oakville.ca/getmedia/3cb7902a-2220-433e-8aed-24f326b745b9/town-hall-oakville-open-data-licence.pdf',
     TRUE, NULL, 'geojson', 'import_production_data.py', 'oakville_addresses', 'data_sourcing/data_sources/oakville/oakville.geojson', FALSE,
     'Commercial permission retained from the confirmed inventory. Attribution text pending per-licence review before commercial launch.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- parry_sound_district_addresses
    ('parry_sound_district_addresses', 'West Parry Sound Geography Network - Address Points', 'confirmed', ARRAY['Parry Sound District']::text[], 'district', ARRAY['P0A', 'P0C', 'P0G', 'P2A']::text[],
     'https://www.wpsgn.ca/', 'https://www.wpsgn.ca/s22/rest/services/NG911/NG911/FeatureServer/9112', 'ArcGIS/FeatureServer', 'Open Government License - Township of The Archipelago', 'https://wpsgn.ca/OpenGovernmentLicense–TownshipofTheArchipelago.pdf',
     TRUE, NULL, 'geojson', 'import_production_data.py', 'parry_sound_district_addresses', 'data_sourcing/data_sources/parry_sound_district_addresses/parry_sound_district_addresses.geojson', FALSE,
     'Commercial permission retained from the confirmed inventory. Attribution text pending per-licence review before commercial launch.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.'),
    -- york_region_addresses
    ('york_region_addresses', 'York Region - Address Points', 'confirmed', ARRAY['York Region']::text[], 'region', ARRAY['L0G', 'L0J', 'L3P', 'L3R', 'L3S', 'L3T', 'L3X', 'L3Y', 'L3Z', 'L4A', 'L4B', 'L4C', 'L4E', 'L4G', 'L4H', 'L4J', 'L4K', 'L4L', 'L4S', 'L6A', 'L6B', 'L6C', 'L6E', 'L6G', 'L7B', 'L9N', 'L9Y']::text[],
     'https://insights-york.opendata.arcgis.com/', 'https://ww8.yorkmaps.ca/arcgis/rest/services/OpenData/Location/MapServer/0', 'ArcGIS/MapServer', 'York Region Open Data Licence', 'https://insights-york.opendata.arcgis.com/documents/york-region-open-data-licence/explore',
     TRUE, NULL, 'geojson', 'import_production_data.py', 'york_region_addresses', 'data_sourcing/data_sources/york_region_addresses/york_region_addresses.geojson', FALSE,
     'Commercial permission retained from the confirmed inventory. Attribution text pending per-licence review before commercial launch.', 'Restored from the confirmed commercial-use inventory and STANDARD_SOURCES; validate upstream schema before import.')
ON CONFLICT (source_key) DO NOTHING;

INSERT INTO data_source_types (data_source_id, data_type_id)
SELECT ds.id, dt.id
FROM data_sources ds CROSS JOIN data_types dt
WHERE dt.type_key = 'addresses' AND ds.source_key IN (
    'barrie_addresses',
    'brantford',
    'burlington',
    'cornwall',
    'kawartha_lakes_addresses',
    'kingston_addresses',
    'london_addresses',
    'peterborough',
    'sarnia',
    'thunder_bay',
    'windsor_addresses',
    'muskoka_addresses',
    'dufferin_county_addresses',
    'durham_region_addresses',
    'elgin_county_addresses',
    'essex_county_tecumseh',
    'frontenac_county',
    'greater_sudbury',
    'haldimand_county_addresses',
    'hastings_county',
    'huron_county',
    'leeds_and_grenville',
    'niagara_region_addresses',
    'norfolk_county',
    'peel_region_addresses',
    'waterloo_region_addresses',
    'milton',
    'oakville',
    'parry_sound_district_addresses',
    'york_region_addresses'
)
ON CONFLICT DO NOTHING;
-- END RESTORED MUNICIPAL SOURCES

-- BEGIN VERIFIED SOURCE CORRECTIONS
-- Also repair default rows from earlier registries. Preserve operator exclusions,
-- custom endpoints, custom licence fields, and all download/import audit state.
UPDATE data_sources AS ds SET
    api_endpoint = CASE WHEN ds.api_endpoint = fixed.old_endpoint
                        THEN fixed.endpoint ELSE ds.api_endpoint END,
    license_url = CASE WHEN ds.license_url = fixed.old_license
                       THEN fixed.license ELSE ds.license_url END,
    license_type = CASE WHEN ds.license_type = fixed.old_type
                        THEN fixed.licence_type ELSE ds.license_type END,
    required_attribution = CASE
        WHEN ds.required_attribution IS NOT DISTINCT FROM fixed.old_attribution
        THEN fixed.attribution ELSE ds.required_attribution END,
    license_notes = CASE WHEN ds.license_notes = fixed.old_notes
                         THEN fixed.notes ELSE ds.license_notes END
FROM (VALUES
    ('windsor_addresses', 'https://mappmycity.ca/arcgis/rest/services/OpenDataServices/PropertyParcels/MapServer/0', 'https://mappmycity.ca/arcgis/rest/services/open_data_services/propertyparcels/MapServer/0', 'https://opendata.citywindsor.ca/', 'https://opendata.citywindsor.ca/Documents/OpenDataTermsofUse.pdf', 'City of Windsor Open Data Terms of Use', 'Open Government Licence – The Corporation of the City of Windsor', NULL, 'Contains information licensed under the Open Government Licence – The Corporation of the City of Windsor.', 'Commercial permission retained from the confirmed inventory. Attribution text pending per-licence review before commercial launch.', 'Commercial reuse and default attribution verified from the publisher PDF archived at docs/licences/windsor_open_data_terms.pdf.'),
    ('sarnia', 'https://services1.arcgis.com/ICybsLmBXrZCZV3x/arcgis/rest/services/Addresses_Open_Data/FeatureServer/0', 'https://services1.arcgis.com/ICybsLmBXrZCZV3x/arcgis/rest/services/Addresses_Open_Data_AGOL/FeatureServer/0', 'https://city-of-sarnia.hub.arcgis.com/pages/terms-of-use', 'https://city-of-sarnia.hub.arcgis.com/pages/terms-of-use', 'Open Government License', 'Open Government Licence – City of Sarnia', NULL, 'Contains information licensed under the Open Government Licence – City of Sarnia', 'Commercial permission retained from the confirmed inventory. Attribution text pending per-licence review before commercial launch.', 'Commercial reuse and default attribution verified from the official Hub page data archived at docs/licences/sarnia_open_data_terms.json.'),
    ('london_addresses', 'https://maps.london.ca/arcgisa/rest/services/OpenData/OpenData_Community/MapServer/0', 'https://maps.london.ca/server/rest/services/OpenData/OpenData_Community/MapServer/0', 'https://london.ca/terms-of-use', 'https://london.maps.arcgis.com/sharing/rest/content/items/e31458fd0c7e41dd9f93144a9550781d/data', 'City of London Terms of Use', 'Terms of Use – City of London Open Data', NULL, NULL, 'Commercial permission retained from the confirmed inventory. Attribution text pending per-licence review before commercial launch.', 'Lawful-purpose reuse permitted. City credit is optional, not mandatory. Redistribution must include the terms or their URL and bind recipients without further restrictions. Publisher PDF archived at docs/licences/london_open_data_terms.pdf.'),
    ('elgin_county_addresses', 'https://services6.arcgis.com/TCQ78XR5HqDZ1SDc/arcgis/rest/services/Elgin_Civic_Numbers_2025/FeatureServer/0', 'https://services6.arcgis.com/TCQ78XR5HqDZ1SDc/arcgis/rest/services/Elgin_Civic_Numbers_2026/FeatureServer/0', 'https://geohub.elgin.ca', 'https://elgin-county.maps.arcgis.com/sharing/rest/content/items/d4f8cfbd8aa64adaa3cd983abf258419/data', 'Open Government Licence - Elgin County', 'Open Government Licence – County of Elgin', 'Contains information licensed under the Open Government Licence – Elgin County.', 'Contains information licensed under the Open Government Licence – County of Elgin.', 'Commercial permission retained from the confirmed inventory. Licence PDF pending archival under docs/licences/ before commercial launch (migration 28).', 'Commercial reuse and default attribution verified from the publisher PDF archived at docs/licences/elgin_open_data_terms.pdf (version 1.0, modified 2024-03-12).')
) AS fixed(source_key, old_endpoint, endpoint, old_license, license,
           old_type, licence_type, old_attribution, attribution, old_notes, notes)
WHERE ds.source_key = fixed.source_key AND (
    ds.api_endpoint = fixed.old_endpoint OR
    (ds.api_endpoint = fixed.endpoint AND ds.license_url = fixed.old_license
     AND fixed.old_license <> fixed.license)
);
-- END VERIFIED SOURCE CORRECTIONS

-- BEGIN HALDIMAND DOWNLOAD EXCLUSION
-- Parcel-derived coordinate handling is not approved. Retain the endpoint,
-- licence and mapping for future repair; normal ETL selects confirmed rows only.
UPDATE data_sources SET
    status = 'blocked',
    notes = 'Disabled by operator pending approved parcel-to-address coordinate handling. Do not download/import through normal ETL; retained endpoint serves parcel polygons, not verified address points.'
WHERE source_key = 'haldimand_county_addresses';
-- END HALDIMAND DOWNLOAD EXCLUSION

COMMIT;

-- ============================================================================
-- VERIFICATION
-- ============================================================================

SELECT 'Data sources seeded successfully' AS status;

SELECT
    source_key,
    source_name,
    status,
    CASE WHEN api_endpoint IS NOT NULL THEN 'API'
         WHEN download_url IS NOT NULL THEN 'Direct URL'
         WHEN requires_manual_download THEN 'Manual'
         ELSE 'Unknown' END AS download_method,
    import_script
FROM data_sources
ORDER BY source_key;

SELECT
    ds.source_key,
    dt.type_key AS data_type
FROM data_sources ds
JOIN data_source_types dst ON ds.id = dst.data_source_id
JOIN data_types dt ON dst.data_type_id = dt.id
ORDER BY ds.source_key;
