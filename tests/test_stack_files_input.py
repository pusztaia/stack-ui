"""The refusals of the stack file checks: what a page or a script sends is refused with a plain message, and a
refused request leaves nothing on disk (no module, no half-written upload folder, no changed file)."""
import os
import unittest

from tests.support import Base, compose_yaml, fresh_server, make_stack


class InputChecksTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = fresh_server()
        from stack_ui import stackfiles
        self.sf = stackfiles
        self.f = make_stack(self.stacks, 'web', compose_yaml('web'))

    def refused(self, fn, *args, text=''):
        with self.assertRaises(ValueError) as ctx:
            fn(*args)
        self.assertIn(text, str(ctx.exception))
        return str(ctx.exception)

    def test_env_lines(self):
        self.sf.check_env('# a comment\n\nTZ=Europe/Budapest\nEMPTY_TOKEN=\nQUOTED_PASSWORD=""\n')
        self.refused(self.sf.check_env, 'TZ=x\njust words\n', text='env line 2 is not KEY=value')
        self.refused(self.sf.check_env, '1BAD=x\n', text='not KEY=value')
        self.refused(self.sf.check_env, 'A B=x\n', text='not KEY=value')
        for line in ('DB_PASSWORD=hunter2', 'api_key=x', 'GH_TOKEN="abc"', 'APP_SECRET = s'):
            self.refused(self.sf.check_env, line + '\n', text='holds a secret')

    def test_compose_needs_services(self):
        self.sf.check_compose(compose_yaml('x'))
        for text in ('a: [\n', 'just text\n', 'services: {}\n', 'services:\n', 'volumes: {}\n', '- a\n'):
            self.refused(self.sf.check_compose, text)

    def test_the_files_field(self):
        self.assertEqual(self.sf.incoming_files({}, 'compose.yaml', '.env'), {})
        self.refused(self.sf.incoming_files, {'files': ['a.json']}, 'compose.yaml', '.env', text='must be an object')
        self.refused(self.sf.incoming_files, {'files': {'a.json': 7}}, 'compose.yaml', '.env', text='must be text')
        self.refused(self.sf.incoming_files, {'files': {'compose.yaml': 'x'}}, 'compose.yaml', '.env', text='composeText')
        self.refused(self.sf.incoming_files, {'files': {'web.env': 'x'}}, 'compose.yaml', 'web.env', text='envText')
        self.refused(self.sf.incoming_files, {'files': {'.env': 'A=1\n'}}, 'compose.yaml', 'web.env', text='".env"')
        self.assertEqual(self.sf.incoming_files({'files': {'a.sh': 'echo\r\nhi'}}, 'compose.yaml', '.env'),
                         {'a.sh': 'echo\nhi\n'})             # CRLF and a missing last newline are made LF

    def assert_nothing_left(self, name):
        self.assertFalse((self.stacks / name).exists())
        self.assertEqual([p.name for p in self.stacks.iterdir() if p.name.startswith('.upload-')], [])

    def test_upload_refusals_leave_nothing(self):
        ok = {'compose.yaml': compose_yaml('up')}
        cases = [
            ({'name': 'Bad Name', 'files': ok}, 'the stack name'),
            ({'name': '../up', 'files': ok}, 'the stack name'),
            ({'name': 'up', 'files': {}}, 'no files'),
            ({'name': 'up', 'files': ['compose.yaml']}, 'no files'),
            ({'name': 'up', 'files': {**ok, 'run.py': 'print()\n'}}, 'only'),
            ({'name': 'up', 'files': {**ok, 'stack.json': '{}\n'}}, 'only'),
            ({'name': 'up', 'files': {'readme.txt': 'x\n'}}, 'needs a compose file'),
            ({'name': 'up', 'files': {**ok, 'other.yaml': compose_yaml('o')}}, 'choose the compose file'),
            ({'name': 'up', 'files': {**ok, 'a.env': 'A=1\n', 'b.env': 'B=1\n'}, 'env': 'c.env'}, 'choose the env file'),
            ({'name': 'up', 'files': {'compose.yaml': 'services: {}\n'}}, 'services'),
            ({'name': 'up', 'files': {**ok, 'up.env': 'DB_PASSWORD=x\n'}}, 'holds a secret'),
        ]
        for data, text in cases:
            self.refused(self.ui.upload, data, text=text)
            self.assert_nothing_left('up')

    def test_upload_of_an_ignored_or_existing_name(self):
        self.patch(self.sf.st, 'IGNORE', {'skipme'})
        self.refused(self.ui.upload, {'name': 'skipme', 'files': {'compose.yaml': compose_yaml('s')}}, text='ignored')
        self.assert_nothing_left('skipme')
        with self.assertRaises(FileExistsError):
            self.ui.upload({'name': 'web', 'files': {'compose.yaml': compose_yaml('web2')}})
        self.assertEqual((self.f / 'compose.yaml').read_text(encoding='utf-8'), compose_yaml('web'))
        self.assertEqual([p.name for p in self.stacks.iterdir() if p.name.startswith('.upload-')], [])

    def test_an_upload_takes_only_the_base_name_and_chooses_the_named_files(self):
        r = self.ui.upload({'name': 'up', 'compose': 'b.yaml', 'env': 'b.env',
                            'files': {'sub/a.yaml': compose_yaml('a'), 'b.yaml': compose_yaml('b'),
                                      'a.env': 'A=1\n', 'b.env': 'B=1\n', 'init.sh': 'echo hi\n'}})
        folder = self.stacks / 'up'
        self.assertTrue((folder / 'a.yaml').is_file())          # 'sub/a.yaml' lands as a.yaml in the module
        self.assertFalse((folder / 'sub').exists())
        self.assertIn('b.yaml', str(r))
        if os.name != 'nt':
            self.assertEqual((folder / 'init.sh').stat().st_mode & 0o777, 0o755)

    def test_an_unknown_history_version(self):
        for version in ('nope', '../compose.yaml', 'compose.yaml.20261021T100000Z'):
            self.refused(self.ui.history_version, 'web', version, text='no saved version')
            self.refused(self.ui.restore, 'web', {'version': version}, text='no saved version')

    @unittest.skipIf(os.name == 'nt', 'links need privileges on Windows')
    def test_a_save_does_not_write_through_a_link(self):
        target = self.tmp / 'outside.json'
        target.write_bytes(b'{"keep": 1}\n')
        (self.f / 'dash.json').symlink_to(target)
        with self.assertRaises(ValueError):
            self.ui.save_files('web', {'composeText': compose_yaml('web'), 'envText': 'TZ=Europe/Budapest\n',
                                       'files': {'dash.json': '{"new": 1}\n'}})
        self.assertEqual(target.read_bytes(), b'{"keep": 1}\n')


if __name__ == '__main__':
    unittest.main()
