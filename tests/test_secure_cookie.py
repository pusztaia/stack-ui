import importlib
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from tests.support import Base
from tests.test_client import sc
from stack_ui import server as srv


class SecureCookieTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = importlib.reload(srv)
        self.ui.Config.password_hash = self.ui.hash_password('right-password', 1000)
        server = ThreadingHTTPServer(('127.0.0.1', 0), self.ui.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f'http://127.0.0.1:{server.server_address[1]}'

    def cookie(self, forwarded_proto=None):
        headers = {'Content-Type': 'application/json', **({'X-Forwarded-Proto': forwarded_proto} if forwarded_proto else {})}
        req = urllib.request.Request(self.base + '/login', json.dumps({'password': 'right-password'}).encode(), headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.headers['Set-Cookie']

    def test_secure_only_when_a_trusted_proxy_says_https(self):
        self.assertNotIn('Secure', self.cookie('https'))     # no trusted proxy: the header is just a claim
        self.ui.Config.trusted_proxies = self.ui.parse_proxies('127.0.0.1')
        self.addCleanup(setattr, self.ui.Config, 'trusted_proxies', [])
        self.assertTrue(self.cookie('https').endswith('; Secure'))
        self.assertTrue(self.cookie('http, https').endswith('; Secure'))
        self.assertNotIn('Secure', self.cookie('http'))
        self.assertNotIn('Secure', self.cookie())
        self.assertIn('HttpOnly; SameSite=Strict', self.cookie('https'))

    def test_new_hashes_use_600000_iterations_and_old_ones_still_work(self):
        self.assertEqual(self.ui.hash_password('x' * 10).split('$')[1], '600000')
        old = self.ui.hash_password('right-password', 300_000)
        self.assertTrue(self.ui.check_password('right-password', old))
        self.assertFalse(self.ui.check_password('wrong-password', old))


class PlainHttpWarningTest(Base):
    def test_warns_once_for_plain_http_to_another_machine_only(self):
        for ok in ('http://127.0.0.1:8770', 'http://localhost:8770', 'http://[::1]:8770', 'https://my-host:8770'):
            sc.WARNED = False
            self.assertFalse(sc.warn_if_plain(ok), ok)
        sc.WARNED = False
        self.addCleanup(setattr, sc, 'WARNED', False)
        self.assertTrue(sc.warn_if_plain('http://192.168.1.10:8770'))
        self.assertFalse(sc.warn_if_plain('http://192.168.1.10:8770'))     # once
