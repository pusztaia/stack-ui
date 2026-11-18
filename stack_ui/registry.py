"""Image updates: the registry's digest of each image tag against the one the container runs."""
from __future__ import annotations

import json
import os
import re
import socket
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from . import podman_api as pa
from . import stacks as st
from .core import plain_address

REGISTRY_SCHEME = 'https'
INDEX_TYPES = ('application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json')


MANIFEST_TYPES = ', '.join((*INDEX_TYPES, 'application/vnd.docker.distribution.manifest.v2+json',
                            'application/vnd.oci.image.manifest.v1+json'))


# image reference -> (the registry digests that mean "current": the tag's, and its manifest for this host's
# platform when the tag is a multi-arch index; empty when unknown, error) of the last check
REMOTE: dict[str, tuple[tuple[str, ...], str]] = {}
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


def target_ok(url: str, trusted_host: str) -> bool:
    """Whether the page may ask a registry's token address or redirect: https, and either the registry's own host
    or a host that resolves only to public addresses (a registry cannot send the page to localhost, the LAN or a
    cloud metadata address, nor to a file: URL)."""
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    if parts.scheme != REGISTRY_SCHEME or not host:
        return False
    if host == trusted_host.lower():
        return True
    try:
        found = {plain_address(info[4][0]) for info in socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)}
    except (OSError, UnicodeError, ValueError):
        return False
    return bool(found) and all(a is not None and a.is_global for a in found)


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not target_ok(newurl, urlsplit(req.full_url).hostname or ''):
            raise urllib.error.URLError(f'a redirect to {urlsplit(newurl).netloc or newurl} is not followed')
        moved = super().redirect_request(req, fp, code, msg, headers, newurl)
        if moved is not None and urlsplit(newurl).hostname != urlsplit(req.full_url).hostname:
            moved.remove_header('Authorization')   # a login or a pull token is for the host it was meant for only
        return moved


REGISTRY_OPENER = urllib.request.build_opener(SafeRedirect)


def registry_fetch(url: str, token: str, method: str) -> tuple[str, str, bytes]:
    """(digest, media type, body: only for GET) of a manifest request."""
    headers = {'Accept': MANIFEST_TYPES, **({'Authorization': f'Bearer {token}'} if token else {})}
    with REGISTRY_OPENER.open(urllib.request.Request(url, method=method, headers=headers), timeout=15) as resp:
        return (resp.headers.get('Docker-Content-Digest', ''), resp.headers.get('Content-Type', '').split(';')[0].strip(),
                resp.read() if method == 'GET' else b'')


def registry_login(registry: str) -> str:
    """The login (base64 of user:password) that the REGISTRY_AUTH_FILE (podman login --authfile) holds for the
    registry's host; '' when there is no such file, entry or valid value. Podman pulls with the same file."""
    path = os.environ.get('REGISTRY_AUTH_FILE', '')
    raw = st.read_plain(Path(path), 256 * 1024) if path else None
    try:
        auths = json.loads(raw.decode('utf-8')).get('auths') if raw else None
    except (ValueError, AttributeError):
        return ''
    want = (urlsplit(f'//{registry}').hostname or '').lower()
    for key, entry in (auths.items() if isinstance(auths, dict) else ()):
        key = str(key)
        host = urlsplit(key if '//' in key else f'//{key}').hostname or ''   # ghcr.io, ghcr.io/owner, https://ghcr.io/v1/
        login = entry.get('auth') if isinstance(entry, dict) else None
        if host == want and isinstance(login, str) and re.fullmatch(r'[A-Za-z0-9+/]+={0,2}', login):
            return login
    return ''


def registry_token(challenge: str, registry: str, repo: str) -> str:
    """A pull token for the registry's Bearer challenge: with the registry's login from REGISTRY_AUTH_FILE when the
    token address is on the registry's own host (a private image), anonymous otherwise."""
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    if not challenge.lower().startswith('bearer') or 'realm' not in params:
        raise ValueError('the registry asks for a login')
    if not target_ok(params['realm'], urlsplit(f'//{registry}').hostname or ''):
        raise ValueError('the registry named a token address that is not allowed (not https, or not a public host)')
    query = urlencode({'service': params.get('service', registry), 'scope': params.get('scope', f'repository:{repo}:pull')})
    own = urlsplit(params['realm']).hostname == urlsplit(f'//{registry}').hostname
    login = registry_login(registry) if own else ''
    request = urllib.request.Request(f'{params["realm"]}?{query}', headers={'Authorization': f'Basic {login}'} if login else {})
    with REGISTRY_OPENER.open(request, timeout=15) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    return data.get('token') or data.get('access_token') or ''


