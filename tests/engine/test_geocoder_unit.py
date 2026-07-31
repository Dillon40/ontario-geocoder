"""
Unit tests for engine/geocoder.py — core geocoding logic with mocked dependencies.
"""
import pytest
from unittest.mock import AsyncMock, patch


class TestGetStats:
    """Tests for get_stats()."""

    def test_initial_stats(self):
        from engine.geocoder import get_stats, reset_stats
        reset_stats()

        stats = get_stats()
        assert stats['total_requests'] == 0
        assert stats['successful'] == 0
        assert stats['failed'] == 0
        assert stats['success_rate'] == 0
        assert stats['cache_hits'] == 0
        assert stats['avg_response_time_ms'] == 0
        assert 'tier_distribution' in stats

    def test_stats_tier_distribution(self):
        from engine.geocoder import get_stats, reset_stats
        reset_stats()

        stats = get_stats()
        td = stats['tier_distribution']
        assert td['tier_1_pin'] == 0
        assert td['tier_2_municipal'] == 0
        assert td['tier_3_nar'] == 0
        assert 'tier_4_google' not in td


class TestResetStats:
    """Tests for reset_stats()."""

    def test_reset(self):
        from engine.geocoder import get_stats, reset_stats, _stats
        # Modify stats
        _stats['total_requests'] = 100
        _stats['successful'] = 50

        reset_stats()
        stats = get_stats()
        assert stats['total_requests'] == 0
        assert stats['successful'] == 0


