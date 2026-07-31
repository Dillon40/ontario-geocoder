"""
Functional endpoint tests using httpx.AsyncClient with ASGITransport.
Tests full middleware + route stack with mocked backends.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx


@pytest.fixture
async def client():
    """Create async test client with disabled auth/rate-limit/ip-filter."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def noop_lifespan(app):
        yield

    with patch('api.middleware.auth.API_KEY_REQUIRED', False), \
         patch('api.middleware.rate_limiter.RATE_LIMIT_ENABLED', False), \
         patch('api.middleware.ip_filter.IP_FILTER_ENABLED', False):
        from api.main import app
        app.router.lifespan_context = noop_lifespan

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


class TestHealthEndpoint:
    """Tests for GET /health."""

    @pytest.mark.asyncio
    async def test_health_returns_200(self, client):
        """Health endpoint should return 200 when services are up."""
        with patch('api.main.database') as mock_db, \
             patch('api.main.cache') as mock_cache:
            mock_db.test_connection = AsyncMock(return_value=True)
            mock_cache.test_connection = AsyncMock(return_value=True)

            response = await client.get('/health')

        assert response.status_code == 200
        data = response.json()
        assert data['status'] == 'healthy'

    @pytest.mark.asyncio
    async def test_health_unhealthy(self, client):
        """Health endpoint should return 503 when services are down."""
        with patch('api.main.database') as mock_db, \
             patch('api.main.cache') as mock_cache:
            mock_db.test_connection = AsyncMock(return_value=False)
            mock_cache.test_connection = AsyncMock(return_value=False)

            response = await client.get('/health')

        assert response.status_code == 503
        data = response.json()
        assert data['status'] == 'unhealthy'

    @pytest.mark.asyncio
    async def test_health_v1_prefix_and_unversioned_alias_agree(self, client):
        """/v1/health is canonical; unversioned /health remains a working
        deprecated alias (and both are exempt from the auth middleware)."""
        with patch('api.main.database') as mock_db, \
             patch('api.main.cache') as mock_cache:
            mock_db.test_connection = AsyncMock(return_value=True)
            mock_cache.test_connection = AsyncMock(return_value=True)

            versioned = await client.get('/v1/health')
            unversioned = await client.get('/health')

        assert versioned.status_code == unversioned.status_code == 200
        assert versioned.json()['status'] == unversioned.json()['status'] == 'healthy'


class TestErrorEnvelope:
    """Every error response — 4xx or 5xx, from any route — uses the single
    {"error": {"code", "message"}} envelope."""

    @pytest.mark.asyncio
    async def test_unmatched_route_404_uses_envelope(self, client):
        """A path that matches no route still gets the standard envelope,
        not FastAPI/Starlette's default {"detail": "Not Found"}."""
        response = await client.get('/this/route/does/not/exist')

        assert response.status_code == 404
        data = response.json()
        assert set(data.keys()) == {'error'}
        assert set(data['error'].keys()) >= {'code', 'message'}

    @pytest.mark.asyncio
    async def test_internal_error_uses_envelope(self, client):
        """An unexpected exception from the geocoding engine is normalized
        to a generic internal_error, never leaking exception internals."""
        with patch('api.main.geocoder') as mock_geocoder:
            mock_geocoder.geocode = AsyncMock(side_effect=RuntimeError('boom'))

            response = await client.post('/geocode', json={'street_name': 'Queen'})

        assert response.status_code == 500
        data = response.json()
        assert data['error']['code'] == 'internal_error'
        assert 'boom' not in data['error']['message']


class TestRootEndpoint:
    """Tests for GET /."""

    @pytest.mark.asyncio
    async def test_root_returns_service_info(self, client):
        """Root endpoint should return service information."""
        response = await client.get('/')

        assert response.status_code == 200
        data = response.json()
        assert data['service'] == 'Property Geocoder Microservice'
        assert 'version' in data
        assert 'endpoints' in data
        assert 'tiers' in data


