"""Stack UI: a web page for the compose stacks of a Podman host (podman-compose through the Podman socket).

    python -m stack_ui                  # in the container (see compose.example.yaml)
    python -m stack_ui --hash-password  # prints the password hash for the stack-ui-password Podman secret

Every subfolder of STACKS_DIR with a compose file is a stack (see stacks.py). The page lists the stacks and their
containers, edits their compose and env files (each save keeps the previous version in .history/; a save shows
its diff first), creates, updates, starts, stops and deletes them with podman-compose (containers in a new
stack's way are removed only when the page confirms it), uploads new stacks (*Add stack*), compares a stack with
what was deployed last, checks the registries for newer images, and shows Containers, Images, Logs, Networks,
Volumes and Podman Secrets (a secret's value goes in and is never shown again).

Off loopback it needs a password (STACK_UI_PASSWORD_FILE, the hash from --hash-password), a session cookie, the
page's token and an allowed Host header (STACK_UI_ALLOWED_HOSTS), so other web pages cannot use it. Scripts use an
API token instead (Settings: generated once, only its SHA-256 is kept in STACK_UI_DATA; it opens /api/ except the
settings, never the page). Env files must not hold secrets: a save or an upload with a non-empty
*TOKEN/*PASSWORD/*SECRET/*API_KEY variable is refused; Podman secrets are the way to give a stack one.
"""
from __future__ import annotations

import argparse
import base64
import difflib
import getpass
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import yaml

from . import __version__
from . import podman_api as pa
from . import stacks as st

HERE = Path(__file__).resolve().parent
PAGE = HERE / 'page.html'
ICON = HERE / 'icon.svg'
DATA = Path(os.environ.get('STACK_UI_DATA') or '/data')
SELF = os.environ.get('STACK_UI_SELF') or os.environ.get('HOSTNAME', '')
PROTECTED = {x.strip() for x in os.environ.get('STACK_UI_PROTECTED', '').split(',') if x.strip()}
PROTECTED_NETWORKS = {x.strip() for x in os.environ.get('STACK_UI_PROTECTED_NETWORKS', 'podman').split(',') if x.strip()}
DEPLOY_LOCK = threading.Lock()   # one change (deploy, start, stop, delete, restart, pull, ...) at a time
FILES_LOCK = threading.Lock()    # one save or upload at a time
MAX_BODY = 1024 * 1024
SESSION_HOURS = 12
HASH_ITERATIONS = 300_000
FILE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$')
UPLOAD_SUFFIXES = ('.yaml', '.yml', '.env', '.sh', '.md', '.txt')
SECRET_VAR = re.compile(r'(TOKEN|PASSWORD|PASSWD|SECRET|API_KEY|APIKEY)$', re.IGNORECASE)
SETTINGS_FILE = Path(os.environ.get('STACK_UI_SETTINGS_FILE') or DATA / 'settings.json')
SETTINGS_LOCK = threading.Lock()


# ---------- settings: the API token ----------

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


# ---------- password ----------

def hash_password(password: str, iterations: int = HASH_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)
    return f'pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}'


def check_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = stored.strip().split('$')
        if algo != 'pbkdf2_sha256':
            return False
        got = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(got, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


# ---------- modules and files ----------

class NotDeployed(LookupError):
    pass


def find_module(name: str) -> tuple[Path, str, str | None]:
    for stack in st.discover():
        if stack[0].name == name:
            return stack
    raise LookupError(name)


def snapshot() -> dict:
    """What Podman runs now; podmanError when its API does not answer."""
    snap = {'containers': None, 'podmanError': ''}
    try:
        snap['containers'] = pa.containers()
    except pa.PodmanError as exc:
        snap['podmanError'] = str(exc)
    return snap


def deployed(name: str, snap: dict) -> bool:
    """A podman-compose project of that name exists (running or not)."""
    return snap['containers'] is not None and name in st.projects(snap['containers'])


def require_podman_state() -> dict:
    snap = snapshot()
    if snap['podmanError']:
        raise st.StackError(snap['podmanError'])
    return snap


def overview() -> dict:
    """Every stack with its containers, and whether Podman could be read."""
    snap = snapshot()
    containers = snap['containers'] or []
    rows = []
    for folder, compose_file, env_file in st.discover():
        name = folder.name
        names = st.container_names((folder / compose_file).read_text(encoding='utf-8'))
        mine = [c for c in containers if st.belongs(c, name, names)]
        stack = None
        if deployed(name, snap):
            running = any(c.get('State') == 'running' for c in st.project_containers(name, containers))
            stack = {'status': 1 if running else 2}
        rows.append({'name': name, 'compose': compose_file, 'env': env_file, 'stack': stack,
                     'containers': [{'name': st.cname(c), 'state': c.get('State'), 'status': c.get('Status'),
                                     'image': c.get('Image')} for c in mine]})
    return {'stacks': rows, 'podmanError': snap['podmanError'], 'source': SOURCE}


def diff(name: str) -> dict:
    """The stack's files against the ones its last deploy used."""
    folder, compose_file, env_file = find_module(name)
    if not deployed(name, snapshot()):
        return {'exists': False}
    d = st.deployed_diff(folder, compose_file, env_file, SOURCE)
    return {'exists': d is not None, **({'compose': d[0], 'env': d[1]} if d else {})}


def drift() -> dict:
    """{name: 'same' | 'differs' | 'unknown'} for every deployed stack (unknown: no record of its last deploy)."""
    snap = snapshot()
    out = {}
    for folder, compose_file, env_file in st.discover():
        if deployed(folder.name, snap):
            d = st.deployed_diff(folder, compose_file, env_file, SOURCE)
            out[folder.name] = 'unknown' if d is None else 'differs' if any(d) else 'same'
    return out


def check_compose(text: str) -> None:
    if yaml is None:
        if not re.search(r'^services:\s*$', text, flags=re.MULTILINE):
            raise ValueError('the compose file has no top-level "services:"')
        return
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f'the compose file is not valid YAML: {exc}') from None
    if not isinstance(data, dict) or not isinstance(data.get('services'), dict) or not data['services']:
        raise ValueError('the compose file needs a top-level "services:" mapping with at least one service')


def check_env(text: str) -> None:
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line or not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', line.split('=', 1)[0].strip()):
            raise ValueError(f'env line {n} is not KEY=value: {line[:60]}')
        key, value = (x.strip() for x in line.split('=', 1))
        if SECRET_VAR.search(key) and value.strip('"'):
            raise ValueError(f'env line {n}: {key} holds a secret; secrets never go into an env file '
                             '(use a Podman secret: Secrets)')


