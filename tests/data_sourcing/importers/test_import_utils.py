"""
Tests for import utilities: FileHashManager, StaleRecordDetector, PropertyIdGenerator.

Run with: uv run pytest tests/data_sourcing/importers/test_import_utils.py -v
"""
import os
import sys
import pytest
import tempfile
import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from contextlib import asynccontextmanager

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / 'data_sourcing' / 'importers'))

from data_sourcing.importers.import_utils import (
    FileHashManager,
    StaleRecordDetector,
    PropertyIdGenerator,
    StaleDeletionAbortedError
)


def create_mock_pool(mock_conn):
    """Create a mock asyncpg pool with proper async context manager support."""
    mock_pool = MagicMock()

    @asynccontextmanager
    async def mock_acquire():
        yield mock_conn

    mock_pool.acquire = mock_acquire
    return mock_pool


class TestFileHashManager:
    """Tests for FileHashManager class."""

    def test_compute_file_hash(self, tmp_path):
        """Test file hash computation."""
        # Create a test file
        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello, World!")

        # Compute hash
        file_hash = FileHashManager.compute_file_hash(str(test_file))

        # Verify it's a valid SHA256 hex string
        assert len(file_hash) == 64
        assert all(c in '0123456789abcdef' for c in file_hash)

        # Compute expected hash
        expected = hashlib.sha256(b"Hello, World!").hexdigest()
        assert file_hash == expected

    def test_compute_file_hash_large_file(self, tmp_path):
        """Test hash computation for files larger than chunk size."""
        # Create a file larger than default chunk size (8KB)
        test_file = tmp_path / "large.bin"
        content = b"x" * 20000  # 20KB
        test_file.write_bytes(content)

        file_hash = FileHashManager.compute_file_hash(str(test_file))

        expected = hashlib.sha256(content).hexdigest()
        assert file_hash == expected

    def test_compute_file_hash_nonexistent(self, tmp_path):
        """Test that nonexistent files raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            FileHashManager.compute_file_hash(str(tmp_path / "nonexistent.txt"))

    @pytest.mark.asyncio
    async def test_should_skip_file_no_previous_import(self):
        """Test that file is not skipped if no previous import exists."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = None  # No previous import
        mock_pool = create_mock_pool(mock_conn)

        with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
            f.write("test content")
            temp_path = f.name

        try:
            should_skip, reason = await FileHashManager.should_skip_file(
                mock_pool, "test_source", temp_path
            )
            assert should_skip is False
            assert reason is None
        finally:
            os.unlink(temp_path)

    @pytest.mark.asyncio
    async def test_should_skip_file_hash_matches(self):
        """Test that file is skipped if hash matches previous import."""
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
            f.write("test content")
            temp_path = f.name

        try:
            file_hash = FileHashManager.compute_file_hash(temp_path)

            mock_conn = AsyncMock()
            mock_conn.fetchrow.return_value = {
                'id': 1,
                'file_hash': file_hash,
                'completed_at': MagicMock(strftime=lambda x: '2024-01-01 12:00:00')
            }
            mock_pool = create_mock_pool(mock_conn)

            should_skip, reason = await FileHashManager.should_skip_file(
                mock_pool, "test_source", temp_path
            )
            assert should_skip is True
            assert "unchanged" in reason.lower()
        finally:
            os.unlink(temp_path)

    @pytest.mark.asyncio
    async def test_should_skip_file_hash_differs(self):
        """Test that file is not skipped if hash differs."""
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
            f.write("test content")
            temp_path = f.name

        try:
            mock_conn = AsyncMock()
            mock_conn.fetchrow.return_value = {
                'id': 1,
                'file_hash': 'different_hash_value',
                'completed_at': MagicMock()
            }
            mock_pool = create_mock_pool(mock_conn)

            should_skip, reason = await FileHashManager.should_skip_file(
                mock_pool, "test_source", temp_path
            )
            assert should_skip is False
            assert reason is None
        finally:
            os.unlink(temp_path)


