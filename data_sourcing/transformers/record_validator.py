"""
Record validator transformer.

Validates address records for required fields and data quality:
- Required field groups (at least one per group)
- Coordinate bounds checking
- Canadian postal code format validation
"""
import re
from typing import Any, Dict, List, Optional

from .base import BaseTransformer, TransformContext, TransformResult


# Coordinate bounds for Canadian provinces/territories
COORDINATE_BOUNDS = {
    'ontario': {
        'lat_min': 41.5,
        'lat_max': 56.9,
        'lon_min': -95.2,
        'lon_max': -74.3,
    },
    'canada': {
        'lat_min': 41.5,
        'lat_max': 83.1,
        'lon_min': -141.0,
        'lon_max': -52.6,
    },
}

# Canadian postal code pattern:
# Position 1: A-Z except D, F, I, O, Q, U, W, Z (20 valid letters)
# Position 2: 0-9
# Position 3: A-Z except D, F, I, O, Q, U (20 valid letters)
# Position 4: 0-9
# Position 5: A-Z except D, F, I, O, Q, U (20 valid letters)
# Position 6: 0-9
POSTAL_CODE_PATTERN = re.compile(
    r'^[ABCEGHJKLMNPRSTVXY]\d[ABCEGHJKLMNPRSTVWXYZ]\d[ABCEGHJKLMNPRSTVWXYZ]\d$'
)


def validate_postal_code(postal_code: Optional[str]) -> tuple[bool, Optional[str]]:
    """
    Validate Canadian postal code format.

    Args:
        postal_code: Postal code to validate (should be normalized: no spaces, uppercase)

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not postal_code:
        return True, None  # Empty is not an error, just missing

    if not POSTAL_CODE_PATTERN.match(postal_code):
        return False, f"Invalid postal code format: {postal_code}"

    return True, None


def validate_coordinates(
    lat: Optional[float],
    lon: Optional[float],
    bounds_name: str = 'ontario'
) -> tuple[bool, Optional[str]]:
    """
    Validate coordinates are within expected bounds.

    Args:
        lat: Latitude
        lon: Longitude
        bounds_name: Name of bounds to check against

    Returns:
        Tuple of (is_valid, error_message)
    """
    if lat is None or lon is None:
        return True, None  # Missing coords handled elsewhere

    bounds = COORDINATE_BOUNDS.get(bounds_name, COORDINATE_BOUNDS['canada'])

    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return False, f"Invalid coordinate values: lat={lat}, lon={lon}"

    if not (bounds['lat_min'] <= lat <= bounds['lat_max']):
        return False, f"Latitude {lat} out of bounds for {bounds_name}"

    if not (bounds['lon_min'] <= lon <= bounds['lon_max']):
        return False, f"Longitude {lon} out of bounds for {bounds_name}"

    return True, None


class RecordValidator(BaseTransformer):
    """
    Transformer that validates address records.

    Checks:
    - Required field groups (at least one field per group must be present)
    - Coordinate bounds
    - Postal code format

    Configuration options:
        required_field_groups: List of field groups, e.g., [['latitude', 'longitude']]
        strict_bounds: Geographic region for bounds checking (default: 'ontario')
        validate_postal: Whether to validate postal code format (default: True)
        fail_on_error: Treat validation failures as errors vs warnings (default: True)
    """

    # Default required field groups
    DEFAULT_REQUIRED_GROUPS = [
        ['latitude'],  # Both coordinate components are required
        ['longitude'],
        ['street_number', 'full_address'],  # Must have address identifier
    ]

    @property
    def name(self) -> str:
        return "RecordValidator"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.required_field_groups = self.config.get(
            'required_field_groups',
            self.DEFAULT_REQUIRED_GROUPS
        )
        self.strict_bounds = self.config.get('strict_bounds', 'ontario')
        self.validate_postal = self.config.get('validate_postal', True)
        self.fail_on_error = self.config.get('fail_on_error', True)

    def transform(self, context: TransformContext) -> TransformResult:
        """Validate record fields and quality."""
        if not self.should_run(context):
            return context.last_result

        errors: List[str] = []
        warnings: List[str] = []

        # Check required field groups
        for group in self.required_field_groups:
            has_value = False
            for field in group:
                value = context.get_field(field)
                if value is not None and value != '':
                    has_value = True
                    break

            if not has_value:
                msg = f"Missing required field(s): {' or '.join(group)}"
                if self.fail_on_error:
                    errors.append(msg)
                else:
                    warnings.append(msg)

        # Validate coordinates
        lat = context.get_field('latitude')
        lon = context.get_field('longitude')

        if lat is not None and lon is not None:
            is_valid, error_msg = validate_coordinates(lat, lon, self.strict_bounds)
            if not is_valid:
                if self.fail_on_error:
                    errors.append(error_msg)
                else:
                    warnings.append(error_msg)

        # Validate postal code
        if self.validate_postal:
            postal = context.get_field('postal_code')
            if postal:
                is_valid, error_msg = validate_postal_code(postal)
                if not is_valid:
                    # Postal validation is typically a warning, not error
                    warnings.append(error_msg)

        # Record errors and warnings
        for warning in warnings:
            context.add_warning(self.name, warning)

        for error in errors:
            context.add_error(self.name, error, data_error=True)

        if errors:
            return TransformResult.ERROR

        return TransformResult.SUCCESS


class CoordinateValidator(BaseTransformer):
    """
    Specialized validator for coordinate-only validation.

    Use when you only need bounds checking without full record validation.
    """

    @property
    def name(self) -> str:
        return "CoordinateValidator"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.bounds_name = self.config.get('bounds', 'ontario')
        self.require_coordinates = self.config.get('require_coordinates', True)

    def transform(self, context: TransformContext) -> TransformResult:
        """Validate coordinates only."""
        if not self.should_run(context):
            return context.last_result

        lat = context.get_field('latitude')
        lon = context.get_field('longitude')

        # Check if coordinates are required
        if self.require_coordinates and (lat is None or lon is None):
            context.add_error(
                self.name,
                "Missing required coordinates", data_error=True
            )
            return TransformResult.ERROR

        # Validate bounds if coordinates present
        if lat is not None and lon is not None:
            is_valid, error_msg = validate_coordinates(lat, lon, self.bounds_name)
            if not is_valid:
                context.add_error(self.name, error_msg, data_error=True)
                return TransformResult.ERROR

        return TransformResult.SUCCESS


class PostalCodeValidator(BaseTransformer):
    """
    Specialized validator for postal code format.

    Use when you only need postal validation without full record validation.
    """

    @property
    def name(self) -> str:
        return "PostalCodeValidator"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.require_postal = self.config.get('require_postal', False)
        self.fail_on_invalid = self.config.get('fail_on_invalid', False)

    def transform(self, context: TransformContext) -> TransformResult:
        """Validate postal code format only."""
        if not self.should_run(context):
            return context.last_result

        postal = context.get_field('postal_code')

        # Check if postal code is required
        if self.require_postal and not postal:
            context.add_error(self.name, "Missing required postal code", data_error=True)
            return TransformResult.ERROR

        # Validate format if present
        if postal:
            is_valid, error_msg = validate_postal_code(postal)
            if not is_valid:
                if self.fail_on_invalid:
                    context.add_error(self.name, error_msg, data_error=True)
                    return TransformResult.ERROR
                else:
                    context.add_warning(self.name, error_msg)

        return TransformResult.SUCCESS
