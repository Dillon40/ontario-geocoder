# Remote VM Setup Guide

This guide will help you set up your remote VM for the geocoder project with all necessary tools and dependencies.

## Overview

This setup **does not use Docker**. All services (PostgreSQL, Redis, Python) run directly on the VM.

### What Gets Installed

**System Services:**
- PostgreSQL 14+ with PostGIS extension
- Redis server
- Python 3.11+
- Build tools and development libraries

**Python Tools:**
- uv - Modern Python package manager
- Project dependencies (via pyproject.toml)

## Quick Start

Use a dedicated VM. Before native setup, export a strong password and retain it securely:

```bash
export DB_PASSWORD="$(openssl rand -hex 32)"
```

There is no default DB password. Native setup requires shell-safe values (generated hex is
recommended), applies tracked migrations, and creates a mode-0600 development `.env` with a
random initial admin key. For production Docker, follow [DEPLOYMENT.md](DEPLOYMENT.md) instead.

### Option 1: Unified Setup (Recommended)

The `initialize_geocoder.sh` script is the single entry point for setting up the complete system:

```bash
# Quick setup - migrations + data seeding only (assumes system deps installed)
./dependency_setup/initialize_geocoder.sh

# Full setup including system packages (for fresh VM installations)
./dependency_setup/initialize_geocoder.sh --full

# Setup and import all confirmed data sources
./dependency_setup/initialize_geocoder.sh --import-data
```

**What it does:**
1. Verifies database connection
2. Applies database schema (idempotent)
3. Runs migrations with version tracking via `run_migrations.py`
4. Seeds data sources into the database
5. Optionally imports all confirmed data sources

### Option 2: Step-by-Step Setup

For more control, run individual scripts in `dependency_setup/`:

```bash
# 1. Install system packages (PostgreSQL, Redis, Python)
./dependency_setup/vm_setup.sh

# 2. Install uv package manager
./dependency_setup/install_uv.sh

# 3. Set up PostgreSQL database
./dependency_setup/setup_database.sh

# 4. Create .env configuration file
./dependency_setup/configure_env.sh

# 5. Run migrations with tracking
uv run python database/run_migrations.py
```

After running these scripts, you'll have a fully configured development environment.

## Detailed Setup Steps

### Step 1: System Packages

```bash
./dependency_setup/vm_setup.sh
```

This script:
- Updates system packages
- Installs PostgreSQL 14 with PostGIS
- Installs Redis server
- Installs Python 3.11+ and development tools
- Starts and enables services

**Services Status:**
- PostgreSQL: Running on localhost:5432
- Redis: Running on localhost:6379

### Step 2: Install uv

```bash
./dependency_setup/install_uv.sh
```

This script:
- Installs uv via official installer (to ~/.cargo/bin/)
- Runs `uv sync` to install project dependencies
- Creates Python virtual environment in `.venv/`

**After Installation:**
```bash
# Verify installation
uv --version

# Install dependencies (if not auto-run)
uv sync

# Run Python scripts with uv
uv run python your_script.py
```

### Step 3: Database Setup

```bash
./dependency_setup/setup_database.sh
```

This script:
- Creates `geocoder` database and user
- Enables PostGIS and PostGIS Topology extensions
- Runs `database/schema.sql` to create tables

### Step 4: Migration Tracking

```bash
# Run migrations with version tracking
uv run python database/run_migrations.py

# Check migration status
uv run python database/run_migrations.py --status

# Preview migrations without applying
uv run python database/run_migrations.py --dry-run
```

The migration runner:
- Tracks applied migrations in `schema_migrations` table
- Computes SHA256 checksums for integrity verification
- Logs timing and success/failure status
- Skips already-applied migrations automatically

**Default Credentials:**
- Database: `geocoder`
- User: `geocoder`
- Password: set via `DB_PASSWORD` — no default; required at setup time
- Host: `localhost`
- Port: `5432`

**Test Connection:**
```bash
PGPASSWORD="$DB_PASSWORD" psql -h localhost -U geocoder -d geocoder
```

### Step 5: Environment Configuration

```bash
./dependency_setup/configure_env.sh
```

This script:
- Creates a mode-0600 development `.env` from `.env.example` with supplied database credentials
- Generates an API pepper and initial admin key without printing them
- Keeps an existing `.env` unchanged; initialization verifies DB connectivity and `/v1/health` verifies Redis

**Generated .env File:**
```bash
DB_HOST=localhost
DB_PORT=5432
DB_NAME=geocoder
DB_USER=geocoder
DB_PASSWORD=your_secure_password_here   # no default; required
REDIS_URL=redis://localhost:6379
```

## Verification

After setup, verify everything is working:

