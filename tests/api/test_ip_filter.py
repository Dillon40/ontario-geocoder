"""
Tests for IP filtering middleware.
"""
import ipaddress
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from api.middleware.ip_filter import (
    IPFilterMiddleware,
    validate_ip_list,
    get_client_ip
)

# When the direct peer (mock_request.client.host defaults to 127.0.0.1) is a
# trusted proxy, forwarding headers are honored. Patch this in for the
# "behind a trusted proxy" cases.
_TRUST_LOCALHOST = [ipaddress.ip_network('127.0.0.1/32')]


class TestIPFilterMiddleware:
    """Tests for IPFilterMiddleware class."""

    @pytest.fixture
    def middleware(self):
        """Create middleware instance."""
        app = MagicMock()
        return IPFilterMiddleware(app, excluded_paths=['/health'])

    def test_is_excluded(self, middleware):
        """Test path exclusion."""
        assert middleware._is_excluded('/health') is True
        assert middleware._is_excluded('/geocode') is False

    def test_get_client_ip_direct(self, middleware, mock_request):
        """Test getting client IP from direct connection."""
        mock_request.headers = {}
        mock_request.client.host = '192.168.1.100'

        ip = middleware._get_client_ip(mock_request)

        assert ip == '192.168.1.100'

    def test_get_client_ip_forwarded_untrusted_peer(self, middleware, mock_request):
        """X-Forwarded-For is ignored when the peer is not a trusted proxy."""
        mock_request.headers = {'X-Forwarded-For': '10.0.0.1, 192.168.1.1'}
        # Peer is 127.0.0.1 (fixture default), TRUSTED_PROXIES is empty.

        ip = middleware._get_client_ip(mock_request)

        assert ip == '127.0.0.1'  # spoofed header not honored

    def test_get_client_ip_forwarded_trusted_peer(self, middleware, mock_request):
        """X-Forwarded-For is honored when the peer IS a trusted proxy."""
        mock_request.headers = {'X-Forwarded-For': '10.0.0.1, 192.168.1.1'}

        with patch('api.middleware.ip_filter._TRUSTED_PROXY_NETWORKS', _TRUST_LOCALHOST):
            ip = middleware._get_client_ip(mock_request)

        assert ip == '10.0.0.1'  # first (client) IP in the chain

    def test_get_client_ip_real_ip_untrusted_peer(self, middleware, mock_request):
        """X-Real-IP is ignored when the peer is not a trusted proxy."""
        mock_request.headers = {'X-Real-IP': '172.16.0.50'}

        ip = middleware._get_client_ip(mock_request)

        assert ip == '127.0.0.1'

    def test_get_client_ip_real_ip_trusted_peer(self, middleware, mock_request):
        """X-Real-IP is honored when the peer IS a trusted proxy."""
        mock_request.headers = {'X-Real-IP': '172.16.0.50'}

        with patch('api.middleware.ip_filter._TRUSTED_PROXY_NETWORKS', _TRUST_LOCALHOST):
            ip = middleware._get_client_ip(mock_request)

        assert ip == '172.16.0.50'

    def test_is_ip_allowed_exact_match(self, middleware):
        """Test IP allowed with exact match."""
        allowed_ips = ['192.168.1.100', '10.0.0.1']

        assert middleware._is_ip_allowed('192.168.1.100', allowed_ips) is True
        assert middleware._is_ip_allowed('10.0.0.1', allowed_ips) is True
        assert middleware._is_ip_allowed('192.168.1.101', allowed_ips) is False

    def test_is_ip_allowed_cidr(self, middleware):
        """Test IP allowed with CIDR range."""
        allowed_ips = ['192.168.1.0/24', '10.0.0.0/8']

        assert middleware._is_ip_allowed('192.168.1.100', allowed_ips) is True
        assert middleware._is_ip_allowed('192.168.1.1', allowed_ips) is True
        assert middleware._is_ip_allowed('192.168.2.1', allowed_ips) is False
        assert middleware._is_ip_allowed('10.255.255.255', allowed_ips) is True

    def test_is_ip_allowed_ipv6(self, middleware):
        """Test IPv6 address handling."""
        allowed_ips = ['::1', '2001:db8::/32']

        assert middleware._is_ip_allowed('::1', allowed_ips) is True
        assert middleware._is_ip_allowed('2001:db8::1', allowed_ips) is True
        assert middleware._is_ip_allowed('2001:db9::1', allowed_ips) is False

    def test_is_ip_allowed_invalid_client(self, middleware):
        """Test invalid client IP returns False."""
        allowed_ips = ['192.168.1.0/24']

        assert middleware._is_ip_allowed('not-an-ip', allowed_ips) is False

    def test_is_ip_allowed_invalid_allowed(self, middleware):
        """Test invalid entry in allowed list is skipped."""
        allowed_ips = ['not-valid', '192.168.1.100']

        # Should still match the valid entry
        assert middleware._is_ip_allowed('192.168.1.100', allowed_ips) is True
        assert middleware._is_ip_allowed('192.168.1.101', allowed_ips) is False

    @pytest.mark.asyncio
    async def test_dispatch_no_api_key(self, middleware, mock_request):
        """Test that requests without API key skip IP filtering."""
        mock_request.state.api_key = None
        call_next = AsyncMock(return_value=MagicMock())

        await middleware.dispatch(mock_request, call_next)

        call_next.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_no_ip_restrictions(self, middleware, mock_request, sample_api_key):
        """Test that keys with no IP restrictions allow all."""
        mock_request.scope["path"] = '/geocode'
        mock_request.state.api_key = sample_api_key  # allowed_ips is []
        call_next = AsyncMock(return_value=MagicMock())

        await middleware.dispatch(mock_request, call_next)

        call_next.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_ip_allowed(self, middleware, mock_request, admin_api_key):
        """Test request from allowed IP."""
        mock_request.scope["path"] = '/geocode'
        mock_request.state.api_key = admin_api_key  # allowed_ips: ['192.168.1.0/24', '10.0.0.1']
        mock_request.headers = {}
        mock_request.client.host = '192.168.1.50'  # In allowed range
        call_next = AsyncMock(return_value=MagicMock())

        await middleware.dispatch(mock_request, call_next)

        call_next.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_ip_blocked(self, middleware, mock_request, admin_api_key):
        """Test request from blocked IP."""
        from unittest.mock import patch as _patch
        mock_request.scope["path"] = '/geocode'
        mock_request.state.api_key = admin_api_key
        mock_request.headers = {}
        mock_request.client.host = '172.16.0.1'  # Not in allowed list
        call_next = AsyncMock()

        with _patch('api.middleware.ip_filter.IP_FILTER_ENABLED', True):
            response = await middleware.dispatch(mock_request, call_next)

        assert response.status_code == 403
        data = json.loads(response.body)
        assert data['error']['code'] == 'ip_not_allowed'
        assert 'message' in data['error']
        call_next.assert_not_called()