class TestGeocodeEndpoint:
    """Tests for POST /geocode."""

    @pytest.mark.asyncio
    async def test_geocode_success(self, client):
        """Geocode should return 200 with valid result."""
        mock_result = {
            'success': True,
            'latitude': 43.65,
            'longitude': -79.38,
            'matched_address': '100 Queen St W, Toronto',
            'source': 'municipal_toronto',
            'confidence': 0.95,
            'match_tier': 2,
            'match_method': 'postal_number',
            'cached': False,
            'response_time_ms': 5.0
        }

        with patch('api.main.geocoder') as mock_geocoder, \
             patch('api.main.license_service') as mock_license:
            mock_geocoder.geocode = AsyncMock(return_value=mock_result)
            mock_license.get_license_info = MagicMock(return_value=None)

            response = await client.post('/geocode', json={
                'street_number': '100',
                'street_name': 'Queen',
                'city': 'Toronto'
            })

        assert response.status_code == 200
        data = response.json()
        assert data['latitude'] == 43.65

    @pytest.mark.asyncio
    async def test_geocode_includes_attribution(self, client):
        """A successful match from a licensed source surfaces the required
        attribution statement both nested in license_info and as the
        top-level 'attribution' field."""
        mock_result = {
            'success': True,
            'latitude': 43.65,
            'longitude': -79.38,
            'matched_address': '100 Queen St W, Toronto',
            'source': 'municipal_toronto',
        }
        mock_license_info = {
            'license_type': 'Open Government Licence - Toronto',
            'license_url': 'https://open.toronto.ca/open-data-licence/',
            'commercial_use_allowed': True,
            'required_attribution': (
                'Contains information licensed under the Open Government '
                'Licence – Toronto.'
            ),
        }

        with patch('api.main.geocoder') as mock_geocoder, \
             patch('api.main.license_service') as mock_license:
            mock_geocoder.geocode = AsyncMock(return_value=mock_result)
            mock_license.get_license_info = MagicMock(return_value=mock_license_info)

            response = await client.post('/geocode', json={
                'street_number': '100',
                'street_name': 'Queen',
                'city': 'Toronto'
            })

        assert response.status_code == 200
        data = response.json()
        assert data['attribution'] == mock_license_info['required_attribution']
        assert data['license_info']['required_attribution'] == mock_license_info['required_attribution']

    @pytest.mark.asyncio
    async def test_geocode_not_found(self, client):
        """Geocode should return 404 when no match."""
        mock_result = {
            'success': False,
            'error': 'No match found in any data source',
            'cached': False,
            'response_time_ms': 10.0
        }

        with patch('api.main.geocoder') as mock_geocoder, \
             patch('api.main.license_service') as mock_license:
            mock_geocoder.geocode = AsyncMock(return_value=mock_result)
            mock_license.get_license_info = MagicMock(return_value=None)

            response = await client.post('/geocode', json={
                'street_name': 'Nonexistent'
            })

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_geocode_invalid_json(self, client):
        """Malformed JSON body fails FastAPI's own request parsing/validation,
        which returns 422 (not the route's own 400 "address_required" check —
        that only runs once a body has successfully parsed to an empty dict)."""
        response = await client.post(
            '/geocode',
            content=b'not json',
            headers={'content-type': 'application/json'}
        )

        assert response.status_code == 422
        data = response.json()
        assert data['error']['code'] == 'validation_error'

    @pytest.mark.asyncio
    async def test_geocode_empty_body_400(self, client):
        """An empty (but validly-parsed) address body hits the route's own
        400 "address_required" check, distinct from the 422 validation case."""
        with patch('api.main.geocoder') as mock_geocoder:
            response = await client.post('/geocode', json={})

        mock_geocoder.geocode.assert_not_called()
        assert response.status_code == 400
        data = response.json()
        assert data['error']['code'] == 'address_required'

    @pytest.mark.asyncio
    async def test_geocode_not_found_error_envelope(self, client):
        """404 (no match) uses the standard error envelope."""
        mock_result = {'success': False, 'error': 'No match found in any data source'}

        with patch('api.main.geocoder') as mock_geocoder, \
             patch('api.main.license_service') as mock_license:
            mock_geocoder.geocode = AsyncMock(return_value=mock_result)
            mock_license.get_license_info = MagicMock(return_value=None)

            response = await client.post('/geocode', json={'street_name': 'Nonexistent'})

        assert response.status_code == 404
        data = response.json()
        assert set(data.keys()) == {'error'}
        assert set(data['error'].keys()) >= {'code', 'message'}
        assert data['error']['code'] == 'address_not_found'

    @pytest.mark.asyncio
    async def test_geocode_v1_prefix_and_unversioned_alias_agree(self, client):
        """/v1/geocode is canonical; the unversioned /geocode alias must
        behave identically for the existing IDX site consumer."""
        mock_result = {
            'success': True,
            'latitude': 43.65,
            'longitude': -79.38,
            'matched_address': '100 Queen St W, Toronto',
            'source': 'municipal_toronto',
        }

        with patch('api.main.geocoder') as mock_geocoder, \
             patch('api.main.license_service') as mock_license:
            mock_geocoder.geocode = AsyncMock(return_value=mock_result)
            mock_license.get_license_info = MagicMock(return_value=None)

            body = {'street_number': '100', 'street_name': 'Queen', 'city': 'Toronto'}
            versioned = await client.post('/v1/geocode', json=body)
            unversioned = await client.post('/geocode', json=body)

        assert versioned.status_code == 200
        assert unversioned.status_code == 200
        assert versioned.json()['latitude'] == unversioned.json()['latitude'] == 43.65


