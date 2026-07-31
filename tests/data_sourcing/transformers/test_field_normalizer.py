"""Tests for field normalizer transformer."""
import pytest
from data_sourcing.transformers import (
    normalize_street_suffix,
    normalize_direction,
    normalize_postal_code,
    normalize_province,
    normalize_city,
    normalize_unit_number,
    normalize_civic_suffix,
    FieldNormalizer,
    TransformContext,
    TransformResult,
)


class TestNormalizeSuffix:
    """Test the normalize_street_suffix function."""

    @pytest.mark.parametrize("input_val,expected", [
        # Abbreviations
        ("ST", "Street"),
        ("STR", "Street"),
        ("AVE", "Avenue"),
        ("AV", "Avenue"),
        ("RD", "Road"),
        ("DR", "Drive"),
        ("CRES", "Crescent"),
        ("CR", "Crescent"),
        ("CRT", "Court"),
        ("CT", "Court"),
        ("PL", "Place"),
        ("BLVD", "Boulevard"),
        ("LANE", "Lane"),
        ("LN", "Lane"),
        ("PKY", "Parkway"),
        ("PKWY", "Parkway"),
        ("TERR", "Terrace"),
        ("WAY", "Way"),

        # Dotted abbreviations
        ("Dr.", "Drive"),
        ("Rd.", "Road"),
        ("Ave.", "Avenue"),
        ("St.", "Street"),
        ("Dr..", "Drive"),

        # New abbreviations
        ("HWY", "Highway"),
        ("HY", "Highway"),
        ("LI", "Line"),
        ("BV", "Boulevard"),
        ("SDRD", "Sideroad"),
        ("SR", "Sideroad"),
        ("GT", "Gate"),
        ("PK", "Park"),
        ("CON", "Concession"),
        ("CL", "Close"),
        ("TER", "Terrace"),
        ("WY", "Way"),
        ("GR", "Grove"),
        ("WDS", "Woods"),
        ("GRN", "Green"),
        ("PT", "Point"),
        ("CIRC", "Circuit"),
        ("CRCT", "Circuit"),
        ("TL", "Trail"),
        ("TR", "Trail"),
        ("PTWY", "Pathway"),
        ("PROM", "Promenade"),
        ("LANDNG", "Landing"),
        ("LKOUT", "Lookout"),
        ("TLINE", "Townline"),

        # Compound suffixes
        ("Side rd", "Sideroad"),
        ("Side road", "Sideroad"),

        # Case variations
        ("st", "Street"),
        ("St", "Street"),
        ("ave", "Avenue"),

        # Already full form
        ("Street", "Street"),
        ("Avenue", "Avenue"),

        # Null-like values
        ("None", None),
        ("none", None),
        ("", None),
        (None, None),
    ])
    def test_normalize_suffix(self, input_val, expected):
        """Test suffix normalization."""
        result = normalize_street_suffix(input_val)
        assert result == expected


class TestNormalizeDirection:
    """Test the normalize_direction function."""

    @pytest.mark.parametrize("input_val,expected", [
        # Cardinal directions
        ("N", "North"),
        ("S", "South"),
        ("E", "East"),
        ("W", "West"),

        # Compound directions
        ("NE", "Northeast"),
        ("NW", "Northwest"),
        ("SE", "Southeast"),
        ("SW", "Southwest"),

        # Case variations
        ("n", "North"),
        ("north", "North"),
        ("NORTH", "North"),

        # Null-like values (Toronto uses "None")
        ("None", None),
        ("none", None),
        ("", None),
        (None, None),
    ])
    def test_normalize_direction(self, input_val, expected):
        """Test direction normalization."""
        result = normalize_direction(input_val)
        assert result == expected