```bash
# Check PostgreSQL
PGPASSWORD="$DB_PASSWORD" psql -h localhost -U geocoder -d geocoder -c "SELECT version();"

# Check PostGIS
PGPASSWORD="$DB_PASSWORD" psql -h localhost -U geocoder -d geocoder -c "SELECT PostGIS_version();"

# Check Redis
redis-cli ping
# Should return: PONG

# Check uv
uv --version

# Check Python environment
uv run python --version
```

## Service Management

### PostgreSQL

```bash
# Status
sudo systemctl status postgresql

# Start/Stop/Restart
sudo systemctl start postgresql
sudo systemctl stop postgresql
sudo systemctl restart postgresql

# View logs
sudo journalctl -u postgresql -f
```

### Redis

```bash
# Status
sudo systemctl status redis-server

# Start/Stop/Restart
sudo systemctl start redis-server
sudo systemctl stop redis-server
sudo systemctl restart redis-server

# Test connection
redis-cli ping

# Monitor commands
redis-cli monitor
```

## Data Import

Once setup is complete, import your geocoding data:

### Import Municipal Data

```bash
# Using wizard mode for new datasets
DB_HOST=localhost uv run python data_sourcing/importers/import_production_data.py \
  --file data_sources/city_data.geojson \
  --source city_name \
  --dry-run

# Import configured datasets (e.g., Guelph)
DB_HOST=localhost uv run python data_sourcing/importers/import_production_data.py \
  --file data_sources/guelph_address_dataset.geojson \
  --source guelph \
  --batch-size 1000
```

### Import NAR Data

```bash
DB_HOST=localhost uv run python data_sourcing/importers/import_nar_data.py \
  --addresses path/to/Address_10.csv \
  --locations path/to/Location_10.csv \
  --province 35 \
  --batch-size 5000
```

### Add FSA Codes

```bash
# Add FSA codes to a dataset's GeoJSON file before importing (PostGIS spatial lookup)
DB_HOST=localhost uv run python data_sourcing/enrichment/add_fsa_to_dataset.py \
  --input data_sources/toronto_address_dataset.geojson \
  --output data_sources/toronto_with_fsa.geojson

# Backfill fsa_code on nar_addresses rows still missing one
DB_HOST=localhost uv run python data_sourcing/enrichment/add_fsa_to_nar.py --db geocoder
```

### Import Audit Trail

All import operations are automatically tracked in the `import_history` table:

```bash
# View recent imports
DB_HOST=localhost psql -d geocoder -c "SELECT * FROM v_latest_imports;"

# Check specific import errors
DB_HOST=localhost psql -d geocoder -c "
  SELECT row_number, error_type, error_message
  FROM import_row_errors
  WHERE import_id = 1
  LIMIT 10;"
```

**Tracked metrics:**
- Import start/completion timestamps
- Rows processed, inserted, updated, failed, skipped
- File path and SHA256 hash for integrity
- Row-level error logging with original data

## Running Scripts

All Python scripts should be run with `DB_HOST=localhost` prefix:

```bash
# General pattern
DB_HOST=localhost uv run python script_name.py

# With PostgreSQL password
DB_HOST=localhost PGPASSWORD="$DB_PASSWORD" psql -h localhost -U geocoder -d geocoder

# Run tests
DB_HOST=localhost uv run pytest tests/ -v
```

## Troubleshooting

### PostgreSQL Connection Refused

```bash
# Check if PostgreSQL is running
sudo systemctl status postgresql

# Check PostgreSQL is listening on localhost
sudo netstat -tlnp | grep 5432

# Check pg_hba.conf allows local connections
sudo nano /etc/postgresql/14/main/pg_hba.conf
# Should have: local all all peer
#              host all all 127.0.0.1/32 md5
```

### Redis Connection Refused

```bash
# Check if Redis is running
sudo systemctl status redis-server

# Check Redis configuration
sudo nano /etc/redis/redis.conf
# Ensure: bind 127.0.0.1 ::1
#         protected-mode yes

# Restart Redis
sudo systemctl restart redis-server
```

### PostGIS Extension Not Found

```bash
# Install PostGIS packages
sudo apt install postgresql-14-postgis-3

# Connect to database and enable
psql -h localhost -U geocoder -d geocoder
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_topology;
```

### Python Import Errors

```bash
# Resync dependencies
uv sync

# Rebuild virtual environment
rm -rf .venv
uv sync

# Check Python version
python3 --version  # Should be 3.11+
```

### uv Command Not Found

```bash
# Source cargo environment
source ~/.cargo/env

# Or add to shell config
echo 'source ~/.cargo/env' >> ~/.bashrc
source ~/.bashrc
```

## Security Considerations

**For Production Use:**