def clean_text(text) -> str:
    if not isinstance(text, str):
        raise ValueError('file contents must be text')
    text = text.replace('\r\n', '\n').replace('\r', '\n')  # LF only: a CR would end up in every value
    return text if text.endswith('\n') else text + '\n'


def write_file(path: Path, text: str) -> None:
    """Keep the previous version in .history/, then replace the file atomically (owners: see st.match_owner)."""
    if path.is_file():
        history = path.parent / '.history'
        if not history.is_dir():
            history.mkdir()
            st.match_owner(history)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup, n = history / f'{path.name}.{stamp}', 1
        while backup.exists():  # two saves in one second keep both versions
            n += 1
            backup = history / f'{path.name}.{stamp}-{n}'
        backup.write_bytes(path.read_bytes())
        st.match_owner(backup)
    tmp = path.with_name(f'.{path.name}.tmp')
    tmp.write_text(text, encoding='utf-8', newline='\n')
    st.match_owner(tmp)
    os.replace(tmp, path)


def files(name: str) -> dict:
    module, compose_file, env_file = find_module(name)
    extra = sorted(p.name for p in module.iterdir() if p.is_file() and p.name not in (compose_file, env_file)
                   and not p.name.startswith('.') and p.suffix in ('.sh', '.md', '.txt'))
    return {'compose': compose_file, 'composeText': (module / compose_file).read_text(encoding='utf-8'),
            'env': env_file, 'envText': (module / env_file).read_text(encoding='utf-8') if env_file else None,
            'others': extra}


def save_files(name: str, data: dict) -> dict:
    module, compose_file, env_file = find_module(name)
    compose = clean_text(data.get('composeText'))
    check_compose(compose)
    pairs = [(compose_file, compose)]
    if env_file:  # a stack without an env file gets none
        env = clean_text(data.get('envText'))
        check_env(env)
        pairs.append((env_file, env))
    with FILES_LOCK:
        changed = []
        for fname, text in pairs:
            if (module / fname).read_text(encoding='utf-8') != text:
                write_file(module / fname, text)
                changed.append(fname)
    return {'saved': changed}


def upload(data: dict) -> dict:
    """A stack package as a new module: {name, files: {filename: text}, compose?, env?}."""
    name = str(data.get('name') or '').strip()
    if not st.STACK_NAME.match(name):
        raise ValueError('the stack name: lowercase letters, digits, - and _ (it is also the podman-compose project name)')
    if name in st.IGNORE:
        raise ValueError(f'"{name}" is ignored (STACK_UI_IGNORE)')
    given = data.get('files')
    if not isinstance(given, dict) or not given:
        raise ValueError('no files')
    pkg = {}
    for fname, text in given.items():
        fname = Path(str(fname)).name
        if not (FILE_NAME.match(fname) or fname == '.env') or not fname.endswith(UPLOAD_SUFFIXES) or fname == st.MANIFEST:
            raise ValueError(f'"{fname}": only {", ".join(UPLOAD_SUFFIXES)} files')
        pkg[fname] = clean_text(text)
    yamls = [f for f in pkg if f.endswith(('.yaml', '.yml'))]
    envs = [f for f in pkg if f.endswith('.env')]
    compose = data.get('compose') or (yamls[0] if len(yamls) == 1 else None)
    env = data.get('env') or (envs[0] if len(envs) == 1 else None)
    if compose not in yamls:
        raise ValueError('choose the compose file' if yamls else 'the package needs a compose file (.yaml)')
    if env is not None and env not in envs:
        raise ValueError('choose the env file')
    check_compose(pkg[compose])
    if env:
        check_env(pkg[env])
    with FILES_LOCK:
        module = st.STACKS_DIR / name
        if module.exists():
            raise FileExistsError(name)
        tmp = st.STACKS_DIR / f'.upload-{name}-{secrets.token_hex(4)}'
        tmp.mkdir(parents=True)
        st.match_owner(tmp)
        for fname, text in pkg.items():
            (tmp / fname).write_text(text, encoding='utf-8', newline='\n')
            if fname.endswith('.sh'):
                os.chmod(tmp / fname, 0o755)
        (tmp / st.MANIFEST).write_text(json.dumps({'compose': compose, 'env': env}, indent=2) + '\n', encoding='utf-8')
        st.match_owner(*(tmp / f for f in [*pkg, st.MANIFEST]))
        os.replace(tmp, module)
    return {'name': name, 'compose': compose, 'env': env, 'files': sorted(pkg)}


# ---------- image updates: the registry's digest of each image tag against the one the container runs ----------

REGISTRY_SCHEME = 'https'
MANIFEST_TYPES = ', '.join(('application/vnd.oci.image.index.v1+json',
                            'application/vnd.docker.distribution.manifest.list.v2+json',
                            'application/vnd.docker.distribution.manifest.v2+json',
                            'application/vnd.oci.image.manifest.v1+json'))
REMOTE: dict[str, tuple[str, str]] = {}  # image reference -> (registry digest or '', error) of the last check
UPDATES_LOCK = threading.Lock()
CHECKED_AT = ''


def parse_ref(ref: str) -> tuple[str, str, str] | None:
    """(registry host, repository, tag) of an image reference; None for a digest-pinned or local image."""
    if '@' in ref:
        return None
    first, _, rest = ref.partition('/')
    registry, path = (first, rest) if rest and ('.' in first or ':' in first or first == 'localhost') else ('docker.io', ref)
    if registry == 'localhost':
        return None
    name, tag = path.rsplit(':', 1) if ':' in path.rsplit('/', 1)[-1] else (path, 'latest')
    if registry in ('docker.io', 'index.docker.io'):
        registry = 'registry-1.docker.io'
        name = name if '/' in name else f'library/{name}'
    return registry, name, tag


def registry_head(url: str, token: str = '') -> str:
    headers = {'Accept': MANIFEST_TYPES, **({'Authorization': f'Bearer {token}'} if token else {})}
    with urllib.request.urlopen(urllib.request.Request(url, method='HEAD', headers=headers), timeout=15) as resp:
        return resp.headers.get('Docker-Content-Digest', '')


