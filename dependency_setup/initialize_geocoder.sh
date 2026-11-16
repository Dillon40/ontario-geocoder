#!/bin/bash
# Unified Geocoder Initialization Script
# Single entry point for setting up the complete geocoder system
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"

echo "=========================================="
echo "Geocoder Initialization"
echo "=========================================="
echo "Project root: $PROJECT_ROOT"
echo ""

# Parse arguments
FULL_SETUP=false
SKIP_DATA=false
IMPORT_DATA=false

show_help() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Unified setup script for the geocoder system."
    echo ""
    echo "Options:"
    echo "  --full        Run full system setup (VM packages, uv)"
    echo "  --skip-data   Skip data source seeding"
    echo "  --import-data Import all confirmed data sources after setup"
    echo "  -h, --help    Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0                    # Quick setup (migrations + seeding only)"
    echo "  $0 --full             # Full setup including VM packages"
    echo "  $0 --import-data      # Setup and import all data"
}

while [[ "$#" -gt 0 ]]; do
    case $1 in
        --full) FULL_SETUP=true ;;
        --skip-data) SKIP_DATA=true ;;
        --import-data) IMPORT_DATA=true ;;
        -h|--help)
            show_help
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            show_help
            exit 1
            ;;
    esac
    shift
done

# Step 1: Full system setup (optional)
if [ "$FULL_SETUP" = true ]; then
    echo -e "${GREEN}[1/5] Running full system setup...${NC}"
    if [ -f "$SCRIPT_DIR/setup_all.sh" ]; then
        bash "$SCRIPT_DIR/setup_all.sh"
    else
        echo -e "${YELLOW}  setup_all.sh not found, skipping...${NC}"
    fi
else
    echo -e "${YELLOW}[1/5] Skipping system setup (use --full to include)${NC}"
fi

# Step 2: Load environment and verify database connection
echo -e "${GREEN}[2/5] Verifying database connection...${NC}"

# Load environment variables if .env exists
if [ -f "$PROJECT_ROOT/.env" ]; then
    set -a
    source "$PROJECT_ROOT/.env"
    set +a
fi

DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-geocoder}"
DB_USER="${DB_USER:-geocoder}"
: "${DB_PASSWORD:?Set DB_PASSWORD in .env or the environment}"
export DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD

# Test database connection
if PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "SELECT 1" > /dev/null 2>&1; then
    echo "  Database connection verified"
else
    echo -e "${RED}  Cannot connect to database.${NC}"
    echo "  Please ensure PostgreSQL is running and the database exists."
    echo "  Run: bash $SCRIPT_DIR/setup_database.sh"
    exit 1
fi

# Step 3: Run schema (idempotent)
echo -e "${GREEN}[3/5] Applying database schema...${NC}"
if [ -f "$PROJECT_ROOT/database/schema.sql" ]; then
    PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
        -f "$PROJECT_ROOT/database/schema.sql" 2>/dev/null || {
        echo "  Schema already exists or was applied successfully"
    }
    echo "  Schema applied"
else
    echo -e "${YELLOW}  schema.sql not found${NC}"
fi

# Step 4: Run migrations
echo -e "${GREEN}[4/5] Running database migrations...${NC}"
if [ -f "$PROJECT_ROOT/database/run_migrations.py" ]; then
    cd "$PROJECT_ROOT"
    uv run database/run_migrations.py
else
    echo -e "${YELLOW}  run_migrations.py not found, running migrations.sql directly...${NC}"
    if [ -f "$PROJECT_ROOT/database/migrations.sql" ]; then
        PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
            -f "$PROJECT_ROOT/database/migrations.sql" 2>/dev/null || true
    fi
fi

# Step 5: Seed and optionally import data
if [ "$SKIP_DATA" = false ]; then
    echo -e "${GREEN}[5/5] Seeding data sources...${NC}"
    if [ -f "$PROJECT_ROOT/database/seed_data_sources.sql" ]; then
        PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
            -f "$PROJECT_ROOT/database/seed_data_sources.sql" 2>/dev/null || true
        echo "  Data sources seeded"
    else
        echo -e "${YELLOW}  seed_data_sources.sql not found${NC}"
    fi

    if [ "$IMPORT_DATA" = true ]; then
        echo ""
        echo "Importing data sources..."
        cd "$PROJECT_ROOT"
        uv run data_sourcing/setup_data.py --all-confirmed --skip-manual
    fi
else
    echo -e "${YELLOW}[5/5] Skipping data setup${NC}"
fi

# Summary
echo ""
echo "=========================================="
echo -e "${GREEN}Initialization Complete${NC}"
echo "=========================================="

# Show migration status
if [ -f "$PROJECT_ROOT/database/run_migrations.py" ]; then
    cd "$PROJECT_ROOT"
    uv run database/run_migrations.py --status
fi

echo ""
echo "Next steps:"
if [ "$IMPORT_DATA" = false ]; then
    echo "  - Import data: uv run data_sourcing/setup_data.py --all-confirmed"
fi
echo "  - Start API:   uv run uvicorn api.main:app --reload"
echo "  - Run tests:   uv run pytest"
echo ""
