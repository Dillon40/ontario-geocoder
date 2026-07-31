# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "fastapi",
#   "uvicorn",
#   "python-dotenv",
# ]
# ///

"""
Property Geocoder Microservice - FastAPI Application
Tiered cascading geocoding system for Ontario property addresses.
"""
import os
import sys
import time
import uuid
import logging
from datetime import datetime
from contextlib import asynccontextmanager
from importlib import metadata
from typing import Optional, List
from fastapi import APIRouter, FastAPI, HTTPException, status, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from pydantic import BaseModel

# =============================================================================
# Logging Configuration
# =============================================================================

# Configure logging before anything else
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger('geocoder.api')

# Load environment variables BEFORE importing services
# This ensures .env values are available when service modules initialize
load_dotenv()

from engine import database, cache, geocoder, license_service
from .services import api_keys
from .services.config_validator import check_and_report, bootstrap_initial_admin_key
from .models import (
    GeocodeRequest, GeocodeResponse, BatchGeocodeRequest, BatchGeocodeResponse,
    HealthResponse, StatsResponse, ServiceInfo, ErrorResponse, AttributionsResponse
)
from .middleware.auth import AuthMiddleware, require_admin
from .middleware.rate_limiter import RateLimitMiddleware
from .middleware.ip_filter import IPFilterMiddleware, get_client_ip
from .config import security_config

# Maximum request body size (10MB)
MAX_BODY_SIZE = int(os.getenv('MAX_BODY_SIZE', 10 * 1024 * 1024))

# Single-sourced API version: read from the installed package metadata
# (pyproject.toml [project].version) so there is exactly one place to bump it.
try:
    API_VERSION = metadata.version('ontario-geocoder')
except metadata.PackageNotFoundError:
    # Running from source without an installed distribution (e.g. bare uvicorn)
    API_VERSION = '1.0.0'


def error_content(code: str, message: str, details=None) -> dict:
    """
    Build the standard error envelope used by every error response:

        {"error": {"code": "<stable_snake_case_code>", "message": "<human message>"}}

    with an optional "details" key for extra machine-readable context.
    """
    error = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"error": error}


# =============================================================================
# Admin Request/Response Models
# =============================================================================

class CreateAPIKeyRequest(BaseModel):
    name: str
    owner: Optional[str] = None
    expires_at: Optional[datetime] = None
    rate_limit_override: Optional[int] = None
    allowed_ips: Optional[List[str]] = None
    permissions: Optional[dict] = None


class UpdateAPIKeyRequest(BaseModel):
    name: Optional[str] = None
    owner: Optional[str] = None
    expires_at: Optional[datetime] = None
    rate_limit_override: Optional[int] = None
    allowed_ips: Optional[List[str]] = None
    permissions: Optional[dict] = None
    is_active: Optional[bool] = None

