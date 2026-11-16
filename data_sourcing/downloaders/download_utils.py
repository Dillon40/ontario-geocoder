#!/usr/bin/env python3
"""
Shared utilities for all downloaders.

Provides consistent GeoJSON output, field validation, and common operations.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Standard data directory
DATA_DIR = Path(__file__).parent.parent / "data_sources"

# Path to dataset mappings config
CONFIG_DIR = Path(__file__).parent.parent.parent / "config"
MAPPINGS_FILE = CONFIG_DIR / "dataset-mappings.json"


def get_output_path(source_key: str) -> Path:
    """
    Get standard output path for a source.

    Returns: data_sources/{source_key}/{source_key}.geojson
    """
    return DATA_DIR / source_key / f"{source_key}.geojson"


def ensure_output_dir(source_key: str) -> Path:
    """Create output directory if needed and return output path."""
    output_path = get_output_path(source_key)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return output_path


# --- GeoJSON Streaming Helpers ---

def stream_geojson_start(file_handle) -> None:
    """Write GeoJSON FeatureCollection header."""
    file_handle.write('{"type": "FeatureCollection", "features": [\n')


def stream_geojson_feature(file_handle, feature: dict, is_first: bool) -> None:
    """Write a single GeoJSON feature with proper comma handling."""
    if not is_first:
        file_handle.write(",\n")
    json.dump(feature, file_handle, separators=(',', ':'))


def stream_geojson_end(file_handle) -> None:
    """Write GeoJSON FeatureCollection footer."""
    file_handle.write("\n]}")


# --- Field Validation ---

def get_expected_fields(source_key: str) -> Optional[set]:
    """
    Load expected field names from dataset-mappings.json.

    Returns set of source field names that should be present,
    or None if source not configured.
    """
    if not MAPPINGS_FILE.exists():
        return None

    try:
        with open(MAPPINGS_FILE) as f:
            mappings = json.load(f)

        if source_key not in mappings:
            return None

        field_mappings = mappings[source_key].get("field_mappings", {})
        # Return the source field names (values), not the target fields (keys)
        return {v for v in field_mappings.values() if v is not None}
    except Exception as e:
        logger.warning(f"Failed to load expected fields for {source_key}: {e}")
        return None


def validate_fields(source_key: str, actual_fields: set, expected_fields: Optional[set] = None) -> None:
    """
    Compare actual fields against expected and log warnings.

    This is informational only - does NOT block the download.
    """
    if expected_fields is None:
        expected_fields = get_expected_fields(source_key)

    if expected_fields is None:
        logger.debug(f"[{source_key}] No expected fields configured, skipping validation")
        return

    missing = expected_fields - actual_fields
    new = actual_fields - expected_fields

    if missing:
        logger.warning(f"[{source_key}] Missing expected fields: {sorted(missing)}")
    if new:
        logger.info(f"[{source_key}] New/unexpected fields: {sorted(new)}")
    if not missing and not new:
        logger.debug(f"[{source_key}] All expected fields present")


# --- ArcGIS Shared Functions ---

async def get_server_info(endpoint: str, client: httpx.AsyncClient) -> dict:
    """
    Query ArcGIS endpoint metadata.

    Returns: {
        "max_record_count": int,
        "oid_field": str,
        "fields": list[str],
        "supports_geojson": bool
    }
    """
    response = await client.get(f"{endpoint}", params={"f": "json"})
    response.raise_for_status()
    data = response.json()

    if "error" in data:
        raise ValueError(f"Server error: {data['error'].get('message', 'Unknown')}")

    # Get OID field name
    oid_field = data.get("objectIdFieldName") or data.get("objectIdField")
    if not oid_field:
        for field in data.get("fields", []):
            if field.get("type") == "esriFieldTypeOID":
                oid_field = field["name"]
                break
    oid_field = oid_field or "OBJECTID"

    # Check GeoJSON support
    supported_formats = data.get("supportedQueryFormats", "")
    supports_geojson = "geojson" in supported_formats.lower()

    return {
        "max_record_count": data.get("maxRecordCount", 1000),
        "oid_field": oid_field,
        "fields": [f["name"] for f in data.get("fields", [])],
        "supports_geojson": supports_geojson,
        "geometry_type": data.get("geometryType", ""),
    }


async def get_record_count(endpoint: str, client: httpx.AsyncClient) -> int:
    """Get total record count from endpoint."""
    response = await client.get(
        f"{endpoint}/query",
        params={"where": "1=1", "returnCountOnly": "true", "f": "json"}
    )
    response.raise_for_status()
    data = response.json()

    if "error" in data:
        raise ValueError(f"Count query error: {data['error'].get('message', 'Unknown')}")

    return data.get("count", 0)


def _parse_features(data: dict, output_format: str) -> list[dict]:
    """Parse features from ArcGIS response (GeoJSON or ESRI JSON)."""
    if output_format == "geojson":
        return data.get("features", [])

    features = []
    for f in data.get("features", []):
        geom = f.get("geometry", {})
        props = f.get("attributes", {})
        feature = {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [geom.get("x", 0), geom.get("y", 0)]
            } if geom else None,
            "properties": props
        }
        features.append(feature)
    return features


def _extract_last_oid(features: list[dict], oid_field: str, last_oid: int) -> int:
    """Find the maximum OID from a list of features."""
    new_last_oid = last_oid
    for feature in features:
        props = feature.get("properties", {})
        for field in [oid_field, oid_field.lower(), "OBJECTID", "objectid", "FID", "OBJECTID_1"]:
            if field in props:
                new_last_oid = max(new_last_oid, props[field])
                break
    return new_last_oid


async def fetch_arcgis_page_with_retry(
    endpoint: str,
    oid_field: str,
    last_oid: int,
    page_size: int,
    output_format: str,
    client: httpx.AsyncClient,
    max_retries: int = 3,
    backoff_base: float = 10.0,
    backoff_max: float = 120.0,
    source_key: str = "",
) -> tuple[list[dict], int]:
    """
    Fetch a single page of ArcGIS features with retry logic.

    Retries on timeouts, HTTP 429, and HTTP 503 with exponential backoff.
    Honors the Retry-After header when present.

    Returns: (features, new_last_oid)
    """
    params = {
        "where": f"{oid_field}>{last_oid}",
        "outFields": "*",
        "outSR": "4326",
        "f": output_format,
        "resultRecordCount": str(page_size),
        "orderByFields": f"{oid_field} ASC"
    }

    retries = 0
    backoff = backoff_base

    while retries < max_retries:
        try:
            response = await client.get(f"{endpoint}/query", params=params)

            if response.status_code in (429, 500, 502, 503, 504):
                retries += 1
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    wait_time = int(retry_after)
                else:
                    wait_time = min(backoff, backoff_max)
                    backoff *= 2

                logger.warning(
                    f"[{source_key}] Rate limited (HTTP {response.status_code}), "
                    f"waiting {wait_time:.0f}s (attempt {retries}/{max_retries})"
                )
                await asyncio.sleep(wait_time)
                continue

            response.raise_for_status()
            data = response.json()

            if "error" in data:
                raise ValueError(f"Query error: {data['error'].get('message', 'Unknown')}")

            features = _parse_features(data, output_format)
            new_last_oid = _extract_last_oid(features, oid_field, last_oid)
            return features, new_last_oid

        except (httpx.TimeoutException, ValueError):
            # Some ArcGIS gateways return an HTML error with HTTP 200.
            # Retry smaller pages; never interpret invalid JSON as an empty page.
            params['resultRecordCount'] = str(max(1, int(params['resultRecordCount']) // 2))
            retries += 1
            wait_time = min(backoff, backoff_max)
            backoff *= 2
            logger.warning(
                f"[{source_key}] Request timeout, waiting {wait_time:.0f}s "
                f"(attempt {retries}/{max_retries})"
            )
            await asyncio.sleep(wait_time)

    raise RuntimeError(f"Max retries ({max_retries}) exceeded for page after OID {last_oid}")


def get_adaptive_timeout(max_record_count: int) -> httpx.Timeout:
    """
    Compute an adaptive timeout based on server's maxRecordCount.

    For large page sizes (>5000), increase read timeout proportionally.
    Formula: max(60, maxRecordCount / 50) seconds for read timeout.
    """
    read_timeout = max(60, max_record_count / 50)
    return httpx.Timeout(read_timeout, connect=30.0)


def get_resume_oid(output_path: Path) -> Optional[int]:
    """
    Check for a partial GeoJSON download and extract the last OID written.

    Reads the last feature from the file and extracts its OID.
    Returns the OID to resume from, or None if no valid partial file.
    """
    if not output_path.exists():
        return None

    file_size = output_path.stat().st_size
    if file_size < 100:
        return None

    # Check if file is complete (ends with "]}")
    try:
        with open(output_path, 'rb') as f:
            f.seek(max(0, file_size - 10))
            tail = f.read().decode('utf-8', errors='ignore').strip()
            if tail.endswith(']}'):
                return None  # File is complete, no resume needed
    except Exception:
        return None

    # Try to read last feature from incomplete file
    try:
        with open(output_path, 'rb') as f:
            # Read last chunk to find the last complete JSON object
            chunk_size = min(file_size, 65536)
            f.seek(max(0, file_size - chunk_size))
            chunk = f.read().decode('utf-8', errors='ignore')

        # Find the last complete feature by looking for the last '{"type":"Feature"'
        last_idx = chunk.rfind('{"type":"Feature"')
        if last_idx == -1:
            return None

        # Extract from that point to find the OID
        remainder = chunk[last_idx:]
        # Find the end of this JSON object (matching braces)
        brace_count = 0
        end_idx = -1
        for i, ch in enumerate(remainder):
            if ch == '{':
                brace_count += 1
            elif ch == '}':
                brace_count -= 1
                if brace_count == 0:
                    end_idx = i + 1
                    break

        if end_idx == -1:
            return None

        feature = json.loads(remainder[:end_idx])
        props = feature.get("properties", {})

        # Look for OID in common field names
        for field in ["OBJECTID", "objectid", "FID", "OBJECTID_1", "OID"]:
            if field in props and isinstance(props[field], (int, float)):
                oid = int(props[field])
                logger.info(f"Found partial download, last OID: {oid}")
                return oid

        return None
    except Exception as e:
        logger.debug(f"Could not parse partial file for resume: {e}")
        return None


# --- File Operations ---

def calculate_file_hash(path: Path) -> str:
    """
    Calculate SHA256 hash of a file for change detection.

    Uses FileHashManager from import_utils for consistency.
    """
    try:
        from ..importers.import_utils import FileHashManager
        return FileHashManager.compute_file_hash(str(path))
    except ImportError:
        # Fallback for standalone execution
        import hashlib
        sha256 = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                sha256.update(chunk)
        return sha256.hexdigest()


def get_file_size_mb(path: Path) -> float:
    """Get file size in megabytes."""
    return path.stat().st_size / (1024 * 1024)


# --- Result Helpers ---

def make_result(
    success: bool,
    source_key: str,
    records: int = 0,
    path: Optional[Path] = None,
    error: Optional[str] = None
) -> dict:
    """Create a standardized result dict."""
    return {
        "success": success,
        "source_key": source_key,
        "records": records,
        "path": str(path) if path else None,
        "error": error
    }


def print_result(result: dict) -> None:
    """Print download result in consistent format."""
    source_key = result["source_key"]
    print(f"\n{'=' * 60}")
    print(f"Source: {source_key}")
    print("-" * 60)

    if result["success"]:
        print(f"Status: SUCCESS")
        print(f"Records: {result['records']:,}")
        if result.get("path"):
            path = Path(result["path"])
            if path.exists():
                size_mb = get_file_size_mb(path)
                print(f"Size: {size_mb:.1f} MB")
                print(f"Path: {result['path']}")
    else:
        print(f"Status: FAILED")
        print(f"Error: {result.get('error', 'Unknown error')}")

    print("=" * 60)
