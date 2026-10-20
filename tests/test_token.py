import importlib
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from tests.support import Base, compose_yaml, make_stack
from stack_ui import podman_api as pa, server as srv, stacks as st


class TokenTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = importlib.reload(srv)
        self.patch(self.ui, 'SETTINGS_FILE', self.tmp / 'settings.json')
        self.ui.Config.password_hash = ''
        server = ThreadingHTTPServer(('127.0.0.1', 0), self.ui.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f'http://127.0.0.1:{server.server_address[1]}'

    def get(self, path, headers):
        req = urllib.request.Request(self.base + path, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_token_lifecycle(self):
        token = self.ui.api_token({})['token']
        self.assertEqual(len(token), 43)
        stored = json.loads((self.tmp / 'settings.json').read_text())
        self.assertNotIn(token, json.dumps(stored))
        self.assertEqual(self.get('/api/stacks', {'Authorization': f'Bearer {token}'})[0], 200)
        self.assertEqual(self.get('/api/stacks', {'Authorization': 'Bearer wrong'})[0], 401)
        self.assertEqual(self.get('/api/settings', {'Authorization': f'Bearer {token}'})[0], 403)
        self.ui.api_token({'revoke': True})
        self.assertEqual(self.get('/api/stacks', {'Authorization': f'Bearer {token}'})[0], 401)

    def test_token_does_not_open_the_page(self):
        token = self.ui.api_token({})['token']
        self.ui.Config.password_hash = self.ui.hash_password('a-long-password', 1000)
        status, _ = self.get('/api/stacks', {'Authorization': f'Bearer {token}'})
        self.assertEqual(status, 200)
        req = urllib.request.Request(self.base + '/', headers={'Authorization': f'Bearer {token}'})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertIn(b'Sign in', r.read())

    def test_settings_keep_token(self):
        self.assertFalse(self.ui.settings_view()['tokenSet'])
        self.ui.api_token({})
        self.assertTrue(self.ui.settings_view()['tokenSet'])