# Server start time for uptime calculation
start_time = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager for startup/shutdown events."""
    # Startup - Validate configuration first
    logger.info("Validating configuration...")
    if not check_and_report():
        logger.critical("Configuration validation failed. Exiting.")
        sys.exit(1)

    # Initialize database
    logger.info("Initializing database connection pool...")
    await database.init_pool()
    db_connected = await database.test_connection()

    if not db_connected:
        logger.critical("Failed to connect to database")
        raise RuntimeError('Database connection failed')

    # Initialize Redis
    logger.info("Connecting to Redis...")
    await cache.connect()

    # Initialize license cache
    logger.info("Loading license cache...")
    await license_service.init_license_cache()

    # Bootstrap initial admin key if specified
    await bootstrap_initial_admin_key()

    # Print startup banner
    port = int(os.getenv('PORT', 3000))
    logger.info("=" * 60)
    logger.info("Property Geocoder Microservice")
    logger.info(f"Server running on port {port}")
    logger.info(f"API available at http://localhost:{port}")
    logger.info("=" * 60)
    logger.info("Endpoints:")
    logger.info(f"  POST   http://localhost:{port}/v1/geocode")
    logger.info(f"  POST   http://localhost:{port}/v1/geocode/batch")
    logger.info(f"  GET    http://localhost:{port}/v1/health")
    logger.info(f"  GET    http://localhost:{port}/v1/stats")
    logger.info(f"  GET    http://localhost:{port}/v1/attributions")
    logger.info("  (unversioned paths remain available as deprecated aliases)")
    logger.info("=" * 60)

    yield

    # Shutdown
    logger.info("Shutting down gracefully...")
    await database.close()
    await cache.close()
    logger.info("Connections closed")


# Create FastAPI app
app = FastAPI(
    title="Property Geocoder Microservice",
    description=(
        "Tiered cascading geocoding system for Ontario property addresses.\n\n"
        "Canonical routes live under `/v1/`. The unversioned routes are "
        "deprecated aliases kept for existing consumers.\n\n"
        "All error responses use a single envelope: "
        '`{"error": {"code": "<snake_case_code>", "message": "<human message>"}}`.'
    ),
    version=API_VERSION,
    lifespan=lifespan
)


# =============================================================================
# Security Headers Middleware
# =============================================================================

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        if security_config.SECURE_HEADERS_ENABLED:
            response.headers['X-Content-Type-Options'] = 'nosniff'
            response.headers['X-Frame-Options'] = 'DENY'
            response.headers['X-XSS-Protection'] = '1; mode=block'
            response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'

        return response


# =============================================================================
# Request Body Size Limit Middleware
# =============================================================================

class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Limit request body size to prevent memory exhaustion attacks."""

    def __init__(self, app, max_size: int = MAX_BODY_SIZE):
        super().__init__(app)
        self.max_size = max_size

    async def dispatch(self, request: Request, call_next):
        # Check Content-Length header if present
        content_length = request.headers.get('content-length')
        if content_length:
            if int(content_length) > self.max_size:
                return JSONResponse(
                    status_code=413,
                    content=error_content(
                        "request_too_large",
                        "Request body too large",
                        details={
                            "max_size_bytes": self.max_size,
                            "max_size_mb": self.max_size / (1024 * 1024)
                        }
                    )
                )

        return await call_next(request)


# =============================================================================
# Request ID Middleware
# =============================================================================

class RequestIDMiddleware(BaseHTTPMiddleware):
    """Add unique request ID to each request for tracing."""

    async def dispatch(self, request: Request, call_next):
        # Get existing request ID or generate new one
        request_id = request.headers.get(security_config.REQUEST_ID_HEADER)
        if not request_id:
            request_id = str(uuid.uuid4())

        # Store in request state
        request.state.request_id = request_id

        # Process request
        response = await call_next(request)

        # Add to response headers
        response.headers[security_config.REQUEST_ID_HEADER] = request_id

        return response


# =============================================================================
# Add Middleware (order matters - first added is last executed)
# =============================================================================

