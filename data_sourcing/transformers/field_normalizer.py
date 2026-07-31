"""
Field normalizer transformer.

Normalizes address fields to canonical forms:
- street_suffix: "ST" → "Street"
- street_direction: "N" → "North", "None" → null
- postal_code: "n1e 1a1" → "N1E1A1"
- province: "ontario" → "ON"
- city: "  guelph  " → "Guelph"
"""
import json
import re
from pathlib import Path
from typing import Optional

from .base import BaseTransformer, TransformContext, TransformResult


# Load mappings
config_dir = Path(__file__).parent.parent.parent / 'config'
with open(config_dir / 'street-type-mappings.json', 'r') as f:
    STREET_TYPE_MAPPINGS = json.load(f)

with open(config_dir / 'direction-mappings.json', 'r') as f:
    DIRECTION_MAPPINGS = json.load(f)


# Province mappings (Canadian provinces and territories)
PROVINCE_MAPPINGS = {
    # Full names to abbreviations
    'alberta': 'AB',
    'british columbia': 'BC',
    'manitoba': 'MB',
    'new brunswick': 'NB',
    'newfoundland and labrador': 'NL',
    'newfoundland': 'NL',
    'nova scotia': 'NS',
    'northwest territories': 'NT',
    'nunavut': 'NU',
    'ontario': 'ON',
    'prince edward island': 'PE',
    'pei': 'PE',
    'quebec': 'QC',
    'saskatchewan': 'SK',
    'yukon': 'YT',
    # Already abbreviated
    'ab': 'AB',
    'bc': 'BC',
    'mb': 'MB',
    'nb': 'NB',
    'nl': 'NL',
    'ns': 'NS',
    'nt': 'NT',
    'nu': 'NU',
    'on': 'ON',
    'pe': 'PE',
    'qc': 'QC',
    'sk': 'SK',
    'yt': 'YT',
}

# Values that should be treated as null/empty
NULL_VALUES = {'none', 'null', 'n/a', 'na', '-', ''}

# Unit number label prefixes to optionally strip
UNIT_LABEL_PREFIXES = [
    'UNIT #', 'SUITE #', 'APT #', 'APT.',
    'UNIT', 'SUITE', 'STE', 'APT', 'APARTMENT', '#'
]


def normalize_street_suffix(suffix: Optional[str]) -> Optional[str]:
    """
    Normalize street suffix to full word form.

    Args:
        suffix: Street suffix (e.g., "ST", "Ave", "road", "Dr.")

    Returns:
        Normalized suffix (e.g., "Street", "Avenue", "Road", "Drive")
    """
    if not suffix:
        return None

    # Check for null-like values
    if suffix.lower().strip() in NULL_VALUES:
        return None

    upper = suffix.upper().strip().rstrip('.')

    # Check compound abbreviations first (multi-word forms like "Side rd")
    if 'compound_abbreviations' in STREET_TYPE_MAPPINGS:
        if upper in STREET_TYPE_MAPPINGS['compound_abbreviations']:
            return STREET_TYPE_MAPPINGS['compound_abbreviations'][upper]

    # Check abbreviations map
    if upper in STREET_TYPE_MAPPINGS['abbreviations']:
        return STREET_TYPE_MAPPINGS['abbreviations'][upper]

    # Check variants map
    lower = suffix.lower().strip().rstrip('.')
    for canonical, variants in STREET_TYPE_MAPPINGS['variants'].items():
        if lower in variants:
            return canonical.capitalize()

    # Return capitalized original if no mapping found
    return suffix.strip().rstrip('.').capitalize()


def normalize_direction(direction: Optional[str]) -> Optional[str]:
    """
    Normalize street direction to full word form.

    Args:
        direction: Direction (e.g., "N", "West", "e", "None")

    Returns:
        Normalized direction (e.g., "North", "West", "East") or None
    """
    if not direction:
        return None

    # Check for null-like values (Toronto uses "None" string)
    if direction.lower().strip() in NULL_VALUES:
        return None

    upper = direction.upper().strip()

    # Check abbreviations map
    if upper in DIRECTION_MAPPINGS['abbreviations']:
        return DIRECTION_MAPPINGS['abbreviations'][upper]

    # Check variants map
    lower = direction.lower().strip()
    for canonical, variants in DIRECTION_MAPPINGS['variants'].items():
        if lower in variants:
            return canonical.capitalize()

    # Return capitalized original if no mapping found
    return direction.strip().capitalize()