class TestStaleRecordDetector:
    """Tests for StaleRecordDetector class."""

    def test_mark_seen(self):
        """Test marking records as seen."""
        detector = StaleRecordDetector(
            pool=MagicMock(),
            import_id=1,
            source_key="test_source",
            table_name="properties",
            id_column="property_id"
        )

        detector.mark_seen("ID1")
        detector.mark_seen("ID2")
        detector.mark_seen("ID3")

        assert detector.seen_count == 3
        assert "ID1" in detector._seen_ids
        assert "ID2" in detector._seen_ids
        assert "ID3" in detector._seen_ids

    def test_mark_seen_ignores_none(self):
        """Test that None values are not tracked."""
        detector = StaleRecordDetector(
            pool=MagicMock(),
            import_id=1,
            source_key="test_source",
            table_name="properties",
            id_column="property_id"
        )

        detector.mark_seen(None)
        detector.mark_seen("")
        detector.mark_seen("ID1")

        # Only "ID1" should be tracked (empty string is still added)
        assert "ID1" in detector._seen_ids

    @pytest.mark.asyncio
    async def test_detect_stale_records(self):
        """Test detecting records in DB but not in current import."""
        mock_conn = AsyncMock()

        # Simulate 5 records in DB
        mock_conn.fetch.return_value = [
            {'property_id': 'ID1'},
            {'property_id': 'ID2'},
            {'property_id': 'ID3'},
            {'property_id': 'ID4'},
            {'property_id': 'ID5'},
        ]
        mock_pool = create_mock_pool(mock_conn)

        detector = StaleRecordDetector(
            pool=mock_pool,
            import_id=1,
            source_key="test_source",
            table_name="properties",
            id_column="property_id"
        )

        # Mark only ID1, ID2, ID3 as seen in current import
        detector.mark_seen("ID1")
        detector.mark_seen("ID2")
        detector.mark_seen("ID3")

        stale_ids = await detector.detect_stale_records()

        # ID4 and ID5 should be stale
        assert len(stale_ids) == 2
        assert "ID4" in stale_ids
        assert "ID5" in stale_ids

    @pytest.mark.asyncio
    async def test_detect_stale_records_none_stale(self):
        """Test when all records are seen (no stale records)."""
        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = [
            {'property_id': 'ID1'},
            {'property_id': 'ID2'},
        ]
        mock_pool = create_mock_pool(mock_conn)

        detector = StaleRecordDetector(
            pool=mock_pool,
            import_id=1,
            source_key="test_source",
            table_name="properties",
            id_column="property_id"
        )

        detector.mark_seen("ID1")
        detector.mark_seen("ID2")

        stale_ids = await detector.detect_stale_records()

        assert len(stale_ids) == 0

    @pytest.mark.asyncio
    async def test_soft_delete_stale_records(self):
        """Test soft deleting stale records.

        The detector's anti-truncation guard (see StaleDeletionAbortedError)
        requires the seen count to be at least ~90% of the DB's active
        record count before it will proceed with a soft delete. With
        _active_count unset, active_count is estimated as
        seen_count + len(stale_ids), so we mark 30 IDs as seen alongside
        3 stale IDs (30 / 33 = ~91%, above the default 90% threshold) to
        keep this test inside "normal import" territory.
        """
        mock_conn = AsyncMock()
        mock_conn.execute.return_value = "UPDATE 3"

        @asynccontextmanager
        async def mock_transaction():
            yield

        mock_conn.transaction = mock_transaction
        mock_pool = create_mock_pool(mock_conn)

        detector = StaleRecordDetector(
            pool=mock_pool,
            import_id=1,
            source_key="test_source",
            table_name="properties",
            id_column="property_id"
        )

        for i in range(30):
            detector.mark_seen(f"SEEN{i}")

        deleted = await detector.soft_delete_stale_records(["ID1", "ID2", "ID3"])

        assert deleted == 3
        mock_conn.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_soft_delete_stale_records_aborts_below_threshold(self):
        """Test the anti-truncation guard aborts when the import file is
        far smaller than the DB's active record count (a likely-truncated
        or partial import file), rather than silently soft-deleting most
        of the table.
        """
        mock_conn = AsyncMock()

        @asynccontextmanager
        async def mock_transaction():
            yield

        mock_conn.transaction = mock_transaction
        mock_pool = create_mock_pool(mock_conn)

        detector = StaleRecordDetector(
            pool=mock_pool,
            import_id=1,
            source_key="test_source",
            table_name="properties",
            id_column="property_id"
        )

        # Only 1 record seen in the "import file" vs. 10 stale IDs implied
        # to exist in the DB -> 1 / 11 = ~9%, well below the 90% threshold.
        detector.mark_seen("SEEN0")
        stale_ids = [f"ID{i}" for i in range(10)]

        with pytest.raises(StaleDeletionAbortedError):
            await detector.soft_delete_stale_records(stale_ids)

        mock_conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_soft_delete_empty_list(self):
        """Test that soft delete with empty list returns 0."""
        detector = StaleRecordDetector(
            pool=MagicMock(),
            import_id=1,
            source_key="test_source",
            table_name="properties",
            id_column="property_id"
        )

        deleted = await detector.soft_delete_stale_records([])
        assert deleted == 0


