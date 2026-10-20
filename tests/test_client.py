import importlib
import importlib.util
import os
import threading
from http.server import ThreadingHTTPServer

from tests.support import ROOT, Base, compose_yaml, make_stack
from stack_ui import server as srv

spec = importlib.util.spec_from_file_location('stack_ui_client', ROOT / 'client' / 'stack-ui-client.py')
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)


class ClientTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = importlib.reload(srv)
        self.patch(self.ui, 'SETTINGS_FILE', self.tmp / 'settings.json')
        self.ui.Config.password_hash = ''
        server = ThreadingHTTPServer(('127.0.0.1', 0), self.ui.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        os.environ['STACK_UI_URL'] = f'http://127.0.0.1:{server.server_address[1]}'
        os.environ['STACK_UI_API_TOKEN'] = self.ui.api_token({})['token']
        self.addCleanup(os.environ.pop, 'STACK_UI_URL', None)
        self.addCleanup(os.environ.pop, 'STACK_UI_API_TOKEN', None)
        self.repo = self.tmp / 'repo'
        self.local = make_stack(self.repo, 'web', compose_yaml('web'))
        (self.local / 'README.md').write_text('# web\n', encoding='utf-8')
        (self.local / 'deploy.py').write_text('# not uploaded\n', encoding='utf-8')

    def test_new_folder_is_uploaded_then_created(self):
        self.assertTrue(sc.deploy(self.local, pull=False, log=lambda _: None))
        self.assertTrue((self.stacks / 'web' / 'README.md').is_file())
        self.assertFalse((self.stacks / 'web' / 'deploy.py').exists())
        self.assertEqual(self.compose_calls()[-1][-2:], ['up', '-d'])

    def test_existing_folder_files_are_saved(self):
        make_stack(self.stacks, 'web', compose_yaml('web'), env='TZ=UTC\n')
        sc.deploy(self.local, pull=False, log=lambda _: None)
        self.assertEqual((self.stacks / 'web' / '.env').read_text(), 'TZ=Europe/Budapest\n')
        self.assertTrue((self.stacks / 'web' / '.history').is_dir())

    def test_dry_run_changes_nothing(self):
        make_stack(self.stacks, 'web', compose_yaml('web'), env='TZ=UTC\n')
        log = []
        sc.deploy(self.local, dry_run=True, log=log.append)
        self.assertEqual((self.stacks / 'web' / '.env').read_text(), 'TZ=UTC\n')
        self.assertEqual(self.compose_calls(), [])
        self.assertTrue(any('+TZ=Europe/Budapest' in line for line in log))

    def test_env_less_folder(self):
        local = make_stack(self.repo, 'noenv', compose_yaml('noenv'), env=None)
        self.assertTrue(sc.deploy(local, pull=False, log=lambda _: None))
        self.assertNotIn('--env-file', self.compose_calls()[-1])

    def test_blocking_asks(self):
        self.mock.add_container('web')
        with self.assertRaises(sc.ClientError):
            sc.deploy(self.local, pull=False, log=lambda _: None, ask=lambda: False)
        self.assertTrue(sc.deploy(self.local, pull=False, log=lambda _: None, ask=lambda: True))

    def test_wrong_token_and_missing_url(self):
        os.environ['STACK_UI_API_TOKEN'] = 'wrong'
        with self.assertRaises(sc.ClientError) as ctx:
            sc.deploy(self.local, log=lambda _: None)
        self.assertIn('401', str(ctx.exception))
        os.environ.pop('STACK_UI_URL')
        with self.assertRaises(sc.ClientError):
            sc.deploy(self.local, log=lambda _: None)

    def test_main_exit_codes(self):
        self.assertEqual(sc.main(['deploy', str(self.local), '--no-pull']), 0)
        os.environ['FAKE_COMPOSE_FAIL_ON'] = 'up'
        self.assertEqual(sc.main(['deploy', str(self.local), '--no-pull']), 1)
        self.assertEqual(sc.main(['deploy', str(self.tmp / 'missing')]), 1)
