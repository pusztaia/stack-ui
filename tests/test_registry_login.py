"""A private image's update check: the registry token is asked with the login of REGISTRY_AUTH_FILE (podman login
--authfile), only from the registry's own host, and a redirect to another host does not carry it."""
import io
import json
import os
import urllib.error
import urllib.request

from tests.support import Base, fresh_server

LOGIN = 'dXNlcjp0b2tlbg=='          # base64 of user:token
GHCR = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:pusztaia/calorielog:pull"'


class FakeOpener:
    """Stands in for REGISTRY_OPENER: records each token request, answers with a token."""

    def __init__(self):
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        return io.BytesIO(json.dumps({'token': 'pull-token'}).encode('utf-8'))


class RegistryLoginTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = fresh_server()
        self.opener = FakeOpener()
        self.patch(self.ui.registry, 'REGISTRY_OPENER', self.opener)
        self.patch(self.ui.socket, 'getaddrinfo', lambda host, port, proto=0: [(2, 1, 6, '', ('104.16.0.1', port))])
        self.auth_file = self.tmp / 'auth.json'
        old = os.environ.get('REGISTRY_AUTH_FILE')
        self.addCleanup(lambda: os.environ.__setitem__('REGISTRY_AUTH_FILE', old) if old is not None
                        else os.environ.pop('REGISTRY_AUTH_FILE', None))
        os.environ['REGISTRY_AUTH_FILE'] = str(self.auth_file)

    def write_auths(self, auths):
        self.auth_file.write_bytes(json.dumps({'auths': auths}).encode('utf-8'))

    def sent_login(self):
        self.assertEqual(len(self.opener.requests), 1)
        return self.opener.requests[0].get_header('Authorization')

    def test_the_token_is_asked_with_the_login_of_the_registry(self):
        self.write_auths({'ghcr.io': {'auth': LOGIN}})
        self.assertEqual(self.ui.registry_token(GHCR, 'ghcr.io', 'pusztaia/calorielog'), 'pull-token')
        self.assertEqual(self.sent_login(), f'Basic {LOGIN}')

    def test_a_key_written_as_an_address_matches_too(self):
        self.write_auths({'https://ghcr.io/v1/': {'auth': LOGIN}})
        self.ui.registry_token(GHCR, 'ghcr.io', 'pusztaia/calorielog')
        self.assertEqual(self.sent_login(), f'Basic {LOGIN}')

    def test_no_file_or_no_entry_asks_without_a_login(self):
        os.environ.pop('REGISTRY_AUTH_FILE')
        self.ui.registry_token(GHCR, 'ghcr.io', 'pusztaia/calorielog')
        self.write_auths({'quay.io': {'auth': LOGIN}})
        os.environ['REGISTRY_AUTH_FILE'] = str(self.auth_file)
        self.ui.registry_token(GHCR, 'ghcr.io', 'pusztaia/calorielog')
        self.assertEqual([r.get_header('Authorization') for r in self.opener.requests], [None, None])

    def test_a_token_address_on_another_host_never_gets_the_login(self):
        self.write_auths({'ghcr.io': {'auth': LOGIN}, 'registry-1.docker.io': {'auth': LOGIN}})
        self.ui.registry_token('Bearer realm="https://token.example/t",service="ghcr.io"', 'ghcr.io', 'a/b')
        self.ui.registry_token('Bearer realm="https://auth.docker.io/token",service="registry.docker.io"',
                               'registry-1.docker.io', 'library/nginx')
        self.assertEqual([r.get_header('Authorization') for r in self.opener.requests], [None, None])

    def test_a_broken_file_or_a_bad_value_is_no_login(self):
        for content in (b'not json', b'[]', b'{"auths": []}', json.dumps({'auths': {'ghcr.io': {'auth': 'a\r\nX: y'}}}).encode(),
                        json.dumps({'auths': {'ghcr.io': {'auth': 7}}}).encode()):
            self.auth_file.write_bytes(content)
            self.assertEqual(self.ui.registry.registry_login('ghcr.io'), '', content)

    def test_a_redirect_to_another_host_drops_the_login(self):
        handler = self.ui.SafeRedirect()
        req = urllib.request.Request('https://ghcr.io/token', headers={'Authorization': f'Basic {LOGIN}'})
        moved = handler.redirect_request(req, None, 302, 'Found', {}, 'https://cdn.example/token')
        self.assertIsNone(moved.get_header('Authorization'))
        same = handler.redirect_request(req, None, 302, 'Found', {}, 'https://ghcr.io/other')
        self.assertEqual(same.get_header('Authorization'), f'Basic {LOGIN}')

    def test_a_private_image_is_checked(self):
        """The whole check of a private GHCR image: refused without a token, served with the one the login got."""
        self.write_auths({'ghcr.io': {'auth': LOGIN}})

        def fetch(url, token, method):
            if token != 'pull-token':
                raise urllib.error.HTTPError(url, 401, 'no', {'WWW-Authenticate': GHCR}, None)
            return 'sha256:new', 'application/vnd.oci.image.manifest.v1+json', b''
        self.patch(self.ui.registry, 'registry_fetch', fetch)
        self.assertEqual(self.ui.check_ref('ghcr.io/pusztaia/calorielog:latest', {'sha256:old'}, None),
                         (('sha256:new',), ''))
