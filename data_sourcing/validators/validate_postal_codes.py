#!/usr/bin/env python3
"""
Validate commercial postal code database against geocoder reference data.

Produces structured validation report with:
- FSA coverage analysis
- Location accuracy assessment
- City-municipality alignment check
- FSA-municipality mapping validation
- Data quality checks

Outputs:
- Console summary with key findings
- Detailed JSON report file

Usage:
    DB_HOST=localhost uv run python validate_postal_codes.py

    # Save to specific file:
    DB_HOST=localhost uv run python validate_postal_codes.py --output report.json
"""

import asyncio
import json
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
from db_utils import get_db_pool


@dataclass
class ValidationCheck:
    """Result of a single validation check."""
    name: str
    status: str  # 'pass', 'warning', 'fail'
    expected: Optional[Any] = None
    actual: Optional[Any] = None
    details: Optional[str] = None
    items: List[Dict] = field(default_factory=list)


@dataclass
class ValidationSection:
    """A section of the validation report."""
    title: str
    checks: List[ValidationCheck] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationReport:
    """Full validation report."""
    timestamp: str
    source_table: str
    record_count: int
    sections: List[ValidationSection] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)
    overall_status: str = 'unknown'


async def get_data_overview(pool) -> Dict[str, int]:
    """Get basic counts from commercial postal codes table."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT
                COUNT(*) as total_records,
                COUNT(DISTINCT fsa_code) as unique_fsas,
                COUNT(DISTINCT city) as unique_cities_raw,
                COUNT(DISTINCT city_normalized) as unique_cities_normalized,
                COUNT(*) FILTER (WHERE location IS NOT NULL) as records_with_coords
            FROM commercial_postal_codes
        """)
        return dict(row)


async def validate_fsa_coverage(pool) -> ValidationSection:
    """Validate FSA coverage against fsa_boundaries."""
    section = ValidationSection(title="FSA Coverage Analysis")

    async with pool.acquire() as conn:
        # Get counts
        stats = await conn.fetchrow("""
            WITH commercial AS (
                SELECT COUNT(DISTINCT fsa_code) as count FROM commercial_postal_codes
            ),
            boundaries AS (
                SELECT COUNT(*) as count FROM fsa_boundaries WHERE province_id = '35'
            ),
            matched AS (
                SELECT COUNT(DISTINCT c.fsa_code) as count
                FROM commercial_postal_codes c
                JOIN fsa_boundaries f ON c.fsa_code = f.fsa_code
            )
            SELECT
                commercial.count as commercial_fsas,
                boundaries.count as boundary_fsas,
                matched.count as matched_fsas
            FROM commercial, boundaries, matched
        """)

        section.summary = {
            'commercial_fsas': stats['commercial_fsas'],
            'boundary_fsas': stats['boundary_fsas'],
            'matched_fsas': stats['matched_fsas'],
            'match_rate': round(stats['matched_fsas'] * 100.0 / stats['boundary_fsas'], 1) if stats['boundary_fsas'] > 0 else 0
        }

        # FSAs in commercial but not boundaries
        commercial_only = await conn.fetch("""
            SELECT DISTINCT c.fsa_code, COUNT(*) as postal_count
            FROM commercial_postal_codes c
            LEFT JOIN fsa_boundaries f ON c.fsa_code = f.fsa_code
            WHERE f.fsa_code IS NULL
            GROUP BY c.fsa_code
            ORDER BY postal_count DESC
        """)

        if commercial_only:
            section.checks.append(ValidationCheck(
                name="FSAs in commercial DB not in boundaries",
                status="warning",
                expected=0,
                actual=len(commercial_only),
                items=[dict(r) for r in commercial_only]
            ))
        else:
            section.checks.append(ValidationCheck(
                name="FSAs in commercial DB not in boundaries",
                status="pass",
                expected=0,
                actual=0
            ))

        # FSAs in boundaries but not commercial
        boundary_only = await conn.fetch("""
            SELECT f.fsa_code, f.province_name, ROUND(f.land_area_km2::numeric, 2) as area_km2
            FROM fsa_boundaries f
            LEFT JOIN commercial_postal_codes c ON f.fsa_code = c.fsa_code
            WHERE f.province_id = '35' AND c.fsa_code IS NULL
            ORDER BY f.fsa_code
        """)

        if boundary_only:
            section.checks.append(ValidationCheck(
                name="Ontario FSAs missing from commercial DB",
                status="warning",
                expected=0,
                actual=len(boundary_only),
                items=[dict(r) for r in boundary_only]
            ))
        else:
            section.checks.append(ValidationCheck(
                name="Ontario FSAs missing from commercial DB",
                status="pass",
                expected=0,
                actual=0
            ))

    return section


