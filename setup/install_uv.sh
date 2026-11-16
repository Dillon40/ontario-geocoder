#!/bin/bash
set -e

echo "=========================================="
echo "Installing uv Package Manager"
echo "=========================================="
echo ""

# Color codes
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Check if uv is already installed
if command -v uv &> /dev/null; then
    echo -e "${YELLOW}uv is already installed:${NC}"
    uv --version
    echo ""
    read -p "Reinstall uv? (y/N) " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Keeping existing uv installation"
        exit 0
    fi
fi

echo "Installing uv via official installer..."
curl -LsSf https://astral.sh/uv/install.sh | sh

echo ""
echo -e "${GREEN}uv installed successfully!${NC}"
echo ""

# Source the shell configuration to make uv available
if [ -f "$HOME/.cargo/env" ]; then
    source "$HOME/.cargo/env"
fi

# Verify installation
if command -v uv &> /dev/null; then
    echo "uv version:"
    uv --version
    echo ""
    echo -e "${GREEN}✓ uv is ready to use${NC}"
else
    echo -e "${YELLOW}⚠ uv installed but not in PATH yet${NC}"
    echo "Please run: source ~/.cargo/env"
    echo "Or restart your shell"
fi

echo ""
echo "=========================================="
echo "Installing Project Dependencies"
echo "=========================================="
echo ""

# Navigate to project directory (parent of setup/)
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_DIR="$( cd "$SCRIPT_DIR/.." && pwd )"
cd "$PROJECT_DIR"

if [ -f "pyproject.toml" ]; then
    echo "Installing Python dependencies with uv..."
    uv sync
    echo ""
    echo -e "${GREEN}✓ Project dependencies installed${NC}"
else
    echo -e "${YELLOW}⚠ pyproject.toml not found in current directory${NC}"
    echo "Run 'uv sync' from the project root to install dependencies"
fi

echo ""
echo "=========================================="
echo "uv Quick Reference"
echo "=========================================="
echo "  uv sync              - Install/update dependencies"
echo "  uv add <package>     - Add a new dependency"
echo "  uv run <command>     - Run command in project environment"
echo "  uv pip list          - List installed packages"
echo ""
