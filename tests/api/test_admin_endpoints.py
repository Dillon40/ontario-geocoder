"""
Integration tests for admin API endpoints.
Tests API key management: create, list, get, update, revoke.
"""
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx


os.environ['DB_PASSWORD'] = 'test_password'
os.environ['ALLOWED_ORIGINS'] = '*'


@pytest.fixture
async def admin_client(admin_api_key):
    """Create async test client with auth enabled and admin key mocked."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def noop_lifespan(app):
        yield

    with patch('api.middleware.auth.API_KEY_REQUIRED', True), \
         patch('api.middleware.rate_limiter.RATE_LIMIT_ENABLED', False), \
         patch('api.middleware.ip_filter.IP_FILTER_ENABLED', False), \
         patch('api.services.api_keys.validate_key', AsyncMock(return_value=admin_api_key)):
        from api.main import app
        app.router.lifespan_context = noop_lifespan

        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


class TestAdminEndpoints:
    """Test admin API key management endpoints."""

    @pytest.mark.asyncio
    async def test_create_api_key_success(self, admin_client):
        """Test creating a new API key."""
        created_key = {
            'id': 3,
            'key': 'gc_newkey123456789012345678901234567890',
            'key_prefix': 'gc_newkey1234',
            'name': 'New Client Key',
            'owner': 'client@example.com',
            'created_at': '2024-01-15T10:00:00',
            'expires_at': None,
            'rate_limit_override': 200,
            'allowed_ips': ['10.0.0.1'],
            'permissions': {'geocode': True, 'batch': True, 'stats': False, 'admin': False},
            'is_active': True
        }

        with patch('api.services.api_keys.create_key', AsyncMock(return_value=created_key)), \
             patch('api.services.api_keys.log_admin_action', AsyncMock()):
            response = await admin_client.post(
                '/admin/keys',
                json={
                    'name': 'New Client Key',
                    'owner': 'client@example.com',
                    'rate_limit_override': 200,
                    'allowed_ips': ['10.0.0.1'],
                    'permissions': {'geocode': True, 'batch': True, 'stats': False, 'admin': False}
                },
                headers={'X-API-Key': 'gc_admin_test_key'}
            )

        assert response.status_code == 200
        data = response.json()
        assert data['success'] is True
        assert data['data']['key'] == created_key['key']
        assert 'Store the key securely' in data['message']

    @pytest.mark.asyncio
    async def test_create_api_key_invalid_ip(self, admin_client):
        """Test creating a key with invalid IP address."""
        response = await admin_client.post(
            '/admin/keys',
            json={
                'name': 'Invalid IP Key',
                'allowed_ips': ['invalid-ip-address']
            },
            headers={'X-API-Key': 'gc_admin_test_key'}
        )

        assert response.status_code == 400
        data = response.json()
        assert data['error']['code'] == 'invalid_ip_list'

    @pytest.mark.asyncio
    async def test_create_api_key_without_admin_permission(self, admin_client, sample_api_key):
        """Test that non-admin keys cannot create new keys."""
        with patch('api.services.api_keys.validate_key', AsyncMock(return_value=sample_api_key)):
            response = await admin_client.post(
                '/admin/keys',
                json={'name': 'Should Fail'},
                headers={'X-API-Key': 'gc_regular_key'}
            )

        # 403 from AuthMiddleware permission check
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_list_api_keys(self, admin_client):
        """Test listing all API keys."""
        keys_list = [
            {'id': 1, 'key_prefix': 'gc_key1', 'name': 'Key 1', 'is_active': True},
            {'id': 2, 'key_prefix': 'gc_key2', 'name': 'Key 2', 'is_active': True}
        ]

        with patch('api.services.api_keys.list_keys', AsyncMock(return_value=keys_list)):
            response = await admin_client.get(
                '/admin/keys',
                headers={'X-API-Key': 'gc_admin_test_key'}
            )

        assert response.status_code == 200
        data = response.json()
        assert data['success'] is True
        assert data['count'] == 2
        assert len(data['data']) == 2

    @pytest.mark.asyncio
    async def test_list_api_keys_with_pagination(self, admin_client):
        """Test listing keys with pagination parameters."""
        with patch('api.services.api_keys.list_keys', AsyncMock(return_value=[])) as mock_list:
            response = await admin_client.get(
                '/admin/keys?limit=10&offset=20&include_inactive=true',
                headers={'X-API-Key': 'gc_admin_test_key'}
            )

        assert response.status_code == 200
        mock_list.assert_called_once_with(
            include_inactive=True,
            limit=10,
            offset=20
        )

    @pytest.mark.asyncio
    async def test_get_api_key_by_id(self, admin_client):
        """Test getting a specific API key by ID."""
        key_data = {
            'id': 5,
            'key_prefix': 'gc_target',
            'name': 'Target Key',
            'owner': 'owner@example.com',
            'is_active': True,
            'permissions': {'geocode': True, 'batch': False}
        }
        usage_stats = {
            'total_requests': 1000,
            'total_geocodes': 5000,
            'avg_response_time_ms': 45.5,
            'period_days': 30
        }

        with patch('api.services.api_keys.get_key_by_id', AsyncMock(return_value=key_data)), \
             patch('api.services.api_keys.get_usage_stats', AsyncMock(return_value=usage_stats)):
            response = await admin_client.get(
                '/admin/keys/5',
                headers={'X-API-Key': 'gc_admin_test_key'}
            )

        assert response.status_code == 200
        data = response.json()
        assert data['success'] is True
        assert data['data']['id'] == 5
        assert data['data']['usage_stats']['total_requests'] == 1000

    @pytest.mark.asyncio
    async def test_get_api_key_not_found(self, admin_client):
        """Test getting a non-existent API key."""
        with patch('api.services.api_keys.get_key_by_id', AsyncMock(return_value=None)):
            response = await admin_client.get(
                '/admin/keys/999',
                headers={'X-API-Key': 'gc_admin_test_key'}
            )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_update_api_key(self, admin_client):
        """Test updating an API key's settings."""
        updated_key = {
            'id': 5,
            'key_prefix': 'gc_updated',
            'name': 'Updated Name',
            'rate_limit_override': 500,
            'is_active': True
        }

        with patch('api.services.api_keys.update_key', AsyncMock(return_value=updated_key)), \
             patch('api.services.api_keys.log_admin_action', AsyncMock()):
            response = await admin_client.patch(
                '/admin/keys/5',
                json={
                    'name': 'Updated Name',
                    'rate_limit_override': 500
                },
                headers={'X-API-Key': 'gc_admin_test_key'}
            )

        assert response.status_code == 200
        data = response.json()
        assert data['success'] is True
        assert data['data']['name'] == 'Updated Name'
        assert data['message'] == 'API key updated successfully'

    @pytest.mark.asyncio
    async def test_update_api_key_invalid_ip(self, admin_client):
        """Test updating a key with invalid IP address."""
        response = await admin_client.patch(
            '/admin/keys/5',
            json={'allowed_ips': ['not-an-ip']},
            headers={'X-API-Key': 'gc_admin_test_key'}
        )

        assert response.status_code == 400
        data = response.json()
        assert data['error']['code'] == 'invalid_ip_list'

    @pytest.mark.asyncio
    async def test_update_api_key_not_found(self, admin_client):
        """Test updating a non-existent API key."""
        with patch('api.services.api_keys.update_key', AsyncMock(return_value=None)):
            response = await admin_client.patch(
                '/admin/keys/999',
                json={'name': 'Should Fail'},
                headers={'X-API-Key': 'gc_admin_test_key'}
            )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_revoke_api_key(self, admin_client):
        """Test revoking an API key."""
        with patch('api.services.api_keys.revoke_key', AsyncMock(return_value=True)), \
             patch('api.services.api_keys.log_admin_action', AsyncMock()):
            response = await admin_client.delete(
                '/admin/keys/5',
                headers={'X-API-Key': 'gc_admin_test_key'}
            )

        assert response.status_code == 200
        data = response.json()
        assert data['success'] is True
        assert 'revoked' in data['message']

    @pytest.mark.asyncio
    async def test_revoke_api_key_not_found(self, admin_client):
        """Test revoking a non-existent API key."""
        with patch('api.services.api_keys.revoke_key', AsyncMock(return_value=False)):
            response = await admin_client.delete(
                '/admin/keys/999',
                headers={'X-API-Key': 'gc_admin_test_key'}
            )

        assert response.status_code == 404
        data = response.json()
        assert data['error']['code'] == 'key_not_found'

    @pytest.mark.asyncio
    async def test_get_api_key_not_found_error_envelope(self, admin_client):
        """404s from admin routes use the standard {"error": {code, message}} envelope."""
        with patch('api.services.api_keys.get_key_by_id', AsyncMock(return_value=None)):
            response = await admin_client.get(
                '/admin/keys/999',
                headers={'X-API-Key': 'gc_admin_test_key'}
            )

        assert response.status_code == 404
        data = response.json()
        assert set(data.keys()) == {'error'}
        assert set(data['error'].keys()) >= {'code', 'message'}
        assert data['error']['code'] == 'key_not_found'

    @pytest.mark.asyncio
    async def test_admin_v1_prefix_and_unversioned_alias_agree(self, admin_client):
        """/v1/admin/keys is canonical; unversioned /admin/keys remains a
        working deprecated alias."""
        keys_list = [{'id': 1, 'key_prefix': 'gc_key1', 'name': 'Key 1', 'is_active': True}]

        with patch('api.services.api_keys.list_keys', AsyncMock(return_value=keys_list)):
            versioned = await admin_client.get(
                '/v1/admin/keys', headers={'X-API-Key': 'gc_admin_test_key'}
            )
        with patch('api.services.api_keys.list_keys', AsyncMock(return_value=keys_list)):
            unversioned = await admin_client.get(
                '/admin/keys', headers={'X-API-Key': 'gc_admin_test_key'}
            )

        assert versioned.status_code == unversioned.status_code == 200
        assert versioned.json()['data'] == unversioned.json()['data']


