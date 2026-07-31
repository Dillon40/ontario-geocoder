-- Seed data for data_sources table
-- Run after migrations.sql to populate known data sources
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
) ON CONFLICT (source_key) DO UPDATE SET
    source_name = EXCLUDED.source_name,
    portal_url = EXCLUDED.portal_url,
    requires_manual_download = EXCLUDED.requires_manual_download,
    manual_download_instructions = EXCLUDED.manual_download_instructions,
    required_attribution = EXCLUDED.required_attribution,
    updated_at = NOW();

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
) ON CONFLICT (source_key) DO UPDATE SET
    source_name = EXCLUDED.source_name,
    portal_url = EXCLUDED.portal_url,
    api_endpoint = EXCLUDED.api_endpoint,
    required_attribution = EXCLUDED.required_attribution,
    updated_at = NOW();

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
) ON CONFLICT (source_key) DO UPDATE SET
    source_name = EXCLUDED.source_name,
    portal_url = EXCLUDED.portal_url,
    api_endpoint = EXCLUDED.api_endpoint,
    required_attribution = EXCLUDED.required_attribution,
    updated_at = NOW();

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
) ON CONFLICT (source_key) DO UPDATE SET
    source_name = EXCLUDED.source_name,
    portal_url = EXCLUDED.portal_url,
    api_endpoint = EXCLUDED.api_endpoint,
    required_attribution = EXCLUDED.required_attribution,
    updated_at = NOW();

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
) ON CONFLICT (source_key) DO UPDATE SET
    source_name = EXCLUDED.source_name,
    portal_url = EXCLUDED.portal_url,
    api_endpoint = EXCLUDED.api_endpoint,
    status = EXCLUDED.status,
    license_notes = EXCLUDED.license_notes,
    updated_at = NOW();

-- ============================================================================
-- BOUNDARY FILES (status = 'confirmed')
-- ============================================================================

-- FSA Boundaries (Statistics Canada)
INSERT INTO data_sources (
    source_key, source_name, status,
    regions_covered, region_type,
    portal_url, download_url, api_type,
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
    'direct_download',
    'Statistics Canada Open Licence',
    'https://www.statcan.gc.ca/en/reference/licence',
    TRUE,
    'Contains information licensed under the Open Government Licence – Canada.',
    'shp',
    'import_fsa_boundaries.py',
    'data_sourcing/data_sources/fsa_boundaries/',
    FALSE,
    'Forward Sortation Area (FSA) boundary polygons for all of Canada. Used for postal code geographic filtering.'
) ON CONFLICT (source_key) DO UPDATE SET
    source_name = EXCLUDED.source_name,
    download_url = EXCLUDED.download_url,
    required_attribution = EXCLUDED.required_attribution,
    updated_at = NOW();

-- Ontario Municipal Boundaries
INSERT INTO data_sources (
    source_key, source_name, status,
    regions_covered, region_type,
    portal_url, download_url, api_type,
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
    'direct_download',
    'Open Government Licence - Ontario',
    'https://www.ontario.ca/page/open-government-licence-ontario',
    TRUE,
    'Contains information licensed under the Open Government Licence – Ontario.',
    'shp',
    'import_municipal_boundaries.py',
    'data_sourcing/data_sources/ontario_municipal_boundaries/',
    FALSE,
    'Ontario lower-tier and single-tier municipal boundaries. Used for FSA-to-municipality mapping.'
) ON CONFLICT (source_key) DO UPDATE SET
    source_name = EXCLUDED.source_name,
    download_url = EXCLUDED.download_url,
    required_attribution = EXCLUDED.required_attribution,
    updated_at = NOW();

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
) ON CONFLICT (source_key) DO UPDATE SET
    source_name = EXCLUDED.source_name,
    portal_url = EXCLUDED.portal_url,
    api_endpoint = EXCLUDED.api_endpoint,
    download_config = EXCLUDED.download_config,
    rate_limit_config = EXCLUDED.rate_limit_config,
    required_attribution = EXCLUDED.required_attribution,
    updated_at = NOW();

