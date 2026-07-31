#!/usr/bin/env python3
"""
Bootstrap script to create the initial admin API key.
Run this on fresh deployments to create the first admin user.

Usage:
    python scripts/create_admin_key.py --name "Admin Key" --owner "admin@example.com"

    # Or with environment variables:
    DB_HOST=localhost python scripts/create_admin_key.py --name "Admin Key"
"""
import os
import sys
import argparse
import secrets
import json
import asyncio

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg
from dotenv import load_dotenv

from api.services import api_keys as api_keys_service

# Load environment variables
load_dotenv()


def generate_key(prefix: str = 'gc_') -> str:
    """Generate a secure API key with prefix."""
    random_bytes = secrets.token_hex(24)  # 48 character hex string
    return f"{prefix}{random_bytes}"


async def create_admin_key(
    name: str,
    owner: str = None,
    rate_limit: int = None
) -> dict:
    """
    Create an admin API key directly in the database.

    Args:
        name: Human-readable name for the key
        owner: Owner identifier (email, username, etc.)
        rate_limit: Optional rate limit override

    Returns:
        Dictionary with key details including the plain key
    """
    # Database configuration
    db_config = {
        'host': os.getenv('DB_HOST', 'postgres'),
        'port': int(os.getenv('DB_PORT', 5432)),
        'database': os.getenv('DB_NAME', 'geocoder'),
        'user': os.getenv('DB_USER', 'geocoder'),
        'password': os.getenv('DB_PASSWORD'),
    }

    # Check for required password
    if not db_config['password']:
        raise ValueError(
            "DB_PASSWORD environment variable is required. "
            "Set it before running this script."
        )

    # Generate key. Hashing and prefix extraction reuse api.services.api_keys
    # so bootstrap-created keys use the same pepper/HMAC scheme and prefix
    # length as keys created through the API.
    prefix = os.getenv('API_KEY_PREFIX', 'gc_')
    plain_key = generate_key(prefix)
    key_hash = api_keys_service._hash_key(plain_key)
    key_prefix = api_keys_service._get_key_prefix(plain_key)

    # Admin permissions
    permissions = {
        'geocode': True,
        'batch': True,
        'stats': True,
        'admin': True
    }

    # Connect to database
    conn = await asyncpg.connect(**db_config)

    try:
        # Insert the key
        query = '''
            INSERT INTO api_keys (
                key_hash, key_prefix, name, owner,
                rate_limit_override, allowed_ips, permissions
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id, created_at
        '''

        result = await conn.fetchrow(
            query,
            key_hash,
            key_prefix,
            name,
            owner,
            rate_limit,
            None,  # allowed_ips - null means all IPs allowed
            json.dumps(permissions)
        )

        return {
            'id': result['id'],
            'key': plain_key,
            'key_prefix': key_prefix,
            'name': name,
            'owner': owner,
            'created_at': result['created_at'].isoformat(),
            'permissions': permissions
        }

    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(
        description='Create an admin API key for the geocoder service',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  %(prog)s --name "Admin Key" --owner "admin@example.com"
  DB_HOST=localhost %(prog)s --name "Bootstrap Admin"

Environment Variables:
  DB_HOST      Database host (default: postgres)
  DB_PORT      Database port (default: 5432)
  DB_NAME      Database name (default: geocoder)
  DB_USER      Database user (default: geocoder)
  DB_PASSWORD  Database password (REQUIRED)
'''
    )

    parser.add_argument(
        '--name', '-n',
        required=True,
        help='Name for the API key (e.g., "Admin Key")'
    )

    parser.add_argument(
        '--owner', '-o',
        default=None,
        help='Owner identifier (e.g., email address)'
    )

    parser.add_argument(
        '--rate-limit', '-r',
        type=int,
        default=None,
        help='Custom rate limit override (requests per minute)'
    )

    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("Geocoder API - Admin Key Bootstrap")
    print("=" * 60 + "\n")

    try:
        result = asyncio.run(create_admin_key(
            name=args.name,
            owner=args.owner,
            rate_limit=args.rate_limit
        ))

        print("Admin API key created successfully!\n")
        print("-" * 60)
        print(f"Key ID:      {result['id']}")
        print(f"Key Prefix:  {result['key_prefix']}")
        print(f"Name:        {result['name']}")
        print(f"Owner:       {result['owner'] or 'N/A'}")
        print(f"Created:     {result['created_at']}")
        print(f"Permissions: {', '.join(k for k, v in result['permissions'].items() if v)}")
        print("-" * 60)
        print("\nIMPORTANT: Store this key securely - it cannot be retrieved again!\n")
        print(f"API Key: {result['key']}")
        print("\n" + "=" * 60 + "\n")

        return 0

    except ValueError as e:
        print(f"Configuration Error: {e}", file=sys.stderr)
        return 1

    except asyncpg.PostgresError as e:
        print(f"Database Error: {e}", file=sys.stderr)
        return 1

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
