"""
Tests for download utilities.

Run with: uv run pytest tests/data_sourcing/downloaders/test_download_utils.py -v
"""
import sys
import json
import pytest
import hashlib
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / 'data_sourcing' / 'downloaders'))

from data_sourcing.downloaders.download_utils import (
    get_output_path,
    ensure_output_dir,
    stream_geojson_start,
    stream_geojson_feature,
    stream_geojson_end,
    validate_fields,
    calculate_file_hash,
    get_file_size_mb,
    make_result,
)


class TestPathHelpers:
    """Tests for path helper functions."""

    def test_get_output_path(self):
        """Test output path generation."""
        path = get_output_path("toronto_addresses")
        assert path.name == "toronto_addresses.geojson"
        assert "toronto_addresses" in str(path.parent)

    def test_ensure_output_dir(self, tmp_path, monkeypatch):
        """Test that output directory is created."""
        # Patch DATA_DIR to use temp directory
        from data_sourcing.downloaders import download_utils
        monkeypatch.setattr(download_utils, 'DATA_DIR', tmp_path)

        output_path = ensure_output_dir("test_source")

        assert output_path.parent.exists()
        assert "test_source" in str(output_path)


class TestGeoJSONStreaming:
    """Tests for GeoJSON streaming functions."""

    def test_stream_geojson_complete(self, tmp_path):
        """Test streaming a complete GeoJSON file."""
        output_file = tmp_path / "test.geojson"

        features = [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [0, 0]}, "properties": {"id": 1}},
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [1, 1]}, "properties": {"id": 2}},
        ]

        with open(output_file, "w") as f:
            stream_geojson_start(f)
            for i, feature in enumerate(features):
                stream_geojson_feature(f, feature, is_first=(i == 0))
            stream_geojson_end(f)

        # Verify it's valid JSON
        with open(output_file) as f:
            data = json.load(f)

        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) == 2
        assert data["features"][0]["properties"]["id"] == 1
        assert data["features"][1]["properties"]["id"] == 2

    def test_stream_geojson_empty(self, tmp_path):
        """Test streaming an empty GeoJSON file."""
        output_file = tmp_path / "empty.geojson"

        with open(output_file, "w") as f:
            stream_geojson_start(f)
            stream_geojson_end(f)

        with open(output_file) as f:
            data = json.load(f)

        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) == 0

    def test_stream_geojson_single_feature(self, tmp_path):
        """Test streaming a single feature."""
        output_file = tmp_path / "single.geojson"

        feature = {"type": "Feature", "geometry": None, "properties": {"name": "test"}}

        with open(output_file, "w") as f:
            stream_geojson_start(f)
            stream_geojson_feature(f, feature, is_first=True)
            stream_geojson_end(f)

        with open(output_file) as f:
            data = json.load(f)

        assert len(data["features"]) == 1
        assert data["features"][0]["properties"]["name"] == "test"


class TestFieldValidation:
    """Tests for field validation functions."""

    def test_validate_fields_all_present(self, caplog):
        """Test when all expected fields are present."""
        import logging
        caplog.set_level(logging.DEBUG)

        actual = {"street_number", "street_name", "city"}
        expected = {"street_number", "street_name", "city"}

        validate_fields("test_source", actual, expected)

        # Should only have debug log, no warnings
        assert "Missing expected fields" not in caplog.text

    def test_validate_fields_missing(self, caplog):
        """Test when expected fields are missing."""
        import logging
        caplog.set_level(logging.WARNING)

        actual = {"street_number", "city"}
        expected = {"street_number", "street_name", "city"}

        validate_fields("test_source", actual, expected)

        assert "Missing expected fields" in caplog.text
        assert "street_name" in caplog.text

    def test_validate_fields_new(self, caplog):
        """Test when unexpected fields are present."""
        import logging
        caplog.set_level(logging.INFO)

        actual = {"street_number", "street_name", "new_field"}
        expected = {"street_number", "street_name"}

        validate_fields("test_source", actual, expected)

        assert "New/unexpected fields" in caplog.text
        assert "new_field" in caplog.text


class TestFileHash:
    """Tests for file hash functions."""

    def test_calculate_file_hash(self, tmp_path):
        """Test file hash calculation."""
        test_file = tmp_path / "test.txt"
        content = "Hello, World!"
        test_file.write_text(content)

        file_hash = calculate_file_hash(test_file)

        expected = hashlib.sha256(content.encode()).hexdigest()
        assert file_hash == expected

    def test_calculate_file_hash_binary(self, tmp_path):
        """Test hash for binary file."""
        test_file = tmp_path / "test.bin"
        content = b"\x00\x01\x02\x03\x04"
        test_file.write_bytes(content)

        file_hash = calculate_file_hash(test_file)

        expected = hashlib.sha256(content).hexdigest()
        assert file_hash == expected

    def test_calculate_file_hash_large(self, tmp_path):
        """Test hash for file larger than chunk size."""
        test_file = tmp_path / "large.bin"
        content = b"x" * 2_000_000  # 2MB
        test_file.write_bytes(content)

        file_hash = calculate_file_hash(test_file)

        expected = hashlib.sha256(content).hexdigest()
        assert file_hash == expected


class TestFileSizeHelper:
    """Tests for file size helper."""

    def test_get_file_size_mb(self, tmp_path):
        """Test file size calculation in MB."""
        test_file = tmp_path / "test.bin"
        # Create a 1MB file
        content = b"x" * (1024 * 1024)
        test_file.write_bytes(content)

        size = get_file_size_mb(test_file)

        assert size == pytest.approx(1.0, abs=0.01)


class TestResultHelpers:
    """Tests for result helper functions."""

    def test_make_result_success(self):
        """Test successful result creation."""
        result = make_result(
            success=True,
            source_key="test_source",
            records=1000,
            path=Path("/tmp/test.geojson")
        )

        assert result["success"] is True
        assert result["source_key"] == "test_source"
        assert result["records"] == 1000
        assert result["path"] == "/tmp/test.geojson"
        assert result["error"] is None

    def test_make_result_failure(self):
        """Test failure result creation."""
        result = make_result(
            success=False,
            source_key="test_source",
            error="Connection timeout"
        )

        assert result["success"] is False
        assert result["source_key"] == "test_source"
        assert result["records"] == 0
        assert result["error"] == "Connection timeout"
