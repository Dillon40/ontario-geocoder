"""Tests for record validator transformer."""
import pytest
from data_sourcing.transformers import (
    validate_postal_code,
    validate_coordinates,
    RecordValidator,
    TransformContext,
    TransformResult,
)


class TestValidatePostalCode:
    """Test the validate_postal_code function."""

    @pytest.mark.parametrize("postal,is_valid", [
        # Valid postal codes
        ("N1E1A1", True),
        ("M5V3L9", True),
        ("K1A0B1", True),
        ("T2N1N4", True),

        # Invalid - wrong letters in first position (D, F, I, O, Q, U, W, Z not allowed)
        ("D1E1A1", False),  # D not allowed in position 1
        ("F1E1A1", False),  # F not allowed in position 1
        ("I1E1A1", False),  # I not allowed in position 1
        ("O1E1A1", False),  # O not allowed in position 1
        ("Q1E1A1", False),  # Q not allowed in position 1
        ("U1E1A1", False),  # U not allowed in position 1
        ("W1E1A1", False),  # W not allowed in position 1
        ("Z1E1A1", False),  # Z not allowed in position 1

        # Invalid - wrong letters in position 3 or 5 (D, F, I, O, Q, U not allowed)
        ("N1D1A1", False),  # D not allowed in position 3
        ("N1E1D1", False),  # D not allowed in position 5

        # Invalid format
        ("N1E1A", False),   # Too short
        ("N1E1A12", False), # Too long
        ("N1E 1A1", False), # Has space (unnormalized)

        # Empty is valid (missing data, not invalid format)
        ("", True),
        (None, True),
    ])
    def test_validate_postal_code(self, postal, is_valid):
        """Test postal code validation."""
        result, error = validate_postal_code(postal)
        assert result == is_valid


class TestValidateCoordinates:
    """Test the validate_coordinates function."""

    @pytest.mark.parametrize("lat,lon,bounds,is_valid", [
        # Valid Ontario coordinates
        (43.6532, -79.3832, 'ontario', True),  # Toronto
        (43.5448, -80.2482, 'ontario', True),  # Guelph
        (45.4215, -75.6972, 'ontario', True),  # Ottawa

        # Out of Ontario bounds
        (40.0, -79.0, 'ontario', False),       # Too far south
        (60.0, -80.0, 'ontario', False),       # Too far north
        (43.0, -65.0, 'ontario', False),       # Too far east
        (43.0, -100.0, 'ontario', False),      # Too far west

        # Valid Canada coordinates
        (49.2827, -123.1207, 'canada', True),  # Vancouver
        (45.5017, -73.5673, 'canada', True),   # Montreal

        # Missing coordinates (valid - handled elsewhere)
        (None, None, 'ontario', True),
        (None, -79.0, 'ontario', True),
    ])
    def test_validate_coordinates(self, lat, lon, bounds, is_valid):
        """Test coordinate validation."""
        result, error = validate_coordinates(lat, lon, bounds)
        assert result == is_valid


class TestRecordValidator:
    """Test the RecordValidator transformer class."""

    def test_valid_record(self):
        """Test validation of a valid record."""
        validator = RecordValidator({'enabled': True})
        context = TransformContext(
            record={
                'latitude': 43.5448,
                'longitude': -80.2482,
                'street_number': '123',
                'postal_code': 'N1E1A1',
            },
            source_name='test'
        )

        result = validator.transform(context)

        assert result == TransformResult.SUCCESS
        assert not context.has_errors

    def test_missing_coordinates_error(self):
        """Test that missing coordinates cause error."""
        validator = RecordValidator({'enabled': True})
        context = TransformContext(
            record={
                'street_number': '123',
            },
            source_name='test'
        )

        result = validator.transform(context)

        assert result == TransformResult.ERROR
        assert context.has_errors

    def test_missing_address_identifier_error(self):
        """Test that missing address identifier causes error."""
        validator = RecordValidator({'enabled': True})
        context = TransformContext(
            record={
                'latitude': 43.5448,
                'longitude': -80.2482,
                # Missing both street_number and full_address
            },
            source_name='test'
        )

        result = validator.transform(context)

        assert result == TransformResult.ERROR
        assert context.has_errors

    def test_out_of_bounds_error(self):
        """Test that out-of-bounds coordinates cause error."""
        validator = RecordValidator({
            'enabled': True,
            'strict_bounds': 'ontario',
        })
        context = TransformContext(
            record={
                'latitude': 40.0,  # Out of Ontario bounds
                'longitude': -79.0,
                'street_number': '123',
            },
            source_name='test'
        )

        result = validator.transform(context)

        assert result == TransformResult.ERROR
        assert context.has_errors

    def test_invalid_postal_code_warning(self):
        """Test that invalid postal code generates warning (not error)."""
        validator = RecordValidator({'enabled': True})
        context = TransformContext(
            record={
                'latitude': 43.5448,
                'longitude': -80.2482,
                'street_number': '123',
                'postal_code': 'INVALID',  # Invalid format
            },
            source_name='test'
        )

        result = validator.transform(context)

        # Postal code issues are warnings, not errors
        assert result == TransformResult.SUCCESS
        assert not context.has_errors
        assert context.has_warnings

    def test_full_address_satisfies_requirement(self):
        """Test that full_address satisfies address identifier requirement."""
        validator = RecordValidator({'enabled': True})
        context = TransformContext(
            record={
                'latitude': 43.5448,
                'longitude': -80.2482,
                'full_address': '123 Main St, Guelph ON',  # No street_number, but has full_address
            },
            source_name='test'
        )

        result = validator.transform(context)

        assert result == TransformResult.SUCCESS
        assert not context.has_errors