# CORS middleware (outermost)
app.add_middleware(
    CORSMiddleware,
    allow_origins=security_config.get_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request body size limit
app.add_middleware(BodySizeLimitMiddleware)

# Security headers
app.add_middleware(SecurityHeadersMiddleware)

# Request ID tracking
app.add_middleware(RequestIDMiddleware)

# Public paths that don't require authentication
public_paths = ['/health', '/v1/health']

# IP filtering (check after auth so we have API key data)
app.add_middleware(IPFilterMiddleware, excluded_paths=public_paths)

# Rate limiting (check after auth so we have API key data)
app.add_middleware(RateLimitMiddleware, excluded_paths=public_paths)

# Authentication (innermost security middleware)
app.add_middleware(AuthMiddleware, excluded_paths=public_paths)


# Request logging middleware
@app.middleware("http")
async def log_requests(request, call_next):
    """Log all HTTP requests with request ID."""
    request_id = getattr(request.state, 'request_id', 'unknown')
    api_key = getattr(request.state, 'api_key', None)
    key_name = api_key.get('name', 'N/A') if api_key else 'unauthenticated'

    logger.info(f"[{request_id[:8]}] {request.method} {request.url.path} - Key: {key_name}")

    response = await call_next(request)
    return response


@app.get("/", response_model=ServiceInfo)
async def root():
    """API information endpoint."""
    return ServiceInfo(
        service="Property Geocoder Microservice",
        version=API_VERSION,
        description="Tiered cascading geocoding system for Ontario property addresses",
        endpoints={
            "POST /v1/geocode": "Geocode a single address",
            "POST /v1/geocode/batch": "Geocode multiple addresses",
            "GET /v1/health": "Health check",
            "GET /v1/stats": "Service statistics",
            "GET /v1/attributions": "Required attribution statements for active data sources",
            "POST /geocode": "DEPRECATED alias for /v1/geocode",
            "POST /geocode/batch": "DEPRECATED alias for /v1/geocode/batch"
        },
        tiers={
            "tier_1": "PIN-based lookup (confidence 1.0)",
            "tier_2": "Municipal dataset match (confidence 0.85-0.95)",
            "tier_3": "National Address Register (confidence 0.70-0.85)"
        }
    )


# =============================================================================
# API Routes (FastAPI APIRouter)
#
# The router is mounted twice below: once under /v1 (canonical) and once at
# the root (deprecated aliases) so the existing IDX consumer keeps working.
# =============================================================================

router = APIRouter()


@router.post(
    "/geocode",
    response_model=GeocodeResponse,
    response_model_exclude_unset=True,
    responses={
        404: {"model": ErrorResponse, "description": "No match found in any data source"},
        400: {"model": ErrorResponse, "description": "Empty or malformed request body"},
    },
)
async def geocode_address(request: Request, body: GeocodeRequest):
    """
    Geocode a single address.

    Canonical path: /v1/geocode. The unversioned /geocode path is a
    DEPRECATED alias kept for backward compatibility.

    Returns 404 with the standard error envelope when no match is found
    (unlike /geocode/batch, which returns 200 with per-item status).
    """
    try:
        # Verbatim raw input as received (Starlette caches the parsed body,
        # so this does not re-read the stream)
        verbatim_input = await request.json()

        # Extract MLS number (will be echoed back)
        mls_number = body.mls_number

        # Convert Pydantic model to dict for processing
        address_input = body.model_dump(exclude_none=True)

        if not address_input:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "address_required", "message": "Address input required"}
            )

        result = await geocoder.geocode(address_input)

        # Set success flag if not already set
        if 'success' not in result:
            result['success'] = bool(result.get('latitude'))

        if not result.get('success'):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "address_not_found",
                    "message": result.get('error') or "No match found in any data source"
                }
            )

        # Enhance response with new fields
        result['original_input'] = verbatim_input
        result['mls_number'] = mls_number

        # Add license info + attribution based on source
        if result.get('source'):
            license_info = license_service.get_license_info(result['source'])
            if license_info:
                result['license_info'] = license_info
                result['attribution'] = license_info.get('required_attribution')

        return result

    except HTTPException:
        raise
    except Exception as error:
        logger.error(f'Geocode endpoint error: {error}')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "internal_error", "message": "Internal server error"}
        )


