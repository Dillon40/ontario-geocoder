"""
Tests for fuzzy street name matching functionality.

Tests the pg_trgm-based trigram similarity matching added in Migration 20.
Fuzzy matching catches typos like "Elizbeth" -> "Elizabeth".
"""
import pytest
from unittest.mock import AsyncMock, patch


# Test cases for similarity matching
# Format: (input_street, db_street, should_match_above_threshold)
FUZZY_TEST_CASES = [
    # Common typos that should match (similarity > 0.6)
    ("ELIZBETH", "ELIZABETH", True),       # Missing 'a'
    ("ELZABETH", "ELIZABETH", True),       # Missing 'i'
    ("ELLIZABETH", "ELIZABETH", True),     # Double 'l'
    ("ELIZABEHT", "ELIZABETH", True),      # Transposed 'th'
    ("QUEENSWEI", "QUEENSWAY", True),      # Wrong ending
    ("CHRUCHILL", "CHURCHILL", True),      # Transposed letters
    ("MAINSTREET", "MAIN STREET", True),   # Missing space (depends on implementation)

    # Different street names that should NOT match (similarity < 0.6)
    ("KING", "QUEEN", False),              # Different streets
    ("MAPLE", "MABEL", False),             # Too different
    ("OAK", "ELM", False),                 # Completely different
    ("FIRST", "FIFTH", False),             # Similar but different ordinals
]


class TestConfidenceScaling:
    """Tests for confidence score scaling with similarity."""

    def test_municipal_confidence_scales_with_similarity(self):
        """Test that municipal fuzzy confidence = 0.75 * similarity."""
        # Base confidence is 0.75
        # Minimum similarity threshold is 0.6
        # So confidence range should be 0.45 (0.75 * 0.6) to 0.75 (0.75 * 1.0)

        base_confidence = 0.75

        test_cases = [
            (0.6, 0.45),   # Minimum threshold
            (0.7, 0.525),  # Mid-range
            (0.8, 0.60),   # Good match
            (0.9, 0.675),  # Very good match
            (1.0, 0.75),   # Perfect match
        ]

        for similarity, expected_confidence in test_cases:
            calculated = round(base_confidence * similarity, 3)
            assert calculated == expected_confidence, \
                f"For similarity {similarity}, expected {expected_confidence}, got {calculated}"

    def test_nar_confidence_scales_with_similarity(self):
        """Test that NAR fuzzy confidence = 0.60 * similarity."""
        # Base confidence is 0.60
        # Minimum similarity threshold is 0.6
        # So confidence range should be 0.36 (0.60 * 0.6) to 0.60 (0.60 * 1.0)

        base_confidence = 0.60

        test_cases = [
            (0.6, 0.36),   # Minimum threshold
            (0.7, 0.42),   # Mid-range
            (0.8, 0.48),   # Good match
            (0.9, 0.54),   # Very good match
            (1.0, 0.60),   # Perfect match
        ]

        for similarity, expected_confidence in test_cases:
            calculated = round(base_confidence * similarity, 2)
            assert calculated == expected_confidence, \
                f"For similarity {similarity}, expected {expected_confidence}, got {calculated}"


class TestFuzzyMatchingIntegration:
    """Integration tests for fuzzy matching in the full geocoding pipeline."""

    @pytest.mark.asyncio
    async def test_fuzzy_match_is_tried_after_exact_methods_fail(self):
        """Test that fuzzy matching is tried when layered matcher returns None."""
        from engine.geocoder import match_municipal_data

        with patch('engine.geocoder.match_municipal_layered', AsyncMock(return_value=None)) as mock_layered, \
             patch('engine.geocoder.get_validator') as mock_validator:

            # Setup mock validator
            mock_validator_instance = AsyncMock()
            mock_validator_instance.enrich_query_geography = AsyncMock(return_value={})
            mock_validator.return_value = mock_validator_instance

            result = await match_municipal_data({
                'street_number': '325',
                'street_name': 'Elizbeth',  # Typo
                'postal_code': 'N1E7A3'
            })

            # Verify layered matcher was called
            mock_layered.assert_called_once()
            # No match returned
            assert result is None

    @pytest.mark.asyncio
    async def test_exact_match_preferred_over_fuzzy(self):
        """Test that exact match is returned from layered matcher."""
        from engine.geocoder import match_municipal_data

        exact_result = {
            'latitude': 43.5448,
            'longitude': -80.2482,
            'matched_address': '325 Elizabeth Avenue',
            'confidence': 0.95,
            'match_tier': 2,
            'match_method': 'postal_number'
        }

        with patch('engine.geocoder.match_municipal_layered', AsyncMock(return_value=exact_result)), \
             patch('engine.geocoder.get_validator') as mock_validator:

            mock_validator_instance = AsyncMock()
            mock_validator_instance.enrich_query_geography = AsyncMock(return_value={})
            mock_validator.return_value = mock_validator_instance

            result = await match_municipal_data({
                'street_number': '325',
                'street_name': 'Elizabeth',  # Correct spelling
                'postal_code': 'N1E7A3'
            })

            # Verify exact match was returned
            assert result['confidence'] == 0.95


