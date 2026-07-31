#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "asyncpg>=0.29.0",
# ]
# ///
"""
NAR Field Combination Analysis

Analyzes NAR dataset to determine match rates for various field combinations.
This helps evaluate external datasets by predicting how many NAR records can be
uniquely matched based on which fields the external dataset provides.

Key Question: Given a limited set of fields (e.g., civic + street_name + city),
what percentage of NAR records can be uniquely identified?

Outputs:
- Console report with match rates for each field combination
- CSV file: nar_field_combination_analysis.csv
- JSON file: nar_field_combination_matrix.json
- Helper function to evaluate new datasets
"""

import asyncio
import asyncpg
import os
import sys
import json
import csv
from datetime import datetime

# StatCan province code for Ontario (mirrors engine.config.ONTARIO_PROV_CODE;
# duplicated here since this script is self-contained and does not import
# the engine package).
ONTARIO_PROV_CODE = 35
from typing import Dict, List, Tuple


class FieldCombinationAnalyzer:
    """Analyzes NAR dataset to calculate match rates for field combinations"""

    # Define realistic field combinations based on common dataset patterns
    FIELD_COMBINATIONS = {
        # Pattern 1: Minimal Data (Low-quality sources)
        "minimal": [
            ("civic_no", "official_street_name"),
            ("mail_postal_code", "civic_no"),
            ("civic_no", "official_street_name", "csd_eng_name"),
        ],

        # Pattern 2: Standard Address Data (Most municipal datasets)
        "standard": [
            ("civic_no", "official_street_name", "official_street_type", "csd_eng_name"),
            ("mail_postal_code", "civic_no", "official_street_name"),
            ("mail_postal_code", "civic_no", "official_street_name", "csd_eng_name"),
            ("civic_no", "official_street_name", "official_street_type", "official_street_dir", "csd_eng_name"),
        ],

        # Pattern 3: High-Quality Data (Property records)
        "high_quality": [
            ("mail_postal_code", "civic_no", "official_street_name", "official_street_type", "csd_eng_name"),
            ("mail_postal_code", "civic_no", "official_street_name", "official_street_type", "official_street_dir", "csd_eng_name"),
            ("mail_postal_code", "civic_no", "official_street_name", "official_street_type", "official_street_dir", "csd_eng_name", "civic_no_suffix"),
        ],

        # Pattern 4: Special Cases
        "special": [
            ("mail_postal_code", "civic_no", "apt_no_label"),  # Condo databases
            ("civic_no", "official_street_name", "official_street_type", "official_street_dir", "csd_eng_name", "civic_no_suffix"),  # No postal
            # All 8 fields (maximum)
            ("mail_postal_code", "civic_no", "official_street_name", "official_street_type",
             "official_street_dir", "csd_eng_name", "civic_no_suffix", "apt_no_label"),
        ],

        # Pattern 5: Address Fields Only (NO city, NO postal, NO fsa)
        "address_fields_only": [
            # All 6 core address fields - tests if street address alone can uniquely identify
            ("civic_no", "civic_no_suffix", "apt_no_label", "official_street_name",
             "official_street_type", "official_street_dir"),
        ],

        # Pattern 6: Address Fields + FSA (NO city, NO postal)
        "address_fields_with_fsa": [
            # All 6 address fields + FSA code - tests FSA effectiveness without full postal
            ("civic_no", "civic_no_suffix", "apt_no_label", "official_street_name",
             "official_street_type", "official_street_dir", "fsa_code"),
        ],

        # Pattern 7: Address Fields + City (NO postal, NO fsa)
        "address_fields_with_city": [
            # All 6 address fields + city but NO postal - tests municipal dataset matching
            ("civic_no", "civic_no_suffix", "apt_no_label", "official_street_name",
             "official_street_type", "official_street_dir", "csd_eng_name"),
        ],
    }

    # Friendly names for fields
    FIELD_DISPLAY_NAMES = {
        "mail_postal_code": "postal",
        "fsa_code": "fsa",
        "civic_no": "civic",
        "official_street_name": "street_name",
        "official_street_type": "street_type",
        "official_street_dir": "street_dir",
        "csd_eng_name": "city",
        "civic_no_suffix": "civic_suffix",
        "apt_no_label": "apt_no"
    }

    def __init__(self):
        self.pool = None
        self.results = []
        self.total_addresses = 0

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
            command_timeout=600  # 10 minutes for large queries
        )

        print("✓ Connected to database\n")

    async def close(self):
        """Close database connection"""
        if self.pool:
            await self.pool.close()

    async def get_total_addresses(self):
        """Get total number of addresses"""
        async with self.pool.acquire() as conn:
            self.total_addresses = await conn.fetchval(
                f"SELECT COUNT(*) FROM nar_addresses WHERE prov_code = {ONTARIO_PROV_CODE}"
            )
        print(f"Total NAR addresses (Ontario): {self.total_addresses:,}\n")

    def get_display_name(self, fields: Tuple[str, ...]) -> str:
        """Convert field tuple to friendly display name"""
        return " + ".join([self.FIELD_DISPLAY_NAMES.get(f, f) for f in fields])

    async def analyze_field_combination(self, fields: Tuple[str, ...]) -> Dict:
        """
        Analyze a specific field combination to determine match rates.

        Returns:
        - unique_count: Number of address records that are unique with these fields
        - ambiguous_count: Number of address records that share values with others
        - unique_pct: Percentage of addresses that are unique
        - ambiguous_pct: Percentage of addresses that are ambiguous
        """
        async with self.pool.acquire() as conn:
            # Build field list
            # Use COALESCE to treat NULL as empty string for grouping
            coalesced_fields = [f"COALESCE({f}, '')" for f in fields]
            field_list = ", ".join(coalesced_fields)

            # Query: Count addresses by this field combination
            # Then determine how many are unique (cnt=1) vs ambiguous (cnt>1)
            query = f"""
                WITH grouped AS (
                    SELECT
                        {field_list},
                        COUNT(*) as cnt
                    FROM nar_addresses
                    WHERE prov_code = {ONTARIO_PROV_CODE}
                    GROUP BY {field_list}
                )
                SELECT
                    SUM(CASE WHEN cnt = 1 THEN 1 ELSE 0 END) as unique_combinations,
                    SUM(CASE WHEN cnt > 1 THEN cnt ELSE 0 END) as ambiguous_addresses,
                    SUM(cnt) as total_addresses
                FROM grouped
            """

            result = await conn.fetchrow(query)

            unique_combinations = result['unique_combinations'] or 0
            ambiguous_addresses = result['ambiguous_addresses'] or 0
            total = result['total_addresses'] or 0

            # The unique addresses = addresses with unique field combinations
            unique_addresses = unique_combinations

            # Percentages
            unique_pct = (unique_addresses / total * 100) if total > 0 else 0
            ambiguous_pct = (ambiguous_addresses / total * 100) if total > 0 else 0

            return {
                'fields': fields,
                'field_count': len(fields),
                'display_name': self.get_display_name(fields),
                'unique_addresses': unique_addresses,
                'ambiguous_addresses': ambiguous_addresses,
                'total_addresses': total,
                'unique_pct': unique_pct,
                'ambiguous_pct': ambiguous_pct,
                'unique_combinations': unique_combinations,
                'duplicate_combination_count': ambiguous_addresses  # For clarity
            }

    async def analyze_all_combinations(self):
        """Analyze all defined field combinations"""
        print("=" * 80)
        print("ANALYZING FIELD COMBINATIONS")
        print("=" * 80)
        print()

        for pattern_name, combinations in self.FIELD_COMBINATIONS.items():
            print(f"\n{'='*80}")
            print(f"PATTERN: {pattern_name.upper()}")
            print(f"{'='*80}\n")

            for fields in combinations:
                display_name = self.get_display_name(fields)
                print(f"Analyzing: {display_name}...")

                result = await self.analyze_field_combination(fields)
                self.results.append(result)

                print(f"  Unique matches:    {result['unique_addresses']:>10,}  ({result['unique_pct']:>6.2f}%)")
                print(f"  Ambiguous matches: {result['ambiguous_addresses']:>10,}  ({result['ambiguous_pct']:>6.2f}%)")
                print()

    async def print_summary(self):
        """Print summary of results"""
        print("\n" + "=" * 80)
        print("SUMMARY: NAR MATCH POTENTIAL MATRIX")
        print("=" * 80)
        print()

        # Sort by unique percentage (descending)
        sorted_results = sorted(self.results, key=lambda x: x['unique_pct'], reverse=True)

        print(f"{'Field Combination':<60} {'Fields':<8} {'Unique %':<12} {'Ambiguous %'}")
        print("-" * 95)

        for result in sorted_results:
            print(f"{result['display_name']:<60} {result['field_count']:<8} {result['unique_pct']:<11.2f}% {result['ambiguous_pct']:<.2f}%")

        print()
        print("=" * 80)
        print()

    async def export_csv(self, filename='nar_field_combination_analysis.csv'):
        """Export results to CSV"""
        filepath = os.path.join(os.path.dirname(__file__), filename)

        print(f"Exporting to CSV: {filename}")

        with open(filepath, 'w', newline='') as csvfile:
            fieldnames = [
                'field_combination', 'field_count', 'fields_list',
                'unique_addresses', 'ambiguous_addresses', 'total_addresses',
                'unique_pct', 'ambiguous_pct'
            ]
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

            writer.writeheader()
            for result in self.results:
                writer.writerow({
                    'field_combination': result['display_name'],
                    'field_count': result['field_count'],
                    'fields_list': ', '.join(result['fields']),
                    'unique_addresses': result['unique_addresses'],
                    'ambiguous_addresses': result['ambiguous_addresses'],
                    'total_addresses': result['total_addresses'],
                    'unique_pct': round(result['unique_pct'], 2),
                    'ambiguous_pct': round(result['ambiguous_pct'], 2)
                })

        print(f"✓ Exported {len(self.results)} combinations to {filepath}\n")

    async def export_json(self, filename='nar_field_combination_matrix.json'):
        """Export results to JSON"""
        filepath = os.path.join(os.path.dirname(__file__), filename)

        print(f"Exporting to JSON: {filename}")

        # Convert results to JSON-serializable format
        json_results = []
        for result in self.results:
            json_result = {
                'fields': list(result['fields']),
                'field_count': result['field_count'],
                'display_name': result['display_name'],
                'unique_addresses': int(result['unique_addresses']),
                'ambiguous_addresses': int(result['ambiguous_addresses']),
                'total_addresses': int(result['total_addresses']),
                'unique_pct': float(result['unique_pct']),
                'ambiguous_pct': float(result['ambiguous_pct']),
            }
            json_results.append(json_result)

        config = {
            'analysis_date': datetime.now().isoformat(),
            'total_nar_addresses': self.total_addresses,
            'field_combinations': json_results,
            'usage_instructions': {
                'description': 'Use this matrix to evaluate external datasets',
                'how_to_use': 'Match your dataset fields to find expected unique match rate',
                'interpretation': {
                    'unique_pct': 'Percentage of NAR records that can be uniquely matched with these fields',
                    'ambiguous_pct': 'Percentage that will return multiple matches',
                    'recommendation': 'Aim for >95% unique match rate for high-confidence enrichment'
                }
            }
        }

        with open(filepath, 'w') as jsonfile:
            json.dump(config, jsonfile, indent=2)

        print(f"✓ Exported configuration to {filepath}\n")

    def evaluate_dataset(self, available_fields: List[str]) -> Dict:
        """
        Helper function to evaluate a new dataset's matching potential.

        Args:
            available_fields: List of field names available in the external dataset
                            (use NAR field names: mail_postal_code, civic_no, etc.)

        Returns:
            Match potential statistics for this dataset
        """
        # Normalize field names
        available_set = set(available_fields)

        # Find exact match in our analysis
        for result in self.results:
            if set(result['fields']) == available_set:
                return {
                    'exact_match': True,
                    'field_combination': result['display_name'],
                    'unique_match_rate': float(result['unique_pct']),
                    'ambiguous_match_rate': float(result['ambiguous_pct']),
                    'recommendation': self._get_recommendation(result['unique_pct']),
                    'confidence_score': float(self._calculate_confidence(result['unique_pct']))
                }

        # Find closest match (subset)
        closest = None
        closest_overlap = 0

        for result in self.results:
            result_set = set(result['fields'])
            overlap = len(available_set.intersection(result_set))

            if overlap > closest_overlap and available_set.issuperset(result_set):
                closest = result
                closest_overlap = overlap

        if closest:
            return {
                'exact_match': False,
                'closest_field_combination': closest['display_name'],
                'unique_match_rate': float(closest['unique_pct']),
                'ambiguous_match_rate': float(closest['ambiguous_pct']),
                'recommendation': self._get_recommendation(closest['unique_pct']),
                'confidence_score': float(self._calculate_confidence(closest['unique_pct'])),
                'note': 'Using closest analyzed combination (dataset has additional fields)'
            }

        return {
            'exact_match': False,
            'recommendation': 'No matching field combination found in analysis',
            'note': 'Consider running custom analysis for this field set'
        }

    def _get_recommendation(self, unique_pct: float) -> str:
        """Get recommendation based on unique match percentage"""
        if unique_pct >= 99:
            return "EXCELLENT - Use for high-confidence NAR enrichment"
        elif unique_pct >= 95:
            return "GOOD - Acceptable for NAR enrichment with validation"
        elif unique_pct >= 90:
            return "MODERATE - Manual review recommended for ambiguous cases"
        elif unique_pct >= 80:
            return "POOR - High ambiguity rate, use with caution"
        else:
            return "UNACCEPTABLE - Too many ambiguous matches for reliable enrichment"

    def _calculate_confidence(self, unique_pct: float) -> float:
        """Calculate confidence score from unique percentage"""
        # Cap at 0.99
        return min(0.99, unique_pct / 100)


