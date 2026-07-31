#!/usr/bin/env python3
"""
Google Forward Geocoding Validator

Validates geocoding accuracy by sending the same addresses to Google's forward
geocoding API and comparing the coordinate distances.

Usage:
    python validate_google_forward.py
    python validate_google_forward.py --sample 500
    python validate_google_forward.py --input path/to/results.json
"""

import argparse
import asyncio
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Dict, List
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


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate the great circle distance in meters between two points
    on the earth (specified in decimal degrees).

    Returns distance in meters.
    """
    # Convert decimal degrees to radians
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])

    # Haversine formula
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
    c = 2 * math.asin(math.sqrt(a))

    # Radius of earth in meters
    r = 6371000

    return c * r


def format_address_for_google(address_data: Dict[str, Any]) -> str:
    """Format address data into a string for Google's API."""
    # Try to build structured address
    parts = []

    # Street address
    if address_data.get('street_number'):
        parts.append(str(address_data['street_number']))

    if address_data.get('street_name'):
        street = str(address_data['street_name'])
        if address_data.get('street_suffix'):
            street += ' ' + str(address_data['street_suffix'])
        if address_data.get('street_direction'):
            street += ' ' + str(address_data['street_direction'])
        parts.append(street)

    # Unit number
    if address_data.get('unit_no'):
        parts[0] = f"Unit {address_data['unit_no']}, {parts[0]}" if parts else f"Unit {address_data['unit_no']}"

    # City, Province, Postal Code
    if address_data.get('city'):
        parts.append(str(address_data['city']))

    if address_data.get('province'):
        parts.append(str(address_data['province']))

    if address_data.get('postal_code'):
        parts.append(str(address_data['postal_code']))

    return ', '.join(parts)


