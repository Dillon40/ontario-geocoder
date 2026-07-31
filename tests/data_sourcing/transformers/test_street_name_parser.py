"""Tests for street name parser transformer."""
import pytest
from data_sourcing.transformers import (
    parse_street_name,
    StreetNameParser,
    TransformContext,
    TransformResult,
)


class TestParseStreetName:
    """Test the parse_street_name function."""

    @pytest.mark.parametrize("input_str,expected", [
        # Basic street name parsing
        ("CHURCHILL AVE N", ("Churchill", "Avenue", "North")),
        ("BANK ST", ("Bank", "Street", None)),
        ("KING GEORGE BLVD S", ("King George", "Boulevard", "South")),
        ("MAIN ST E", ("Main", "Street", "East")),
        ("QUEEN ST W", ("Queen", "Street", "West")),

        # Compound directions
        ("RIVER RD NE", ("River", "Road", "Northeast")),
        ("SUNSET DR SW", ("Sunset", "Drive", "Southwest")),

        # Various suffixes
        ("OAK CRES", ("Oak", "Crescent", None)),
        ("PINE CRT", ("Pine", "Court", None)),
        ("MAPLE PL", ("Maple", "Place", None)),
        ("ELM LANE", ("Elm", "Lane", None)),
        ("CEDAR PKWY", ("Cedar", "Parkway", None)),
        ("BIRCH TERR", ("Birch", "Terrace", None)),
        ("WILLOW WAY", ("Willow", "Way", None)),

        # Numbered streets
        ("5TH AVE", ("5th", "Avenue", None)),
        ("1ST ST N", ("1st", "Street", "North")),
        ("22ND AVE S", ("22nd", "Avenue", "South")),

        # No suffix (just street name)
        ("BROADWAY", ("Broadway", None, None)),
    ])
    def test_parse_street_name(self, input_str, expected):
        """Test parsing various street name patterns."""
        result = parse_street_name(input_str)
        assert result == expected

    def test_parse_empty_string(self):
        """Test parsing empty string."""
        result = parse_street_name("")
        assert result == (None, None, None)

    def test_parse_none(self):
        """Test parsing None."""
        result = parse_street_name(None)
        assert result == (None, None, None)

    def test_case_normalization(self):
        """Test that output is properly title-cased."""
        name, suffix, direction = parse_street_name("UPPER CANADA ST N")
        assert name == "Upper Canada"
        assert suffix == "Street"
        assert direction == "North"


class TestStreetNameParserTransformer:
    """Test the StreetNameParser transformer class."""

    def test_parser_enabled(self):
        """Test parser when enabled."""
        parser = StreetNameParser({'enabled': True})
        context = TransformContext(
            record={'street_name': 'CHURCHILL AVE N'},
            source_name='test'
        )

        result = parser.transform(context)

        assert result == TransformResult.MODIFIED
        assert context.record['street_name'] == 'Churchill'
        assert context.record['street_suffix'] == 'Avenue'
        assert context.record['street_direction'] == 'North'

    def test_parser_disabled(self):
        """Test parser when disabled."""
        parser = StreetNameParser({'enabled': False})
        context = TransformContext(
            record={'street_name': 'CHURCHILL AVE N'},
            source_name='test'
        )

        result = parser.transform(context)

        assert result == TransformResult.SUCCESS
        assert context.record['street_name'] == 'CHURCHILL AVE N'
        assert 'street_suffix' not in context.record

    def test_preserves_original(self):
        """Test that original value is preserved."""
        parser = StreetNameParser({'enabled': True, 'preserve_original': True})
        context = TransformContext(
            record={'street_name': 'CHURCHILL AVE N'},
            source_name='test'
        )

        parser.transform(context)

        assert context.record['street_name_original'] == 'CHURCHILL AVE N'

    def test_no_overwrite_existing(self):
        """Test that existing suffix/direction not overwritten by default."""
        parser = StreetNameParser({'enabled': True, 'overwrite_existing': False})
        context = TransformContext(
            record={
                'street_name': 'CHURCHILL AVE N',
                'street_suffix': 'Street',  # Already has suffix
            },
            source_name='test'
        )

        result = parser.transform(context)

        assert result == TransformResult.SUCCESS
        assert context.record['street_suffix'] == 'Street'  # Unchanged

    def test_modification_tracking(self):
        """Test that modifications are tracked."""
        parser = StreetNameParser({'enabled': True})
        context = TransformContext(
            record={'street_name': 'BANK ST'},
            source_name='test'
        )

        parser.transform(context)

        assert context.was_modified
        assert len(context.modifications) >= 2  # street_name and street_suffix
        assert 'street_name' in context.modified_fields
        assert 'street_suffix' in context.modified_fields
