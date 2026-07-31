"""
Tests for engine/address_parser.py — pure functions, no mocking needed.
"""
from engine.address_parser import (
    normalize_postal_code,
    get_postal_prefix,
    normalize_street_suffix,
    normalize_direction,
    parse_address_string,
    parse_address,
    generate_address_hash,
    build_full_address,
)


class TestNormalizePostalCode:
    """Tests for normalize_postal_code()."""

    def test_removes_spaces(self):
        assert normalize_postal_code("M5V 1A1") == "M5V1A1"

    def test_removes_multiple_spaces(self):
        assert normalize_postal_code("M 5 V 1 A 1") == "M5V1A1"

    def test_uppercase(self):
        assert normalize_postal_code("m5v1a1") == "M5V1A1"

    def test_mixed_case_with_space(self):
        assert normalize_postal_code("m5v 1a1") == "M5V1A1"

    def test_none(self):
        assert normalize_postal_code(None) is None

    def test_empty_string(self):
        assert normalize_postal_code("") is None

    def test_already_normalized(self):
        assert normalize_postal_code("M5V1A1") == "M5V1A1"


class TestGetPostalPrefix:
    """Tests for get_postal_prefix()."""

    def test_extracts_first_three(self):
        assert get_postal_prefix("M5V1A1") == "M5V"

    def test_from_spaced_postal(self):
        assert get_postal_prefix("M5V 1A1") == "M5V"

    def test_lowercase(self):
        assert get_postal_prefix("m5v1a1") == "M5V"

    def test_none(self):
        assert get_postal_prefix(None) is None

    def test_empty(self):
        assert get_postal_prefix("") is None

    def test_short_input(self):
        # Should still return what's available
        assert get_postal_prefix("M5") == "M5"


class TestNormalizeStreetSuffix:
    """Tests for normalize_street_suffix()."""

    def test_st_to_street(self):
        assert normalize_street_suffix("ST") == "Street"

    def test_ave_to_avenue(self):
        assert normalize_street_suffix("AVE") == "Avenue"

    def test_rd_to_road(self):
        assert normalize_street_suffix("RD") == "Road"

    def test_dr_to_drive(self):
        assert normalize_street_suffix("DR") == "Drive"

    def test_blvd_to_boulevard(self):
        assert normalize_street_suffix("BLVD") == "Boulevard"

    def test_cres_to_crescent(self):
        assert normalize_street_suffix("CRES") == "Crescent"

    def test_case_insensitive(self):
        assert normalize_street_suffix("st") == "Street"
        assert normalize_street_suffix("Ave") == "Avenue"

    def test_dotted_abbreviations(self):
        assert normalize_street_suffix("Dr.") == "Drive"
        assert normalize_street_suffix("Rd.") == "Road"
        assert normalize_street_suffix("Ave.") == "Avenue"
        assert normalize_street_suffix("St.") == "Street"

    def test_double_dot(self):
        assert normalize_street_suffix("Dr..") == "Drive"

    def test_new_abbreviations(self):
        assert normalize_street_suffix("HWY") == "Highway"
        assert normalize_street_suffix("LI") == "Line"
        assert normalize_street_suffix("BV") == "Boulevard"
        assert normalize_street_suffix("SDRD") == "Sideroad"
        assert normalize_street_suffix("GT") == "Gate"
        assert normalize_street_suffix("PK") == "Park"
        assert normalize_street_suffix("TER") == "Terrace"
        assert normalize_street_suffix("WY") == "Way"
        assert normalize_street_suffix("GR") == "Grove"
        assert normalize_street_suffix("PT") == "Point"

    def test_compound_suffixes(self):
        assert normalize_street_suffix("Side rd") == "Sideroad"
        assert normalize_street_suffix("Side road") == "Sideroad"

    def test_unknown_returns_capitalized(self):
        assert normalize_street_suffix("xyz") == "Xyz"

    def test_none(self):
        assert normalize_street_suffix(None) is None

    def test_empty(self):
        assert normalize_street_suffix("") is None


class TestNormalizeDirection:
    """Tests for normalize_direction()."""

    def test_n_to_north(self):
        assert normalize_direction("N") == "North"

    def test_s_to_south(self):
        assert normalize_direction("S") == "South"

    def test_e_to_east(self):
        assert normalize_direction("E") == "East"

    def test_w_to_west(self):
        assert normalize_direction("W") == "West"

    def test_ne_to_northeast(self):
        assert normalize_direction("NE") == "Northeast"

    def test_nw_to_northwest(self):
        assert normalize_direction("NW") == "Northwest"

    def test_se_to_southeast(self):
        assert normalize_direction("SE") == "Southeast"

    def test_sw_to_southwest(self):
        assert normalize_direction("SW") == "Southwest"

    def test_case_insensitive(self):
        assert normalize_direction("n") == "North"
        assert normalize_direction("ne") == "Northeast"

    def test_full_word(self):
        assert normalize_direction("north") == "North"

    def test_unknown_returns_capitalized(self):
        assert normalize_direction("xyz") == "Xyz"

    def test_none(self):
        assert normalize_direction(None) is None

    def test_empty(self):
        assert normalize_direction("") is None


