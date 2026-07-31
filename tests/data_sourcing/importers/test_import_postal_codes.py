"""
Tests for commercial postal code import script.

Run with: uv run pytest tests/data_sourcing/importers/test_import_postal_codes.py -v
"""
import sys
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / 'data_sourcing' / 'importers'))


class TestDatabaseUrl:
    """Tests for database URL construction."""

    def test_get_database_url_defaults(self):
        """Test default database URL construction."""
        with patch.dict('os.environ', {}, clear=True):
            from data_sourcing.importers.import_postal_codes import get_database_url
            url = get_database_url()
            assert 'postgresql://' in url
            assert 'geocoder' in url
            assert 'localhost' in url

    def test_get_database_url_custom(self):
        """Test database URL with custom environment variables."""
        env = {
            'DB_HOST': 'dbserver',
            'DB_PORT': '5433',
            'DB_NAME': 'mydb',
            'DB_USER': 'myuser',
            'DB_PASSWORD': 'mypass'
        }
        with patch.dict('os.environ', env):
            from data_sourcing.importers.import_postal_codes import get_database_url
            url = get_database_url()
            assert 'dbserver:5433' in url
            assert 'mydb' in url
            assert 'myuser:mypass' in url


class TestTableNames:
    """Tests for table name constants."""

    def test_table_names(self):
        """Test table name constants are set correctly."""
        from data_sourcing.importers.import_postal_codes import TABLE_NAME, STAGING_TABLE

        assert TABLE_NAME == 'commercial_postal_codes'
        assert STAGING_TABLE == 'commercial_postal_codes_staging'


class TestBatchInsert:
    """Tests for batch_insert function."""

    def test_batch_insert_empty_records(self):
        """Test that empty records list returns 0."""
        from data_sourcing.importers.import_postal_codes import batch_insert

        mock_engine = MagicMock()
        result = batch_insert(mock_engine, [])
        assert result == 0

    def test_batch_insert_returns_count(self):
        """Test that batch_insert returns correct count."""
        from data_sourcing.importers.import_postal_codes import batch_insert

        # Create mock engine with context manager
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        records = [
            {
                'postal_code_raw': 'L7J 2L7',
                'postal_code': 'L7J2L7',
                'fsa_code': 'L7J',
                'city': 'Acton',
                'uri': 'acton',
                'latitude': 43.58,
                'longitude': -80.01,
                'source_community_id': 517,
                'source_county_id': 15,
                'source_region_id': 3
            },
            {
                'postal_code_raw': 'M5V 1A1',
                'postal_code': 'M5V1A1',
                'fsa_code': 'M5V',
                'city': 'Toronto',
                'uri': 'toronto',
                'latitude': 43.64,
                'longitude': -79.39,
                'source_community_id': 100,
                'source_county_id': 10,
                'source_region_id': 1
            }
        ]

        result = batch_insert(mock_engine, records)
        assert result == 2


class TestRecordStructure:
    """Tests for expected record structure."""

    def test_record_has_required_fields(self):
        """Test that parsed records have all required fields for import."""
        required_fields = [
            'postal_code_raw',
            'postal_code',
            'fsa_code',
            'city',
            'uri',
            'latitude',
            'longitude',
            'source_community_id',
            'source_county_id',
            'source_region_id'
        ]

        # Sample record from parser
        from data_sourcing.importers.parse_mysql_postal_dump import parse_mysql_dump
        import tempfile

        dump_content = "(1, 'L7J 2L7', 'L7J', 517, 15, 3, 'Acton', 'acton', 43.58, -80.01);"

        with tempfile.NamedTemporaryFile(mode='w', suffix='.sql', delete=False) as f:
            f.write(dump_content)
            f.flush()

            for batch in parse_mysql_dump(f.name):
                for record in batch:
                    for field in required_fields:
                        assert field in record, f"Missing field: {field}"


class TestSQLStatements:
    """Tests for SQL statement generation."""

    def test_staging_table_sql_contains_required_columns(self):
        """Test that staging table creation includes all columns."""
        from data_sourcing.importers.import_postal_codes import create_staging_table

        # Create mock engine that captures SQL
        executed_sql = []

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute = MagicMock(side_effect=lambda sql: executed_sql.append(str(sql.text) if hasattr(sql, 'text') else str(sql)))

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        create_staging_table(mock_engine)

        # Check that CREATE TABLE was executed
        all_sql = ' '.join(executed_sql)
        assert 'CREATE TABLE' in all_sql
        assert 'postal_code_raw' in all_sql
        assert 'postal_code' in all_sql
        assert 'fsa_code' in all_sql
        assert 'city_normalized' in all_sql


class TestIndexCreation:
    """Tests for index creation."""

    def test_create_indexes_creates_all_indexes(self):
        """Test that all required indexes are created."""
        from data_sourcing.importers.import_postal_codes import create_indexes

        executed_sql = []

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute = MagicMock(side_effect=lambda sql: executed_sql.append(str(sql.text) if hasattr(sql, 'text') else str(sql)))

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        create_indexes(mock_engine)

        # Get all SQL executed
        all_sql = ' '.join(executed_sql)

        # Check for expected indexes
        assert 'idx_' in all_sql
        assert 'postal_code' in all_sql
        assert 'fsa' in all_sql
        assert 'city_norm' in all_sql
        assert 'GIST' in all_sql  # Spatial index


class TestImportFlow:
    """Tests for the overall import flow."""

    def test_default_dump_path_constant(self):
        """Test that the default dump path constant is set correctly."""
        from data_sourcing.importers.import_postal_codes import DEFAULT_DUMP_PATH

        assert 'postal_db' in DEFAULT_DUMP_PATH
        assert DEFAULT_DUMP_PATH.endswith('.sql')

    def test_import_requires_existing_file(self):
        """Test that import raises error for non-existent file."""
        from data_sourcing.importers.import_postal_codes import import_postal_codes
        import tempfile
        import os

        # Create and delete a temp file to get a non-existent path
        with tempfile.NamedTemporaryFile(delete=False) as f:
            temp_path = f.name
        os.unlink(temp_path)

        with pytest.raises(FileNotFoundError):
            import_postal_codes(temp_path)
