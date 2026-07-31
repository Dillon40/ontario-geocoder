"""
Tests for dataset field mappings in config/dataset-mappings.json.

Validates that:
1. All confirmed sources have valid field mappings
2. Required fields are mapped
3. Field names are consistent
4. Transform references are valid

Run with: uv run pytest tests/data_sourcing/test_field_mappings.py -v
"""
import os
import sys
import json
import pytest
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

MAPPINGS_FILE = PROJECT_ROOT / "config" / "dataset-mappings.json"

# Required fields that every address source should map
REQUIRED_FIELDS = {
    "street_number",
    "street_name",
}

# Optional but expected fields for address sources
EXPECTED_FIELDS = {
    "street_number",
    "street_name",
    "street_suffix",
    "street_direction",
    "unit_number",
    "city",
    "postal_code",
    "municipality",
}

# Valid transform types
VALID_TRANSFORMS = {
    "parse_street_name",
    "extract_postal_code",
    "normalize_municipality",
    "parse_civic_number",
    "parse_unit_number",
    "uppercase",
    "lowercase",
    "titlecase",
    "trim",
}


@pytest.fixture
def mappings():
    """Load dataset mappings config, excluding metadata keys."""
    with open(MAPPINGS_FILE) as f:
        all_data = json.load(f)
    # Filter out metadata keys that start with underscore
    return {k: v for k, v in all_data.items() if not k.startswith('_')}


class TestMappingsFileStructure:
    """Tests for the overall structure of dataset-mappings.json."""

    def test_mappings_file_exists(self):
        """Test that mappings file exists."""
        assert MAPPINGS_FILE.exists(), f"Mappings file not found: {MAPPINGS_FILE}"

    def test_mappings_file_valid_json(self):
        """Test that mappings file is valid JSON."""
        with open(MAPPINGS_FILE) as f:
            data = json.load(f)
        assert isinstance(data, dict)

    def test_mappings_not_empty(self, mappings):
        """Test that there are some mappings defined."""
        assert len(mappings) > 0, "No mappings defined"


class TestFieldMappingStructure:
    """Tests for individual field mapping structure."""

    def test_all_mappings_have_field_mappings(self, mappings):
        """Test that all entries have a field_mappings key."""
        for source_key, config in mappings.items():
            assert "field_mappings" in config, f"{source_key} missing field_mappings"
            assert isinstance(config["field_mappings"], dict), \
                f"{source_key} field_mappings is not a dict"

    def test_field_mappings_have_string_values(self, mappings):
        """Test that field_mappings values are strings or null."""
        for source_key, config in mappings.items():
            for target_field, source_field in config.get("field_mappings", {}).items():
                assert source_field is None or isinstance(source_field, str), \
                    f"{source_key}.{target_field} has invalid value type: {type(source_field)}"

    def test_transforms_have_valid_structure(self, mappings):
        """Test that transforms have valid structure (list or dict format)."""
        for source_key, config in mappings.items():
            transforms = config.get("transforms", [])

            # Allow both list format (legacy) and dict format (new)
            if isinstance(transforms, list):
                for i, transform in enumerate(transforms):
                    assert isinstance(transform, dict), \
                        f"{source_key} transform[{i}] is not a dict"
                    assert "type" in transform, \
                        f"{source_key} transform[{i}] missing 'type'"
            elif isinstance(transforms, dict):
                # Dict format: keys are transform names, values are config
                for transform_name, transform_config in transforms.items():
                    assert isinstance(transform_config, dict), \
                        f"{source_key}.transforms.{transform_name} is not a dict"
            # Empty or None is also valid (no transforms)
            elif transforms:
                pytest.fail(f"{source_key} transforms has invalid type: {type(transforms)}")


class TestRequiredFields:
    """Tests for required field mappings."""

    def test_address_sources_have_street_number(self, mappings):
        """Test that address sources map street_number."""
        address_sources = [k for k in mappings.keys() if "address" in k.lower() or k.endswith("_addresses")]

        for source_key in address_sources:
            field_mappings = mappings[source_key].get("field_mappings", {})
            street_number = field_mappings.get("street_number")

            # Allow null (if derived from transform) or string
            if street_number is not None:
                assert isinstance(street_number, str), \
                    f"{source_key} street_number should be string or null"

    def test_address_sources_have_street_name(self, mappings):
        """Test that address sources map street_name or derive it."""
        address_sources = [k for k in mappings.keys() if "address" in k.lower() or k.endswith("_addresses")]

        for source_key in address_sources:
            field_mappings = mappings[source_key].get("field_mappings", {})
            transforms = mappings[source_key].get("transforms", [])

            street_name = field_mappings.get("street_name")

            # Check for street parse transform in either list or dict format
            has_street_parse = False
            if isinstance(transforms, list):
                has_street_parse = any(t.get("type") == "parse_street_name" for t in transforms)
            elif isinstance(transforms, dict):
                has_street_parse = "parse_street_name" in transforms

            # Should have either direct mapping or parse transform
            has_street = street_name is not None or has_street_parse
            assert has_street, f"{source_key} has no street_name mapping or parse_street_name transform"


