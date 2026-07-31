#!/usr/bin/env python3
"""
Simple geocoder using Nominatim's public API via geopy.
Adds latitude/longitude coordinates to address JSON objects.
"""

import json
import time
from pathlib import Path
from typing import Dict, Optional
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError


class NominatimGeocoder:
    def __init__(self, user_agent: str = "geocoder_app", rate_limit_seconds: float = 1.0):
        """
        Initialize Nominatim geocoder.

        Args:
            user_agent: Identifier for your application (required by Nominatim)
            rate_limit_seconds: Delay between requests (Nominatim requires max 1 req/sec)
        """
        self.geolocator = Nominatim(user_agent=user_agent)
        self.rate_limit = rate_limit_seconds
        self.last_request_time = 0

    def _build_address_string(self, address: Dict) -> str:
        """Build a formatted address string from address components."""
        parts = []

        # Handle both PascalCase and snake_case formats
        # Build street address from components if not already provided
        street_address = (
            address.get("StreetAddress") or
            address.get("street_address") or
            address.get("AddressLine1") or
            address.get("address_line1")
        )

        if not street_address:
            # Build from components
            num = address.get("StreetNumber") or address.get("street_number", "")
            name = address.get("StreetName") or address.get("street_name", "")
            suffix = address.get("StreetSuffix") or address.get("street_suffix", "")
            direction = address.get("StreetDirection") or address.get("street_direction", "")
            unit = address.get("UnitNo") or address.get("unit_no", "")

            components = [num, name, suffix]
            if direction:
                components.append(direction)
            street_address = " ".join(filter(None, components))
            if unit:
                street_address = f"Unit {unit}, {street_address}"

        if street_address:
            parts.append(street_address)

        # City
        city = address.get("City") or address.get("city") or address.get("CommunityName")
        if city:
            parts.append(city)

        # Province
        province = address.get("Province") or address.get("province")
        if province:
            parts.append(province)

        # Postal code (format Canadian postal codes properly)
        postal = address.get("PostalCode") or address.get("postal_code")
        if postal:
            # Add space to Canadian postal codes if missing (A1A1A1 -> A1A 1A1)
            postal = postal.strip().replace(" ", "")
            if len(postal) == 6 and postal.isalnum():
                postal = f"{postal[:3]} {postal[3:]}"
            parts.append(postal)

        # Country
        country = address.get("Country") or address.get("country")
        if country:
            parts.append(country)

        return ", ".join(parts)

    def _respect_rate_limit(self):
        """Ensure we don't exceed Nominatim's rate limit."""
        elapsed = time.time() - self.last_request_time
        if elapsed < self.rate_limit:
            time.sleep(self.rate_limit - elapsed)
        self.last_request_time = time.time()

    def geocode_address(self, address: Dict) -> Optional[Dict]:
        """
        Geocode an address and return coordinates.

        Args:
            address: Dictionary containing address components

        Returns:
            Dict with lat/lon if successful, None otherwise
        """
        address_string = self._build_address_string(address)

        try:
            self._respect_rate_limit()
            location = self.geolocator.geocode(address_string, timeout=10)

            if location:
                return {
                    "latitude": location.latitude,
                    "longitude": location.longitude,
                    "geocoded_address": location.address
                }
            else:
                print(f"[FAIL] No results found for: {address_string}")
                return None

        except GeocoderTimedOut:
            print(f"[WARN] Timeout for: {address_string}")
            return None
        except GeocoderServiceError as e:
            print(f"⚠️  Service error for {address_string}: {e}")
            return None
        except Exception as e:
            print(f"[FAIL] Error geocoding {address_string}: {e}")
            return None

    def process_json_file(self, input_file: str, output_file: str = None, terminal_output: bool = False):
        """
        Read JSON file, geocode addresses, and write results.

        Args:
            input_file: Path to input JSON file
            output_file: Path to output JSON file (optional if terminal_output=True)
            terminal_output: If True, print results as table instead of writing to file
        """
        # Read input file, handling potential headers
        with open(input_file, 'r') as f:
            content = f.read()
            # Find the first { or [ to start parsing JSON
            json_start = min(
                content.find('{') if content.find('{') != -1 else len(content),
                content.find('[') if content.find('[') != -1 else len(content)
            )
            if json_start == len(content):
                raise ValueError("No JSON content found in file")
            data = json.loads(content[json_start:])

        # Determine format and get address list
        if isinstance(data, list):
            # Format: Array of records with "Address" field
            addresses = data
            format_type = "nested"
        elif isinstance(data, dict) and "addresses" in data:
            # Format: Object with "addresses" array
            addresses = data["addresses"]
            format_type = "flat"
        elif isinstance(data, dict) and "PropertyDetails" in data:
            # Format: Single property with PropertyDetails
            addresses = [data]
            format_type = "property_details"
        elif isinstance(data, dict) and "Address" in data:
            # Format: Single record with Address field
            addresses = [data]
            format_type = "nested"
        else:
            raise ValueError("Unrecognized JSON format. Expected list of records, object with 'addresses' key, or PropertyDetails object")

        # Process each record
        total = len(addresses)
        geocoded = 0
        results_table = []

        print(f"Processing {total} addresses (format: {format_type})...")

        for i, record in enumerate(addresses, 1):
            # Extract address based on format
            if format_type == "property_details":
                if "PropertyDetails" not in record or "Address" not in record["PropertyDetails"]:
                    print(f"⚠️  Record {i} missing 'PropertyDetails.Address' field, skipping")
                    continue
                address = record["PropertyDetails"]["Address"]
            elif format_type == "nested":
                if "Address" not in record:
                    print(f"⚠️  Record {i} missing 'Address' field, skipping")
                    continue
                address = record["Address"]
            else:
                address = record

            # Build display name
            display = (
                address.get("StreetAddress") or
                address.get("street_address") or
                f"{address.get('street_number', '')} {address.get('street_name', '')}".strip() or
                "N/A"
            )

            if not terminal_output:
                print(f"[{i}/{total}] Geocoding: {display}")

            result = self.geocode_address(address)

            if result:
                geocode_data = {
                    "latitude": result["latitude"],
                    "longitude": result["longitude"],
                    "source": "Nominatim",
                    "geocoded_address": result["geocoded_address"]
                }

                if format_type == "property_details":
                    record["PropertyDetails"]["Geocode"] = geocode_data
                elif format_type == "nested":
                    record["Geocode"] = geocode_data
                else:
                    record.update(geocode_data)

                geocoded += 1

                if terminal_output:
                    results_table.append({
                        "address": display,
                        "lat": result["latitude"],
                        "lon": result["longitude"]
                    })
                else:
                    print(f"✓ Geocoded: {result['latitude']}, {result['longitude']}")
            else:
                if format_type == "property_details":
                    record["PropertyDetails"]["Geocode"] = None
                elif format_type == "nested":
                    record["Geocode"] = None
                else:
                    record.update({"latitude": None, "longitude": None})

                if terminal_output:
                    results_table.append({
                        "address": display,
                        "lat": "FAILED",
                        "lon": "FAILED"
                    })

        # Output results
        if terminal_output:
            # Print as table
            print(f"\n{'='*80}")
            print(f"{'ADDRESS':<50} {'LATITUDE':<15} {'LONGITUDE':<15}")
            print(f"{'-'*80}")
            for row in results_table:
                print(f"{row['address']:<50} {str(row['lat']):<15} {str(row['lon']):<15}")
            print(f"{'='*80}")
            print(f"✓ Complete! Geocoded {geocoded}/{total} addresses")
        else:
            # Write to file
            if output_file:
                with open(output_file, 'w') as f:
                    json.dump(data, f, indent=2)
                print(f"\n{'='*60}")
                print(f"✓ Complete! Geocoded {geocoded}/{total} addresses")
                print(f"✓ Results written to: {output_file}")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Geocode addresses using Nominatim")
    parser.add_argument("input_file", help="Input JSON file with addresses")
    parser.add_argument("-o", "--output", help="Output JSON file (default: input_geocoded.json)")
    parser.add_argument("-t", "--terminal", action="store_true",
                       help="Print results to terminal as table instead of writing to file")
    parser.add_argument("--user-agent", default="geocoder_app",
                       help="User agent for Nominatim (default: geocoder_app)")
    parser.add_argument("--rate-limit", type=float, default=1.0,
                       help="Seconds between requests (default: 1.0)")

    args = parser.parse_args()

    # Determine output file
    output_file = None
    if not args.terminal:
        if args.output:
            output_file = args.output
        else:
            input_path = Path(args.input_file)
            output_file = input_path.parent / f"{input_path.stem}_geocoded{input_path.suffix}"

    # Run geocoder
    geocoder = NominatimGeocoder(
        user_agent=args.user_agent,
        rate_limit_seconds=args.rate_limit
    )

    geocoder.process_json_file(
        args.input_file,
        str(output_file) if output_file else None,
        terminal_output=args.terminal
    )


if __name__ == "__main__":
    main()