def remote_digest(registry: str, repo: str, tag: str) -> str:
    """The digest the registry serves for the tag (a HEAD request; anonymous token when it asks for one)."""
    url = f'{REGISTRY_SCHEME}://{registry}/v2/{repo}/manifests/{tag}'
    try:
        return registry_head(url)
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise
        challenge = exc.headers.get('WWW-Authenticate', '')
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    if not challenge.lower().startswith('bearer') or 'realm' not in params:
        raise ValueError('the registry asks for a login')
    query = urlencode({'service': params.get('service', registry), 'scope': params.get('scope', f'repository:{repo}:pull')})
    with urllib.request.urlopen(f'{params["realm"]}?{query}', timeout=15) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    return registry_head(url, data.get('token') or data.get('access_token') or '')


def check_ref(ref: str) -> tuple[str, str]:
    parsed = parse_ref(ref)
    if parsed is None:
        return '', 'pinned to a digest or built locally'
    try:
        digest = remote_digest(*parsed)
        return (digest, '') if digest else ('', 'the registry sent no digest')
    except urllib.error.HTTPError as exc:
        return '', f'registry: HTTP {exc.code}'
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return '', f'registry: {exc}'


def stack_images() -> dict[str, list[dict]]:
    """{stack: [{container, image, digests}]}: the registry digests of the image each container runs."""
    containers = pa.containers()
    inspected: dict[str, list[str]] = {}
    found = {}
    for folder, compose_file, _ in st.discover():
        names = st.container_names((folder / compose_file).read_text(encoding='utf-8'))
        rows = []
        for c in containers:
            if not st.belongs(c, folder.name, names):
                continue
            image_id = str(c.get('ImageID') or '')
            if image_id not in inspected:
                inspected[image_id] = [d.split('@', 1)[1] for d in pa.image(image_id).get('RepoDigests') or [] if '@' in d]
            rows.append({'container': st.cname(c), 'image': str(c.get('Image') or ''), 'digests': inspected[image_id]})
        if rows:
            found[folder.name] = rows
    return found


def updates(check: bool) -> dict:
    """{checkedAt, stacks: {name: {status, images: [{container, image, status, message}]}}}. check asks the
    registries again; otherwise the last check's digests are compared with what runs now (after an Update the
    badge goes away without asking the registries)."""
    global CHECKED_AT
    if not check and not CHECKED_AT:
        return {'checkedAt': '', 'stacks': {}}
    images = stack_images()
    if check:
        refs = sorted({r['image'] for rows in images.values() for r in rows if r['digests']})
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = dict(zip(refs, pool.map(check_ref, refs)))
        with UPDATES_LOCK:
            REMOTE.update(results)
            CHECKED_AT = datetime.now(timezone.utc).isoformat(timespec='seconds')
    out = {}
    for name, rows in images.items():
        listed = []
        for r in rows:
            digest, error = REMOTE.get(r['image'], ('', 'not checked yet'))
            status = ('local' if not r['digests'] else 'unknown' if not digest
                      else 'current' if digest in r['digests'] else 'update')
            message = ('built here: not checked' if status == 'local' else error if status == 'unknown'
                       else 'a newer image is in the registry' if status == 'update' else 'up to date')
            listed.append({'container': r['container'], 'image': r['image'], 'status': status, 'message': message})
        states = {x['status'] for x in listed}
        status = ('update' if 'update' in states else 'unknown' if 'unknown' in states
                  else 'current' if 'current' in states else 'local' if states else 'none')
        out[name] = {'status': status, 'images': listed}
    return {'checkedAt': CHECKED_AT, 'stacks': out}


# ---------- images: what is on the Pi, and removing the untagged leftovers of pulls ----------

def short_id(image_id: str) -> str:
    return str(image_id or '').split(':', 1)[-1]


def image_table(containers: list[dict], imgs: list[dict], endpoint: int | None) -> list[dict]:
    """The images with their tags, size and the stacks (or containers) using them. removable: untagged (the old
    image a pull replaced) and used by no container."""
    mods = [(m.name, st.container_names((m / c).read_text(encoding='utf-8'))) for m, c, _ in st.discover()]
    used: dict[str, set[str]] = {}
    for c in containers:
        owner = next((n for n, names in mods if st.belongs(c, n, names)), None)
        labels = c.get('Labels') or {}
        owner = owner or next((labels[x] for x in st.COMPOSE_PROJECT_LABELS if labels.get(x)), None) or st.cname(c)
        used.setdefault(short_id(c.get('ImageID')), set()).add(owner)
    rows = []
    for img in imgs:
        iid = short_id(img.get('Id'))
        tags = [t for t in img.get('RepoTags') or [] if t and t != '<none>:<none>']
        users = sorted(used.get(iid, set()))
        rows.append({'id': iid, 'endpoint': endpoint, 'tags': tags, 'size': int(img.get('Size') or 0),
                     'created': int(img.get('Created') or 0), 'usedBy': users, 'removable': not tags and not users})
    return rows


def all_images() -> list[dict]:
    rows = image_table(pa.containers(), pa.images(), None)
    rows.sort(key=lambda r: (not r['usedBy'], r['tags'][0] if r['tags'] else '~', -r['created']))
    return rows


def images() -> dict:
    rows = all_images()
    return {'images': rows, 'reclaimable': sum(r['size'] for r in rows if r['removable'])}


def prune(data: dict) -> dict:
    """Remove the chosen images, but only those that are still removable (untagged and unused) right now."""
    wanted = {str(x) for x in data.get('ids') or []}
    removed, errors, freed = [], [], 0
    with DEPLOY_LOCK:
        for r in all_images():
            if r['id'] not in wanted or not r['removable']:
                continue
            try:
                pa.remove_image(r['id'])
                removed.append(r['id'])
                freed += r['size']
            except pa.PodmanError as exc:
                errors.append(f'{r["id"][:12]}: HTTP {exc.code}')
    return {'removed': removed, 'freed': freed, 'errors': errors}


IMAGE_REF = re.compile(r'^[a-z0-9][a-z0-9._/:@-]{0,254}$')
IMAGE_TAG = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$')


def find_image(iid: str) -> dict:
    row = next((r for r in all_images() if r['id'] == short_id(iid)), None)
    if row is None:
        raise ValueError(f'no image "{iid}"')
    return row


def image_pull(data: dict) -> dict:
    """Pull an image by reference (anonymous; up to 15 minutes)."""
    require_podman()
    ref = str(data.get('reference') or '').strip()
    if not IMAGE_REF.match(ref):
        raise ValueError('the image: e.g. docker.io/library/nginx:1.27 (lowercase, no spaces)')
    with DEPLOY_LOCK:
        pa.pull_image(ref)
    return {'pulled': ref}