async def validate_location_accuracy(pool) -> ValidationSection:
    """Validate FSA centroid locations."""
    section = ValidationSection(title="FSA Location Accuracy")

    async with pool.acquire() as conn:
        # Distance distribution
        bands = await conn.fetch("""
            WITH commercial_fsa_centroids AS (
                SELECT fsa_code, AVG(latitude) as comm_lat, AVG(longitude) as comm_lon
                FROM commercial_postal_codes
                WHERE latitude IS NOT NULL
                GROUP BY fsa_code
            ),
            fsa_boundary_centroids AS (
                SELECT fsa_code,
                    ST_Y(ST_Centroid(geometry::geometry))::decimal(12,8) as bound_lat,
                    ST_X(ST_Centroid(geometry::geometry))::decimal(12,8) as bound_lon
                FROM fsa_boundaries WHERE province_id = '35'
            ),
            distances AS (
                SELECT c.fsa_code,
                    ST_Distance(
                        ST_SetSRID(ST_MakePoint(c.comm_lon, c.comm_lat), 4326)::geography,
                        ST_SetSRID(ST_MakePoint(b.bound_lon, b.bound_lat), 4326)::geography
                    ) / 1000 as distance_km
                FROM commercial_fsa_centroids c
                JOIN fsa_boundary_centroids b ON c.fsa_code = b.fsa_code
            )
            SELECT
                CASE
                    WHEN distance_km < 1 THEN '< 1 km'
                    WHEN distance_km < 5 THEN '1-5 km'
                    WHEN distance_km < 10 THEN '5-10 km'
                    WHEN distance_km < 25 THEN '10-25 km'
                    ELSE '> 25 km'
                END as band,
                COUNT(*) as count
            FROM distances
            GROUP BY 1
            ORDER BY MIN(distance_km)
        """)

        section.summary = {
            'distance_distribution': {r['band']: r['count'] for r in bands}
        }

        # Check for large discrepancies
        large_discrepancies = await conn.fetch("""
            WITH commercial_fsa_centroids AS (
                SELECT fsa_code, AVG(latitude) as comm_lat, AVG(longitude) as comm_lon, COUNT(*) as postal_count
                FROM commercial_postal_codes
                WHERE latitude IS NOT NULL
                GROUP BY fsa_code
            ),
            fsa_boundary_centroids AS (
                SELECT fsa_code,
                    ST_Y(ST_Centroid(geometry::geometry))::decimal(12,8) as bound_lat,
                    ST_X(ST_Centroid(geometry::geometry))::decimal(12,8) as bound_lon
                FROM fsa_boundaries WHERE province_id = '35'
            )
            SELECT c.fsa_code, c.postal_count,
                ROUND((ST_Distance(
                    ST_SetSRID(ST_MakePoint(c.comm_lon, c.comm_lat), 4326)::geography,
                    ST_SetSRID(ST_MakePoint(b.bound_lon, b.bound_lat), 4326)::geography
                ) / 1000)::numeric, 2) as distance_km
            FROM commercial_fsa_centroids c
            JOIN fsa_boundary_centroids b ON c.fsa_code = b.fsa_code
            WHERE ST_Distance(
                ST_SetSRID(ST_MakePoint(c.comm_lon, c.comm_lat), 4326)::geography,
                ST_SetSRID(ST_MakePoint(b.bound_lon, b.bound_lat), 4326)::geography
            ) / 1000 > 25
            ORDER BY distance_km DESC
        """)

        if large_discrepancies:
            section.checks.append(ValidationCheck(
                name="FSAs with >25km centroid discrepancy",
                status="fail",
                expected=0,
                actual=len(large_discrepancies),
                details="These FSAs have significant location mismatches",
                items=[dict(r) for r in large_discrepancies]
            ))
        else:
            section.checks.append(ValidationCheck(
                name="FSAs with >25km centroid discrepancy",
                status="pass",
                expected=0,
                actual=0
            ))

    return section


