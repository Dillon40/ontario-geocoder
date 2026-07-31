#!/usr/bin/env python3
"""
Tests for GeographicValidator including zone-based municipality inference.

Uses mocked database to avoid requiring a live PostgreSQL connection.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def mock_db_pool():
    """Mock database pool that returns mock connections."""
    mock_conn = AsyncMock()
    mock_pool = MagicMock()

    # Make pool.acquire() return an async context manager yielding mock_conn
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_pool.acquire.return_value = mock_ctx

    return mock_pool, mock_conn


class TestZoneInference:
    """Tests for postal zone-based municipality inference."""

    @pytest.fixture
    def validator(self):
        """Create a GeographicValidator instance."""
        from engine.geographic_validator import GeographicValidator
        return GeographicValidator()

    @pytest.mark.asyncio
    async def test_single_municipality_zone_toronto(self, validator, mock_db_pool):
        """M5 zone should infer Toronto (single municipality)."""
        mock_pool, mock_conn = mock_db_pool
        mock_conn.fetch.return_value = [{'municipality_name_short': 'TORONTO'}]

        with patch('engine.geographic_validator.database.get_pool', return_value=mock_pool):
            result = await validator.infer_municipality_from_zone("M5")
        assert result == "TORONTO"

    @pytest.mark.asyncio
    async def test_single_municipality_zone_mississauga(self, validator, mock_db_pool):
        """L5 zone should infer Mississauga (single municipality)."""
        mock_pool, mock_conn = mock_db_pool
        mock_conn.fetch.return_value = [{'municipality_name_short': 'MISSISSAUGA'}]

        with patch('engine.geographic_validator.database.get_pool', return_value=mock_pool):
            result = await validator.infer_municipality_from_zone("L5")
        assert result == "MISSISSAUGA"

    @pytest.mark.asyncio
    async def test_single_municipality_zone_hamilton(self, validator, mock_db_pool):
        """L8 zone should infer Hamilton (single municipality)."""
        mock_pool, mock_conn = mock_db_pool
        mock_conn.fetch.return_value = [{'municipality_name_short': 'HAMILTON'}]

        with patch('engine.geographic_validator.database.get_pool', return_value=mock_pool):
            result = await validator.infer_municipality_from_zone("L8")
        assert result == "HAMILTON"

    @pytest.mark.asyncio
    async def test_multi_municipality_zone_returns_none(self, validator, mock_db_pool):
        """L0 zone spans multiple municipalities, should return None."""
        mock_pool, mock_conn = mock_db_pool
        mock_conn.fetch.return_value = [
            {'municipality_name_short': 'INNISFIL'},
            {'municipality_name_short': 'BRADFORD WEST GWILLIMBURY'}
        ]

        with patch('engine.geographic_validator.database.get_pool', return_value=mock_pool):
            result = await validator.infer_municipality_from_zone("L0")
        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_zone_returns_none(self, validator, mock_db_pool):
        """Invalid zone codes should return None."""
        mock_pool, mock_conn = mock_db_pool

        with patch('engine.geographic_validator.database.get_pool', return_value=mock_pool):
            # Too short
            assert await validator.infer_municipality_from_zone("M") is None
            # Too long (FSA, not zone)
            assert await validator.infer_municipality_from_zone("M5V") is None
            # Empty
            assert await validator.infer_municipality_from_zone("") is None
            # None
            assert await validator.infer_municipality_from_zone(None) is None

    @pytest.mark.asyncio
    async def test_zone_inference_caching(self, validator, mock_db_pool):
        """Zone inference results should be cached."""
        mock_pool, mock_conn = mock_db_pool
        mock_conn.fetch.return_value = [{'municipality_name_short': 'TORONTO'}]

        with patch('engine.geographic_validator.database.get_pool', return_value=mock_pool):
            # Clear cache
            validator._mapping_cache.clear()

            # First call
            result1 = await validator.infer_municipality_from_zone("M5")
            assert "zone:M5" in validator._mapping_cache

            # Second call should use cache (no additional DB call)
            result2 = await validator.infer_municipality_from_zone("M5")
            assert result1 == result2
            # fetch should only be called once since second call uses cache
            assert mock_conn.fetch.call_count == 1


class TestEnrichQueryGeographyZoneFallback:
    """Tests for zone-based fallback in enrich_query_geography."""

    @pytest.fixture
    def validator(self):
        """Create a GeographicValidator instance."""
        from engine.geographic_validator import GeographicValidator
        return GeographicValidator()

    @pytest.mark.asyncio
    async def test_unknown_fsa_with_single_municipality_zone(self, validator, mock_db_pool):
        """Unknown FSA in single-municipality zone should infer city."""
        mock_pool, mock_conn = mock_db_pool

        # First call: get_municipalities_for_fsa returns empty (unknown FSA)
        # Second call: infer_municipality_from_zone returns single municipality
        mock_conn.fetch.side_effect = [
            [],  # get_municipalities_for_fsa: M5Z not found
            [{'municipality_name_short': 'TORONTO'}]  # infer_municipality_from_zone: M5 zone
        ]

        with patch('engine.geographic_validator.database.get_pool', return_value=mock_pool):
            query = {'postal_code': 'M5Z1A1'}
            enriched = await validator.enrich_query_geography(query)

        assert enriched.get('normalized_city') == 'TORONTO'
        assert enriched.get('geography_confidence_adjustment') == validator.CONFIDENCE_BOOST_ZONE_INFERRED
        assert any("zone" in note and "M5" in note for note in enriched.get('geography_notes', []))

    @pytest.mark.asyncio
    async def test_unknown_fsa_with_multi_municipality_zone(self, validator, mock_db_pool):
        """Unknown FSA in multi-municipality zone should not infer city."""
        mock_pool, mock_conn = mock_db_pool

        mock_conn.fetch.side_effect = [
            [],  # get_municipalities_for_fsa: L0Z not found
            [  # infer_municipality_from_zone: L0 zone has multiple
                {'municipality_name_short': 'INNISFIL'},
                {'municipality_name_short': 'BRADFORD WEST GWILLIMBURY'}
            ]
        ]

        with patch('engine.geographic_validator.database.get_pool', return_value=mock_pool):
            query = {'postal_code': 'L0Z1A1'}
            enriched = await validator.enrich_query_geography(query)

        assert enriched.get('normalized_city') is None
        assert any("multiple municipalities" in note for note in enriched.get('geography_notes', []))

    @pytest.mark.asyncio
    async def test_known_fsa_does_not_use_zone_fallback(self, validator, mock_db_pool):
        """Known FSA should use normal FSA inference, not zone fallback."""
        mock_pool, mock_conn = mock_db_pool

        # get_municipalities_for_fsa returns results (FSA is known)
        mock_conn.fetch.return_value = [
            {
                'municipal_id': 1,
                'municipality_name': 'TORONTO',
                'municipality_name_full': 'City of Toronto',
                'is_primary': True,
                'coverage_pct': 100.0,
                'address_count': 5000
            }
        ]

        with patch('engine.geographic_validator.database.get_pool', return_value=mock_pool):
            query = {'postal_code': 'M5V1A1'}
            enriched = await validator.enrich_query_geography(query)

        # Should use FSA inference, not zone inference
        if enriched.get('geography_notes'):
            assert not any("zone" in note.lower() and "unknown fsa" in note.lower()
                          for note in enriched['geography_notes'])


class TestConfidenceConstants:
    """Tests for confidence adjustment constants."""

    def test_zone_confidence_lower_than_fsa(self):
        """Zone inference confidence should be lower than FSA inference."""
        from engine.geographic_validator import GeographicValidator

        assert GeographicValidator.CONFIDENCE_BOOST_ZONE_INFERRED < GeographicValidator.CONFIDENCE_BOOST_INFERRED
        assert GeographicValidator.CONFIDENCE_BOOST_ZONE_INFERRED > 0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
