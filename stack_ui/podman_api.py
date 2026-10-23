"""Podman's own REST API (libpod) on the host's socket: containers, images, networks, volumes, secrets, logs.

Used by stack-ui in its container (the host's rootful socket, /run/podman/podman.sock, is mounted in) and by
stacks.py. Standard library only; no stack logic here. PODMAN_SOCKET names the socket; PODMAN_API_URL
(http://host:port) replaces it with plain TCP (the tests' mock server). Errors are PodmanError, with the HTTP
status in .code (0 when the socket cannot be reached). A secret's value only goes in, never comes back.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import socket
from urllib.parse import quote, urlencode, urlsplit

SOCKET = os.environ.get('PODMAN_SOCKET', '/run/podman/podman.sock')
API_URL = os.environ.get('PODMAN_API_URL', '').rstrip('/')
PREFIX = '/v4.0.0/libpod'  # Podman 4 and 5 both answer this version
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$')  # networks, volumes, secrets
MAX_SECRET = 64 * 1024


class PodmanError(Exception):
    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.code = code


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__('localhost', timeout=timeout)
        self.socket_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def configured() -> bool:
    """A Podman API to talk to: PODMAN_API_URL, or the socket file exists."""
    return bool(API_URL) or os.path.exists(SOCKET)


def check_name(name: str, what: str = 'name') -> str:
    if not isinstance(name, str) or not NAME.match(name):
        raise ValueError(f'the {what}: letters, digits, _ . - (at most 63, starting with a letter or digit)')
    return name


def _connection(timeout: float) -> http.client.HTTPConnection:
    if API_URL:
        u = urlsplit(API_URL)
        return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    return UnixConnection(SOCKET, timeout)


def _message(raw: bytes) -> str:
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and data.get('message'):
            return str(data['message'])
    except ValueError:
        pass
    return raw.decode('utf-8', 'replace')


def request(method: str, path: str, *, query: dict | None = None, body=None, data: bytes | None = None,
            timeout: float = 30) -> bytes:
    """One libpod call; the raw answer. body is sent as JSON, data as raw bytes (a secret's value)."""
    url = PREFIX + path + ('?' + urlencode(query) if query else '')
    payload = data if data is not None else json.dumps(body).encode('utf-8') if body is not None else None
    headers = {'Content-Type': 'application/octet-stream' if data is not None else 'application/json'}
    try:
        conn = _connection(timeout)
        try:
            conn.request(method, url, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        finally:
            conn.close()
    except (OSError, http.client.HTTPException) as exc:
        raise PodmanError(f'the Podman API ({API_URL or SOCKET}) cannot be reached: {exc}. On the host: '
                          'sudo systemctl enable --now podman.socket, then restart stack-ui') from None
    if resp.status >= 400:
        raise PodmanError(f'Podman {method} {path}: HTTP {resp.status} {_message(raw)[:300]}', resp.status)
    return raw


def call(method: str, path: str, **kw):
    raw = request(method, path, **kw)
    return json.loads(raw) if raw.strip() else None


def version() -> str:
    return str((call('GET', '/version') or {}).get('Version') or '')


def cname(c: dict) -> str:
    return (c.get('Names') or ['?'])[0].lstrip('/')


# ---------- containers ----------

def containers(label: str = '') -> list[dict]:
    """Every container (also the stopped ones); label 'key=value' filters."""
    query = {'all': 'true'}
    if label:
        query['filters'] = json.dumps({'label': [label]})
    return call('GET', '/containers/json', query=query) or []


def container_action(cid: str, action: str) -> None:
    if action not in ('start', 'stop', 'restart'):
        raise ValueError(f'unknown container action {action}')
    query = {'timeout': '30'} if action == 'stop' else {'t': '30'} if action == 'restart' else None
    request('POST', f'/containers/{quote(cid, safe="")}/{action}', query=query, timeout=90)


def container_stats() -> list[dict]:
    """One reading of every running container's resource use. The CPU fields are totals since the container
    started (CPUNano: its CPU time, SystemNano: the time of the reading), so a percentage needs two readings."""
    return (call('GET', '/containers/stats', query={'stream': 'false'}) or {}).get('Stats') or []


def remove_container(cid: str) -> None:
    request('DELETE', f'/containers/{quote(cid, safe="")}', query={'force': 'true'}, timeout=90)


def inspect_container(cid: str) -> dict:
    return call('GET', f'/containers/{quote(cid, safe="")}/json') or {}


def demux(raw: bytes) -> str:
    """A log answer as text: without a TTY Podman frames every chunk (8-byte header: stream, 0, 0, 0, size)."""
    if len(raw) >= 8 and raw[0] in (0, 1, 2) and raw[1:4] == b'\0\0\0':
        out, i = [], 0
        while i + 8 <= len(raw):
            size = int.from_bytes(raw[i + 4:i + 8], 'big')
            out.append(raw[i + 8:i + 8 + size])
            i += 8 + size
        raw = b''.join(out)
    return raw.decode('utf-8', 'replace')


def logs(cid: str, tail: int = 200) -> list[str]:
    raw = request('GET', f'/containers/{quote(cid, safe="")}/logs',
                  query={'stdout': 'true', 'stderr': 'true', 'timestamps': 'true', 'tail': str(int(tail))})
    return demux(raw).splitlines()


# ---------- images ----------

def images() -> list[dict]:
    return call('GET', '/images/json') or []


def image(iid: str) -> dict:
    return call('GET', f'/images/{quote(iid, safe="")}/json') or {}


def remove_image(iid: str, force: bool = False) -> None:
    """force: also an image with several tags (the caller checked that no container uses it)."""
    request('DELETE', f'/images/{quote(iid, safe="")}', query={'force': 'true'} if force else None, timeout=90)


def pull_image(reference: str) -> None:
    """Pull an image (anonymous). The answer is a stream of JSON lines; one with "error" means it failed, even
    though the HTTP status is 200."""
    raw = request('POST', '/images/pull', query={'reference': reference, 'quiet': 'false'}, timeout=900)
    for line in raw.decode('utf-8', 'replace').splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get('error'):
            raise PodmanError(f'pull {reference}: {str(item["error"])[:300]}')


def tag_image(iid: str, repo: str, tag: str) -> None:
    request('POST', f'/images/{quote(iid, safe="")}/tag', query={'repo': repo, 'tag': tag})


# ---------- networks ----------

def networks() -> list[dict]:
    return call('GET', '/networks/json') or []


def create_network(name: str, subnet: str = '', gateway: str = '') -> dict:
    body = {'name': check_name(name, 'network name'), 'driver': 'bridge'}
    if subnet:
        ipaddress.ip_network(subnet)            # ValueError: not a subnet
        entry = {'subnet': subnet}
        if gateway:
            ipaddress.ip_address(gateway)
            entry['gateway'] = gateway
        body['subnets'] = [entry]
    elif gateway:
        raise ValueError('a gateway needs a subnet')
    return call('POST', '/networks/create', body=body) or {}


def remove_network(name: str) -> None:
    request('DELETE', f'/networks/{quote(check_name(name, "network name"), safe="")}')


# ---------- volumes ----------

def volumes() -> list[dict]:
    return call('GET', '/volumes/json') or []


def create_volume(name: str) -> dict:
    return call('POST', '/volumes/create', body={'Name': check_name(name, 'volume name')}) or {}


def remove_volume(name: str) -> None:
    request('DELETE', f'/volumes/{quote(check_name(name, "volume name"), safe="")}')


def volume_users(name: str) -> list[str]:
    found = call('GET', '/containers/json', query={'all': 'true', 'filters': json.dumps({'volume': [name]})}) or []
    return sorted(cname(c) for c in found)


# ---------- secrets ----------

def secrets() -> list[dict]:
    """Every Podman secret: {name, id, created, updated}; never its value."""
    return [{'name': str((s.get('Spec') or {}).get('Name') or ''), 'id': str(s.get('ID') or ''),
             'created': str(s.get('CreatedAt') or ''), 'updated': str(s.get('UpdatedAt') or '')}
            for s in call('GET', '/secrets/json') or []]


def create_secret(name: str, value: bytes, replace: bool = False) -> None:
    check_name(name, 'secret name')
    if not value:
        raise ValueError('the secret is empty')
    if len(value) > MAX_SECRET:
        raise ValueError('the secret is larger than 64 KB')
    query = {'name': name, **({'replace': 'true'} if replace else {})}
    request('POST', '/secrets/create', query=query, data=value)


def remove_secret(name: str) -> None:
    request('DELETE', f'/secrets/{quote(check_name(name, "secret name"), safe="")}')
