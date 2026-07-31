"""
Unit number parser transformer.

Parses unit numbers from address fields:
- "Unit 5 - 123 Main St" -> unit_no="5"
- "Suite 200 - 123 Main St" -> unit_no="200"
- "Apt 3B - 123 Main St" -> unit_no="3B"
- "#5 - 123 Main St" -> unit_no="5"
- "123 Main St Unit 5" -> unit_no="5"
- "5-123 Main St" -> unit_no="5" (when hyphenated parsing enabled)
"""
import re
from typing import Any, Dict, Optional, Tuple

from .base import BaseTransformer, TransformContext, TransformResult


# Unit label prefixes (case-insensitive matching)
UNIT_LABELS = [
    'APARTMENT', 'APT\\.?', 'SUITE', 'STE\\.?', 'UNIT', '#'
]

# Pattern for labeled unit prefix: "Unit 5 - ...", "Suite 200 ...", "#5-..."
# Matches: Unit/Suite/Apt/# followed by unit number, then separator
UNIT_PREFIX_PATTERN = re.compile(
    r'^(?:' + '|'.join(UNIT_LABELS) + r')\s*'  # Label
    r'(\d+[A-Za-z]?|[A-Za-z]\d*)'              # Unit number (e.g., 5, 5A, A, A1)
    r'[\s,\-]+',                                # Separator (space, comma, hyphen)
    re.IGNORECASE
)

# Pattern for labeled unit suffix: "... Unit 5", "..., Suite 200"
# Matches: separator then Unit/Suite/Apt/# followed by unit number at end
UNIT_SUFFIX_PATTERN = re.compile(
    r'[\s,\-]+'                                 # Separator
    r'(?:' + '|'.join(UNIT_LABELS) + r')\s*'   # Label
    r'(\d+[A-Za-z]?|[A-Za-z]\d*)$',            # Unit number at end
    re.IGNORECASE
)

# Pattern for hyphenated unit-street format: "5-123 Main St"
# Only matches when first part is clearly a unit (short number or letter combo)
# Does NOT match address ranges like "100-102"
HYPHEN_UNIT_STREET_PATTERN = re.compile(
    r'^(\d{1,2}[A-Za-z]?|[A-Za-z]\d?)'         # Unit (1-2 digits optionally with letter, or letter with digit)
    r'-'                                        # Hyphen separator
    r'(\d{2,})\s+',                             # Street number (2+ digits) followed by space
    re.IGNORECASE
)

# Pattern for standalone unit field value: strips labels like "Unit", "Suite" etc.
UNIT_LABEL_STRIP_PATTERN = re.compile(
    r'^(?:' + '|'.join(UNIT_LABELS) + r')\s*',
    re.IGNORECASE
)


def parse_unit_number(
    address_str: Optional[str],
    parse_hyphenated: bool = False
) -> Tuple[Optional[str], bool]:
    """
    Parse a unit number from an address string.

    Args:
        address_str: The address string (e.g., "Unit 5 - 123 Main St")
        parse_hyphenated: Whether to parse hyphenated format (e.g., "5-123 Main St")

    Returns:
        Tuple of (unit_number, found) where unit_number is the extracted value
        and found indicates whether a unit was found
    """
    if not address_str:
        return None, False

    address_str = str(address_str).strip()

    if not address_str:
        return None, False

    # Try prefix pattern: "Unit 5 - 123 Main St"
    match = UNIT_PREFIX_PATTERN.match(address_str)
    if match:
        unit = match.group(1).strip()
        return unit.upper() if unit.isalpha() else unit, True

    # Try suffix pattern: "123 Main St Unit 5"
    match = UNIT_SUFFIX_PATTERN.search(address_str)
    if match:
        unit = match.group(1).strip()
        return unit.upper() if unit.isalpha() else unit, True

    # Try hyphenated format if enabled: "5-123 Main St"
    if parse_hyphenated:
        match = HYPHEN_UNIT_STREET_PATTERN.match(address_str)
        if match:
            unit = match.group(1).strip()
            return unit.upper() if unit.isalpha() else unit, True

    return None, False


def strip_unit_label(unit_str: Optional[str]) -> Optional[str]:
    """
    Strip unit label prefix from a unit string.

    Args:
        unit_str: Unit string possibly with label (e.g., "Unit 5", "Suite 200")

    Returns:
        Unit number without label (e.g., "5", "200")
    """
    if not unit_str:
        return None

    unit_str = str(unit_str).strip()

    if not unit_str:
        return None

    # Strip label prefix
    stripped = UNIT_LABEL_STRIP_PATTERN.sub('', unit_str).strip()

    # Remove any leading punctuation left over
    stripped = stripped.lstrip('#.- ')

    return stripped if stripped else unit_str


class UnitNumberParser(BaseTransformer):
    """
    Transformer that parses unit numbers from address fields.

    Configuration options:
        source_field: Field containing address to parse (default: "full_address")
        passthrough_unit: If true, don't parse if unit_no already set (default: True)
        parse_hyphenated: Enable hyphenated format parsing (default: False)
        strip_labels: Strip labels from existing unit_no values (default: True)
    """

    @property
    def name(self) -> str:
        return "UnitNumberParser"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.source_field = self.config.get('source_field', 'full_address')
        self.passthrough_unit = self.config.get('passthrough_unit', True)
        self.parse_hyphenated = self.config.get('parse_hyphenated', False)
        self.strip_labels = self.config.get('strip_labels', True)

    def transform(self, context: TransformContext) -> TransformResult:
        """Parse unit number from address field."""
        if not self.should_run(context):
            return context.last_result

        # Check if unit_no already exists
        existing_unit = context.get_field('unit_no')
        if existing_unit:
            if self.passthrough_unit:
                # Passthrough mode: skip parsing, but optionally strip labels
                if self.strip_labels:
                    stripped = strip_unit_label(existing_unit)
                    if stripped != existing_unit:
                        context.set_field(
                            'unit_no',
                            stripped,
                            self.name,
                            reason=f"Stripped label from: {existing_unit}"
                        )
                        return TransformResult.MODIFIED
                return TransformResult.SUCCESS

        # Get source field value
        source_value = context.get_field(self.source_field)
        if not source_value:
            return TransformResult.SUCCESS

        # Parse unit number from source field
        unit_no, found = parse_unit_number(
            str(source_value),
            parse_hyphenated=self.parse_hyphenated
        )

        if not found or not unit_no:
            return TransformResult.SUCCESS

        # Set the unit number (don't modify source field per requirements)
        context.set_field(
            'unit_no',
            unit_no,
            self.name,
            reason=f"Parsed from {self.source_field}: {source_value}"
        )

        return TransformResult.MODIFIED
