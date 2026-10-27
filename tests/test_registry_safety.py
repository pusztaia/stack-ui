import importlib
import urllib.error
import urllib.request

from tests.support import Base
from stack_ui import server as srv

PUBLIC = '104.16.0.1'


class RegistrySafetyTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = importlib.reload(srv)
        self.dns = {'auth.docker.io': [PUBLIC], 'evil.example': ['10.0.0.5'], 'mixed.example': [PUBLIC, '127.0.0.1']}

        def getaddrinfo(host, port, proto=0):
            if host not in self.dns:
                raise OSError('no such host')
            return [(2, 1, 6, '', (a, port)) for a in self.dns[host]]
        self.patch(self.ui.socket, 'getaddrinfo', getaddrinfo)

    def test_target_ok(self):
        ok = self.ui.target_ok
        self.assertTrue(ok('https://auth.docker.io/token', 'registry-1.docker.io'))
        self.assertTrue(ok('https://ghcr.io/token', 'GHCR.io'))                   # the registry's own host
        self.assertTrue(ok('https://192.168.1.5:5000/token', '192.168.1.5'))      # a LAN registry's own token address
        for url in ('http://auth.docker.io/token', 'file:///etc/passwd', 'ftp://auth.docker.io/x',
                    'https://127.0.0.1/token', 'https://[::1]/token', 'https://169.254.169.254/latest',
                    'https://10.1.2.3/token', 'https://evil.example/token', 'https://mixed.example/token',
                    'https://unknown.example/token', 'https:///nohost'):
            self.assertFalse(ok(url, 'registry-1.docker.io'), url)

    def test_token_realm_is_checked(self):
        for realm in ('http://auth.docker.io/token', 'https://evil.example/token', 'file:///etc/passwd'):
            with self.assertRaises(ValueError):
                self.ui.registry_token(f'Bearer realm="{realm}",service="x"', 'registry-1.docker.io', 'library/nginx')

    def test_check_ref_reports_a_refused_realm_instead_of_asking_it(self):
        def fetch(url, token, method):
            raise urllib.error.HTTPError(url, 401, 'no', {'WWW-Authenticate': 'Bearer realm="https://evil.example/t"'}, None)
        self.patch(self.ui, 'registry_fetch', fetch)
        digests, message = self.ui.check_ref('nginx:latest', set(), None)
        self.assertEqual(digests, ())
        self.assertIn('not allowed', message)

    def test_redirects_to_private_or_plain_http_hosts_are_not_followed(self):
        handler = self.ui.SafeRedirect()
        req = urllib.request.Request('https://auth.docker.io/token')
        for target in ('http://auth.docker.io/x', 'https://10.0.0.5/x', 'https://127.0.0.1:2375/x', 'file:///etc/passwd'):
            with self.assertRaises(urllib.error.URLError):
                handler.redirect_request(req, None, 302, 'Found', {}, target)
        new = handler.redirect_request(req, None, 302, 'Found', {}, 'https://auth.docker.io/other')
        self.assertEqual(new.full_url, 'https://auth.docker.io/other')
