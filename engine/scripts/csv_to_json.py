#!/usr/bin/env python3
import argparse
import csv
import json

def csv_to_json(csv_file, json_file):
    """Convert CSV file to JSON format"""
    data = []

    # Define field names for CSV columns (no header row in this CSV)
    fieldnames = [
        'street_number',
        'street_name',
        'street_suffix',
        'street_direction',
        'unit_no',
        'city',
        'province',
        'postal_code'
    ]

    with open(csv_file, 'r', encoding='utf-8') as f:
        csv_reader = csv.DictReader(f, fieldnames=fieldnames)
        for row in csv_reader:
            # Convert empty strings to None for cleaner JSON
            cleaned_row = {k: (v if v else None) for k, v in row.items()}
            data.append(cleaned_row)

    # Wrap in batch format for API
    batch_data = {"addresses": data}

    with open(json_file, 'w', encoding='utf-8') as f:
        json.dump(batch_data, f, indent=2, ensure_ascii=False)

    print(f"Converted {len(data)} records from {csv_file} to {json_file}")
    print(f"Ready for batch submission to /geocode/batch")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Convert a headerless address CSV to batch-geocode JSON')
    parser.add_argument('input', help='Path to the input CSV file')
    parser.add_argument('output', help='Path to write the output JSON file')
    args = parser.parse_args()

    csv_to_json(args.input, args.output)
