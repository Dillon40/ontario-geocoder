#!/usr/bin/env python3
"""
Batch geocode Toronto addresses via the production API.

Reads addresses from JSON file, sends them to the /geocode/batch endpoint,
and saves the results.

Usage:
    python batch_geocode_toronto.py --input addresses.json --output results.json
    (or set GEOCODER_BATCH_INPUT / GEOCODER_BATCH_OUTPUT env vars)
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
import httpx
from typing import List, Dict, Any

# Configuration
API_URL = os.getenv("GEOCODER_API_URL", "http://localhost:3000")
API_KEY = os.getenv("GEOCODER_API_KEY")
if not API_KEY:
    sys.exit(
        "GEOCODER_API_KEY environment variable is required. "
        "Export a valid API key before running this script."
    )
BATCH_SIZE = 100  # API max is 1000, using 100 for better progress tracking


def read_json_addresses(json_path: str) -> List[Dict[str, Any]]:
    """Read addresses from JSON file."""
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Extract addresses array
    addresses = data.get('addresses', [])

    if not addresses:
        raise ValueError("No 'addresses' array found in JSON file")

    return addresses


def send_batch(client: httpx.Client, addresses: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Send a batch of addresses to the API."""
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": API_KEY
    }

    payload = {
        "addresses": addresses
    }

    response = client.post(
        f"{API_URL}/geocode/batch",
        json=payload,
        headers=headers,
        timeout=300.0  # 5 minutes timeout for large batches
    )

    response.raise_for_status()
    return response.json()


def main():
    parser = argparse.ArgumentParser(description='Batch geocode addresses via the production API')
    parser.add_argument(
        '--input',
        type=str,
        default=os.getenv('GEOCODER_BATCH_INPUT'),
        required=os.getenv('GEOCODER_BATCH_INPUT') is None,
        help='Input JSON file of addresses (or set GEOCODER_BATCH_INPUT)'
    )
    parser.add_argument(
        '--output',
        type=str,
        default=os.getenv('GEOCODER_BATCH_OUTPUT'),
        required=os.getenv('GEOCODER_BATCH_OUTPUT') is None,
        help='Output JSON file for results (or set GEOCODER_BATCH_OUTPUT)'
    )
    args = parser.parse_args()
    json_file = args.input
    output_file = args.output

    print("=" * 70)
    print("Toronto Address Batch Geocoding")
    print("=" * 70)
    print()

    # Check if JSON exists
    json_path = Path(json_file)
    if not json_path.exists():
        print(f"Error: JSON file not found at {json_file}")
        return 1

    # Read addresses
    print(f"Reading addresses from {json_file}...")
    try:
        addresses = read_json_addresses(json_file)
    except Exception as e:
        print(f"Error reading JSON file: {e}")
        return 1

    print(f"Loaded {len(addresses):,} addresses")
    print()

    # Process in batches
    all_results = []
    total_batches = (len(addresses) + BATCH_SIZE - 1) // BATCH_SIZE

    print(f"Processing {total_batches} batches of {BATCH_SIZE} addresses each...")
    print("-" * 70)

    start_time = time.time()

    with httpx.Client() as client:
        for batch_num in range(total_batches):
            batch_start = batch_num * BATCH_SIZE
            batch_end = min(batch_start + BATCH_SIZE, len(addresses))
            batch = addresses[batch_start:batch_end]

            batch_start_time = time.time()

            try:
                result = send_batch(client, batch)
                batch_time = time.time() - batch_start_time

                # Store results with corrected indices
                batch_results = result.get('results', [])
                for r in batch_results:
                    # Add batch offset to index
                    if 'index' in r:
                        r['index'] += batch_start
                all_results.extend(batch_results)

                # Progress report
                summary = result.get('summary', {})
                successful = summary.get('successful', 0)
                failed = summary.get('failed', 0)
                cache_hits = summary.get('cache_hits', 0)

                print(f"Batch {batch_num + 1}/{total_batches}: "
                      f"{successful} successful, {failed} failed, "
                      f"{cache_hits} cache hits "
                      f"({batch_time:.1f}s)")

            except httpx.HTTPStatusError as e:
                print(f"Error in batch {batch_num + 1}: {e}")
                print(f"Response: {e.response.text}")
                # Continue with next batch
                continue

            except Exception as e:
                print(f"Unexpected error in batch {batch_num + 1}: {e}")
                continue

            # Small delay between batches to avoid overwhelming the API
            if batch_num < total_batches - 1:
                time.sleep(0.5)

    total_time = time.time() - start_time

    # Summary
    print("-" * 70)
    print()
    print("Summary:")
    print(f"  Total addresses processed: {len(all_results):,}")
    print(f"  Total time: {total_time:.1f} seconds")
    print(f"  Average time per address: {total_time / len(all_results):.3f} seconds")
    print()

    # Count successes
    successful_count = sum(1 for r in all_results if r.get('success'))
    failed_count = len(all_results) - successful_count
    cache_count = sum(1 for r in all_results if r.get('cached'))

    print(f"  Successful geocodes: {successful_count:,} ({successful_count/len(all_results)*100:.1f}%)")
    print(f"  Failed geocodes: {failed_count:,} ({failed_count/len(all_results)*100:.1f}%)")
    print(f"  Cache hits: {cache_count:,} ({cache_count/len(all_results)*100:.1f}%)")
    print()

    # Tier distribution
    tier_counts = {}
    for result in all_results:
        if result.get('success'):
            tier = result.get('match_tier', 'unknown')
            tier_counts[tier] = tier_counts.get(tier, 0) + 1

    if tier_counts:
        print("  Tier distribution:")
        for tier in sorted(tier_counts.keys()):
            count = tier_counts[tier]
            print(f"    Tier {tier}: {count:,} ({count/successful_count*100:.1f}%)")
        print()

    # Save results
    output_path = Path(output_file)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump({
            'metadata': {
                'source_file': json_file,
                'total_addresses': len(addresses),
                'processed_addresses': len(all_results),
                'successful': successful_count,
                'failed': failed_count,
                'cache_hits': cache_count,
                'total_time_seconds': total_time,
                'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
            },
            'tier_distribution': tier_counts,
            'results': all_results
        }, f, indent=2)

    print(f"Results saved to {output_file}")
    print()
    print("=" * 70)

    return 0


if __name__ == '__main__':
    sys.exit(main())
