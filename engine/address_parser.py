"""
Address parsing and normalization utilities.
Handles structured and unstructured address formats with smart normalization.
"""
import json
import hashlib
import re
from pathlib import Path
from typing import Optional, Dict, Any


# Load mapping configurations from config/ directory at project root
config_dir = Path(__file__).parent.parent / 'config'

with open(config_dir / 'street-type-mappings.json', 'r') as f:
    street_type_mappings = json.load(f)

with open(config_dir / 'direction-mappings.json', 'r') as f:
    direction_mappings = json.load(f)


def normalize_postal_code(postal_code: Optional[str]) -> Optional[str]:
    """
    Normalize postal code by removing spaces and converting to uppercase.

    Args:
        postal_code: Raw postal code

    Returns:
        Normalized postal code
    """
    if not postal_code:
        return None
    return re.sub(r'\s+', '', postal_code).upper()


def get_postal_prefix(postal_code: Optional[str]) -> Optional[str]:
    """
    Get postal code prefix (first 3 characters).

    Args:
        postal_code: Postal code

    Returns:
        Postal code prefix
    """
    if not postal_code:
        return None
    normalized = normalize_postal_code(postal_code)
    return normalized[:3] if normalized else None


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

    upper = suffix.upper().rstrip('.')

    # Check compound abbreviations first (multi-word forms like "Side rd")
    if 'compound_abbreviations' in street_type_mappings:
        if upper in street_type_mappings['compound_abbreviations']:
            return street_type_mappings['compound_abbreviations'][upper]

    # Check abbreviations map
    if upper in street_type_mappings['abbreviations']:
        return street_type_mappings['abbreviations'][upper]

    # Check variants map
    lower = suffix.lower().rstrip('.')
    for canonical, variants in street_type_mappings['variants'].items():
        if lower in variants:
            return canonical.capitalize()

    # Return capitalized original if no mapping found
    return suffix.rstrip('.').capitalize()


def normalize_direction(direction: Optional[str]) -> Optional[str]:
    """
    Normalize street direction to full word form.

    Args:
        direction: Direction (e.g., "N", "West", "e")

    Returns:
        Normalized direction (e.g., "North", "West", "East")
    """
    if not direction:
        return None

    upper = direction.upper()

    # Check abbreviations map
    if upper in direction_mappings['abbreviations']:
        return direction_mappings['abbreviations'][upper]

    # Check variants map
    lower = direction.lower()
    for canonical, variants in direction_mappings['variants'].items():
        if lower in variants:
            return canonical.capitalize()

    # Return capitalized original if no mapping found
    return direction.capitalize()


def parse_address_string(address_string: str) -> Dict[str, Optional[str]]:
    """
    Parse address string into components (basic implementation).

    Args:
        address_string: Full address string

    Returns:
        Address components
    """
    components = {
        'street_number': None,
        'street_name': None,
        'street_suffix': None,
        'street_direction': None,
        'city': None,
        'province': None,
        'postal_code': None
    }

    # Extract postal code (Canadian format: A1A 1A1)
    postal_match = re.search(r'([A-Z]\d[A-Z]\s?\d[A-Z]\d)', address_string, re.IGNORECASE)
    if postal_match:
        components['postal_code'] = postal_match.group(1)

    # Split by comma
    parts = [p.strip() for p in address_string.split(',')]

    if len(parts) >= 1:
        # First part is street address
        street_parts = parts[0].split()

        # First token is usually the street number
        if street_parts and re.match(r'^\d+', street_parts[0]):
            components['street_number'] = street_parts.pop(0)

        # Last token might be direction (strip dots before checking)
        if street_parts and street_parts[-1].upper().rstrip('.') in direction_mappings['abbreviations']:
            components['street_direction'] = street_parts.pop()

        # Second to last might be suffix (strip dots before checking)
        if street_parts:
            potential_suffix = street_parts[-1]
            cleaned_upper = potential_suffix.upper().rstrip('.')
            cleaned_lower = potential_suffix.lower().rstrip('.')
            if (cleaned_upper in street_type_mappings['abbreviations'] or
                cleaned_lower in [v for variants in street_type_mappings['variants'].values() for v in variants]):
                components['street_suffix'] = street_parts.pop()

        # Remaining parts are street name
        if street_parts:
            components['street_name'] = ' '.join(street_parts)

    # Second part is usually city
    if len(parts) >= 2:
        components['city'] = parts[1].strip()

    # Third part might have province
    if len(parts) >= 3:
        province_match = re.search(r'\b([A-Z]{2})\b', parts[2])
        if province_match:
            components['province'] = province_match.group(1)

    return components