@router.post(
    "/geocode/batch",
    response_model=BatchGeocodeResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Malformed request body"},
    },
)
async def geocode_batch(request: Request, body: BatchGeocodeRequest):
    """
    Geocode multiple addresses (max 1000 per request).

    Canonical path: /v1/geocode/batch. The unversioned /geocode/batch path
    is a DEPRECATED alias kept for backward compatibility.

    Always returns 200 with a per-item "success" flag in each result
    (unlike the single /geocode endpoint, which returns 404 on no match).
    """
    try:
        # Get original addresses from raw body for verbatim preservation
        raw_data = await request.json()
        raw_addresses = raw_data.get('addresses', []) if isinstance(raw_data, dict) else []

        # List size (1-1000) is enforced by BatchGeocodeRequest validation
        result = await geocoder.geocode_batch(body.addresses)

        # Enhance each result with new fields
        for i, item in enumerate(result.get('results', [])):
            # Parity with the single /geocode endpoint, which sets this post-hoc
            if 'success' not in item:
                item['success'] = bool(item.get('latitude'))

            # Get original input for this item
            original = raw_addresses[i] if i < len(raw_addresses) else None
            item['original_input'] = original

            # Extract MLS number from original input
            mls_number = None
            if isinstance(original, dict):
                mls_number = original.get('mls_number')
            item['mls_number'] = mls_number

            # Add license info + attribution based on source
            if item.get('source'):
                license_info = license_service.get_license_info(item['source'])
                if license_info:
                    item['license_info'] = license_info
                    item['attribution'] = license_info.get('required_attribution')

        return result

    except HTTPException:
        raise
    except Exception as error:
        logger.error(f'Batch geocode endpoint error: {error}')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "internal_error", "message": "Internal server error"}
        )


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={
        503: {"model": HealthResponse, "description": "One or more backing services are down"},
    },
)
async def health_check():
    """
    Health check endpoint.

    Canonical path: /v1/health. The unversioned /health path is a
    DEPRECATED alias kept for backward compatibility.
    """
    try:
        db_connected = await database.test_connection()
        cache_connected = await cache.test_connection()

        healthy = db_connected and cache_connected

        response = HealthResponse(
            status="healthy" if healthy else "unhealthy",
            database="connected" if db_connected else "disconnected",
            redis="connected" if cache_connected else "disconnected",
            timestamp=datetime.utcnow().isoformat()
        )

        status_code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE

        return JSONResponse(content=response.model_dump(), status_code=status_code)

    except Exception as error:
        logger.error(f'Health check error: {error}')
        return JSONResponse(
            content={
                "status": "unhealthy",
                "database": "unknown",
                "redis": "unknown",
                "timestamp": datetime.utcnow().isoformat()
            },
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE
        )


@router.get("/stats", response_model=StatsResponse)
async def get_stats():
    """
    Service statistics endpoint.

    Canonical path: /v1/stats. The unversioned /stats path is a
    DEPRECATED alias kept for backward compatibility.
    """
    try:
        uptime_seconds = int(time.time() - start_time)
        db_stats = await database.get_stats()
        cache_stats = cache.get_stats()
        geocoding_stats = geocoder.get_stats()

        return {
            "uptime_seconds": uptime_seconds,
            "database": {
                "properties_count": db_stats['properties_total'],
                "cache_entries": db_stats['cache_entries'],
                "datasets": db_stats['datasets'],
                "nar_addresses_count": db_stats['nar_addresses'],
                "nar_locations_count": db_stats['nar_locations']
            },
            "cache": {
                "hit_rate": cache_stats['hit_rate'],
                "total_hits": cache_stats['total_hits'],
                "total_misses": cache_stats['total_misses'],
                "connected": cache_stats['connected']
            },
            "geocoding": geocoding_stats
        }

    except Exception as error:
        logger.error(f'Stats endpoint error: {error}')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "stats_failed", "message": "Failed to retrieve statistics"}
        )


@router.get("/attributions", response_model=AttributionsResponse)
async def get_attributions():
    """
    List required attribution statements for every active data source.

    Canonical path: /v1/attributions. The unversioned /attributions path is
    a DEPRECATED alias kept for backward compatibility.

    Licence compliance: callers displaying geocoded results derived from
    these sources (StatCan NAR, municipal Open Government Licence datasets,
    etc.) must surface the matching required_attribution statement.
    """
    try:
        return {"attributions": license_service.get_all_attributions()}

    except Exception as error:
        logger.error(f'Attributions endpoint error: {error}')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "attributions_failed", "message": "Failed to retrieve attributions"}
        )