class TestNormalizePostalCode:
    """Test the normalize_postal_code function."""

    @pytest.mark.parametrize("input_val,expected", [
        # With space
        ("N1E 1A1", "N1E1A1"),
        ("M5V 3L9", "M5V3L9"),

        # Without space
        ("N1E1A1", "N1E1A1"),

        # Lowercase
        ("n1e1a1", "N1E1A1"),
        ("n1e 1a1", "N1E1A1"),

        # Multiple spaces
        ("N1E  1A1", "N1E1A1"),

        # Null-like
        ("", None),
        (None, None),
    ])
    def test_normalize_postal_code(self, input_val, expected):
        """Test postal code normalization."""
        result = normalize_postal_code(input_val)
        assert result == expected


class TestNormalizeProvince:
    """Test the normalize_province function."""

    @pytest.mark.parametrize("input_val,expected", [
        # Full names
        ("ontario", "ON"),
        ("Ontario", "ON"),
        ("ONTARIO", "ON"),
        ("british columbia", "BC"),
        ("quebec", "QC"),
        ("alberta", "AB"),

        # Already abbreviated
        ("ON", "ON"),
        ("on", "ON"),
        ("BC", "BC"),

        # Null-like
        ("", None),
        (None, None),
    ])
    def test_normalize_province(self, input_val, expected):
        """Test province normalization."""
        result = normalize_province(input_val)
        assert result == expected


class TestNormalizeCity:
    """Test the normalize_city function."""

    @pytest.mark.parametrize("input_val,expected", [
        # Case normalization
        ("TORONTO", "Toronto"),
        ("toronto", "Toronto"),
        ("guelph", "Guelph"),

        # Trim whitespace
        ("  Toronto  ", "Toronto"),
        ("  guelph", "Guelph"),

        # Null-like
        ("", None),
        (None, None),
    ])
    def test_normalize_city(self, input_val, expected):
        """Test city normalization."""
        result = normalize_city(input_val)
        assert result == expected


class TestFieldNormalizerTransformer:
    """Test the FieldNormalizer transformer class."""

    def test_normalizes_all_fields(self):
        """Test that all fields are normalized."""
        normalizer = FieldNormalizer({'enabled': True})
        context = TransformContext(
            record={
                'street_suffix': 'ST',
                'street_direction': 'N',
                'postal_code': 'n1e 1a1',
                'province': 'ontario',
                'city': '  GUELPH  ',
            },
            source_name='test'
        )

        result = normalizer.transform(context)

        assert result == TransformResult.MODIFIED
        assert context.record['street_suffix'] == 'Street'
        assert context.record['street_direction'] == 'North'
        assert context.record['postal_code'] == 'N1E1A1'
        assert context.record['fsa_code'] == 'N1E'
        assert context.record['province'] == 'ON'
        assert context.record['city'] == 'Guelph'

    def test_clears_none_direction(self):
        """Test that 'None' direction is cleared (Toronto data)."""
        normalizer = FieldNormalizer({'enabled': True})
        context = TransformContext(
            record={'street_direction': 'None'},
            source_name='test'
        )

        normalizer.transform(context)

        assert context.record['street_direction'] is None

    def test_no_modification_if_already_normalized(self):
        """Test no modification when fields already normalized."""
        normalizer = FieldNormalizer({'enabled': True})
        context = TransformContext(
            record={
                'street_suffix': 'Street',
                'street_direction': 'North',
                'province': 'ON',
            },
            source_name='test'
        )

        result = normalizer.transform(context)

        assert result == TransformResult.SUCCESS
        assert not context.was_modified