def image_tag(data: dict) -> dict:
    """{id, repo, tag}: one more name for an image."""
    require_podman()
    repo, tag = str(data.get('repo') or '').strip(), str(data.get('tag') or '').strip()
    if not IMAGE_REF.match(repo) or '@' in repo:
        raise ValueError('the repository: e.g. localhost/nginx (lowercase, no tag)')
    if not IMAGE_TAG.match(tag):
        raise ValueError('the tag: letters, digits, _ . - (e.g. 1.27 or keep)')
    with DEPLOY_LOCK:
        row = find_image(str(data.get('id') or ''))
        pa.tag_image(row['id'], repo, tag)
    return {'tagged': f'{repo}:{tag}'}


def image_remove(data: dict) -> dict:
    """{id}: remove one image, only if no container (running or stopped) uses it, checked again here."""
    require_podman()
    with DEPLOY_LOCK:
        row = find_image(str(data.get('id') or ''))
        if row['usedBy']:
            raise ValueError(f'the image is used by {", ".join(row["usedBy"])}')
        # without force, name by name: Podman itself refuses if a container started using it meanwhile
        # (force would remove that container too)
        for tag in row['tags'][1:]:
            pa.remove_image(tag)
        pa.remove_image(row['tags'][0] if row['tags'] else row['id'])
    return {'removed': row['id'], 'freed': row['size']}


def image_details(iid: str) -> dict:
    require_podman()
    row = find_image(iid)
    info = pa.image(row['id'])
    return {'id': row['id'], 'tags': row['tags'], 'digests': info.get('RepoDigests') or [], 'size': row['size'],
            'created': row['created'], 'architecture': info.get('Architecture') or '', 'os': info.get('Os') or '',
            'labels': info.get('Labels') or {}, 'layers': len((info.get('RootFS') or {}).get('Layers') or []),
            'usedBy': row['usedBy']}


# ---------- Podman: containers, logs, networks, volumes, secrets ----------

LOG_TAILS = (200, 500, 2000)


def require_podman() -> None:
    if not pa.configured():
        raise st.StackError('the Podman API is not available here (no socket at PODMAN_SOCKET)')


def stack_label(c: dict) -> str:
    labels = c.get('Labels') or {}
    return str(labels.get(st.PROJECT_LABEL) or labels.get('com.docker.compose.project') or '')


CONTAINER_ACTIONS = ('start', 'stop', 'restart', 'remove')


def self_projects() -> set[str]:
    """The compose projects of this page's own container (STACK_UI_SELF / HOSTNAME, by name or id)."""
    if not SELF:
        return set()
    try:
        own = [c for c in pa.containers() if st.cname(c) == SELF or str(c.get('Id') or '').startswith(SELF)]
    except pa.PodmanError:
        return set()
    return {(c.get('Labels') or {}).get(x) for c in own for x in st.COMPOSE_PROJECT_LABELS} - {None}


def is_protected(c: dict, own_projects: set[str]) -> bool:
    """This page's own container and its compose project, and STACK_UI_PROTECTED (names or compose projects):
    never stopped, restarted or removed here."""
    labels = c.get('Labels') or {}
    name, cid = st.cname(c), str(c.get('Id') or '')
    own = bool(SELF) and (name == SELF or cid.startswith(SELF))
    projects = {labels.get(x) for x in st.COMPOSE_PROJECT_LABELS} - {None}
    return own or name in PROTECTED or bool(projects & (PROTECTED | own_projects))


def published_ports(c: dict) -> list[dict]:
    return [{'host': str(p.get('host_ip') or ''), 'port': int(p.get('host_port') or 0),
             'container': int(p.get('container_port') or 0), 'protocol': str(p.get('protocol') or 'tcp')}
            for p in c.get('Ports') or [] if int(p.get('host_port') or 0) > 0]


def container_table() -> dict:
    """Every container: its stack (a module it belongs to, else its compose project), whether it is part of a
    stack module, whether it is protected, and its published ports."""
    require_podman()
    mods = [(m.name, st.container_names((m / c).read_text(encoding='utf-8'))) for m, c, _ in st.discover()]
    names = {n for n, _ in mods}
    own_projects = self_projects()
    rows = []
    for c in pa.containers():
        owner = next((n for n, cnames in mods if st.belongs(c, n, cnames)), '')
        stack = owner or stack_label(c)
        rows.append({'name': st.cname(c), 'id': str(c.get('Id') or ''), 'state': c.get('State'),
                     'status': c.get('Status'), 'image': c.get('Image'), 'created': int(c.get('Created') or 0),
                     'stack': stack, 'ports': published_ports(c), 'inStack': bool(owner) or stack in names,
                     'protected': is_protected(c, own_projects)})
    return {'containers': sorted(rows, key=lambda r: (r['stack'] or '~', r['name']))}


def find_container(name: str) -> dict:
    row = next((r for r in container_table()['containers'] if r['name'] == name), None)
    if row is None:
        raise ValueError(f'no container "{name}"')
    return row


# wider than SECRET_VAR (which checks env files): anywhere in the name, for what the page shows
SECRET_NAME = re.compile(r'SECRET|PASSW|TOKEN|API_?KEY|PRIVATE|CREDENTIAL', re.IGNORECASE)
URL_PASSWORD = re.compile(r'(://[^/\s:@]*:)[^@\s/]+@')
MASK = '••••'


def mask_list(items: list) -> list:
    """KEY=value (also --flag=value), and the word after a secret-looking --flag, masked; passwords in URLs too."""
    out = []
    for n, item in enumerate(items):
        text = str(item)
        prev = str(items[n - 1]) if n else ''
        key, sep, _ = text.partition('=')
        if sep and SECRET_NAME.search(key.lstrip('-')):
            text = f'{key}={MASK}'
        elif prev.startswith('-') and '=' not in prev and SECRET_NAME.search(prev):
            text = MASK
        out.append(URL_PASSWORD.sub(rf'\g<1>{MASK}@', text))
    return out


def container_details(name: str) -> dict:
    """The container's inspect; secret-looking values masked in the environment and the command lines
    (Config.Env, Config.CreateCommand, Config.Cmd, Config.Entrypoint, Args)."""
    info = pa.inspect_container(find_container(name)['id'])
    config = info.get('Config') if isinstance(info.get('Config'), dict) else {}
    for holder, field in ((config, 'Env'), (config, 'CreateCommand'), (config, 'Cmd'), (config, 'Entrypoint'),
                          (info, 'Args')):
        if isinstance(holder.get(field), list):
            holder[field] = mask_list(holder[field])
        elif isinstance(holder.get(field), str):
            holder[field] = mask_list([holder[field]])[0]
    return info


