"""
Tests for authentication middleware.
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException

from api.middleware.auth import AuthMiddleware, get_current_api_key, require_admin


class TestAuthMiddleware:
    """Tests for AuthMiddleware class."""

    @pytest.fixture
    def middleware(self):
        """Create middleware instance."""
        app = MagicMock()
        return AuthMiddleware(app, excluded_paths=['/health', '/docs/*'])

    def test_is_excluded_exact_match(self, middleware):
        """Test exact path exclusion."""
        assert middleware._is_excluded('/health') is True
        assert middleware._is_excluded('/geocode') is False

    def test_is_excluded_wildcard(self, middleware):
        """Test wildcard path exclusion."""
        assert middleware._is_excluded('/docs/openapi.json') is True
        assert middleware._is_excluded('/docs/') is True
        assert middleware._is_excluded('/api/docs') is False

    def test_is_excluded_trailing_slash(self, middleware):
        """Test trailing slash handling."""
        assert middleware._is_excluded('/health/') is True
        assert middleware._is_excluded('/health') is True

    def test_check_permissions_geocode(self, middleware, sample_api_key, mock_request):
        """Test permission check for geocode endpoint."""
        mock_request.url.path = '/geocode'
        assert middleware._check_permissions(mock_request, sample_api_key) is True

    def test_check_permissions_batch(self, middleware, sample_api_key, mock_request):
        """Test permission check for batch endpoint."""
        mock_request.url.path = '/geocode/batch'
        assert middleware._check_permissions(mock_request, sample_api_key) is True

    def test_check_permissions_stats_denied(self, middleware, sample_api_key, mock_request):
        """Test permission denied for stats endpoint."""
        mock_request.url.path = '/stats'
        # sample_api_key has stats: False
        assert middleware._check_permissions(mock_request, sample_api_key) is False

    def test_check_permissions_admin_denied(self, middleware, sample_api_key, mock_request):
        """Test permission denied for admin endpoint."""
        mock_request.url.path = '/admin/keys'
        # sample_api_key has admin: False
        assert middleware._check_permissions(mock_request, sample_api_key) is False

    def test_check_permissions_admin_allowed(self, middleware, admin_api_key, mock_request):
        """Test permission allowed for admin with admin key."""
        mock_request.url.path = '/admin/keys'
        assert middleware._check_permissions(mock_request, admin_api_key) is True

    @pytest.mark.asyncio
    async def test_dispatch_excluded_path(self, middleware, mock_request):
        """Test that excluded paths skip authentication."""
        mock_request.url.path = '/health'
        call_next = AsyncMock(return_value=MagicMock())

        await middleware.dispatch(mock_request, call_next)

        assert mock_request.state.api_key is None
        call_next.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_missing_api_key(self, middleware, mock_request):
        """Test that missing API key returns 401."""
        mock_request.url.path = '/geocode'
        mock_request.headers = {}
        call_next = AsyncMock()

        response = await middleware.dispatch(mock_request, call_next)

        assert response.status_code == 401
        data = json.loads(response.body)
        assert data['error']['code'] == 'missing_api_key'
        assert 'message' in data['error']
        call_next.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_invalid_api_key(self, middleware, mock_request, mock_database, mock_cache):
        """Test that invalid API key returns 401."""
        mock_request.url.path = '/geocode'
        mock_request.headers = {'X-API-Key': 'invalid_key'}
        call_next = AsyncMock()

        with patch('api.middleware.auth.api_keys.validate_key', AsyncMock(return_value=None)):
            response = await middleware.dispatch(mock_request, call_next)

        assert response.status_code == 401
        data = json.loads(response.body)
        assert data['error']['code'] == 'invalid_api_key'
        assert 'message' in data['error']
        call_next.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_valid_api_key(self, middleware, mock_request, sample_api_key):
        """Test successful authentication with valid API key."""
        mock_request.url.path = '/geocode'
        mock_request.headers = {'X-API-Key': 'gc_valid_key'}
        call_next = AsyncMock(return_value=MagicMock())

        with patch('api.middleware.auth.api_keys.validate_key', AsyncMock(return_value=sample_api_key)):
            await middleware.dispatch(mock_request, call_next)

        assert mock_request.state.api_key == sample_api_key
        call_next.assert_called_once()


class TestRequireAdmin:
    """Tests for require_admin dependency."""

    @pytest.mark.asyncio
    async def test_require_admin_no_key(self, mock_request):
        """Test require_admin with no API key."""
        mock_request.state.api_key = None

        with pytest.raises(HTTPException) as exc_info:
            await require_admin(mock_request)

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_require_admin_not_admin(self, mock_request, sample_api_key):
        """Test require_admin with non-admin key."""
        mock_request.state.api_key = sample_api_key

        with pytest.raises(HTTPException) as exc_info:
            await require_admin(mock_request)

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_require_admin_is_admin(self, mock_request, admin_api_key):
        """Test require_admin with admin key."""
        mock_request.state.api_key = admin_api_key

        result = await require_admin(mock_request)

        assert result is True


class TestGetCurrentApiKey:
    """Tests for get_current_api_key helper."""

    def test_get_current_api_key_exists(self, mock_request, sample_api_key):
        """Test getting API key from request state."""
        mock_request.state.api_key = sample_api_key

        result = get_current_api_key(mock_request)

        assert result == sample_api_key

    def test_get_current_api_key_none(self, mock_request):
        """Test getting API key when none set."""
        mock_request.state.api_key = None

        result = get_current_api_key(mock_request)

        assert result is None
