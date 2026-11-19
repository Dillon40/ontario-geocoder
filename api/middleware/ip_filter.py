"""
IP filtering middleware for API key-based IP whitelisting.
Supports both individual IPs and CIDR notation.
"""
import os
import logging
import ipaddress
from typing import Optional, List
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger('geocoder.ip_filter')

# Configuration
IP_FILTER_ENABLED = os.getenv('IP_FILTER_ENABLED', 'true').lower() == 'true'

# Trusted reverse proxies (comma-separated IPs or CIDR ranges).
# X-Forwarded-For / X-Real-IP headers are only honored when the direct
# socket peer is in this list. Default is empty: trust no proxy headers.
TRUSTED_PROXIES = os.getenv('TRUSTED_PROXIES', '')


def _parse_trusted_proxies(raw: str) -> list:
    """
    Parse the TRUSTED_PROXIES env var into a list of ip_network objects.
    Single IPs are treated as /32 (or /128) networks. Invalid entries
    are logged and skipped.

    Args:
        raw: Comma-separated IPs/CIDRs

    Returns:
        List of ip_network objects
    """
    networks = []
    for entry in raw.split(','):
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError as error:
            logger.warning(f"Ignoring invalid TRUSTED_PROXIES entry '{entry}': {error}")
    return networks


_TRUSTED_PROXY_NETWORKS = _parse_trusted_proxies(TRUSTED_PROXIES)


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


def _is_trusted_proxy(peer_ip: str) -> bool:
    """
    Check if the direct socket peer is a trusted reverse proxy.

    Args:
        peer_ip: IP address of the direct connection peer

    Returns:
        True if the peer is in the TRUSTED_PROXIES allowlist
    """
    if not _TRUSTED_PROXY_NETWORKS:
        return False

    try:
        addr = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False

    return any(addr in network for network in _TRUSTED_PROXY_NETWORKS)


class IPFilterMiddleware(BaseHTTPMiddleware):
    """
    Middleware to enforce IP restrictions based on API key's allowed_ips.
    """

    def __init__(self, app, excluded_paths: Optional[list] = None):
        """
        Initialize IP filter.

        Args:
            app: FastAPI application
            excluded_paths: List of paths to exclude from IP filtering
        """
        super().__init__(app)
        self.excluded_paths = excluded_paths or []

    async def dispatch(self, request: Request, call_next):
        """
        Check if client IP is allowed for the API key.

        Args:
            request: Incoming request
            call_next: Next middleware/route handler

        Returns:
            Response
        """
        # Check if IP filtering is enabled
        if not IP_FILTER_ENABLED:
            return await call_next(request)

        # Check if path is excluded
        if self._is_excluded(request.scope["path"]):
            return await call_next(request)

        # Get API key from request state (set by auth middleware)
        api_key = getattr(request.state, 'api_key', None)

        if not api_key:
            # No API key, skip IP filtering (auth middleware will handle)
            return await call_next(request)

        # Get allowed IPs for this key
        allowed_ips = api_key.get('allowed_ips', [])

        # If no IP restrictions, allow all
        if not allowed_ips:
            return await call_next(request)

        # Get client IP
        client_ip = self._get_client_ip(request)

        if not client_ip:
            # Note: exceptions raised inside BaseHTTPMiddleware.dispatch bypass
            # FastAPI's exception handlers, so return a response directly.
            logger.warning(f'IP filter rejected (no client IP): {request.method} {request.scope["path"]}')
            return JSONResponse(
                status_code=400,
                content=_error_content(
                    'invalid_client_ip',
                    'Could not determine client IP address.'
                )
            )

        # Check if IP is allowed
        if not self._is_ip_allowed(client_ip, allowed_ips):
            logger.warning(
                f'IP filter rejected ({client_ip} not allowed, key id={api_key.get("id")}): '
                f'{request.method} {request.scope["path"]}'
            )
            return JSONResponse(
                status_code=403,
                content=_error_content(
                    'ip_not_allowed',
                    f'IP address {client_ip} is not in the allowed list for this API key.'
                )
            )

        # Process request
        return await call_next(request)

    def _is_excluded(self, path: str) -> bool:
        """
        Check if path is excluded from IP filtering.

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

    def _get_client_ip(self, request: Request) -> Optional[str]:
        """
        Extract client IP from request, handling proxies.

        Args:
            request: FastAPI request

        Returns:
            Client IP string or None
        """
        return get_client_ip(request)

    def _is_ip_allowed(self, client_ip: str, allowed_ips: List[str]) -> bool:
        """
        Check if client IP matches any allowed IP or CIDR range.

        Args:
            client_ip: Client IP address string
            allowed_ips: List of allowed IPs or CIDR ranges

        Returns:
            True if allowed
        """
        try:
            client = ipaddress.ip_address(client_ip)
        except ValueError:
            # Invalid IP format
            return False

        for allowed in allowed_ips:
            try:
                # Check if it's a CIDR range
                if '/' in allowed:
                    network = ipaddress.ip_network(allowed, strict=False)
                    if client in network:
                        return True
                else:
                    # Single IP address
                    if client == ipaddress.ip_address(allowed):
                        return True
            except ValueError:
                # Invalid entry in allowed list, skip
                continue

        return False


def validate_ip_list(ip_list: List[str]) -> tuple[bool, List[str]]:
    """
    Validate a list of IP addresses and CIDR ranges.

    Args:
        ip_list: List of IP strings

    Returns:
        Tuple of (is_valid, list of errors)
    """
    errors = []

    for ip in ip_list:
        try:
            if '/' in ip:
                ipaddress.ip_network(ip, strict=False)
            else:
                ipaddress.ip_address(ip)
        except ValueError as e:
            errors.append(f"Invalid IP/CIDR '{ip}': {str(e)}")

    return len(errors) == 0, errors


def get_client_ip(request: Request) -> Optional[str]:
    """
    Utility function to get client IP from request.
    Exported for use in other modules (including audit logging).

    Proxy headers (X-Forwarded-For / X-Real-IP) are only trusted when the
    direct socket peer is in the TRUSTED_PROXIES allowlist. Otherwise the
    socket peer address is used, so clients cannot spoof their IP by
    sending forged headers.

    Args:
        request: FastAPI request

    Returns:
        Client IP string or None
    """
    # Direct socket peer
    peer_ip = request.client.host if request.client else None

    # Only honor proxy headers if the peer is a trusted proxy
    if peer_ip and _is_trusted_proxy(peer_ip):
        # Check X-Forwarded-For header (take the first IP: original client)
        forwarded_for = request.headers.get('X-Forwarded-For')
        if forwarded_for:
            return forwarded_for.split(',')[0].strip()

        # Check X-Real-IP header
        real_ip = request.headers.get('X-Real-IP')
        if real_ip:
            return real_ip.strip()

    return peer_ip
