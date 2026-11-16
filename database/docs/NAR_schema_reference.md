Here are the column headers for your PostgreSQL NAR dataset tables:

nar_addresses table

1. id - integer (primary key)
2. loc_guid - uuid
3. addr_guid - uuid (unique)
4. civic_no - varchar(20)
5. civic_no_suffix - varchar(10)
6. apt_no_label - text (migration 34 preserves long upstream unit labels without truncation)
7. official_street_name - varchar(100)
8. official_street_type - varchar(20)
9. official_street_dir - varchar(20) (widened from varchar(10) by migration 26)
10. csd_eng_name - varchar(100) (Census Subdivision English name)
11. prov_code - integer
12. mail_postal_code - varchar(10)
13. bg_x - numeric (X coordinate)
14. bg_y - numeric (Y coordinate)
15. fsa_code - varchar(3)
16. enriched_from_source - varchar(100)
17. enrichment_confidence - numeric(4,3)
18. enrichment_date - timestamp
19. is_deleted - boolean (soft delete flag, default false)
20. deleted_at - timestamp
21. deleted_by_import_id - integer
22. last_seen_import_id - integer
23. latitude - numeric(10,8) (derived from bg_x/bg_y, EPSG:3347 -> EPSG:4326, migration 30)
24. longitude - numeric(11,8) (derived from bg_x/bg_y, EPSG:3347 -> EPSG:4326, migration 30)

nar_locations table

1. id - integer (primary key)
2. loc_guid - uuid (unique)
3. bg_latitude - numeric(10,8)
4. bg_longitude - numeric(11,8)
5. csd_code - integer (Census Subdivision code)
6. is_deleted - boolean (soft delete flag, default false)
7. deleted_at - timestamp
8. deleted_by_import_id - integer
9. last_seen_import_id - integer