def container_do(data: dict) -> dict:
    """{name, action}: start, stop, restart or remove one container. Remove only outside the stack modules;
    this page's own container and STACK_UI_PROTECTED are never stopped, restarted or removed here."""
    name, action = str(data.get('name') or ''), str(data.get('action') or '')
    if action not in CONTAINER_ACTIONS:
        raise ValueError(f'unknown container action "{action}"')
    with DEPLOY_LOCK:
        row = find_container(name)
        if row['protected'] and action != 'start':
            raise ValueError(f'"{name}" is protected (this page itself, or STACK_UI_PROTECTED): it is not stopped, '
                             'restarted or removed here')
        if action == 'remove':
            if row['inStack']:
                raise ValueError(f'"{name}" belongs to the stack {row["stack"]}: delete or update the stack instead')
            pa.remove_container(row['id'])
        else:
            pa.container_action(row['id'], action)
    return {'ok': True, 'action': action, 'name': name}


def container_logs(name: str, tail: int) -> dict:
    require_podman()
    if tail not in LOG_TAILS:
        raise ValueError(f'lines: one of {", ".join(map(str, LOG_TAILS))}')
    found = next((c for c in pa.containers() if st.cname(c) == name), None)
    if found is None:
        raise ValueError(f'no container "{name}"')
    return {'container': name, 'lines': pa.logs(str(found['Id']), tail)}


def network_list() -> dict:
    require_podman()
    containers = pa.containers()
    rows = []
    for n in pa.networks():
        name = str(n.get('name') or '')
        rows.append({'name': name, 'driver': n.get('driver'), 'subnets': [s.get('subnet') for s in n.get('subnets') or []],
                     'usedBy': sorted(st.cname(c) for c in containers if name in (c.get('Networks') or [])),
                     'protected': name in PROTECTED_NETWORKS})
    return {'networks': sorted(rows, key=lambda r: r['name'])}


def network_create(data: dict) -> dict:
    require_podman()
    with DEPLOY_LOCK:
        pa.create_network(str(data.get('name') or ''), str(data.get('subnet') or '').strip(),
                          str(data.get('gateway') or '').strip())
    return {'created': data.get('name')}


def network_delete(data: dict) -> dict:
    """Only an unused network that is not podman's default or the proxy network."""
    name = str(data.get('name') or '')
    with DEPLOY_LOCK:
        row = next((r for r in network_list()['networks'] if r['name'] == name), None)
        if row is None:
            raise ValueError(f'no network "{name}"')
        if row['protected']:
            raise ValueError(f'"{name}" is protected: the stacks need it')
        if row['usedBy']:
            raise ValueError(f'"{name}" is used by {", ".join(row["usedBy"])}')
        pa.remove_network(name)
    return {'removed': name}


def volume_list() -> dict:
    require_podman()
    rows = [{'name': str(v.get('Name') or ''), 'driver': v.get('Driver'), 'mountpoint': v.get('Mountpoint'),
             'usedBy': pa.volume_users(str(v.get('Name') or ''))} for v in pa.volumes()]
    return {'volumes': sorted(rows, key=lambda r: r['name'])}


def volume_create(data: dict) -> dict:
    require_podman()
    with DEPLOY_LOCK:
        pa.create_volume(str(data.get('name') or ''))
    return {'created': data.get('name')}


def volume_delete(data: dict) -> dict:
    """{names: [...]}: removes those that no container (running or stopped) uses, checked again here."""
    wanted = [str(x) for x in data.get('names') or []]
    removed, errors = [], []
    with DEPLOY_LOCK:
        rows = {r['name']: r for r in volume_list()['volumes']}
        for name in wanted:
            row = rows.get(name)
            if row is None:
                errors.append(f'{name}: not found')
            elif row['usedBy']:
                errors.append(f'{name}: used by {", ".join(row["usedBy"])}')
            else:
                pa.remove_volume(name)
                removed.append(name)
    return {'removed': removed, 'errors': errors}


def secret_refs() -> dict[str, list[str]]:
    """{secret: [modules whose compose file names it as an external secret]}."""
    refs: dict[str, list[str]] = {}
    for module, compose_file, _ in st.discover():
        try:
            names = st.external_secrets((module / compose_file).read_text(encoding='utf-8'))
        except (OSError, ValueError, yaml.YAMLError):
            continue
        for s in names:
            refs.setdefault(s, []).append(module.name)
    return refs


def secret_list() -> dict:
    """The Podman secrets (never their values) and the stacks naming each; missing: named but not created."""
    require_podman()
    refs = secret_refs()
    have = sorted(pa.secrets(), key=lambda s: s['name'])
    names = {s['name'] for s in have}
    return {'secrets': [{'name': s['name'], 'created': s['created'], 'updated': s['updated'],
                         'usedBy': refs.get(s['name'], [])} for s in have],
            'missing': sorted(set(refs) - names)}


def secret_save(data: dict) -> dict:
    """{name, value, replace}: create a secret, or replace its value. The value is never stored here, logged or
    sent back; a running container sees a new value only after it is recreated (the page offers it)."""
    require_podman()
    name = str(data.get('name') or '')
    value = data.get('value')
    if not isinstance(value, str):
        raise ValueError('the secret value must be text')
    with DEPLOY_LOCK:
        pa.create_secret(name, value.encode('utf-8'), bool(data.get('replace')))
    return {'name': name, 'usedBy': secret_refs().get(name, [])}


def secret_delete(data: dict) -> dict:
    require_podman()
    name = str(data.get('name') or '')
    used = secret_refs().get(name)
    if used:
        raise ValueError(f'"{name}" is used by {", ".join(used)}: remove it from their compose files first')
    with DEPLOY_LOCK:
        pa.remove_secret(name)
    return {'removed': name}


def preview_files(name: str, data: dict) -> dict:
    """What a save would change (unified diffs, now -> new), after the same checks as the save."""
    module, compose_file, env_file = find_module(name)
    compose = clean_text(data.get('composeText'))
    check_compose(compose)
    env = None
    if env_file:
        env = clean_text(data.get('envText'))
        check_env(env)

    def udiff(fname: str | None, new: str | None) -> str:
        if not fname:
            return ''
        old = (module / fname).read_text(encoding='utf-8')
        return '\n'.join(difflib.unified_diff(old.splitlines(), new.splitlines(), f'now: {fname}', f'new: {fname}',
                                              lineterm=''))

    return {'compose': udiff(compose_file, compose), 'env': udiff(env_file, env)}


