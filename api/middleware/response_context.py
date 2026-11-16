"""Outermost response metadata, error containment and authenticated access logging."""
import logging
import uuid
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from api.config import security_config

logger = logging.getLogger('geocoder.api')

class ResponseContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        state = scope.setdefault('state', {})
        request_id = Headers(scope=scope).get(security_config.REQUEST_ID_HEADER) or str(uuid.uuid4())
        state['request_id'] = request_id
        started = False

        async def send_response(message):
            nonlocal started
            if message['type'] == 'http.response.start':
                started = True
                headers = MutableHeaders(scope=message)
                headers[security_config.REQUEST_ID_HEADER] = request_id
                if security_config.SECURE_HEADERS_ENABLED:
                    headers['X-Content-Type-Options'] = 'nosniff'
                    headers['X-Frame-Options'] = 'DENY'
                    headers['X-XSS-Protection'] = '1; mode=block'
                    headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
                key = state.get('api_key') or {}
                logger.info('[%s] %s %s - Key: %s - Status: %s', request_id,
                            scope['method'], scope['path'], key.get('name', 'unauthenticated'),
                            message['status'])
            await send(message)

        try:
            await self.app(scope, receive, send_response)
        except Exception:
            logger.exception('[%s] Unhandled request error', request_id)
            if started:
                raise
            response = JSONResponse(status_code=500, content={
                'error': {'code': 'internal_error', 'message': 'Internal server error'}})
            await response(scope, receive, send_response)
