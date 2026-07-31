#!/bin/bash
set -e

echo "=========================================="
echo "PostgreSQL Database Setup"
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

# Default configuration
# WARNING: DB_PASSWORD below is a LOCAL DEVELOPMENT ONLY default.
# Never use this password in a shared or production environment.
DB_NAME="geocoder"
DB_USER="geocoder"
DB_PASSWORD="geocoder123"

# Find psql binary (handles RHEL/AlmaLinux where it's in /usr/pgsql-XX/bin)
find_psql() {
    if command -v psql &> /dev/null; then
        PSQL_BIN="psql"
    elif [ -f /usr/pgsql-16/bin/psql ]; then
        PSQL_BIN="/usr/pgsql-16/bin/psql"
        export PATH="/usr/pgsql-16/bin:$PATH"
    elif [ -f /usr/pgsql-15/bin/psql ]; then
        PSQL_BIN="/usr/pgsql-15/bin/psql"
        export PATH="/usr/pgsql-15/bin:$PATH"
    elif [ -f /usr/pgsql-14/bin/psql ]; then
        PSQL_BIN="/usr/pgsql-14/bin/psql"
        export PATH="/usr/pgsql-14/bin:$PATH"
    else
        echo -e "${RED}Error: psql not found${NC}"
        echo "Please ensure PostgreSQL client is installed"
        exit 1
    fi
    echo -e "${BLUE}Using psql:${NC} $PSQL_BIN"
}

# Find the correct PostgreSQL service name
find_pg_service() {
    # Try various service names
    for service in postgresql-16 postgresql-15 postgresql-14 postgresql; do
        if sudo systemctl list-unit-files | grep -q "^${service}.service"; then
            PG_SERVICE="$service"
            return 0
        fi
    done
    PG_SERVICE="postgresql"
}

find_psql
find_pg_service

echo ""
echo "This script will:"
echo "  1. Create PostgreSQL database and user"
echo "  2. Enable PostGIS extension"
echo "  3. Initialize database schema"
echo "  4. Run migrations"
echo ""
echo -e "${YELLOW}Default Configuration:${NC}"
echo "  Database: $DB_NAME"
echo "  User: $DB_USER"
echo "  Password: $DB_PASSWORD"
echo ""
echo -e "${RED}WARNING:${NC} LOCAL DEVELOPMENT ONLY - never use this default password in a shared or production environment."
echo ""
read -p "Continue with these settings? (y/N) " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Setup cancelled"
    exit 1
fi

echo ""
echo -e "${GREEN}[1/5] Checking PostgreSQL service...${NC}"
if ! sudo systemctl is-active --quiet "$PG_SERVICE"; then
    echo "Starting PostgreSQL ($PG_SERVICE)..."
    sudo systemctl start "$PG_SERVICE"
    sudo systemctl enable "$PG_SERVICE"
fi
echo "PostgreSQL is running"

echo ""
echo -e "${GREEN}[2/5] Creating database and user...${NC}"

# Create user and database
sudo -u postgres $PSQL_BIN -v ON_ERROR_STOP=1 <<-EOSQL
    -- Create user if not exists
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_user WHERE usename = '$DB_USER') THEN
            CREATE USER $DB_USER WITH PASSWORD '$DB_PASSWORD';
        ELSE
            RAISE NOTICE 'User $DB_USER already exists';
        END IF;
    END
    \$\$;

    -- Create database if not exists
    SELECT 'CREATE DATABASE $DB_NAME OWNER $DB_USER'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$DB_NAME')\gexec

    -- Grant privileges
    GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;
EOSQL

echo "Database and user created successfully"

echo ""
echo -e "${GREEN}[3/5] Enabling PostGIS extension...${NC}"
sudo -u postgres $PSQL_BIN -d "$DB_NAME" -v ON_ERROR_STOP=1 <<-EOSQL
    CREATE EXTENSION IF NOT EXISTS postgis;
    CREATE EXTENSION IF NOT EXISTS postgis_topology;
EOSQL
echo "PostGIS enabled"

echo ""
echo -e "${GREEN}[4/5] Running database schema...${NC}"

if [ -f "$PROJECT_DIR/database/schema.sql" ]; then
    PGPASSWORD="$DB_PASSWORD" $PSQL_BIN -h localhost -U "$DB_USER" -d "$DB_NAME" -f "$PROJECT_DIR/database/schema.sql"
    echo "Schema created successfully"
else
    echo -e "${RED}Error: database/schema.sql not found${NC}"
    exit 1
fi

echo ""
echo -e "${GREEN}[5/5] Running database migrations...${NC}"
if [ -f "$PROJECT_DIR/database/migrations.sql" ]; then
    PGPASSWORD="$DB_PASSWORD" $PSQL_BIN -h localhost -U "$DB_USER" -d "$DB_NAME" -f "$PROJECT_DIR/database/migrations.sql"
    echo "Migrations completed successfully"
else
    echo -e "${YELLOW}⚠ No migrations.sql found - skipping${NC}"
fi

echo ""
echo "=========================================="
echo "Database Setup Complete"
echo "=========================================="
echo ""
echo -e "${GREEN}✓ Database: $DB_NAME${NC}"
echo -e "${GREEN}✓ User: $DB_USER${NC}"
echo -e "${GREEN}✓ PostGIS: Enabled${NC}"
echo -e "${GREEN}✓ Schema: Initialized${NC}"
echo ""
echo "Connection details:"
echo "  Host: localhost"
echo "  Port: 5432"
echo "  Database: $DB_NAME"
echo "  User: $DB_USER"
echo "  Password: $DB_PASSWORD"
echo ""
echo "Test connection:"
echo "  PGPASSWORD=$DB_PASSWORD $PSQL_BIN -h localhost -U $DB_USER -d $DB_NAME"
echo ""
echo -e "${BLUE}Next Step:${NC} Run ./configure_env.sh to create your .env file"
echo ""
