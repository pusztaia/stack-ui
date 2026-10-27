import importlib
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from tests.support import Base
from stack_ui import server as srv


class LoginThrottleTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = importlib.reload(srv)
        self.ui.Config.password_hash = self.ui.hash_password('right-password', 1000)
        self.patch(self.ui.time, 'sleep', lambda _: None)
        server = ThreadingHTTPServer(('127.0.0.1', 0), self.ui.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f'http://127.0.0.1:{server.server_address[1]}'

    def login(self, password, forwarded=None):
        headers = {'Content-Type': 'application/json', **({'X-Forwarded-For': forwarded} if forwarded else {})}
        req = urllib.request.Request(self.base + '/login', json.dumps({'password': password}).encode(), headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as exc:
            exc.read()
            return exc.code

    def test_five_wrong_passwords_lock_the_address_and_a_right_one_is_not_counted(self):
        self.assertEqual([self.login('no') for _ in range(5)], [401] * 5)
        self.assertEqual(self.login('right-password'), 429)
        self.ui.FAILURES.clear()
        self.assertEqual([self.login('no') for _ in range(4)], [401] * 4)
        self.assertEqual(self.login('right-password'), 200)
        self.assertEqual([self.login('no'), self.login('no')], [401, 429])   # 4 earlier failures + 1: the right one did not count

    def test_parallel_guesses_are_all_counted(self):
        codes = []
        threads = [threading.Thread(target=lambda: codes.append(self.login('no'))) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(codes.count(401), 5)
        self.assertEqual(codes.count(429), 15)

    def test_forwarded_for_is_ignored_unless_the_peer_is_a_trusted_proxy(self):
        for i in range(5):
            self.login('no', forwarded=f'9.9.9.{i}')
        self.assertEqual(self.login('no', forwarded='9.9.9.99'), 429)   # one address, whatever it claims

    def test_behind_a_trusted_proxy_each_client_has_its_own_count(self):
        self.ui.Config.trusted_proxies = self.ui.parse_proxies('127.0.0.1, 10.0.0.0/8')
        self.addCleanup(setattr, self.ui.Config, 'trusted_proxies', [])
        for _ in range(5):
            self.login('no', forwarded='203.0.113.7')
        self.assertEqual(self.login('no', forwarded='203.0.113.7'), 429)
        self.assertEqual(self.login('right-password', forwarded='203.0.113.8'), 200)
        # a made-up leftmost address does not hide the client: the last untrusted one counts
        self.assertEqual(self.login('no', forwarded='1.2.3.4, 203.0.113.7, 10.1.1.1'), 429)

    def test_bad_proxy_list_is_refused(self):
        with self.assertRaises(ValueError):
            self.ui.parse_proxies('not-an-address')
