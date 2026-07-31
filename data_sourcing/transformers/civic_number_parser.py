"""
Civic number parser transformer.

Parses civic number suffixes from street numbers:
- "100A" -> street_number="100", civic_no_suffix="A"
- "22B" -> street_number="22", civic_no_suffix="B"
- "15 1/2" -> street_number="15", civic_no_suffix="1/2"
- "100-A" -> street_number="100", civic_no_suffix="A"
- "100-102" -> unchanged (address range, not suffix)
"""
import re
from typing import Any, Dict, Optional, Tuple

from .base import BaseTransformer, TransformContext, TransformResult


# Pattern for letter suffixes attached to number: "100A", "22B", "15C"
# Must be at end of string, captures number and single letter
LETTER_SUFFIX_PATTERN = re.compile(
    r'^(\d+)([A-Za-z])$'
)

# Pattern for fractional suffixes: "15 1/2", "100 3/4"
FRACTION_SUFFIX_PATTERN = re.compile(
    r'^(\d+)\s+(\d+/\d+)$'
)

# Pattern for letter suffix with space: "100 A", "22 B"
SPACED_LETTER_PATTERN = re.compile(
    r'^(\d+)\s+([A-Za-z])$'
)

# Pattern for hyphenated suffix (distinguishing from ranges):
# "100-A" is suffix, "100-102" is range
HYPHEN_SUFFIX_PATTERN = re.compile(
    r'^(\d+)-([A-Za-z])$'
)


def parse_civic_number(street_number: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Parse a street number into base number and suffix.

    Args:
        street_number: The street number string (e.g., "100A", "15 1/2")

    Returns:
        Tuple of (base_number, suffix) where suffix may be None
    """
    if not street_number:
        return street_number, None

    street_number = str(street_number).strip()

    # Empty string
    if not street_number:
        return street_number, None

    # Already just a number
    if street_number.isdigit():
        return street_number, None

    # Check letter suffix attached: "100A"
    match = LETTER_SUFFIX_PATTERN.match(street_number)
    if match:
        return match.group(1), match.group(2).upper()

    # Check fractional suffix: "15 1/2"
    match = FRACTION_SUFFIX_PATTERN.match(street_number)
    if match:
        return match.group(1), match.group(2)

    # Check spaced letter: "100 A"
    match = SPACED_LETTER_PATTERN.match(street_number)
    if match:
        return match.group(1), match.group(2).upper()

    # Check hyphenated letter: "100-A" (NOT ranges like "100-102")
    match = HYPHEN_SUFFIX_PATTERN.match(street_number)
    if match:
        return match.group(1), match.group(2).upper()

    # No recognized pattern - return as-is with no suffix
    return street_number, None


class CivicNumberParser(BaseTransformer):
    """
    Transformer that parses civic number suffixes from street numbers.

    Configuration options:
        source_field: Field containing street number (default: "street_number")
        passthrough_suffix: If true, don't parse if civic_no_suffix already set (default: True)
    """

    @property
    def name(self) -> str:
        return "CivicNumberParser"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.source_field = self.config.get('source_field', 'street_number')
        self.passthrough_suffix = self.config.get('passthrough_suffix', True)

    def transform(self, context: TransformContext) -> TransformResult:
        """Parse civic number suffix from street number."""
        if not self.should_run(context):
            return context.last_result

        # Skip if suffix already provided (passthrough from source)
        existing_suffix = context.get_field('civic_no_suffix')
        if self.passthrough_suffix and existing_suffix:
            return TransformResult.SUCCESS

        street_number = context.get_field(self.source_field)
        if not street_number:
            return TransformResult.SUCCESS

        # Parse the street number
        base_number, suffix = parse_civic_number(str(street_number))

        if base_number is None:
            return TransformResult.SUCCESS

        modified = False

        # Only update if we extracted something new
        if suffix:
            # Update street_number if it changed (had suffix removed)
            if base_number != str(street_number):
                context.set_field(
                    self.source_field,
                    base_number,
                    self.name,
                    reason=f"Extracted base number from: {street_number}"
                )
                modified = True

            # Set suffix
            context.set_field(
                'civic_no_suffix',
                suffix,
                self.name,
                reason=f"Parsed from street number: {street_number}"
            )
            modified = True

        return TransformResult.MODIFIED if modified else TransformResult.SUCCESS
