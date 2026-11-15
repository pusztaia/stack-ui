"""The settings file (the API token's hash) and the page password's hash."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
from pathlib import Path

from . import __version__
from .core import DATA

HASH_ITERATIONS = 600_000   # new hashes; an older hash keeps the count it was made with
SETTINGS_FILE = Path(os.environ.get('STACK_UI_SETTINGS_FILE') or DATA / 'settings.json')
SETTINGS_LOCK = threading.Lock()


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_settings(new: dict) -> None:
    """Replace the settings file atomically, 0600 (call with SETTINGS_LOCK held)."""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_name(f'.{SETTINGS_FILE.name}.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        json.dump(new, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, SETTINGS_FILE)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def api_token(data: dict) -> dict:
    """A new API token for the deploy scripts (shown once; only its SHA-256 is kept), or {revoke: true}."""
    with SETTINGS_LOCK:
        current = load_settings()
        token = '' if data.get('revoke') else secrets.token_urlsafe(32)
        if token:
            current['api_token_sha256'] = token_hash(token)
        else:
            current.pop('api_token_sha256', None)
        write_settings(current)
    return {'token': token, 'tokenSet': bool(token)}


def bearer_ok(header: str) -> bool:
    if not header.startswith('Bearer '):
        return False
    stored = str(load_settings().get('api_token_sha256') or '')
    return bool(stored) and hmac.compare_digest(token_hash(header[7:].strip()), stored)


def settings_view() -> dict:
    return {'tokenSet': bool(load_settings().get('api_token_sha256')), 'version': __version__}


def hash_password(password: str, iterations: int = HASH_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)
    return f'pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}'


def read_password_hash(path: Path) -> str:
    """The last pbkdf2_sha256$ line of the file (prompts or CRs around it are ignored); '' when there is none."""
    lines = [x.strip() for x in path.read_text(encoding='utf-8', errors='replace').splitlines()]
    return next((x for x in reversed(lines) if x.startswith('pbkdf2_sha256$')), '')


def check_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = stored.strip().split('$')
        if algo != 'pbkdf2_sha256':
            return False
        got = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(got, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False
