"""
Street name parser transformer.

Parses composite street names like "CHURCHILL AVE N" into components:
- street_name: "Churchill"
- street_suffix: "Avenue"
- street_direction: "North"
"""
import json
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .base import BaseTransformer, TransformContext, TransformResult


# Load street type mappings
config_dir = Path(__file__).parent.parent.parent / 'config'
with open(config_dir / 'street-type-mappings.json', 'r') as f:
    STREET_TYPE_MAPPINGS = json.load(f)

with open(config_dir / 'direction-mappings.json', 'r') as f:
    DIRECTION_MAPPINGS = json.load(f)


def _build_suffix_pattern() -> str:
    """Build regex pattern for all known street suffixes."""
    # Collect all suffix abbreviations and variants
    suffixes = set()

    # Add abbreviations
    for abbrev in STREET_TYPE_MAPPINGS['abbreviations'].keys():
        suffixes.add(abbrev)

    # Add variants
    for variants in STREET_TYPE_MAPPINGS['variants'].values():
        for variant in variants:
            suffixes.add(variant.upper())

    # Sort by length (longest first) to match greedily
    sorted_suffixes = sorted(suffixes, key=len, reverse=True)

    # Escape any regex special characters and join with OR
    return '|'.join(re.escape(s) for s in sorted_suffixes)


def _build_direction_pattern() -> str:
    """Build regex pattern for all known directions."""
    directions = set()

    # Add abbreviations
    for abbrev in DIRECTION_MAPPINGS['abbreviations'].keys():
        directions.add(abbrev)

    # Add variants
    for variants in DIRECTION_MAPPINGS['variants'].values():
        for variant in variants:
            directions.add(variant.upper())

    # Sort by length (longest first) - important for NE vs N
    sorted_directions = sorted(directions, key=len, reverse=True)

    return '|'.join(re.escape(d) for d in sorted_directions)


# Pre-compile patterns
SUFFIX_PATTERN = _build_suffix_pattern()
DIRECTION_PATTERN = _build_direction_pattern()

# Main street parsing pattern
# Matches: "STREET NAME SUFFIX [DIRECTION]"
# Examples:
#   "CHURCHILL AVE N" -> street_name="CHURCHILL", suffix="AVE", direction="N"
#   "BANK ST" -> street_name="BANK", suffix="ST", direction=None
#   "5TH AVE" -> street_name="5TH", suffix="AVE", direction=None
#   "KING GEORGE BLVD S" -> street_name="KING GEORGE", suffix="BLVD", direction="S"
STREET_PATTERN = re.compile(
    rf'^(?P<street_name>.+?)\s+'
    rf'(?P<suffix>{SUFFIX_PATTERN})'
    rf'(?:\s+(?P<direction>{DIRECTION_PATTERN}))?$',
    re.IGNORECASE
)

# Pattern for numbered streets (1ST, 2ND, 3RD, etc.)
NUMBERED_STREET_PATTERN = re.compile(
    r'^(\d+)(ST|ND|RD|TH)$',
    re.IGNORECASE
)


def normalize_suffix(suffix: str) -> str:
    """Normalize street suffix to canonical form."""
    upper = suffix.upper().rstrip('.')
    if upper in STREET_TYPE_MAPPINGS['abbreviations']:
        return STREET_TYPE_MAPPINGS['abbreviations'][upper]

    # Check variants
    lower = suffix.lower().rstrip('.')
    for canonical, variants in STREET_TYPE_MAPPINGS['variants'].items():
        if lower in variants:
            return canonical.capitalize()

    return suffix.rstrip('.').capitalize()


def normalize_direction(direction: str) -> str:
    """Normalize direction to canonical form."""
    upper = direction.upper()
    if upper in DIRECTION_MAPPINGS['abbreviations']:
        return DIRECTION_MAPPINGS['abbreviations'][upper]

    # Check variants
    lower = direction.lower()
    for canonical, variants in DIRECTION_MAPPINGS['variants'].items():
        if lower in variants:
            return canonical.capitalize()

    return direction.capitalize()