# =============================================================================
# Admin Endpoints - API Key Management
#
# Registered on the same shared `router` as the core routes above, so they
# get the same /v1 canonical + unversioned deprecated-alias mounting.
# =============================================================================

@router.post("/admin/keys")
async def create_api_key(request: Request, body: CreateAPIKeyRequest):
    """
    Create a new API key.
    Returns the full key only once - store it securely.
    Requires admin permission.
    """
    await require_admin(request)

    try:
        # Validate IP list if provided
        if body.allowed_ips:
            from .middleware.ip_filter import validate_ip_list
            is_valid, errors = validate_ip_list(body.allowed_ips)
            if not is_valid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "invalid_ip_list",
                        "message": "Invalid IP addresses in allowed_ips",
                        "details": errors
                    }
                )

        result = await api_keys.create_key(
            name=body.name,
            owner=body.owner,
            expires_at=body.expires_at,
            rate_limit_override=body.rate_limit_override,
            allowed_ips=body.allowed_ips,
            permissions=body.permissions
        )

        # Audit log
        admin_key = getattr(request.state, 'api_key', {})
        await api_keys.log_admin_action(
            admin_key_id=admin_key.get('id'),
            admin_key_name=admin_key.get('name', 'unknown'),
            action='create_key',
            target_type='api_key',
            target_id=result['id'],
            target_name=result['name'],
            details={'owner': body.owner, 'permissions': body.permissions},
            client_ip=get_client_ip(request),
            request_id=getattr(request.state, 'request_id', None)
        )

        return {
            "success": True,
            "message": "API key created successfully. Store the key securely - it cannot be retrieved again.",
            "data": result
        }

    except HTTPException:
        raise
    except Exception as error:
        logger.error(f'Create API key error: {error}')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "key_creation_failed",
                "message": "Failed to create API key"
            }
        )


@router.get("/admin/keys")
async def list_api_keys(
    request: Request,
    include_inactive: bool = False,
    limit: int = 100,
    offset: int = 0
):
    """
    List all API keys (without the actual key values).
    Requires admin permission.
    """
    await require_admin(request)

    try:
        keys = await api_keys.list_keys(
            include_inactive=include_inactive,
            limit=limit,
            offset=offset
        )

        return {
            "success": True,
            "count": len(keys),
            "data": keys
        }

    except HTTPException:
        raise
    except Exception as error:
        logger.error(f'List API keys error: {error}')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "list_keys_failed",
                "message": "Failed to list API keys"
            }
        )


@router.get("/admin/keys/{key_id}")
async def get_api_key(request: Request, key_id: int):
    """
    Get details for a specific API key.
    Requires admin permission.
    """
    await require_admin(request)

    try:
        key_data = await api_keys.get_key_by_id(key_id)

        if not key_data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "key_not_found",
                    "message": f"API key with ID {key_id} not found"
                }
            )

        # Get usage stats
        usage = await api_keys.get_usage_stats(key_id)

        return {
            "success": True,
            "data": {
                **key_data,
                "usage_stats": usage
            }
        }

    except HTTPException:
        raise
    except Exception as error:
        logger.error(f'Get API key error: {error}')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "get_key_failed",
                "message": "Failed to retrieve API key"
            }
        )