def platform_digest(index: dict, platform: tuple[str, str, str]) -> list[str]:
    """The digest of the index's manifest for (os, architecture, variant); a missing variant matches any."""
    os_, arch, variant = platform
    found = [m for m in index.get('manifests') or [] if isinstance(m, dict) and isinstance(m.get('platform'), dict)
             and m['platform'].get('os') == os_ and m['platform'].get('architecture') == arch]
    exact = [m for m in found if variant and m['platform'].get('variant') == variant]
    return [str((exact or found)[0].get('digest') or '')] if found else []


def remote_digests(registry: str, repo: str, tag: str, local: set[str], platform: tuple[str, str, str] | None) -> tuple[str, ...]:
    """The digests the registry serves for the tag: a HEAD request (anonymous token when it asks for one). When
    the tag's digest is not one of the local ones and it is a multi-arch index, the index is read too (one GET,
    only then: registries count those) for the manifest of this host's platform: an index changes when another
    platform's image does, while this host's stays the same."""
    url = f'{REGISTRY_SCHEME}://{registry}/v2/{repo}/manifests/{tag}'
    token = ''
    try:
        digest, media_type, _ = registry_fetch(url, token, 'HEAD')
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise
        token = registry_token(exc.headers.get('WWW-Authenticate', ''), registry, repo)
        digest, media_type, _ = registry_fetch(url, token, 'HEAD')
    if not digest or digest in local or media_type not in INDEX_TYPES or platform is None:
        return (digest,) if digest else ()
    _, _, body = registry_fetch(f'{REGISTRY_SCHEME}://{registry}/v2/{repo}/manifests/{digest}', token, 'GET')
    return (digest, *platform_digest(json.loads(body.decode('utf-8')), platform))


def check_ref(ref: str, local: set[str], platform: tuple[str, str, str] | None) -> tuple[tuple[str, ...], str]:
    parsed = parse_ref(ref)
    if parsed is None:
        return (), 'pinned to a digest or built locally'
    try:
        digests = remote_digests(*parsed, local, platform)
        return (digests, '') if digests else ((), 'the registry sent no digest')
    except urllib.error.HTTPError as exc:
        return (), f'registry: HTTP {exc.code}'
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return (), f'registry: {exc}'


def stack_images() -> dict[str, list[dict]]:
    """{stack: [{container, image, digests, platform}]}: the registry digests and the platform (os, architecture,
    variant; None when unknown) of the image each container runs."""
    containers = pa.containers()
    inspected: dict[str, dict] = {}
    found = {}
    for folder, compose_file, _ in st.discover():
        names = st.container_names_of(folder, compose_file)
        rows = []
        for c in containers:
            if not st.belongs(c, folder.name, names):
                continue
            image_id = str(c.get('ImageID') or '')
            if image_id not in inspected:
                info = pa.image(image_id)
                os_, arch = str(info.get('Os') or ''), str(info.get('Architecture') or '')
                inspected[image_id] = {
                    'digests': [d.split('@', 1)[1] for d in info.get('RepoDigests') or [] if '@' in d],
                    'platform': (os_, arch, str(info.get('Variant') or '')) if os_ and arch else None}
            rows.append({'container': st.cname(c), 'image': str(c.get('Image') or ''), **inspected[image_id]})
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
        local: dict[str, set[str]] = {}
        platforms: dict[str, tuple[str, str, str] | None] = {}
        for r in (r for rows in images.values() for r in rows if r['digests'] and parse_ref(r['image'])):
            local.setdefault(r['image'], set()).update(r['digests'])
            platforms[r['image']] = platforms.get(r['image']) or r['platform']
        refs = sorted(local)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = dict(zip(refs, pool.map(lambda ref: check_ref(ref, local[ref], platforms[ref]), refs), strict=True))
        with UPDATES_LOCK:
            REMOTE.update(results)
            CHECKED_AT = datetime.now(timezone.utc).isoformat(timespec='seconds')
    out = {}
    for name, rows in images.items():
        listed = []
        for r in rows:
            digests, error = REMOTE.get(r['image'], ((), 'not checked yet'))
            # built on this host (podman gives such an image a localhost/... digest too) or pinned to a digest:
            # nothing to ask a registry about
            status = ('local' if not r['digests'] or parse_ref(r['image']) is None else 'unknown' if not digests
                      else 'current' if any(d in r['digests'] for d in digests) else 'update')
            message = ('built here: not checked' if status == 'local' else error if status == 'unknown'
                       else 'a newer image is in the registry' if status == 'update' else 'up to date')
            listed.append({'container': r['container'], 'image': r['image'], 'status': status, 'message': message})
        states = {x['status'] for x in listed}
        status = ('update' if 'update' in states else 'unknown' if 'unknown' in states
                  else 'current' if 'current' in states else 'local' if states else 'none')
        out[name] = {'status': status, 'images': listed}
    return {'checkedAt': CHECKED_AT, 'stacks': out}
