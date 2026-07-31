"""
Tests for MySQL dump parser for commercial postal codes.

Run with: uv run pytest tests/data_sourcing/importers/test_parse_mysql_dump.py -v
"""
import sys
import pytest
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / 'data_sourcing' / 'importers'))

from data_sourcing.importers.parse_mysql_postal_dump import (
    parse_value,
    parse_record,
    parse_mysql_dump,
    VALUE_PATTERN
)


class TestParseValue:
    """Tests for parse_value function."""

    def test_parse_null_string(self):
        """Test NULL string returns None."""
        assert parse_value('NULL') is None

    def test_parse_empty_string(self):
        """Test empty string returns None."""
        assert parse_value('') is None

    def test_parse_string(self):
        """Test normal string parsing."""
        assert parse_value('Toronto') == 'Toronto'
        assert parse_value('St. Thomas') == 'St. Thomas'

    def test_parse_int(self):
        """Test integer parsing."""
        assert parse_value('123', 'int') == 123
        assert parse_value('0', 'int') == 0

    def test_parse_float(self):
        """Test float parsing."""
        assert parse_value('43.58033000', 'float') == 43.58033
        assert parse_value('-80.01144600', 'float') == -80.011446


class TestValuePattern:
    """Tests for the regex VALUE_PATTERN."""

    def test_pattern_matches_valid_record(self):
        """Test pattern matches valid INSERT value tuple."""
        line = "(1, 'L7J 2L7', 'L7J', 517, 15, 3, 'Acton', 'acton', 43.58033000, -80.01144600)"
        match = VALUE_PATTERN.search(line)
        assert match is not None
        assert match.group(1) == '1'
        assert match.group(2) == 'L7J 2L7'
        assert match.group(3) == 'L7J'
        assert match.group(7) == 'Acton'

    def test_pattern_matches_null_values(self):
        """Test pattern matches records with NULL values."""
        line = "(100, 'M5V 1A1', 'M5V', NULL, NULL, NULL, 'Toronto', 'toronto', NULL, NULL)"
        match = VALUE_PATTERN.search(line)
        assert match is not None
        assert match.group(4) == 'NULL'
        assert match.group(9) == 'NULL'

    def test_pattern_matches_special_city_names(self):
        """Test pattern matches cities with special characters."""
        # St. Thomas (with period)
        line = "(50, 'N5P 1A1', 'N5P', 100, 20, 5, 'St. Thomas', 'st-thomas', 42.77, -81.20)"
        match = VALUE_PATTERN.search(line)
        assert match is not None
        assert match.group(7) == 'St. Thomas'

    def test_pattern_matches_multiple_tuples(self):
        """Test pattern finds all tuples in a line."""
        line = "(1, 'L7J 2L7', 'L7J', 517, 15, 3, 'Acton', 'acton', 43.58, -80.01),(2, 'P3L 1E9', 'P3L', 500, 11, 13, 'Garson', 'garson', 46.55, -80.86)"
        matches = list(VALUE_PATTERN.finditer(line))
        assert len(matches) == 2
        assert matches[0].group(7) == 'Acton'
        assert matches[1].group(7) == 'Garson'


class TestParseRecord:
    """Tests for parse_record function."""

    def test_parse_complete_record(self):
        """Test parsing a complete record."""
        line = "(1, 'L7J 2L7', 'L7J', 517, 15, 3, 'Acton', 'acton', 43.58033000, -80.01144600)"
        match = VALUE_PATTERN.search(line)
        record = parse_record(match)

        assert record['source_id'] == 1
        assert record['postal_code_raw'] == 'L7J 2L7'
        assert record['postal_code'] == 'L7J2L7'  # Normalized
        assert record['fsa_code'] == 'L7J'
        assert record['source_community_id'] == 517
        assert record['source_county_id'] == 15
        assert record['source_region_id'] == 3
        assert record['city'] == 'Acton'
        assert record['uri'] == 'acton'
        assert record['latitude'] == 43.58033
        assert record['longitude'] == -80.011446

    def test_parse_record_with_nulls(self):
        """Test parsing record with NULL values."""
        line = "(100, 'M5V 1A1', 'M5V', NULL, NULL, NULL, 'Toronto', 'toronto', NULL, NULL)"
        match = VALUE_PATTERN.search(line)
        record = parse_record(match)

        assert record['source_id'] == 100
        assert record['postal_code_raw'] == 'M5V 1A1'
        assert record['postal_code'] == 'M5V1A1'
        assert record['source_community_id'] is None
        assert record['source_county_id'] is None
        assert record['source_region_id'] is None
        assert record['latitude'] is None
        assert record['longitude'] is None

    def test_postal_code_normalization(self):
        """Test that postal codes are normalized (space removed)."""
        test_cases = [
            ('L7J 2L7', 'L7J2L7'),
            ('M5V 1A1', 'M5V1A1'),
            ('K1A 0A6', 'K1A0A6'),
        ]

        for raw, expected in test_cases:
            line = f"(1, '{raw}', '{raw[:3]}', 100, 10, 1, 'City', 'city', 43.0, -80.0)"
            match = VALUE_PATTERN.search(line)
            record = parse_record(match)
            assert record['postal_code'] == expected


