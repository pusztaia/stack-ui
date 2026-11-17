"""What the server answers to a request it cannot do: each one gets a plain status and message, none drops the
connection or reaches the stack's files."""
import http.client
import json
import threading

from tests.support import Base, compose_yaml, fresh_server, make_stack


class ServerErrorsTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = fresh_server()
        self.ui.Config.password_hash = ''
        self.patch(self.ui.auth, 'SETTINGS_FILE', self.tmp / 'settings.json')
        server = self.ui.Server(('127.0.0.1', 0), self.ui.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]
        self.page = {'X-UI-Token': self.ui.Config.local_token, 'Content-Type': 'application/json'}

    def call(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=15)
        self.addCleanup(conn.close)
        data = raw if raw is not None else (json.dumps(body if body is not None else {}) if method == 'POST' else None)
        conn.request(method, path, body=data, headers=headers if headers is not None else self.page)
        resp = conn.getresponse()
        text = resp.read()
        try:
            return resp.status, json.loads(text or b'{}'), resp
        except ValueError:
            return resp.status, text, resp

    def test_a_host_name_that_is_not_allowed(self):
        code, body, _ = self.call('GET', '/api/stacks', headers={**self.page, 'Host': 'evil.example'})
        self.assertEqual((code, body['error']), (403, 'this host name is not allowed (STACK_UI_ALLOWED_HOSTS)'))

    def test_the_icon_and_an_unknown_page(self):
        for path in ('/icon.svg', '/favicon.ico'):
            code, _, resp = self.call('GET', path, headers={})
            self.assertEqual((code, resp.getheader('Content-Type')), (200, 'image/svg+xml'), path)
        self.assertEqual(self.call('GET', '/nope', headers={})[:2], (404, {'error': 'not found'}))
        self.assertEqual(self.call('GET', '/api/nope')[:2], (404, {'error': 'not found'}))

    def test_a_body_that_is_not_a_json_object(self):
        for raw in ('[1, 2]', '"text"', '{not json'):
            code, body, _ = self.call('POST', '/api/stacks', raw=raw)
            self.assertEqual(code, 400, raw)
            self.assertTrue(body['error'], raw)
        self.assertEqual(self.call('POST', '/api/stacks', raw='[1]')[1]['error'], 'the body must be a JSON object')

    def test_an_existing_stack_and_one_not_deployed(self):
        make_stack(self.stacks, 'web', compose_yaml('web'))
        code, body, _ = self.call('POST', '/api/stacks', {'name': 'web', 'files': {'compose.yaml': compose_yaml('web')}})
        self.assertEqual((code, body['error']), (409, 'a stack "web" already exists'))
        code, body, _ = self.call('POST', '/api/stacks/web/stop')
        self.assertEqual((code, body['error']), (404, 'the stack "web" is not deployed'))

    def test_a_saved_version_that_does_not_exist(self):
        make_stack(self.stacks, 'web', compose_yaml('web'))
        self.assertEqual(self.call('GET', '/api/stacks/web/history')[:2], (200, {'versions': []}))
        code, body, _ = self.call('GET', '/api/stacks/web/history?version=../compose.yaml')
        self.assertEqual((code, body['error']), (400, 'no saved version "../compose.yaml"'))

    def test_logout_ends_the_session(self):
        self.ui.Config.password_hash = self.ui.hash_password('page-password-1', 1000)
        self.patch(self.ui.time, 'sleep', lambda _: None)
        code, _, resp = self.call('POST', '/login', {'password': 'page-password-1'}, headers={'Content-Type': 'application/json'})
        self.assertEqual(code, 200)
        cookie = resp.getheader('Set-Cookie').split(';')[0]
        self.assertEqual(self.call('GET', '/session', headers={'Cookie': cookie})[1], {'signedIn': True})
        code, _, resp = self.call('POST', '/logout', headers={'Cookie': cookie})
        self.assertEqual(code, 200)
        self.assertIn('Max-Age=0', resp.getheader('Set-Cookie'))
        self.assertEqual(self.call('GET', '/session', headers={'Cookie': cookie})[1], {'signedIn': False})

    def test_a_login_body_that_is_not_an_object_is_a_wrong_password(self):
        self.ui.Config.password_hash = self.ui.hash_password('page-password-1', 1000)
        self.patch(self.ui.time, 'sleep', lambda _: None)
        for raw in ('[1]', '"page-password-1"', '{bad', '{"password": ["page-password-1"]}'):
            code, body, _ = self.call('POST', '/login', raw=raw, headers={'Content-Type': 'application/json'})
            self.assertEqual(code, 401, raw)
