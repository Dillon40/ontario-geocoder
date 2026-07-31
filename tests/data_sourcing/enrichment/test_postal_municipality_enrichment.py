"""
Tests for postal municipality enrichment functionality.

Run with: uv run pytest tests/data_sourcing/enrichment/test_postal_municipality_enrichment.py -v
"""
import pytest
from unittest.mock import MagicMock, AsyncMock
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))


class TestPostalMunicipalitySetup:
    """Tests for PostalMunicipalitySetup transformer."""

    def test_transformer_name(self):
        """Test transformer has correct name."""
        from data_sourcing.transformers.postal_municipality_setup import PostalMunicipalitySetup

        transformer = PostalMunicipalitySetup()
        assert transformer.name == "PostalMunicipalitySetup"

    def test_default_min_alias_count(self):
        """Test default minimum alias count is 10."""
        from data_sourcing.transformers.postal_municipality_setup import PostalMunicipalitySetup

        transformer = PostalMunicipalitySetup()
        assert transformer.min_alias_count == 10

    def test_custom_min_alias_count(self):
        """Test custom minimum alias count."""
        from data_sourcing.transformers.postal_municipality_setup import PostalMunicipalitySetup

        transformer = PostalMunicipalitySetup(min_alias_count=5)
        assert transformer.min_alias_count == 5

    def test_normalize_canonical_name_with_prefix(self):
        """Test canonical name normalization removes prefixes."""
        from data_sourcing.transformers.postal_municipality_setup import PostalMunicipalitySetup

        transformer = PostalMunicipalitySetup()

        assert transformer._normalize_canonical_name("CITY OF TORONTO") == "Toronto"
        assert transformer._normalize_canonical_name("TOWN OF OAKVILLE") == "Oakville"
        assert transformer._normalize_canonical_name("TOWNSHIP OF KING") == "King"
        assert transformer._normalize_canonical_name("MUNICIPALITY OF CHATHAM-KENT") == "Chatham-Kent"

    def test_normalize_canonical_name_without_prefix(self):
        """Test canonical name normalization handles names without prefixes."""
        from data_sourcing.transformers.postal_municipality_setup import PostalMunicipalitySetup

        transformer = PostalMunicipalitySetup()

        assert transformer._normalize_canonical_name("MISSISSIPPI MILLS") == "Mississippi Mills"
        assert transformer._normalize_canonical_name("GREATER SUDBURY") == "Greater Sudbury"

    def test_normalize_canonical_name_none(self):
        """Test canonical name normalization handles None."""
        from data_sourcing.transformers.postal_municipality_setup import PostalMunicipalitySetup

        transformer = PostalMunicipalitySetup()
        assert transformer._normalize_canonical_name(None) is None

    def test_normalize_canonical_name_empty(self):
        """Test canonical name normalization handles empty string."""
        from data_sourcing.transformers.postal_municipality_setup import PostalMunicipalitySetup

        transformer = PostalMunicipalitySetup()
        assert transformer._normalize_canonical_name("") == ""


class TestPostalMunicipalitySetupAsync:
    """Async tests for PostalMunicipalitySetup."""

    @pytest.mark.asyncio
    async def test_needs_setup_no_table(self):
        """Test needs_setup returns False when table doesn't exist."""
        from data_sourcing.transformers.postal_municipality_setup import PostalMunicipalitySetup

        transformer = PostalMunicipalitySetup()

        # Mock pool and connection
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value=False)  # Table doesn't exist

        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_conn), __aexit__=AsyncMock()))

        result = await transformer.needs_setup(mock_pool)
        assert result is False

    @pytest.mark.asyncio
    async def test_needs_setup_empty_table(self):
        """Test needs_setup returns False when table is empty."""
        from data_sourcing.transformers.postal_municipality_setup import PostalMunicipalitySetup

        transformer = PostalMunicipalitySetup()

        # Mock pool and connection
        mock_conn = AsyncMock()
        # First call: table exists, second call: table empty
        mock_conn.fetchval = AsyncMock(side_effect=[True, False])

        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_conn), __aexit__=AsyncMock()))

        result = await transformer.needs_setup(mock_pool)
        assert result is False

    @pytest.mark.asyncio
    async def test_needs_setup_all_enriched(self):
        """Test needs_setup returns False when all postal codes are enriched."""
        from data_sourcing.transformers.postal_municipality_setup import PostalMunicipalitySetup

        transformer = PostalMunicipalitySetup()

        # Mock pool and connection
        mock_conn = AsyncMock()
        # table exists, has data, col exists, unenriched=0
        mock_conn.fetchval = AsyncMock(side_effect=[True, True, True, 0])

        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_conn), __aexit__=AsyncMock()))

        result = await transformer.needs_setup(mock_pool)
        assert result is False

    @pytest.mark.asyncio
    async def test_needs_setup_has_unenriched(self):
        """Test needs_setup returns True when there are unenriched postal codes."""
        from data_sourcing.transformers.postal_municipality_setup import PostalMunicipalitySetup

        transformer = PostalMunicipalitySetup()

        # Mock pool and connection
        mock_conn = AsyncMock()
        # table exists, has data, col exists, unenriched=100
        mock_conn.fetchval = AsyncMock(side_effect=[True, True, True, 100])

        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_conn), __aexit__=AsyncMock()))

        result = await transformer.needs_setup(mock_pool)
        assert result is True

    @pytest.mark.asyncio
    async def test_check_dependencies_no_boundaries(self):
        """Test check_dependencies fails when boundaries table is empty."""
        from data_sourcing.transformers.postal_municipality_setup import PostalMunicipalitySetup

        transformer = PostalMunicipalitySetup()

        # Mock pool and connection
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value=False)  # No boundaries

        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_conn), __aexit__=AsyncMock()))

        ok, msg = await transformer.check_dependencies(mock_pool)
        assert ok is False
        assert "lower_municipal_boundaries" in msg

    @pytest.mark.asyncio
    async def test_check_dependencies_no_aliases_table(self):
        """Test check_dependencies fails when aliases table doesn't exist."""
        from data_sourcing.transformers.postal_municipality_setup import PostalMunicipalitySetup

        transformer = PostalMunicipalitySetup()

        # Mock pool and connection
        mock_conn = AsyncMock()
        # boundaries exist, aliases table doesn't
        mock_conn.fetchval = AsyncMock(side_effect=[True, False])

        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_conn), __aexit__=AsyncMock()))

        ok, msg = await transformer.check_dependencies(mock_pool)
        assert ok is False
        assert "municipality_aliases" in msg

    @pytest.mark.asyncio
    async def test_check_dependencies_success(self):
        """Test check_dependencies succeeds when all dependencies exist."""
        from data_sourcing.transformers.postal_municipality_setup import PostalMunicipalitySetup

        transformer = PostalMunicipalitySetup()

        # Mock pool and connection
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(side_effect=[True, True])  # Both exist

        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_conn), __aexit__=AsyncMock()))

        ok, msg = await transformer.check_dependencies(mock_pool)
        assert ok is True
        assert msg == ""


