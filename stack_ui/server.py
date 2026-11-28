"""Stack UI: a web page for the compose stacks of a Podman host (podman-compose through the Podman socket).

    python -m stack_ui                  # in the container (see compose.example.yaml)
    python -m stack_ui --hash-password  # prints the password hash for the stack-ui-password Podman secret

Every subfolder of STACKS_DIR with a compose file is a stack (see stacks.py). The page lists the stacks and their
containers, edits their compose and env files (each save keeps the previous version in .history/; a save shows
its diff first), creates, updates, starts, stops and deletes them with podman-compose (containers in a new
stack's way are removed only when the page confirms it), uploads new stacks (*Add stack*), compares a stack with
what was deployed last, checks the registries for newer images, and shows Containers, Images, Logs, Networks,
Volumes and Podman Secrets (a secret's value goes in and is never shown again).

Off loopback it needs a password (STACK_UI_PASSWORD_FILE, the hash from --hash-password; with STACK_UI_USERNAME a
user name too), a session cookie, the
page's token and an allowed Host header (STACK_UI_ALLOWED_HOSTS), so other web pages cannot use it. Scripts use an
API token instead (Settings: generated once, only its SHA-256 is kept in STACK_UI_DATA; it opens /api/ except the
settings, never the page). Env files must not hold secrets: a save or an upload with a non-empty
*TOKEN/*PASSWORD/*SECRET/*API_KEY variable is refused; Podman secrets are the way to give a stack one.

This module is the HTTP side (sign-in, sessions, the routes, start-up). The work is done by its parts: core (the
shared settings, the one change lock, protected stacks), auth (settings file, password), stackfiles (a stack's
files and their history), stackops (deploy, roll back, start / stop / delete, drift), registry (image updates),
usage (resources, health), image_ops (images), podman_objects (containers, logs, networks, volumes, secrets) and
autoupdate (the daily check's update of a stack). A value that changes while the page runs, or that the tests set
(core.CURRENT, core.SOURCE, core.PROTECTED, auth.SETTINGS_FILE, ...), is read through its module, never copied.
"""
from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import re
import secrets
import signal
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, NamedTuple
from urllib.parse import parse_qs, urlsplit

from . import __version__
from . import core, auth, stackfiles, registry, usage, image_ops, podman_objects, stackops, autoupdate
from . import backups
from . import podman_api as pa
from . import activity
from . import schedule
from . import stacks as st
from .auth import *  # noqa: F401,F403 - the names the handler and the tests use
from .autoupdate import *  # noqa: F401,F403 - the names the handler and the tests use
from .core import *  # noqa: F401,F403 - the names the handler and the tests use
from .image_ops import *  # noqa: F401,F403 - the names the handler and the tests use
from .podman_objects import *  # noqa: F401,F403 - the names the handler and the tests use
from .registry import *  # noqa: F401,F403 - the names the handler and the tests use
from .stackfiles import *  # noqa: F401,F403 - the names the handler and the tests use
from .stackops import *  # noqa: F401,F403 - the names the handler and the tests use
from .usage import *  # noqa: F401,F403 - the names the handler and the tests use

HERE = Path(__file__).resolve().parent
PAGE = HERE / 'page.html'
ICON = HERE / 'icon.svg'
TOUCH_ICON = HERE / 'apple-touch-icon.png'   # Safari (Add to Dock / Home Screen) takes no SVG: icon.svg on a square, 180 px
MAX_BODY = 1024 * 1024
MAX_CONNECTIONS = 64     # open connections at once (each is a thread)
REQUEST_TIMEOUT = 30
SHUTDOWN_WAIT = 8        # seconds a stop waits for a change (deploy, pull, ...) in progress; podman kills after 10
SESSION_HOURS = 12


class Config:
    password_hash = ''        # empty: no login (workstation mode)
    username = ''             # STACK_UI_USERNAME: when set, the sign-in asks for it too
    allowed_hosts: set[str] = {'127.0.0.1', 'localhost'}
    trusted_proxies: list = []   # networks whose X-Forwarded-For is believed (STACK_UI_TRUSTED_PROXIES)
    local_token = secrets.token_urlsafe(24)