1. **Change Database Password:**
   ```bash
   # In PostgreSQL
   sudo -u postgres psql
   ALTER USER geocoder WITH PASSWORD 'new_secure_password';

   # Update .env file
   nano .env
   # Change DB_PASSWORD=new_secure_password
   ```

2. **Configure PostgreSQL for Remote Access (if needed):**
   ```bash
   # Edit postgresql.conf
   sudo nano /etc/postgresql/14/main/postgresql.conf
   # Change: listen_addresses = '*'

   # Edit pg_hba.conf to allow specific IPs
   sudo nano /etc/postgresql/14/main/pg_hba.conf
   # Add: host geocoder geocoder 10.0.0.0/24 md5

   # Restart PostgreSQL
   sudo systemctl restart postgresql
   ```

3. **Firewall Configuration:**
   ```bash
   # Allow PostgreSQL from specific IP (if needed)
   sudo ufw allow from 10.0.0.0/24 to any port 5432

   # Allow Redis from specific IP (if needed)
   sudo ufw allow from 10.0.0.0/24 to any port 6379
   ```

## Data Source Management

### Initial Data Source Migration

The setup scripts seed `database/seed_data_sources.sql` after tracked migrations. For an
existing database that needs initialization:

```bash
uv run python database/run_migrations.py --initialize --replay-unverified
```

Back up before replaying legacy NULL-checksum migrations. Markdown source lists are descriptive
snapshots, not authoritative initialization inputs. Do not run the legacy markdown migration
over a freshly seeded database: it may replace corrected endpoint metadata.

### Managing Data Sources

Once migrated, use the CLI to manage data sources:

```bash
# List all sources
DB_HOST=localhost uv run python data_sourcing/management/manage_data_sources.py list-sources

# Show detailed info for a source
DB_HOST=localhost uv run python data_sourcing/management/manage_data_sources.py show-source toronto

# Add a new source (auto-regenerates docs)
DB_HOST=localhost uv run python data_sourcing/management/manage_data_sources.py add-source \
  --key new_city \
  --name "New City Open Data" \
  --regions "NewCity" \
  --status potential \
  --portal-url "https://opendata.newcity.ca" \
  --data-types "addresses,parcels"

# Update an existing source
DB_HOST=localhost uv run python data_sourcing/management/manage_data_sources.py update-source toronto \
  --status confirmed \
  --record-count 525409
```

**Documentation auto-regeneration:**
- When you add a source, markdown docs are automatically regenerated
- This keeps `data_sourcing/docs/*.md` in sync with the database
- Commit the regenerated docs to keep git up-to-date

## Data Setup (Automated)

After migrating data sources to the database, use the automated data setup script:

```bash
# Download and import all confirmed data sources
DB_HOST=localhost uv run python data_sourcing/setup_data.py --all-confirmed

# Or download only (no import)
DB_HOST=localhost uv run python data_sourcing/setup_data.py --all-confirmed --download-only

# Or import only (if data already downloaded)
DB_HOST=localhost uv run python data_sourcing/setup_data.py --all-confirmed --import-only

# Skip only sources explicitly marked as manual (NAR downloads automatically)
DB_HOST=localhost uv run python data_sourcing/setup_data.py --all-confirmed --skip-manual

# Process specific source
DB_HOST=localhost uv run python data_sourcing/setup_data.py --source guelph_addresses

# List available sources
DB_HOST=localhost uv run python data_sourcing/setup_data.py --list
```

**Note:** NAR downloads automatically from Statistics Canada. Setup imports every Ontario CSV part
and backfills coordinates/FSA codes. Retain the downloaded files for retry; a nonzero exit means
at least one download/import failed. Review `import_history` before treating coverage as complete.

## Next Steps

After data setup is complete:

1. **Verify data** using the setup script's `--list` option
2. **Test geocoding** with sample data
3. **Review logs** to ensure everything is working
4. **Optimize performance** based on your data size

## Additional Tools

### Useful PostgreSQL Tools

```bash
# Install pgAdmin4 (optional GUI tool)
# See: https://www.pgadmin.org/download/

# Install psql client only (if needed on another machine)
sudo apt install postgresql-client
```

### Useful Redis Tools

```bash
# Install Redis Commander (web GUI)
npm install -g redis-commander
redis-commander

# Or use redis-cli for command line
redis-cli
> INFO
> KEYS *
> GET key_name
```

## Reference

- **PostgreSQL Documentation:** https://www.postgresql.org/docs/14/
- **PostGIS Documentation:** https://postgis.net/documentation/
- **Redis Documentation:** https://redis.io/documentation
- **uv Documentation:** https://github.com/astral-sh/uv

## Support

For project-specific issues, refer to:
- `README.md` - Main project documentation
- `DEPLOYMENT.md` - Deployment architecture