async def validate_city_alignment(pool) -> ValidationSection:
    """Validate city names against municipality_aliases."""
    section = ValidationSection(title="City-Municipality Alignment")

    async with pool.acquire() as conn:
        # Get counts
        stats = await conn.fetchrow("""
            WITH commercial_cities AS (
                SELECT COUNT(DISTINCT city_normalized) as count
                FROM commercial_postal_codes WHERE city_normalized IS NOT NULL
            ),
            matched_cities AS (
                SELECT COUNT(DISTINCT c.city_normalized) as count
                FROM commercial_postal_codes c
                JOIN municipality_aliases a ON c.city_normalized = a.normalized_alias
            )
            SELECT
                commercial_cities.count as commercial_cities,
                matched_cities.count as matched_cities
            FROM commercial_cities, matched_cities
        """)

        section.summary = {
            'commercial_cities': stats['commercial_cities'],
            'matched_cities': stats['matched_cities'],
            'unmatched_cities': stats['commercial_cities'] - stats['matched_cities'],
            'match_rate': round(stats['matched_cities'] * 100.0 / stats['commercial_cities'], 1) if stats['commercial_cities'] > 0 else 0
        }

        # Get unmatched cities (top 50 by postal count)
        unmatched = await conn.fetch("""
            SELECT c.city as city_raw, c.city_normalized,
                COUNT(*) as postal_count,
                COUNT(DISTINCT c.fsa_code) as fsa_count
            FROM commercial_postal_codes c
            LEFT JOIN municipality_aliases a ON c.city_normalized = a.normalized_alias
            WHERE a.id IS NULL AND c.city_normalized IS NOT NULL
            GROUP BY c.city, c.city_normalized
            ORDER BY postal_count DESC
            LIMIT 50
        """)

        if unmatched:
            section.checks.append(ValidationCheck(
                name="Cities without municipality alias",
                status="warning",
                expected=0,
                actual=section.summary['unmatched_cities'],
                details="These cities need alias entries for proper matching",
                items=[dict(r) for r in unmatched]
            ))
        else:
            section.checks.append(ValidationCheck(
                name="Cities without municipality alias",
                status="pass",
                expected=0,
                actual=0
            ))

    return section


async def validate_fsa_municipality_mapping(pool) -> ValidationSection:
    """Validate FSA+city combinations against fsa_municipality_mappings."""
    section = ValidationSection(title="FSA-Municipality Mapping Validation")

    async with pool.acquire() as conn:
        # Get counts
        stats = await conn.fetchrow("""
            WITH commercial_fsa_city AS (
                SELECT DISTINCT fsa_code, city_normalized
                FROM commercial_postal_codes
                WHERE city_normalized IS NOT NULL
            ),
            matched AS (
                SELECT COUNT(*) as count
                FROM commercial_fsa_city c
                JOIN fsa_municipality_mappings m
                    ON c.fsa_code = m.fsa_code
                    AND c.city_normalized = normalize_municipality_name(m.municipality_name_short)
            )
            SELECT
                (SELECT COUNT(*) FROM commercial_fsa_city) as total_combos,
                matched.count as matched_combos
            FROM matched
        """)

        section.summary = {
            'total_combinations': stats['total_combos'],
            'matched_combinations': stats['matched_combos'],
            'new_combinations': stats['total_combos'] - stats['matched_combos'],
            'match_rate': round(stats['matched_combos'] * 100.0 / stats['total_combos'], 1) if stats['total_combos'] > 0 else 0
        }

        # Get new FSA-city combinations (top 50)
        new_combos = await conn.fetch("""
            WITH commercial_fsa_city AS (
                SELECT fsa_code, city_normalized, city, COUNT(*) as postal_count
                FROM commercial_postal_codes
                WHERE city_normalized IS NOT NULL
                GROUP BY fsa_code, city_normalized, city
            )
            SELECT c.fsa_code, c.city as city_raw, c.city_normalized, c.postal_count
            FROM commercial_fsa_city c
            LEFT JOIN fsa_municipality_mappings m
                ON c.fsa_code = m.fsa_code
                AND c.city_normalized = normalize_municipality_name(m.municipality_name_short)
            WHERE m.id IS NULL
            ORDER BY c.postal_count DESC
            LIMIT 50
        """)

        if new_combos:
            section.checks.append(ValidationCheck(
                name="FSA-city combinations not in mappings",
                status="warning",
                expected=0,
                actual=section.summary['new_combinations'],
                details="New relationships to consider for fsa_municipality_mappings",
                items=[dict(r) for r in new_combos]
            ))
        else:
            section.checks.append(ValidationCheck(
                name="FSA-city combinations not in mappings",
                status="pass",
                expected=0,
                actual=0
            ))

    return section


