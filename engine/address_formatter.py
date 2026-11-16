"""Display labels from matched database records, never from request text."""
import re
from typing import Any, Mapping, Optional
from .address_parser import normalize_direction, normalize_street_suffix


def format_property_address(row: Mapping[str, Any]) -> Optional[str]:
    """Preserve a supplied full address, otherwise format known stored components.

    Some sources (notably Hamilton) provide components but no complete label.
    This fallback works for existing rows, PIN matches and batch results without
    rewriting upstream data or requiring an import. No missing fields are invented.
    """
    supplied = row.get('full_address')
    if supplied is not None and str(supplied).strip():
        return str(supplied)

    def text(field):
        value = row.get(field)
        return '' if value is None else str(value).strip()

    number, suffix = text('street_number'), text('civic_no_suffix')
    name = text('street_name')
    if not number and not name:
        return None
    if number and suffix:
        separator = '' if suffix[0].isalpha() or suffix[0] in '-/' else ' '
        number += separator + suffix
    street = ' '.join(part for part in (
        number, name, normalize_street_suffix(text('street_suffix')),
        normalize_direction(text('street_direction')),
    ) if part)
    unit = text('unit_no')
    if unit:
        label = unit if re.match(r'^(unit\b|suite\b|apt\b|apartment\b|#)', unit, re.I) else f'Unit {unit}'
        street = f'{label}, {street}'
    postal = re.sub(r'\s+', '', text('postal_code')).upper()
    if len(postal) == 6:
        postal = f'{postal[:3]} {postal[3:]}'
    region = ' '.join(part for part in (text('province'), postal) if part)
    return ', '.join(part for part in (street, text('city'), region) if part)