class TestBatchGeocodeEndpoint:
    """Tests for POST /geocode/batch."""

    @pytest.mark.asyncio
    async def test_batch_success(self, client):
        """Batch geocode should return results for all addresses."""
        mock_result = {
            'results': [
                {'success': True, 'latitude': 43.65, 'longitude': -79.38, 'source': None},
                {'success': True, 'latitude': 43.70, 'longitude': -79.40, 'source': None}
            ],
            'summary': {
                'total': 2,
                'successful': 2,
                'failed': 0,
                'cache_hits': 0
            }
        }

        with patch('api.main.geocoder') as mock_geocoder, \
             patch('api.main.license_service') as mock_license:
            mock_geocoder.geocode_batch = AsyncMock(return_value=mock_result)
            mock_license.get_license_info = MagicMock(return_value=None)

            response = await client.post('/geocode/batch', json={
                'addresses': [
                    {'street_name': 'Queen'},
                    {'street_name': 'King'}
                ]
            })

        assert response.status_code == 200
        data = response.json()
        assert data['summary']['total'] == 2

    @pytest.mark.asyncio
    async def test_batch_empty_list(self, client):
        """Batch geocode should reject an empty address list with a 422
        RequestValidationError (BatchGeocodeRequest.addresses has
        min_length=1) using the standard error envelope."""
        response = await client.post('/geocode/batch', json={
            'addresses': []
        })

        assert response.status_code == 422
        data = response.json()
        assert data['error']['code'] == 'validation_error'

    @pytest.mark.asyncio
    async def test_batch_v1_prefix_and_unversioned_alias_agree(self, client):
        """/v1/geocode/batch is canonical; unversioned /geocode/batch is a
        deprecated alias that must keep working for existing consumers."""
        mock_result = {
            'results': [{'success': True, 'latitude': 43.65, 'longitude': -79.38, 'source': None}],
            'summary': {'total': 1, 'successful': 1, 'failed': 0, 'cache_hits': 0}
        }

        with patch('api.main.geocoder') as mock_geocoder, \
             patch('api.main.license_service') as mock_license:
            mock_geocoder.geocode_batch = AsyncMock(return_value=mock_result)
            mock_license.get_license_info = MagicMock(return_value=None)

            body = {'addresses': [{'street_name': 'Queen'}]}
            versioned = await client.post('/v1/geocode/batch', json=body)
            unversioned = await client.post('/geocode/batch', json=body)

        assert versioned.status_code == 200
        assert unversioned.status_code == 200
        assert versioned.json()['summary'] == unversioned.json()['summary']


