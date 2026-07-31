"""Tests for unit number parser transformer."""
import pytest
from data_sourcing.transformers import (
    parse_unit_number,
    strip_unit_label,
    UnitNumberParser,
    TransformContext,
    TransformResult,
)


class TestParseUnitNumber:
    """Test the parse_unit_number function."""

    @pytest.mark.parametrize("input_str,expected_unit,expected_found", [
        # Labeled prefix formats
        ("Unit 5 - 123 Main St", "5", True),
        ("Suite 200 - 123 Main St", "200", True),
        ("Apt 3B - 123 Main St", "3B", True),
        ("Apt. 3B - 123 Main St", "3B", True),
        ("APT 10 - 123 Main St", "10", True),
        ("Apartment 10A - 123 Main St", "10A", True),
        ("#5 - 123 Main St", "5", True),
        ("STE 100 - 123 Main St", "100", True),
        ("Ste. 100 - 123 Main St", "100", True),
        ("Unit A - 123 Main St", "A", True),
        ("Suite B - 123 Main St", "B", True),

        # Labeled suffix formats
        ("123 Main St Unit 5", "5", True),
        ("123 Main St, Suite 200", "200", True),
        ("123 Main St, Apt. 3B", "3B", True),
        ("123 Main St Apartment 10A", "10A", True),
        ("123 Main St #5", "5", True),
        ("123 Main St, Unit A", "A", True),

        # No unit number
        ("123 Main St", None, False),
        ("456 Oak Ave", None, False),
        ("789 Elm Blvd", None, False),

        # Edge cases - addresses without recognizable unit patterns
        ("100-102 Main St", None, False),  # Address range, not unit
        ("100A Main St", None, False),  # Civic suffix, not unit
    ])
    def test_parse_unit_number(self, input_str, expected_unit, expected_found):
        """Test parsing various unit number patterns."""
        result_unit, result_found = parse_unit_number(input_str)
        assert result_unit == expected_unit
        assert result_found == expected_found

    def test_parse_empty_string(self):
        """Test parsing empty string."""
        result = parse_unit_number("")
        assert result == (None, False)

    def test_parse_none(self):
        """Test parsing None."""
        result = parse_unit_number(None)
        assert result == (None, False)

    def test_parse_whitespace(self):
        """Test parsing string with leading/trailing whitespace."""
        result = parse_unit_number("  Unit 5 - 123 Main St  ")
        assert result == ("5", True)

    @pytest.mark.parametrize("input_str,expected_unit", [
        # Hyphenated format (unit-street)
        ("5-123 Main St", "5"),
        ("A-123 Main St", "A"),
        ("3B-123 Main St", "3B"),
        ("12-456 Oak Ave", "12"),
    ])
    def test_parse_hyphenated_enabled(self, input_str, expected_unit):
        """Test hyphenated format parsing when enabled."""
        result_unit, result_found = parse_unit_number(input_str, parse_hyphenated=True)
        assert result_unit == expected_unit
        assert result_found is True

    @pytest.mark.parametrize("input_str", [
        "5-123 Main St",
        "A-123 Main St",
        "12-456 Oak Ave",
    ])
    def test_parse_hyphenated_disabled(self, input_str):
        """Test hyphenated format not parsed when disabled (default)."""
        result_unit, result_found = parse_unit_number(input_str, parse_hyphenated=False)
        assert result_found is False


class TestStripUnitLabel:
    """Test the strip_unit_label function."""

    @pytest.mark.parametrize("input_str,expected", [
        ("Unit 5", "5"),
        ("Suite 200", "200"),
        ("Apt 3B", "3B"),
        ("Apt. 3B", "3B"),
        ("Apartment 10A", "10A"),
        ("#5", "5"),
        ("STE 100", "100"),
        ("Ste. 100", "100"),
        # Already stripped
        ("5", "5"),
        ("200", "200"),
        ("3B", "3B"),
        # With extra formatting
        ("Unit #5", "5"),
        ("Suite - 200", "200"),
    ])
    def test_strip_unit_label(self, input_str, expected):
        """Test stripping unit labels."""
        result = strip_unit_label(input_str)
        assert result == expected

    def test_strip_empty_string(self):
        """Test stripping empty string."""
        result = strip_unit_label("")
        assert result is None

    def test_strip_none(self):
        """Test stripping None."""
        result = strip_unit_label(None)
        assert result is None


