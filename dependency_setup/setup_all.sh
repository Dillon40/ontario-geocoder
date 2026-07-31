#!/bin/bash
set -e

echo "=========================================="
echo "Complete VM Setup - Geocoder Project"
echo "=========================================="
echo ""
echo "This will run all setup scripts in sequence:"
echo "  1. System packages (PostgreSQL, Redis, Python)"
echo "  2. uv package manager"
echo "  3. PostgreSQL database initialization"
echo "  4. Environment configuration"
echo ""
read -p "Continue with complete setup? (y/N) " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Setup cancelled"
    exit 1
fi

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

echo ""
echo "=========================================="
echo "Step 1/4: System Packages"
echo "=========================================="
bash "$SCRIPT_DIR/vm_setup.sh"

echo ""
echo "=========================================="
echo "Step 2/4: uv Package Manager"
echo "=========================================="
# Source cargo env in case it was just installed
[ -f "$HOME/.cargo/env" ] && source "$HOME/.cargo/env"
bash "$SCRIPT_DIR/install_uv.sh"

echo ""
echo "=========================================="
echo "Step 3/4: Database Setup"
echo "=========================================="
bash "$SCRIPT_DIR/setup_database.sh"

echo ""
echo "=========================================="
echo "Step 4/4: Environment Configuration"
echo "=========================================="
bash "$SCRIPT_DIR/configure_env.sh"

echo ""
echo "=========================================="
echo "Setup Complete!"
echo "=========================================="
echo ""
echo "All components have been installed and configured."
echo ""
echo "Next steps:"
echo "  1. Source your shell config: source ~/.bashrc (or restart shell)"
echo "  2. Review the .env file: nano .env"
echo "  3. Run migrations: ./dependency_setup/initialize_geocoder.sh"
echo "  4. Import data: uv run data_sourcing/setup_data.py --all-confirmed"
echo ""
echo "For detailed documentation, see docs/VM_SETUP.md"
echo ""
