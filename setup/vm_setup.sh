#!/bin/bash
set -e

echo "=========================================="
echo "Geocoder Project VM Setup"
echo "=========================================="
echo ""

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Check if running as root
if [ "$EUID" -eq 0 ]; then
    echo -e "${RED}Please do not run this script as root or with sudo${NC}"
    echo "The script will prompt for sudo when needed"
    exit 1
fi

# Detect OS and package manager
detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS_ID="$ID"
        OS_ID_LIKE="$ID_LIKE"
        OS_VERSION_ID="$VERSION_ID"
        OS_PRETTY_NAME="$PRETTY_NAME"
    elif [ -f /etc/redhat-release ]; then
        OS_ID="rhel"
        OS_PRETTY_NAME=$(cat /etc/redhat-release)
    elif [ -f /etc/debian_version ]; then
        OS_ID="debian"
        OS_PRETTY_NAME="Debian $(cat /etc/debian_version)"
    else
        OS_ID="unknown"
        OS_PRETTY_NAME="Unknown"
    fi

    # Detect package manager
    if command -v dnf &> /dev/null; then
        PKG_MANAGER="dnf"
        PKG_INSTALL="sudo dnf install -y"
        PKG_UPDATE="sudo dnf check-update || true"  # dnf check-update returns 100 if updates available
        PKG_UPGRADE="sudo dnf upgrade -y"
    elif command -v yum &> /dev/null; then
        PKG_MANAGER="yum"
        PKG_INSTALL="sudo yum install -y"
        PKG_UPDATE="sudo yum check-update || true"
        PKG_UPGRADE="sudo yum upgrade -y"
    elif command -v apt-get &> /dev/null; then
        PKG_MANAGER="apt"
        PKG_INSTALL="sudo apt-get install -y"
        PKG_UPDATE="sudo apt-get update"
        PKG_UPGRADE="sudo apt-get upgrade -y"
    elif command -v pacman &> /dev/null; then
        PKG_MANAGER="pacman"
        PKG_INSTALL="sudo pacman -S --noconfirm"
        PKG_UPDATE="sudo pacman -Sy"
        PKG_UPGRADE="sudo pacman -Syu --noconfirm"
    elif command -v zypper &> /dev/null; then
        PKG_MANAGER="zypper"
        PKG_INSTALL="sudo zypper install -y"
        PKG_UPDATE="sudo zypper refresh"
        PKG_UPGRADE="sudo zypper update -y"
    elif command -v apk &> /dev/null; then
        PKG_MANAGER="apk"
        PKG_INSTALL="sudo apk add"
        PKG_UPDATE="sudo apk update"
        PKG_UPGRADE="sudo apk upgrade"
    else
        echo -e "${RED}No supported package manager found${NC}"
        echo "Supported: dnf, yum, apt, pacman, zypper, apk"
        exit 1
    fi

    echo -e "${BLUE}Detected OS:${NC} $OS_PRETTY_NAME"
    echo -e "${BLUE}Package Manager:${NC} $PKG_MANAGER"
}

# Get package names for each package manager
get_packages() {
    case $PKG_MANAGER in
        dnf|yum)
            # RHEL/CentOS/Fedora/AlmaLinux/Rocky
            POSTGRES_PKGS="postgresql-server postgresql-contrib"
            POSTGIS_PKGS="postgis34_16"  # PostGIS 3.4 for PostgreSQL 16
            REDIS_PKGS="redis"
            PYTHON_PKGS="python3.11 python3.11-devel python3-pip"
            BUILD_PKGS="gcc gcc-c++ make libpq-devel gdal gdal-devel"
            UTIL_PKGS="curl wget git unzip"
            REDIS_SERVICE="redis"

            # Check if we need EPEL and PostgreSQL repos
            NEEDS_REPOS=true
            ;;
        apt)
            # Debian/Ubuntu
            POSTGRES_PKGS="postgresql postgresql-contrib"
            POSTGIS_PKGS="postgis postgresql-16-postgis-3"
            REDIS_PKGS="redis-server"
            PYTHON_PKGS="python3.11 python3.11-venv python3.11-dev python3-pip"
            BUILD_PKGS="build-essential libpq-dev gdal-bin libgdal-dev"
            UTIL_PKGS="curl wget git unzip"
            REDIS_SERVICE="redis-server"
            NEEDS_REPOS=false
            ;;
        pacman)
            # Arch Linux
            POSTGRES_PKGS="postgresql"
            POSTGIS_PKGS="postgis"
            REDIS_PKGS="redis"
            PYTHON_PKGS="python python-pip"
            BUILD_PKGS="base-devel postgresql-libs gdal"
            UTIL_PKGS="curl wget git unzip"
            REDIS_SERVICE="redis"
            NEEDS_REPOS=false
            ;;
        zypper)
            # openSUSE
            POSTGRES_PKGS="postgresql-server postgresql-contrib"
            POSTGIS_PKGS="postgis"
            REDIS_PKGS="redis"
            PYTHON_PKGS="python311 python311-devel python3-pip"
            BUILD_PKGS="gcc gcc-c++ make postgresql-devel gdal gdal-devel"
            UTIL_PKGS="curl wget git unzip"
            REDIS_SERVICE="redis"
            NEEDS_REPOS=false
            ;;
        apk)
            # Alpine
            POSTGRES_PKGS="postgresql postgresql-contrib"
            POSTGIS_PKGS="postgis"
            REDIS_PKGS="redis"
            PYTHON_PKGS="python3 python3-dev py3-pip"
            BUILD_PKGS="build-base postgresql-dev gdal gdal-dev"
            UTIL_PKGS="curl wget git unzip"
            REDIS_SERVICE="redis"
            NEEDS_REPOS=false
            ;;
    esac
}