class TestNormalizeUnitNumber:
    """Test the normalize_unit_number function."""

    @pytest.mark.parametrize("input_val,strip_labels,expected", [
        # Basic unit numbers
        ("5", False, "5"),
        ("200", False, "200"),
        ("3B", False, "3B"),

        # With labels - not stripped
        ("Unit 5", False, "Unit 5"),
        ("Suite 200", False, "Suite 200"),
        ("Apt 3B", False, "Apt 3B"),

        # With labels - stripped
        ("Unit 5", True, "5"),
        ("UNIT 5", True, "5"),
        ("Suite 200", True, "200"),
        ("SUITE 200", True, "200"),
        ("Apt 3B", True, "3B"),
        ("APT 3B", True, "3B"),
        ("APT. 3B", True, "3B"),
        ("Apartment 10", True, "10"),
        ("#5", True, "5"),
        ("Unit #5", True, "5"),

        # Whitespace handling
        ("  5  ", False, "5"),
        ("  Unit 5  ", True, "5"),

        # Null-like values
        ("None", False, None),
        ("none", False, None),
        ("", False, None),
        (None, False, None),
    ])
    def test_normalize_unit_number(self, input_val, strip_labels, expected):
        """Test unit number normalization."""
        result = normalize_unit_number(input_val, strip_labels)
        assert result == expected


class TestNormalizeCivicSuffix:
    """Test the normalize_civic_suffix function."""

    @pytest.mark.parametrize("input_val,expected", [
        # Letter suffixes
        ("a", "A"),
        ("A", "A"),
        ("b", "B"),
        ("z", "Z"),

        # Fractional suffixes (preserved)
        ("1/2", "1/2"),
        ("3/4", "3/4"),

        # Already uppercase
        ("B", "B"),

        # Whitespace
        ("  a  ", "A"),

        # Null-like values
        ("None", None),
        ("none", None),
        ("", None),
        (None, None),
    ])
    def test_normalize_civic_suffix(self, input_val, expected):
        """Test civic suffix normalization."""
        result = normalize_civic_suffix(input_val)
        assert result == expected


class TestFieldNormalizerWithNewFields:
    """Test FieldNormalizer with unit_no and civic_no_suffix."""

    def test_normalizes_unit_number(self):
        """Test that unit_no is normalized."""
        normalizer = FieldNormalizer({'enabled': True})
        context = TransformContext(
            record={'unit_no': '  5  '},
            source_name='test'
        )

        result = normalizer.transform(context)

        assert result == TransformResult.MODIFIED
        assert context.record['unit_no'] == '5'

    def test_normalizes_civic_suffix(self):
        """Test that civic_no_suffix is normalized."""
        normalizer = FieldNormalizer({'enabled': True})
        context = TransformContext(
            record={'civic_no_suffix': 'a'},
            source_name='test'
        )

        result = normalizer.transform(context)

        assert result == TransformResult.MODIFIED
        assert context.record['civic_no_suffix'] == 'A'

    def test_strips_unit_labels_when_configured(self):
        """Test unit label stripping with config."""
        normalizer = FieldNormalizer({
            'enabled': True,
            'strip_unit_labels': True
        })
        context = TransformContext(
            record={'unit_no': 'Unit 5'},
            source_name='test'
        )

        normalizer.transform(context)

        assert context.record['unit_no'] == '5'


class TestLouthQualifier:
    """Test handling of Niagara 'louth' geographic qualifier."""

    def test_street_louth_moved_to_name(self):
        """Test that 'Street louth' strips qualifier and appends to name."""
        normalizer = FieldNormalizer({'enabled': True})
        context = TransformContext(
            record={
                'street_name': 'Main',
                'street_suffix': 'Street louth',
            },
            source_name='test'
        )

        result = normalizer.transform(context)

        assert result == TransformResult.MODIFIED
        assert context.record['street_name'] == 'Main Louth'
        assert context.record['street_suffix'] == 'Street'

    def test_avenue_louth_moved_to_name(self):
        """Test that 'Avenue louth' strips qualifier and appends to name."""
        normalizer = FieldNormalizer({'enabled': True})
        context = TransformContext(
            record={
                'street_name': 'King',
                'street_suffix': 'Avenue louth',
            },
            source_name='test'
        )

        result = normalizer.transform(context)

        assert result == TransformResult.MODIFIED
        assert context.record['street_name'] == 'King Louth'
        assert context.record['street_suffix'] == 'Avenue'

    def test_no_louth_no_change(self):
        """Test that suffixes without 'louth' are not affected."""
        normalizer = FieldNormalizer({'enabled': True})
        context = TransformContext(
            record={
                'street_name': 'Main',
                'street_suffix': 'Street',
            },
            source_name='test'
        )

        result = normalizer.transform(context)

        assert result == TransformResult.SUCCESS
        assert context.record['street_name'] == 'Main'
        assert context.record['street_suffix'] == 'Street'