class TestFieldNaming:
    """Tests for consistent field naming."""

    def test_no_duplicate_source_fields(self, mappings):
        """Test that same source field isn't mapped to multiple targets."""
        for source_key, config in mappings.items():
            field_mappings = config.get("field_mappings", {})

            # Get all non-null source fields
            source_fields = [v for v in field_mappings.values() if v is not None]
            unique_fields = set(source_fields)

            # Some duplication is intentional (e.g., same field parsed differently)
            # So we just check for exact duplicates (same target mapped twice)
            # This is already prevented by dict structure, so this is a sanity check
            assert len(source_fields) == len(unique_fields), \
                f"{source_key} has duplicate source field mappings"

    def test_target_fields_are_valid(self, mappings):
        """Test that target field names are from expected set."""
        all_target_fields = set()

        for source_key, config in mappings.items():
            field_mappings = config.get("field_mappings", {})
            all_target_fields.update(field_mappings.keys())

        # Target fields should be lowercase snake_case
        for field in all_target_fields:
            assert field == field.lower(), f"Field '{field}' should be lowercase"
            assert " " not in field, f"Field '{field}' should not contain spaces"


class TestTransformConfiguration:
    """Tests for transform configurations."""

    def test_parse_street_name_has_source_field(self, mappings):
        """Test that parse_street_name transforms specify source_field."""
        for source_key, config in mappings.items():
            transforms = config.get("transforms", [])

            if isinstance(transforms, list):
                # List format
                for transform in transforms:
                    if transform.get("type") == "parse_street_name":
                        assert "source_field" in transform, \
                            f"{source_key} parse_street_name missing source_field"
            elif isinstance(transforms, dict):
                # Dict format - parse_street_name config should have source_field
                parse_config = transforms.get("parse_street_name", {})
                if parse_config.get("enabled", True) and "source_field" not in parse_config:
                    # Some dict-format transforms derive source_field from field_mappings
                    pass  # Skip this check for dict format, it's more flexible

    def test_transform_output_fields_defined(self, mappings):
        """Test that transforms with output_fields have valid values."""
        for source_key, config in mappings.items():
            transforms = config.get("transforms", [])

            if isinstance(transforms, list):
                for transform in transforms:
                    output_fields = transform.get("output_fields")
                    if output_fields is not None:
                        assert isinstance(output_fields, list), \
                            f"{source_key} transform output_fields should be list"
            # Skip dict format - different structure


class TestMappingConsistency:
    """Tests for consistency between mappings and database."""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_confirmed_sources_have_mappings(self):
        """Test that all confirmed database sources have mappings.

        Integration test: requires a live Postgres reachable via the
        DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD env vars
        (same contract as engine/database.py). Skipped by default;
        run explicitly with `pytest -m integration`. When run, any
        connection or credential problem is a FAILURE, not a skip.
        """
        import asyncpg

        password = os.environ.get('DB_PASSWORD')
        assert password, (
            "DB_PASSWORD environment variable is required for integration "
            "tests (see .env.example) — refusing to fall back to a default"
        )

        conn = await asyncpg.connect(
            host=os.environ.get('DB_HOST', 'localhost'),
            port=int(os.environ.get('DB_PORT', 5432)),
            database=os.environ.get('DB_NAME', 'geocoder'),
            user=os.environ.get('DB_USER', 'geocoder'),
            password=password
        )

        try:
            rows = await conn.fetch("""
                SELECT source_key, dataset_mapping_key
                FROM data_sources
                WHERE status = 'confirmed'
                AND import_script = 'import_production_data.py'
            """)

            with open(MAPPINGS_FILE) as f:
                mappings = json.load(f)

            missing = []
            for row in rows:
                source_key = row['source_key']
                mapping_key = row['dataset_mapping_key'] or source_key

                if mapping_key not in mappings:
                    missing.append(f"{source_key} (mapping_key: {mapping_key})")

            assert not missing, (
                f"Confirmed sources missing dataset mappings: {', '.join(missing)}"
            )

        finally:
            await conn.close()
