#!/usr/bin/env python3
"""
Google Reverse Geocoding Validator

Validates geocoding accuracy by reverse-geocoding our coordinates through
Google's API and checking if the returned address matches the original.

Usage:
    python validate_google_reverse.py
    python validate_google_reverse.py --sample 500
    python validate_google_reverse.py --input path/to/results.json
"""

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


def is_google_api_enabled() -> bool:
    """Check if Google API is enabled and configured."""
    api_key = os.getenv('GOOGLE_GEOCODING_API_KEY')
    enabled = os.getenv('GOOGLE_API_ENABLED', 'false').lower() == 'true'
    return enabled and api_key is not None and len(api_key) > 0


async def reverse_geocode(lat: float, lng: float) -> Optional[dict]:
    """
    Reverse geocode coordinates using Google Geocoding API.

    Args:
        lat: Latitude
        lng: Longitude

    Returns:
        Reverse geocoding result or None
    """
    api_key = os.getenv('GOOGLE_GEOCODING_API_KEY')

    params = {
        'latlng': f'{lat},{lng}',
        'key': api_key
    }
    url = f'https://maps.googleapis.com/maps/api/geocode/json?{urlencode(params)}'

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=10.0)
            data = response.json()

        if data.get('status') == 'OK' and data.get('results'):
            result = data['results'][0]
            return {
                'formatted_address': result.get('formatted_address', ''),
                'address_components': result.get('address_components', []),
                'place_id': result.get('place_id', ''),
                'types': result.get('types', [])
            }
        elif data.get('status') == 'ZERO_RESULTS':
            return None
        else:
            error_msg = data.get('error_message', data.get('status'))
            print(f'Google API error: {error_msg}')
            return None

    except Exception as error:
        print(f'Reverse geocoding error: {error}')
        return None


def extract_address_component(components: list, component_type: str) -> Optional[str]:
    """Extract a specific component from Google's address_components."""
    for component in components:
        if component_type in component.get('types', []):
            return component.get('short_name') or component.get('long_name')
    return None


def normalize_street_name(name: str) -> str:
    """Normalize street name for comparison."""
    if not name:
        return ''

    # Convert to uppercase
    normalized = name.upper()

    # Remove common prefixes/suffixes and punctuation
    normalized = re.sub(r'[.,#]', '', normalized)

    # Normalize street type abbreviations
    replacements = {
        r'\bSTREET\b': 'ST',
        r'\bAVENUE\b': 'AVE',
        r'\bROAD\b': 'RD',
        r'\bDRIVE\b': 'DR',
        r'\bBOULEVARD\b': 'BLVD',
        r'\bCRESCENT\b': 'CRES',
        r'\bCOURT\b': 'CT',
        r'\bPLACE\b': 'PL',
        r'\bLANE\b': 'LN',
        r'\bCIRCLE\b': 'CIR',
        r'\bTERRACE\b': 'TERR',
        r'\bPARKWAY\b': 'PKWY',
        r'\bSQUARE\b': 'SQ',
        r'\bNORTH\b': 'N',
        r'\bSOUTH\b': 'S',
        r'\bEAST\b': 'E',
        r'\bWEST\b': 'W',
    }

    for pattern, replacement in replacements.items():
        normalized = re.sub(pattern, replacement, normalized)

    # Remove extra spaces
    normalized = ' '.join(normalized.split())

    return normalized


def parse_our_address(matched_address: str) -> dict:
    """Parse our matched_address format (e.g., '1 Riverton Dr')."""
    parts = matched_address.strip().split()

    result = {
        'street_number': '',
        'street_name': '',
        'street_suffix': ''
    }

    if not parts:
        return result

    # First part is usually the street number
    if parts[0].isdigit() or re.match(r'^\d+[A-Za-z]?$', parts[0]):
        result['street_number'] = parts[0]
        parts = parts[1:]

    if parts:
        # Last part is usually the suffix
        result['street_suffix'] = parts[-1] if len(parts) > 1 else ''
        result['street_name'] = ' '.join(parts[:-1]) if len(parts) > 1 else parts[0]

    return result


