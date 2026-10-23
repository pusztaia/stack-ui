import importlib
import json
import os

from tests.support import Base, compose_yaml, make_stack
from stack_ui import server as srv
from stack_ui import stacks as st


class StackFilesTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = importlib.reload(srv)
        self.f = make_stack(self.stacks, 'web', compose_yaml('web'))
        self.base = {'composeText': compose_yaml('web'), 'envText': 'TZ=Europe/Budapest\n'}

    def save(self, files):
        return self.ui.save_files('web', {**self.base, 'files': files})

    def test_save_writes_changed_only_and_reports(self):
        (self.f / 'old.txt').write_bytes(b'keep\n')
        r = self.save({'init.sh': 'echo hi\n', 'dash.json': '{"a": 1}\n'})
        self.assertEqual(sorted(r['saved']), ['dash.json', 'init.sh'])
        self.assertEqual(r['onlyOnHost'], ['old.txt'])
        if os.name != 'nt':
            self.assertEqual((self.f / 'init.sh').stat().st_mode & 0o777, 0o755)
        r = self.save({'init.sh': 'echo hi\n', 'dash.json': '{"a": 2}\n'})
        self.assertEqual(r['saved'], ['dash.json'])
        self.assertTrue(any(p.name.startswith('dash.json.') for p in (self.f / '.history').iterdir()))

    def test_page_save_without_files_reports_nothing_only_on_host(self):
        (self.f / 'old.txt').write_bytes(b'keep\n')
        self.assertEqual(self.ui.save_files('web', self.base)['onlyOnHost'], [])

    def test_one_bad_file_writes_nothing(self):
        for files in ({'ok.json': '{}\n', 'bad.json': '{nope\n'}, {'ok.json': '{}\n', 'bad.yaml': 'a: [\n'},
                      {'ok.json': '{}\n', '../x.json': '{}\n'}, {'ok.json': '{}\n', 'x.py': 'print()\n'},
                      {'ok.json': '{}\n', 'stack.json': '{}\n'}, {'ok.json': '{}\n', 'compose.yaml': 'services: {}\n'},
                      {'ok.json': '{}\n', 'big.txt': 'x' * (st.MAX_FILE + 1)}):
            with self.assertRaises(ValueError):
                self.ui.save_files('web', {**self.base, 'composeText': compose_yaml('web') + '# changed\n', 'files': files})
            self.assertFalse((self.f / 'ok.json').exists())
            self.assertNotIn('# changed', (self.f / 'compose.yaml').read_text())

    def test_json_secret_nested_and_reference(self):
        with self.assertRaises(ValueError) as ctx:
            self.save({'ds.json': json.dumps({'datasources': [{'secureJsonData': {'password': 'x'}}]})})
        self.assertIn('ds.json', str(ctx.exception))
        self.save({'ds.json': json.dumps({'datasources': [{'secureJsonData': {'password': '$__file{/run/secrets/x}'}}]})})

    def test_yaml_and_env_like_secrets(self):
        for name, text in (('a.yaml', 'admin_password: hunter2\n'), ('b.env', 'API_TOKEN=abc\n'), ('c.txt', 'SECRET=1\n')):
            with self.assertRaises(ValueError):
                self.save({name: text})
        self.save({'ok.yaml': 'admin_password: ${GF_PASSWORD}\ntoken: ""\nkey: /run/secrets/k\n',
                   'ok.sh': 'PASSWORD=whatever\n', 'ok.md': 'TOKEN=example\n'})

    def test_yaml_list_secret(self):
        with self.assertRaises(ValueError):
            self.save({'svc.yaml': 'environment:\n  - DB_PASSWORD=x\n'})

    def test_preview(self):
        (self.f / 'dash.json').write_bytes(b'{"a": 1}\n')
        d = self.ui.preview_files('web', {**self.base, 'files': {'dash.json': '{"a": 2}\n', 'new.sh': 'echo\n'}})
        self.assertIn('+{"a": 2}', d['files']['dash.json'])
        self.assertIn('+echo', d['files']['new.sh'])
        self.assertFalse((self.f / 'new.sh').exists())

    def test_files_listing_and_view(self):
        (self.f / 'dash.json').write_bytes(b'{"a": 1}\n')
        f = self.ui.files('web')
        self.assertEqual(f['stackFiles'], [{'name': 'dash.json', 'size': 9, 'changed': None}])
        st.deploy(self.f, 'compose.yaml', '.env', pull=False, approve=lambda cs: True, log=lambda _: None)
        self.assertFalse(self.ui.files('web')['stackFiles'][0]['changed'])
        (self.f / 'dash.json').write_bytes(b'{"a": 2}\n')
        self.assertTrue(self.ui.files('web')['stackFiles'][0]['changed'])
        self.assertEqual(self.ui.file_text('web', 'dash.json'), {'name': 'dash.json', 'text': '{"a": 2}\n'})
        for bad in ('compose.yaml', '.env', '../x', 'nope.json'):
            with self.assertRaises(ValueError):
                self.ui.file_text('web', bad)

    def test_delete_and_deleted_file_restorable(self):
        (self.f / 'init.sh').write_bytes(b'echo hi\n')
        self.ui.delete_file('web', {'file': 'init.sh'})
        self.assertFalse((self.f / 'init.sh').exists())
        versions = [v for v in self.ui.history('web') if v['file'] == 'init.sh']
        self.assertEqual(len(versions), 1)
        self.ui.restore('web', {'version': versions[0]['version']})
        self.assertEqual((self.f / 'init.sh').read_text(), 'echo hi\n')
        for bad in ('compose.yaml', '.env', 'stack.json', 'nope.sh'):
            with self.assertRaises(ValueError):
                self.ui.delete_file('web', {'file': bad})

    def test_deleted_file_restorable(self):
        (self.f / 'dash.json').write_bytes(b'{"a": 1}\n')
        self.save({'dash.json': '{"a": 2}\n'})
        (self.f / 'dash.json').unlink()
        names = {v['file'] for v in self.ui.history('web')}
        self.assertIn('dash.json', names)

    def test_restore_rechecks(self):
        hist = self.f / '.history'
        hist.mkdir()
        (hist / 'bad.json.20261021T100000Z').write_bytes(b'{nope\n')
        with self.assertRaises(ValueError):
            self.ui.restore('web', {'version': 'bad.json.20261021T100000Z'})

    def test_upload_with_json_and_checks(self):
        r = self.ui.upload({'name': 'up', 'files': {'compose.yaml': compose_yaml('up'), 'dash.json': '{}\n'}})
        self.assertIn('dash.json', r['files'])
        for files in ({'compose.yaml': compose_yaml('u2'), 'bad.json': '{nope'},
                      {'compose.yaml': compose_yaml('u2'), 'ds.json': '{"password": "x"}'}):
            with self.assertRaises(ValueError):
                self.ui.upload({'name': 'u2', 'files': files})
        self.assertFalse((self.stacks / 'u2').exists())

    def test_diff_reports_files(self):
        (self.f / 'dash.json').write_bytes(b'{"a": 1}\n')
        self.mock.add_container('web', project='web')
        st.deploy(self.f, 'compose.yaml', '.env', pull=False, approve=lambda cs: True, log=lambda _: None)
        (self.f / 'dash.json').write_bytes(b'{"a": 2}\n')
        (self.f / '.env').write_bytes((self.f / '.env').read_bytes() + b'A=1\nB=2\n')
        d = self.ui.diff('web')
        self.assertIn('+{"a": 2}', d['files']['dash.json'])
        self.assertEqual(self.ui.drift(), {'web': {'state': 'differs', 'changes': [
            {'file': '.env', 'added': 2, 'removed': 0}, {'file': 'dash.json', 'added': 1, 'removed': 1}]}})