class TestDroppedMigration26Parity:
    """Parity guard for dropped SQL migration 26 (street_suffix mass UPDATE).

    Migration 26 in database/migrations.sql was removed without ever being
    applied: suffix normalization happens at import time via FieldNormalizer
    (this module) and at query time via engine.address_parser, both reading
    the shared mapping in config/street-type-mappings.json. These tests pin
    every mapping the SQL CASE encoded, so dropping the migration loses
    nothing.
    """

    # Exact (input, canonical) pairs from migration 26's CASE expression.
    MIGRATION_26_CASE_PAIRS = [
        ("ST", "Street"),
        ("STR", "Street"),
        ("STREET", "Street"),
        ("AVE", "Avenue"),
        ("AV", "Avenue"),
        ("AVENUE", "Avenue"),
        ("RD", "Road"),
        ("ROAD", "Road"),
        ("DR", "Drive"),
        ("DRIVE", "Drive"),
        ("CRES", "Crescent"),
        ("CR", "Crescent"),
        ("CRESCENT", "Crescent"),
        ("CRT", "Court"),
        ("CT", "Court"),
        ("COURT", "Court"),
        ("PL", "Place"),
        ("PLACE", "Place"),
        ("BLVD", "Boulevard"),
        ("BV", "Boulevard"),
        ("BOULEVARD", "Boulevard"),
        ("LN", "Lane"),
        ("LANE", "Lane"),
        ("PKY", "Parkway"),
        ("PKWY", "Parkway"),
        ("PARKWAY", "Parkway"),
        ("TERR", "Terrace"),
        ("TER", "Terrace"),
        ("TERRACE", "Terrace"),
        ("WAY", "Way"),
        ("WY", "Way"),
        ("CIR", "Circle"),
        ("CRCL", "Circle"),
        ("CIRCLE", "Circle"),
        ("CIRC", "Circuit"),
        ("CRCT", "Circuit"),
        ("CIRCUIT", "Circuit"),
        ("TRL", "Trail"),
        ("TL", "Trail"),
        ("TR", "Trail"),
        ("TRAIL", "Trail"),
        ("HTS", "Heights"),
        ("HEIGHTS", "Heights"),
        ("GRV", "Grove"),
        ("GR", "Grove"),
        ("GROVE", "Grove"),
        ("GDNS", "Gardens"),
        ("GARDENS", "Gardens"),
        ("GD", "Garden"),
        ("GARDEN", "Garden"),
        ("SQ", "Square"),
        ("SQUARE", "Square"),
        ("HWY", "Highway"),
        ("HY", "Highway"),
        ("HIGHWAY", "Highway"),
        ("LI", "Line"),
        ("LINE", "Line"),
        ("SR", "Sideroad"),
        ("SDRD", "Sideroad"),
        ("SIDEROAD", "Sideroad"),
        ("CON", "Concession"),
        ("CONCESSION", "Concession"),
        ("CL", "Close"),
        ("CLOSE", "Close"),
        ("XING", "Crossing"),
        ("CROSSING", "Crossing"),
        ("LANDNG", "Landing"),
        ("LANDING", "Landing"),
        ("LKOUT", "Lookout"),
        ("LOOKOUT", "Lookout"),
        ("PK", "Park"),
        ("PARK", "Park"),
        ("PTWY", "Pathway"),
        ("PATHWAY", "Pathway"),
        ("PROM", "Promenade"),
        ("PROMENADE", "Promenade"),
        ("PT", "Point"),
        ("POINT", "Point"),
        ("WDS", "Woods"),
        ("WOODS", "Woods"),
        ("GRN", "Green"),
        ("GREEN", "Green"),
        ("ORCH", "Orchard"),
        ("ORCHARD", "Orchard"),
        ("LWN", "Lawn"),
        ("LAWN", "Lawn"),
        ("TLINE", "Townline"),
        ("TOWNLINE", "Townline"),
        ("GT", "Gate"),
        ("GATE", "Gate"),
        ("COMMON", "Common"),
        ("CMNS", "Common"),
        ("HILL", "Hill"),
        ("GLADE", "Glade"),
        ("WALK", "Walk"),
        ("MEWS", "Mews"),
        ("PATH", "Path"),
        ("WOOD", "Wood"),
        ("COVE", "Cove"),
        ("RIDGE", "Ridge"),
        ("CORNERS", "Corners"),
        ("CRNRS", "Corners"),
    ]

    @pytest.mark.parametrize("input_val,expected", MIGRATION_26_CASE_PAIRS)
    def test_sql_case_mapping_preserved(self, input_val, expected):
        assert normalize_street_suffix(input_val) == expected

    @pytest.mark.parametrize("input_val,expected", [
        # Migration 26 step 2: compound suffixes
        ("SIDE RD", "Sideroad"),
        ("SIDE ROAD", "Sideroad"),
        ("PRIVATE ROAD", "Private Road"),
        ("PRIVATE RD", "Private Road"),
        # Migration 26 step 3: trailing dots stripped before mapping
        ("Rd.", "Road"),
        ("AVE.", "Avenue"),
        ("cres.", "Crescent"),
    ])
    def test_compound_and_dot_handling_preserved(self, input_val, expected):
        assert normalize_street_suffix(input_val) == expected

    def test_louth_qualifier_preserved(self):
        """Migration 26 step 1: 'louth' qualifier moves to street_name.

        Full coverage lives in TestLouthQualifier; this test pins parity
        with the dropped migration explicitly.
        """
        normalizer = FieldNormalizer({'enabled': True})
        context = TransformContext(
            record={'street_name': 'Seventh', 'street_suffix': 'Street louth'},
            source_name='test'
        )
        normalizer.transform(context)
        assert context.record['street_name'] == 'Seventh Louth'
        assert context.record['street_suffix'] == 'Street'


