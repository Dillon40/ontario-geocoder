# Geocoder - Project Makefile
# Run `make help` for available targets

.DEFAULT_GOAL := help
SHELL := /bin/bash

# ---------------------------------------------------------------------------
# Configuration (override with environment or .env)
# ---------------------------------------------------------------------------
-include .env
export

DB_HOST   ?= localhost
DB_PORT   ?= 5432
DB_NAME   ?= geocoder
DB_USER   ?= geocoder
# No default password: falls back to DB_PASSWORD from .env; database targets
# fail with a clear error if neither DB_PASS nor DB_PASSWORD is set.
DB_PASS   ?= $(DB_PASSWORD)
PSQL       = { [ -n "$(DB_PASS)" ] || { echo "ERROR: DB_PASS is not set - define DB_PASSWORD in .env or pass DB_PASS=..."; exit 1; }; } && PGPASSWORD=$(DB_PASS) psql -h $(DB_HOST) -p $(DB_PORT) -U $(DB_USER) -d $(DB_NAME)

# ---------------------------------------------------------------------------
# Setup targets
# ---------------------------------------------------------------------------

.PHONY: setup
setup: ## First-time setup: system packages, uv, database, env, seeds
	@echo "=== Full System Setup ==="
	bash setup/setup_all.sh
	bash setup/initialize_geocoder.sh

.PHONY: setup-db
setup-db: ## Create database, user, extensions, apply schema + migrations
	bash setup/setup_database.sh

.PHONY: setup-env
setup-env: ## Generate .env from template
	bash setup/configure_env.sh

.PHONY: setup-python
setup-python: ## Install uv and sync Python dependencies
	bash setup/install_uv.sh

# ---------------------------------------------------------------------------
# Database targets
# ---------------------------------------------------------------------------

.PHONY: db-schema
db-schema: ## Apply schema.sql (idempotent)
	$(PSQL) -v ON_ERROR_STOP=1 -f database/schema.sql

.PHONY: db-migrate
db-migrate: ## Run all database migrations
	uv run python database/run_migrations.py

.PHONY: db-seed
db-seed: ## Seed the data_sources table
	$(PSQL) -v ON_ERROR_STOP=1 -f database/seed_data_sources.sql

.PHONY: db-init
db-init: db-schema db-migrate db-seed ## Schema + migrations + seed (for existing database)
	@echo "Database initialized."

.PHONY: db-status
db-status: ## Show table row counts and migration status
	@$(PSQL) -c "\
		SELECT t.table_name, \
		       COALESCE(s.n_live_tup, 0) AS row_estimate \
		FROM information_schema.tables t \
		LEFT JOIN pg_stat_user_tables s ON s.relname = t.table_name \
		WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE' \
		ORDER BY COALESCE(s.n_live_tup, 0) DESC;"
	@echo ""
	@$(PSQL) -c "SELECT migration_number, migration_name, applied_at::date \
		FROM schema_migrations ORDER BY migration_number;" 2>/dev/null || true

.PHONY: db-shell
db-shell: ## Open psql shell
	$(PSQL)

# ---------------------------------------------------------------------------
# Data import targets
# ---------------------------------------------------------------------------

.PHONY: import-all
import-all: ## Download + import all confirmed sources (interactive consent)
	uv run python data_sourcing/setup_data.py --all-confirmed

.PHONY: import-all-auto
import-all-auto: ## Download + import all sources (auto-accept non-commercial)
	uv run python data_sourcing/setup_data.py --all-confirmed --accept-non-commercial

.PHONY: import-only
import-only: ## Import from already-downloaded files (no download step)
	uv run python data_sourcing/setup_data.py --all-confirmed --import-only --accept-non-commercial

.PHONY: import-commercial
import-commercial: ## Import only commercially-licensed sources
	uv run python data_sourcing/setup_data.py --all-confirmed --commercial-only

.PHONY: import-source
import-source: ## Import a single source (usage: make import-source SRC=guelph_addresses)
	@if [ -z "$(SRC)" ]; then echo "Usage: make import-source SRC=<source_key>"; exit 1; fi
	uv run python data_sourcing/setup_data.py --source $(SRC) --accept-non-commercial