def normalize_postal_code(postal_code: Optional[str]) -> Optional[str]:
    """
    Normalize postal code: remove spaces, uppercase.

    Args:
        postal_code: Raw postal code (e.g., "n1e 1a1")

    Returns:
        Normalized postal code (e.g., "N1E1A1")
    """
    if not postal_code:
        return None

    if postal_code.lower().strip() in NULL_VALUES:
        return None

    # Remove all whitespace and uppercase
    normalized = re.sub(r'\s+', '', postal_code).upper()

    # Return only if it looks like a valid postal code format
    if re.match(r'^[A-Z]\d[A-Z]\d[A-Z]\d$', normalized):
        return normalized

    return normalized if normalized else None


def get_fsa_code(postal_code: Optional[str]) -> Optional[str]:
    """
    Extract FSA (Forward Sortation Area - first 3 characters) from postal code.

    Args:
        postal_code: Normalized postal code

    Returns:
        FSA code (first 3 characters)
    """
    if not postal_code or len(postal_code) < 3:
        return None
    return postal_code[:3]


def normalize_province(province: Optional[str]) -> Optional[str]:
    """
    Normalize province to 2-letter abbreviation.

    Args:
        province: Province name or abbreviation

    Returns:
        2-letter province code
    """
    if not province:
        return None

    if province.lower().strip() in NULL_VALUES:
        return None

    lower = province.lower().strip()
    return PROVINCE_MAPPINGS.get(lower, province.upper().strip())


def normalize_city(city: Optional[str]) -> Optional[str]:
    """
    Normalize city name: trim whitespace, title case.

    Args:
        city: City name

    Returns:
        Normalized city name
    """
    if not city:
        return None

    if city.lower().strip() in NULL_VALUES:
        return None

    return city.strip().title()


def normalize_unit_number(unit_no: Optional[str], strip_labels: bool = False) -> Optional[str]:
    """
    Normalize unit/apartment number.

    Args:
        unit_no: Raw unit number (e.g., "Unit 5", "APT 3B", "200")
        strip_labels: If True, remove prefix labels like "Unit", "Suite"

    Returns:
        Normalized unit number
    """
    if not unit_no:
        return None

    if str(unit_no).lower().strip() in NULL_VALUES:
        return None

    unit_no = str(unit_no).strip()

    if strip_labels:
        upper = unit_no.upper()
        for prefix in UNIT_LABEL_PREFIXES:
            if upper.startswith(prefix):
                unit_no = unit_no[len(prefix):].strip()
                # Remove any remaining leading punctuation
                unit_no = unit_no.lstrip('# .-')
                break

    return unit_no.strip() if unit_no.strip() else None


def normalize_civic_suffix(suffix: Optional[str]) -> Optional[str]:
    """
    Normalize civic number suffix.

    Args:
        suffix: Raw suffix (e.g., "a", "B", "1/2")

    Returns:
        Normalized suffix (uppercase for letters, preserve fractions)
    """
    if not suffix:
        return None

    if str(suffix).lower().strip() in NULL_VALUES:
        return None

    suffix = str(suffix).strip()

    # Uppercase letter suffixes, preserve fractions as-is
    if suffix.isalpha():
        return suffix.upper()

    return suffix


