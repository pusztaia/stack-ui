"""Two things a stack's files must survive: a link planted at the temp name of a save (a container that mounts the stack's
folder can plant one: the page writes as root), and a file that is not UTF-8 text (an old editor, a container)."""
import json
import os
import unittest

from tests.support import Base, compose_yaml, make_stack, fresh_server
from tests.test_stack_paths import link
from stack_ui import stacks as st


class TempNameLinkTest(Base):
    """The page writes a file into <name>.tmp and renames it over the real one; a link at that name must not be followed."""

    def setUp(self):
        super().setUp()
        self.ui = fresh_server()
        self.outside = self.tmp / 'outside'
        self.outside.mkdir()
        self.victim = self.outside / 'victim.txt'
        self.victim.write_text('ORIGINAL\n', encoding='utf-8')
        self.folder = make_stack(self.stacks, 'web', compose_yaml('web'))

    def test_a_save_does_not_write_through_a_link_at_the_temp_name(self):
        for name in ('.compose.yaml.tmp', '.env.tmp'):
            link(self.victim, self.folder / name)
        r = self.ui.save_files('web', {'composeText': compose_yaml('web') + '# changed\n', 'envText': 'TZ=UTC\n'})
        self.assertEqual(sorted(r['saved']), ['.env', 'compose.yaml'])
        self.assertEqual(self.victim.read_text(encoding='utf-8'), 'ORIGINAL\n', 'what the link pointed at was overwritten')
        compose = self.folder / 'compose.yaml'
        self.assertFalse(compose.is_symlink())
        self.assertIn('# changed', compose.read_text(encoding='utf-8'))
        self.assertEqual((self.folder / '.env').read_text(encoding='utf-8'), 'TZ=UTC\n')
        self.assertEqual([f.name for f, _, _ in st.discover()], ['web'], 'the stack must still be one')

    def test_the_manifest_is_not_written_through_a_dangling_link(self):
        target = self.outside / 'created-by-the-page.json'
        link(target, self.folder / 'stack.json')
        self.ui.save_files('web', {'composeText': compose_yaml('web'), 'envText': 'TZ=Europe/Budapest\n',
                                   'files': {'extra.json': '{}\n'}})
        self.assertFalse(target.exists(), 'the page created a file where the link pointed')
        manifest = self.folder / 'stack.json'
        self.assertFalse(manifest.is_symlink())
        self.assertEqual(json.loads(manifest.read_text(encoding='utf-8'))['compose'], 'compose.yaml')

    def test_the_history_copy_does_not_follow_a_link(self):
        """A container can swap a file for a link between the page's check and its copy: backup() reads without following it
        (what the link points at would land in .history/, which the container can read)."""
        secret = self.outside / 'settings.json'
        secret.write_text('{"api_token_sha256": "SECRET"}\n', encoding='utf-8')
        link(secret, self.folder / 'app.json')
        with self.assertRaises(ValueError):
            self.ui.stackfiles.backup(self.folder / 'app.json')
        history = self.folder / '.history'
        self.assertEqual(list(history.iterdir()) if history.is_dir() else [], [], 'something was copied')

    def test_no_temp_file_is_left_behind(self):
        self.ui.save_files('web', {'composeText': compose_yaml('web') + '# a\n', 'envText': 'TZ=UTC\n'})
        self.assertEqual([p.name for p in self.folder.iterdir() if p.name.endswith('.tmp')], [])

    def test_a_saved_file_keeps_its_mode_rules(self):
        self.ui.save_files('web', {'composeText': compose_yaml('web'), 'envText': 'TZ=UTC\n',
                                   'files': {'run.sh': '#!/bin/sh\necho hi\n'}})
        if os.name != 'nt':
            self.assertTrue(os.access(self.folder / 'run.sh', os.X_OK), 'a .sh file is executable')


class NotUtf8Test(Base):
    """One stack whose compose file is Windows-1250 text must not take the others down with it."""

    def setUp(self):
        super().setUp()
        self.ui = fresh_server()
        make_stack(self.stacks, 'web', compose_yaml('web'))
        old = make_stack(self.stacks, 'old', compose_yaml('old'))
        # a Hungarian comment as an old Windows editor saves it: 0xF5 and 0xFB are no UTF-8 (a lone start byte)
        (old / 'compose.yaml').write_bytes(compose_yaml('old').encode('utf-8') + '# őűáé\n'.encode('cp1250'))
        self.mock.add_container('web', project='web')

    def test_the_stacks_are_all_listed(self):
        self.assertEqual([s['name'] for s in self.ui.overview()['stacks']], ['old', 'web'])

    def test_the_container_and_image_lists_work(self):
        self.assertIn('containers', self.ui.container_table())
        self.assertIsInstance(self.ui.images(), dict)

    def test_the_update_check_lists_the_others(self):
        self.mock.images.append({'Id': 'img1', 'RepoTags': ['docker.io/library/nginx:latest'], 'RepoDigests': [],
                                 'Size': 1, 'Created': 1})
        self.assertIn('web', self.ui.registry.stack_images())

    def test_the_page_can_open_and_fix_the_file(self):
        shown = self.ui.files('old')
        self.assertIn('�', shown['composeText'], 'the bad bytes show as the replacement character')
        self.ui.preview_files('old', {'composeText': shown['composeText'], 'envText': shown['envText']})
        # saving exactly what the page showed must still write: the file's bytes differ from that text
        r = self.ui.save_files('old', {'composeText': shown['composeText'], 'envText': shown['envText']})
        self.assertEqual(r['saved'], ['compose.yaml'])
        (self.stacks / 'old' / 'compose.yaml').read_bytes().decode('utf-8')    # valid UTF-8 now
        fixed = compose_yaml('old') + '# fixed\n'
        r = self.ui.save_files('old', {'composeText': fixed, 'envText': shown['envText']})
        self.assertEqual(r['saved'], ['compose.yaml'])
        self.assertEqual((self.stacks / 'old' / 'compose.yaml').read_text(encoding='utf-8'), fixed)


if __name__ == '__main__':
    unittest.main()