.PHONY: import-nar
import-nar: ## Import NAR Ontario (files must be in data_sourcing/data_sources/nar_ontario/)
	uv run python data_sourcing/setup_data.py --source nar_ontario --import-only --accept-non-commercial

.PHONY: list-sources
list-sources: ## List all confirmed data sources and their status
	uv run python data_sourcing/setup_data.py --list

# ---------------------------------------------------------------------------
# Run targets
# ---------------------------------------------------------------------------

.PHONY: dev
dev: ## Start API server in development mode (auto-reload)
	uv run uvicorn api.main:app --no-proxy-headers --reload --host 0.0.0.0 --port 3000

.PHONY: prod
prod: ## Start API server in production mode (gunicorn)
	uv run gunicorn api.main:app \
		--bind 0.0.0.0:3000 \
		--workers 4 \
		--worker-class api.worker.GeocoderWorker \
		--timeout 120

# ---------------------------------------------------------------------------
# Docker targets
# ---------------------------------------------------------------------------

.PHONY: docker-up
docker-up: ## Start full stack via Docker Compose (postgres, redis, api)
	docker compose -f docker-compose.prod.yml up -d

.PHONY: docker-down
docker-down: ## Stop Docker Compose stack
	docker compose -f docker-compose.prod.yml down

.PHONY: docker-logs
docker-logs: ## Tail Docker Compose logs
	docker compose -f docker-compose.prod.yml logs -f

.PHONY: docker-etl
ETL_ARGS ?= --all-confirmed --accept-non-commercial

docker-etl: ## Run ETL inside Docker (override ETL_ARGS for a source, --list or --help)
	docker compose -f docker-compose.prod.yml run --rm etl \
		data_sourcing.setup_data $(ETL_ARGS)

# ---------------------------------------------------------------------------
# Test & verify targets
# ---------------------------------------------------------------------------

.PHONY: test
test: ## Run all tests
	uv run pytest

.PHONY: test-parser
test-parser: ## Run address parser tests
	uv run pytest tests/engine/test_address_parser.py -v

.PHONY: test-transformers
test-transformers: ## Run transformer tests
	uv run pytest tests/data_sourcing/transformers/ -v

.PHONY: verify
verify: ## Run ETL verification (compare verify DB vs prod)
	uv run python data_sourcing/management/verify_etl_pipeline.py --diff --report etl_verification_report.txt

.PHONY: verify-quick
verify-quick: ## Quick count-only verification (no hash comparison)
	uv run python data_sourcing/management/verify_etl_pipeline.py --quick

.PHONY: verify-skip-etl
verify-skip-etl: ## Re-compare existing verify DB without re-running ETL
	uv run python data_sourcing/management/verify_etl_pipeline.py --skip-etl --diff --report etl_verification_report.txt

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

.PHONY: clean-data
clean-data: ## Remove downloaded data files (keeps NAR and boundary files)
	@echo "Removing downloaded GeoJSON files (keeping NAR, FSA, municipal boundaries)..."
	@for dir in data_sourcing/data_sources/*/; do \
		name=$$(basename "$$dir"); \
		case "$$name" in \
			nar_ontario|fsa_boundaries|ontario_municipal_boundaries|postal_db) \
				echo "  KEEP: $$name";; \
			*) \
				echo "  DELETE: $$name"; \
				rm -rf "$$dir";; \
		esac; \
	done
	@echo "Done. Run 'make import-all' to re-download."

.PHONY: disk
disk: ## Show disk usage for data files and database
	@echo "=== Data files ==="
	@du -sh data_sourcing/data_sources/ 2>/dev/null || echo "  (no data files)"
	@echo ""
	@echo "=== Database ==="
	@$(PSQL) -c "SELECT pg_size_pretty(pg_database_size('$(DB_NAME)')) AS db_size;" 2>/dev/null || echo "  (cannot connect)"
	@echo ""
	@echo "=== Disk ==="
	@df -h / | tail -1

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

.PHONY: help
help: ## Show this help
	@echo "Geocoder - Available targets:"
	@echo ""
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' Makefile | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Quick start (new machine):"
	@echo "  make setup          # install everything"
	@echo "  make import-all     # download + import data"
	@echo "  make dev            # start API server"
