"""
Rate limiting middleware using Redis for distributed rate limiting.
Implements sliding window algorithm per API key.
"""
import os
import secrets
import time
import logging
from typing import Optional, Dict, Any
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from engine.cache import get_raw_client

logger = logging.getLogger('geocoder.rate_limiter')

# Configuration
RATE_LIMIT_ENABLED = os.getenv('RATE_LIMIT_ENABLED', 'true').lower() == 'true'
RATE_LIMIT_REQUESTS = int(os.getenv('RATE_LIMIT_REQUESTS', 100))  # requests per window
RATE_LIMIT_WINDOW = int(os.getenv('RATE_LIMIT_WINDOW', 60))  # window in seconds
RATE_LIMIT_BURST = int(os.getenv('RATE_LIMIT_BURST', 20))  # max burst requests
# Fail-closed: deny requests when Redis is unavailable (more secure)
RATE_LIMIT_FAIL_CLOSED = os.getenv('RATE_LIMIT_FAIL_CLOSED', 'true').lower() == 'true'
# Admin endpoint rate limiting (stricter to prevent brute-force)
ADMIN_RATE_LIMIT_REQUESTS = int(os.getenv('ADMIN_RATE_LIMIT_REQUESTS', 30))  # requests per window
ADMIN_RATE_LIMIT_WINDOW = int(os.getenv('ADMIN_RATE_LIMIT_WINDOW', 60))  # window in seconds


