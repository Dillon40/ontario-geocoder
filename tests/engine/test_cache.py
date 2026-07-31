"""
Unit tests for engine/cache.py — Redis cache with mocked client.
"""
import json
import pytest
from unittest.mock import AsyncMock
import engine.cache as cache_module


@pytest.fixture(autouse=True)
def reset_cache_state():
    """Reset cache module state before each test."""
    cache_module._is_connected = False
    cache_module._client = None
    cache_module._last_reconnect_attempt = 0.0
    cache_module._stats = {
        'hits': 0,
        'misses': 0,
        'sets': 0,
        'errors': 0,
        'reconnections': 0
    }
    yield


@pytest.fixture
def connected_cache():
    """Set up a connected cache with a mock Redis client."""
    mock_client = AsyncMock()
    cache_module._client = mock_client
    cache_module._is_connected = True
    return mock_client


class TestCacheGet:
    """Tests for cache.get()."""

    @pytest.mark.asyncio
    async def test_cache_hit(self, connected_cache):
        """Test successful cache retrieval."""
        data = {'latitude': 43.65, 'longitude': -79.38}
        connected_cache.get = AsyncMock(return_value=json.dumps(data))

        result = await cache_module.get('test:key')

        assert result == data
        assert cache_module._stats['hits'] == 1

    @pytest.mark.asyncio
    async def test_cache_miss(self, connected_cache):
        """Test cache miss returns None."""
        connected_cache.get = AsyncMock(return_value=None)

        result = await cache_module.get('test:missing')

        assert result is None
        assert cache_module._stats['misses'] == 1

    @pytest.mark.asyncio
    async def test_disconnected_returns_none(self):
        """Test that disconnected cache returns None."""
        cache_module._is_connected = False
        cache_module._last_reconnect_attempt = 99999999999.0  # In cooldown

        result = await cache_module.get('test:key')

        assert result is None
        assert cache_module._stats['misses'] == 1

    @pytest.mark.asyncio
    async def test_error_marks_disconnected(self, connected_cache):
        """Test that errors mark cache as disconnected."""
        connected_cache.get = AsyncMock(side_effect=Exception("Redis error"))

        result = await cache_module.get('test:key')

        assert result is None
        assert cache_module._is_connected is False
        assert cache_module._stats['errors'] == 1


class TestCacheSet:
    """Tests for cache.set()."""

    @pytest.mark.asyncio
    async def test_set_success(self, connected_cache):
        """Test successful cache set."""
        connected_cache.setex = AsyncMock()

        result = await cache_module.set('test:key', {'lat': 43.65})

        assert result is True
        assert cache_module._stats['sets'] == 1
        connected_cache.setex.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_disconnected(self):
        """Test set when disconnected returns False."""
        cache_module._is_connected = False
        cache_module._last_reconnect_attempt = 99999999999.0

        result = await cache_module.set('test:key', {'lat': 43.65})

        assert result is False

    @pytest.mark.asyncio
    async def test_set_custom_ttl(self, connected_cache):
        """Test set with custom TTL."""
        connected_cache.setex = AsyncMock()

        await cache_module.set('test:key', {'lat': 43.65}, ttl=3600)

        args = connected_cache.setex.call_args[0]
        assert args[0] == 'test:key'
        assert args[1] == 3600

    @pytest.mark.asyncio
    async def test_set_error_marks_disconnected(self, connected_cache):
        """Test that set errors mark cache as disconnected."""
        connected_cache.setex = AsyncMock(side_effect=Exception("Redis error"))

        result = await cache_module.set('test:key', {'lat': 43.65})

        assert result is False
        assert cache_module._is_connected is False
        assert cache_module._stats['errors'] == 1


class TestCacheSetNegative:
    """Tests for cache.set_negative()."""

    @pytest.mark.asyncio
    async def test_set_negative(self, connected_cache):
        """Test negative cache entry."""
        connected_cache.setex = AsyncMock()

        result = await cache_module.set_negative('test:missing')

        assert result is True
        # Verify the stored value includes success=False
        call_args = connected_cache.setex.call_args[0]
        stored = json.loads(call_args[2])
        assert stored['success'] is False
        assert stored['cached'] is True
        # Verify negative TTL (shorter than default)
        assert call_args[1] == cache_module.NEGATIVE_CACHE_TTL_SECONDS


class TestCacheDelete:
    """Tests for cache.delete()."""

    @pytest.mark.asyncio
    async def test_delete_success(self, connected_cache):
        """Test successful cache deletion."""
        connected_cache.delete = AsyncMock()

        result = await cache_module.delete('test:key')

        assert result is True
        connected_cache.delete.assert_called_once_with('test:key')

    @pytest.mark.asyncio
    async def test_delete_disconnected(self):
        """Test delete when disconnected."""
        cache_module._is_connected = False
        cache_module._last_reconnect_attempt = 99999999999.0

        result = await cache_module.delete('test:key')

        assert result is False

    @pytest.mark.asyncio
    async def test_delete_error_marks_disconnected(self, connected_cache):
        """Test that delete errors mark cache as disconnected."""
        connected_cache.delete = AsyncMock(side_effect=Exception("Redis error"))

        result = await cache_module.delete('test:key')

        assert result is False
        assert cache_module._is_connected is False


class TestCacheGetStats:
    """Tests for cache.get_stats()."""

    def test_initial_stats(self):
        """Test initial stats are zeroed."""
        stats = cache_module.get_stats()
        assert stats['connected'] is False
        assert stats['hit_rate'] == 0
        assert stats['total_hits'] == 0
        assert stats['total_misses'] == 0

    def test_hit_rate_calculation(self):
        """Test hit rate is calculated correctly."""
        cache_module._stats['hits'] = 75
        cache_module._stats['misses'] = 25

        stats = cache_module.get_stats()
        assert stats['hit_rate'] == 0.75

    def test_zero_division_protection(self):
        """Test that zero total requests doesn't cause division error."""
        cache_module._stats['hits'] = 0
        cache_module._stats['misses'] = 0

        stats = cache_module.get_stats()
        assert stats['hit_rate'] == 0

    def test_connection_status_reflected(self):
        """Test that connected status is reflected in stats."""
        cache_module._is_connected = True
        stats = cache_module.get_stats()
        assert stats['connected'] is True

        cache_module._is_connected = False
        stats = cache_module.get_stats()
        assert stats['connected'] is False
