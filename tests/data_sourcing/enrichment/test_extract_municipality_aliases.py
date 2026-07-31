"""
Regression test for extract_municipality_aliases.py's SQL query surface.

Guards against the stale-column bug where the script referenced
lower_municipal_boundaries."NM_SHORT_E" / "NAME_E" (quoted, mixed-case
identifiers) after those columns were renamed to short_name / name in
schema.sql. That mismatch made every query in the script fail against a
live database, silently leaving municipality_aliases under-populated.

This is a static check (no database connection needed): it parses the
CREATE TABLE block for lower_municipal_boundaries out of
database/schema.sql to get the real column list, then parses every SQL
string passed to a asyncpg connection call in the script and asserts any
double-quoted (case-preserving) identifier it uses is an actual column.

Run with: uv run pytest tests/data_sourcing/enrichment/test_extract_municipality_aliases.py -v
"""
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
SCHEMA_FILE = PROJECT_ROOT / 'database' / 'schema.sql'
SCRIPT_FILE = PROJECT_ROOT / 'data_sourcing' / 'enrichment' / 'extract_municipality_aliases.py'

TABLE_NAME = 'lower_municipal_boundaries'


def _get_schema_columns(table_name: str) -> set:
    """Extract the column names of a table's CREATE TABLE block in schema.sql."""
    schema_sql = SCHEMA_FILE.read_text()
    match = re.search(
        rf'CREATE TABLE IF NOT EXISTS {re.escape(table_name)} \((.*?)\n\);',
        schema_sql, re.DOTALL
    )
    assert match, f"Could not find CREATE TABLE block for {table_name} in schema.sql"

    columns = set()
    for line in match.group(1).splitlines():
        line = line.strip().rstrip(',')
        if not line or line.startswith('--'):
            continue
        column_match = re.match(r'"([A-Za-z_][A-Za-z0-9_]*)"|([A-Za-z_][A-Za-z0-9_]*)', line)
        if column_match:
            columns.add(column_match.group(1) or column_match.group(2))
    return columns


def _get_quoted_identifiers_in_queries(script_path: Path) -> set:
    """
    Extract every double-quoted (case-preserving) SQL identifier referenced
    inside triple-quoted strings passed directly to an asyncpg connection
    call (conn.fetch/fetchval/fetchrow/execute) - i.e. real query text, not
    docstrings or comments elsewhere in the file.
    """
    source = script_path.read_text()
    query_blocks = re.findall(
        r'conn\.(?:fetch|fetchval|fetchrow|execute)\(\s*"""(.*?)"""',
        source, re.DOTALL
    )
    identifiers = set()
    for block in query_blocks:
        identifiers.update(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', block))
    return identifiers


class TestExtractMunicipalityAliasesSchemaSurface:
    """extract_municipality_aliases.py's queries must only reference real columns."""

    def test_quoted_identifiers_are_real_columns(self):
        """
        Every double-quoted identifier in the script's SQL text must exist
        as an actual column of lower_municipal_boundaries per schema.sql.

        This is the check that would have caught the "NM_SHORT_E" / "NAME_E"
        stale-column bug: those identifiers were quoted references to
        columns that had already been renamed to short_name / name.
        """
        real_columns = _get_schema_columns(TABLE_NAME)
        referenced = _get_quoted_identifiers_in_queries(SCRIPT_FILE)

        stale = referenced - real_columns
        assert not stale, (
            f"extract_municipality_aliases.py references quoted column(s) "
            f"{sorted(stale)} that do not exist on {TABLE_NAME} per "
            f"schema.sql. Known real columns: {sorted(real_columns)}"
        )

    def test_known_stale_columns_are_not_referenced(self):
        """Belt-and-suspenders: the specific historical stale names must be gone."""
        source = SCRIPT_FILE.read_text()
        assert '"NM_SHORT_E"' not in source
        assert '"NAME_E"' not in source

    def test_expected_renamed_columns_are_used(self):
        """The script should use the current (unquoted) column names."""
        source = SCRIPT_FILE.read_text()
        assert 'short_name' in source
        assert re.search(r'\bname\b', source), "expected unquoted 'name' column reference"
