"""A small in-memory libpod REST API on 127.0.0.1 for the tests: never the real Podman."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

PREFIX = '/v4.0.0/libpod'


class MockPodman:
    def __init__(self):
        self.containers: list[dict] = []   # libpod list entries (+ MockVolumes: the volume names it uses)
        self.images: list[dict] = []       # Id, RepoTags, RepoDigests, Size, Created
        self.networks: list[dict] = []     # name, driver, subnets
        self.volumes: list[dict] = []      # Name, Driver, Mountpoint
        self.secrets: dict[str, bytes] = {}
        self.logs: dict[str, bytes] = {}   # container id -> raw log answer
        self.calls: list[tuple[str, str, dict]] = []
        self.fail: dict[tuple[str, str], tuple[int, str]] = {}  # (method, path) -> (status, message)
        self.pull_errors: dict[str, str] = {}  # image reference -> the error the pull stream reports
        self.pulled: list[str] = []
        self.stats: list[dict] = []        # what GET /containers/stats answers now (running containers only)
        self.stats_next: list[list[dict]] = []   # after an answer, stats becomes the first item of this queue
        self.server = None

    def start(self) -> str:
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f'http://127.0.0.1:{self.server.server_address[1]}'

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def add_container(self, name: str, project: str = '', state: str = 'running', image: str = 'docker.io/library/nginx:latest',
                      image_id: str = 'img1', networks=(), volumes=(), labels: dict | None = None,
                      env=(), ports=(), created: int = 0) -> dict:
        labels = dict(labels or {})
        if project:
            labels['io.podman.compose.project'] = project
            labels['com.docker.compose.project'] = project
        c = {'Id': f'id-{name}', 'Names': [name], 'Image': image, 'ImageID': image_id, 'State': state,
             'Status': state, 'Labels': labels, 'Networks': list(networks), 'MockVolumes': list(volumes),
             'Ports': list(ports), 'Created': created, 'MockEnv': list(env)}
        self.containers.append(c)
        return c


def _find(items: list[dict], key: str, value) -> dict | None:
    return next((x for x in items if x.get(key) == value), None)


def route(mock: MockPodman, h, method: str, seg: list[str], q: dict, body: bytes):
    if seg == ['version']:
        return h.reply(200, {'Version': '5.4.2'})
    if seg == ['containers', 'json']:
        cs = mock.containers
        filters = json.loads(q.get('filters') or '{}')
        for label in filters.get('label', []):
            k, _, v = label.partition('=')
            cs = [c for c in cs if c['Labels'].get(k) == v]
        for vol in filters.get('volume', []):
            cs = [c for c in cs if vol in c.get('MockVolumes', [])]
        return h.reply(200, cs)
    if seg == ['containers', 'stats']:
        answer = {'Error': None, 'Stats': mock.stats}
        if mock.stats_next:
            mock.stats = mock.stats_next.pop(0)
        return h.reply(200, answer)
    if seg[0] == 'containers' and len(seg) >= 2:
        c = next((c for c in mock.containers if seg[1] in (c['Id'], c['Names'][0])), None)
        if c is None:
            return h.reply(404, {'message': 'no such container'})
        if method == 'DELETE' and len(seg) == 2:
            mock.containers.remove(c)
            return h.reply(200, [])
        if method == 'POST' and len(seg) == 3 and seg[2] in ('start', 'stop', 'restart'):
            c['State'] = 'exited' if seg[2] == 'stop' else 'running'
            return h.reply(204)
        if method == 'GET' and seg[2:] == ['logs']:
            return h.reply(200, raw=mock.logs.get(c['Id'], b''))
        if method == 'GET' and seg[2:] == ['json']:
            return h.reply(200, {'Id': c['Id'], 'Name': c['Names'][0], 'State': {'Status': c['State']},
                                 'Config': {'Env': c.get('MockEnv', []), 'Labels': c['Labels'],
                                            'CreateCommand': c.get('MockCreate', []), 'Cmd': c.get('MockCmd', [])},
                                 'Args': c.get('MockCmd', [])})
    if seg == ['images', 'pull'] and method == 'POST':
        ref = q.get('reference', '')
        start = json.dumps({'stream': f'Trying to pull {ref}...\n'}) + '\n'
        if ref in mock.pull_errors:
            return h.reply(200, raw=(start + json.dumps({'error': mock.pull_errors[ref]}) + '\n').encode())
        mock.pulled.append(ref)
        iid = f'pulled{len(mock.pulled)}'
        mock.images.append({'Id': iid, 'RepoTags': [ref], 'RepoDigests': [], 'Size': 1, 'Created': 1})
        return h.reply(200, raw=(start + json.dumps({'images': [iid], 'id': iid}) + '\n').encode())
    if seg == ['images', 'json']:
        return h.reply(200, mock.images)
    if seg[0] == 'images' and len(seg) >= 2:
        img = _find(mock.images, 'Id', seg[1]) or next((i for i in mock.images if seg[1] in (i.get('RepoTags') or [])), None)
        if img is None:
            return h.reply(404, {'message': 'no such image'})
        if method == 'GET' and seg[2:] == ['json']:
            return h.reply(200, img)
        if method == 'POST' and seg[2:] == ['tag']:
            img['RepoTags'] = (img.get('RepoTags') or []) + [f'{q["repo"]}:{q["tag"]}']
            return h.reply(201)
        if method == 'DELETE' and len(seg) == 2:
            if seg[1] != img['Id']:  # by a tag: untag; the image goes with its last name
                img['RepoTags'] = [x for x in img['RepoTags'] if x != seg[1]]
                if img['RepoTags']:
                    return h.reply(200, [])
            elif len(img.get('RepoTags') or []) > 1 and q.get('force') != 'true':
                return h.reply(409, {'message': 'image is referenced in multiple repositories'})
            mock.images.remove(img)
            return h.reply(200, [])
    if seg == ['networks', 'json']:
        return h.reply(200, mock.networks)
    if seg == ['networks', 'create'] and method == 'POST':
        data = json.loads(body)
        if _find(mock.networks, 'name', data['name']):
            return h.reply(409, {'message': 'network name already used'})
        mock.networks.append({'name': data['name'], 'driver': data.get('driver', 'bridge'), 'subnets': data.get('subnets') or []})
        return h.reply(200, mock.networks[-1])
    if seg[0] == 'networks' and len(seg) == 2 and method == 'DELETE':
        n = _find(mock.networks, 'name', seg[1])
        if n is None:
            return h.reply(404, {'message': 'no such network'})
        mock.networks.remove(n)
        return h.reply(200, [])
    if seg == ['volumes', 'json']:
        return h.reply(200, mock.volumes)
    if seg == ['volumes', 'create'] and method == 'POST':
        data = json.loads(body)
        if _find(mock.volumes, 'Name', data['Name']):
            return h.reply(409, {'message': 'volume already exists'})
        mock.volumes.append({'Name': data['Name'], 'Driver': 'local',
                             'Mountpoint': f'/var/lib/containers/storage/volumes/{data["Name"]}/_data'})
        return h.reply(201, mock.volumes[-1])
    if seg[0] == 'volumes' and len(seg) == 2 and method == 'DELETE':
        v = _find(mock.volumes, 'Name', seg[1])
        if v is None:
            return h.reply(404, {'message': 'no such volume'})
        mock.volumes.remove(v)
        return h.reply(204)
    if seg == ['secrets', 'json']:
        return h.reply(200, [{'ID': f's-{k}', 'Spec': {'Name': k, 'Driver': {'Name': 'file'}},
                              'CreatedAt': '2026-10-20T10:00:00Z', 'UpdatedAt': '2026-10-20T10:00:00Z'} for k in mock.secrets])
    if seg == ['secrets', 'create'] and method == 'POST':
        name = q['name']
        if name in mock.secrets and q.get('replace') != 'true':
            return h.reply(409, {'message': f'{name}: secret name in use'})
        mock.secrets[name] = body
        return h.reply(200, {'ID': f's-{name}'})
    if seg[0] == 'secrets' and len(seg) == 2 and method == 'DELETE':
        if seg[1] not in mock.secrets:
            return h.reply(404, {'message': 'no such secret'})
        del mock.secrets[seg[1]]
        return h.reply(204)
    return h.reply(404, {'message': 'not found: ' + '/'.join(seg)})


def make_handler(mock: MockPodman):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code: int, obj=None, raw: bytes | None = None):
            body = raw if raw is not None else (json.dumps(obj).encode() if obj is not None else b'')
            self.send_response(code)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def handle_any(self, method: str):
            parts = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(parts.query).items()}
            n = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(n) if n else b''
            if not parts.path.startswith(PREFIX):
                return self.reply(404, {'message': 'no such API'})
            path = parts.path[len(PREFIX):]
            mock.calls.append((method, path, q))
            if (method, path) in mock.fail:
                code, message = mock.fail[(method, path)]
                return self.reply(code, {'message': message})
            return route(mock, self, method, [unquote(s) for s in path.strip('/').split('/')], q, body)

        def do_GET(self):
            self.handle_any('GET')

        def do_POST(self):
            self.handle_any('POST')

        def do_DELETE(self):
            self.handle_any('DELETE')

    return Handler
