"""
Authentication middleware for API key validation.
"""
import os
import logging
from typing import Optional, Dict, Any
from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from ..services import api_keys

logger = logging.getLogger('geocoder.auth')


# Configuration
API_KEY_HEADER = os.getenv('API_KEY_HEADER', 'X-API-Key')
API_KEY_REQUIRED = os.getenv('API_KEY_REQUIRED', 'true').lower() == 'true'


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


def get_current_api_key(request: Request) -> Optional[Dict[str, Any]]:
    """
    Get the validated API key data from request state.

    Args:
        request: FastAPI request object

    Returns:
        API key data or None
    """
    return getattr(request.state, 'api_key', None)


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Middleware to validate API keys on incoming requests.
    """

    def __init__(self, app, excluded_paths: Optional[list] = None):
        """
        Initialize auth middleware.

        Args:
            app: FastAPI application
            excluded_paths: List of paths to exclude from auth (e.g., ['/health'])
        """
        super().__init__(app)
        self.excluded_paths = excluded_paths or []

    async def dispatch(self, request: Request, call_next):
        """
        Process request and validate API key.

        Args:
            request: Incoming request
            call_next: Next middleware/route handler

        Returns:
            Response
        """
        # Check if path is excluded
        if self._is_excluded(request.url.path):
            request.state.api_key = None
            return await call_next(request)

        # Check if auth is required
        if not API_KEY_REQUIRED:
            request.state.api_key = None
            return await call_next(request)

        # Extract API key from header
        api_key = request.headers.get(API_KEY_HEADER)

        if not api_key:
            # Note: exceptions raised inside BaseHTTPMiddleware.dispatch bypass
            # FastAPI's exception handlers, so return a response directly.
            logger.warning(f'Auth rejected (missing API key): {request.method} {request.url.path}')
            return JSONResponse(
                status_code=401,
                content=_error_content(
                    'missing_api_key',
                    f'API key required. Provide key in {API_KEY_HEADER} header.'
                )
            )

        # Validate API key
        key_data = await api_keys.validate_key(api_key)

        if not key_data:
            logger.warning(f'Auth rejected (invalid API key): {request.method} {request.url.path}')
            return JSONResponse(
                status_code=401,
                content=_error_content(
                    'invalid_api_key',
                    'Invalid or expired API key.'
                )
            )

        # Record the validated identity even if permission is denied, so outer
        # access logging can distinguish a known key from unauthenticated traffic.
        request.state.api_key = key_data

        # Check permissions for this endpoint
        if not self._check_permissions(request, key_data):
            logger.warning(
                f'Auth rejected (insufficient permissions, key id={key_data.get("id")}): '
                f'{request.method} {request.url.path}'
            )
            return JSONResponse(
                status_code=403,
                content=_error_content(
                    'insufficient_permissions',
                    'API key does not have permission for this endpoint.'
                )
            )

        # Continue to next handler
        response = await call_next(request)

        return response

    def _is_excluded(self, path: str) -> bool:
        """
        Check if path is excluded from authentication.

        Args:
            path: Request path

        Returns:
            True if excluded
        """
        # Clean path (remove trailing slash)
        clean_path = path.rstrip('/')

        for excluded in self.excluded_paths:
            # Wildcard match (e.g., /admin/*)
            if excluded.endswith('*'):
                prefix = excluded[:-1]
                # Match both with and without trailing slash
                if clean_path.startswith(prefix.rstrip('/')):
                    return True
            else:
                # Exact match (compare without trailing slashes)
                excluded_clean = excluded.rstrip('/')
                if clean_path == excluded_clean:
                    return True

        return False

    def _check_permissions(self, request: Request, key_data: Dict[str, Any]) -> bool:
        """
        Check if API key has permission for the requested endpoint.

        Args:
            request: FastAPI request
            key_data: Validated API key data

        Returns:
            True if permitted
        """
        permissions = key_data.get('permissions', {})
        path = request.url.path.lower()

        # Map paths to required permissions
        if '/admin/' in path:
            return permissions.get('admin', False)

        # Fail closed: missing permissions deny (matching admin/stats behavior)
        if '/geocode/batch' in path:
            return permissions.get('batch', False)

        if '/geocode' in path:
            return permissions.get('geocode', False)

        if '/stats' in path:
            return permissions.get('stats', False)

        # Default allow for other endpoints
        return True


async def require_permission(request: Request, permission: str) -> bool:
    """
    Check if current request has a specific permission.
    Use as a dependency in route handlers.

    Args:
        request: FastAPI request
        permission: Required permission name

    Returns:
        True if permitted

    Raises:
        HTTPException: If permission denied
    """
    key_data = get_current_api_key(request)

    if not key_data:
        raise HTTPException(
            status_code=401,
            detail={
                'code': 'authentication_required',
                'message': 'Authentication required for this endpoint.'
            }
        )

    permissions = key_data.get('permissions', {})

    if not permissions.get(permission, False):
        raise HTTPException(
            status_code=403,
            detail={
                'code': 'insufficient_permissions',
                'message': f'Permission "{permission}" required for this endpoint.'
            }
        )

    return True


async def require_admin(request: Request) -> bool:
    """
    Require admin permission for endpoint.
    Use as a dependency.

    Args:
        request: FastAPI request

    Returns:
        True if admin

    Raises:
        HTTPException: If not admin
    """
    return await require_permission(request, 'admin')
