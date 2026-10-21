import importlib
import os

from tests.support import Base, compose_yaml, make_stack
from stack_ui import server as srv
from stack_ui import stacks as st


class ServerTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = importlib.reload(srv)
        self.patch(self.ui, 'SETTINGS_FILE', self.tmp / 'settings.json')
        make_stack(self.stacks, 'web', compose_yaml('web'))

    def test_overview_and_deploy(self):
        self.assertIsNone(self.ui.overview()['stacks'][0]['stack'])
        self.assertTrue(self.ui.run_deploy('web', pull=False, remove=[])['ok'])
        self.mock.add_container('web', project='web')
        self.assertEqual(self.ui.overview()['stacks'][0]['stack'], {'status': 1})

    def test_hide_undeployed_flag(self):
        """STACK_UI_HIDE_UNDEPLOYED only tells the page; the API still lists every folder (the client needs them)."""
        self.assertFalse(self.ui.overview()['hideUndeployed'])
        os.environ['STACK_UI_HIDE_UNDEPLOYED'] = 'true'
        self.addCleanup(os.environ.pop, 'STACK_UI_HIDE_UNDEPLOYED', None)
        self.ui = importlib.reload(srv)
        self.patch(self.ui, 'SETTINGS_FILE', self.tmp / 'settings.json')
        data = self.ui.overview()
        self.assertTrue(data['hideUndeployed'])
        self.assertEqual([s['name'] for s in data['stacks']], ['web'])

    def test_legacy_project_drift_unknown(self):
        self.mock.add_container('web', project='web')
        self.assertEqual(self.ui.drift(), {'web': 'unknown'})

    def test_podman_unreadable_refuses_changes(self):
        self.mock.fail[('GET', '/containers/json')] = (500, 'down')
        self.assertIn('down', self.ui.overview()['podmanError'])
        self.assertFalse(self.ui.run_deploy('web', pull=False, remove=[])['ok'])
        self.assertEqual(self.compose_calls(), [])

    def test_save_without_env(self):
        make_stack(self.stacks, 'noenv', compose_yaml('noenv'), env=None)
        self.assertIsNone(self.ui.files('noenv')['envText'])
        self.ui.save_files('noenv', {'composeText': compose_yaml('noenv') + '# x\n', 'envText': 'A=1\n'})
        self.assertEqual(sorted(p.name for p in (self.stacks / 'noenv').iterdir() if p.is_file()), ['compose.yaml'])
        self.assertEqual(self.ui.preview_files('noenv', {'composeText': compose_yaml('noenv'), 'envText': None})['env'], '')

    def test_upload_without_env(self):
        r = self.ui.upload({'name': 'up', 'files': {'compose.yaml': compose_yaml('up')}})
        self.assertIsNone(r['env'])
        self.assertIn('up', [f.name for f, _, _ in st.discover()])

    def test_upload_dot_env(self):
        r = self.ui.upload({'name': 'up2', 'files': {'compose.yaml': compose_yaml('up2'), '.env': 'TZ=UTC\n'}})
        self.assertEqual(r['env'], '.env')

    def test_self_project_protected(self):
        self.patch(self.ui, 'SELF', 'stack-ui')
        self.mock.add_container('stack-ui', project='tools')
        self.mock.add_container('tools-helper', project='tools')
        rows = {r['name']: r for r in self.ui.container_table()['containers']}
        self.assertTrue(rows['stack-ui']['protected'] and rows['tools-helper']['protected'])
        for action in ('stop', 'remove'):
            with self.assertRaises(ValueError):
                self.ui.container_do({'name': 'tools-helper', 'action': action})

    def test_self_by_container_id(self):
        self.patch(self.ui, 'SELF', 'id-stac')       # HOSTNAME is the short container id
        self.mock.add_container('stack-ui')
        self.assertTrue(self.ui.container_table()['containers'][0]['protected'])

    def test_protected_env_lists(self):
        self.patch(self.ui, 'PROTECTED', {'db'})
        self.patch(self.ui, 'PROTECTED_NETWORKS', {'podman', 'lan'})
        self.mock.add_container('db')
        self.mock.networks.append({'name': 'lan', 'driver': 'bridge', 'subnets': []})
        self.assertTrue({r['name']: r for r in self.ui.container_table()['containers']}['db']['protected'])
        self.assertTrue({r['name']: r for r in self.ui.network_list()['networks']}['lan']['protected'])

    def test_settings_view(self):
        self.assertEqual(self.ui.settings_view(), {'tokenSet': False, 'version': '1.0.3'})

    def test_main_requires_stacks_dir(self):
        self.patch(st, 'STACKS_DIR', None)
        self.assertEqual(self.ui.main(['--no-browser', '--port', '0']), 1)

    def test_main_requires_password_off_loopback(self):
        self.assertEqual(self.ui.main(['--bind', '0.0.0.0', '--port', '0', '--password-file', '']), 1)
