"""
Tests for commercial postal code validation module.

Run with: uv run pytest tests/data_sourcing/validators/test_validate_postal_codes.py -v

Note: Integration tests require a running database with commercial_postal_codes populated.
"""
import sys
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from contextlib import asynccontextmanager

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / 'data_sourcing' / 'validators'))


def create_mock_pool(mock_conn):
    """Create a mock asyncpg pool with proper async context manager support."""
    mock_pool = MagicMock()

    @asynccontextmanager
    async def mock_acquire():
        yield mock_conn

    mock_pool.acquire = mock_acquire
    mock_pool.close = AsyncMock()
    return mock_pool


class TestValidationCheck:
    """Tests for ValidationCheck dataclass."""

    def test_validation_check_creation(self):
        """Test creating a ValidationCheck."""
        from data_sourcing.validators.validate_postal_codes import ValidationCheck

        check = ValidationCheck(
            name="Test Check",
            status="pass",
            expected=0,
            actual=0
        )

        assert check.name == "Test Check"
        assert check.status == "pass"
        assert check.expected == 0
        assert check.actual == 0
        assert check.items == []

    def test_validation_check_with_items(self):
        """Test ValidationCheck with items list."""
        from data_sourcing.validators.validate_postal_codes import ValidationCheck

        check = ValidationCheck(
            name="Test Check",
            status="warning",
            expected=0,
            actual=2,
            items=[{'fsa_code': 'ABC'}, {'fsa_code': 'DEF'}]
        )

        assert len(check.items) == 2
        assert check.items[0]['fsa_code'] == 'ABC'


class TestValidationSection:
    """Tests for ValidationSection dataclass."""

    def test_validation_section_creation(self):
        """Test creating a ValidationSection."""
        from data_sourcing.validators.validate_postal_codes import (
            ValidationSection, ValidationCheck
        )

        section = ValidationSection(title="Test Section")
        section.checks.append(ValidationCheck(
            name="Check 1", status="pass", expected=0, actual=0
        ))

        assert section.title == "Test Section"
        assert len(section.checks) == 1
        assert section.summary == {}


class TestValidationReport:
    """Tests for ValidationReport dataclass."""

    def test_validation_report_creation(self):
        """Test creating a ValidationReport."""
        from data_sourcing.validators.validate_postal_codes import ValidationReport

        report = ValidationReport(
            timestamp="2024-01-01T00:00:00",
            source_table="commercial_postal_codes",
            record_count=100
        )

        assert report.source_table == "commercial_postal_codes"
        assert report.record_count == 100
        assert report.sections == []
        assert report.recommendations == []
        assert report.overall_status == 'unknown'


class TestGenerateRecommendations:
    """Tests for recommendation generation."""

    def test_generate_recommendations_for_unmatched_cities(self):
        """Test recommendations generated for unmatched cities."""
        from data_sourcing.validators.validate_postal_codes import (
            generate_recommendations, ValidationReport, ValidationSection
        )

        report = ValidationReport(
            timestamp="2024-01-01",
            source_table="test",
            record_count=100
        )

        section = ValidationSection(title="City-Municipality Alignment")
        section.summary = {'unmatched_cities': 50}
        report.sections.append(section)

        recommendations = generate_recommendations(report)

        assert len(recommendations) >= 1
        assert any('50' in r and 'alias' in r.lower() for r in recommendations)

    def test_generate_recommendations_for_new_fsa_combos(self):
        """Test recommendations for new FSA-city combinations."""
        from data_sourcing.validators.validate_postal_codes import (
            generate_recommendations, ValidationReport, ValidationSection
        )

        report = ValidationReport(
            timestamp="2024-01-01",
            source_table="test",
            record_count=100
        )

        section = ValidationSection(title="FSA-Municipality Mapping Validation")
        section.summary = {'new_combinations': 25}
        report.sections.append(section)

        recommendations = generate_recommendations(report)

        assert len(recommendations) >= 1
        assert any('25' in r and 'FSA' in r for r in recommendations)


class TestGetDataOverview:
    """Tests for get_data_overview function."""

    @pytest.mark.asyncio
    async def test_get_data_overview_returns_counts(self):
        """Test that data overview returns expected counts."""
        from data_sourcing.validators.validate_postal_codes import get_data_overview

        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = {
            'total_records': 298500,
            'unique_fsas': 532,
            'unique_cities_raw': 1228,
            'unique_cities_normalized': 1200,
            'records_with_coords': 298000
        }

        mock_pool = create_mock_pool(mock_conn)

        result = await get_data_overview(mock_pool)

        assert result['total_records'] == 298500
        assert result['unique_fsas'] == 532
        assert result['records_with_coords'] == 298000


class TestValidateFsaCoverage:
    """Tests for FSA coverage validation."""

    @pytest.mark.asyncio
    async def test_fsa_coverage_all_matched(self):
        """Test FSA coverage when all FSAs match."""
        from data_sourcing.validators.validate_postal_codes import validate_fsa_coverage

        mock_conn = AsyncMock()

        # Mock stats query
        mock_conn.fetchrow.return_value = {
            'commercial_fsas': 532,
            'boundary_fsas': 532,
            'matched_fsas': 532
        }

        # Mock fetch queries (no mismatches)
        mock_conn.fetch.return_value = []

        mock_pool = create_mock_pool(mock_conn)

        section = await validate_fsa_coverage(mock_pool)

        assert section.title == "FSA Coverage Analysis"
        assert section.summary['match_rate'] == 100.0
        # All checks should pass
        assert all(c.status == 'pass' for c in section.checks)

    @pytest.mark.asyncio
    async def test_fsa_coverage_with_mismatches(self):
        """Test FSA coverage with some mismatches."""
        from data_sourcing.validators.validate_postal_codes import validate_fsa_coverage

        mock_conn = AsyncMock()

        mock_conn.fetchrow.return_value = {
            'commercial_fsas': 534,
            'boundary_fsas': 532,
            'matched_fsas': 530
        }

        # First fetch: commercial-only FSAs
        # Second fetch: boundary-only FSAs
        mock_conn.fetch.side_effect = [
            [{'fsa_code': 'XXX', 'postal_count': 10}],
            [{'fsa_code': 'YYY', 'province_name': 'Ontario', 'area_km2': 100}]
        ]

        mock_pool = create_mock_pool(mock_conn)

        section = await validate_fsa_coverage(mock_pool)

        # Should have warnings
        assert any(c.status == 'warning' for c in section.checks)
        assert section.summary['matched_fsas'] == 530


