"""
Shared pytest configuration and fixtures (API security tests + markers).
"""
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Set test environment variables before imports
os.environ['API_KEY_REQUIRED'] = 'true'
os.environ['RATE_LIMIT_ENABLED'] = 'true'
os.environ['IP_FILTER_ENABLED'] = 'true'
os.environ['DB_HOST'] = 'localhost'
os.environ['REDIS_URL'] = 'redis://localhost:6379'


def pytest_configure(config):
    """Register custom markers (kept here so pyproject.toml stays untouched)."""
    config.addinivalue_line(
        "markers",
        "integration: needs live services (Postgres via DB_* env vars). "
        "Skipped by default; run with `pytest -m integration` — connection "
        "errors then FAIL instead of skipping.",
    )


def pytest_collection_modifyitems(config, items):
    """Skip integration tests unless a mark expression was given explicitly.

    A plain `pytest` run stays green with no infrastructure; passing any
    `-m` expression (e.g. `-m integration`) bypasses this and lets the
    tests run — and fail loudly — as written.
    """
    if config.getoption("-m"):
        return

    skip_integration = pytest.mark.skip(
        reason="integration test (needs live DB) — run with `pytest -m integration`"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)


@pytest.fixture
def mock_database():
    """Mock database service."""
    with patch('engine.database') as mock_db:
        mock_db.query = AsyncMock(return_value=[])
        mock_db.execute = AsyncMock(return_value='OK')
        yield mock_db


@pytest.fixture
def mock_cache():
    """Mock cache service."""
    with patch('engine.cache') as mock_cache:
        mock_cache.get = AsyncMock(return_value=None)
        mock_cache.set = AsyncMock(return_value=True)
        mock_cache.delete = AsyncMock(return_value=True)
        mock_cache._is_connected = True
        mock_cache._client = MagicMock()
        yield mock_cache


@pytest.fixture
def sample_api_key():
    """Sample API key data."""
    return {
        'id': 1,
        'key_prefix': 'gc_test12345',
        'name': 'Test Key',
        'owner': 'test@example.com',
        'created_at': '2024-01-01T00:00:00',
        'expires_at': None,
        'last_used_at': None,
        'is_active': True,
        'rate_limit_override': None,
        'allowed_ips': [],
        'permissions': {
            'geocode': True,
            'batch': True,
            'stats': False,
            'admin': False
        }
    }


@pytest.fixture
def admin_api_key():
    """Sample admin API key data."""
    return {
        'id': 2,
        'key_prefix': 'gc_admin1234',
        'name': 'Admin Key',
        'owner': 'admin@example.com',
        'created_at': '2024-01-01T00:00:00',
        'expires_at': None,
        'last_used_at': None,
        'is_active': True,
        'rate_limit_override': 1000,
        'allowed_ips': ['192.168.1.0/24', '10.0.0.1'],
        'permissions': {
            'geocode': True,
            'batch': True,
            'stats': True,
            'admin': True
        }
    }


@pytest.fixture
def mock_request():
    """Create a mock FastAPI request."""
    request = MagicMock()
    request.url.path = '/geocode'
    request.method = 'POST'
    request.headers = {}
    request.client = MagicMock()
    request.client.host = '127.0.0.1'
    request.state = MagicMock()
    return request