class FieldNormalizer(BaseTransformer):
    """
    Transformer that normalizes address fields to canonical forms.

    Normalizes:
    - street_suffix: abbreviation → full word
    - street_direction: abbreviation → full word, "None" → null
    - postal_code: remove spaces, uppercase
    - province: full name → abbreviation
    - city: trim, title case
    """

    @property
    def name(self) -> str:
        return "FieldNormalizer"

    # Geographic qualifiers that may be appended to street suffixes
    # (e.g., Niagara's "Street louth" where "louth" is a former township name)
    GEOGRAPHIC_QUALIFIERS = ['louth']

    def transform(self, context: TransformContext) -> TransformResult:
        """Normalize address fields."""
        if not self.should_run(context):
            return context.last_result

        modified = False

        # Pre-process: strip geographic qualifiers from suffix (e.g., "Street louth")
        suffix = context.get_field('street_suffix')
        if suffix:
            suffix_lower = suffix.lower().strip()
            for qualifier in self.GEOGRAPHIC_QUALIFIERS:
                if suffix_lower.endswith(' ' + qualifier):
                    # Strip qualifier from suffix, append to street_name
                    clean_suffix = suffix[:-(len(qualifier) + 1)].strip()
                    street_name = context.get_field('street_name') or ''
                    new_name = (street_name + ' ' + qualifier.capitalize()).strip()
                    context.set_field(
                        'street_name', new_name, self.name,
                        reason=f"Moved geographic qualifier '{qualifier}' from suffix to street name"
                    )
                    context.set_field(
                        'street_suffix', clean_suffix, self.name,
                        reason=f"Stripped geographic qualifier '{qualifier}'"
                    )
                    modified = True
                    break

        # Normalize street suffix
        suffix = context.get_field('street_suffix')
        if suffix:
            normalized = normalize_street_suffix(suffix)
            if normalized != suffix:
                context.set_field(
                    'street_suffix',
                    normalized,
                    self.name,
                    reason=f"Normalized: {repr(suffix)} → {repr(normalized)}"
                )
                modified = True

        # Normalize street direction
        direction = context.get_field('street_direction')
        if direction:
            normalized = normalize_direction(direction)
            if normalized != direction:
                context.set_field(
                    'street_direction',
                    normalized,
                    self.name,
                    reason=f"Normalized: {repr(direction)} → {repr(normalized)}"
                )
                modified = True

        # Normalize postal code
        postal = context.get_field('postal_code')
        if postal:
            normalized = normalize_postal_code(postal)
            if normalized != postal:
                context.set_field(
                    'postal_code',
                    normalized,
                    self.name,
                    reason=f"Normalized: {repr(postal)} → {repr(normalized)}"
                )
                modified = True

            # Also set fsa_code if we have a valid postal code
            if normalized and len(normalized) >= 3:
                fsa = get_fsa_code(normalized)
                existing_fsa = context.get_field('fsa_code')
                if fsa != existing_fsa:
                    context.set_field(
                        'fsa_code',
                        fsa,
                        self.name,
                        reason="Extracted from postal code"
                    )
                    modified = True

        # Normalize province
        province = context.get_field('province')
        if province:
            normalized = normalize_province(province)
            if normalized != province:
                context.set_field(
                    'province',
                    normalized,
                    self.name,
                    reason=f"Normalized: {repr(province)} → {repr(normalized)}"
                )
                modified = True

        # Normalize city
        city = context.get_field('city')
        if city:
            normalized = normalize_city(city)
            if normalized != city:
                context.set_field(
                    'city',
                    normalized,
                    self.name,
                    reason=f"Normalized: {repr(city)} → {repr(normalized)}"
                )
                modified = True

        # Normalize unit number
        unit_no = context.get_field('unit_no')
        if unit_no:
            # Configurable: strip_labels can be set in config
            strip_labels = self.config.get('strip_unit_labels', False)
            normalized = normalize_unit_number(unit_no, strip_labels)
            if normalized != unit_no:
                context.set_field(
                    'unit_no',
                    normalized,
                    self.name,
                    reason=f"Normalized: {repr(unit_no)} → {repr(normalized)}"
                )
                modified = True

        # Normalize civic suffix (if set by CivicNumberParser or from source)
        civic_suffix = context.get_field('civic_no_suffix')
        if civic_suffix:
            normalized = normalize_civic_suffix(civic_suffix)
            if normalized != civic_suffix:
                context.set_field(
                    'civic_no_suffix',
                    normalized,
                    self.name,
                    reason=f"Normalized: {repr(civic_suffix)} → {repr(normalized)}"
                )
                modified = True

        return TransformResult.MODIFIED if modified else TransformResult.SUCCESS