SESSIONS: dict[str, tuple[float, str]] = {}   # session id -> (expires, page token)


# Cookies belong to the host, not the port: another service on this host (any port, also HTTPS) may keep its own
# "sid", and a Secure cookie of that name makes the browser drop a plain-HTTP "sid". Hence a name of our own.
COOKIE = 'stack_ui_session'
COOKIE_RE = re.compile(r'(?:^|;\s*)' + COOKIE + r'=([A-Za-z0-9_-]+)')
FAILURES: dict[str, list[float]] = {}          # client address -> recent failed logins
LOGIN_LOCK = threading.Lock()
LOGIN_WINDOW = 300     # seconds a failed login counts
LOGIN_TRIES = 5
LOCKOUT_NOTED: dict[str, float] = {}           # client address -> when its lock out was written to the Activity log


USERNAME = re.compile(r'^[A-Za-z0-9._-]{1,64}$')


def parse_proxies(text: str) -> list:
    """The networks of a comma separated list of addresses / CIDRs (ValueError for a bad one)."""
    return [ipaddress.ip_network(x.strip(), strict=False) for x in text.split(',') if x.strip()]


def new_session() -> tuple[str, str]:
    sid, token = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    now = time.time()
    with LOGIN_LOCK:
        for k in [k for k, (exp, _) in SESSIONS.items() if exp < now]:
            del SESSIONS[k]
        SESSIONS[sid] = (now + SESSION_HOURS * 3600, token)
    return sid, token