def compare_addresses(our_address: str, google_result: dict) -> dict:
    """
    Compare our geocoded address with Google's reverse geocoded result.

    Returns match status and details.
    """
    if not google_result:
        return {
            'match_type': 'no_result',
            'confidence': 0.0,
            'details': 'Google returned no results for these coordinates'
        }

    # Parse our address
    our_parsed = parse_our_address(our_address)
    our_number = our_parsed['street_number']
    our_street = normalize_street_name(f"{our_parsed['street_name']} {our_parsed['street_suffix']}")

    # Extract Google's address components
    components = google_result.get('address_components', [])
    google_number = extract_address_component(components, 'street_number') or ''
    google_route = extract_address_component(components, 'route') or ''
    google_street = normalize_street_name(google_route)

    formatted_address = google_result.get('formatted_address', '')

    # Check for exact match
    if our_number == google_number and our_street == google_street:
        return {
            'match_type': 'exact',
            'confidence': 1.0,
            'details': 'Street number and name match exactly',
            'our_address': f'{our_number} {our_street}',
            'google_address': f'{google_number} {google_street}',
            'google_formatted': formatted_address
        }

    # Check for street match (different number)
    if our_street == google_street:
        return {
            'match_type': 'street_match',
            'confidence': 0.7,
            'details': f'Street matches but numbers differ ({our_number} vs {google_number})',
            'our_address': f'{our_number} {our_street}',
            'google_address': f'{google_number} {google_street}',
            'google_formatted': formatted_address
        }

    # Check for number match (different street)
    if our_number == google_number:
        return {
            'match_type': 'number_match',
            'confidence': 0.3,
            'details': f'Number matches but streets differ ({our_street} vs {google_street})',
            'our_address': f'{our_number} {our_street}',
            'google_address': f'{google_number} {google_street}',
            'google_formatted': formatted_address
        }

    # Check if our address appears in the formatted address
    our_normalized = normalize_street_name(our_address)
    google_normalized = normalize_street_name(formatted_address)

    if our_normalized in google_normalized or our_address.upper() in formatted_address.upper():
        return {
            'match_type': 'partial',
            'confidence': 0.5,
            'details': 'Our address found within Google formatted address',
            'our_address': our_address,
            'google_address': f'{google_number} {google_street}',
            'google_formatted': formatted_address
        }

    # No match
    return {
        'match_type': 'mismatch',
        'confidence': 0.0,
        'details': 'Address components do not match',
        'our_address': f'{our_number} {our_street}',
        'google_address': f'{google_number} {google_street}',
        'google_formatted': formatted_address
    }


async def validate_addresses(addresses: list, sample_size: int) -> dict:
    """
    Validate a sample of addresses against Google's reverse geocoding.

    Args:
        addresses: List of geocoded address results
        sample_size: Number of addresses to validate

    Returns:
        Validation results
    """
    # Take sample
    sample = addresses[:sample_size]

    results = {
        'total_validated': len(sample),
        'exact_matches': 0,
        'street_matches': 0,
        'number_matches': 0,
        'partial_matches': 0,
        'mismatches': 0,
        'no_results': 0,
        'api_calls': 0,
        'validations': []
    }

    print(f"\nValidating {len(sample)} addresses against Google Reverse Geocoding API...")
    print("-" * 60)

    for i, addr in enumerate(sample, 1):
        lat = addr.get('latitude')
        lng = addr.get('longitude')
        matched_address = addr.get('matched_address', '')

        if lat is None or lng is None:
            print(f"[{i}/{len(sample)}] Skipping - no coordinates")
            continue

        # Call Google API
        google_result = await reverse_geocode(lat, lng)
        results['api_calls'] += 1

        # Compare addresses
        comparison = compare_addresses(matched_address, google_result)

        # Update counters
        match_type = comparison['match_type']
        if match_type == 'exact':
            results['exact_matches'] += 1
            status = '✓'
        elif match_type == 'street_match':
            results['street_matches'] += 1
            status = '~'
        elif match_type == 'number_match':
            results['number_matches'] += 1
            status = '?'
        elif match_type == 'partial':
            results['partial_matches'] += 1
            status = '~'
        elif match_type == 'no_result':
            results['no_results'] += 1
            status = '!'
        else:
            results['mismatches'] += 1
            status = '✗'

        # Store validation result
        validation = {
            'index': addr.get('index', i-1),
            'coordinates': {'lat': lat, 'lng': lng},
            'our_address': matched_address,
            'source': addr.get('source', ''),
            'match_tier': addr.get('match_tier', 0),
            'confidence': addr.get('confidence', 0),
            'match_method': addr.get('match_method', ''),
            'validation': comparison
        }
        results['validations'].append(validation)

        # Progress indicator
        google_addr = comparison.get('google_formatted', 'N/A')[:50]
        print(f"[{i}/{len(sample)}] {status} {matched_address} → {google_addr}")

        # Small delay to avoid rate limiting
        if i < len(sample):
            await asyncio.sleep(0.05)

    return results


