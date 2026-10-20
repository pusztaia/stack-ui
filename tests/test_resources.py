import importlib
import json

from tests.support import Base, compose_yaml, make_stack
from stack_ui import podman_api as pa, server as srv, stacks as st


class ResourcesTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = importlib.reload(srv)

    def test_container_list_and_logs(self):
        self.mock.add_container('web', project='web')
        self.mock.logs['id-web'] = b'hello\nworld\n'
        row = self.ui.container_table()['containers'][0]
        self.assertEqual({k: row[k] for k in ('name', 'state', 'image', 'stack')},
                         {'name': 'web', 'state': 'running', 'image': 'docker.io/library/nginx:latest', 'stack': 'web'})
        self.assertEqual(self.ui.container_logs('web', 500)['lines'], ['hello', 'world'])
        with self.assertRaises(ValueError):
            self.ui.container_logs('web', 7)
        with self.assertRaises(ValueError):
            self.ui.container_logs('nope', 200)

    def test_podman_required(self):
        self.patch(pa, 'API_URL', '')
        self.patch(pa, 'SOCKET', str(self.tmp / 'no.sock'))
        with self.assertRaises(st.StackError):
            self.ui.network_list()

    def test_networks_protected_and_in_use(self):
        self.patch(self.ui, 'PROTECTED_NETWORKS', {'podman', 'proxy-net'})
        self.mock.networks += [{'name': 'podman', 'driver': 'bridge', 'subnets': []},
                               {'name': 'proxy-net', 'driver': 'bridge', 'subnets': [{'subnet': '10.89.0.0/24'}]},
                               {'name': 'web_default', 'driver': 'bridge', 'subnets': []},
                               {'name': 'spare', 'driver': 'bridge', 'subnets': []}]
        self.mock.add_container('web', networks=['web_default'])
        rows = {r['name']: r for r in self.ui.network_list()['networks']}
        self.assertTrue(rows['proxy-net']['protected'])
        self.assertEqual(rows['web_default']['usedBy'], ['web'])
        for name in ('podman', 'proxy-net', 'web_default', 'missing'):
            with self.assertRaises(ValueError):
                self.ui.network_delete({'name': name})
        self.ui.network_delete({'name': 'spare'})
        self.ui.network_create({'name': 'lan', 'subnet': '', 'gateway': ''})
        self.assertEqual(sorted(n['name'] for n in self.mock.networks), ['lan', 'podman', 'proxy-net', 'web_default'])

    def test_volumes_only_unused_deleted(self):
        self.ui.volume_create({'name': 'a'})
        self.ui.volume_create({'name': 'b'})
        self.mock.add_container('web', volumes=['a'])
        r = self.ui.volume_delete({'names': ['a', 'b', 'c']})
        self.assertEqual(r['removed'], ['b'])
        self.assertEqual(len(r['errors']), 2)
        self.assertEqual([v['Name'] for v in self.mock.volumes], ['a'])

    def test_secrets_refs_and_delete_protection(self):
        make_stack(self.stacks, 'diun', compose_yaml('diun', ('tg_token',)))
        self.ui.secret_save({'name': 'tg_token', 'value': 'abc', 'replace': False})
        self.ui.secret_save({'name': 'spare', 'value': 'x', 'replace': False})
        data = self.ui.secret_list()
        self.assertEqual({s['name']: s['usedBy'] for s in data['secrets']}, {'tg_token': ['diun'], 'spare': []})
        self.assertEqual(data['missing'], [])
        with self.assertRaises(ValueError):
            self.ui.secret_delete({'name': 'tg_token'})
        self.ui.secret_delete({'name': 'spare'})
        self.assertEqual(list(self.mock.secrets), ['tg_token'])
        r = self.ui.secret_save({'name': 'tg_token', 'value': 'new', 'replace': True})
        self.assertEqual(r['usedBy'], ['diun'])
        self.assertEqual(self.mock.secrets['tg_token'], b'new')

    def test_missing_secrets_listed(self):
        make_stack(self.stacks, 'diun', compose_yaml('diun', ('tg_token',)))
        self.assertEqual(self.ui.secret_list()['missing'], ['tg_token'])

    def test_secret_value_never_returned(self):
        self.ui.secret_save({'name': 's', 'value': 'TOPSECRET', 'replace': False})
        outputs = [self.ui.secret_list()]
        try:
            self.ui.secret_save({'name': 's', 'value': 'TOPSECRET', 'replace': False})  # 409: already exists
        except pa.PodmanError as exc:
            outputs.append(str(exc))
        self.assertEqual(len(outputs), 2)
        self.assertTrue(all('TOPSECRET' not in json.dumps(o) for o in outputs))

    def test_secret_value_must_be_text(self):
        with self.assertRaises(ValueError):
            self.ui.secret_save({'name': 's', 'value': 5})