async def forward_geocode(address: str) -> Optional[Dict[str, Any]]:
    """
    Forward geocode an address using Google Geocoding API.

    Args:
        address: Address string to geocode

    Returns:
        Geocoding result or None
    """
    api_key = os.getenv('GOOGLE_GEOCODING_API_KEY')

    params = {
        'address': address,
        'key': api_key,
        'region': 'ca'  # Bias results to Canada
    }
    url = f'https://maps.googleapis.com/maps/api/geocode/json?{urlencode(params)}'

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=10.0)
            data = response.json()

        if data.get('status') == 'OK' and data.get('results'):
            result = data['results'][0]
            location = result['geometry']['location']

            return {
                'latitude': location['lat'],
                'longitude': location['lng'],
                'formatted_address': result.get('formatted_address', ''),
                'location_type': result['geometry'].get('location_type', ''),
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
        print(f'Forward geocoding error: {error}')
        return None


def categorize_distance(distance_m: float) -> str:
    """Categorize distance into accuracy buckets."""
    if distance_m < 10:
        return 'excellent'  # < 10m
    elif distance_m < 50:
        return 'very_good'  # 10-50m
    elif distance_m < 100:
        return 'good'       # 50-100m
    elif distance_m < 500:
        return 'acceptable' # 100-500m
    else:
        return 'poor'       # > 500m


async def validate_addresses(
    our_results: List[Dict[str, Any]],
    original_addresses: List[Dict[str, Any]],
    sample_size: int
) -> Dict[str, Any]:
    """
    Validate addresses against Google's forward geocoding.

    Args:
        our_results: Our geocoding results
        original_addresses: Original input addresses
        sample_size: Number of addresses to validate

    Returns:
        Validation results
    """
    # Take sample
    sample_our = our_results[:sample_size]

    results = {
        'total_validated': 0,
        'successful_comparisons': 0,
        'our_failures': 0,
        'google_failures': 0,
        'distance_stats': {
            'excellent': 0,      # < 10m
            'very_good': 0,      # 10-50m
            'good': 0,           # 50-100m
            'acceptable': 0,     # 100-500m
            'poor': 0            # > 500m
        },
        'distances': [],
        'api_calls': 0,
        'validations': []
    }

    print(f"\nValidating {len(sample_our)} addresses against Google Forward Geocoding API...")
    print("-" * 70)

    for i, our_result in enumerate(sample_our, 1):
        our_lat = our_result.get('latitude')
        our_lng = our_result.get('longitude')

        # Skip if we didn't geocode it
        if our_lat is None or our_lng is None:
            results['our_failures'] += 1
            print(f"[{i}/{len(sample_our)}] ✗ Skipped - no coordinates from our geocoder")
            continue

        # Get the original address using the index from our result
        result_index = our_result.get('index', i - 1)
        if result_index >= len(original_addresses):
            print(f"[{i}/{len(sample_our)}] ✗ Error - index out of range: {result_index}")
            continue

        original_addr = original_addresses[result_index]

        # Format address for Google
        address_str = format_address_for_google(original_addr)

        # Call Google API
        google_result = await forward_geocode(address_str)
        results['api_calls'] += 1
        results['total_validated'] += 1

        if not google_result:
            results['google_failures'] += 1
            validation = {
                'index': i - 1,
                'input_address': address_str,
                'our_coordinates': {'lat': our_lat, 'lng': our_lng},
                'our_matched_address': our_result.get('matched_address', ''),
                'our_source': our_result.get('source', ''),
                'our_tier': our_result.get('match_tier', 0),
                'google_result': None,
                'distance_m': None,
                'accuracy': 'google_failed'
            }
            results['validations'].append(validation)
            print(f"[{i}/{len(sample_our)}] ! Google failed to geocode: {address_str[:50]}")
            continue

        # Calculate distance
        google_lat = google_result['latitude']
        google_lng = google_result['longitude']
        distance_m = haversine_distance(our_lat, our_lng, google_lat, google_lng)

        # Categorize
        accuracy = categorize_distance(distance_m)
        results['distance_stats'][accuracy] += 1
        results['distances'].append(distance_m)
        results['successful_comparisons'] += 1

        # Store validation result
        validation = {
            'index': i - 1,
            'input_address': address_str,
            'our_coordinates': {'lat': our_lat, 'lng': our_lng},
            'our_matched_address': our_result.get('matched_address', ''),
            'our_source': our_result.get('source', ''),
            'our_tier': our_result.get('match_tier', 0),
            'our_confidence': our_result.get('confidence', 0),
            'google_coordinates': {'lat': google_lat, 'lng': google_lng},
            'google_formatted_address': google_result['formatted_address'],
            'google_location_type': google_result['location_type'],
            'distance_m': round(distance_m, 2),
            'accuracy': accuracy
        }
        results['validations'].append(validation)

        # Progress indicator with accuracy symbol
        symbols = {
            'excellent': '★',
            'very_good': '✓',
            'good': '~',
            'acceptable': '○',
            'poor': '✗'
        }
        symbol = symbols.get(accuracy, '?')

        google_addr = google_result['formatted_address'][:45]
        print(f"[{i}/{len(sample_our)}] {symbol} {distance_m:6.1f}m - {our_result.get('matched_address', '')} → {google_addr}")

        # Small delay to avoid rate limiting
        if i < len(sample_our):
            await asyncio.sleep(0.05)

    return results


def generate_report(results: Dict[str, Any], output_path: Path) -> None:
    """Generate validation report."""
    total = results['total_validated']
    successful = results['successful_comparisons']

    if total == 0:
        print("\nNo addresses validated.")
        return

    # Calculate statistics
    distances = results['distances']
    if distances:
        avg_distance = sum(distances) / len(distances)
        median_distance = sorted(distances)[len(distances) // 2]
        max_distance = max(distances)
        min_distance = min(distances)

        # Percentiles
        distances_sorted = sorted(distances)
        p50 = distances_sorted[int(len(distances_sorted) * 0.50)]
        p90 = distances_sorted[int(len(distances_sorted) * 0.90)]
        p95 = distances_sorted[int(len(distances_sorted) * 0.95)]
        p99 = distances_sorted[int(len(distances_sorted) * 0.99)]
    else:
        avg_distance = median_distance = max_distance = min_distance = 0
        p50 = p90 = p95 = p99 = 0

    stats = results['distance_stats']

    # Calculate percentages
    excellent_rate = (stats['excellent'] / successful * 100) if successful > 0 else 0
    very_good_rate = (stats['very_good'] / successful * 100) if successful > 0 else 0
    good_rate = (stats['good'] / successful * 100) if successful > 0 else 0
    acceptable_rate = (stats['acceptable'] / successful * 100) if successful > 0 else 0
    poor_rate = (stats['poor'] / successful * 100) if successful > 0 else 0

    # Combined accuracy rates
    within_10m = stats['excellent']
    within_50m = stats['excellent'] + stats['very_good']
    within_100m = within_50m + stats['good']
    within_500m = within_100m + stats['acceptable']

    # Estimated cost ($5 per 1000 requests)
    estimated_cost = (results['api_calls'] / 1000) * 5

    report = {
        'validation_date': datetime.now().isoformat(),
        'summary': {
            'total_validated': total,
            'successful_comparisons': successful,
            'our_failures': results['our_failures'],
            'google_failures': results['google_failures']
        },
        'distance_stats': {
            'avg_distance_m': round(avg_distance, 2),
            'median_distance_m': round(median_distance, 2),
            'min_distance_m': round(min_distance, 2),
            'max_distance_m': round(max_distance, 2),
            'p50_distance_m': round(p50, 2),
            'p90_distance_m': round(p90, 2),
            'p95_distance_m': round(p95, 2),
            'p99_distance_m': round(p99, 2)
        },
        'accuracy_breakdown': {
            'excellent_lt_10m': stats['excellent'],
            'very_good_10_50m': stats['very_good'],
            'good_50_100m': stats['good'],
            'acceptable_100_500m': stats['acceptable'],
            'poor_gt_500m': stats['poor']
        },
        'accuracy_rates': {
            'excellent_rate': round(excellent_rate, 2),
            'very_good_rate': round(very_good_rate, 2),
            'good_rate': round(good_rate, 2),
            'acceptable_rate': round(acceptable_rate, 2),
            'poor_rate': round(poor_rate, 2),
            'within_10m_rate': round((within_10m / successful * 100) if successful > 0 else 0, 2),
            'within_50m_rate': round((within_50m / successful * 100) if successful > 0 else 0, 2),
            'within_100m_rate': round((within_100m / successful * 100) if successful > 0 else 0, 2),
            'within_500m_rate': round((within_500m / successful * 100) if successful > 0 else 0, 2)
        },
        'api_usage': {
            'api_calls': results['api_calls'],
            'estimated_cost_usd': round(estimated_cost, 4)
        },
        'validations': results['validations']
    }

    # Print summary
    print("\n" + "=" * 70)
    print("VALIDATION SUMMARY")
    print("=" * 70)
    print(f"Total Validated:       {total}")
    print(f"Successful Comparisons: {successful}")
    print(f"Our Failures:          {results['our_failures']}")
    print(f"Google Failures:       {results['google_failures']}")
    print("\n" + "-" * 70)
    print("DISTANCE STATISTICS")
    print("-" * 70)
    print(f"Average Distance:      {avg_distance:.2f} meters")
    print(f"Median Distance:       {median_distance:.2f} meters")
    print(f"Min Distance:          {min_distance:.2f} meters")
    print(f"Max Distance:          {max_distance:.2f} meters")
    print(f"50th Percentile:       {p50:.2f} meters")
    print(f"90th Percentile:       {p90:.2f} meters")
    print(f"95th Percentile:       {p95:.2f} meters")
    print(f"99th Percentile:       {p99:.2f} meters")
    print("\n" + "-" * 70)
    print("ACCURACY BREAKDOWN")
    print("-" * 70)
    print(f"Excellent (< 10m):     {stats['excellent']:4d} ({excellent_rate:.1f}%)")
    print(f"Very Good (10-50m):    {stats['very_good']:4d} ({very_good_rate:.1f}%)")
    print(f"Good (50-100m):        {stats['good']:4d} ({good_rate:.1f}%)")
    print(f"Acceptable (100-500m): {stats['acceptable']:4d} ({acceptable_rate:.1f}%)")
    print(f"Poor (> 500m):         {stats['poor']:4d} ({poor_rate:.1f}%)")
    print("\n" + "-" * 70)
    print("CUMULATIVE ACCURACY")
    print("-" * 70)
    print(f"Within 10 meters:      {within_10m:4d} ({(within_10m/successful*100) if successful > 0 else 0:.1f}%)")
    print(f"Within 50 meters:      {within_50m:4d} ({(within_50m/successful*100) if successful > 0 else 0:.1f}%)")
    print(f"Within 100 meters:     {within_100m:4d} ({(within_100m/successful*100) if successful > 0 else 0:.1f}%)")
    print(f"Within 500 meters:     {within_500m:4d} ({(within_500m/successful*100) if successful > 0 else 0:.1f}%)")
    print("\n" + "-" * 70)
    print(f"API Calls:             {results['api_calls']}")
    print(f"Estimated Cost:        ${estimated_cost:.4f}")
    print("=" * 70)

    # Show worst performers
    worst = sorted(results['validations'],
                   key=lambda x: x.get('distance_m', 0) if x.get('distance_m') is not None else 0,
                   reverse=True)[:10]

    if worst and worst[0].get('distance_m'):
        print("\nWORST 10 MATCHES (Largest Distance):")
        print("-" * 70)
        for w in worst:
            if w.get('distance_m') is None:
                continue
            print(f"  Distance: {w['distance_m']:8.1f}m")
            print(f"  Our:      {w.get('our_matched_address', 'N/A')}")
            print(f"  Google:   {w.get('google_formatted_address', 'N/A')}")
            print()

    # Save report
    with open(output_path, 'w') as f:
        json.dump(report, f, indent=2)

    print(f"\nFull report saved to: {output_path}")


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Validate geocoding results against Google Forward Geocoding API'
    )
    parser.add_argument(
        '--sample',
        type=int,
        default=5000,
        help='Number of addresses to validate (default: 5000)'
    )
    parser.add_argument(
        '--results',
        type=str,
        default=os.getenv('VALIDATE_RESULTS_FILE'),
        required=os.getenv('VALIDATE_RESULTS_FILE') is None,
        help='Our geocoding results file (or set VALIDATE_RESULTS_FILE)'
    )
    parser.add_argument(
        '--input',
        type=str,
        default=os.getenv('VALIDATE_INPUT_FILE'),
        required=os.getenv('VALIDATE_INPUT_FILE') is None,
        help='Original input addresses file (or set VALIDATE_INPUT_FILE)'
    )
    parser.add_argument(
        '--output',
        type=str,
        default='reports/google_forward_validation_report.json',
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

    # Load our results
    results_path = Path(args.results)
    if not results_path.exists():
        print(f"ERROR: Results file not found: {results_path}")
        sys.exit(1)

    with open(results_path) as f:
        our_data = json.load(f)

    our_results = our_data.get('results', [])
    if not our_results:
        print("ERROR: No results found in results file")
        sys.exit(1)

    # Load original addresses
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}")
        sys.exit(1)

    with open(input_path) as f:
        input_data = json.load(f)

    original_addresses = input_data.get('addresses', [])
    if not original_addresses:
        print("ERROR: No addresses found in input file")
        sys.exit(1)

    print(f"Loaded {len(our_results)} our results and {len(original_addresses)} original addresses")

    # Filter to only our successful geocodes
    successful_results = [r for r in our_results if r.get('latitude') is not None]
    print(f"Found {len(successful_results)} successful geocodes to validate")

    # Validate sample
    sample_size = min(args.sample, len(successful_results))
    results = await validate_addresses(successful_results, original_addresses, sample_size)

    # Generate report
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    generate_report(results, output_path)


if __name__ == '__main__':
    asyncio.run(main())