class TestParseAddressString:
    """Tests for parse_address_string()."""

    def test_full_address(self):
        result = parse_address_string("100 Queen St W, Toronto, ON M5H 2N2")
        assert result['street_number'] == "100"
        assert result['street_name'] == "Queen"
        assert result['city'] == "Toronto"
        assert result['postal_code'] == "M5H 2N2"

    def test_simple_address(self):
        result = parse_address_string("325 Elizabeth Ave, Guelph")
        assert result['street_number'] == "325"
        assert result['street_name'] == "Elizabeth"
        assert result['city'] == "Guelph"

    def test_missing_city(self):
        result = parse_address_string("123 Main St")
        assert result['street_number'] == "123"
        assert result['street_name'] == "Main"
        assert result['city'] is None

    def test_postal_code_extraction(self):
        result = parse_address_string("anywhere M5V1A1 something")
        assert result['postal_code'] == "M5V1A1"

    def test_no_street_number(self):
        result = parse_address_string("Main Street, Toronto")
        assert result['street_number'] is None
        assert result['city'] == "Toronto"

    def test_dotted_suffix_parsed(self):
        """Dotted suffix like 'St.' should be detected as a suffix."""
        result = parse_address_string("123 Main St., Toronto")
        assert result['street_number'] == "123"
        assert result['street_name'] == "Main"
        assert result['street_suffix'] == "St."
        assert result['city'] == "Toronto"

    def test_dotted_direction_parsed(self):
        """Dotted direction should still be detected."""
        result = parse_address_string("123 Main St N., Toronto")
        assert result['street_number'] == "123"
        assert result['street_name'] == "Main"


class TestParseAddress:
    """Tests for parse_address()."""

    def test_idx_format(self):
        addr = {
            'Address': {
                'StreetNumber': '100',
                'StreetName': 'Queen',
                'StreetSuffix': 'St',
                'StreetDirection': 'W',
                'City': 'Toronto',
                'Province': 'ON',
                'PostalCode': 'M5H 2N2'
            }
        }
        result = parse_address(addr)
        assert result['street_number'] == '100'
        assert result['street_name'] == 'Queen'
        assert result['street_suffix'] == 'St'
        assert result['street_direction'] == 'W'
        assert result['city'] == 'Toronto'
        assert result['postal_code'] == 'M5H2N2'
        assert result['postal_prefix'] == 'M5H'
        assert result['street_suffix_normalized'] == 'Street'
        assert result['street_direction_normalized'] == 'West'

    def test_string_address(self):
        result = parse_address("100 Queen St, Toronto, ON M5H 2N2")
        assert result['street_number'] == '100'
        assert result['postal_code'] == 'M5H2N2'

    def test_flat_dict_snake_case(self):
        addr = {
            'street_number': '100',
            'street_name': 'Queen',
            'city': 'Toronto',
            'postal_code': 'M5H 2N2'
        }
        result = parse_address(addr)
        assert result['street_number'] == '100'
        assert result['street_name'] == 'Queen'
        assert result['postal_code'] == 'M5H2N2'

    def test_flat_dict_camel_case(self):
        addr = {
            'streetNumber': '100',
            'streetName': 'Queen',
            'postalCode': 'M5H2N2'
        }
        result = parse_address(addr)
        assert result['street_number'] == '100'
        assert result['street_name'] == 'Queen'
        assert result['postal_code'] == 'M5H2N2'

    def test_dict_with_address_string(self):
        addr = {'address': '100 Queen St, Toronto'}
        result = parse_address(addr)
        assert result['street_number'] == '100'

    def test_pin_extraction(self):
        addr = {'street_number': '100', 'pin': '12345678'}
        result = parse_address(addr)
        assert result['pin'] == '12345678'

    def test_pin_camel_case(self):
        addr = {'street_number': '100', 'PIN': '12345678'}
        result = parse_address(addr)
        assert result['pin'] == '12345678'

    def test_normalization_applied(self):
        addr = {
            'street_number': '100',
            'street_name': '  Queen  ',
            'street_suffix': 'st',
            'street_direction': 'w',
            'postal_code': 'm5h 2n2'
        }
        result = parse_address(addr)
        assert result['street_name'] == 'Queen'
        assert result['postal_code'] == 'M5H2N2'
        assert result['street_suffix_normalized'] == 'Street'
        assert result['street_direction_normalized'] == 'West'