@router.patch("/admin/keys/{key_id}")
async def update_api_key(request: Request, key_id: int, body: UpdateAPIKeyRequest):
    """
    Update an API key's settings.
    Requires admin permission.
    """
    await require_admin(request)

    try:
        # Validate IP list if provided
        if body.allowed_ips is not None:
            from .middleware.ip_filter import validate_ip_list
            is_valid, errors = validate_ip_list(body.allowed_ips)
            if not is_valid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "invalid_ip_list",
                        "message": "Invalid IP addresses in allowed_ips",
                        "details": errors
                    }
                )

        result = await api_keys.update_key(
            key_id=key_id,
            name=body.name,
            owner=body.owner,
            expires_at=body.expires_at,
            rate_limit_override=body.rate_limit_override,
            allowed_ips=body.allowed_ips,
            permissions=body.permissions,
            is_active=body.is_active
        )

        if not result:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "key_not_found",
                    "message": f"API key with ID {key_id} not found"
                }
            )

        # Audit log
        admin_key = getattr(request.state, 'api_key', {})
        await api_keys.log_admin_action(
            admin_key_id=admin_key.get('id'),
            admin_key_name=admin_key.get('name', 'unknown'),
            action='update_key',
            target_type='api_key',
            target_id=key_id,
            target_name=result.get('name'),
            details=body.model_dump(exclude_none=True),
            client_ip=get_client_ip(request),
            request_id=getattr(request.state, 'request_id', None)
        )

        return {
            "success": True,
            "message": "API key updated successfully",
            "data": result
        }

    except HTTPException:
        raise
    except Exception as error:
        logger.error(f'Update API key error: {error}')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "update_key_failed",
                "message": "Failed to update API key"
            }
        )


@router.delete("/admin/keys/{key_id}")
async def revoke_api_key(request: Request, key_id: int):
    """
    Revoke (deactivate) an API key.
    Requires admin permission.
    """
    await require_admin(request)

    try:
        success = await api_keys.revoke_key(key_id)

        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "key_not_found",
                    "message": f"API key with ID {key_id} not found"
                }
            )

        # Audit log
        admin_key = getattr(request.state, 'api_key', {})
        await api_keys.log_admin_action(
            admin_key_id=admin_key.get('id'),
            admin_key_name=admin_key.get('name', 'unknown'),
            action='revoke_key',
            target_type='api_key',
            target_id=key_id,
            target_name=None,
            details=None,
            client_ip=get_client_ip(request),
            request_id=getattr(request.state, 'request_id', None)
        )

        return {
            "success": True,
            "message": f"API key {key_id} has been revoked"
        }

    except HTTPException:
        raise
    except Exception as error:
        logger.error(f'Revoke API key error: {error}')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "revoke_key_failed",
                "message": "Failed to revoke API key"
            }
        )


# =============================================================================
# Mount the shared router: /v1/* is canonical, unversioned paths remain as
# deprecated aliases (same route objects, so behaviour is identical) for the
# existing IDX site consumer. Must happen after every route above has been
# registered on `router` — include_router() copies the route list as of the
# call, it does not track later additions.
# =============================================================================

app.include_router(router, prefix="/v1")
app.include_router(router, deprecated=True)


# =============================================================================
# Exception Handlers
#
# Every error response — however it originates — is normalized to the single
# envelope: {"error": {"code": "...", "message": "...", "details": ...?}}.
# =============================================================================

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """
    Normalize every HTTPException (including ones raised by Starlette itself,
    e.g. for an unmatched route) into the standard error envelope.

    Route handlers in this file raise HTTPException with a
    {"code", "message"[, "details"]} detail dict; that is unpacked directly.
    Anything else (a bare string detail, or none) falls back to a generic
    "http_error" code so no response ever skips the envelope.
    """
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        content = error_content(
            detail["code"],
            detail.get("message", ""),
            detail.get("details")
        )
    else:
        message = detail if isinstance(detail, str) else "An error occurred"
        content = error_content("http_error", message)

    return JSONResponse(status_code=exc.status_code, content=content)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Pydantic/FastAPI request validation failures (422) use the envelope too."""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=error_content(
            "validation_error",
            "Request validation failed",
            details=jsonable_encoder(exc.errors())
        )
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Last-resort catch-all so a truly unexpected error still returns the envelope."""
    logger.error(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=error_content("internal_error", "Internal server error")
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv('PORT', 3000))
    uvicorn.run(
        "api.main:app",
        host="0.0.0.0",
        port=port,
        reload=os.getenv('ENV', 'production') == 'development'
    )