async def validate_data_quality(pool) -> ValidationSection:
    """Validate overall data quality."""
    section = ValidationSection(title="Data Quality Checks")

    async with pool.acquire() as conn:
        stats = await conn.fetchrow("""
            SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE latitude IS NOT NULL AND longitude IS NOT NULL) as with_coords,
                COUNT(*) FILTER (WHERE city IS NOT NULL) as with_city,
                COUNT(*) FILTER (
                    WHERE latitude BETWEEN 41.0 AND 57.0
                    AND longitude BETWEEN -95.0 AND -74.0
                ) as in_ontario_bbox,
                COUNT(*) FILTER (
                    WHERE latitude IS NOT NULL
                    AND (latitude < 41.0 OR latitude > 57.0 OR longitude < -95.0 OR longitude > -74.0)
                ) as outside_ontario,
                COUNT(*) FILTER (
                    WHERE postal_code_raw ~ '^[A-Z][0-9][A-Z] [0-9][A-Z][0-9]$'
                ) as valid_format
            FROM commercial_postal_codes
        """)

        section.summary = {
            'total_records': stats['total'],
            'with_coordinates': stats['with_coords'],
            'with_city': stats['with_city'],
            'in_ontario_bbox': stats['in_ontario_bbox'],
            'outside_ontario': stats['outside_ontario'],
            'valid_postal_format': stats['valid_format']
        }

        # Check coordinates in Ontario
        if stats['outside_ontario'] > 0:
            outside = await conn.fetch("""
                SELECT postal_code_raw, fsa_code, city, latitude, longitude
                FROM commercial_postal_codes
                WHERE latitude IS NOT NULL
                AND (latitude < 41.0 OR latitude > 57.0 OR longitude < -95.0 OR longitude > -74.0)
                LIMIT 20
            """)
            section.checks.append(ValidationCheck(
                name="Coordinates outside Ontario bounding box",
                status="fail" if stats['outside_ontario'] > 100 else "warning",
                expected=0,
                actual=stats['outside_ontario'],
                items=[dict(r) for r in outside]
            ))
        else:
            section.checks.append(ValidationCheck(
                name="Coordinates outside Ontario bounding box",
                status="pass",
                expected=0,
                actual=0
            ))

        # Check postal code format
        invalid_format = stats['total'] - stats['valid_format']
        if invalid_format > 0:
            section.checks.append(ValidationCheck(
                name="Invalid postal code format",
                status="warning",
                expected=0,
                actual=invalid_format
            ))
        else:
            section.checks.append(ValidationCheck(
                name="Invalid postal code format",
                status="pass",
                expected=0,
                actual=0
            ))

    return section