def title_case_street_name(name: str) -> str:
    """
    Convert street name to title case, preserving numbered streets.

    Examples:
        "CHURCHILL" -> "Churchill"
        "KING GEORGE" -> "King George"
        "5TH" -> "5th"
        "MCDONALD" -> "McDonald"
    """
    words = name.split()
    result = []

    for word in words:
        # Check for numbered ordinals (1ST, 2ND, etc.)
        if NUMBERED_STREET_PATTERN.match(word):
            result.append(word.lower())
        # Check for Mc/Mac names
        elif word.upper().startswith('MC') and len(word) > 2:
            result.append('Mc' + word[2:].capitalize())
        elif word.upper().startswith('MAC') and len(word) > 3:
            result.append('Mac' + word[3:].capitalize())
        else:
            result.append(word.capitalize())

    return ' '.join(result)


def parse_street_name(street_string: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Parse a composite street name into components.

    Args:
        street_string: Full street name (e.g., "CHURCHILL AVE N")

    Returns:
        Tuple of (street_name, suffix, direction) or (None, None, None) if no match
    """
    if not street_string:
        return None, None, None

    street_string = street_string.strip()

    match = STREET_PATTERN.match(street_string)
    if match:
        street_name = title_case_street_name(match.group('street_name'))
        suffix = normalize_suffix(match.group('suffix'))
        direction = match.group('direction')
        if direction:
            direction = normalize_direction(direction)
        return street_name, suffix, direction

    # No match - return original as street name
    return title_case_street_name(street_string), None, None


class StreetNameParser(BaseTransformer):
    """
    Transformer that parses composite street names into components.

    Configuration options:
        source_field: Field containing composite street name (default: "street_name")
        preserve_original: Save original value in {source_field}_original (default: True)
        overwrite_existing: Overwrite existing suffix/direction fields (default: False)
    """

    @property
    def name(self) -> str:
        return "StreetNameParser"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.source_field = self.config.get('source_field', 'street_name')
        self.preserve_original = self.config.get('preserve_original', True)
        self.overwrite_existing = self.config.get('overwrite_existing', False)

    def transform(self, context: TransformContext) -> TransformResult:
        """Parse composite street name into components."""
        if not self.should_run(context):
            return context.last_result

        # Get source value
        source_value = context.get_field(self.source_field)
        if not source_value:
            return TransformResult.SUCCESS

        # Skip if already parsed (unless overwrite enabled)
        if not self.overwrite_existing:
            if context.get_field('street_suffix') or context.get_field('street_direction'):
                return TransformResult.SUCCESS

        # Parse the street name
        street_name, suffix, direction = parse_street_name(source_value)

        if street_name is None:
            return TransformResult.SUCCESS

        # Track modifications
        modified = False

        # Only update if we extracted something new
        if suffix or direction:
            # Save original if preserving
            if self.preserve_original:
                context.set_field(
                    f'{self.source_field}_original',
                    source_value,
                    self.name,
                    reason="Preserving original composite street name"
                )

            # Update street name (without suffix/direction)
            if street_name != source_value:
                context.set_field(
                    self.source_field,
                    street_name,
                    self.name,
                    reason=f"Extracted from composite: {source_value}"
                )
                modified = True

            # Set suffix if found
            if suffix:
                context.set_field(
                    'street_suffix',
                    suffix,
                    self.name,
                    reason=f"Parsed from: {source_value}"
                )
                modified = True

            # Set direction if found
            if direction:
                context.set_field(
                    'street_direction',
                    direction,
                    self.name,
                    reason=f"Parsed from: {source_value}"
                )
                modified = True
        else:
            # No suffix/direction found, but still normalize case
            normalized = title_case_street_name(source_value)
            if normalized != source_value:
                context.set_field(
                    self.source_field,
                    normalized,
                    self.name,
                    reason="Normalized case"
                )
                modified = True

        return TransformResult.MODIFIED if modified else TransformResult.SUCCESS