# Setup required repositories for RHEL-based systems
setup_rhel_repos() {
    echo -e "${GREEN}Setting up required repositories...${NC}"

    # Enable EPEL repository
    if ! rpm -q epel-release &> /dev/null; then
        echo "Installing EPEL repository..."
        $PKG_INSTALL epel-release
    else
        echo "EPEL repository already installed"
    fi

    # Enable CRB/PowerTools for development packages
    if [ "$PKG_MANAGER" = "dnf" ]; then
        echo "Enabling CRB repository..."
        sudo dnf config-manager --set-enabled crb 2>/dev/null || \
        sudo dnf config-manager --set-enabled powertools 2>/dev/null || \
        echo "CRB/PowerTools may already be enabled or not available"
    fi

    # Install PostgreSQL repository for latest version
    if ! rpm -q pgdg-redhat-repo &> /dev/null; then
        echo "Installing PostgreSQL repository..."
        sudo dnf install -y https://download.postgresql.org/pub/repos/yum/reporpms/EL-9-x86_64/pgdg-redhat-repo-latest.noarch.rpm 2>/dev/null || \
        sudo dnf install -y https://download.postgresql.org/pub/repos/yum/reporpms/EL-8-x86_64/pgdg-redhat-repo-latest.noarch.rpm 2>/dev/null || \
        echo "PostgreSQL repo may already be installed"

        # Disable built-in PostgreSQL module to use PGDG
        sudo dnf -qy module disable postgresql 2>/dev/null || true
    else
        echo "PostgreSQL repository already installed"
    fi
}

# Initialize PostgreSQL on RHEL-based systems
init_postgres_rhel() {
    # Find the correct PostgreSQL version installed
    PG_VERSION=$(ls /usr/pgsql-* 2>/dev/null | grep -oP '\d+' | sort -rn | head -1)
    if [ -z "$PG_VERSION" ]; then
        PG_VERSION="16"  # Default to 16 if detection fails
    fi

    PG_DATA="/var/lib/pgsql/${PG_VERSION}/data"
    PG_SERVICE="postgresql-${PG_VERSION}"

    if [ ! -f "$PG_DATA/PG_VERSION" ]; then
        echo "Initializing PostgreSQL ${PG_VERSION} database..."
        sudo /usr/pgsql-${PG_VERSION}/bin/postgresql-${PG_VERSION}-setup initdb
    else
        echo "PostgreSQL database already initialized"
    fi

    # Update pg_hba.conf to allow password authentication
    if [ -f "$PG_DATA/pg_hba.conf" ]; then
        # Backup original
        sudo cp "$PG_DATA/pg_hba.conf" "$PG_DATA/pg_hba.conf.bak" 2>/dev/null || true

        # Check if we need to update authentication method
        if grep -q "ident" "$PG_DATA/pg_hba.conf"; then
            echo "Updating PostgreSQL authentication to md5..."
            sudo sed -i 's/ident/md5/g' "$PG_DATA/pg_hba.conf"
            sudo sed -i 's/peer/md5/g' "$PG_DATA/pg_hba.conf"
        fi
    fi

    echo "$PG_SERVICE"
}

# Detect OS and set up variables
detect_os
get_packages

echo ""
echo "This script will install:"
echo "  - PostgreSQL 16+ with PostGIS extension"
echo "  - Redis server"
echo "  - Python 3.11+"
echo "  - Build essentials and development tools"
echo ""
read -p "Continue with installation? (y/N) " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Installation cancelled"
    exit 1
fi

echo ""
echo -e "${GREEN}[1/6] Updating package database...${NC}"
$PKG_UPDATE

# Setup repositories for RHEL-based systems
if [ "$NEEDS_REPOS" = true ]; then
    echo ""
    echo -e "${GREEN}[2/6] Setting up repositories...${NC}"
    setup_rhel_repos
else
    echo ""
    echo -e "${YELLOW}[2/6] Skipping repository setup (not needed for $PKG_MANAGER)${NC}"
fi

echo ""
echo -e "${GREEN}[3/6] Installing PostgreSQL...${NC}"
case $PKG_MANAGER in
    dnf|yum)
        # Install specific PostgreSQL version from PGDG
        $PKG_INSTALL postgresql16-server postgresql16-contrib
        ;;
    *)
        $PKG_INSTALL $POSTGRES_PKGS
        ;;