class TestParseAddressSuffixDeduplication:
    """Tests for suffix deduplication in parse_address()."""

    def test_strips_duplicate_suffix_from_street_name(self):
        addr = {
            'street_number': '100',
            'street_name': 'King Edward Ave',
            'street_suffix': 'Ave',
        }
        result = parse_address(addr)
        assert result['street_name'] == 'King Edward'

    def test_strips_duplicate_suffix_different_forms(self):
        addr = {
            'street_number': '200',
            'street_name': 'Maple Street',
            'street_suffix': 'St',
        }
        result = parse_address(addr)
        assert result['street_name'] == 'Maple'

    def test_no_strip_single_word_name(self):
        """Don't strip if street_name is a single word (e.g., 'Avenue' as a street name)."""
        addr = {
            'street_number': '50',
            'street_name': 'Avenue',
            'street_suffix': 'Ave',
        }
        result = parse_address(addr)
        assert result['street_name'] == 'Avenue'

    def test_no_strip_when_no_match(self):
        addr = {
            'street_number': '100',
            'street_name': 'King Edward',
            'street_suffix': 'Ave',
        }
        result = parse_address(addr)
        assert result['street_name'] == 'King Edward'

    def test_no_strip_when_no_suffix(self):
        addr = {
            'street_number': '100',
            'street_name': 'King Edward Ave',
        }
        result = parse_address(addr)
        assert result['street_name'] == 'King Edward Ave'

    def test_strips_suffix_when_direction_is_last_word(self):
        """'King Street North' + suffix 'Street' → 'King North'."""
        addr = {
            'street_number': '100',
            'street_name': 'King Street North',
            'street_suffix': 'Street',
        }
        result = parse_address(addr)
        assert result['street_name'] == 'King North'

    def test_compound_suffix_dedup(self):
        """'County Side Road' + suffix 'SDRD' → 'County'."""
        addr = {
            'street_number': '50',
            'street_name': 'County Side Road',
            'street_suffix': 'SDRD',
        }
        result = parse_address(addr)
        assert result['street_name'] == 'County'

    def test_no_strip_compound_would_leave_empty(self):
        """'Side Road' + suffix 'Sideroad' → no change (only 2 words)."""
        addr = {
            'street_number': '10',
            'street_name': 'Side Road',
            'street_suffix': 'Sideroad',
        }
        result = parse_address(addr)
        assert result['street_name'] == 'Side Road'

    def test_avenue_road_preserved(self):
        """'Avenue Road' + suffix 'Avenue' → no change (last word 'Road' ≠ 'Avenue')."""
        addr = {
            'street_number': '200',
            'street_name': 'Avenue Road',
            'street_suffix': 'Avenue',
        }
        result = parse_address(addr)
        assert result['street_name'] == 'Avenue Road'


class TestGenerateAddressHash:
    """Tests for generate_address_hash()."""

    def test_deterministic(self):
        components = {'street_number': '100', 'street_name': 'Queen', 'city': 'Toronto'}
        hash1 = generate_address_hash(components)
        hash2 = generate_address_hash(components)
        assert hash1 == hash2

    def test_case_insensitive(self):
        comp1 = {'street_name': 'Queen', 'city': 'Toronto'}
        comp2 = {'street_name': 'queen', 'city': 'toronto'}
        assert generate_address_hash(comp1) == generate_address_hash(comp2)

    def test_different_addresses_different_hashes(self):
        comp1 = {'street_name': 'Queen', 'city': 'Toronto'}
        comp2 = {'street_name': 'King', 'city': 'Toronto'}
        assert generate_address_hash(comp1) != generate_address_hash(comp2)

    def test_empty_components(self):
        # Should not raise
        result = generate_address_hash({})
        assert isinstance(result, str)
        assert len(result) == 32  # MD5 hex digest length


class TestBuildFullAddress:
    """Tests for build_full_address()."""

    def test_full_components(self):
        components = {
            'street_number': '100',
            'street_name': 'Queen',
            'street_suffix': 'Street',
            'street_direction': 'West',
            'city': 'Toronto',
            'province': 'ON',
            'postal_code': 'M5H2N2'
        }
        result = build_full_address(components)
        assert result == "100 Queen Street West, Toronto, ON M5H 2N2"

    def test_minimal_components(self):
        components = {'street_number': '100', 'street_name': 'Queen'}
        result = build_full_address(components)
        assert result == "100 Queen"

    def test_with_unit(self):
        components = {
            'unit_no': '5',
            'street_number': '100',
            'street_name': 'Queen',
            'street_suffix': 'St'
        }
        result = build_full_address(components)
        assert result == "Unit 5 100 Queen St"

    def test_postal_formatting(self):
        components = {
            'street_number': '100',
            'street_name': 'Queen',
            'postal_code': 'M5H2N2'
        }
        result = build_full_address(components)
        assert "M5H 2N2" in result

    def test_empty_components(self):
        result = build_full_address({})
        assert result == ""

    def test_city_only(self):
        components = {'city': 'Toronto'}
        result = build_full_address(components)
        assert result == ", Toronto"