class TestGeocode:
    """Tests for geocode() — async, requires mocking."""

    @pytest.mark.asyncio
    async def test_cache_hit(self):
        from engine.geocoder import geocode, reset_stats

        reset_stats()
        cached_result = {
            'latitude': 43.65,
            'longitude': -79.38,
            'success': True,
            'match_tier': 2
        }

        with patch('engine.geocoder.cache') as mock_cache, \
             patch('engine.geocoder.parse_address', return_value={'street_name': 'Queen'}), \
             patch('engine.geocoder.generate_address_hash', return_value='abc123'):
            mock_cache.get = AsyncMock(return_value=cached_result)

            result = await geocode({'street_name': 'Queen'})

        assert result['cached'] is True
        assert result['latitude'] == 43.65

    @pytest.mark.asyncio
    async def test_tier1_pin_hit(self):
        from engine.geocoder import geocode, reset_stats

        reset_stats()
        pin_result = {
            'latitude': 43.65,
            'longitude': -79.38,
            'match_tier': 1,
            'match_method': 'pin_lookup',
            'confidence': 1.0,
            'matched_address': '100 Queen',
            'source': 'municipal_toronto',
            'property_id': 'TOR-123',
            'assessment_roll_number': None,
            'pin': 12345,
            'gpid': None
        }

        with patch('engine.geocoder.cache') as mock_cache, \
             patch('engine.geocoder.parse_address', return_value={'pin': '12345'}), \
             patch('engine.geocoder.generate_address_hash', return_value='abc123'), \
             patch('engine.geocoder.lookup_by_pin', AsyncMock(return_value=pin_result)):
            mock_cache.get = AsyncMock(return_value=None)
            mock_cache.set = AsyncMock(return_value=True)

            result = await geocode({'pin': '12345'})

        assert result['match_tier'] == 1
        assert result['cached'] is False

    @pytest.mark.asyncio
    async def test_tier2_municipal_hit(self):
        from engine.geocoder import geocode, reset_stats

        reset_stats()
        muni_result = {
            'latitude': 43.65,
            'longitude': -79.38,
            'match_tier': 2,
            'match_method': 'postal_number',
            'confidence': 0.95,
            'matched_address': '100 Queen St',
            'source': 'municipal_toronto',
            'property_id': 'TOR-123',
            'assessment_roll_number': None,
            'pin': None,
            'gpid': None
        }

        with patch('engine.geocoder.cache') as mock_cache, \
             patch('engine.geocoder.parse_address', return_value={'street_name': 'Queen'}), \
             patch('engine.geocoder.generate_address_hash', return_value='abc123'), \
             patch('engine.geocoder.lookup_by_pin', AsyncMock(return_value=None)), \
             patch('engine.geocoder.match_municipal_data', AsyncMock(return_value=muni_result)):
            mock_cache.get = AsyncMock(return_value=None)
            mock_cache.set = AsyncMock(return_value=True)

            result = await geocode({'street_name': 'Queen'})

        assert result['match_tier'] == 2

    @pytest.mark.asyncio
    async def test_tier3_nar_hit(self):
        from engine.geocoder import geocode, reset_stats

        reset_stats()
        nar_result = {
            'latitude': 43.65,
            'longitude': -79.38,
            'match_tier': 3,
            'match_method': 'nar_postal',
            'confidence': 0.80,
            'matched_address': '100 Queen St',
            'source': 'nar',
            'property_id': None,
            'assessment_roll_number': None,
            'pin': None,
            'gpid': None
        }

        with patch('engine.geocoder.cache') as mock_cache, \
             patch('engine.geocoder.parse_address', return_value={'street_name': 'Queen'}), \
             patch('engine.geocoder.generate_address_hash', return_value='abc123'), \
             patch('engine.geocoder.lookup_by_pin', AsyncMock(return_value=None)), \
             patch('engine.geocoder.match_municipal_data', AsyncMock(return_value=None)), \
             patch('engine.geocoder.match_nar_data', AsyncMock(return_value=nar_result)):
            mock_cache.get = AsyncMock(return_value=None)
            mock_cache.set = AsyncMock(return_value=True)

            result = await geocode({'street_name': 'Queen'})

        assert result['match_tier'] == 3

    @pytest.mark.asyncio
    async def test_no_match(self):
        from engine.geocoder import geocode, reset_stats

        reset_stats()

        with patch('engine.geocoder.cache') as mock_cache, \
             patch('engine.geocoder.parse_address', return_value={'street_name': 'Nonexistent'}), \
             patch('engine.geocoder.generate_address_hash', return_value='abc123'), \
             patch('engine.geocoder.lookup_by_pin', AsyncMock(return_value=None)), \
             patch('engine.geocoder.match_municipal_data', AsyncMock(return_value=None)), \
             patch('engine.geocoder.match_nar_data', AsyncMock(return_value=None)):
            mock_cache.get = AsyncMock(return_value=None)
            mock_cache.set = AsyncMock(return_value=True)
            mock_cache.set_negative = AsyncMock(return_value=True)

            result = await geocode({'street_name': 'Nonexistent'})

        assert result['success'] is False
        assert 'No match found' in result['error']

    @pytest.mark.asyncio
    async def test_caches_successful_result(self):
        from engine.geocoder import geocode, reset_stats

        reset_stats()
        pin_result = {
            'latitude': 43.65,
            'longitude': -79.38,
            'match_tier': 1,
            'match_method': 'pin_lookup',
            'confidence': 1.0,
            'matched_address': '100 Queen',
            'source': 'municipal_toronto',
            'property_id': 'TOR-123',
            'assessment_roll_number': None,
            'pin': 12345,
            'gpid': None
        }

        with patch('engine.geocoder.cache') as mock_cache, \
             patch('engine.geocoder.parse_address', return_value={'pin': '12345'}), \
             patch('engine.geocoder.generate_address_hash', return_value='abc123'), \
             patch('engine.geocoder.lookup_by_pin', AsyncMock(return_value=pin_result)):
            mock_cache.get = AsyncMock(return_value=None)
            mock_cache.set = AsyncMock(return_value=True)

            await geocode({'pin': '12345'})

        mock_cache.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_exception_handling(self):
        from engine.geocoder import geocode, reset_stats

        reset_stats()

        with patch('engine.geocoder.cache') as mock_cache, \
             patch('engine.geocoder.parse_address', side_effect=ValueError("bad input")):
            mock_cache.get = AsyncMock(return_value=None)

            result = await geocode("bad")

        assert result['success'] is False
        # Internal error detail must NOT leak to API clients — only a
        # generic message is returned (the full error goes to the log)
        assert 'bad input' not in result['error']
        assert result['error'] == 'Internal geocoding error'

    @pytest.mark.asyncio
    async def test_stats_updated_on_success(self):
        from engine.geocoder import geocode, get_stats, reset_stats

        reset_stats()
        pin_result = {
            'latitude': 43.65,
            'longitude': -79.38,
            'match_tier': 1,
            'match_method': 'pin_lookup',
            'confidence': 1.0,
            'matched_address': '100 Queen',
            'source': 'municipal_toronto',
            'property_id': 'TOR-123',
            'assessment_roll_number': None,
            'pin': 12345,
            'gpid': None
        }

        with patch('engine.geocoder.cache') as mock_cache, \
             patch('engine.geocoder.parse_address', return_value={'pin': '12345'}), \
             patch('engine.geocoder.generate_address_hash', return_value='abc123'), \
             patch('engine.geocoder.lookup_by_pin', AsyncMock(return_value=pin_result)):
            mock_cache.get = AsyncMock(return_value=None)
            mock_cache.set = AsyncMock(return_value=True)

            await geocode({'pin': '12345'})

        stats = get_stats()
        assert stats['total_requests'] == 1
        assert stats['successful'] == 1
        assert stats['tier_distribution']['tier_1_pin'] == 1


