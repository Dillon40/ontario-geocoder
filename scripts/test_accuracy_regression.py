"""
Accuracy regression harness for the layered geocoder (pre-release accuracy gate).

Runs a sample of the 5,000-address Toronto benchmark
(`2025_11_27_toronto_data_scrape_5000_properties.json`) through the live
municipal matcher (`engine.geocoder.match_municipal_data`, the same code
path used to produce `reports/geocoder_results_5000_v2.json`) and compares
the resulting coordinates against Google's forward-geocoding API — the same
ground truth used to produce the recorded baseline in
`reports/geocoder_results_5000_v2_vs_google.json`:

    median distance:  6.86m
    avg distance:    32.76m
    p95 distance:    49.55m
    p99 distance:    89.27m
    failures:           74  (out of 4,799 compared)

It asserts current accuracy does not regress past pinned thresholds, and
that specific previously-identified regression cases resolve within
tolerance:

    - "92 King Edward Ave, Toronto, M4C5J6" (benchmark index 3317) — was
      6.5m under the old `municipal_toronto` matcher, regressed to 6777.5m
      under `fsa_core_city` (see reports/geocoder_results_5000_v2_vs_google.json
      "regressions").
    - The "1 Palace Pier Crt, Toronto, M8V3W9" unit cluster (indices 1827,
      2112, 3154, 3403, 4433) — was 19.6m, regressed to 114.9m, same cause.

HOW TO RUN
----------
This module needs a live DB (Tier 2 municipal matcher) and a live Google
Geocoding API key. It is a manual/CI pre-release gate, not part of the
default test suite:

    DB_PASSWORD=... DB_HOST=... DB_NAME=... DB_USER=... \\
    GOOGLE_API_ENABLED=true GOOGLE_GEOCODING_API_KEY=... \\
    .venv/bin/python -m pytest scripts/test_accuracy_regression.py -v -s

Optional env vars:
    ACCURACY_SAMPLE_SIZE   Number of benchmark addresses to sample for the
                            aggregate threshold checks (default: 200 — the
                            pinned 200-case sample used for routine runs).
                            Set to 4799+ to replay (close to) the full
                            benchmark used to record the pinned baseline.
                            Each address costs one Google Geocoding API call
                            (~$5 / 1000 requests) — the default keeps a
                            routine run cheap; use a bigger sample for the
                            actual pre-release gate.

SKIP BEHAVIOR
-------------
Every test in this module calls `pytest.skip` (never fails) when:
    - DB_PASSWORD is unset, or the DB is unreachable
    - the benchmark input file is missing
    - Google Geocoding API is not configured (GOOGLE_API_ENABLED=true and
      GOOGLE_GEOCODING_API_KEY both required)

This keeps a plain, no-infrastructure `pytest` run green.
"""
import asyncio
import json
import math
import os
import sys
from pathlib import Path
from statistics import median
from urllib.parse import urlencode

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

BENCHMARK_FILE = PROJECT_ROOT / "2025_11_27_toronto_data_scrape_5000_properties.json"

# Pinned thresholds, from reports/geocoder_results_5000_v2_vs_google.json
# ("new_vs_google": compared=4799, our_failures=74, median=6.86, p95=49.55)
MEDIAN_THRESHOLD_M = 7.0
P95_THRESHOLD_M = 50.0
BASELINE_COMPARED = 4799
BASELINE_FAILURES = 74

NAMED_CASE_TOLERANCE_M = 50.0  # "very_good" bucket per validate_google_forward.py

NAMED_REGRESSION_CASES = [
    {"index": 3317, "label": "92 King Edward Ave"},
    {"index": 1827, "label": "1 Palace Pier Crt Unit 2104"},
    {"index": 2112, "label": "1 Palace Pier Crt Unit 2812"},
    {"index": 3154, "label": "1 Palace Pier Crt Unit 4009"},
    {"index": 3403, "label": "1 Palace Pier Crt Unit 1605"},
    {"index": 4433, "label": "1 Palace Pier Crt Unit 309"},
]

DEFAULT_SAMPLE_SIZE = 200


def _google_configured() -> bool:
    return (
        os.getenv("GOOGLE_API_ENABLED", "false").lower() == "true"
        and bool(os.getenv("GOOGLE_GEOCODING_API_KEY"))
    )


def haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters (mirrors engine/scripts/validate_google_forward.py)."""
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * math.asin(math.sqrt(a)) * 6371000


def _format_address_for_google(addr: dict) -> str:
    parts = []
    if addr.get("street_number"):
        parts.append(str(addr["street_number"]))
    if addr.get("street_name"):
        street = str(addr["street_name"])
        if addr.get("street_suffix"):
            street += " " + str(addr["street_suffix"])
        if addr.get("street_direction"):
            street += " " + str(addr["street_direction"])
        parts.append(street)
    if addr.get("unit_no"):
        parts[0] = f"Unit {addr['unit_no']}, {parts[0]}" if parts else f"Unit {addr['unit_no']}"
    if addr.get("city"):
        parts.append(str(addr["city"]))
    if addr.get("province"):
        parts.append(str(addr["province"]))
    if addr.get("postal_code"):
        parts.append(str(addr["postal_code"]))
    return ", ".join(parts)


async def _google_forward_geocode(address: str):
    import httpx

    api_key = os.getenv("GOOGLE_GEOCODING_API_KEY")
    params = {"address": address, "key": api_key, "region": "ca"}
    url = f"https://maps.googleapis.com/maps/api/geocode/json?{urlencode(params)}"

    async with httpx.AsyncClient() as client:
        response = await client.get(url, timeout=10.0)
        data = response.json()

    if data.get("status") == "OK" and data.get("results"):
        location = data["results"][0]["geometry"]["location"]
        return location["lat"], location["lng"]
    return None


def _load_benchmark_addresses() -> list:
    if not BENCHMARK_FILE.exists():
        pytest.skip(f"benchmark input file not found: {BENCHMARK_FILE}")
    with open(BENCHMARK_FILE) as f:
        data = json.load(f)
    addresses = data.get("addresses", [])
    if not addresses:
        pytest.skip(f"benchmark input file has no addresses: {BENCHMARK_FILE}")
    return addresses


@pytest.fixture
def benchmark_addresses():
    return _load_benchmark_addresses()


@pytest.fixture
def require_google():
    if not _google_configured():
        pytest.skip(
            "GOOGLE_GEOCODING_API_KEY / GOOGLE_API_ENABLED=true not configured "
            "- accuracy regression harness needs Google as ground truth "
            "(see module docstring)"
        )


@pytest.fixture
async def db_pool():
    if not os.getenv("DB_PASSWORD"):
        pytest.skip(
            "DB_PASSWORD not set - accuracy regression harness needs a live "
            "DB (see module docstring)"
        )
    from engine import database

    try:
        pool = await database.init_pool()
    except Exception as exc:
        pytest.skip(f"could not connect to DB: {exc}")
    yield pool
    await database.close()


async def _geocode_our(raw_addr: dict):
    """Geocode one benchmark address via the live Tier-2 municipal matcher."""
    from engine.address_parser import parse_address
    from engine.geocoder import match_municipal_data

    components = parse_address({
        "street_number": raw_addr.get("street_number"),
        "street_name": raw_addr.get("street_name"),
        "street_suffix": raw_addr.get("street_suffix"),
        "street_direction": raw_addr.get("street_direction"),
        "unit_no": raw_addr.get("unit_no"),
        "city": raw_addr.get("city"),
        "province": raw_addr.get("province"),
        "postal_code": raw_addr.get("postal_code"),
        "pin": raw_addr.get("pin"),
    })
    return await match_municipal_data(components)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", NAMED_REGRESSION_CASES, ids=[c["label"] for c in NAMED_REGRESSION_CASES]
)
async def test_named_regression_case_resolves_within_tolerance(
    case, db_pool, benchmark_addresses, require_google
):
    """Known-regression cases must resolve within NAMED_CASE_TOLERANCE_M of Google."""
    index = case["index"]
    assert index < len(benchmark_addresses), (
        f"benchmark file only has {len(benchmark_addresses)} addresses, "
        f"cannot check pinned index {index}"
    )
    raw_addr = benchmark_addresses[index]

    our_result = await _geocode_our(raw_addr)
    assert our_result is not None, (
        f"{case['label']} (index {index}) failed to geocode at all "
        f"(previously matched via municipal_toronto/fsa_core_city)"
    )

    google_coords = await _google_forward_geocode(_format_address_for_google(raw_addr))
    assert google_coords is not None, f"Google failed to geocode {case['label']} - cannot verify"

    distance_m = haversine_distance_m(
        our_result["latitude"], our_result["longitude"], *google_coords
    )
    assert distance_m <= NAMED_CASE_TOLERANCE_M, (
        f"{case['label']} (index {index}) is {distance_m:.1f}m from Google's "
        f"geocode (tolerance {NAMED_CASE_TOLERANCE_M}m) - matched via "
        f"{our_result.get('match_method')}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pinned_accuracy_thresholds(db_pool, benchmark_addresses, require_google):
    """Aggregate accuracy over a benchmark sample must not regress past pinned thresholds."""
    sample_size = min(
        int(os.getenv("ACCURACY_SAMPLE_SIZE", DEFAULT_SAMPLE_SIZE)),
        len(benchmark_addresses),
    )
    # Deterministic, evenly-spaced sample so results are reproducible across runs.
    step = max(1, len(benchmark_addresses) // sample_size)
    sample_indices = list(range(0, len(benchmark_addresses), step))[:sample_size]

    distances = []
    our_failures = 0
    google_failures = 0

    for idx in sample_indices:
        raw_addr = benchmark_addresses[idx]
        our_result = await _geocode_our(raw_addr)
        if our_result is None:
            our_failures += 1
            continue

        google_coords = await _google_forward_geocode(_format_address_for_google(raw_addr))
        if google_coords is None:
            google_failures += 1
            continue

        distances.append(
            haversine_distance_m(our_result["latitude"], our_result["longitude"], *google_coords)
        )
        await asyncio.sleep(0.05)  # be gentle on the Google API

    compared = len(distances)
    assert compared > 0, (
        f"no successful comparisons out of {len(sample_indices)} sampled "
        f"({our_failures} our-failures, {google_failures} google-failures)"
    )

    distances_sorted = sorted(distances)
    median_distance = median(distances_sorted)
    p95_distance = distances_sorted[min(len(distances_sorted) - 1, int(len(distances_sorted) * 0.95))]

    max_allowed_failures = max(
        1, math.ceil(min(compared, BASELINE_COMPARED) / BASELINE_COMPARED * BASELINE_FAILURES)
    )

    assert median_distance <= MEDIAN_THRESHOLD_M, (
        f"median distance {median_distance:.2f}m exceeds pinned threshold "
        f"{MEDIAN_THRESHOLD_M}m (baseline: 6.86m)"
    )
    assert p95_distance <= P95_THRESHOLD_M, (
        f"p95 distance {p95_distance:.2f}m exceeds pinned threshold "
        f"{P95_THRESHOLD_M}m (baseline: 49.55m)"
    )
    assert our_failures <= max_allowed_failures, (
        f"{our_failures} our-failures out of {len(sample_indices)} sampled exceeds "
        f"the scaled pinned threshold of {max_allowed_failures} "
        f"(baseline: 74 out of {BASELINE_COMPARED})"
    )
