"""Validate HTTP routing inputs and bound request bodies before parsing."""
import ipaddress
import re

from starlette.datastructures import Headers
from starlette.responses import JSONResponse


def error_response(status, code, message):
    return JSONResponse(status_code=status, content={
        'error': {'code': code, 'message': message}})


def valid_host(host):
    # Accept DNS/IPv4 authorities and bracketed IPv6, with an optional port.
    # Never allow URL delimiters, credentials, whitespace or percent escapes.
    match = re.fullmatch(r'(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)(?::([0-9]{1,5}))?', host)
    if not match:
        return False
    name, port = match.groups()
    if port is not None and not 0 < int(port) <= 65535:
        return False
    if name.startswith('['):
        try:
            ipaddress.IPv6Address(name[1:-1])
        except ValueError:
            return False
    return True


class RequestTargetMiddleware:
    """Reject malformed authorities before any URL-based framework behavior."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        hosts = Headers(scope=scope).getlist('host')
        path = scope.get('path', '')
        if len(hosts) != 1 or not valid_host(hosts[0]) or not path.startswith('/'):
            return await error_response(400, 'invalid_request_target',
                                        'Invalid request host or path')(scope, receive, send)
        await self.app(scope, receive, send)


class BodySizeLimitMiddleware:
    """Buffer at most max_size body bytes, then replay them to the application.

    Checking before invoking the app prevents a downstream JSON parser from
    swallowing a receive exception and replacing the intended 413 with a 400.
    """

    def __init__(self, app, max_size=10 * 1024 * 1024):
        self.app = app
        self.max_size = max_size

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        lengths = Headers(scope=scope).getlist('content-length')
        if lengths and (len(lengths) != 1 or not re.fullmatch(r'[0-9]+', lengths[0])
                        or len(lengths[0]) > 20):
            return await error_response(400, 'invalid_content_length',
                                        'Invalid Content-Length')(scope, receive, send)
        if lengths and int(lengths[0]) > self.max_size:
            return await error_response(413, 'request_too_large',
                                        'Request body too large')(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            if len(body) + len(chunk) > self.max_size:
                return await error_response(413, 'request_too_large',
                                            'Request body too large')(scope, receive, send)
            body.extend(chunk)
            if not message.get('more_body', False):
                break

        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
            return await receive()

        await self.app(scope, replay, send)
