"""
Unit tests for engine/layered_matcher.py — layer cascade, disambiguation
scoring, and tie handling. All DB rows are mocked; no live database needed.
"""
import pytest
from unittest.mock import AsyncMock, patch

from engine.layered_matcher import (
    LAYER_CONFIDENCE,
    DISAMBIGUATION_MARGIN,
    LayeredMatcher,
    LayeredNARMatcher,
    LayerResult,
    disambiguate,
)


def make_property_row(**overrides):
    """A properties row with sensible defaults, dict-based like asyncpg Records."""
    row = {
        'id': 1,
        'latitude': 43.6500,
        'longitude': -79.3800,
        'full_address': '100 Queen St W, Toronto',
        'source_dataset': 'toronto',
        'property_id': 'TOR-1',
        'assessment_roll_number': None,
        'pin': None,
        'gpid': None,
        'postal_code': 'M5H2N2',
        'fsa_code': 'M5H',
        'street_number': '100',
        'street_name': 'Queen',
        'street_suffix': 'St',
        'street_direction': 'W',
        'unit_no': None,
        'civic_no_suffix': None,
        'city': 'Toronto',
        'province': 'ON',
    }
    row.update(overrides)
    return row


class TestDisambiguateBasics:
    """Basic contract of disambiguate()."""

    def test_empty_results(self):
        assert disambiguate([], {}) == (None, False, 0)

    def test_single_result_needs_no_disambiguation(self):
        row = make_property_row()
        best, was_disambiguated, margin = disambiguate([row], {})
        assert best is row
        assert was_disambiguated is False
        assert margin == 100

    def test_clear_winner_by_direction(self):
        """Direction match (30 pts) beats no match by more than the margin."""
        west = make_property_row(id=1, street_direction='W')
        east = make_property_row(id=2, street_direction='E', latitude=43.6600)
        components = {'street_direction_normalized': 'W'}

        best, was_disambiguated, margin = disambiguate([east, west], components)

        assert best is west
        assert was_disambiguated is True
        assert margin == 30
        assert margin > DISAMBIGUATION_MARGIN

    def test_all_zero_scores_stays_ambiguous(self):
        """No discriminating input fields — nothing to pick a winner with."""
        rows = [make_property_row(id=1), make_property_row(id=2, latitude=44.0)]
        best, was_disambiguated, margin = disambiguate(rows, {})
        assert best is None
        assert was_disambiguated is False
        assert margin == 0

    def test_small_margin_stays_ambiguous(self):
        """A lead of <= DISAMBIGUATION_MARGIN points is not a clear winner."""
        # Postal match = 10 pts vs 0 pts: margin of exactly 10, not enough
        match = make_property_row(id=1, postal_code='M5H2N2', fsa_code=None)
        no_match = make_property_row(id=2, postal_code='M4C1B5', fsa_code=None,
                                     latitude=43.7000)
        components = {'postal_code': 'M5H2N2'}

        best, was_disambiguated, margin = disambiguate([match, no_match], components)

        assert margin == 10
        assert best is None
        assert was_disambiguated is False


class TestDisambiguateScoring:
    """Point values for each disambiguation signal."""

    def test_suffix_match_scores_20(self):
        with_suffix = make_property_row(id=1, street_suffix='Ave')
        other = make_property_row(id=2, street_suffix='Blvd', latitude=43.7)
        components = {'street_suffix_normalized': 'Ave'}

        best, was_disambiguated, margin = disambiguate([other, with_suffix], components)
        assert best is with_suffix
        assert margin == 20

    def test_civic_suffix_match_scores_25(self):
        plain = make_property_row(id=1, civic_no_suffix=None, latitude=43.7)
        suffixed = make_property_row(id=2, civic_no_suffix='A')
        components = {'civic_no_suffix': 'A'}

        best, was_disambiguated, margin = disambiguate([plain, suffixed], components)
        assert best is suffixed
        assert margin == 25

    def test_fsa_and_postal_stack(self):
        """Full postal (10) + FSA (25) = 35-point lead for the right row."""
        right = make_property_row(id=1, postal_code='M5H2N2', fsa_code='M5H')
        wrong = make_property_row(id=2, postal_code='L4B1A1', fsa_code='L4B',
                                  latitude=43.8500)
        components = {'postal_code': 'M5H2N2'}

        best, was_disambiguated, margin = disambiguate([wrong, right], components)
        assert best is right
        assert margin == 35