class TestUnitNumberParserTransformer:
    """Test the UnitNumberParser transformer class."""

    def test_parser_extracts_prefix_unit(self):
        """Test parser extracts unit from prefix position."""
        parser = UnitNumberParser({
            'enabled': True,
            'source_field': 'full_address'
        })
        context = TransformContext(
            record={'full_address': 'Unit 5 - 123 Main St'},
            source_name='test'
        )

        result = parser.transform(context)

        assert result == TransformResult.MODIFIED
        assert context.record['unit_no'] == '5'
        # Source field should be preserved
        assert context.record['full_address'] == 'Unit 5 - 123 Main St'

    def test_parser_extracts_suffix_unit(self):
        """Test parser extracts unit from suffix position."""
        parser = UnitNumberParser({
            'enabled': True,
            'source_field': 'full_address'
        })
        context = TransformContext(
            record={'full_address': '123 Main St, Suite 200'},
            source_name='test'
        )

        result = parser.transform(context)

        assert result == TransformResult.MODIFIED
        assert context.record['unit_no'] == '200'

    def test_parser_no_unit_found(self):
        """Test parser when no unit pattern found."""
        parser = UnitNumberParser({
            'enabled': True,
            'source_field': 'full_address'
        })
        context = TransformContext(
            record={'full_address': '123 Main St'},
            source_name='test'
        )

        result = parser.transform(context)

        assert result == TransformResult.SUCCESS
        assert 'unit_no' not in context.record or context.record.get('unit_no') is None

    def test_parser_disabled(self):
        """Test parser when disabled."""
        parser = UnitNumberParser({'enabled': False})
        context = TransformContext(
            record={'full_address': 'Unit 5 - 123 Main St'},
            source_name='test'
        )

        result = parser.transform(context)

        assert result == TransformResult.SUCCESS
        assert 'unit_no' not in context.record

    def test_passthrough_existing_unit(self):
        """Test that existing unit is preserved when passthrough enabled."""
        parser = UnitNumberParser({
            'enabled': True,
            'source_field': 'full_address',
            'passthrough_unit': True
        })
        context = TransformContext(
            record={
                'full_address': 'Unit 5 - 123 Main St',
                'unit_no': '10'  # Already has unit
            },
            source_name='test'
        )

        result = parser.transform(context)

        # Should skip parsing because unit already exists
        assert result == TransformResult.SUCCESS
        assert context.record['unit_no'] == '10'

    def test_passthrough_strips_labels(self):
        """Test that passthrough mode strips labels from existing unit."""
        parser = UnitNumberParser({
            'enabled': True,
            'source_field': 'full_address',
            'passthrough_unit': True,
            'strip_labels': True
        })
        context = TransformContext(
            record={
                'full_address': '123 Main St',
                'unit_no': 'Unit 10'  # Has label
            },
            source_name='test'
        )

        result = parser.transform(context)

        assert result == TransformResult.MODIFIED
        assert context.record['unit_no'] == '10'  # Label stripped

    def test_hyphenated_disabled_by_default(self):
        """Test that hyphenated parsing is disabled by default."""
        parser = UnitNumberParser({
            'enabled': True,
            'source_field': 'full_address'
        })
        context = TransformContext(
            record={'full_address': '5-123 Main St'},
            source_name='test'
        )

        result = parser.transform(context)

        assert result == TransformResult.SUCCESS
        assert 'unit_no' not in context.record or context.record.get('unit_no') is None

    def test_hyphenated_enabled(self):
        """Test hyphenated parsing when enabled."""
        parser = UnitNumberParser({
            'enabled': True,
            'source_field': 'full_address',
            'parse_hyphenated': True
        })
        context = TransformContext(
            record={'full_address': '5-123 Main St'},
            source_name='test'
        )

        result = parser.transform(context)

        assert result == TransformResult.MODIFIED
        assert context.record['unit_no'] == '5'

    def test_modification_tracking(self):
        """Test that modifications are tracked."""
        parser = UnitNumberParser({
            'enabled': True,
            'source_field': 'full_address'
        })
        context = TransformContext(
            record={'full_address': 'Unit 5 - 123 Main St'},
            source_name='test'
        )

        parser.transform(context)

        assert context.was_modified
        assert 'unit_no' in context.modified_fields

    def test_custom_source_field(self):
        """Test parser with custom source field."""
        parser = UnitNumberParser({
            'enabled': True,
            'source_field': 'address'
        })
        context = TransformContext(
            record={'address': 'Suite 55 - 100 Oak Ave'},
            source_name='test'
        )

        result = parser.transform(context)

        assert result == TransformResult.MODIFIED
        assert context.record['unit_no'] == '55'

    def test_parser_preserves_source_field(self):
        """Test that parser does not modify source field."""
        original_address = 'Unit 5 - 123 Main St'
        parser = UnitNumberParser({
            'enabled': True,
            'source_field': 'full_address'
        })
        context = TransformContext(
            record={'full_address': original_address},
            source_name='test'
        )

        parser.transform(context)

        # Source field should remain unchanged
        assert context.record['full_address'] == original_address
        # Only unit_no should be in modified_fields
        assert 'full_address' not in context.modified_fields