class TestValidateIPList:
    """Tests for validate_ip_list helper."""

    def test_valid_ips(self):
        """Test validation of valid IP list."""
        ip_list = ['192.168.1.1', '10.0.0.0/8', '::1']

        is_valid, errors = validate_ip_list(ip_list)

        assert is_valid is True
        assert len(errors) == 0

    def test_invalid_ip(self):
        """Test validation with invalid IP."""
        ip_list = ['192.168.1.1', 'not-an-ip', '10.0.0.1']

        is_valid, errors = validate_ip_list(ip_list)

        assert is_valid is False
        assert len(errors) == 1
        assert 'not-an-ip' in errors[0]

    def test_invalid_cidr(self):
        """Test validation with invalid CIDR."""
        ip_list = ['192.168.1.0/33']  # Invalid prefix length

        is_valid, errors = validate_ip_list(ip_list)

        assert is_valid is False
        assert len(errors) == 1

    def test_empty_list(self):
        """Test validation of empty list."""
        is_valid, errors = validate_ip_list([])

        assert is_valid is True
        assert len(errors) == 0


class TestGetClientIP:
    """Tests for get_client_ip helper function."""

    def test_forwarded_for_untrusted_peer(self, mock_request):
        """X-Forwarded-For is ignored from an untrusted peer (anti-spoofing)."""
        mock_request.headers = {'X-Forwarded-For': '203.0.113.1, 198.51.100.1'}

        ip = get_client_ip(mock_request)

        assert ip == '127.0.0.1'  # falls back to the real socket peer

    def test_forwarded_for_trusted_peer(self, mock_request):
        """X-Forwarded-For is honored from a trusted proxy peer."""
        mock_request.headers = {'X-Forwarded-For': '203.0.113.1, 198.51.100.1'}

        with patch('api.middleware.ip_filter._TRUSTED_PROXY_NETWORKS', _TRUST_LOCALHOST):
            ip = get_client_ip(mock_request)

        assert ip == '203.0.113.1'

    def test_real_ip_untrusted_peer(self, mock_request):
        """X-Real-IP is ignored from an untrusted peer (anti-spoofing)."""
        mock_request.headers = {'X-Real-IP': '203.0.113.50'}

        ip = get_client_ip(mock_request)

        assert ip == '127.0.0.1'

    def test_real_ip_trusted_peer(self, mock_request):
        """X-Real-IP is honored from a trusted proxy peer."""
        mock_request.headers = {'X-Real-IP': '203.0.113.50'}

        with patch('api.middleware.ip_filter._TRUSTED_PROXY_NETWORKS', _TRUST_LOCALHOST):
            ip = get_client_ip(mock_request)

        assert ip == '203.0.113.50'

    def test_direct_client(self, mock_request):
        """Test direct client connection."""
        mock_request.headers = {}
        mock_request.client.host = '127.0.0.1'

        ip = get_client_ip(mock_request)

        assert ip == '127.0.0.1'

    def test_no_client(self, mock_request):
        """Test when client is None."""
        mock_request.headers = {}
        mock_request.client = None

        ip = get_client_ip(mock_request)

        assert ip is None
