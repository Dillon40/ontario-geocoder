# =============================================================================
# Property Geocoder Microservice - Production Dockerfile
# Multi-stage build for optimized production image
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Builder - Install dependencies
# -----------------------------------------------------------------------------
FROM python:3.11.15-slim AS builder

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy

# Install build dependencies (needed for asyncpg, psycopg2, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast package management
RUN pip install uv

# Set work directory
WORKDIR /app

# Copy dependency files and source (needed for hatchling build)
COPY pyproject.toml uv.lock ./
COPY api/ ./api/
COPY engine/ ./engine/

# Create virtual environment (at UV_PROJECT_ENVIRONMENT) and install
# dependencies exactly as pinned in uv.lock (fails if the lock is stale)
RUN uv sync --frozen --no-dev --no-cache

# -----------------------------------------------------------------------------
# Stage 2: Runtime - Production image
# -----------------------------------------------------------------------------
FROM python:3.11.15-slim AS runtime

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/opt/venv/bin:$PATH" \
    # Application settings
    PORT=3000 \
    ENV=production \
    # Security defaults
    API_KEY_REQUIRED=true \
    RATE_LIMIT_ENABLED=true \
    IP_FILTER_ENABLED=true \
    SECURE_HEADERS_ENABLED=true

# Install runtime system dependencies (libpq for asyncpg/psycopg2)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for security
RUN groupadd -r geocoder && useradd -r -g geocoder geocoder

# Set work directory
WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv

# Copy application code
COPY api/ ./api/
COPY engine/ ./engine/
COPY config/ ./config/
COPY database/ ./database/

# Set ownership to non-root user
RUN chown -R geocoder:geocoder /app

# Switch to non-root user
USER geocoder

# Expose port
EXPOSE 3000

# Health check — uses PORT env var, Python-based (no curl needed)
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://localhost:{os.environ.get(\"PORT\",3000)}/health', timeout=5)" || exit 1

# Run with gunicorn + uvicorn workers for production
# - PORT: configurable via env var (default 3000)
# - workers: 2-4x CPU cores (adjust via GUNICORN_WORKERS env var)
# - worker-class: uvicorn.workers.UvicornWorker for async support
# - timeout: 120s for batch geocoding operations
# - graceful-timeout: 30s for graceful shutdown
CMD sh -c "exec gunicorn api.main:app \
    --bind 0.0.0.0:${PORT:-3000} \
    --workers ${GUNICORN_WORKERS:-4} \
    --worker-class uvicorn.workers.UvicornWorker \
    --timeout 120 \
    --graceful-timeout 30 \
    --keep-alive 5 \
    --access-logfile - \
    --error-logfile - \
    --capture-output \
    --enable-stdio-inheritance"
