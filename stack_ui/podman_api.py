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
import queue
import re
import socket
import threading
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


class LogFrames:
    """Turns the chunks of a followed log answer into lines, wherever the chunks end. Without a TTY Podman frames
    every piece of output (8-byte header: stream, 0, 0, 0, size) and a chunk may end inside a header or a body; with
    a TTY the answer is plain text. The first byte tells which (0, 1 or 2 is a frame header), as in demux()."""

    LINE_LIMIT = 32 * 1024   # bytes of one line kept; a program that redraws a progress line with \r and never
                             # prints a newline must not grow the page container's memory without bound

    def __init__(self):
        self.framed = None   # unknown until the first byte arrives
        self.raw = b''       # framed bytes that are not a whole frame yet
        self.text = b''      # output of an unfinished line (at most LINE_LIMIT)
        self.discard = False  # the unfinished line is over the limit: its rest is dropped up to the next newline

    def _lines(self, payload: bytes) -> list[str]:
        """Adds output to the unfinished line; the lines it finishes are returned. One pass, bounded memory."""
        done, pos, size = [], 0, len(payload)
        while pos < size:
            nl = payload.find(b'\n', pos)
            end = size if nl < 0 else nl
            if not self.discard:
                room = self.LINE_LIMIT - len(self.text)
                self.text += payload[pos:min(end, pos + room)]
                if end - pos > room:
                    self.discard = True
            if nl < 0:
                break
            done.append(self.text.decode('utf-8', 'replace').rstrip('\r'))
            self.text, self.discard = b'', False
            pos = nl + 1
        return done

    def feed(self, data: bytes) -> list[str]:
        """The complete lines these bytes finish."""
        if self.framed is None and data:
            self.framed = data[0] in (0, 1, 2)
        if not self.framed:
            return self._lines(data)
        self.raw += data
        done = []
        while len(self.raw) >= 8:
            size = int.from_bytes(self.raw[4:8], 'big')
            if len(self.raw) < 8 + size:
                break
            done += self._lines(self.raw[8:8 + size])
            self.raw = self.raw[8 + size:]
        return done

    def rest(self) -> list[str]:
        """The unfinished last line, when the stream has ended."""
        line, self.text, self.discard = self.text, b'', False
        return [line.decode('utf-8', 'replace').rstrip('\r')] if line else []


_END = object()           # what the reader hands over when the log has ended


class _Failure:
    def __init__(self, message: str):
        self.message = message


def _pump(response, put, stopped) -> None:
    """The reader thread's loop: every complete line goes to put(); the end is _END, a broken read a _Failure."""
    frames = LogFrames()
    try:
        while not stopped():
            chunk = response.read1(65536)
            if not chunk:
                break
            for line in frames.feed(chunk):
                put(line)
        for line in frames.rest():
            put(line)
        put(_END)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        put(_END if stopped() else _Failure(f'the Podman log stream broke: {exc}'))


class LogStream:
    """A followed container log: opened at once (PodmanError if Podman refuses), read by a thread, handed out by
    lines(). close() ends it and frees the thread and the connection."""
    QUEUE_MAX = 1000    # lines waiting for the consumer: a slow one slows the reader instead of filling memory

    def __init__(self, cid: str, tail: int = 200, since: str = ''):
        query = {'stdout': 'true', 'stderr': 'true', 'timestamps': 'true', 'follow': 'true', 'tail': str(int(tail))}
        if since:
            query['since'] = since
        self.queue = queue.Queue(maxsize=self.QUEUE_MAX)
        self.closed = False
        self.conn = _connection(10)
        self.sock = None
        path = f'/containers/{quote(cid, safe="")}/logs'
        try:
            self.conn.request('GET', PREFIX + path + '?' + urlencode(query), headers={})
            self.sock = self.conn.sock           # kept here: for an answer without a length http.client hands the
            self.response = self.conn.getresponse()      # socket to the response and forgets it
            self.sock.settimeout(None)           # a quiet log is not an error: lines() does the timing
        except (OSError, http.client.HTTPException) as exc:
            self.conn.close()
            raise PodmanError(f'the Podman API ({API_URL or SOCKET}) cannot be reached: {exc}') from None
        if self.response.status >= 400:
            raw = self.response.read()
            self.conn.close()
            raise PodmanError(f'Podman GET {path}: HTTP {self.response.status} {_message(raw)[:300]}',
                              self.response.status)
        self.thread = threading.Thread(target=_pump, args=(self.response, self._put, lambda: self.closed), daemon=True)
        self.thread.start()

    def _put(self, item) -> None:
        while not self.closed:
            try:
                self.queue.put(item, timeout=0.5)
                return
            except queue.Full:
                continue

    def lines(self, idle: float = 15.0):
        """Each line as it arrives (str); None after `idle` quiet seconds; ends with the log or after close().
        PodmanError when the stream broke."""
        while True:
            try:
                item = self.queue.get(timeout=idle)
            except queue.Empty:
                if self.closed:
                    return
                yield None
                continue
            if item is _END:
                return
            if isinstance(item, _Failure):
                raise PodmanError(item.message)
            yield item

    def close(self) -> None:
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)         # Linux: wakes a read that waits for the next line
        except (OSError, AttributeError):
            pass
        try:
            socket.socket(fileno=self.sock.detach()).close()     # Windows: only closing the handle wakes it
        except (OSError, AttributeError, ValueError):
            pass
        self.response.close()
        self.conn.close()


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