def generate_report(results: dict, output_path: Path) -> None:
    """Generate validation report."""
    total = results['total_validated']

    if total == 0:
        print("\nNo addresses validated.")
        return

    # Calculate statistics
    exact_rate = (results['exact_matches'] / total) * 100
    street_rate = (results['street_matches'] / total) * 100
    mismatch_rate = (results['mismatches'] / total) * 100

    # Calculate overall match rate (exact + street matches)
    match_rate = ((results['exact_matches'] + results['street_matches']) / total) * 100

    # Estimated cost ($5 per 1000 requests)
    estimated_cost = (results['api_calls'] / 1000) * 5

    report = {
        'validation_date': datetime.now().isoformat(),
        'summary': {
            'total_validated': total,
            'exact_matches': results['exact_matches'],
            'street_matches': results['street_matches'],
            'number_matches': results['number_matches'],
            'partial_matches': results['partial_matches'],
            'mismatches': results['mismatches'],
            'no_results': results['no_results']
        },
        'rates': {
            'exact_match_rate': round(exact_rate, 2),
            'street_match_rate': round(street_rate, 2),
            'overall_match_rate': round(match_rate, 2),
            'mismatch_rate': round(mismatch_rate, 2)
        },
        'api_usage': {
            'api_calls': results['api_calls'],
            'estimated_cost_usd': round(estimated_cost, 4)
        },
        'validations': results['validations']
    }

    # Print summary
    print("\n" + "=" * 60)
    print("VALIDATION SUMMARY")
    print("=" * 60)
    print(f"Total Validated:     {total}")
    print(f"Exact Matches:       {results['exact_matches']} ({exact_rate:.1f}%)")
    print(f"Street Matches:      {results['street_matches']} ({street_rate:.1f}%)")
    print(f"Partial Matches:     {results['partial_matches']}")
    print(f"Number Only Matches: {results['number_matches']}")
    print(f"Mismatches:          {results['mismatches']} ({mismatch_rate:.1f}%)")
    print(f"No Results:          {results['no_results']}")
    print("-" * 60)
    print(f"Overall Match Rate:  {match_rate:.1f}%")
    print(f"API Calls:           {results['api_calls']}")
    print(f"Estimated Cost:      ${estimated_cost:.4f}")
    print("=" * 60)

    # Show mismatches for review
    mismatches = [v for v in results['validations']
                  if v['validation']['match_type'] == 'mismatch']

    if mismatches:
        print("\nMISMATCHES FOR REVIEW:")
        print("-" * 60)
        for m in mismatches[:10]:  # Show first 10
            print(f"  Our:    {m['our_address']}")
            print(f"  Google: {m['validation'].get('google_formatted', 'N/A')}")
            print(f"  Coords: ({m['coordinates']['lat']}, {m['coordinates']['lng']})")
            print()
        if len(mismatches) > 10:
            print(f"  ... and {len(mismatches) - 10} more mismatches")

    # Save report
    with open(output_path, 'w') as f:
        json.dump(report, f, indent=2)

    print(f"\nFull report saved to: {output_path}")


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Validate geocoding results against Google Reverse Geocoding API'
    )
    parser.add_argument(
        '--sample',
        type=int,
        default=100,
        help='Number of addresses to validate (default: 100)'
    )
    parser.add_argument(
        '--input',
        type=str,
        default='sample_input_data/toronto_sample_properties_geocoded_SUCCESS.json',
        help='Input file with geocoded results'
    )
    parser.add_argument(
        '--output',
        type=str,
        default='reports/google_validation_report.json',
        help='Output file for validation report'
    )

    args = parser.parse_args()

    # Check Google API configuration
    if not is_google_api_enabled():
        print("ERROR: Google Geocoding API is not configured.")
        print("\nTo configure:")
        print("1. Get an API key from https://console.cloud.google.com/")
        print("2. Enable the Geocoding API")
        print("3. Add to your .env file:")
        print("   GOOGLE_GEOCODING_API_KEY=your_key_here")
        print("   GOOGLE_API_ENABLED=true")
        sys.exit(1)

    # Load input data
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}")
        sys.exit(1)

    with open(input_path) as f:
        data = json.load(f)

    addresses = data.get('addresses', [])
    if not addresses:
        print("ERROR: No addresses found in input file")
        sys.exit(1)

    print(f"Loaded {len(addresses)} addresses from {input_path}")

    # Validate sample
    sample_size = min(args.sample, len(addresses))
    results = await validate_addresses(addresses, sample_size)

    # Generate report
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    generate_report(results, output_path)


if __name__ == '__main__':
    asyncio.run(main())