class TestAdminRateLimiting:
    """Test admin endpoint rate limiting."""

    def test_admin_rate_limit_config_exists(self):
        """Test that admin rate limit configuration is loaded."""
        from api.middleware.rate_limiter import (
            ADMIN_RATE_LIMIT_REQUESTS,
            ADMIN_RATE_LIMIT_WINDOW
        )

        # Default values from environment (30 requests per 60-second window)
        assert ADMIN_RATE_LIMIT_REQUESTS == 30
        assert ADMIN_RATE_LIMIT_WINDOW == 60


class TestAuditLogging:
    """Test admin audit logging functionality."""

    @pytest.mark.asyncio
    async def test_log_admin_action(self):
        """Test that admin actions are logged."""
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value='OK')

        with patch('api.services.api_keys.database', mock_db):
            from api.services.api_keys import log_admin_action

            await log_admin_action(
                admin_key_id=1,
                admin_key_name='Admin Key',
                action='create_key',
                target_type='api_key',
                target_id=5,
                target_name='New Key',
                details={'owner': 'test@example.com'},
                client_ip='192.168.1.1',
                request_id='abc-123'
            )

        # Verify database was called
        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args
        assert 'INSERT INTO admin_audit_log' in call_args[0][0]

    @pytest.mark.asyncio
    async def test_log_admin_action_handles_errors(self):
        """Test that audit logging errors don't crash the application."""
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(side_effect=Exception('DB Error'))

        with patch('api.services.api_keys.database', mock_db):
            from api.services.api_keys import log_admin_action

            # Should not raise - errors are logged but not propagated
            await log_admin_action(
                admin_key_id=1,
                admin_key_name='Admin Key',
                action='create_key'
            )
        # Test passes if no exception is raised