def parse_address(address_input: Any) -> Dict[str, Any]:
    """
    Parse structured address object into normalized components.

    Args:
        address_input: Address input (either string or structured object)

    Returns:
        Normalized address components
    """
    components = {}

    # Handle structured address (IDX format)
    if isinstance(address_input, dict) and 'Address' in address_input and isinstance(address_input['Address'], dict):
        addr = address_input['Address']
        components = {
            'street_number': addr.get('StreetNumber'),
            'street_name': addr.get('StreetName'),
            'street_suffix': addr.get('StreetSuffix'),
            'street_direction': addr.get('StreetDirection'),
            'unit_no': addr.get('UnitNumber'),
            'city': addr.get('City'),
            'province': addr.get('Province') or addr.get('StateOrProvince'),
            'postal_code': addr.get('PostalCode'),
            'country': addr.get('Country', 'Canada')
        }
    # Handle string address
    elif isinstance(address_input, str):
        components = parse_address_string(address_input)
    elif isinstance(address_input, dict) and 'address' in address_input:
        components = parse_address_string(address_input['address'])
    # Handle flat structured format (snake_case, camelCase, or bare PascalCase —
    # batch entries arrive unwrapped, so PascalCase must be readable here too)
    elif isinstance(address_input, dict):
        components = {
            'street_number': address_input.get('street_number') or address_input.get('streetNumber') or address_input.get('StreetNumber'),
            'street_name': address_input.get('street_name') or address_input.get('streetName') or address_input.get('StreetName'),
            'street_suffix': address_input.get('street_suffix') or address_input.get('streetSuffix') or address_input.get('StreetSuffix'),
            'street_direction': address_input.get('street_direction') or address_input.get('streetDirection') or address_input.get('StreetDirection'),
            'unit_no': address_input.get('unit_no') or address_input.get('unitNumber') or address_input.get('UnitNumber'),
            'city': address_input.get('city') or address_input.get('City'),
            'province': address_input.get('province') or address_input.get('Province') or address_input.get('StateOrProvince'),
            'postal_code': address_input.get('postal_code') or address_input.get('postalCode') or address_input.get('PostalCode'),
            'country': address_input.get('country', 'Canada')
        }

    # Add PIN if provided
    if isinstance(address_input, dict):
        components['pin'] = address_input.get('pin') or address_input.get('PIN')
    else:
        components['pin'] = None

    # Split letter-suffixed civic numbers ("68A" -> 68 + "A") to mirror ETL
    # storage (data_sourcing/transformers/civic_number_parser.py): properties
    # stores the base number in street_number and the letter in civic_no_suffix.
    if components.get('street_number') and not components.get('civic_no_suffix'):
        m = re.match(r'^(\d+)[ -]?([A-Za-z])$', str(components['street_number']).strip())
        if m:
            components['street_number'] = m.group(1)
            components['civic_no_suffix'] = m.group(2).upper()

    # Normalize components
    if components.get('postal_code'):
        components['postal_code'] = normalize_postal_code(components['postal_code'])
        components['postal_prefix'] = get_postal_prefix(components['postal_code'])

    if components.get('street_suffix'):
        components['street_suffix_normalized'] = normalize_street_suffix(components['street_suffix'])

    if components.get('street_direction'):
        components['street_direction_normalized'] = normalize_direction(components['street_direction'])

    # Normalize street name (trim and keep as is)
    if components.get('street_name'):
        components['street_name'] = components['street_name'].strip()

    # Deduplicate: if street_name ends with the same word as street_suffix, strip it
    if components.get('street_name') and components.get('street_suffix'):
        name_words = components['street_name'].split()
        suffix_normalized = normalize_street_suffix(components['street_suffix'])
        stripped = False

        # Check 1: Last word matches suffix (e.g., "King Edward Ave" + "Ave")
        if not stripped and len(name_words) > 1:
            last_normalized = normalize_street_suffix(name_words[-1])
            if last_normalized and suffix_normalized and last_normalized.upper() == suffix_normalized.upper():
                components['street_name'] = ' '.join(name_words[:-1])
                stripped = True

        # Check 2: Second-to-last word matches suffix when last word is a direction
        # e.g., "King Street North" + "Street" → "King North"
        if not stripped and len(name_words) > 2:
            last_upper = name_words[-1].upper().rstrip('.')
            last_lower = name_words[-1].lower().rstrip('.')
            is_direction = (
                last_upper in direction_mappings['abbreviations'] or
                any(last_lower in variants for variants in direction_mappings['variants'].values())
            )
            if is_direction:
                second_last_normalized = normalize_street_suffix(name_words[-2])
                if second_last_normalized and suffix_normalized and second_last_normalized.upper() == suffix_normalized.upper():
                    components['street_name'] = ' '.join(name_words[:-2]) + ' ' + name_words[-1]
                    stripped = True

        # Check 3: Compound suffix match — last two words form a compound suffix
        # e.g., "County Side Road" + "SDRD" → "County"
        if not stripped and len(name_words) > 2:
            compound = ' '.join(name_words[-2:])
            compound_normalized = normalize_street_suffix(compound)
            if compound_normalized and suffix_normalized and compound_normalized.upper() == suffix_normalized.upper():
                components['street_name'] = ' '.join(name_words[:-2])
                stripped = True

    return components


def generate_address_hash(components: Dict[str, Any]) -> str:
    """
    Generate hash for address (for caching).

    Args:
        components: Address components

    Returns:
        Address hash
    """
    key = '|'.join([
        str(components.get('pin') or ''),
        str(components.get('street_number') or ''),
        str(components.get('civic_no_suffix') or ''),
        str(components.get('street_name') or ''),
        str(components.get('street_suffix_normalized') or components.get('street_suffix') or ''),
        str(components.get('street_direction_normalized') or components.get('street_direction') or ''),
        str(components.get('city') or ''),
        str(components.get('postal_code') or '')
    ]).upper()

    return hashlib.md5(key.encode()).hexdigest()


def build_full_address(components: Dict[str, Any]) -> str:
    """
    Build full address string from components.

    Args:
        components: Address components

    Returns:
        Full address string
    """
    parts = []

    if components.get('unit_no'):
        parts.append(f"Unit {components['unit_no']}")
    if components.get('street_number'):
        parts.append(components['street_number'])
    if components.get('street_name'):
        parts.append(components['street_name'])
    if components.get('street_suffix'):
        parts.append(components['street_suffix'])
    if components.get('street_direction'):
        parts.append(components['street_direction'])

    street_address = ' '.join(parts)

    if components.get('city'):
        street_address += f", {components['city']}"
    if components.get('province'):
        street_address += f", {components['province']}"
    if components.get('postal_code'):
        # Format postal code with space
        pc = components['postal_code']
        formatted = f"{pc[:3]} {pc[3:]}" if len(pc) == 6 else pc
        street_address += f" {formatted}"

    return street_address
