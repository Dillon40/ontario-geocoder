#!/bin/bash
# Native development environment. Docker users copy .env.example directly.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PROJECT_DIR
if [ -e "$PROJECT_DIR/.env" ]; then
    echo 'Keeping existing .env (not overwritten). Review it before starting the API.'
    exit 0
fi
: "${DB_PASSWORD:?Export DB_PASSWORD used to create the native database first}"
export DB_PASSWORD
python3 - <<'PY'
import os, secrets
from pathlib import Path
root = Path(os.environ['PROJECT_DIR'])
values = {
    'DB_HOST': os.getenv('DB_HOST', 'localhost'),
    'DB_PORT': os.getenv('DB_PORT', '5432'),
    'DB_NAME': os.getenv('DB_NAME', 'geocoder'),
    'DB_USER': os.getenv('DB_USER', 'geocoder'),
    'DB_PASSWORD': os.environ['DB_PASSWORD'],
    'REDIS_URL': os.getenv('REDIS_URL', 'redis://localhost:6379'),
    'API_KEY_PEPPER': secrets.token_hex(32),
    'ALLOWED_ORIGINS': 'http://localhost:3000',
    'ENV': 'development',
}
# This file is also consumed by make and native shell setup. Reject values that
# would be interpreted as shell/make syntax instead of silently changing them.
for key, value in values.items():
    if any(c in value for c in '\n\r\t \"\'`$;#\\'):
        raise SystemExit(f'{key} is not shell-safe. Use generated hex credentials for native setup.')
lines=[]
for line in (root / '.env.example').read_text().splitlines():
    key=line.split('=',1)[0]
    if key in values: line=f'{key}={values[key]}'
    if line.startswith('# INITIAL_ADMIN_KEY='):
        line='INITIAL_ADMIN_KEY='+secrets.token_hex(32)
    lines.append(line)
fd=os.open(root / '.env', os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
with os.fdopen(fd,'w') as f: f.write('\n'.join(lines)+'\n')
print('Created mode-0600 native development .env; secrets not printed.')
PY
printf '%s\n' 'Connection checks: run initialize_geocoder.sh, then check /v1/health.'