class TestPropertyIdGenerator:
    """Tests for PropertyIdGenerator class."""

    def test_get_prefix_known_source(self):
        """Test prefix generation for known sources."""
        assert PropertyIdGenerator.get_prefix("toronto") == "TOR"
        assert PropertyIdGenerator.get_prefix("toronto_addresses") == "TOR"
        assert PropertyIdGenerator.get_prefix("ottawa") == "OTT"
        assert PropertyIdGenerator.get_prefix("guelph") == "GUE"
        assert PropertyIdGenerator.get_prefix("bruce_county") == "BRC"

    def test_get_prefix_unknown_source(self):
        """Test prefix generation for unknown sources."""
        assert PropertyIdGenerator.get_prefix("newcity") == "NEW"
        assert PropertyIdGenerator.get_prefix("ab") == "AB"

    def test_generate_uses_existing_id(self):
        """Test that existing property_id is used if present."""
        record = {'property_id': 'EXISTING-123'}
        result = PropertyIdGenerator.generate("toronto", record, 1)
        assert result == "TOR-ID-EXISTING-123"

    def test_generate_from_pin(self):
        """Test ID generation from PIN."""
        record = {'pin': '12345678'}
        result = PropertyIdGenerator.generate("toronto", record, 1)
        assert result == "TOR-PIN-12345678"

    def test_generate_from_assessment_roll(self):
        """Test ID generation from assessment roll number."""
        record = {'assessment_roll_number': 'ARN123'}
        result = PropertyIdGenerator.generate("toronto", record, 1)
        assert result == "TOR-ARN-ARN123"

    def test_generate_from_coordinates(self):
        """Test ID generation from coordinates."""
        record = {'latitude': 43.65107, 'longitude': -79.347015}
        result = PropertyIdGenerator.generate("toronto", record, 1)

        # Should have format TOR-GEO-{12char_hash}
        assert result.startswith("TOR-GEO-")
        assert len(result) == len("TOR-GEO-") + 12

    def test_generate_fallback_row_number(self):
        """Test fallback to row number when no other ID available."""
        record = {}
        result = PropertyIdGenerator.generate("toronto", record, 42)
        assert result == "TOR-ROW-00000042"

    def test_generate_priority_order(self):
        """Test that PIN takes priority over other options."""
        record = {
            'property_id': '',  # Empty, should not use
            'pin': '12345',
            'assessment_roll_number': 'ARN999',
            'latitude': 43.0,
            'longitude': -79.0
        }
        result = PropertyIdGenerator.generate("toronto", record, 1)
        assert result == "TOR-PIN-12345"

    def test_validate_valid_ids(self):
        """Test validation of valid property IDs."""
        assert PropertyIdGenerator.validate("TOR-PIN-12345") is True
        assert PropertyIdGenerator.validate("OTT-ARN-9876") is True
        assert PropertyIdGenerator.validate("GUE-GEO-abc123def456") is True
        assert PropertyIdGenerator.validate("BRC-ROW-00000001") is True

    def test_validate_invalid_ids(self):
        """Test validation of invalid property IDs."""
        assert PropertyIdGenerator.validate(None) is False
        assert PropertyIdGenerator.validate("") is False
        assert PropertyIdGenerator.validate("invalid") is False
        assert PropertyIdGenerator.validate(123) is False


class TestPropertyIdGeneratorEdgeCases:
    """Edge case tests for PropertyIdGenerator."""

    def test_generate_with_whitespace_existing_id(self):
        """Test that whitespace in existing ID is trimmed and prefixed."""
        record = {'property_id': '  ID123  '}
        result = PropertyIdGenerator.generate("toronto", record, 1)
        # Short IDs get prefixed with source-ID-
        assert result == "TOR-ID-ID123"

    def test_generate_with_numeric_coordinates(self):
        """Test coordinates as numeric strings."""
        record = {'latitude': '43.65', 'longitude': '-79.35'}
        result = PropertyIdGenerator.generate("toronto", record, 1)
        assert result.startswith("TOR-GEO-")

    def test_generate_with_invalid_coordinates(self):
        """Test fallback when coordinates are invalid."""
        record = {'latitude': 'invalid', 'longitude': 'invalid'}
        result = PropertyIdGenerator.generate("toronto", record, 1)
        assert result == "TOR-ROW-00000001"

    def test_generate_preserves_formatted_existing_id(self):
        """Test that existing ID with proper format is preserved."""
        record = {'property_id': 'TOR-PIN-12345'}
        result = PropertyIdGenerator.generate("toronto", record, 1)
        assert result == "TOR-PIN-12345"