async def main():
    """Main analysis workflow"""
    print("\n" + "=" * 80)
    print("NAR FIELD COMBINATION ANALYSIS")
    print("=" * 80)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    print()

    analyzer = FieldCombinationAnalyzer()

    try:
        await analyzer.connect()
        await analyzer.get_total_addresses()
        await analyzer.analyze_all_combinations()
        await analyzer.print_summary()
        await analyzer.export_csv()
        await analyzer.export_json()

        print("=" * 80)
        print("ANALYSIS COMPLETE")
        print("=" * 80)
        print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print()
        print("Next steps:")
        print("1. Review nar_field_combination_analysis.csv for detailed results")
        print("2. Review nar_field_combination_matrix.json for dataset evaluation")
        print("3. Use evaluate_dataset() function to assess external datasets")
        print("=" * 80)
        print()

        # Example usage of evaluate_dataset
        print("\nEXAMPLE: Evaluating a hypothetical dataset")
        print("-" * 80)
        example_fields = ['civic_no', 'official_street_name', 'official_street_type', 'csd_eng_name']
        evaluation = analyzer.evaluate_dataset(example_fields)
        print(f"Dataset fields: {', '.join(example_fields)}")
        print(f"Evaluation: {json.dumps(evaluation, indent=2)}")
        print()

    except Exception as e:
        print(f"\n[FAIL] Error during analysis: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await analyzer.close()


if __name__ == '__main__':
    asyncio.run(main())
