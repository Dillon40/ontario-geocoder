"""
Middleware package for API security and request handling.

Request flow: Auth -> Rate Limit -> IP Filter -> Route Handler.
Each middleware validates and can short-circuit the request early (401/429/403)
before it reaches business logic, and attaches validated data (e.g.
request.state.api_key) for downstream handlers to reuse.
"""
from .auth import AuthMiddleware, get_current_api_key
from .rate_limiter import RateLimitMiddleware
from .ip_filter import IPFilterMiddleware

__all__ = [
    'AuthMiddleware',
    'get_current_api_key',
    'RateLimitMiddleware',
    'IPFilterMiddleware'
]