async def generate_report(output_path: Optional[str] = None) -> ValidationReport:
    """Generate complete validation report."""
    print("Connecting to database...")
    pool = await get_db_pool()

    try:
        # Get overview
        print("Getting data overview...")
        overview = await get_data_overview(pool)

        report = ValidationReport(
            timestamp=datetime.now().isoformat(),
            source_table='commercial_postal_codes',
            record_count=overview['total_records']
        )

        # Run validations
        print("\n1. Validating FSA coverage...")
        report.sections.append(await validate_fsa_coverage(pool))

        print("2. Validating location accuracy...")
        report.sections.append(await validate_location_accuracy(pool))

        print("3. Validating city-municipality alignment...")
        report.sections.append(await validate_city_alignment(pool))

        print("4. Validating FSA-municipality mappings...")
        report.sections.append(await validate_fsa_municipality_mapping(pool))

        print("5. Running data quality checks...")
        report.sections.append(await validate_data_quality(pool))

        # Generate recommendations
        report.recommendations = generate_recommendations(report)

        # Determine overall status
        all_checks = [c for s in report.sections for c in s.checks]
        if any(c.status == 'fail' for c in all_checks):
            report.overall_status = 'fail'
        elif any(c.status == 'warning' for c in all_checks):
            report.overall_status = 'warning'
        else:
            report.overall_status = 'pass'

        # Print console summary
        print_summary(report, overview)

        # Save JSON report
        if output_path:
            save_report(report, output_path)

        return report

    finally:
        await pool.close()


def generate_recommendations(report: ValidationReport) -> List[str]:
    """Generate recommendations based on validation results."""
    recommendations = []

    for section in report.sections:
        if section.title == "City-Municipality Alignment":
            unmatched = section.summary.get('unmatched_cities', 0)
            if unmatched > 0:
                recommendations.append(
                    f"Add {unmatched} missing city entries to municipality_aliases table"
                )

        elif section.title == "FSA-Municipality Mapping Validation":
            new_combos = section.summary.get('new_combinations', 0)
            if new_combos > 0:
                recommendations.append(
                    f"Review {new_combos} new FSA-city combinations for potential addition to fsa_municipality_mappings"
                )

        elif section.title == "FSA Location Accuracy":
            dist = section.summary.get('distance_distribution', {})
            if dist.get('> 25 km', 0) > 0:
                recommendations.append(
                    f"Investigate {dist['> 25 km']} FSAs with >25km centroid discrepancy"
                )

    return recommendations


def print_summary(report: ValidationReport, overview: Dict) -> None:
    """Print console summary."""
    print("\n" + "=" * 70)
    print("VALIDATION SUMMARY")
    print("=" * 70)

    print(f"\nData Overview:")
    print(f"  Total records: {overview['total_records']:,}")
    print(f"  Unique FSAs: {overview['unique_fsas']}")
    print(f"  Unique cities: {overview['unique_cities_normalized']}")
    print(f"  Records with coords: {overview['records_with_coords']:,}")

    print(f"\nValidation Results:")
    for section in report.sections:
        status_counts = {'pass': 0, 'warning': 0, 'fail': 0}
        for check in section.checks:
            status_counts[check.status] += 1

        status_str = []
        if status_counts['pass']:
            status_str.append(f"{status_counts['pass']} pass")
        if status_counts['warning']:
            status_str.append(f"{status_counts['warning']} warning")
        if status_counts['fail']:
            status_str.append(f"{status_counts['fail']} fail")

        print(f"  {section.title}: {', '.join(status_str)}")

        # Print summary metrics
        for key, value in section.summary.items():
            if isinstance(value, dict):
                print(f"    {key}:")
                for k, v in value.items():
                    print(f"      {k}: {v}")
            else:
                print(f"    {key}: {value}")

    if report.recommendations:
        print(f"\nRecommendations:")
        for rec in report.recommendations:
            print(f"  - {rec}")

    print(f"\nOverall Status: {report.overall_status.upper()}")
    print("=" * 70)


def save_report(report: ValidationReport, output_path: str) -> None:
    """Save report to JSON file."""
    output_path = Path(output_path)

    # Convert dataclasses to dict
    report_dict = asdict(report)

    with open(output_path, 'w') as f:
        json.dump(report_dict, f, indent=2, default=str)

    print(f"\nDetailed report saved to: {output_path}")


async def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='Validate commercial postal code database'
    )
    parser.add_argument(
        '--output', '-o',
        default='postal_code_validation_report.json',
        help='Output JSON report path (default: postal_code_validation_report.json)'
    )

    args = parser.parse_args()

    try:
        await generate_report(args.output)
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())
