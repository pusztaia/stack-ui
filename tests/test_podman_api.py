import json
import unittest

from tests import support  # noqa: F401  (sys.path)
from tests.mock_libpod import MockPodman
from stack_ui import podman_api as pa


class PodmanApiTest(unittest.TestCase):
    def setUp(self):
        self.mock = MockPodman()
        self.url = self.mock.start()
        self.addCleanup(self.mock.stop)
        old = pa.API_URL
        pa.API_URL = self.url
        self.addCleanup(setattr, pa, 'API_URL', old)

    def test_configured_by_url(self):
        self.assertTrue(pa.configured())

    def test_not_configured_without_socket(self):
        pa.API_URL = ''
        old = pa.SOCKET
        pa.SOCKET = '/nonexistent/podman.sock'
        self.addCleanup(setattr, pa, 'SOCKET', old)
        self.assertFalse(pa.configured())

    def test_version(self):
        self.assertEqual(pa.version(), '5.4.2')

    def test_containers_label_filter(self):
        self.mock.add_container('a', project='one')
        self.mock.add_container('b', project='two')
        self.assertEqual([pa.cname(c) for c in pa.containers('io.podman.compose.project=two')], ['b'])
        self.assertEqual(len(pa.containers()), 2)

    def test_container_action_and_remove(self):
        self.mock.add_container('a')
        pa.container_action('id-a', 'stop')
        self.assertEqual(self.mock.containers[0]['State'], 'exited')
        self.assertIn(('POST', '/containers/id-a/stop', {'timeout': '30'}), self.mock.calls)
        pa.remove_container('id-a')
        self.assertEqual(self.mock.containers, [])

    def test_container_action_rejects_unknown(self):
        with self.assertRaises(ValueError):
            pa.container_action('id-a', 'kill')

    def test_logs_demultiplexed(self):
        self.mock.add_container('a')

        def frame(stream, text):
            return bytes([stream, 0, 0, 0]) + len(text).to_bytes(4, 'big') + text

        self.mock.logs['id-a'] = frame(1, b'2026-10-20T10:00:00Z out\n') + frame(2, b'2026-10-20T10:00:01Z err\n')
        self.assertEqual(pa.logs('id-a', 200), ['2026-10-20T10:00:00Z out', '2026-10-20T10:00:01Z err'])
        self.assertIn(('GET', '/containers/id-a/logs',
                       {'stdout': 'true', 'stderr': 'true', 'timestamps': 'true', 'tail': '200'}), self.mock.calls)

    def test_logs_plain_tty(self):
        self.mock.add_container('a')
        self.mock.logs['id-a'] = b'line one\nline two\n'
        self.assertEqual(pa.logs('id-a'), ['line one', 'line two'])

    def test_http_error_carries_message_and_code(self):
        with self.assertRaises(pa.PodmanError) as ctx:
            pa.remove_container('nope')
        self.assertEqual(ctx.exception.code, 404)
        self.assertIn('no such container', str(ctx.exception))

    def test_unreachable_socket(self):
        pa.API_URL = 'http://127.0.0.1:1'
        with self.assertRaises(pa.PodmanError) as ctx:
            pa.containers()
        self.assertIn('podman.socket', str(ctx.exception))

    def test_networks(self):
        pa.create_network('lan', '10.89.5.0/24', '10.89.5.1')
        self.assertEqual(self.mock.networks[0]['subnets'], [{'subnet': '10.89.5.0/24', 'gateway': '10.89.5.1'}])
        with self.assertRaises(ValueError):
            pa.create_network('bad name')
        with self.assertRaises(ValueError):
            pa.create_network('x', 'not-a-subnet')
        with self.assertRaises(ValueError):
            pa.create_network('x', '', '10.0.0.1')
        pa.remove_network('lan')
        self.assertEqual(pa.networks(), [])

    def test_volumes(self):
        pa.create_volume('data')
        self.mock.add_container('a', volumes=['data'])
        self.assertEqual(pa.volume_users('data'), ['a'])
        pa.remove_volume('data')
        self.assertEqual(pa.volumes(), [])

    def test_secrets(self):
        pa.create_secret('tg_token', b'abc')
        self.assertEqual(self.mock.secrets['tg_token'], b'abc')
        self.assertEqual([s['name'] for s in pa.secrets()], ['tg_token'])
        with self.assertRaises(pa.PodmanError):
            pa.create_secret('tg_token', b'new')
        pa.create_secret('tg_token', b'new', replace=True)
        self.assertEqual(self.mock.secrets['tg_token'], b'new')
        for bad in (b'', b'x' * (pa.MAX_SECRET + 1)):
            with self.assertRaises(ValueError):
                pa.create_secret('s', bad)
        pa.remove_secret('tg_token')
        self.assertEqual(pa.secrets(), [])

    def test_secret_value_not_in_request_url(self):
        pa.create_secret('s', b'topsecret')
        self.assertTrue(all('topsecret' not in json.dumps(c) for c in self.mock.calls))

    def test_images(self):
        self.mock.images.append({'Id': 'abc', 'RepoTags': ['x:1'], 'RepoDigests': ['x@sha256:1'], 'Size': 5, 'Created': 1})
        self.assertEqual(pa.image('abc')['RepoDigests'], ['x@sha256:1'])
        pa.remove_image('abc')
        self.assertEqual(pa.images(), [])


    def test_inspect_container(self):
        self.mock.add_container('a', env=['TZ=UTC'])
        self.assertEqual(pa.inspect_container('id-a')['Config']['Env'], ['TZ=UTC'])

    def test_pull_ok(self):
        pa.pull_image('docker.io/library/nginx:1.27')
        self.assertEqual(self.mock.pulled, ['docker.io/library/nginx:1.27'])
        self.assertIn(('POST', '/images/pull', {'reference': 'docker.io/library/nginx:1.27', 'quiet': 'false'}), self.mock.calls)

    def test_pull_error_in_stream(self):
        self.mock.pull_errors['docker.io/library/nope:1'] = 'manifest unknown'
        with self.assertRaises(pa.PodmanError) as ctx:
            pa.pull_image('docker.io/library/nope:1')
        self.assertIn('manifest unknown', str(ctx.exception))

    def test_tag_image(self):
        self.mock.images.append({'Id': 'abc', 'RepoTags': ['x:1'], 'RepoDigests': [], 'Size': 1, 'Created': 1})
        pa.tag_image('abc', 'localhost/x', 'keep')
        self.assertEqual(self.mock.images[0]['RepoTags'], ['x:1', 'localhost/x:keep'])

    def test_remove_image_force(self):
        self.mock.images.append({'Id': 'abc', 'RepoTags': ['x:1', 'y:1'], 'RepoDigests': [], 'Size': 1, 'Created': 1})
        pa.remove_image('abc', force=True)
        self.assertIn(('DELETE', '/images/abc', {'force': 'true'}), self.mock.calls)

if __name__ == '__main__':
    unittest.main()
