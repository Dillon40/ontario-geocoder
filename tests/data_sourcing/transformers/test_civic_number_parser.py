"""Tests for civic number parser transformer."""
import pytest
from data_sourcing.transformers import (
    parse_civic_number,
    CivicNumberParser,
    TransformContext,
    TransformResult,
)


class TestParseCivicNumber:
    """Test the parse_civic_number function."""

    @pytest.mark.parametrize("input_str,expected", [
        # Letter suffixes attached
        ("100A", ("100", "A")),
        ("22B", ("22", "B")),
        ("15c", ("15", "C")),  # Lowercase normalized to uppercase
        ("999Z", ("999", "Z")),

        # Fractional suffixes
        ("15 1/2", ("15", "1/2")),
        ("100 3/4", ("100", "3/4")),

        # Spaced letter suffixes
        ("100 A", ("100", "A")),
        ("22 B", ("22", "B")),

        # Hyphenated letter suffixes (NOT ranges)
        ("100-A", ("100", "A")),
        ("22-B", ("22", "B")),

        # Simple numbers (no suffix)
        ("100", ("100", None)),
        ("22", ("22", None)),
        ("1", ("1", None)),
        ("9999", ("9999", None)),

        # Address ranges - NOT parsed as suffixes
        ("100-102", ("100-102", None)),
        ("1-5", ("1-5", None)),
        ("200-204", ("200-204", None)),

        # Multi-letter - ambiguous, skip
        ("100AB", ("100AB", None)),
        ("22XY", ("22XY", None)),
    ])
    def test_parse_civic_number(self, input_str, expected):
        """Test parsing various civic number patterns."""
        result = parse_civic_number(input_str)
        assert result == expected

    def test_parse_empty_string(self):
        """Test parsing empty string."""
        result = parse_civic_number("")
        assert result == ("", None)

    def test_parse_none(self):
        """Test parsing None."""
        result = parse_civic_number(None)
        assert result == (None, None)

    def test_parse_whitespace(self):
        """Test parsing string with leading/trailing whitespace."""
        result = parse_civic_number("  100A  ")
        assert result == ("100", "A")


class TestCivicNumberParserTransformer:
    """Test the CivicNumberParser transformer class."""

    def test_parser_extracts_letter_suffix(self):
        """Test parser extracts letter suffix."""
        parser = CivicNumberParser({'enabled': True})
        context = TransformContext(
            record={'street_number': '100A'},
            source_name='test'
        )

        result = parser.transform(context)

        assert result == TransformResult.MODIFIED
        assert context.record['street_number'] == '100'
        assert context.record['civic_no_suffix'] == 'A'

    def test_parser_extracts_fractional_suffix(self):
        """Test parser extracts fractional suffix."""
        parser = CivicNumberParser({'enabled': True})
        context = TransformContext(
            record={'street_number': '15 1/2'},
            source_name='test'
        )

        result = parser.transform(context)

        assert result == TransformResult.MODIFIED
        assert context.record['street_number'] == '15'
        assert context.record['civic_no_suffix'] == '1/2'

    def test_parser_leaves_simple_number(self):
        """Test parser leaves simple numbers unchanged."""
        parser = CivicNumberParser({'enabled': True})
        context = TransformContext(
            record={'street_number': '100'},
            source_name='test'
        )

        result = parser.transform(context)

        assert result == TransformResult.SUCCESS
        assert context.record['street_number'] == '100'
        assert 'civic_no_suffix' not in context.record or context.record.get('civic_no_suffix') is None

    def test_parser_leaves_range_unchanged(self):
        """Test parser leaves address ranges unchanged."""
        parser = CivicNumberParser({'enabled': True})
        context = TransformContext(
            record={'street_number': '100-102'},
            source_name='test'
        )

        result = parser.transform(context)

        assert result == TransformResult.SUCCESS
        assert context.record['street_number'] == '100-102'

    def test_parser_disabled(self):
        """Test parser when disabled."""
        parser = CivicNumberParser({'enabled': False})
        context = TransformContext(
            record={'street_number': '100A'},
            source_name='test'
        )

        result = parser.transform(context)

        assert result == TransformResult.SUCCESS
        assert context.record['street_number'] == '100A'
        assert 'civic_no_suffix' not in context.record

    def test_passthrough_existing_suffix(self):
        """Test that existing suffix is preserved when passthrough enabled."""
        parser = CivicNumberParser({'enabled': True, 'passthrough_suffix': True})
        context = TransformContext(
            record={
                'street_number': '100A',
                'civic_no_suffix': 'B'  # Already has suffix
            },
            source_name='test'
        )

        result = parser.transform(context)

        # Should skip parsing because suffix already exists
        assert result == TransformResult.SUCCESS
        assert context.record['street_number'] == '100A'
        assert context.record['civic_no_suffix'] == 'B'

    def test_modification_tracking(self):
        """Test that modifications are tracked."""
        parser = CivicNumberParser({'enabled': True})
        context = TransformContext(
            record={'street_number': '100A'},
            source_name='test'
        )

        parser.transform(context)

        assert context.was_modified
        assert 'street_number' in context.modified_fields
        assert 'civic_no_suffix' in context.modified_fields

    def test_custom_source_field(self):
        """Test parser with custom source field."""
        parser = CivicNumberParser({
            'enabled': True,
            'source_field': 'civic_no'
        })
        context = TransformContext(
            record={'civic_no': '55B'},
            source_name='test'
        )

        result = parser.transform(context)

        assert result == TransformResult.MODIFIED
        assert context.record['civic_no'] == '55'
        assert context.record['civic_no_suffix'] == 'B'