class TestParseMysqlDump:
    """Tests for parse_mysql_dump generator function."""

    def test_parse_simple_dump(self, tmp_path):
        """Test parsing a simple MySQL dump file."""
        dump_content = """-- MySQL dump
SET SQL_MODE = "NO_AUTO_VALUE_ON_ZERO";

INSERT INTO `local_postalcodes` VALUES
(1, 'L7J 2L7', 'L7J', 517, 15, 3, 'Acton', 'acton', 43.58033000, -80.01144600),
(2, 'P3L 1E9', 'P3L', 500, 11, 13, 'Garson', 'garson', 46.54910200, -80.85862600);
"""
        dump_file = tmp_path / "test.sql"
        dump_file.write_text(dump_content)

        batches = list(parse_mysql_dump(str(dump_file), batch_size=10))
        assert len(batches) == 1
        assert len(batches[0]) == 2
        assert batches[0][0]['city'] == 'Acton'
        assert batches[0][1]['city'] == 'Garson'

    def test_parse_batching(self, tmp_path):
        """Test that records are yielded in correct batch sizes."""
        # Create dump with 5 records
        records = []
        for i in range(5):
            records.append(f"({i}, 'A1A 1A1', 'A1A', 1, 1, 1, 'City{i}', 'city{i}', 43.0, -80.0)")

        dump_content = f"""INSERT INTO `local_postalcodes` VALUES
{','.join(records)};
"""
        dump_file = tmp_path / "test.sql"
        dump_file.write_text(dump_content)

        # Batch size 2 should yield 3 batches (2, 2, 1)
        batches = list(parse_mysql_dump(str(dump_file), batch_size=2))
        assert len(batches) == 3
        assert len(batches[0]) == 2
        assert len(batches[1]) == 2
        assert len(batches[2]) == 1

    def test_parse_skips_non_data_lines(self, tmp_path):
        """Test that comments and SQL commands are skipped."""
        dump_content = """-- This is a comment
/* Block comment */
SET SQL_MODE = "NO_AUTO_VALUE_ON_ZERO";
CREATE TABLE `local_postalcodes` (id INT);
ALTER TABLE `local_postalcodes` ADD PRIMARY KEY (id);

(1, 'L7J 2L7', 'L7J', 517, 15, 3, 'Acton', 'acton', 43.58, -80.01);
"""
        dump_file = tmp_path / "test.sql"
        dump_file.write_text(dump_content)

        batches = list(parse_mysql_dump(str(dump_file)))
        assert len(batches) == 1
        assert len(batches[0]) == 1

    def test_parse_nonexistent_file(self, tmp_path):
        """Test that nonexistent file raises error."""
        with pytest.raises(FileNotFoundError):
            list(parse_mysql_dump(str(tmp_path / "nonexistent.sql")))

    def test_parse_empty_file(self, tmp_path):
        """Test parsing empty file yields nothing."""
        dump_file = tmp_path / "empty.sql"
        dump_file.write_text("")

        batches = list(parse_mysql_dump(str(dump_file)))
        assert len(batches) == 0

    def test_parse_handles_empty_city(self, tmp_path):
        """Test parsing records with empty city field."""
        dump_content = "(1, 'L7J 2L7', 'L7J', 517, 15, 3, '', '', 43.58, -80.01);"
        dump_file = tmp_path / "test.sql"
        dump_file.write_text(dump_content)

        batches = list(parse_mysql_dump(str(dump_file)))
        assert len(batches) == 1
        assert batches[0][0]['city'] is None
        assert batches[0][0]['uri'] is None


class TestIntegration:
    """Integration tests using sample of real data format."""

    def test_parse_real_format(self, tmp_path):
        """Test parsing data in the exact format from the real dump."""
        dump_content = """-- phpMyAdmin SQL Dump
-- version 5.2.2
-- https://www.phpmyadmin.net/

SET SQL_MODE = "NO_AUTO_VALUE_ON_ZERO";
START TRANSACTION;
SET time_zone = "+00:00";

CREATE TABLE `local_postalcodes` (
  `id` bigint UNSIGNED NOT NULL,
  `postal` varchar(7) NOT NULL,
  `fsa` varchar(3) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO `local_postalcodes` (`id`, `postal`, `fsa`, `community`, `county`, `region`, `city`, `uri`, `latitude`, `longitude`) VALUES
(1, 'L7J 2L7', 'L7J', 517, 15, 3, 'Acton', 'acton', 43.58033000, -80.01144600),
(2, 'P3L 1E9', 'P3L', 500, 11, 13, 'Garson', 'garson', 46.54910200, -80.85862600),
(3, 'L7J 1K8', 'L7J', 517, 15, 3, 'Acton', 'acton', 43.63103790, -80.03848850);

ALTER TABLE `local_postalcodes`
  ADD PRIMARY KEY (`id`);

COMMIT;
"""
        dump_file = tmp_path / "cdn_db.sql"
        dump_file.write_text(dump_content)

        records = []
        for batch in parse_mysql_dump(str(dump_file)):
            records.extend(batch)

        assert len(records) == 3
        assert records[0]['postal_code'] == 'L7J2L7'
        assert records[1]['city'] == 'Garson'
        assert records[2]['latitude'] == 43.6310379