def _error_content(code: str, message: str, details=None) -> dict:
    """
    Build the standard error envelope: {"error": {"code", "message"[, "details"]}}.

    Matches api.main.error_content exactly, but is duplicated here rather
    than imported: api.main imports this module while constructing the
    FastAPI app, so `from .main import error_content` would be circular.
    """
    error = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"error": error}


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Middleware to enforce rate limits per API key using sliding window.
    """

    def __init__(self, app, excluded_paths: Optional[list] = None):
        """
        Initialize rate limiter.

        Args:
            app: FastAPI application
            excluded_paths: List of paths to exclude from rate limiting
        """
        super().__init__(app)
        self.excluded_paths = excluded_paths or []

    async def dispatch(self, request: Request, call_next):
        """
        Check rate limits before processing request.

        Args:
            request: Incoming request
            call_next: Next middleware/route handler

        Returns:
            Response
        """
        # Check if rate limiting is enabled
        if not RATE_LIMIT_ENABLED:
            return await call_next(request)

        # Check if path is excluded
        if self._is_excluded(request.scope["path"]):
            return await call_next(request)

        # Get API key from request state (set by auth middleware)
        api_key = getattr(request.state, 'api_key', None)

        if not api_key:
            # No API key, skip rate limiting (auth middleware will handle)
            return await call_next(request)

        # Check if this is an admin endpoint
        is_admin_endpoint = request.scope["path"].rstrip('/').startswith(('/admin/', '/v1/admin/'))

        # Get rate limit for this key (use admin limits for admin endpoints)
        if is_admin_endpoint:
            rate_limit = ADMIN_RATE_LIMIT_REQUESTS
            window = ADMIN_RATE_LIMIT_WINDOW
        else:
            rate_limit = self._get_rate_limit(api_key)
            window = RATE_LIMIT_WINDOW

        # Build rate limit key (separate buckets for admin endpoints)
        key_id = api_key.get('id', 'unknown')
        rate_key = f"rate_limit:{key_id}:admin" if is_admin_endpoint else f"rate_limit:{key_id}"

        # Check rate limit
        is_allowed, current_count, reset_time = await self._check_rate_limit(
            rate_key, rate_limit, window
        )

        if not is_allowed:
            retry_after = max(1, int(reset_time - time.time()))
            # Note: exceptions raised inside BaseHTTPMiddleware.dispatch bypass
            # FastAPI's exception handlers, so return a response directly.
            logger.warning(
                f'Rate limit exceeded (key id={key_id}): {request.method} {request.scope["path"]}'
            )
            return JSONResponse(
                status_code=429,
                content=_error_content(
                    'rate_limit_exceeded',
                    f'Rate limit exceeded. Max {rate_limit} requests per {window} seconds.',
                    details={'retry_after': retry_after}
                ),
                headers={
                    'Retry-After': str(retry_after),
                    'X-RateLimit-Limit': str(rate_limit),
                    'X-RateLimit-Remaining': '0',
                    'X-RateLimit-Reset': str(int(reset_time))
                }
            )

        # Process request
        response = await call_next(request)

        # Add rate limit headers to response
        remaining = max(0, rate_limit - current_count)
        response.headers['X-RateLimit-Limit'] = str(rate_limit)
        response.headers['X-RateLimit-Remaining'] = str(remaining)
        response.headers['X-RateLimit-Reset'] = str(int(reset_time))

        return response

    def _is_excluded(self, path: str) -> bool:
        """
        Check if path is excluded from rate limiting.

        Args:
            path: Request path

        Returns:
            True if excluded
        """
        clean_path = path.rstrip('/')

        for excluded in self.excluded_paths:
            excluded_clean = excluded.rstrip('/')

            if clean_path == excluded_clean:
                return True

            if excluded_clean.endswith('*'):
                prefix = excluded_clean[:-1]
                if clean_path.startswith(prefix):
                    return True

        return False

    def _get_rate_limit(self, api_key: Dict[str, Any]) -> int:
        """
        Get the rate limit for an API key.

        Args:
            api_key: API key data

        Returns:
            Rate limit (requests per window)
        """
        # Check for per-key override
        override = api_key.get('rate_limit_override')
        if override and override > 0:
            return override

        return RATE_LIMIT_REQUESTS

    async def _check_rate_limit(
        self,
        key: str,
        limit: int,
        window: int
    ) -> tuple[bool, int, float]:
        """
        Check if request is within rate limits using sliding window.

        Args:
            key: Rate limit key
            limit: Maximum requests
            window: Window size in seconds

        Returns:
            Tuple of (is_allowed, current_count, window_reset_time)
        """
        now = time.time()
        window_start = now - window
        window_reset = now + window

        # Get Redis client directly for atomic operations
        client = get_raw_client()

        if client is None:
            # Redis not available
            if RATE_LIMIT_FAIL_CLOSED:
                # Fail-closed: deny request when Redis unavailable (more secure)
                logger.warning("Rate limiting unavailable (Redis down). Denying request (fail-closed mode).")
                return False, 0, window_reset
            else:
                # Fail-open: allow request when Redis unavailable (better availability)
                logger.warning("Rate limiting unavailable (Redis down). Allowing request (fail-open mode).")
                return True, 0, window_reset

        try:
            # Use sorted set for sliding window
            # Key: timestamp, Score: timestamp, Member: unique request ID
            request_id = f"{now}:{secrets.token_hex(4)}"

            # Pipeline for atomic operations
            async with client.pipeline() as pipe:
                # Remove old entries outside window
                pipe.zremrangebyscore(key, 0, window_start)

                # Count current entries in window
                pipe.zcard(key)

                # Add new entry
                pipe.zadd(key, {request_id: now})

                # Set expiry on the key
                pipe.expire(key, window * 2)

                results = await pipe.execute()

            current_count = results[1]  # zcard result

            # Check if we're over limit (after adding)
            if current_count >= limit:
                # Get oldest entry to calculate reset time
                oldest = await client.zrange(key, 0, 0, withscores=True)
                if oldest:
                    oldest_time = oldest[0][1]
                    window_reset = oldest_time + window

                return False, current_count, window_reset

            return True, current_count + 1, window_reset

        except Exception as error:
            logger.error(f'Rate limit check error: {error}')
            if RATE_LIMIT_FAIL_CLOSED:
                return False, 0, window_reset
            return True, 0, window_reset


async def get_rate_limit_status(api_key_id: int) -> Dict[str, Any]:
    """
    Get current rate limit status for an API key.

    Args:
        api_key_id: API key database ID

    Returns:
        Rate limit status dict
    """
    client = get_raw_client()

    if client is None:
        return {
            'available': False,
            'message': 'Rate limiting service unavailable'
        }

    try:
        key = f"rate_limit:{api_key_id}"
        window = RATE_LIMIT_WINDOW
        window_start = time.time() - window

        # Count entries in current window
        count = await client.zcount(key, window_start, '+inf')

        return {
            'available': True,
            'current_requests': count,
            'limit': RATE_LIMIT_REQUESTS,
            'window_seconds': window,
            'remaining': max(0, RATE_LIMIT_REQUESTS - count)
        }

    except Exception as error:
        return {
            'available': False,
            'message': str(error)
        }
