#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "asyncpg>=0.29.0",
# ]
# ///
"""
NAR Dataset Confidence Rating Analysis

Analyzes the NAR address dataset to determine accurate confidence ratings based on:
1. Postal code density (properties per postal code)
2. Civic number ambiguity (multiple civic numbers per postal code)
3. Coordinate coverage percentage
4. Overall uniqueness probability

Outputs:
- Console statistics report
- CSV file: nar_postal_code_analysis.csv
- JSON file: nar_confidence_config.json
"""

import asyncio
import asyncpg
import os
import sys
import json
import csv
from datetime import datetime
from decimal import Decimal

# StatCan province code for Ontario (mirrors engine.config.ONTARIO_PROV_CODE;
# duplicated here since this script is self-contained and does not import
# the engine package).
ONTARIO_PROV_CODE = 35


class NARConfidenceAnalyzer:
    """Analyzes NAR dataset to calculate confidence ratings"""

    def __init__(self):
        self.pool = None
        self.stats = {}
        self.postal_code_data = []
        self.same_coords_diff_streets_data = []

    async def connect(self):
        """Connect to NAR database"""
        db_password = os.getenv('DB_PASSWORD')
        if not db_password:
            sys.exit(
                "DB_PASSWORD environment variable is required. "
                "Set it to your PostgreSQL password."
            )

        db_config = {
            'host': os.getenv('DB_HOST', 'localhost'),
            'port': int(os.getenv('DB_PORT', 5432)),
            'database': os.getenv('DB_NAME', 'geocoder'),
            'user': os.getenv('DB_USER', 'geocoder'),
            'password': db_password
        }

        print(f"Connecting to database: {db_config['host']}:{db_config['port']}/{db_config['database']}")

        self.pool = await asyncpg.create_pool(
            **db_config,
            min_size=1,
            max_size=5,
            command_timeout=300  # 5 minutes for large queries
        )

        print("✓ Connected to database\n")

    async def close(self):
        """Close database connection"""
        if self.pool:
            await self.pool.close()

    async def analyze_basic_stats(self):
        """Get basic statistics about the NAR dataset"""
        print("=" * 80)
        print("BASIC DATASET STATISTICS")
        print("=" * 80)

        async with self.pool.acquire() as conn:
            # Total addresses
            total_addresses = await conn.fetchval(
                f"SELECT COUNT(*) FROM nar_addresses WHERE prov_code = {ONTARIO_PROV_CODE}"
            )
            print(f"Total NAR addresses (Ontario): {total_addresses:,}")

            # Addresses with coordinates
            with_coords = await conn.fetchval(
                f"""SELECT COUNT(*) FROM nar_addresses a
                   JOIN nar_locations l ON a.loc_guid = l.loc_guid
                   WHERE a.prov_code = {ONTARIO_PROV_CODE}
                   AND l.bg_latitude IS NOT NULL
                   AND l.bg_longitude IS NOT NULL"""
            )
            coord_pct = (with_coords / total_addresses * 100) if total_addresses > 0 else 0
            print(f"Addresses with coordinates: {with_coords:,} ({coord_pct:.2f}%)")

            # Addresses without coordinates
            without_coords = total_addresses - with_coords
            print(f"Addresses without coordinates: {without_coords:,} ({100-coord_pct:.2f}%)")

            # Unique postal codes
            unique_postal_codes = await conn.fetchval(
                f"SELECT COUNT(DISTINCT mail_postal_code) FROM nar_addresses WHERE prov_code = {ONTARIO_PROV_CODE}"
            )
            print(f"Unique postal codes: {unique_postal_codes:,}")

            # Unique civic numbers
            unique_civic_numbers = await conn.fetchval(
                f"SELECT COUNT(DISTINCT civic_no) FROM nar_addresses WHERE prov_code = {ONTARIO_PROV_CODE}"
            )
            print(f"Unique civic numbers: {unique_civic_numbers:,}")

            # Unique postal code + civic number combinations
            unique_combinations = await conn.fetchval(
                f"""SELECT COUNT(*) FROM (
                    SELECT DISTINCT mail_postal_code, civic_no
                    FROM nar_addresses
                    WHERE prov_code = {ONTARIO_PROV_CODE}
                ) AS combinations"""
            )
            print(f"Unique postal+civic combinations: {unique_combinations:,}")

            self.stats['total_addresses'] = total_addresses
            self.stats['with_coords'] = with_coords
            self.stats['without_coords'] = without_coords
            self.stats['coord_coverage_pct'] = coord_pct
            self.stats['unique_postal_codes'] = unique_postal_codes
            self.stats['unique_civic_numbers'] = unique_civic_numbers
            self.stats['unique_combinations'] = unique_combinations

        print()

    async def analyze_postal_code_density(self):
        """Analyze how many properties belong to each postal code"""
        print("=" * 80)
        print("POSTAL CODE DENSITY ANALYSIS")
        print("=" * 80)

        async with self.pool.acquire() as conn:
            # Get property count per postal code
            rows = await conn.fetch(f"""
                SELECT
                    mail_postal_code,
                    COUNT(*) as property_count,
                    COUNT(DISTINCT civic_no) as unique_civic_numbers,
                    COUNT(CASE WHEN l.bg_latitude IS NOT NULL THEN 1 END) as with_coords,
                    COUNT(*) as total
                FROM nar_addresses a
                LEFT JOIN nar_locations l ON a.loc_guid = l.loc_guid
                WHERE a.prov_code = {ONTARIO_PROV_CODE}
                GROUP BY mail_postal_code
                ORDER BY property_count DESC
            """)

            property_counts = [row['property_count'] for row in rows]

            # Calculate statistics
            total_postal_codes = len(rows)
            single_property_postal_codes = sum(1 for count in property_counts if count == 1)
            multi_property_postal_codes = total_postal_codes - single_property_postal_codes

            print(f"Total postal codes analyzed: {total_postal_codes:,}")
            print(f"Single property postal codes: {single_property_postal_codes:,} ({single_property_postal_codes/total_postal_codes*100:.2f}%)")
            print(f"Multi-property postal codes: {multi_property_postal_codes:,} ({multi_property_postal_codes/total_postal_codes*100:.2f}%)")
            print()

            # Distribution statistics
            avg_properties = sum(property_counts) / len(property_counts)
            max_properties = max(property_counts)
            min_properties = min(property_counts)

            print(f"Properties per postal code:")
            print(f"  Average: {avg_properties:.2f}")
            print(f"  Minimum: {min_properties}")
            print(f"  Maximum: {max_properties}")

            # Percentiles
            sorted_counts = sorted(property_counts)
            p50 = sorted_counts[len(sorted_counts) // 2]
            p75 = sorted_counts[int(len(sorted_counts) * 0.75)]
            p90 = sorted_counts[int(len(sorted_counts) * 0.90)]
            p95 = sorted_counts[int(len(sorted_counts) * 0.95)]
            p99 = sorted_counts[int(len(sorted_counts) * 0.99)]

            print(f"  50th percentile (median): {p50}")
            print(f"  75th percentile: {p75}")
            print(f"  90th percentile: {p90}")
            print(f"  95th percentile: {p95}")
            print(f"  99th percentile: {p99}")
            print()

            # Top 10 densest postal codes
            print("Top 10 densest postal codes:")
            for i, row in enumerate(rows[:10], 1):
                coord_pct = (row['with_coords'] / row['total'] * 100) if row['total'] > 0 else 0
                print(f"  {i}. {row['mail_postal_code']}: {row['property_count']:,} properties, "
                      f"{row['unique_civic_numbers']} unique civic #s, "
                      f"{coord_pct:.1f}% with coords")

            self.stats['postal_code_density'] = {
                'total_postal_codes': total_postal_codes,
                'single_property': single_property_postal_codes,
                'multi_property': multi_property_postal_codes,
                'avg_properties': avg_properties,
                'min_properties': min_properties,
                'max_properties': max_properties,
                'median': p50,
                'p75': p75,
                'p90': p90,
                'p95': p95,
                'p99': p99
            }

            # Store for CSV export
            self.postal_code_data = []
            for row in rows:
                coord_pct = (row['with_coords'] / row['total'] * 100) if row['total'] > 0 else 0
                self.postal_code_data.append({
                    'postal_code': row['mail_postal_code'],
                    'property_count': row['property_count'],
                    'unique_civic_numbers': row['unique_civic_numbers'],
                    'coordinate_coverage_pct': round(coord_pct, 2)
                })

        print()

    async def analyze_civic_number_ambiguity(self):
        """Analyze postal codes with multiple civic numbers"""
        print("=" * 80)
        print("CIVIC NUMBER AMBIGUITY ANALYSIS")
        print("=" * 80)

        async with self.pool.acquire() as conn:
            # Postal codes with multiple civic numbers
            rows = await conn.fetch(f"""
                SELECT
                    mail_postal_code,
                    COUNT(DISTINCT civic_no) as civic_count
                FROM nar_addresses
                WHERE prov_code = {ONTARIO_PROV_CODE}
                GROUP BY mail_postal_code
                HAVING COUNT(DISTINCT civic_no) > 1
                ORDER BY civic_count DESC
            """)

            total_ambiguous = len(rows)
            civic_counts = [row['civic_count'] for row in rows]

            print(f"Postal codes with multiple civic numbers: {total_ambiguous:,}")
            print(f"Percentage of all postal codes: {total_ambiguous/self.stats['postal_code_density']['total_postal_codes']*100:.2f}%")
            print()

            if civic_counts:
                avg_civic = sum(civic_counts) / len(civic_counts)
                max_civic = max(civic_counts)
                min_civic = min(civic_counts)

                print(f"Civic numbers per ambiguous postal code:")
                print(f"  Average: {avg_civic:.2f}")
                print(f"  Minimum: {min_civic}")
                print(f"  Maximum: {max_civic}")
                print()

                # Distribution of civic number counts
                civic_2_5 = sum(1 for c in civic_counts if 2 <= c <= 5)
                civic_6_10 = sum(1 for c in civic_counts if 6 <= c <= 10)
                civic_11_20 = sum(1 for c in civic_counts if 11 <= c <= 20)
                civic_21_plus = sum(1 for c in civic_counts if c > 20)

                print(f"Distribution of civic numbers:")
                print(f"  2-5 civic numbers: {civic_2_5:,} postal codes ({civic_2_5/total_ambiguous*100:.2f}%)")
                print(f"  6-10 civic numbers: {civic_6_10:,} postal codes ({civic_6_10/total_ambiguous*100:.2f}%)")
                print(f"  11-20 civic numbers: {civic_11_20:,} postal codes ({civic_11_20/total_ambiguous*100:.2f}%)")
                print(f"  21+ civic numbers: {civic_21_plus:,} postal codes ({civic_21_plus/total_ambiguous*100:.2f}%)")
                print()

                # Top 10 most ambiguous postal codes
                print("Top 10 most ambiguous postal codes:")
                for i, row in enumerate(rows[:10], 1):
                    print(f"  {i}. {row['mail_postal_code']}: {row['civic_count']} different civic numbers")

                self.stats['civic_ambiguity'] = {
                    'total_ambiguous': total_ambiguous,
                    'percentage_ambiguous': total_ambiguous/self.stats['postal_code_density']['total_postal_codes']*100,
                    'avg_civic_numbers': avg_civic,
                    'min_civic_numbers': min_civic,
                    'max_civic_numbers': max_civic,
                    'distribution': {
                        '2-5': civic_2_5,
                        '6-10': civic_6_10,
                        '11-20': civic_11_20,
                        '21+': civic_21_plus
                    }
                }

        print()

    async def analyze_postal_civic_uniqueness(self):
        """Analyze uniqueness of postal_code + civic_no combinations"""
        print("=" * 80)
        print("POSTAL CODE + CIVIC NUMBER UNIQUENESS ANALYSIS")
        print("=" * 80)

        async with self.pool.acquire() as conn:
            # Count how many addresses share the same postal+civic combination
            rows = await conn.fetch(f"""
                SELECT
                    COUNT(*) as address_count,
                    COUNT(CASE WHEN dup_count = 1 THEN 1 END) as unique_combinations,
                    COUNT(CASE WHEN dup_count > 1 THEN 1 END) as duplicate_combinations,
                    MAX(dup_count) as max_duplicates,
                    AVG(dup_count) as avg_duplicates
                FROM (
                    SELECT
                        mail_postal_code,
                        civic_no,
                        COUNT(*) as dup_count
                    FROM nar_addresses
                    WHERE prov_code = {ONTARIO_PROV_CODE}
                    GROUP BY mail_postal_code, civic_no
                ) AS combinations
            """)

            row = rows[0]
            unique = row['unique_combinations']
            duplicates = row['duplicate_combinations']
            total = unique + duplicates

            print(f"Postal+Civic combinations (raw):")
            print(f"  Total unique combinations: {total:,}")
            print(f"  Unique (1 address): {unique:,} ({unique/total*100:.2f}%)")
            print(f"  Duplicates (2+ addresses): {duplicates:,} ({duplicates/total*100:.2f}%)")
            print()

            # Identify condo towers within duplicates (resolvable with coords + units)
            print(f"Analyzing duplicates to identify resolvable condo towers...")
            condo_combinations = await conn.fetch(f"""
                SELECT
                    a.mail_postal_code,
                    a.civic_no,
                    COUNT(*) as address_count,
                    COUNT(DISTINCT official_street_name) as unique_streets,
                    COUNT(DISTINCT apt_no_label) as unique_units,
                    COUNT(DISTINCT CAST(l.bg_latitude AS TEXT) || ',' || CAST(l.bg_longitude AS TEXT)) as unique_coords,
                    COUNT(CASE WHEN l.bg_latitude IS NOT NULL THEN 1 END) as with_coords
                FROM nar_addresses a
                LEFT JOIN nar_locations l ON a.loc_guid = l.loc_guid
                WHERE a.prov_code = {ONTARIO_PROV_CODE}
                GROUP BY a.mail_postal_code, a.civic_no
                HAVING COUNT(*) > 1
                    AND COUNT(DISTINCT CAST(l.bg_latitude AS TEXT) || ',' || CAST(l.bg_longitude AS TEXT)) = 1
                    AND COUNT(DISTINCT official_street_name) = 1
                    AND COUNT(DISTINCT apt_no_label) > 1
                    AND COUNT(CASE WHEN l.bg_latitude IS NOT NULL THEN 1 END) > 0
            """)

            condo_towers = len(condo_combinations)

            # Identify resolvable different streets (those which can be resolved with full street address + apt)
            print(f"Analyzing duplicates to identify resolvable different streets...")
            resolvable_streets = await conn.fetch(f"""
                WITH postal_civic_dups AS (
                    SELECT
                        a.mail_postal_code,
                        a.civic_no,
                        COUNT(*) as address_count
                    FROM nar_addresses a
                    WHERE a.prov_code = {ONTARIO_PROV_CODE}
                    GROUP BY a.mail_postal_code, a.civic_no
                    HAVING COUNT(*) > 1
                        -- Exclude condo towers
                        AND NOT EXISTS (
                            SELECT 1
                            FROM nar_addresses a2
                            LEFT JOIN nar_locations l2 ON a2.loc_guid = l2.loc_guid
                            WHERE a2.mail_postal_code = a.mail_postal_code
                                AND a2.civic_no = a.civic_no
                                AND a2.prov_code = {ONTARIO_PROV_CODE}
                            GROUP BY a2.mail_postal_code, a2.civic_no
                            HAVING COUNT(DISTINCT CAST(l2.bg_latitude AS TEXT) || ',' || CAST(l2.bg_longitude AS TEXT)) = 1
                                AND COUNT(DISTINCT a2.official_street_name) = 1
                                AND COUNT(DISTINCT a2.apt_no_label) > 1
                                AND COUNT(CASE WHEN l2.bg_latitude IS NOT NULL THEN 1 END) > 0
                        )
                ),
                street_disambiguated AS (
                    SELECT
                        a.mail_postal_code,
                        a.civic_no,
                        a.official_street_name,
                        a.official_street_type,
                        a.official_street_dir,
                        a.csd_eng_name,
                        a.civic_no_suffix,
                        a.apt_no_label,
                        COUNT(*) as combo_count
                    FROM nar_addresses a
                    INNER JOIN postal_civic_dups pcd
                        ON a.mail_postal_code = pcd.mail_postal_code
                        AND a.civic_no = pcd.civic_no
                    WHERE a.prov_code = {ONTARIO_PROV_CODE}
                    GROUP BY a.mail_postal_code, a.civic_no, a.official_street_name,
                             a.official_street_type, a.official_street_dir, a.csd_eng_name,
                             a.civic_no_suffix, a.apt_no_label
                )
                SELECT DISTINCT mail_postal_code, civic_no
                FROM street_disambiguated
                WHERE combo_count = 1
            """)

            resolvable_different_streets = len(resolvable_streets)
            true_duplicates = duplicates - condo_towers - resolvable_different_streets
            effectively_unique = unique + condo_towers + resolvable_different_streets

            print(f"  Condo towers (same coords, same street, diff units): {condo_towers:,} ({condo_towers/duplicates*100:.2f}% of duplicates)")
            print(f"  Resolvable via full address (street_name + street_type + street_dir + city + civic_suffix + apt): {resolvable_different_streets:,} ({resolvable_different_streets/duplicates*100:.2f}% of duplicates)")
            print(f"  True ambiguous duplicates: {true_duplicates:,} ({true_duplicates/duplicates*100:.2f}% of duplicates)")
            print()

            print(f"Adjusted Postal+Civic combinations:")
            print(f"  Effectively unique (including resolvable): {effectively_unique:,} ({effectively_unique/total*100:.2f}%)")
            print(f"    → Includes {unique:,} truly unique + {condo_towers:,} condo towers + {resolvable_different_streets:,} resolvable via full address")
            print(f"  True ambiguous matches: {true_duplicates:,} ({true_duplicates/total*100:.2f}%)")
            print(f"  Maximum addresses per combination: {row['max_duplicates']}")
            print(f"  Average addresses per combination: {row['avg_duplicates']:.2f}")
            print()

            # Probability analysis - use adjusted numbers
            effective_unique_probability = effectively_unique / total * 100
            true_ambiguous_probability = true_duplicates / total * 100

            print(f"Match Probability for Postal+Civic Lookup (Adjusted):")
            print(f"  Unique match (resolvable): {effective_unique_probability:.2f}%")
            print(f"    → Includes {unique:,} truly unique + {condo_towers:,} condo towers + {resolvable_different_streets:,} resolvable via full address")
            print(f"  Ambiguous match (unresolvable without more data): {true_ambiguous_probability:.2f}%")
            print()

            # Examples of true duplicate combinations (excluding condo towers and resolvable streets)
            dup_examples = await conn.fetch(f"""
                WITH duplicates AS (
                    SELECT
                        a.mail_postal_code,
                        a.civic_no,
                        COUNT(*) as count,
                        COUNT(DISTINCT official_street_name) as unique_streets,
                        COUNT(DISTINCT apt_no_label) as unique_units,
                        COUNT(DISTINCT CAST(l.bg_latitude AS TEXT) || ',' || CAST(l.bg_longitude AS TEXT)) as unique_coords,
                        COUNT(CASE WHEN l.bg_latitude IS NOT NULL THEN 1 END) as with_coords,
                        STRING_AGG(DISTINCT official_street_name, ', ') as street_names,
                        STRING_AGG(DISTINCT csd_eng_name, ', ') as cities
                    FROM nar_addresses a
                    LEFT JOIN nar_locations l ON a.loc_guid = l.loc_guid
                    WHERE a.prov_code = {ONTARIO_PROV_CODE}
                    GROUP BY a.mail_postal_code, a.civic_no
                    HAVING COUNT(*) > 1
                ),
                street_disambiguated AS (
                    SELECT
                        a.mail_postal_code,
                        a.civic_no,
                        a.official_street_name,
                        a.official_street_dir,
                        a.csd_eng_name,
                        a.civic_no_suffix,
                        COUNT(*) as combo_count
                    FROM nar_addresses a
                    INNER JOIN duplicates d
                        ON a.mail_postal_code = d.mail_postal_code
                        AND a.civic_no = d.civic_no
                    WHERE a.prov_code = {ONTARIO_PROV_CODE}
                    GROUP BY a.mail_postal_code, a.civic_no, a.official_street_name,
                             a.official_street_dir, a.csd_eng_name, a.civic_no_suffix
                ),
                resolvable_via_street AS (
                    SELECT DISTINCT mail_postal_code, civic_no
                    FROM street_disambiguated
                    WHERE combo_count = 1
                )
                SELECT *
                FROM duplicates
                WHERE NOT (unique_coords = 1 AND unique_streets = 1 AND unique_units > 1 AND with_coords > 0)
                    AND NOT EXISTS (
                        SELECT 1 FROM resolvable_via_street r
                        WHERE r.mail_postal_code = duplicates.mail_postal_code
                            AND r.civic_no = duplicates.civic_no
                    )
                ORDER BY count DESC
                LIMIT 10
            """)

            print(f"Top 10 truly ambiguous Postal+Civic combinations (excluding condo towers & resolvable streets):")
            for i, dup in enumerate(dup_examples, 1):
                print(f"  {i}. {dup['mail_postal_code']} + {dup['civic_no']}: {dup['count']} addresses")
                print(f"     Streets: {dup['street_names'][:80]}{'...' if len(dup['street_names']) > 80 else ''}")
                print(f"     Cities: {dup['cities'][:80]}{'...' if len(dup['cities']) > 80 else ''}")

            self.stats['uniqueness'] = {
                'total_combinations': total,
                'unique_combinations': unique,
                'condo_tower_combinations': condo_towers,
                'resolvable_different_streets': resolvable_different_streets,
                'effectively_unique_combinations': effectively_unique,
                'duplicate_combinations': duplicates,
                'true_ambiguous_combinations': true_duplicates,
                'raw_unique_match_probability': unique / total * 100,
                'effective_unique_match_probability': effective_unique_probability,
                'ambiguous_match_probability': true_ambiguous_probability,
                'max_duplicates': row['max_duplicates'],
                'avg_duplicates': float(row['avg_duplicates'])
            }

        print()

    async def analyze_duplicate_disambiguation(self):
        """Deeply analyze remaining ambiguous postal+civic combinations (excluding condo towers and resolvable streets)"""
        print("=" * 80)
        print("DEEP ANALYSIS: REMAINING AMBIGUOUS DUPLICATES")
        print("=" * 80)
        print("Analyzing truly ambiguous postal+civic combinations...")
        print("(Condo towers and addresses resolvable via full address already excluded)")
        print("(Full address = street_name + street_type + street_dir + city + civic_suffix + apt_no)")
        print()

        async with self.pool.acquire() as conn:
            # Get postal+civic combinations with duplicates, EXCLUDING condo towers AND resolvable streets
            dup_combinations = await conn.fetch(f"""
                WITH postal_civic_dups AS (
                    SELECT
                        a.mail_postal_code,
                        a.civic_no,
                        COUNT(*) as address_count,
                        COUNT(DISTINCT official_street_name) as unique_streets,
                        COUNT(DISTINCT csd_eng_name) as unique_cities,
                        COUNT(DISTINCT apt_no_label) as unique_units,
                        COUNT(DISTINCT CAST(l.bg_latitude AS TEXT) || ',' || CAST(l.bg_longitude AS TEXT)) as unique_coords,
                        COUNT(CASE WHEN l.bg_latitude IS NOT NULL THEN 1 END) as with_coords
                    FROM nar_addresses a
                    LEFT JOIN nar_locations l ON a.loc_guid = l.loc_guid
                    WHERE a.prov_code = {ONTARIO_PROV_CODE}
                    GROUP BY a.mail_postal_code, a.civic_no
                    HAVING COUNT(*) > 1
                        -- Exclude condo towers
                        AND NOT (
                            COUNT(DISTINCT CAST(l.bg_latitude AS TEXT) || ',' || CAST(l.bg_longitude AS TEXT)) = 1
                            AND COUNT(DISTINCT official_street_name) = 1
                            AND COUNT(DISTINCT apt_no_label) > 1
                            AND COUNT(CASE WHEN l.bg_latitude IS NOT NULL THEN 1 END) > 0
                        )
                ),
                street_disambiguated AS (
                    SELECT
                        a.mail_postal_code,
                        a.civic_no,
                        a.official_street_name,
                        a.official_street_type,
                        a.official_street_dir,
                        a.csd_eng_name,
                        a.civic_no_suffix,
                        a.apt_no_label,
                        COUNT(*) as combo_count
                    FROM nar_addresses a
                    INNER JOIN postal_civic_dups pcd
                        ON a.mail_postal_code = pcd.mail_postal_code
                        AND a.civic_no = pcd.civic_no
                    WHERE a.prov_code = {ONTARIO_PROV_CODE}
                    GROUP BY a.mail_postal_code, a.civic_no, a.official_street_name,
                             a.official_street_type, a.official_street_dir, a.csd_eng_name,
                             a.civic_no_suffix, a.apt_no_label
                ),
                resolvable_via_street AS (
                    SELECT DISTINCT mail_postal_code, civic_no
                    FROM street_disambiguated
                    WHERE combo_count = 1
                )
                SELECT pcd.*
                FROM postal_civic_dups pcd
                WHERE NOT EXISTS (
                    SELECT 1 FROM resolvable_via_street r
                    WHERE r.mail_postal_code = pcd.mail_postal_code
                        AND r.civic_no = pcd.civic_no
                )
                ORDER BY pcd.address_count DESC
            """)

            print(f"Found {len(dup_combinations):,} truly ambiguous postal+civic combinations")
            print(f"(Already excluded: condo towers and addresses resolvable via full address)")
            print()

            # Categorize remaining duplicates
            same_street_same_coords = 0  # Same street, same coords (RESOLVABLE - same location)
            same_street_diff_coords = 0  # Same street, different coords (partially resolvable with coordinate proximity)
            different_streets_same_coords = 0  # Different streets, SAME coords (data quality issue - RESOLVABLE)
            different_streets_diff_coords = 0  # Different streets, different coords (true ambiguity)
            no_coord_data = 0  # Missing coordinate data to verify
            other_ambiguities = 0  # Other edge cases

            true_ambiguities = []  # Track problematic cases
            different_streets_cases = []  # Track "different streets" cases for deeper analysis
            same_coords_cases = []  # Track ALL cases with same coords (RESOLVABLE regardless of street)
            no_coord_data_cases = []  # Track "no coord data" cases for deeper analysis

            for dup in dup_combinations:
                postal = dup['mail_postal_code']
                civic = dup['civic_no']
                addr_count = dup['address_count']
                unique_streets = dup['unique_streets']
                unique_cities = dup['unique_cities']
                unique_coords = dup['unique_coords']
                with_coords = dup['with_coords']

                # Classify this duplicate
                if with_coords == 0:
                    # No coordinate data available
                    no_coord_data += 1
                    no_coord_data_cases.append((postal, civic))
                elif unique_coords == 1 and with_coords > 0:
                    # SAME COORDINATES - always resolvable regardless of street/unit variations
                    if unique_streets == 1:
                        # Same street, same coords = same building (RESOLVABLE)
                        same_street_same_coords += 1
                        same_coords_cases.append((postal, civic))
                    else:
                        # Different streets, same coords = data quality issue (RESOLVABLE)
                        different_streets_same_coords += 1
                        same_coords_cases.append((postal, civic))
                elif unique_streets == 1 and unique_coords > 1:
                    # Same street, different coords (varying units at different locations on same street)
                    same_street_diff_coords += 1
                    true_ambiguities.append({
                        'postal_code': postal,
                        'civic_no': civic,
                        'address_count': addr_count,
                        'unique_streets': unique_streets,
                        'unique_cities': unique_cities,
                        'unique_coords': unique_coords,
                        'category': 'same_street_diff_coords'
                    })
                elif unique_streets > 1:
                    # Different streets with different/no coords = True ambiguity (problematic)
                    different_streets_diff_coords += 1
                    different_streets_cases.append((postal, civic))
                    true_ambiguities.append({
                        'postal_code': postal,
                        'civic_no': civic,
                        'address_count': addr_count,
                        'unique_streets': unique_streets,
                        'unique_cities': unique_cities,
                        'unique_coords': unique_coords,
                        'category': 'different_streets'
                    })
                else:
                    # Other edge cases
                    other_ambiguities += 1
                    true_ambiguities.append({
                        'postal_code': postal,
                        'civic_no': civic,
                        'address_count': addr_count,
                        'unique_streets': unique_streets,
                        'unique_cities': unique_cities,
                        'unique_coords': unique_coords,
                        'category': 'no_differentiation'
                    })

            total_dups = len(dup_combinations)

            if total_dups > 0:
                total_resolvable = same_street_same_coords + different_streets_same_coords
                print(f"Ambiguous Duplicate Classification:")
                print(f"  Same street, SAME coordinates: {same_street_same_coords:,} ({same_street_same_coords/total_dups*100:.2f}%)")
                print(f"    → RESOLVABLE: Same location (unit variations like BSMT/main)")
                print(f"  Different streets, SAME coordinates: {different_streets_same_coords:,} ({different_streets_same_coords/total_dups*100:.2f}%)")
                print(f"    → RESOLVABLE: Data quality issue - same location, different street names")
                print(f"  Same street, different coordinates: {same_street_diff_coords:,} ({same_street_diff_coords/total_dups*100:.2f}%)")
                print(f"    → PARTIALLY RESOLVABLE: May distinguish with coordinate proximity")
                print(f"  Different streets, different coords: {different_streets_diff_coords:,} ({different_streets_diff_coords/total_dups*100:.2f}%)")
                print(f"    → PROBLEMATIC: Cannot distinguish without street name")
                print(f"  No coordinate data: {no_coord_data:,} ({no_coord_data/total_dups*100:.2f}%)")
                print(f"    → UNKNOWN: Cannot verify without coordinates")
                print(f"  Other ambiguities: {other_ambiguities:,} ({other_ambiguities/total_dups*100:.2f}%)")
                print(f"    → PROBLEMATIC: Edge cases requiring additional data")
                print()
                print(f"Summary:")
                print(f"  Total resolvable (same coords): {total_resolvable:,} ({total_resolvable/total_dups*100:.2f}%)")
                print(f"  Partially resolvable (same street, diff coords): {same_street_diff_coords:,} ({same_street_diff_coords/total_dups*100:.2f}%)")
                print(f"  Problematic: {different_streets_diff_coords + other_ambiguities:,} ({(different_streets_diff_coords + other_ambiguities)/total_dups*100:.2f}%)")
                print()

                # Show top problematic cases
                if true_ambiguities:
                    print(f"Top 10 Most Problematic Ambiguities:")
                    true_ambiguities_sorted = sorted(true_ambiguities, key=lambda x: x['address_count'], reverse=True)[:10]

                    for i, amb in enumerate(true_ambiguities_sorted, 1):
                        print(f"  {i}. {amb['postal_code']} + {amb['civic_no']} [{amb['category']}]:")
                        print(f"     {amb['address_count']} addresses, {amb['unique_streets']} different streets, "
                              f"{amb['unique_cities']} cities")

                        # Get details for this ambiguous case
                        details = await conn.fetch(f"""
                            SELECT
                                official_street_name,
                                official_street_type,
                                official_street_dir,
                                csd_eng_name,
                                apt_no_label,
                                civic_no_suffix,
                                l.bg_latitude,
                                l.bg_longitude
                            FROM nar_addresses a
                            LEFT JOIN nar_locations l ON a.loc_guid = l.loc_guid
                            WHERE a.prov_code = {ONTARIO_PROV_CODE}
                                AND a.mail_postal_code = $1
                                AND a.civic_no = $2
                            ORDER BY official_street_name, apt_no_label
                            LIMIT 5
                        """, amb['postal_code'], amb['civic_no'])

                        for detail in details:
                            unit_str = f" Unit {detail['apt_no_label']}" if detail['apt_no_label'] else ""
                            coord_str = f"({detail['bg_latitude']:.6f}, {detail['bg_longitude']:.6f})" if detail['bg_latitude'] else "(no coords)"
                            street_parts = [
                                detail['official_street_name'],
                                detail['official_street_type'],
                                detail['official_street_dir']
                            ]
                            street_full = ' '.join([p for p in street_parts if p])
                            suffix_str = f" {detail['civic_no_suffix']}" if detail['civic_no_suffix'] else ""
                            print(f"       - {street_full}{suffix_str}{unit_str}, {detail['csd_eng_name']} {coord_str}")
                    print()

                # Show examples of ALL "same coords" cases (RESOLVABLE)
                if same_coords_cases:
                    print(f"\nTop 10 'Same Coordinates' Cases (RESOLVABLE - same physical location):")

                    # Gather detailed data for these cases for CSV export
                    same_coords_details = []

                    for i, (postal, civic) in enumerate(same_coords_cases[:10], 1):
                        details = await conn.fetch(f"""
                            SELECT
                                a.mail_postal_code,
                                a.civic_no,
                                official_street_name,
                                official_street_type,
                                official_street_dir,
                                csd_eng_name,
                                apt_no_label,
                                civic_no_suffix,
                                l.bg_latitude,
                                l.bg_longitude,
                                COUNT(*) OVER (PARTITION BY a.mail_postal_code, a.civic_no) as total_addresses
                            FROM nar_addresses a
                            LEFT JOIN nar_locations l ON a.loc_guid = l.loc_guid
                            WHERE a.prov_code = {ONTARIO_PROV_CODE}
                                AND a.mail_postal_code = $1
                                AND a.civic_no = $2
                            ORDER BY official_street_name, apt_no_label
                            LIMIT 10
                        """, postal, civic)

                        if details:
                            print(f"  {i}. {postal} + {civic}: {details[0]['total_addresses']} addresses, same coords")
                            streets = set()
                            units = set()
                            coord = None
                            for detail in details:
                                unit_str = f" Unit {detail['apt_no_label']}" if detail['apt_no_label'] else ""
                                if detail['apt_no_label']:
                                    units.add(detail['apt_no_label'])
                                if detail['bg_latitude']:
                                    coord_str = f"({detail['bg_latitude']:.6f}, {detail['bg_longitude']:.6f})"
                                    coord = (detail['bg_latitude'], detail['bg_longitude'])
                                else:
                                    coord_str = "(no coords)"
                                street_parts = [
                                    detail['official_street_name'],
                                    detail['official_street_type'],
                                    detail['official_street_dir']
                                ]
                                street_full = ' '.join([p for p in street_parts if p])
                                streets.add(street_full)
                                suffix_str = f" {detail['civic_no_suffix']}" if detail['civic_no_suffix'] else ""
                                print(f"       - {street_full}{suffix_str}{unit_str}, {detail['csd_eng_name']} {coord_str}")

                            # Store for CSV export
                            same_coords_details.append({
                                'postal_code': postal,
                                'civic_no': civic,
                                'total_addresses': details[0]['total_addresses'],
                                'unique_streets': len(streets),
                                'unique_units': len(units),
                                'streets': ' | '.join(sorted(streets)),
                                'units': ' | '.join(sorted(units)) if units else '',
                                'city': details[0]['csd_eng_name'],
                                'latitude': coord[0] if coord else None,
                                'longitude': coord[1] if coord else None
                            })

                    # Store all same_coords cases for CSV export
                    print(f"\nGathering full details for all {len(same_coords_cases):,} 'same coords' cases...")
                    for postal, civic in same_coords_cases[10:]:  # Get remaining cases not shown above
                        details = await conn.fetch(f"""
                            SELECT
                                a.mail_postal_code,
                                a.civic_no,
                                STRING_AGG(DISTINCT official_street_name || ' ' || COALESCE(official_street_type, '') || ' ' || COALESCE(official_street_dir, ''), ' | ') as streets,
                                STRING_AGG(DISTINCT apt_no_label, ' | ') FILTER (WHERE apt_no_label IS NOT NULL) as units,
                                csd_eng_name,
                                MAX(l.bg_latitude) as latitude,
                                MAX(l.bg_longitude) as longitude,
                                COUNT(*) as total_addresses,
                                COUNT(DISTINCT official_street_name) as unique_streets,
                                COUNT(DISTINCT apt_no_label) FILTER (WHERE apt_no_label IS NOT NULL) as unique_units
                            FROM nar_addresses a
                            LEFT JOIN nar_locations l ON a.loc_guid = l.loc_guid
                            WHERE a.prov_code = {ONTARIO_PROV_CODE}
                                AND a.mail_postal_code = $1
                                AND a.civic_no = $2
                            GROUP BY a.mail_postal_code, a.civic_no, csd_eng_name
                        """, postal, civic)

                        if details:
                            detail = details[0]
                            same_coords_details.append({
                                'postal_code': detail['mail_postal_code'],
                                'civic_no': detail['civic_no'],
                                'total_addresses': detail['total_addresses'],
                                'unique_streets': detail['unique_streets'],
                                'unique_units': detail['unique_units'] if detail['unique_units'] else 0,
                                'streets': detail['streets'],
                                'units': detail['units'] if detail['units'] else '',
                                'city': detail['csd_eng_name'],
                                'latitude': float(detail['latitude']) if detail['latitude'] else None,
                                'longitude': float(detail['longitude']) if detail['longitude'] else None
                            })

                    self.same_coords_diff_streets_data = same_coords_details
                    print(f"✓ Gathered {len(same_coords_details):,} cases for CSV export\n")
                else:
                    self.same_coords_diff_streets_data = []

                # DEEPER ANALYSIS: Test all combinations of additional fields
                await self._analyze_field_combinations_for_disambiguation(
                    conn, different_streets_cases, no_coord_data_cases
                )

            # Store results
            self.stats['duplicate_disambiguation'] = {
                'total_ambiguous_duplicates': total_dups,
                'same_street_same_coords': same_street_same_coords,
                'same_street_diff_coords': same_street_diff_coords,
                'different_streets_same_coords': different_streets_same_coords,
                'different_streets_diff_coords': different_streets_diff_coords,
                'total_resolvable_same_coords': total_resolvable,
                'no_coord_data': no_coord_data,
                'other_ambiguities': other_ambiguities,
                'true_ambiguity_count': len(true_ambiguities)
            }

        print()

    async def _analyze_field_combinations_for_disambiguation(self, conn, different_streets_cases, no_coord_data_cases):
        """
        Analyze all combinations of additional fields to determine which minimize/eliminate duplication.
        Tests combinations of: street_name, street_type, street_dir, city, civic_suffix, apt_no, loc_guid, addr_guid
        """
        from itertools import combinations

        print("=" * 80)
        print("DEEPER ANALYSIS: FIELD COMBINATION TESTING")
        print("=" * 80)
        print("Testing all combinations of additional address fields to find optimal disambiguation...")
        print("Available fields: street_name, street_type, street_dir, city, civic_suffix, apt_no, loc_guid, addr_guid")
        print()

        # Define the additional fields we can test (beyond postal_code + civic_no)
        additional_fields = {
            'street_name': 'official_street_name',
            'street_type': 'official_street_type',
            'street_dir': 'official_street_dir',
            'city': 'csd_eng_name',
            'civic_suffix': 'civic_no_suffix',
            'apt_no': 'apt_no_label',
            'loc_guid': 'loc_guid',
            'addr_guid': 'addr_guid'
        }

        # Test individual fields and all combinations
        field_combinations_to_test = []

        # Single fields
        for field_name in additional_fields.keys():
            field_combinations_to_test.append([field_name])

        # Combinations of 2 fields
        for combo in combinations(additional_fields.keys(), 2):
            field_combinations_to_test.append(list(combo))

        # Combinations of 3 fields
        for combo in combinations(additional_fields.keys(), 3):
            field_combinations_to_test.append(list(combo))

        # Combinations of 4 fields
        for combo in combinations(additional_fields.keys(), 4):
            field_combinations_to_test.append(list(combo))

        # All 5 fields
        field_combinations_to_test.append(list(additional_fields.keys()))

        # Analyze "Different streets" cases (excluding those already resolvable with street+dir+city+suffix)
        if different_streets_cases:
            print(f"ANALYZING 'DIFFERENT STREETS' CASES ({len(different_streets_cases):,} postal+civic combinations)")
            print("(Note: Cases resolvable with street_name + street_dir + city + civic_suffix already excluded)")
            print("-" * 80)
            await self._test_field_combinations(
                conn, different_streets_cases, additional_fields, field_combinations_to_test,
                "different_streets"
            )

        # Analyze "No coordinate data" cases
        if no_coord_data_cases:
            print(f"\nANALYZING 'NO COORDINATE DATA' CASES ({len(no_coord_data_cases):,} postal+civic combinations)")
            print("(These already have identical: postal + civic + street_name + street_type + street_dir + city + civic_suffix + apt_no)")
            print("(Testing additional fields: loc_guid, addr_guid, and other combinations)")
            print("-" * 80)
            await self._test_field_combinations(
                conn, no_coord_data_cases, additional_fields, field_combinations_to_test,
                "no_coord_data"
            )

    async def _test_field_combinations(self, conn, cases, additional_fields, field_combinations_to_test, category_name):
        """
        Test each field combination to see how many duplicates remain.
        """
        if not cases:
            return

        # Sample size for testing: 3500 cases (covers all "different streets" and 10% of "no coord data")
        SAMPLE_SIZE = 3500

        results = []

        for field_combo in field_combinations_to_test:
            # Build the GROUP BY clause with postal_code, civic_no, plus the additional fields
            db_fields = [additional_fields[f] for f in field_combo]
            group_by_fields = ['a.mail_postal_code', 'a.civic_no'] + [f'a.{f}' for f in db_fields]
            group_by_clause = ', '.join(group_by_fields)

            # Build WHERE clause to only check the cases we're interested in
            case_conditions = ' OR '.join([
                f"(a.mail_postal_code = '{postal}' AND a.civic_no = '{civic}')"
                for postal, civic in cases[:SAMPLE_SIZE]
            ])

            # Query to count remaining duplicates with this field combination
            query = f"""
                SELECT COUNT(*) as duplicate_count
                FROM (
                    SELECT {group_by_clause}, COUNT(*) as cnt
                    FROM nar_addresses a
                    WHERE a.prov_code = {ONTARIO_PROV_CODE}
                        AND ({case_conditions})
                    GROUP BY {group_by_clause}
                    HAVING COUNT(*) > 1
                ) AS dups
            """

            try:
                result = await conn.fetchval(query)
                remaining_dups = result if result else 0

                # Calculate how many were resolved
                original_count = len(cases[:SAMPLE_SIZE])
                resolved = original_count - remaining_dups
                resolved_pct = (resolved / original_count * 100) if original_count > 0 else 0

                results.append({
                    'fields': field_combo,
                    'field_count': len(field_combo),
                    'remaining_duplicates': remaining_dups,
                    'resolved': resolved,
                    'resolved_pct': resolved_pct
                })
            except Exception as e:
                print(f"  Error testing {', '.join(field_combo)}: {e}")

        # Sort results by effectiveness (most resolved first)
        results.sort(key=lambda x: (x['resolved_pct'], -x['field_count']), reverse=True)

        # Display results grouped by field count
        sample_size = min(SAMPLE_SIZE, len(cases))
        print(f"\nResults (testing sample of {sample_size} cases):")
        print(f"{'Fields Used':<60} {'Resolved':<10} {'Remaining':<10} {'% Resolved':<12}")
        print("-" * 95)

        for field_count in range(1, 6):
            count_results = [r for r in results if r['field_count'] == field_count]
            if count_results:
                print(f"\n{field_count} Additional Field(s):")
                for r in count_results[:10]:  # Show top 10 per field count
                    fields_str = ' + '.join(r['fields'])
                    print(f"  {fields_str:<58} {r['resolved']:<10} {r['remaining_duplicates']:<10} {r['resolved_pct']:<11.2f}%")

        # Highlight best combinations
        print(f"\n{'='*95}")
        print("BEST COMBINATIONS (Top 5):")
        print("-" * 95)
        for i, r in enumerate(results[:5], 1):
            fields_str = ' + '.join(r['fields'])
            print(f"  {i}. {fields_str}")
            print(f"     → Resolved {r['resolved']}/{sample_size} ({r['resolved_pct']:.2f}%) | "
                  f"Remaining: {r['remaining_duplicates']}")

        # Store results for this category
        if category_name not in self.stats.get('field_combination_analysis', {}):
            if 'field_combination_analysis' not in self.stats:
                self.stats['field_combination_analysis'] = {}
            self.stats['field_combination_analysis'][category_name] = {}

        self.stats['field_combination_analysis'][category_name] = {
            'sample_size': sample_size,
            'total_cases': len(cases),
            'best_single_field': results[0]['fields'] if results and results[0]['field_count'] == 1 else None,
            'best_combination': results[0]['fields'] if results else None,
            'best_combination_resolved_pct': results[0]['resolved_pct'] if results else 0,
            'all_results': results[:20]  # Store top 20 results
        }

        print()

    async def calculate_confidence_ratings(self):
        """Calculate recommended confidence ratings based on analysis"""
        print("=" * 80)
        print("RECOMMENDED CONFIDENCE RATINGS")
        print("=" * 80)

        # Method 1: Postal Code + Civic Number
        # Use effective uniqueness probability (includes condo towers and resolvable streets as resolvable)
        effective_unique_prob = self.stats['uniqueness']['effective_unique_match_probability']
        raw_unique_prob = self.stats['uniqueness']['raw_unique_match_probability']
        ambiguous_prob = self.stats['uniqueness']['ambiguous_match_probability']
        coord_coverage = self.stats['coord_coverage_pct']
        condo_towers = self.stats['uniqueness']['condo_tower_combinations']
        resolvable_streets = self.stats['uniqueness']['resolvable_different_streets']
        total_combos = self.stats['uniqueness']['total_combinations']

        # Calculate base confidence for Method 1
        # Start with effective uniqueness probability (treating condo towers and resolvable streets as resolvable)
        method1_base = min(0.95, effective_unique_prob / 100)  # Cap at 0.95

        print(f"Method 1: Postal Code + Civic Number")
        print(f"  Current confidence: 0.80")
        print(f"  Raw unique match probability: {raw_unique_prob:.2f}%")
        print(f"  Condo towers (resolvable): {condo_towers:,} combinations ({condo_towers/total_combos*100:.2f}%)")
        print(f"  Resolvable different streets: {resolvable_streets:,} combinations ({resolvable_streets/total_combos*100:.2f}%)")
        print(f"  Effective unique match probability: {effective_unique_prob:.2f}%")
        print(f"    → Includes truly unique + condo towers + resolvable streets")
        print(f"  Remaining ambiguous matches: {ambiguous_prob:.2f}%")
        print(f"  Coordinate coverage: {coord_coverage:.2f}%")
        print(f"  Recommended base confidence: {method1_base:.3f}")
        print(f"  → RECOMMENDED: {method1_base:.2f} (based on {effective_unique_prob:.2f}% effective uniqueness)")
        print(f"  → NOTE: For matches with street name/dir/city/suffix, can achieve higher confidence")
        print()

        # Method 2: Full Address String
        # Less specific than postal+civic, but still good
        print(f"Method 2: Full Address String")
        print(f"  Current confidence: 0.75")
        print(f"  → RECOMMENDED: 0.75 (good balance, no change needed)")
        print()

        # Method 3: Component Fuzzy Match
        # Most flexible, lowest confidence
        print(f"Method 3: Component Fuzzy Match")
        print(f"  Current confidence: 0.70")
        print(f"  → RECOMMENDED: 0.65 (fuzzy matching, most ambiguous)")
        print()

        # Dynamic confidence based on match count
        print(f"Dynamic Confidence Adjustments:")
        print(f"  Single match (1 result): +0.05 to base confidence")
        print(f"  Multiple matches (2-5 results): No adjustment")
        print(f"  Many matches (6+ results): -0.05 to base confidence")
        print(f"  No coordinates: -0.10 to base confidence")
        print()

        # Store recommended confidence using effective uniqueness
        recommended_base = round(method1_base, 2)

        self.stats['recommended_confidence'] = {
            'method1_postal_civic': {
                'current': 0.80,
                'recommended_base': recommended_base,
                'single_match': min(0.95, recommended_base + 0.05),
                'multiple_matches': recommended_base,
                'many_matches': max(0.70, recommended_base - 0.05),
                'no_coordinates': max(0.65, recommended_base - 0.10)
            },
            'method2_full_address': {
                'current': 0.75,
                'recommended_base': 0.75,
                'single_match': 0.80,
                'multiple_matches': 0.75,
                'many_matches': 0.70,
                'no_coordinates': 0.65
            },
            'method3_component_fuzzy': {
                'current': 0.70,
                'recommended_base': 0.65,
                'single_match': 0.70,
                'multiple_matches': 0.65,
                'many_matches': 0.60,
                'no_coordinates': 0.55
            }
        }

        print()

    async def add_confidence_multiplier_to_data(self):
        """Add confidence multipliers to postal code data"""
        print("Calculating confidence multipliers for each postal code...")

        for data in self.postal_code_data:
            property_count = data['property_count']
            civic_count = data['unique_civic_numbers']
            coord_coverage = data['coordinate_coverage_pct']

            # Base multiplier: 1.0 for single property, decreases with more properties
            if property_count == 1:
                multiplier = 1.10  # Boost for definite match
            elif property_count <= 5:
                multiplier = 1.05
            elif property_count <= 10:
                multiplier = 1.00
            elif property_count <= 20:
                multiplier = 0.95
            else:
                multiplier = 0.90

            # Adjust for civic number ambiguity
            if civic_count > 10:
                multiplier -= 0.05

            # Adjust for coordinate coverage
            if coord_coverage < 50:
                multiplier -= 0.10
            elif coord_coverage < 80:
                multiplier -= 0.05

            # Ensure multiplier stays in reasonable range
            multiplier = max(0.70, min(1.15, multiplier))

            data['confidence_multiplier'] = round(multiplier, 2)

        print(f"✓ Calculated multipliers for {len(self.postal_code_data):,} postal codes\n")

    async def export_csv(self, filename='nar_postal_code_analysis.csv'):
        """Export postal code analysis to CSV"""
        filepath = os.path.join(os.path.dirname(__file__), filename)

        print(f"Exporting to CSV: {filename}")

        with open(filepath, 'w', newline='') as csvfile:
            fieldnames = ['postal_code', 'property_count', 'unique_civic_numbers',
                         'coordinate_coverage_pct', 'confidence_multiplier']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

            writer.writeheader()
            writer.writerows(self.postal_code_data)

        print(f"✓ Exported {len(self.postal_code_data):,} postal codes to {filepath}\n")

    async def export_json(self, filename='nar_confidence_config.json'):
        """Export confidence configuration to JSON"""
        filepath = os.path.join(os.path.dirname(__file__), filename)

        print(f"Exporting to JSON: {filename}")

        config = {
            'analysis_date': datetime.now().isoformat(),
            'dataset_statistics': self.stats,
            'usage_instructions': {
                'description': 'Use these confidence ratings in the NAR matcher (engine/layered_matcher.py)',
                'method1_postal_civic': 'Base: 0.85, adjust +/-0.05 based on match count',
                'method2_full_address': 'Base: 0.75, adjust +/-0.05 based on match count',
                'method3_component_fuzzy': 'Base: 0.65, adjust +/-0.05 based on match count',
                'coordinate_penalty': 'Subtract 0.10 if coordinates are NULL'
            }
        }

        # Custom JSON encoder for Decimal types
        class DecimalEncoder(json.JSONEncoder):
            def default(self, obj):
                if isinstance(obj, Decimal):
                    return float(obj)
                return super(DecimalEncoder, self).default(obj)

        with open(filepath, 'w') as jsonfile:
            json.dump(config, jsonfile, indent=2, cls=DecimalEncoder)

        print(f"✓ Exported configuration to {filepath}\n")

    async def export_same_coords_diff_streets_csv(self, filename='nar_same_coords_resolvable.csv'):
        """Export same coords cases (resolvable) to CSV for analysis"""
        filepath = os.path.join(os.path.dirname(__file__), filename)

        print(f"Exporting same coords (resolvable) analysis to CSV: {filename}")

        if not self.same_coords_diff_streets_data:
            print(f"No data to export (no same coords cases found)\n")
            return

        with open(filepath, 'w', newline='') as csvfile:
            fieldnames = ['postal_code', 'civic_no', 'total_addresses', 'unique_streets', 'unique_units',
                         'streets', 'units', 'city', 'latitude', 'longitude']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

            writer.writeheader()
            writer.writerows(self.same_coords_diff_streets_data)

        print(f"✓ Exported {len(self.same_coords_diff_streets_data):,} cases to {filepath}")
        print(f"  These are RESOLVABLE - same physical location (may have street/unit variations)\n")


async def main():
    """Main analysis workflow"""
    print("\n" + "=" * 80)
    print("NAR DATASET CONFIDENCE RATING ANALYSIS")
    print("=" * 80)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    print()

    analyzer = NARConfidenceAnalyzer()

    try:
        await analyzer.connect()

        # Run all analyses
        await analyzer.analyze_basic_stats()
        await analyzer.analyze_postal_code_density()
        await analyzer.analyze_civic_number_ambiguity()
        await analyzer.analyze_postal_civic_uniqueness()
        await analyzer.analyze_duplicate_disambiguation()
        await analyzer.calculate_confidence_ratings()

        # Add confidence multipliers and export
        await analyzer.add_confidence_multiplier_to_data()
        await analyzer.export_csv()
        await analyzer.export_json()
        await analyzer.export_same_coords_diff_streets_csv()

        print("=" * 80)
        print("ANALYSIS COMPLETE")
        print("=" * 80)
        print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print()
        print("Next steps:")
        print("1. Review nar_postal_code_analysis.csv for postal code details")
        print("2. Review nar_confidence_config.json for recommendations")
        print("3. Review nar_same_coords_resolvable.csv for resolvable same-location cases")
        print("4. Update the NAR confidence ratings in engine/layered_matcher.py")
        print("=" * 80)
        print()

    except Exception as e:
        print(f"\n[FAIL] Error during analysis: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await analyzer.close()


if __name__ == '__main__':
    asyncio.run(main())