-- Middlesex County Addresses (multi-endpoint aggregation)
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
    'confirmed',
    ARRAY['Middlesex Centre', 'Strathroy-Caradoc', 'Thames Centre', 'Adelaide Metcalfe', 'Lucan Biddulph', 'North Middlesex', 'Southwest Middlesex', 'Newbury'],
    'county',
    ARRAY['N0L', 'N0M', 'N6G', 'N6H', 'N6J', 'N6K', 'N6L', 'N6M', 'N6N', 'N6P'],
    'https://data-middlesex.opendata.arcgis.com/',
    NULL,  -- Uses multi_endpoint_config instead
    'ArcGIS/Hub',
    'Open Government Licence',
    'https://data-middlesex.opendata.arcgis.com/pages/licence',
    TRUE,
    'Contains information licensed under the Open Government Licence – Middlesex County.',
    'geojson',
    'import_production_data.py',
    'middlesex_county',
    'data_sourcing/data_sources/middlesex_county_addresses/middlesex_county_addresses.geojson',
    FALSE,
    '{
        "hub_base": "https://data-middlesex.opendata.arcgis.com",
        "try_hub_first": true,
        "endpoints": [
            {"name": "Newbury", "item_id": "d0a1517560ac43b49aff55c6aebb1747", "service_path": "Public/NEWB_Address", "is_condo": false},
            {"name": "Strathroy-Caradoc", "item_id": "6d3553e73cea44929846aea1f941ff7e", "service_path": "Public/SC_Address", "is_condo": false},
            {"name": "Middlesex Centre", "item_id": "5351a19fc0104bc28fa8b30e8c2bd6c8", "service_path": "Public/MIDC_Address", "is_condo": false},
            {"name": "Adelaide Metcalfe", "item_id": "7e38a49a41ea46f5a90600f1e29100a6", "service_path": "Public/AM_Address2", "is_condo": false},
            {"name": "Thames Centre", "item_id": "35529f629d4d4353b5a2994852eb2d1d", "service_path": "Public/TC_Address", "is_condo": false},
            {"name": "Lucan Biddulph", "item_id": "6401b3e7ef5a4c95a678ec44b15981be", "service_path": "Public/LB_Address", "is_condo": false},
            {"name": "North Middlesex", "item_id": "c515dd6ff8f74853a0b892a55072b559", "service_path": "Public/NMID_Address", "is_condo": false},
            {"name": "Southwest Middlesex", "item_id": "842c0b17fc934b02a9e17cd4647b78a8", "service_path": "Public/SWM_Address", "is_condo": false},
            {"name": "Lucan Biddulph Units", "item_id": "90377eb1148e4014a356ebd7485910bf", "service_path": "Public/LB_Address_Unit", "is_condo": true},
            {"name": "North Middlesex Units", "item_id": "eb3f3b21976348d4b345738fbc9e6db3", "service_path": "Public/NMID_Address_Unit", "is_condo": true},
            {"name": "Strathroy-Caradoc Units", "item_id": "7112d2f76fe24aeaabab01efff1218d9", "service_path": "Public/SC_Address_Unit", "is_condo": true},
            {"name": "Middlesex Centre Units", "item_id": "72d8bee41c724bf7ad4ae21e5536a1ab", "service_path": "Public/MIDC_Address_Unit", "is_condo": true},
            {"name": "Southwest Middlesex Units", "item_id": "7cc9ab7aec5e489a8ee69e28dbae29af", "service_path": "Public/SWM_Address_Unit", "is_condo": true},
            {"name": "Thames Centre Units", "item_id": "f58216e394f84b8887acb38596590f5f", "service_path": "Public/TC_Address_Unit", "is_condo": true}
        ]
    }'::jsonb,
    'Middlesex County Open Data - Aggregates 14 separate municipal endpoints (8 main address + 6 condo unit datasets). Uses Hub download API first, falls back to FeatureServer.'
) ON CONFLICT (source_key) DO UPDATE SET
    source_name = EXCLUDED.source_name,
    portal_url = EXCLUDED.portal_url,
    multi_endpoint_config = EXCLUDED.multi_endpoint_config,
    required_attribution = EXCLUDED.required_attribution,
    updated_at = NOW();

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
