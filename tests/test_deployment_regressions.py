"""Regression coverage for deployment, ingestion and API contracts."""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]

def test_native_env_keeps_credentials_private_and_existing_file(tmp_path):
    import os, shutil, subprocess
    scripts=tmp_path/'dependency_setup'; scripts.mkdir()
    script=scripts/'configure_env.sh'
    shutil.copy(ROOT/'dependency_setup/configure_env.sh',script)
    shutil.copy(ROOT/'.env.example',tmp_path/'.env.example')
    env={**os.environ,'DB_PASSWORD':'test_only_hex_password','DB_HOST':'localhost','DB_NAME':'geocoder','DB_USER':'geocoder'}
    run=subprocess.run(['bash',str(script)],env=env,capture_output=True,text=True)
    assert run.returncode==0,run.stderr
    output=(tmp_path/'.env').read_text()
    assert 'DB_PASSWORD=test_only_hex_password' in output
    assert 'INITIAL_ADMIN_KEY=' in output
    assert 'test_only_hex_password' not in run.stdout
    assert (tmp_path/'.env').stat().st_mode & 0o777 == 0o600
    subprocess.run(['bash',str(script)],env=env,check=True,capture_output=True)
    assert (tmp_path/'.env').read_text()==output