LOGIN_PAGE = '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Stack UI</title>
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="apple-mobile-web-app-title" content="Stack UI">
<style>:root{--bg:#17222d;--panel:#1f2d3a;--text:#e8eef3;--muted:#a3b3c2;--line:#34485a;--accent:#2b73d8;--bad:#f26b6b;color-scheme:dark}
@media (prefers-color-scheme:light){:root{--bg:#f2f5f7;--panel:#fff;--text:#14212c;--muted:#50626f;--line:#d3dde5;--accent:#1f6fd1;--bad:#c93a3a;color-scheme:light}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,sans-serif}
form{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:24px;width:min(340px,calc(100vw - 32px));box-sizing:border-box}
h1{margin:0 0 4px;font:600 24px/1.15 Bahnschrift,"DIN Alternate",system-ui,sans-serif}form>img{display:block;width:48px;height:48px;margin:0 0 14px}p{margin:0 0 16px;color:var(--muted);font-size:13px}
input,button{font:inherit;width:100%;box-sizing:border-box;padding:9px 11px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--text)}
button{margin-top:12px;background:var(--accent);border-color:var(--accent);color:#fff;cursor:pointer}#err{color:var(--bad);font-size:13px;min-height:18px;margin-top:8px}</style>
</head><body><form id="f"><img src="/icon.svg" alt=""><h1>Stack UI</h1><p>Compose stacks on this host</p>
__USER_FIELD__<input type="password" id="pw" autocomplete="current-password" placeholder="Password" required>
<button>Sign in</button><div id="err"></div></form>
<script>document.getElementById('f').addEventListener('submit',async(e)=>{e.preventDefault();
const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:(document.getElementById('user')||{}).value||'',password:document.getElementById('pw').value})});
if(!r.ok){const d=await r.json().catch(()=>({}));document.getElementById('err').textContent=d.error||'Sign-in failed';return;}
const s=await fetch('/session').then((x)=>x.json()).catch(()=>({}));
if(s.signedIn){location.replace('/');}else{document.getElementById('err').textContent=
'The sign-in is right, but this browser did not keep the sign-in cookie. Allow cookies for this address (in Brave: Shields, cookies) and try again.';}});</script>
</body></html>'''


USER_FIELD = '<input id="user" autocomplete="username" placeholder="User name" autocapitalize="none" spellcheck="false" required>'


def login_page() -> str:
    """The sign-in page: a user name field when STACK_UI_USERNAME is set; the first field has the focus."""
    field = USER_FIELD if Config.username else ''
    page = LOGIN_PAGE.replace('__USER_FIELD__', field)
    return page.replace('placeholder="User name"' if field else 'placeholder="Password"',
                        ('placeholder="User name"' if field else 'placeholder="Password"') + ' autofocus', 1)


class Route(NamedTuple):
    """One /api/ route. path: its parts after /api/; a {placeholder} part takes any one non-empty part, handed to
    run in order. run(args, data, query) gives the JSON answer; with stream, run(handler, args, query) answers by
    itself. page_only: the scripts' API token may not use it (it changes or shows how the page itself works)."""
    method: str
    path: tuple
    run: Callable
    page_only: bool = False
    stream: bool = False


def route(method: str, path: str, run: Callable, **kw) -> Route:
    return Route(method, tuple(path.split('/')), run, **kw)


def deploy_route(args, data, query):
    remove = [str(x) for x in data.get('remove') or []]
    return run_deploy(args[0], bool(data.get('pull', True)), remove, bool(data.get('onlyUpdate')),
                      bool(data.get('recreate')))


def logs_route(args, data, query):
    tail = (query.get('tail') or ['200'])[0]
    return container_logs(args[0], int(tail) if tail.isdigit() else 0)


def history_route(args, data, query):
    version = (query.get('version') or [''])[0]
    return history_version(args[0], version) if version else {'versions': history(args[0])}


def operation_route(args, data, query):
    return {'operation': current_operation(), 'alerts': schedule.alerts(), 'scheduleRunning': schedule.RUN_LOCK.locked()}


# The functions are looked up when a request comes (a lambda, not the function itself): a test, or a later
# reload, that replaces one is used. The first line that matches answers.
ROUTES = [
    route('GET', 'settings', lambda a, d, q: settings_view(), page_only=True),
    route('POST', 'settings/token', lambda a, d, q: api_token(d), page_only=True),
    route('GET', 'operation', lambda a, d, q: operation_route(a, d, q)),
    route('POST', 'operation/cancel', lambda a, d, q: cancel_operation(d)),
    route('GET', 'schedule', lambda a, d, q: schedule_view(), page_only=True),
    route('POST', 'schedule', lambda a, d, q: schedule_save(d), page_only=True),
    route('POST', 'schedule/run', lambda a, d, q: schedule_run(), page_only=True),
    route('POST', 'schedule/ack', lambda a, d, q: schedule_ack(d), page_only=True),
    route('GET', 'updates', lambda a, d, q: registry.updates(check=False)),   # the last check against what runs now
    route('POST', 'updates', lambda a, d, q: registry.updates(check=True)),   # ask the registries
    route('GET', 'drift', lambda a, d, q: drift()),
    route('GET', 'resources', lambda a, d, q: resources()),
    route('GET', 'health', lambda a, d, q: health_info()),
    route('GET', 'activity', lambda a, d, q: activity.listing(q)),
    # the page's own state: a backup (it holds the API token's hash: the page only)
    route('GET', 'backup', lambda a, d, q: backups.view(), page_only=True),
    route('GET', 'backup/download', lambda h, a, q: h.send_backup(), stream=True, page_only=True),
    route('POST', 'backup/save', lambda a, d, q: backups.save(), page_only=True),
    # stacks
    route('GET', 'stacks', lambda a, d, q: overview()),
    route('POST', 'stacks', lambda a, d, q: upload(d)),
    route('POST', 'stacks/auto', lambda a, d, q: stacks_auto(d), page_only=True),
    route('GET', 'stacks/{name}/files', lambda a, d, q: files(a[0])),
    route('POST', 'stacks/{name}/files', lambda a, d, q: save_files(a[0], d)),
    route('GET', 'stacks/{name}/files/{file}', lambda a, d, q: file_text(a[0], a[1])),
    route('POST', 'stacks/{name}/files/delete', lambda a, d, q: delete_file(a[0], d)),
    route('POST', 'stacks/{name}/files/preview', lambda a, d, q: preview_files(a[0], d)),
    route('GET', 'stacks/{name}/diff', lambda a, d, q: diff(a[0])),
    route('GET', 'stacks/{name}/plan', lambda a, d, q: plan(a[0])),
    route('POST', 'stacks/{name}/deploy', lambda a, d, q: deploy_route(a, d, q)),
    route('POST', 'stacks/{name}/start', lambda a, d, q: stack_action(a[0], 'start', d)),
    route('POST', 'stacks/{name}/stop', lambda a, d, q: stack_action(a[0], 'stop', d)),
    route('POST', 'stacks/{name}/delete', lambda a, d, q: stack_action(a[0], 'delete', d)),
    route('POST', 'stacks/{name}/restart', lambda a, d, q: restart(a[0], d)),
    route('GET', 'stacks/{name}/rollback', lambda a, d, q: rollback_view(a[0])),
    route('POST', 'stacks/{name}/rollback', lambda a, d, q: run_rollback(a[0])),
    route('POST', 'stacks/{name}/rollback/discard', lambda a, d, q: discard_rollback(a[0])),
    route('GET', 'stacks/{name}/folder', lambda a, d, q: stack_folder(a[0])),
    route('GET', 'stacks/{name}/history', lambda a, d, q: history_route(a, d, q)),
    route('POST', 'stacks/{name}/restore', lambda a, d, q: restore(a[0], d)),
    route('POST', 'stacks/{name}/auto', lambda a, d, q: stack_auto(a[0], d), page_only=True),
    # containers
    route('GET', 'containers', lambda a, d, q: container_table()),
    route('POST', 'containers/action', lambda a, d, q: container_do(d)),
    route('GET', 'containers/{name}/inspect', lambda a, d, q: container_details(a[0])),
    route('GET', 'containers/{name}/logs', lambda a, d, q: logs_route(a, d, q)),
    route('GET', 'containers/{name}/logs/stream', lambda h, a, q: h.stream_logs(a[0], q), stream=True),
    # images
    route('GET', 'images', lambda a, d, q: images()),
    route('POST', 'images/prune', lambda a, d, q: prune(d)),
    route('POST', 'images/pull', lambda a, d, q: image_pull(d)),
    route('POST', 'images/tag', lambda a, d, q: image_tag(d)),
    route('POST', 'images/delete', lambda a, d, q: image_remove(d)),
    route('GET', 'images/{id}/inspect', lambda a, d, q: image_details(a[0])),
    # networks, volumes, secrets
    route('GET', 'networks', lambda a, d, q: network_list()),
    route('POST', 'networks', lambda a, d, q: network_create(d)),
    route('POST', 'networks/delete', lambda a, d, q: network_delete(d)),
    route('GET', 'volumes', lambda a, d, q: volume_list()),
    route('POST', 'volumes', lambda a, d, q: volume_create(d)),
    route('POST', 'volumes/delete', lambda a, d, q: volume_delete(d)),
    route('GET', 'secrets', lambda a, d, q: secret_list()),
    route('POST', 'secrets', lambda a, d, q: secret_save(d)),
    route('POST', 'secrets/delete', lambda a, d, q: secret_delete(d)),
]


def find_route(method: str, parts: list[str]) -> tuple[Route | None, list[str]]:
    """The route for the request (parts: the path split at /, 'api' first) and the parts its placeholders took."""
    for r in ROUTES:
        if r.method != method or len(parts) != len(r.path) + 1 or parts[0] != 'api':
            continue
        args = []
        for want, got in zip(r.path, parts[1:], strict=False):
            if want.startswith('{'):
                if not got:
                    break
                args.append(got)
            elif want != got:
                break
        else:
            return r, args
    return None, []


class Server(ThreadingHTTPServer):
    """A thread per request, at most MAX_CONNECTIONS at once: a flood of open connections must not use up the
    container's process limit (and with it podman-compose); the surplus is closed at once."""
    daemon_threads = True
    request_queue_size = MAX_CONNECTIONS   # the listen queue (socketserver's default is 5: a burst beyond it was reset)

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            return self.shutdown_request(request)
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = 'stack-ui'
    timeout = REQUEST_TIMEOUT   # seconds a connection may stay silent: no thread waits forever for a body

    def log_message(self, fmt, *args):
        sys.stderr.write('  %s %s\n' % (self.client_address[0], fmt % args))

    def security_headers(self) -> None:
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')

    def send_stream_headers(self) -> None:
        """200 for an answer of unknown length: it lasts until the connection closes (HTTP/1.0)."""
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
        self.send_header('X-Accel-Buffering', 'no')    # a reverse proxy must not hold the lines back
        self.security_headers()
        self.end_headers()
        self.wfile.flush()

    def stream_logs(self, name: str, query: dict) -> None:
        """GET /api/containers/<name>/logs/stream: the log as it is written, one JSON object per line."""
        tail = (query.get('tail') or ['200'])[0]
        stream, slots = open_log_stream(name, int(tail) if tail.isdigit() else 0, (query.get('since') or [''])[0])
        try:
            self.send_stream_headers()
            try:
                for line in stream.lines(podman_objects.LOG_HEARTBEAT):
                    self.wfile.write(json_line({'hb': 1} if line is None else {'l': cut_line(line)}))
                self.wfile.write(json_line({'end': 'the container stopped'}))
            except pa.PodmanError as exc:
                self.wfile.write(json_line({'error': str(exc)}))
        except OSError:
            pass                                       # the client went away: a write failed
        finally:
            stream.close()
            slots.release()
            self.close_connection = True

    def send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.security_headers()
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_backup(self) -> None:
        name, body = backups.download()
        self.send(200, body, 'application/gzip', {'Content-Disposition': f'attachment; filename="{name}"'})

    def json(self, code: int, obj, headers: dict | None = None) -> None:
        self.send(code, json.dumps(obj).encode('utf-8'), 'application/json; charset=utf-8', headers)

    def from_trusted_proxy(self) -> bool:
        addr = plain_address(self.client_address[0])
        return addr is not None and any(addr in n for n in Config.trusted_proxies)

    def secure_cookie(self) -> str:
        """'; Secure' when a trusted proxy says the browser came in over https (the page itself speaks plain http)."""
        if not self.from_trusted_proxy():
            return ''
        protos = ','.join(self.headers.get_all('X-Forwarded-Proto') or []).split(',')
        return '; Secure' if protos[-1].strip().lower() == 'https' else ''

    def client_ip(self) -> str:
        """Who is asking, for the login throttle: the connection's address, or, when it is a trusted proxy, the
        last X-Forwarded-For address that is not a trusted proxy itself (the ones before it are the client's claim)."""
        peer = self.client_address[0]
        nets = Config.trusted_proxies
        if not self.from_trusted_proxy():
            return peer
        hops = ','.join(self.headers.get_all('X-Forwarded-For') or []).split(',')
        for hop in reversed([h for h in hops if h.strip()]):
            addr = plain_address(hop)
            if addr is None:
                return peer
            if not any(addr in n for n in nets):
                return str(addr)
        return peer

    def host_ok(self) -> bool:
        host = (self.headers.get('Host') or '').strip().lower()
        host = host[1:host.index(']')] if host.startswith('[') else host.rsplit(':', 1)[0] if host.count(':') == 1 else host
        return host in Config.allowed_hosts

    def read_body(self) -> bytes | None:
        """The body, read before any answer (an unread body makes Windows reset the connection); None if its
        Content-Length is too large or not a plain number (a negative one would read until the client hangs up)."""
        length = (self.headers.get('Content-Length') or '0').strip()
        n = int(length) if length.isascii() and length.isdigit() else -1
        if not 0 <= n <= MAX_BODY:
            self.close_connection = True
            return None
        return self.rfile.read(n) if n else b''

    def session(self) -> str | None:
        """The page token of this request's session; the fixed local token without a password."""
        if not Config.password_hash:
            return Config.local_token
        cookie = self.headers.get('Cookie') or ''
        m = COOKIE_RE.search(cookie)
        entry = SESSIONS.get(m.group(1)) if m else None
        if not entry or entry[0] < time.time():
            return None
        return entry[1]

    def login(self, raw: bytes) -> None:
        ip = self.client_ip()
        now = time.time()
        with LOGIN_LOCK:  # the attempt is counted before the password is checked: parallel guesses count too
            for k in [k for k, v in FAILURES.items() if not v or v[-1] < now - LOGIN_WINDOW]:
                del FAILURES[k]
            recent = [t for t in FAILURES.get(ip, []) if now - t < LOGIN_WINDOW]
            for k in [k for k, at in LOCKOUT_NOTED.items() if at < now - LOGIN_WINDOW]:
                del LOCKOUT_NOTED[k]
            if len(recent) >= LOGIN_TRIES:
                FAILURES[ip] = recent
                noted = LOCKOUT_NOTED.get(ip, 0) > recent[-1]   # this lock out (since its last failure) is written
                if not noted:
                    LOCKOUT_NOTED[ip] = now
                recent = None
            else:
                FAILURES[ip] = [*recent, now]
        if recent is None:
            if not noted:
                activity.sign_in(ip, 'throttled', f'{LOGIN_TRIES} wrong sign-ins within {LOGIN_WINDOW // 60} minutes')
            return self.json(429, {'error': 'too many attempts: wait a few minutes'})
        try:
            body = json.loads(raw or b'{}')
        except ValueError:
            body = {}
        password = body.get('password') if isinstance(body, dict) else ''   # a body that is no object: no password
        password = password if isinstance(password, str) else ''
        user = body.get('username') if isinstance(body, dict) else ''
        user = user if isinstance(user, str) else ''
        right = check_password(password, Config.password_hash)   # always checked: the time says nothing about the name
        if Config.username:
            right = secrets.compare_digest(user.encode('utf-8'), Config.username.encode('utf-8')) and right
        if not right:
            activity.sign_in(ip, 'failed')    # not what was typed: a password typed into the name field stays out
            time.sleep(1)
            return self.json(401, {'error': 'wrong user name or password' if Config.username else 'wrong password'})
        with LOGIN_LOCK:  # a right password does not count against the address
            if now in FAILURES.get(ip, []):
                FAILURES[ip].remove(now)
        sid, _ = new_session()
        activity.sign_in(ip, 'ok')
        cookie = f'{COOKIE}={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_HOURS * 3600}{self.secure_cookie()}'
        return self.json(200, {'ok': True}, {'Set-Cookie': cookie})

    def dispatch(self, method: str) -> None:
        raw = self.read_body()
        if raw is None:
            return self.json(413, {'error': 'request too large, or its Content-Length is not valid'})
        if not self.host_ok():
            return self.json(403, {'error': 'this host name is not allowed (STACK_UI_ALLOWED_HOSTS)'})
        path = urlsplit(self.path).path
        query = parse_qs(urlsplit(self.path).query)
        if method == 'POST' and path == '/login' and Config.password_hash:
            return self.login(raw)
        bearer = (self.headers.get('Authorization') or '').strip()
        if bearer and path.startswith('/api/'):  # the deploy scripts: the API token, not a page session
            parts = path.strip('/').split('/')      # as api() reads it (a trailing slash is the same route)
            found, _ = find_route(method, parts)
            if (found and found.page_only) or parts[1:2] in (['settings'], ['schedule']):
                return self.json(403, {'error': 'the API token cannot read or change the settings'})
            if not bearer_ok(bearer):
                return self.json(401, {'error': 'wrong or revoked API token (Settings of the page)'})
            return self.api(method, path, query, raw, 'script')
        token = self.session()
        if method == 'POST' and path == '/logout':
            m = COOKIE_RE.search(self.headers.get('Cookie') or '')
            if m:
                SESSIONS.pop(m.group(1), None)
            return self.json(200, {'ok': True}, {'Set-Cookie': f'{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0{self.secure_cookie()}'})
        if method == 'GET' and path == '/session':  # did the browser keep the cookie? (the sign-in page asks)
            return self.json(200, {'signedIn': token is not None})
        if method == 'GET' and path in ('/icon.svg', '/favicon.ico'):  # public: the sign-in page shows it too
            return self.send(200, ICON.read_bytes(), 'image/svg+xml')
        if method == 'GET' and path in ('/apple-touch-icon.png', '/apple-touch-icon-precomposed.png'):  # public too
            return self.send(200, TOUCH_ICON.read_bytes(), 'image/png')
        if method == 'GET' and path in ('/', '/index.html'):
            if token is None:
                return self.send(200, login_page().encode('utf-8'), 'text/html; charset=utf-8')
            page = (PAGE.read_text(encoding='utf-8').replace('__UI_TOKEN__', token)
                    .replace('__AUTH__', 'true' if Config.password_hash else 'false').replace('__SOURCE__', core.SOURCE)
                    .replace('__VERSION__', __version__))
            return self.send(200, page.encode('utf-8'), 'text/html; charset=utf-8')
        if not path.startswith('/api/'):
            return self.json(404, {'error': 'not found'})
        if token is None:
            return self.json(401, {'error': 'signed out: reload the page'})
        if not secrets.compare_digest(self.headers.get('X-UI-Token', ''), token):
            return self.json(403, {'error': 'missing or wrong page token: reload the page'})
        return self.api(method, path, query, raw)

    def api(self, method: str, path: str, query: dict, raw: bytes, source: str = 'page') -> None:
        """One /api/ request, after its sign-in (page session and token, or the API token) was checked; source
        (page, script) is what the Activity log writes for a change it makes."""
        activity.ACTOR.source = source
        try:
            return self._api(method, path, query, raw)
        finally:
            activity.ACTOR.source = ''

    def _api(self, method: str, path: str, query: dict, raw: bytes) -> None:
        try:
            data = json.loads(raw) if raw else {}
            if not isinstance(data, dict):
                raise ValueError('the body must be a JSON object')
            found, args = find_route(method, path.strip('/').split('/'))
            if found is None:
                return self.json(404, {'error': 'not found'})
            if found.stream:
                return found.run(self, args, query)
            return self.json(200, found.run(args, data, query))
        except Conflict as exc:
            return self.json(409, {'error': str(exc), **({'operation': exc.operation} if exc.operation else {})})
        except Busy as exc:
            return self.json(429, {'error': str(exc)})
        except NoContainer as exc:
            return self.json(404, {'error': f'no container "{exc}"'})
        except FileExistsError as exc:
            return self.json(409, {'error': f'a stack "{exc}" already exists'})
        except NotDeployed as exc:
            return self.json(404, {'error': f'the stack "{exc}" is not deployed'})
        except LookupError as exc:
            return self.json(404, {'error': f'no stack {exc}'})
        except (st.StackError, pa.PodmanError) as exc:
            return self.json(502, {'error': str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            return self.json(400, {'error': str(exc)})
        except Exception as exc:  # noqa: BLE001 - an answer, not a dropped connection
            self.log_message('error: %s', repr(exc))
            return self.json(500, {'error': f'unexpected error: {type(exc).__name__}: {exc}'})

    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')


def loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == 'localhost'


def main(argv: list[str] | None = None) -> int:
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--bind', default=env('STACK_UI_BIND', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(env('STACK_UI_PORT', '8770')))
    parser.add_argument('--trusted-proxies', default=env('STACK_UI_TRUSTED_PROXIES', ''),
                        help='addresses / networks of reverse proxies whose X-Forwarded-For names the real client '
                             '(comma separated); without it every client behind a proxy is the proxy to the login throttle')
    parser.add_argument('--allowed-hosts', default=env('STACK_UI_ALLOWED_HOSTS', ''),
                        help='extra host names / addresses the page may be opened as (comma separated)')
    default_password = env('STACK_UI_PASSWORD_FILE', '/run/secrets/stack-ui-password')
    parser.add_argument('--password-file', default=default_password if Path(default_password).is_file() else '',
                        help='file with the password hash (--hash-password); required off loopback')
    parser.add_argument('--username', default=env('STACK_UI_USERNAME', ''),
                        help='a user name the sign-in asks for besides the password (optional)')
    parser.add_argument('--source-name', default=env('STACK_UI_SOURCE_NAME', 'host'))
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--hash-password', action='store_true', help='ask for a password and print its hash line')
    args = parser.parse_args(argv)
    if args.hash_password:
        if sys.stdin.isatty():
            first = getpass.getpass('New password: ', stream=sys.stderr)
            again = getpass.getpass('Again: ', stream=sys.stderr)
        else:  # from a pipe (podman run -i): one line, no prompt in the output
            first = again = sys.stdin.readline().rstrip('\r\n')
        if len(first) < 10 or first != again:
            print('ERROR: the two entries differ or the password is shorter than 10 characters', file=sys.stderr)
            return 1
        print(hash_password(first))
        return 0
    core.SOURCE = args.source_name
    if st.STACKS_DIR is None or not st.STACKS_DIR.is_dir():
        print(f'ERROR: STACKS_DIR must name an existing directory (now: {st.STACKS_DIR})', file=sys.stderr)
        return 1
    if args.password_file:
        Config.password_hash = read_password_hash(Path(args.password_file))
        if not Config.password_hash:
            print(f'ERROR: {args.password_file} does not hold a hash from --hash-password', file=sys.stderr)
            return 1
        Config.username = args.username.strip()
        if Config.username and not USERNAME.match(Config.username):
            print('ERROR: STACK_UI_USERNAME: letters, digits and . _ - only, at most 64', file=sys.stderr)
            return 1
    elif not loopback(args.bind):
        print('ERROR: listening beyond this computer needs a password (--password-file / STACK_UI_PASSWORD_FILE)',
              file=sys.stderr)
        return 1
    try:
        Config.trusted_proxies = parse_proxies(args.trusted_proxies)
    except ValueError as exc:
        print(f'ERROR: STACK_UI_TRUSTED_PROXIES: {exc}', file=sys.stderr)
        return 1
    Config.allowed_hosts = {'127.0.0.1', 'localhost'} | {h.strip().lower() for h in args.allowed_hosts.split(',') if h.strip()}
    if not pa.configured():
        print(f'Note: no Podman socket at {pa.SOCKET}: mount it (see compose.example.yaml)', file=sys.stderr)
    try:
        adopted = adopt_records()
    except Exception as exc:  # noqa: BLE001 - a start must not fail on it
        adopted = []
        print(f'Note: the old deploy records could not be looked at: {exc!r}', file=sys.stderr)
    if adopted:
        print(f'Note: took over the deploy record of: {", ".join(adopted)}', file=sys.stderr)
    server = Server((args.bind, args.port), Handler)
    url = f'http://{"127.0.0.1" if args.bind in ("0.0.0.0", "::") else args.bind}:{args.port}/'
    podman = f'socket {pa.API_URL or pa.SOCKET}' if pa.configured() else 'not available'
    print(f'Stack UI {__version__}: {url}  (stacks in {st.STACKS_DIR}; settings in {auth.SETTINGS_FILE}; Podman {podman}; '
          f'{"password" if Config.password_hash else "no password, this computer only"}; Ctrl+C stops it)', flush=True)
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    def stop(*_):  # a thread: serve_forever runs in this one, and shutdown() waits for it to return
        threading.Thread(target=server.shutdown, daemon=True).start()
    try:
        signal.signal(signal.SIGTERM, stop)   # podman stop, systemd: leave at once instead of waiting for SIGKILL
    except ValueError:                        # not the main thread
        pass
    schedule.start_thread(scheduled_check, auto_update)   # (after the handler: a stop must not find none)
    activity.start_thread()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if DEPLOY_LOCK.acquire(timeout=SHUTDOWN_WAIT):   # a change in progress gets a moment to finish
            DEPLOY_LOCK.release()
    return 0