class TestDisambiguateTieHandling:
    """Exact ties: same-building ties are safe, distinct-location ties are not."""

    def test_tie_same_coordinates_returns_first(self):
        """Condo units in one building: identical coords, any row is correct."""
        unit_1 = make_property_row(id=1, unit_no='101',
                                   latitude=43.6500, longitude=-79.3800)
        unit_2 = make_property_row(id=2, unit_no='102',
                                   latitude=43.6500, longitude=-79.3800)
        # Both rows score identically (postal match on each)
        components = {'postal_code': 'M5H2N2'}

        best, was_disambiguated, margin = disambiguate([unit_1, unit_2], components)

        assert best is unit_1
        assert was_disambiguated is True
        assert margin == 0

    def test_tie_distinct_coordinates_returns_no_match(self):
        """Genuinely different locations that tie must NOT silently pick row 0."""
        location_a = make_property_row(id=1, latitude=43.6500, longitude=-79.3800)
        location_b = make_property_row(id=2, latitude=43.9100, longitude=-78.9500)
        # Same postal on both — identical scores, but different places
        components = {'postal_code': 'M5H2N2'}

        best, was_disambiguated, margin = disambiguate([location_a, location_b], components)

        assert best is None
        assert was_disambiguated is False
        assert margin == 0

    def test_tie_with_missing_coordinates_stays_ambiguous(self):
        """If we can't verify the tied rows are the same place, don't guess."""
        known = make_property_row(id=1)
        unknown = make_property_row(id=2, latitude=None, longitude=None)
        components = {'postal_code': 'M5H2N2'}

        best, was_disambiguated, margin = disambiguate([known, unknown], components)

        assert best is None
        assert was_disambiguated is False

    def test_tie_float_noise_still_counts_as_same_location(self):
        """Sub-micro-degree float differences must not split a building."""
        unit_1 = make_property_row(id=1, latitude=43.65000000001, longitude=-79.38)
        unit_2 = make_property_row(id=2, latitude=43.65000000002, longitude=-79.38)
        components = {'postal_code': 'M5H2N2'}

        best, was_disambiguated, margin = disambiguate([unit_1, unit_2], components)

        assert best is unit_1
        assert was_disambiguated is True

    def test_tie_within_radius_recovers(self):
        """Duplicate rows for one civic address, geocoded a few metres apart,
        are one building — pick the top-scored row instead of going ambiguous.
        Real case: 108 Peter St, Toronto, two Toronto rows ~4m apart."""
        row_a = make_property_row(id=1, latitude=43.64767933, longitude=-79.39327650)
        row_b = make_property_row(id=2, latitude=43.64767807, longitude=-79.39332653)
        # Postal chosen to match the default row fsa/postal so both rows tie on
        # a non-zero score; the 4m coordinate gap is what this test exercises.
        components = {'postal_code': 'M5H2N2'}

        best, was_disambiguated, margin = disambiguate([row_a, row_b], components)

        assert best is row_a
        assert was_disambiguated is True

    def test_tie_beyond_radius_stays_ambiguous(self):
        """Same civic+street+city at points ~500m apart are genuinely different
        places — must not be collapsed by the radius tolerance."""
        row_a = make_property_row(id=1, latitude=43.6500, longitude=-79.3800)
        row_b = make_property_row(id=2, latitude=43.6500, longitude=-79.3862)
        components = {'postal_code': 'M5H2N2'}

        best, was_disambiguated, margin = disambiguate([row_a, row_b], components)

        assert best is None
        assert was_disambiguated is False

    def test_tie_nar_rows_use_bg_coordinates(self):
        """NAR rows carry bg_latitude/bg_longitude — same rules apply."""
        nar_row = {
            'id': 1,
            'bg_latitude': 43.65,
            'bg_longitude': -79.38,
            'mail_postal_code': 'M5H2N2',
            'official_street_dir': None,
            'official_street_type': None,
            'civic_no_suffix': None,
        }
        nar_row_2 = dict(nar_row, id=2, bg_latitude=44.20)
        components = {'postal_code': 'M5H2N2'}

        best, was_disambiguated, margin = disambiguate(
            [nar_row, nar_row_2], components, table_type='nar')

        assert best is None
        assert was_disambiguated is False


