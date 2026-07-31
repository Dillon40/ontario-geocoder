#!/bin/bash
set -e

echo "=========================================="
echo "Environment Configuration"
echo "=========================================="
echo ""

# Color codes
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

# Navigate to project directory (parent of dependency_setup/)
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_DIR="$( cd "$SCRIPT_DIR/.." && pwd )"
ENV_FILE="$PROJECT_DIR/.env"

# Find psql binary (handles RHEL/AlmaLinux where it's in /usr/pgsql-XX/bin)
find_psql() {
    if command -v psql &> /dev/null; then
        PSQL_BIN="psql"
    elif [ -f /usr/pgsql-16/bin/psql ]; then
        PSQL_BIN="/usr/pgsql-16/bin/psql"
    elif [ -f /usr/pgsql-15/bin/psql ]; then
        PSQL_BIN="/usr/pgsql-15/bin/psql"
    elif [ -f /usr/pgsql-14/bin/psql ]; then
        PSQL_BIN="/usr/pgsql-14/bin/psql"
    else
        PSQL_BIN=""
    fi
}

find_psql

# Check if .env already exists
if [ -f "$ENV_FILE" ]; then
    echo -e "${YELLOW}⚠ .env file already exists${NC}"
    echo ""
    read -p "Overwrite existing .env file? (y/N) " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Keeping existing .env file"
        exit 0
    fi
    # Backup existing file
    cp "$ENV_FILE" "$ENV_FILE.backup.$(date +%Y%m%d_%H%M%S)"
    echo -e "${BLUE}Backed up existing .env file${NC}"
    echo ""
fi

# Default values
# WARNING: DB_PASSWORD below is a LOCAL DEVELOPMENT ONLY default.
# Never use this password in a shared or production environment.
DB_HOST="localhost"
DB_PORT="5432"
DB_NAME="geocoder"
DB_USER="geocoder"
DB_PASSWORD="geocoder123"
REDIS_URL="redis://localhost:6379"

echo "Creating .env file with default configuration..."
echo ""

# Create .env file
cat > "$ENV_FILE" <<EOF
# Database Configuration
DB_HOST=$DB_HOST
DB_PORT=$DB_PORT
DB_NAME=$DB_NAME
DB_USER=$DB_USER
DB_PASSWORD=$DB_PASSWORD

# Database Connection Pool
MIN_DB_CONNECTIONS=5
MAX_DB_CONNECTIONS=20

# Redis Configuration (optional - remove if not using cache)
REDIS_URL=$REDIS_URL

# Debugging
LOG_SQL_QUERIES=false

# Batch Processing
BATCH_CONCURRENCY_LIMIT=10
EOF

echo -e "${GREEN}✓ .env file created${NC}"
echo ""

# Display the configuration
echo "=========================================="
echo "Configuration Summary"
echo "=========================================="
echo ""
echo "Database:"
echo "  Host: $DB_HOST"
echo "  Port: $DB_PORT"
echo "  Database: $DB_NAME"
echo "  User: $DB_USER"
echo "  Password: $DB_PASSWORD"
echo ""
echo "Redis:"
echo "  URL: $REDIS_URL"
echo ""

# Test database connection
echo "=========================================="
echo "Testing Database Connection"
echo "=========================================="
echo ""

if [ -n "$PSQL_BIN" ]; then
    if PGPASSWORD="$DB_PASSWORD" $PSQL_BIN -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -c "SELECT version();" > /dev/null 2>&1; then
        echo -e "${GREEN}✓ Database connection successful${NC}"

        # Check PostGIS
        if PGPASSWORD="$DB_PASSWORD" $PSQL_BIN -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -c "SELECT PostGIS_version();" > /dev/null 2>&1; then
            echo -e "${GREEN}✓ PostGIS extension available${NC}"
        else
            echo -e "${RED}✗ PostGIS extension not found${NC}"
        fi
    else
        echo -e "${RED}✗ Database connection failed${NC}"
        echo "Please check your database configuration"
    fi
else
    echo -e "${YELLOW}⚠ psql not found in PATH - skipping connection test${NC}"
fi

echo ""

# Test Redis connection
echo "=========================================="
echo "Testing Redis Connection"
echo "=========================================="
echo ""

if command -v redis-cli &> /dev/null; then
    if redis-cli ping > /dev/null 2>&1; then
        echo -e "${GREEN}✓ Redis connection successful${NC}"
    else
        echo -e "${RED}✗ Redis connection failed${NC}"
        echo "Make sure Redis is running: sudo systemctl start redis-server"
    fi
else
    echo -e "${YELLOW}⚠ redis-cli not found in PATH - skipping connection test${NC}"
fi

echo ""
echo "=========================================="
echo "Configuration Complete"
echo "=========================================="
echo ""
echo "Your .env file is located at: $ENV_FILE"
echo ""
echo -e "${BLUE}Important Notes:${NC}"
echo "  • Change DB_PASSWORD for production use"
echo ""
echo -e "${GREEN}Next Steps:${NC}"
echo "  1. Review and edit .env file: nano .env"
echo "  2. Import data: uv run database/import_production_data.py --help"
echo "  3. Test scripts: DB_HOST=localhost uv run python <script_name>"
echo ""
