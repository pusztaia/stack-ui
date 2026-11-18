"""Does the page escape what the server tells it? The server is filled with hostile text (container, image, network, volume
and secret names, labels, ports, compose file content, Podman's error messages), the page's script runs in Node
(tests/page_escape.js) against it, and every HTML string the page builds for every view is searched for the raw text.
tests/test_page.py only looks for strings in the page and tests/page_logic.js calls single functions; this follows the data
through the real views. Skipped where Node is not installed; the CI installs it before the tests."""
import json
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from tests.support import Base, fresh_server, make_stack, ROOT

PAYLOADS = {
    'img': '<img src=x onerror=PWN1()>',
    'svg': '"><svg onload=PWN2()>',
    'script': "'><script>PWN3()</script>",
    'cell': '</td><b id=PWN4>',
}
# (route, does hostile text reach it?) - the views that list what Podman and the stack folders hold
DATA_ROUTES = ['stacks', 'stacks/web', 'stacks/web/files', 'containers', 'images', 'networks', 'volumes', 'secrets']
OTHER_ROUTES = ['stacks/web/history', 'stacks/web/diff', 'resources', 'health', 'activity', 'settings']


def hostile_compose() -> str:
    q = json.dumps
    return (f'services:\n  app:\n    image: {q(PAYLOADS["svg"])}\n    container_name: web\n'
            f'    labels:\n      {q(PAYLOADS["script"])}: {q(PAYLOADS["img"])}\n'
            f'  {q(PAYLOADS["cell"])}:\n    image: docker.io/library/nginx:latest\n')


@unittest.skipUnless(shutil.which('node'), 'needs Node')
class PageEscapeTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = fresh_server()
        self.patch(self.ui.auth, 'SETTINGS_FILE', self.tmp / 'settings.json')
        self.ui.Config.password_hash = ''
        make_stack(self.stacks, 'web', hostile_compose())
        p = PAYLOADS
        self.mock.add_container(p['img'], project='web', image=p['svg'], networks=[p['cell']], volumes=[p['script']],
                                labels={p['script']: p['img']}, env=[f'K={p["svg"]}'],
                                ports=[{'host_ip': p['svg'], 'container_port': 80, 'host_port': 8080, 'protocol': p['cell']}])
        self.mock.add_container(p['svg'], image=p['script'], state=p['cell'], labels={'io.podman.compose.project': p['img']})
        self.mock.images.append({'Id': 'imgA', 'RepoTags': [p['svg'], p['script']], 'RepoDigests': [p['img']], 'Size': 1, 'Created': 1})
        self.mock.networks.append({'name': p['img'], 'driver': p['svg'], 'subnets': [{'subnet': p['script'], 'gateway': p['cell']}]})
        self.mock.volumes.append({'Name': p['cell'], 'Driver': p['svg'], 'Mountpoint': p['script']})
        self.mock.secrets[p['img']] = b'x'
        server = ThreadingHTTPServer(('127.0.0.1', 0), self.ui.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f'http://127.0.0.1:{server.server_address[1]}'

    def served_script(self) -> str:
        """The page's script as the server serves it: with the page token its API calls must carry."""
        with urllib.request.urlopen(self.base + '/', timeout=10) as r:
            html = r.read().decode('utf-8')
        return re.search(r'<script>(.*)</script>', html, re.S).group(1)

    def run_node(self, routes) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'page.js'
            script.write_bytes(self.served_script().encode('utf-8'))
            config = Path(tmp) / 'config.json'
            config.write_text(json.dumps({'payloads': PAYLOADS, 'routes': routes}), encoding='utf-8')
            r = subprocess.run(['node', str(ROOT / 'tests' / 'page_escape.js'), str(script), self.base, str(config)],
                               capture_output=True, timeout=90)
        out = r.stdout.decode('utf-8', 'replace')
        err = r.stderr.decode('utf-8', 'replace')
        self.assertEqual(r.returncode, 0, out + err)
        return json.loads(out.strip().splitlines()[-1])

    def check(self, routes):
        routes = [{'path': r, 'expect': expect} for r, expect in routes]
        result = self.run_node(routes)
        self.assertEqual(result['violations'], [], 'hostile text reached the page as HTML')
        for route in (r['path'] for r in routes if r['expect']):
            self.assertTrue(any(key.startswith(route + ':') for key in result['seen']),
                            f'{route} never showed the hostile text (escaped): the test would pass for nothing')
        return result

    def test_what_the_server_lists_is_escaped(self):
        self.check([(r, True) for r in DATA_ROUTES] + [(r, False) for r in OTHER_ROUTES])

    def test_podmans_error_messages_are_escaped(self):
        for method, path in (('GET', '/containers/json'), ('GET', '/images/json'), ('GET', '/networks/json')):
            self.mock.fail[(method, path)] = (500, PAYLOADS['img'] + PAYLOADS['svg'] + PAYLOADS['script'])
        self.check([('stacks', True), ('containers', True), ('images', True), ('networks', True)])


if __name__ == '__main__':
    unittest.main()
