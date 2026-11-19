"""Regressions for the minimal pre-deployment security fixes (no live services)."""
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.responses import Response

from api.main import app
from api.middleware.auth import AuthMiddleware
from api.middleware.ip_filter import IPFilterMiddleware
from api.middleware.rate_limiter import RateLimitMiddleware
from api.middleware.request_limits import (
    BodySizeLimitMiddleware, RequestTargetMiddleware, valid_host,
)


@pytest.mark.parametrize('host', ['audit.invalid/health?', 'audit.invalid#x',
                                  'user@audit.invalid', 'audit.invalid\\health',
                                  'audit.invalid%2fhealth', 'audit.invalid:bad'])
@pytest.mark.parametrize('path', ['/v1/stats', '/stats', '/v1/geocode', '/geocode',
                                  '/v1/admin/keys', '/admin/keys'])
async def test_malformed_host_rejected_on_actual_app(host, path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url='http://audit.invalid') as client:
        response = await client.get(path, headers={'Host': host})
    assert response.status_code == 400
    assert response.json()['error']['code'] == 'invalid_request_target'
    assert 'x-request-id' in response.headers


@pytest.mark.parametrize('path', ['/v1/stats', '/stats', '/v1/geocode', '/geocode',
                                  '/v1/admin/keys', '/admin/keys'])
async def test_normal_host_still_requires_authentication(path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url='http://audit.invalid') as client:
        response = await client.get(path)
    assert response.status_code == 401


@pytest.mark.parametrize('host', ['localhost:3000', 'api.example.com', '127.0.0.1:13000',
                                  '[::1]:3000', 'geocoder-api', 'example.com.'])
def test_valid_authorities(host):
    assert valid_host(host)


@pytest.mark.parametrize('headers,path', [
    ([], '/v1/stats'),
    ([(b'host', b'good'), (b'host', b'bad')], '/v1/stats'),
    ([(b'host', b'good')], '@other/health'),
])
async def test_invalid_asgi_request_target_never_calls_app(headers, path):
    downstream = AsyncMock()
    send = AsyncMock()
    await RequestTargetMiddleware(downstream)(
        {'type': 'http', 'path': path, 'headers': headers}, AsyncMock(), send)
    downstream.assert_not_awaited()
    assert send.call_args_list[0].args[0]['status'] == 400


async def test_security_uses_router_path_even_with_poisoned_url(sample_api_key):
    request = Request({'type': 'http', 'path': '/v1/stats', 'method': 'GET',
                       'headers': [(b'host', b'audit.invalid/health?')],
                       'state': {'api_key': sample_api_key}})
    # Defense in depth: bypass the outer guard and exercise middleware directly.
    auth = AuthMiddleware(AsyncMock(), excluded_paths=['/health'])
    downstream = AsyncMock(return_value=Response())
    assert not auth._check_permissions(request, sample_api_key)  # no stats permission
    response = await auth.dispatch(request, downstream)
    assert response.status_code == 401
    downstream.assert_not_awaited()

    request.state.api_key = {**sample_api_key, 'allowed_ips': ['192.0.2.1']}
    request.scope['client'] = ('198.51.100.1', 1234)
    ip_filter = IPFilterMiddleware(AsyncMock(), excluded_paths=['/health'])
    response = await ip_filter.dispatch(request, downstream)
    assert response.status_code == 403

    limiter = RateLimitMiddleware(AsyncMock(), excluded_paths=['/health'])
    limiter._check_rate_limit = AsyncMock(return_value=(False, 100, 9999999999))
    response = await limiter.dispatch(request, downstream)
    assert response.status_code == 429


@pytest.mark.parametrize('path', ['/admin/keys', '/v1/admin/keys', '/v1/admin/keys/42'])
async def test_admin_routes_share_strict_bucket(path, sample_api_key):
    from api.middleware import rate_limiter
    request = Request({'type': 'http', 'path': path, 'method': 'GET', 'headers': [],
                       'state': {'api_key': {**sample_api_key, 'rate_limit_override': 9999}}})
    limiter = RateLimitMiddleware(AsyncMock())
    limiter._check_rate_limit = AsyncMock(return_value=(True, 1, 9999999999))
    response = await limiter.dispatch(request, AsyncMock(return_value=Response()))
    limiter._check_rate_limit.assert_awaited_once_with(
        'rate_limit:1:admin', rate_limiter.ADMIN_RATE_LIMIT_REQUESTS,
        rate_limiter.ADMIN_RATE_LIMIT_WINDOW)
    assert response.headers['X-RateLimit-Limit'] == str(rate_limiter.ADMIN_RATE_LIMIT_REQUESTS)


@pytest.mark.parametrize('size,declared,expected', [
    (8, None, 200), (9, None, 413), (9, '1', 413), (8, '8', 200),
    (0, '9', 413), (0, 'oops', 400), (0, '-1', 400), (0, '+1', 400),
])
async def test_stream_limit_with_real_json_parser(size, declared, expected):
    test_app = FastAPI()
    @test_app.post('/body')
    async def body(request: Request):
        return await request.json()
    test_app.add_middleware(BodySizeLimitMiddleware, max_size=8)

    async def stream():
        payload = b'{}' + b' ' * max(0, size - 2) if size else b''
        for offset in range(0, len(payload), 3):
            yield payload[offset:offset + 3]
    headers = {'content-type': 'application/json'}
    if declared is not None:
        headers['content-length'] = declared
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=test_app),
                                base_url='http://audit.invalid') as client:
        response = await client.post('/body', content=stream(), headers=headers)
    assert response.status_code == expected
    if expected == 413:
        assert response.json()['error']['code'] == 'request_too_large'
    elif expected == 200:
        assert response.json() == {}


async def test_body_limit_stops_reading_at_overflow():
    downstream, send = AsyncMock(), AsyncMock()
    receive = AsyncMock(side_effect=[
        {'type': 'http.request', 'body': b'12345', 'more_body': True},
        {'type': 'http.request', 'body': b'67890', 'more_body': True},
        AssertionError('must not consume the rest of an oversized body'),
    ])
    await BodySizeLimitMiddleware(downstream, 8)(
        {'type': 'http', 'headers': []}, receive, send)
    assert receive.await_count == 2
    downstream.assert_not_awaited()
    assert send.call_args_list[0].args[0]['status'] == 413


async def test_disconnect_during_upload_does_not_invoke_app():
    downstream = AsyncMock()
    await BodySizeLimitMiddleware(downstream, 8)(
        {'type': 'http', 'headers': []},
        AsyncMock(return_value={'type': 'http.disconnect'}), AsyncMock())
    downstream.assert_not_awaited()


async def test_duplicate_content_length_rejected():
    downstream, send = AsyncMock(), AsyncMock()
    await BodySizeLimitMiddleware(downstream, 8)(
        {'type': 'http', 'headers': [(b'content-length', b'1'), (b'content-length', b'2')]},
        AsyncMock(), send)
    downstream.assert_not_awaited()
    assert send.call_args_list[0].args[0]['status'] == 400