def history(name: str) -> list[dict]:
    """Every saved version of the module's compose and env file, newest first: [{version, file, time}]."""
    module, compose_file, env_file = find_module(name)
    folder = module / '.history'
    found = []
    for fname in (compose_file, env_file):
        pattern = re.compile(re.escape(fname) + r'\.(\d{8}T\d{6}Z)(?:-(\d+))?')
        for p in folder.iterdir() if folder.is_dir() else []:
            m = pattern.fullmatch(p.name)
            if m and p.is_file():
                t = datetime.strptime(m.group(1), '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
                found.append((t, int(m.group(2) or 1), {'version': p.name, 'file': fname, 'time': t.isoformat()}))
    found.sort(key=lambda x: x[:2], reverse=True)
    return [x[2] for x in found]


def history_entry(name: str, version: str) -> tuple[Path, dict]:
    """The module directory and the history entry of version; only a listed version (no path tricks)."""
    entry = next((h for h in history(name) if h['version'] == version), None)
    if entry is None:
        raise ValueError(f'no saved version "{version}"')
    return find_module(name)[0], entry


def history_version(name: str, version: str) -> dict:
    """One saved version and its diff from the current file (what Restore would bring back)."""
    module, entry = history_entry(name, version)
    text = (module / '.history' / version).read_text(encoding='utf-8')
    current = (module / entry['file']).read_text(encoding='utf-8')
    d = '\n'.join(difflib.unified_diff(current.splitlines(), text.splitlines(), f'now: {entry["file"]}', version,
                                       lineterm=''))
    return {'file': entry['file'], 'time': entry['time'], 'text': text, 'diff': d}


def restore(name: str, data: dict) -> dict:
    """Put a saved version back (checked like a save); the current file goes into .history/ first."""
    _, compose_file, _ = find_module(name)
    module, entry = history_entry(name, str(data.get('version') or ''))
    text = clean_text((module / '.history' / entry['version']).read_text(encoding='utf-8'))
    (check_compose if entry['file'] == compose_file else check_env)(text)
    with FILES_LOCK:
        if (module / entry['file']).read_text(encoding='utf-8') != text:
            write_file(module / entry['file'], text)
    return {'restored': entry['file']}


def blocking_rows(containers: list[dict]) -> list[dict]:
    return [{'name': st.cname(c), 'state': c.get('State'), 'image': c.get('Image')} for c in containers]


def plan(name: str) -> dict:
    folder, compose_file, _ = find_module(name)
    require_podman_state()
    p = st.plan(folder, compose_file)
    return {'action': p['action'], 'blocking': blocking_rows(p['blocking']), 'missingSecrets': p['missing_secrets']}


def run_deploy(name: str, pull: bool, remove: list[str], only_update: bool = False, recreate: bool = False) -> dict:
    """Deploy with podman-compose; containers in the way go only if all of them are named in remove.
    only_update: nothing is created, a stack that is not deployed is skipped. recreate: recreate the containers
    without a pull (after a secret changed)."""
    folder, compose_file, env_file = find_module(name)
    log: list[str] = []

    def approve(containers: list[dict]) -> bool:
        wanted = {st.cname(c) for c in containers}
        if wanted <= set(remove):
            return True
        log.append('Not removed, because the page did not confirm: ' + ', '.join(sorted(wanted - set(remove))))
        return False

    with DEPLOY_LOCK:
        try:
            snap = require_podman_state()
            if only_update and not deployed(name, snap):
                return {'ok': False, 'log': ['not deployed yet: skipped']}
            st.deploy(folder, compose_file, env_file, pull=pull, recreate=recreate, approve=approve, log=log.append)
            return {'ok': True, 'log': log}
        except (st.StackError, pa.PodmanError) as exc:
            return {'ok': False, 'log': log + [f'ERROR: {exc}']}


def stack_action(name: str, action: str, data: dict) -> dict:
    """start, stop or delete the stack; delete needs {confirm: <name>}."""
    folder, compose_file, env_file = find_module(name)
    if action == 'delete' and data.get('confirm') != name:
        raise ValueError(f'type the stack name "{name}" to delete it')
    with DEPLOY_LOCK:
        if not deployed(name, require_podman_state()):
            raise NotDeployed(name)
        st.stack_action(folder, compose_file, env_file, action, log=lambda _: None)
    return {'ok': True, 'action': action}


def restart(name: str, data: dict) -> dict:
    """Restart one container of the stack, looked up here by name (an id from the browser is not trusted)."""
    folder, compose_file, _ = find_module(name)
    wanted = str(data.get('container') or '')
    names = st.container_names((folder / compose_file).read_text(encoding='utf-8'))
    with DEPLOY_LOCK:
        found = next((c for c in pa.containers() if st.cname(c) == wanted and st.belongs(c, name, names)), None)
        if found is None:
            raise ValueError(f'"{wanted}" is not a container of {name}')
        pa.container_action(str(found['Id']), 'restart')
    return {'restarted': wanted}


# ---------- server ----------

class Config:
    password_hash = ''        # empty: no login (workstation mode)
    allowed_hosts: set[str] = {'127.0.0.1', 'localhost'}
    local_token = secrets.token_urlsafe(24)


SOURCE = 'repository'  # how the diff names this side; 'Pi' in the container
SESSIONS: dict[str, tuple[float, str]] = {}   # session id -> (expires, page token)
# Cookies belong to the host, not the port: another service on the Pi (any port, also HTTPS) may keep its own
# "sid", and a Secure cookie of that name makes the browser drop a plain-HTTP "sid". Hence a name of our own.
COOKIE = 'stack_ui_session'
COOKIE_RE = re.compile(r'(?:^|;\s*)' + COOKIE + r'=([A-Za-z0-9_-]+)')
FAILURES: dict[str, list[float]] = {}          # client address -> recent failed logins
LOGIN_LOCK = threading.Lock()


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
<style>:root{--bg:#26262c;--panel:#2f2f37;--text:#ececf1;--muted:#a1a1ad;--line:#3d3d47;--accent:#7c3aed;--bad:#f87171;color-scheme:dark}
@media (prefers-color-scheme:light){:root{--bg:#f4f4f7;--panel:#fff;--text:#1f1f26;--muted:#5f5f6b;--line:#e3e3ea;--accent:#7c3aed;--bad:#dc2626;color-scheme:light}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,sans-serif}
form{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:24px;width:min(340px,calc(100vw - 32px));box-sizing:border-box}
h1{margin:0 0 4px;font-size:19px}form>img{display:block;width:48px;height:48px;margin:0 0 14px}p{margin:0 0 16px;color:var(--muted);font-size:13px}
input,button{font:inherit;width:100%;box-sizing:border-box;padding:9px 11px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--text)}
button{margin-top:12px;background:var(--accent);border-color:var(--accent);color:#fff;cursor:pointer}#err{color:var(--bad);font-size:13px;min-height:18px;margin-top:8px}</style>
</head><body><form id="f"><img src="/icon.svg" alt=""><h1>Stack UI</h1><p>Compose stacks on this host</p>
<input type="password" id="pw" autocomplete="current-password" placeholder="Password" autofocus required>
<button>Sign in</button><div id="err"></div></form>
<script>document.getElementById('f').addEventListener('submit',async(e)=>{e.preventDefault();
const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('pw').value})});
if(!r.ok){const d=await r.json().catch(()=>({}));document.getElementById('err').textContent=d.error||'Sign-in failed';return;}
const s=await fetch('/session').then((x)=>x.json()).catch(()=>({}));
if(s.signedIn){location.replace('/');}else{document.getElementById('err').textContent=
'The password is right, but this browser did not keep the sign-in cookie. Allow cookies for this address (in Brave: Shields, cookies) and try again.';}});</script>
</body></html>'''


class Handler(BaseHTTPRequestHandler):
    server_version = 'stack-ui'

    def log_message(self, fmt, *args):
        sys.stderr.write('  %s %s\n' % (self.client_address[0], fmt % args))

    def send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def json(self, code: int, obj, headers: dict | None = None) -> None:
        self.send(code, json.dumps(obj).encode('utf-8'), 'application/json; charset=utf-8', headers)

    def host_ok(self) -> bool:
        host = (self.headers.get('Host') or '').strip().lower()
        host = host[1:host.index(']')] if host.startswith('[') else host.rsplit(':', 1)[0] if host.count(':') == 1 else host
        return host in Config.allowed_hosts

    def read_body(self) -> bytes | None:
        """The body, read before any answer (an unread body makes Windows reset the connection); None if too large."""
        n = int(self.headers.get('Content-Length') or 0)
        if n > MAX_BODY:
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
        ip = self.client_address[0]
        now = time.time()
        with LOGIN_LOCK:
            recent = [t for t in FAILURES.get(ip, []) if now - t < 300]
            FAILURES[ip] = recent
        if len(recent) >= 5:
            return self.json(429, {'error': 'too many attempts: wait a few minutes'})
        try:
            password = str(json.loads(raw or b'{}').get('password') or '')
        except ValueError:
            password = ''
        if not check_password(password, Config.password_hash):
            with LOGIN_LOCK:
                FAILURES.setdefault(ip, []).append(now)
            time.sleep(1)
            return self.json(401, {'error': 'wrong password'})
        sid, _ = new_session()
        cookie = f'{COOKIE}={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_HOURS * 3600}'
        return self.json(200, {'ok': True}, {'Set-Cookie': cookie})

    def dispatch(self, method: str) -> None:
        raw = self.read_body()
        if raw is None:
            return self.json(413, {'error': 'request too large'})
        if not self.host_ok():
            return self.json(403, {'error': 'this host name is not allowed (STACK_UI_ALLOWED_HOSTS)'})
        path = urlsplit(self.path).path
        query = parse_qs(urlsplit(self.path).query)
        if method == 'POST' and path == '/login' and Config.password_hash:
            return self.login(raw)
        bearer = (self.headers.get('Authorization') or '').strip()
        if bearer and path.startswith('/api/'):  # the deploy scripts: the API token, not a page session
            if path == '/api/settings' or path.startswith('/api/settings/'):
                return self.json(403, {'error': 'the API token cannot read or change the settings'})
            if not bearer_ok(bearer):
                return self.json(401, {'error': 'wrong or revoked API token (Settings of the page)'})
            return self.api(method, path, query, raw)
        token = self.session()
        if method == 'POST' and path == '/logout':
            m = COOKIE_RE.search(self.headers.get('Cookie') or '')
            if m:
                SESSIONS.pop(m.group(1), None)
            return self.json(200, {'ok': True}, {'Set-Cookie': f'{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'})
        if method == 'GET' and path == '/session':  # did the browser keep the cookie? (the sign-in page asks)
            return self.json(200, {'signedIn': token is not None})
        if method == 'GET' and path in ('/icon.svg', '/favicon.ico'):  # public: the sign-in page shows it too
            return self.send(200, ICON.read_bytes(), 'image/svg+xml')
        if method == 'GET' and path in ('/', '/index.html'):
            if token is None:
                return self.send(200, LOGIN_PAGE.encode('utf-8'), 'text/html; charset=utf-8')
            page = (PAGE.read_text(encoding='utf-8').replace('__UI_TOKEN__', token)
                    .replace('__AUTH__', 'true' if Config.password_hash else 'false').replace('__SOURCE__', SOURCE))
            return self.send(200, page.encode('utf-8'), 'text/html; charset=utf-8')
        if not path.startswith('/api/'):
            return self.json(404, {'error': 'not found'})
        if token is None:
            return self.json(401, {'error': 'signed out: reload the page'})
        if not secrets.compare_digest(self.headers.get('X-UI-Token', ''), token):
            return self.json(403, {'error': 'missing or wrong page token: reload the page'})
        return self.api(method, path, query, raw)

    def api(self, method: str, path: str, query: dict, raw: bytes) -> None:
        """One /api/ request, after its sign-in (page session and token, or the API token) was checked."""
        try:
            data = json.loads(raw) if raw else {}
            if not isinstance(data, dict):
                raise ValueError('the body must be a JSON object')
            parts = path.strip('/').split('/')  # api, stacks[, name, action]
            if method == 'GET' and parts == ['api', 'settings']:
                return self.json(200, settings_view())
            if method == 'POST' and parts == ['api', 'settings', 'token']:
                return self.json(200, api_token(data))
            if method == 'GET' and parts == ['api', 'stacks']:
                return self.json(200, overview())
            if method == 'POST' and parts == ['api', 'stacks']:
                return self.json(200, upload(data))
            if method == 'GET' and parts == ['api', 'drift']:
                return self.json(200, drift())
            if method == 'GET' and parts == ['api', 'containers']:
                return self.json(200, container_table())
            if method == 'POST' and parts == ['api', 'containers', 'action']:
                return self.json(200, container_do(data))
            if method == 'GET' and len(parts) == 4 and parts[1] == 'containers' and parts[3] == 'inspect':
                return self.json(200, container_details(parts[2]))
            if method == 'GET' and len(parts) == 4 and parts[1] == 'containers' and parts[3] == 'logs':
                tail = (query.get('tail') or ['200'])[0]
                return self.json(200, container_logs(parts[2], int(tail) if tail.isdigit() else 0))
            for kind, listing, create, delete in (('networks', network_list, network_create, network_delete),
                                                  ('volumes', volume_list, volume_create, volume_delete),
                                                  ('secrets', secret_list, secret_save, secret_delete)):
                if parts == ['api', kind]:
                    return self.json(200, listing() if method == 'GET' else create(data))
                if method == 'POST' and parts == ['api', kind, 'delete']:
                    return self.json(200, delete(data))
            if method == 'GET' and parts == ['api', 'images']:
                return self.json(200, images())
            if method == 'POST' and parts == ['api', 'images', 'prune']:
                return self.json(200, prune(data))
            if method == 'POST' and len(parts) == 3 and parts[1] == 'images' and parts[2] in ('pull', 'tag', 'delete'):
                return self.json(200, {'pull': image_pull, 'tag': image_tag, 'delete': image_remove}[parts[2]](data))
            if method == 'GET' and len(parts) == 4 and parts[1] == 'images' and parts[3] == 'inspect':
                return self.json(200, image_details(parts[2]))
            if method == 'POST' and len(parts) == 5 and parts[1] == 'stacks' and parts[3:] == ['files', 'preview']:
                return self.json(200, preview_files(parts[2], data))
            if parts == ['api', 'updates']:  # GET: the last check against what runs now; POST: ask the registries
                return self.json(200, updates(check=method == 'POST'))
            if len(parts) == 4 and parts[1] == 'stacks':
                name, action = parts[2], parts[3]
                if action == 'files' and method == 'GET':
                    return self.json(200, files(name))
                if action == 'files' and method == 'POST':
                    return self.json(200, save_files(name, data))
                if method == 'GET' and action == 'diff':
                    return self.json(200, diff(name))
                if method == 'GET' and action == 'plan':
                    return self.json(200, plan(name))
                if method == 'POST' and action == 'deploy':
                    remove = [str(x) for x in data.get('remove') or []]
                    return self.json(200, run_deploy(name, bool(data.get('pull', True)), remove,
                                                     bool(data.get('onlyUpdate')), bool(data.get('recreate'))))
                if method == 'POST' and action in ('start', 'stop', 'delete'):
                    return self.json(200, stack_action(name, action, data))
                if method == 'POST' and action == 'restart':
                    return self.json(200, restart(name, data))
                if method == 'GET' and action == 'history':
                    version = (query.get('version') or [''])[0]
                    return self.json(200, history_version(name, version) if version else {'versions': history(name)})
                if method == 'POST' and action == 'restore':
                    return self.json(200, restore(name, data))
            return self.json(404, {'error': 'not found'})
        except FileExistsError as exc:
            return self.json(409, {'error': f'a stack "{exc}" already exists'})
        except NotDeployed as exc:
            return self.json(404, {'error': f'the stack "{exc}" is not deployed'})
        except LookupError as exc:
            return self.json(404, {'error': f'no module {exc}'})
        except (st.StackError, pa.PodmanError) as exc:
            return self.json(502, {'error': str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            return self.json(400, {'error': str(exc)})

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
    global SOURCE
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--bind', default=env('STACK_UI_BIND', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(env('STACK_UI_PORT', '8770')))
    parser.add_argument('--allowed-hosts', default=env('STACK_UI_ALLOWED_HOSTS', ''),
                        help='extra host names / addresses the page may be opened as (comma separated)')
    default_password = env('STACK_UI_PASSWORD_FILE', '/run/secrets/stack-ui-password')
    parser.add_argument('--password-file', default=default_password if Path(default_password).is_file() else '',
                        help='file with the password hash (--hash-password); required off loopback')
    parser.add_argument('--source-name', default=env('STACK_UI_SOURCE_NAME', 'host'))
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--hash-password', action='store_true', help='ask for a password and print its hash line')
    args = parser.parse_args(argv)
    if args.hash_password:
        first = getpass.getpass('New password: ')
        if len(first) < 10 or first != getpass.getpass('Again: '):
            print('ERROR: the two entries differ or the password is shorter than 10 characters', file=sys.stderr)
            return 1
        print(hash_password(first))
        return 0
    SOURCE = args.source_name
    if st.STACKS_DIR is None or not st.STACKS_DIR.is_dir():
        print(f'ERROR: STACKS_DIR must name an existing directory (now: {st.STACKS_DIR})', file=sys.stderr)
        return 1
    if args.password_file:
        Config.password_hash = Path(args.password_file).read_text(encoding='utf-8').strip()
        if not Config.password_hash.startswith('pbkdf2_sha256$'):
            print(f'ERROR: {args.password_file} does not hold a hash from --hash-password', file=sys.stderr)
            return 1
    elif not loopback(args.bind):
        print('ERROR: listening beyond this computer needs a password (--password-file / STACK_UI_PASSWORD_FILE)',
              file=sys.stderr)
        return 1
    Config.allowed_hosts = {'127.0.0.1', 'localhost'} | {h.strip().lower() for h in args.allowed_hosts.split(',') if h.strip()}
    if not pa.configured():
        print(f'Note: no Podman socket at {pa.SOCKET}: mount it (see compose.example.yaml)', file=sys.stderr)
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    url = f'http://{"127.0.0.1" if args.bind in ("0.0.0.0", "::") else args.bind}:{args.port}/'
    podman = f'socket {pa.API_URL or pa.SOCKET}' if pa.configured() else 'not available'
    print(f'Stack UI {__version__}: {url}  (stacks in {st.STACKS_DIR}; settings in {SETTINGS_FILE}; Podman {podman}; '
          f'{"password" if Config.password_hash else "no password, this computer only"}; Ctrl+C stops it)', flush=True)
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0