class TestLayerCascade:
    """Cascade ordering of LayeredMatcher with mocked DB."""

    FULL_COMPONENTS = {
        'street_number': '100',
        'street_name': 'Queen',
        'postal_code': 'M5H2N2',
        'city': 'Toronto',
    }

    @pytest.mark.asyncio
    async def test_all_layers_tried_in_order_when_nothing_matches(self):
        matcher = LayeredMatcher()

        with patch('engine.layered_matcher.database') as mock_db:
            mock_db.query = AsyncMock(return_value=[])
            result = await matcher.match(dict(self.FULL_COMPONENTS))

        assert result is None
        layers = [a.layer for a in matcher.metadata.layers_tried]
        assert layers == [0, 1, 2, 3, 4, 5, 6]

    @pytest.mark.asyncio
    async def test_without_postal_skips_postal_layers(self):
        """No postal code: layers 1, 2, 3, 5 are skipped entirely."""
        matcher = LayeredMatcher()
        components = {'street_number': '100', 'street_name': 'Queen', 'city': 'Toronto'}

        with patch('engine.layered_matcher.database') as mock_db:
            mock_db.query = AsyncMock(return_value=[])
            result = await matcher.match(components)

        assert result is None
        layers = [a.layer for a in matcher.metadata.layers_tried]
        # Layer 0 records a NO_MATCH attempt (missing postal), then city layers
        assert layers == [0, 4, 6]

    @pytest.mark.asyncio
    async def test_missing_core_identity_returns_none_without_queries(self):
        matcher = LayeredMatcher()

        with patch('engine.layered_matcher.database') as mock_db:
            mock_db.query = AsyncMock(return_value=[])
            result = await matcher.match({'postal_code': 'M5H2N2'})

        assert result is None
        assert matcher.metadata.layers_tried == []
        mock_db.query.assert_not_called()

    @pytest.mark.asyncio
    async def test_layer_0_single_match_wins_immediately(self):
        matcher = LayeredMatcher()
        row = make_property_row()

        with patch('engine.layered_matcher.database') as mock_db:
            mock_db.query = AsyncMock(return_value=[row])
            result = await matcher.match(dict(self.FULL_COMPONENTS))

        assert result is not None
        assert result['match_layer'] == 0
        assert result['confidence'] == LAYER_CONFIDENCE[0]
        assert result['match_tier'] == 2
        assert mock_db.query.await_count == 1

    @pytest.mark.asyncio
    async def test_falls_through_to_layer_1(self):
        """Layer 0 empty, layer 1 single row: result comes from layer 1."""
        matcher = LayeredMatcher()
        row = make_property_row()

        with patch('engine.layered_matcher.database') as mock_db:
            mock_db.query = AsyncMock(side_effect=[[], [row]])
            result = await matcher.match(dict(self.FULL_COMPONENTS))

        assert result is not None
        assert result['match_layer'] == 1
        assert result['confidence'] == LAYER_CONFIDENCE[1]
        attempts = {a.layer: a.result for a in matcher.metadata.layers_tried}
        assert attempts[0] == LayerResult.NO_MATCH
        assert attempts[1] == LayerResult.SINGLE_MATCH

    @pytest.mark.asyncio
    async def test_ambiguous_layer_proceeds_to_next(self):
        """Distinct-location tie in layer 1 must cascade, not return row 0."""
        matcher = LayeredMatcher()
        location_a = make_property_row(id=1, latitude=43.65)
        location_b = make_property_row(id=2, latitude=43.91)
        winner = make_property_row(id=3, latitude=44.05, full_address='unique row')

        with patch('engine.layered_matcher.database') as mock_db:
            # Layer 0: no rows; layer 1: tied pair; layer 2: single clear row
            mock_db.query = AsyncMock(side_effect=[[], [location_a, location_b], [winner]])
            result = await matcher.match(dict(self.FULL_COMPONENTS))

        assert result is not None
        assert result['match_layer'] == 2
        assert result['matched_address'] == 'unique row'
        attempts = {a.layer: a.result for a in matcher.metadata.layers_tried}
        assert attempts[1] == LayerResult.AMBIGUOUS

    @pytest.mark.asyncio
    async def test_disambiguated_match_records_margin(self):
        matcher = LayeredMatcher()
        west = make_property_row(id=1, street_direction='W')
        east = make_property_row(id=2, street_direction='E', latitude=43.7)
        components = dict(self.FULL_COMPONENTS, street_direction_normalized='W')

        with patch('engine.layered_matcher.database') as mock_db:
            mock_db.query = AsyncMock(side_effect=[[], [east, west]])
            result = await matcher.match(components)

        assert result is not None
        assert result['match_layer'] == 1
        assert result['latitude'] == west['latitude']
        assert result['match_metadata']['disambiguation_used'] is True
        assert result['match_metadata']['disambiguation_margin'] == 30

    @pytest.mark.asyncio
    async def test_nar_cascade_layer_parity_with_municipal(self):
        """NAR matcher walks the same layer sequence as the municipal one."""
        nar_matcher = LayeredNARMatcher()

        with patch('engine.layered_matcher.database') as mock_db:
            mock_db.query = AsyncMock(return_value=[])
            result = await nar_matcher.match(dict(self.FULL_COMPONENTS))

        assert result is None
        layers = [a.layer for a in nar_matcher.metadata.layers_tried]
        assert layers == [0, 1, 2, 3, 4, 5, 6]


class TestQueryDeterminism:
    """Every LIMITed candidate query must carry a deterministic ORDER BY."""

    @pytest.mark.asyncio
    async def test_every_limit_query_has_order_by(self):
        captured = []

        async def capture_query(sql, params=None):
            captured.append(sql)
            return []

        components = {
            'street_number': '100',
            'street_name': 'Queen',
            'postal_code': 'M5H2N2',
            'city': 'Toronto',
        }

        with patch('engine.layered_matcher.database') as mock_db:
            mock_db.query = AsyncMock(side_effect=capture_query)
            await LayeredMatcher().match(dict(components))
            await LayeredNARMatcher().match(dict(components))

        assert captured, "expected layer queries to run"
        for sql in captured:
            if 'LIMIT' in sql:
                assert 'ORDER BY' in sql, f"LIMIT without ORDER BY in:\n{sql}"
