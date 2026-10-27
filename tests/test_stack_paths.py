import importlib
import json
import os
import shutil
import unittest

from tests.support import Base, compose_yaml, make_stack
from stack_ui import server as srv
from stack_ui import stacks as st


def link(target, path):
    try:
        os.symlink(target, path)
    except (OSError, NotImplementedError):
        raise unittest.SkipTest('no symbolic links here') from None


class StackPathsTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = importlib.reload(srv)
        self.outside = self.tmp / 'outside.txt'
        self.outside.write_text('services:\n  x: {image: nginx}\n', encoding='utf-8')

    def names(self):
        return [f.name for f, _, _ in st.discover()]

    def test_manifest_cannot_point_outside_the_folder(self):
        for compose, env in ((str(self.outside), None), ('../outside.txt', None), ('compose.yaml', str(self.outside)),
                             ('compose.yaml', '../outside.txt'), ('sub/compose.yaml', None)):
            f = make_stack(self.stacks, 'web', compose_yaml('web'))
            (f / 'stack.json').write_text(json.dumps({'compose': compose, 'env': env}), encoding='utf-8')
            self.assertEqual(self.names(), [], (compose, env))
            self.assertIsNone(st.stack_files(f))
            shutil.rmtree(f)

    def test_manifest_with_plain_names_still_works(self):
        f = make_stack(self.stacks, 'web', compose_yaml('web'), compose_name='my.yaml', env_name='my.env')
        (f / 'stack.json').write_text(json.dumps({'compose': 'my.yaml', 'env': 'my.env'}), encoding='utf-8')
        self.assertEqual(st.stack_files(f), ('my.yaml', 'my.env'))

    def test_a_linked_compose_or_env_file_is_not_followed(self):
        f = make_stack(self.stacks, 'web', compose_yaml('web'), env=None)
        (f / 'compose.yaml').unlink()
        link(self.outside, f / 'compose.yaml')
        self.assertEqual(self.names(), [])
        (f / 'compose.yaml').unlink()
        (f / 'compose.yaml').write_text(compose_yaml('web'), encoding='utf-8')
        link(self.outside, f / '.env')
        self.assertEqual(st.stack_files(f), ('compose.yaml', None))

    def test_a_linked_stack_file_is_not_listed_read_or_written(self):
        f = make_stack(self.stacks, 'web', compose_yaml('web'))
        link(self.outside, f / 'notes.txt')
        self.assertEqual(self.ui.files('web')['stackFiles'], [])
        with self.assertRaises(ValueError):
            self.ui.file_text('web', 'notes.txt')
        before = self.outside.read_text(encoding='utf-8')
        body = {'composeText': compose_yaml('web') + '# changed\n', 'envText': 'TZ=UTC\n', 'files': {'notes.txt': 'x\n'}}
        with self.assertRaises(ValueError):
            self.ui.save_files('web', body)
        self.assertEqual(self.outside.read_text(encoding='utf-8'), before)
        self.assertNotIn('# changed', (f / 'compose.yaml').read_text(encoding='utf-8'))
        self.assertFalse((f / '.history').exists())
