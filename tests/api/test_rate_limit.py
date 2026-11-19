"""
Tests for rate limiting middleware.
"""
import json
import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch

from api.middleware.rate_limiter import RateLimitMiddleware, get_rate_limit_status


class TestRateLimitMiddleware:
    """Tests for RateLimitMiddleware class."""

    @pytest.fixture
    def middleware(self):
        """Create middleware instance."""
        app = MagicMock()
        return RateLimitMiddleware(app, excluded_paths=['/health'])

    def test_is_excluded(self, middleware):
        """Test path exclusion."""
        assert middleware._is_excluded('/health') is True
        assert middleware._is_excluded('/geocode') is False

    def test_get_rate_limit_default(self, middleware, sample_api_key):
        """Test default rate limit."""
        limit = middleware._get_rate_limit(sample_api_key)
        # Default is 100 (from environment variable default)
        assert limit == 100

    def test_get_rate_limit_override(self, middleware, admin_api_key):
        """Test rate limit override from API key."""
        limit = middleware._get_rate_limit(admin_api_key)
        assert limit == 1000  # admin_api_key has override of 1000

    @pytest.mark.asyncio
    async def test_dispatch_no_api_key(self, middleware, mock_request):
        """Test that requests without API key skip rate limiting."""
        mock_request.state.api_key = None
        call_next = AsyncMock(return_value=MagicMock())

        await middleware.dispatch(mock_request, call_next)

        call_next.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_excluded_path(self, middleware, mock_request, sample_api_key):
        """Test that excluded paths skip rate limiting."""
        mock_request.scope["path"] = '/health'
        mock_request.state.api_key = sample_api_key
        call_next = AsyncMock(return_value=MagicMock())

        await middleware.dispatch(mock_request, call_next)

        call_next.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_allowed(self, middleware, mock_request, sample_api_key):
        """Test request within rate limit."""
        mock_request.scope["path"] = '/geocode'
        mock_request.state.api_key = sample_api_key

        mock_response = MagicMock()
        mock_response.headers = {}
        call_next = AsyncMock(return_value=mock_response)

        # Mock _check_rate_limit to allow request
        with patch('api.middleware.rate_limiter.RATE_LIMIT_ENABLED', True), \
             patch.object(
                middleware, '_check_rate_limit',
                AsyncMock(return_value=(True, 5, time.time() + 60))
             ):
            response = await middleware.dispatch(mock_request, call_next)

        call_next.assert_called_once()
        assert 'X-RateLimit-Limit' in response.headers
        assert 'X-RateLimit-Remaining' in response.headers

    @pytest.mark.asyncio
    async def test_dispatch_rate_limited(self, middleware, mock_request, sample_api_key):
        """Test request exceeding rate limit."""
        mock_request.scope["path"] = '/geocode'
        mock_request.state.api_key = sample_api_key
        call_next = AsyncMock()

        # Mock _check_rate_limit to deny request
        with patch('api.middleware.rate_limiter.RATE_LIMIT_ENABLED', True), \
             patch.object(
                middleware, '_check_rate_limit',
                AsyncMock(return_value=(False, 100, time.time() + 30))
             ):
            response = await middleware.dispatch(mock_request, call_next)

        assert response.status_code == 429
        data = json.loads(response.body)
        assert data['error']['code'] == 'rate_limit_exceeded'
        assert 'message' in data['error']
        assert isinstance(data['error']['details']['retry_after'], int)
        assert data['error']['details']['retry_after'] >= 1
        assert 'Retry-After' in response.headers
        call_next.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_rate_limit_redis_unavailable(self, middleware):
        """Test rate limit check when Redis is unavailable (fail closed by default)."""
        import os
        # Store original value
        original_fail_closed = os.environ.get('RATE_LIMIT_FAIL_CLOSED')

        try:
            # Test fail-closed (default)
            os.environ['RATE_LIMIT_FAIL_CLOSED'] = 'true'
            from importlib import reload
            import api.middleware.rate_limiter as rl_module
            reload(rl_module)

            with patch('engine.cache._is_connected', False):
                is_allowed, count, reset = await middleware._check_rate_limit(
                    'test_key', 100, 60
                )

            # Should fail closed (deny) when Redis unavailable
            assert is_allowed is False

            # Test fail-open mode
            os.environ['RATE_LIMIT_FAIL_CLOSED'] = 'false'
            reload(rl_module)

            with patch('engine.cache._is_connected', False):
                is_allowed, count, reset = await middleware._check_rate_limit(
                    'test_key', 100, 60
                )

            # Should fail open (allow) when Redis unavailable
            assert is_allowed is True
        finally:
            # Restore original value
            if original_fail_closed is not None:
                os.environ['RATE_LIMIT_FAIL_CLOSED'] = original_fail_closed
            elif 'RATE_LIMIT_FAIL_CLOSED' in os.environ:
                del os.environ['RATE_LIMIT_FAIL_CLOSED']


class TestGetRateLimitStatus:
    """Tests for get_rate_limit_status helper."""

    @pytest.mark.asyncio
    async def test_status_redis_unavailable(self):
        """Test status when Redis unavailable."""
        with patch('engine.cache._is_connected', False):
            status = await get_rate_limit_status(1)

        assert status['available'] is False

    @pytest.mark.asyncio
    async def test_status_success(self):
        """Test successful status retrieval."""
        mock_client = MagicMock()
        mock_client.zcount = AsyncMock(return_value=50)

        with patch('engine.cache._is_connected', True), \
             patch('engine.cache._client', mock_client):
            status = await get_rate_limit_status(1)

        assert status['available'] is True
        assert status['current_requests'] == 50
        assert status['remaining'] == 50  # 100 - 50