class TestEngineEtlSuffixParity:
    """Drift guard between the two normalize_street_suffix implementations.

    The ETL (data_sourcing.transformers.field_normalizer) and the engine
    (engine.address_parser) each carry a thin implementation because the
    Docker images cannot import each other (Dockerfile.etl ships only
    data_sourcing/ + config/; the API image ships engine/ + config/). The
    single source of truth is config/street-type-mappings.json. Assert both
    implementations agree on every key that mapping defines.
    """

    def test_implementations_agree_on_all_mapping_keys(self):
        import json
        from pathlib import Path

        from engine.address_parser import (
            normalize_street_suffix as engine_normalize_street_suffix,
        )

        config_path = (
            Path(__file__).resolve().parents[3]
            / 'config' / 'street-type-mappings.json'
        )
        with open(config_path) as f:
            mappings = json.load(f)

        keys = set(mappings['abbreviations'])
        keys.update(mappings.get('compound_abbreviations', {}))
        for variants in mappings['variants'].values():
            keys.update(v.upper() for v in variants)

        for key in sorted(keys):
            etl_result = normalize_street_suffix(key)
            engine_result = engine_normalize_street_suffix(key)
            assert etl_result == engine_result, (
                f"Implementations diverge for {key!r}: "
                f"ETL={etl_result!r} engine={engine_result!r}"
            )
