import importlib
import io
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from tests.support import Base, compose_yaml, make_stack
from stack_ui import server as srv
from stack_ui import stacks as st


class ReviewFixesTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = importlib.reload(srv)
        self.patch(self.ui, 'SETTINGS_FILE', self.tmp / 'settings.json')

    # Critical 1: the page cannot stop, delete or recreate its own stack
    def test_own_stack_refused(self):
        self.patch(self.ui, 'SELF', 'stack-ui')
        make_stack(self.stacks, 'stack-ui', compose_yaml('stack-ui'))
        self.mock.add_container('stack-ui', project='stack-ui')
        r = self.ui.run_deploy('stack-ui', pull=True, remove=[])
        self.assertFalse(r['ok'])
        self.assertIn('protected', r['log'][-1])
        for action in ('stop', 'delete'):
            with self.assertRaises(ValueError):
                self.ui.stack_action('stack-ui', action, {'confirm': 'stack-ui'})
        self.assertEqual(self.compose_calls(), [])
        self.assertTrue(self.ui.overview()['stacks'][0]['protected'])

    def test_protected_list_covers_stacks(self):
        self.patch(self.ui, 'PROTECTED', {'db'})
        make_stack(self.stacks, 'db', compose_yaml('db'))
        self.assertFalse(self.ui.run_deploy('db', pull=False, remove=[])['ok'])

    # Critical 2: History of a stack without an env file
    def test_history_without_env(self):
        make_stack(self.stacks, 'noenv', compose_yaml('noenv'), env=None)
        self.ui.save_files('noenv', {'composeText': compose_yaml('noenv') + '# x\n'})
        versions = self.ui.history('noenv')
        self.assertEqual([v['file'] for v in versions], ['compose.yaml'])
        self.ui.restore('noenv', {'version': versions[0]['version']})

    # Critical 2 / Important 1: an unexpected error answers 500 JSON instead of dropping the connection
    def test_unexpected_error_is_500(self):
        self.ui.Config.password_hash = ''
        server = ThreadingHTTPServer(('127.0.0.1', 0), self.ui.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def boom():
            raise RuntimeError('boom')

        self.patch(self.ui, 'overview', boom)
        req = urllib.request.Request(f'http://127.0.0.1:{server.server_address[1]}/api/stacks',
                                     headers={'X-UI-Token': self.ui.Config.local_token})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 500)
        self.assertIn('boom', json.loads(ctx.exception.read())['error'])

    # Important 1: one unreadable folder does not break the list
    def test_unreadable_folder_skipped(self):
        make_stack(self.stacks, 'good', compose_yaml('good'))
        make_stack(self.stacks, 'bad', compose_yaml('bad'))
        real = st.stack_files

        def flaky(folder):
            if folder.name == 'bad':
                raise PermissionError('denied')
            return real(folder)

        self.patch(st, 'stack_files', flaky)
        self.assertEqual([f.name for f, _, _ in st.discover()], ['good'])

    # Important 3: the page's own password secret cannot be removed
    def test_password_secret_protected(self):
        self.mock.secrets['stack-ui-password'] = b'pbkdf2_sha256$x'
        self.assertTrue({s['name']: s for s in self.ui.secret_list()['secrets']}['stack-ui-password']['protected'])
        with self.assertRaises(ValueError):
            self.ui.secret_delete({'name': 'stack-ui-password'})
        self.assertIn('stack-ui-password', self.mock.secrets)

    # Critical 3: --hash-password from a pipe (no tty), and a hash file with prompt noise around it
    def test_hash_password_from_stdin(self):
        out = io.StringIO()
        self.patch(sys, 'stdin', io.StringIO('a-long-password\n'))
        self.patch(sys, 'stdout', out)
        self.assertEqual(self.ui.main(['--hash-password']), 0)
        line = out.getvalue().strip()
        self.assertTrue(line.startswith('pbkdf2_sha256$'))
        self.assertTrue(self.ui.check_password('a-long-password', line))
        self.patch(sys, 'stdin', io.StringIO('short\n'))
        self.assertEqual(self.ui.main(['--hash-password']), 1)

    def test_read_password_hash_skips_noise(self):
        h = self.ui.hash_password('a-long-password', 1000)
        f = self.tmp / 'secret'
        f.write_text(f'New password: \r\nAgain: \r\n{h}\r\n', encoding='utf-8')
        self.assertEqual(self.ui.read_password_hash(f), h)
        f.write_text('garbage\n', encoding='utf-8')
        self.assertEqual(self.ui.read_password_hash(f), '')