class TestGeocodeBatch:
    """Tests for geocode_batch()."""

    @pytest.mark.asyncio
    async def test_batch_processes_all(self):
        from engine.geocoder import geocode_batch

        result_template = {
            'latitude': 43.65,
            'longitude': -79.38,
            'success': True,
            'match_tier': 2,
            'match_method': 'postal_number',
            'confidence': 0.95,
            'matched_address': '100 Queen',
            'source': 'municipal_toronto',
            'property_id': 'TOR-123',
            'assessment_roll_number': None,
            'pin': None,
            'gpid': None,
            'cached': False,
            'response_time_ms': 1.0,
            'confidence_factors': {'base_confidence': 0.95, 'tier': 2, 'match_method': 'postal_number'}
        }

        with patch('engine.geocoder.geocode', AsyncMock(return_value=result_template)):
            result = await geocode_batch([{'street_name': 'Queen'}, {'street_name': 'King'}])

        assert result['summary']['total'] == 2
        assert len(result['results']) == 2

    @pytest.mark.asyncio
    async def test_batch_handles_exceptions(self):
        from engine.geocoder import geocode_batch

        async def mock_geocode(addr):
            if addr.get('fail'):
                raise ValueError("test error")
            return {
                'success': True,
                'latitude': 43.65,
                'longitude': -79.38
            }

        with patch('engine.geocoder.geocode', side_effect=mock_geocode):
            result = await geocode_batch([
                {'street_name': 'Queen'},
                {'fail': True}
            ])

        assert result['summary']['total'] == 2
        # One succeeded, one failed
        failed = [r for r in result['results'] if r.get('success') is False]
        assert len(failed) == 1
        assert 'test error' in failed[0]['error']

    @pytest.mark.asyncio
    async def test_batch_summary_counts(self):
        from engine.geocoder import geocode_batch

        success = {'success': True, 'latitude': 43.65, 'longitude': -79.38, 'cached': False}
        cached = {'success': True, 'latitude': 43.65, 'longitude': -79.38, 'cached': True}
        fail = {'success': False, 'error': 'not found', 'cached': False}

        results = [success, cached, fail]
        call_count = 0

        async def mock_geocode(addr):
            nonlocal call_count
            r = results[call_count].copy()
            call_count += 1
            return r

        with patch('engine.geocoder.geocode', side_effect=mock_geocode):
            result = await geocode_batch([{}, {}, {}])

        summary = result['summary']
        assert summary['total'] == 3
        assert summary['successful'] == 2
        assert summary['failed'] == 1
        assert summary['cache_hits'] == 1