class TestValidateLocationAccuracy:
    """Tests for location accuracy validation."""

    @pytest.mark.asyncio
    async def test_location_accuracy_all_good(self):
        """Test location accuracy when all centroids are close."""
        from data_sourcing.validators.validate_postal_codes import validate_location_accuracy

        mock_conn = AsyncMock()

        # Mock distance distribution
        mock_conn.fetch.side_effect = [
            [
                {'band': '< 1 km', 'count': 400},
                {'band': '1-5 km', 'count': 130},
                {'band': '5-10 km', 'count': 2}
            ],
            []  # No large discrepancies
        ]

        mock_pool = create_mock_pool(mock_conn)

        section = await validate_location_accuracy(mock_pool)

        assert section.title == "FSA Location Accuracy"
        assert '< 1 km' in section.summary['distance_distribution']
        # No fail status checks
        assert not any(c.status == 'fail' for c in section.checks)


class TestValidateCityAlignment:
    """Tests for city-municipality alignment validation."""

    @pytest.mark.asyncio
    async def test_city_alignment_partial_match(self):
        """Test city alignment with partial matches."""
        from data_sourcing.validators.validate_postal_codes import validate_city_alignment

        mock_conn = AsyncMock()

        mock_conn.fetchrow.return_value = {
            'commercial_cities': 1000,
            'matched_cities': 800
        }

        mock_conn.fetch.return_value = [
            {'city_raw': 'Acton', 'city_normalized': 'ACTON', 'postal_count': 500, 'fsa_count': 2}
        ]

        mock_pool = create_mock_pool(mock_conn)

        section = await validate_city_alignment(mock_pool)

        assert section.summary['unmatched_cities'] == 200
        assert section.summary['match_rate'] == 80.0
        assert any(c.status == 'warning' for c in section.checks)


class TestValidateDataQuality:
    """Tests for data quality validation."""

    @pytest.mark.asyncio
    async def test_data_quality_all_valid(self):
        """Test data quality when all records are valid."""
        from data_sourcing.validators.validate_postal_codes import validate_data_quality

        mock_conn = AsyncMock()

        mock_conn.fetchrow.return_value = {
            'total': 100000,
            'with_coords': 100000,
            'with_city': 100000,
            'in_ontario_bbox': 100000,
            'outside_ontario': 0,
            'valid_format': 100000
        }

        mock_pool = create_mock_pool(mock_conn)

        section = await validate_data_quality(mock_pool)

        assert section.summary['outside_ontario'] == 0
        assert all(c.status == 'pass' for c in section.checks)

    @pytest.mark.asyncio
    async def test_data_quality_with_issues(self):
        """Test data quality with some issues."""
        from data_sourcing.validators.validate_postal_codes import validate_data_quality

        mock_conn = AsyncMock()

        mock_conn.fetchrow.return_value = {
            'total': 100000,
            'with_coords': 99000,
            'with_city': 99500,
            'in_ontario_bbox': 99990,
            'outside_ontario': 10,
            'valid_format': 99999
        }

        # Mock fetch for outside Ontario records
        mock_conn.fetch.return_value = [
            {'postal_code_raw': 'A1A 1A1', 'fsa_code': 'A1A', 'city': 'Test', 'latitude': 40.0, 'longitude': -80.0}
        ]

        mock_pool = create_mock_pool(mock_conn)

        section = await validate_data_quality(mock_pool)

        assert section.summary['outside_ontario'] == 10
        # Should have warning for outside Ontario
        assert any(c.status == 'warning' for c in section.checks)


class TestReportSerialization:
    """Tests for report JSON serialization."""

    def test_report_to_json(self, tmp_path):
        """Test that report can be serialized to JSON."""
        from data_sourcing.validators.validate_postal_codes import (
            ValidationReport, ValidationSection, ValidationCheck, save_report
        )

        report = ValidationReport(
            timestamp="2024-01-01T00:00:00",
            source_table="commercial_postal_codes",
            record_count=100
        )

        section = ValidationSection(title="Test Section")
        section.checks.append(ValidationCheck(
            name="Test Check",
            status="pass",
            expected=0,
            actual=0
        ))
        section.summary = {'test_metric': 100}
        report.sections.append(section)
        report.recommendations = ["Test recommendation"]
        report.overall_status = "pass"

        output_path = tmp_path / "report.json"
        save_report(report, str(output_path))

        # Verify file was created
        assert output_path.exists()

        # Verify it's valid JSON
        with open(output_path) as f:
            data = json.load(f)

        assert data['source_table'] == 'commercial_postal_codes'
        assert data['record_count'] == 100
        assert len(data['sections']) == 1
        assert data['sections'][0]['title'] == 'Test Section'