esac

echo ""
echo -e "${GREEN}[4/6] Installing PostGIS, Redis, and Python...${NC}"
case $PKG_MANAGER in
    dnf|yum)
        # PostGIS for PostgreSQL 16
        $PKG_INSTALL postgis34_16 || $PKG_INSTALL postgis33_16 || echo -e "${YELLOW}PostGIS installation may need manual setup${NC}"
        $PKG_INSTALL $REDIS_PKGS
        $PKG_INSTALL $PYTHON_PKGS || $PKG_INSTALL python3 python3-devel python3-pip
        ;;
    *)
        $PKG_INSTALL $POSTGIS_PKGS || echo -e "${YELLOW}PostGIS installation may need manual setup${NC}"
        $PKG_INSTALL $REDIS_PKGS
        $PKG_INSTALL $PYTHON_PKGS
        ;;
esac

echo ""
echo -e "${GREEN}[5/6] Installing build tools and utilities...${NC}"
$PKG_INSTALL $BUILD_PKGS || echo -e "${YELLOW}Some build packages may not be available${NC}"
$PKG_INSTALL $UTIL_PKGS

echo ""
echo -e "${GREEN}[6/6] Configuring services...${NC}"

# Initialize and start PostgreSQL
case $PKG_MANAGER in
    dnf|yum)
        PG_SERVICE=$(init_postgres_rhel)
        ;;
    pacman)
        # Arch needs manual init
        if [ ! -d /var/lib/postgres/data ]; then
            sudo -u postgres initdb -D /var/lib/postgres/data
        fi
        PG_SERVICE="postgresql"
        ;;
    *)
        PG_SERVICE="postgresql"
        ;;
esac

# Start PostgreSQL
echo "Starting PostgreSQL..."
if sudo systemctl start "$PG_SERVICE" 2>/dev/null; then
    sudo systemctl enable "$PG_SERVICE"
    echo -e "${GREEN}✓ PostgreSQL is running${NC}"
else
    # Try alternative service name
    if sudo systemctl start postgresql 2>/dev/null; then
        sudo systemctl enable postgresql
        echo -e "${GREEN}✓ PostgreSQL is running${NC}"
    else
        echo -e "${YELLOW}⚠ Could not start PostgreSQL automatically${NC}"
        echo "  You may need to start it manually"
    fi
fi

# Start Redis
echo "Starting Redis..."
if sudo systemctl start "$REDIS_SERVICE" 2>/dev/null; then
    sudo systemctl enable "$REDIS_SERVICE"
    echo -e "${GREEN}✓ Redis is running${NC}"
else
    # Try alternative service name
    if sudo systemctl start redis-server 2>/dev/null; then
        sudo systemctl enable redis-server
        echo -e "${GREEN}✓ Redis is running${NC}"
    else
        echo -e "${YELLOW}⚠ Could not start Redis automatically${NC}"
        echo "  You may need to start it manually"
    fi
fi

echo ""
echo "=========================================="
echo -e "${GREEN}System packages installed successfully!${NC}"
echo "=========================================="
echo ""

# Verify installations
echo "Verifying installations:"
echo ""

# PostgreSQL
if command -v psql &> /dev/null; then
    echo -e "${GREEN}✓ PostgreSQL:${NC} $(psql --version 2>/dev/null || echo 'installed')"
elif [ -f /usr/pgsql-16/bin/psql ]; then
    echo -e "${GREEN}✓ PostgreSQL:${NC} $(/usr/pgsql-16/bin/psql --version)"
    echo -e "${YELLOW}  Note: Add /usr/pgsql-16/bin to your PATH${NC}"
    echo "  Run: echo 'export PATH=/usr/pgsql-16/bin:\$PATH' >> ~/.bashrc && source ~/.bashrc"
else
    echo -e "${YELLOW}⚠ PostgreSQL: not found in PATH${NC}"
fi

# Redis
if command -v redis-cli &> /dev/null; then
    echo -e "${GREEN}✓ Redis:${NC} $(redis-cli --version)"
else
    echo -e "${YELLOW}⚠ Redis: not found in PATH${NC}"
fi

# Python
if command -v python3.11 &> /dev/null; then
    echo -e "${GREEN}✓ Python:${NC} $(python3.11 --version)"
elif command -v python3 &> /dev/null; then
    echo -e "${GREEN}✓ Python:${NC} $(python3 --version)"
else
    echo -e "${YELLOW}⚠ Python: not found${NC}"
fi

echo ""
echo "=========================================="
echo "Next Steps"
echo "=========================================="
echo "1. If PostgreSQL binaries are not in PATH, add them:"
echo "   export PATH=/usr/pgsql-16/bin:\$PATH"
echo ""
echo "2. Run ./install_uv.sh to install uv package manager"
echo "3. Run ./setup_database.sh to configure PostgreSQL database"
echo "4. Run ./configure_env.sh to set up environment variables"
echo ""