class TestAttributionsEndpoint:
    """Tests for GET /v1/attributions."""

    @pytest.mark.asyncio
    async def test_attributions_returns_active_sources(self, client):
        """Lists required attribution statements for active data sources."""
        mock_attributions = [
            {
                'source_key': 'nar_ontario',
                'source_name': 'Statistics Canada - National Address Register (Ontario)',
                'required_attribution': 'Contains information licensed under the Open Government Licence – Canada.',
                'license_type': 'Statistics Canada Open Licence',
                'license_url': 'https://www.statcan.gc.ca/en/reference/licence',
            },
            {
                'source_key': 'toronto_addresses',
                'source_name': 'City of Toronto - Address Points (MTOR)',
                'required_attribution': 'Contains information licensed under the Open Government Licence – Toronto.',
                'license_type': 'Open Government Licence - Toronto',
                'license_url': 'https://open.toronto.ca/open-data-licence/',
            },
        ]

        with patch('api.main.license_service') as mock_license:
            mock_license.get_all_attributions = MagicMock(return_value=mock_attributions)

            response = await client.get('/v1/attributions')

        assert response.status_code == 200
        data = response.json()
        assert data['attributions'] == mock_attributions

    @pytest.mark.asyncio
    async def test_attributions_v1_prefix_and_unversioned_alias_agree(self, client):
        """/v1/attributions is canonical; unversioned /attributions is a
        deprecated alias that must keep working."""
        mock_attributions = [{
            'source_key': 'nar_ontario',
            'source_name': 'Statistics Canada - National Address Register (Ontario)',
            'required_attribution': 'Contains information licensed under the Open Government Licence – Canada.',
            'license_type': 'Statistics Canada Open Licence',
            'license_url': 'https://www.statcan.gc.ca/en/reference/licence',
        }]

        with patch('api.main.license_service') as mock_license:
            mock_license.get_all_attributions = MagicMock(return_value=mock_attributions)

            versioned = await client.get('/v1/attributions')
            unversioned = await client.get('/attributions')

        assert versioned.status_code == 200
        assert unversioned.status_code == 200
        assert versioned.json() == unversioned.json()


class TestAuthIntegration:
    """Tests for auth middleware integration.

    Note: HTTPException from BaseHTTPMiddleware.dispatch is caught by
    Starlette's ServerErrorMiddleware as 500 in test environment, so we
    verify auth blocks requests (non-200) rather than exact status codes.
    """

    @pytest.mark.asyncio
    async def test_blocked_without_key(self):
        """Requests without API key should be rejected."""
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def noop_lifespan(app):
            yield

        with patch('api.middleware.auth.API_KEY_REQUIRED', True), \
             patch('api.middleware.rate_limiter.RATE_LIMIT_ENABLED', False), \
             patch('api.middleware.ip_filter.IP_FILTER_ENABLED', False):
            from api.main import app
            app.router.lifespan_context = noop_lifespan

            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                response = await ac.post('/geocode', json={'street_name': 'Queen'})

        # Auth middleware rejects unauthenticated requests with 401
        assert response.status_code == 401
        data = response.json()
        assert data['error']['code'] == 'missing_api_key'
        assert 'message' in data['error']

    @pytest.mark.asyncio
    async def test_200_with_valid_key(self):
        """Requests with valid API key should succeed."""
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def noop_lifespan(app):
            yield

        mock_result = {
            'success': True,
            'latitude': 43.65,
            'longitude': -79.38,
            'matched_address': '100 Queen St',
            'source': None,
            'cached': False,
            'response_time_ms': 1.0
        }

        with patch('api.middleware.auth.API_KEY_REQUIRED', True), \
             patch('api.middleware.rate_limiter.RATE_LIMIT_ENABLED', False), \
             patch('api.middleware.ip_filter.IP_FILTER_ENABLED', False), \
             patch('api.services.api_keys.validate_key', AsyncMock(return_value={
                 'id': 1,
                 'name': 'Test',
                 'permissions': {'geocode': True, 'batch': True, 'stats': False, 'admin': False}
             })), \
             patch('api.main.geocoder') as mock_geocoder, \
             patch('api.main.license_service') as mock_license:
            mock_geocoder.geocode = AsyncMock(return_value=mock_result)
            mock_license.get_license_info = MagicMock(return_value=None)

            from api.main import app
            app.router.lifespan_context = noop_lifespan

            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                response = await ac.post(
                    '/geocode',
                    json={'street_name': 'Queen'},
                    headers={'X-API-Key': 'gc_valid_test_key'}
                )

        assert response.status_code == 200