class TestTransformerImport:
    """Tests for transformer registration."""

    def test_transformer_importable_from_package(self):
        """Test PostalMunicipalitySetup is importable from transformers package."""
        from data_sourcing.transformers import PostalMunicipalitySetup

        assert PostalMunicipalitySetup is not None

    def test_transformer_in_all_list(self):
        """Test PostalMunicipalitySetup is in __all__ list."""
        from data_sourcing import transformers

        assert 'PostalMunicipalitySetup' in transformers.__all__

    def test_transformer_is_setup_transformer(self):
        """Test PostalMunicipalitySetup inherits from SetupTransformer."""
        from data_sourcing.transformers import PostalMunicipalitySetup, SetupTransformer

        transformer = PostalMunicipalitySetup()
        assert isinstance(transformer, SetupTransformer)


class TestEnrichmentScriptStructure:
    """Tests for enrichment script structure (without database)."""

    def test_enrichment_script_exists(self):
        """Test enrichment script file exists."""
        script_path = Path(__file__).parent.parent.parent.parent / 'data_sourcing' / 'enrichment' / 'enrich_postal_municipalities.py'
        assert script_path.exists(), f"Script not found at {script_path}"

    def test_alias_extraction_script_exists(self):
        """Test alias extraction script file exists."""
        script_path = Path(__file__).parent.parent.parent.parent / 'data_sourcing' / 'enrichment' / 'extract_postal_city_aliases.py'
        assert script_path.exists(), f"Script not found at {script_path}"

    def test_enrichment_script_importable(self):
        """Test enrichment script is importable."""
        try:
            sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / 'data_sourcing' / 'enrichment'))
            import enrich_postal_municipalities
            assert hasattr(enrich_postal_municipalities, 'PostalMunicipalityEnricher')
            assert hasattr(enrich_postal_municipalities, 'main')
        finally:
            sys.path.pop(0)

    def test_alias_script_importable(self):
        """Test alias extraction script is importable."""
        try:
            sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / 'data_sourcing' / 'enrichment'))
            import extract_postal_city_aliases
            assert hasattr(extract_postal_city_aliases, 'PostalCityAliasExtractor')
            assert hasattr(extract_postal_city_aliases, 'main')
        finally:
            sys.path.pop(0)


class TestEnricherClassAttributes:
    """Tests for enricher class default attributes."""

    def test_enricher_default_attributes(self):
        """Test PostalMunicipalityEnricher has expected default attributes."""
        sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / 'data_sourcing' / 'enrichment'))
        try:
            from enrich_postal_municipalities import PostalMunicipalityEnricher

            enricher = PostalMunicipalityEnricher()
            assert enricher.pool is None
            assert enricher.total_postal_codes == 0
            assert enricher.total_municipalities == 0
            assert enricher.enriched_count == 0
            assert enricher.unmatched_count == 0
        finally:
            sys.path.pop(0)

    def test_alias_extractor_default_attributes(self):
        """Test PostalCityAliasExtractor has expected default attributes."""
        sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / 'data_sourcing' / 'enrichment'))
        try:
            from extract_postal_city_aliases import PostalCityAliasExtractor

            extractor = PostalCityAliasExtractor()
            assert extractor.pool is None
            assert extractor.aliases_found == 0
            assert extractor.min_postal_count == 10
        finally:
            sys.path.pop(0)
