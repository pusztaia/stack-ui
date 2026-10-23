import importlib
import os

from tests.support import Base, compose_yaml, make_stack
from stack_ui import server as srv, stacks as st


class StackDeleteTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = importlib.reload(srv)
        self.folder = make_stack(self.stacks, 'web', compose_yaml('web'))

    def deployed(self):
        self.mock.add_container('web', project='web')

    def test_delete_with_the_folder_removes_it_after_the_containers(self):
        self.deployed()
        self.ui.stack_action('web', 'delete', {'confirm': 'web', 'deleteFolder': True})
        self.assertEqual(self.compose_calls()[-1][-1], 'down')
        self.assertFalse(self.folder.exists())

    def test_delete_without_the_folder_keeps_it(self):
        self.deployed()
        self.ui.stack_action('web', 'delete', {'confirm': 'web'})
        self.assertTrue(self.folder.is_dir())

    def test_a_stack_without_containers_loses_only_its_folder(self):
        self.ui.stack_action('web', 'delete', {'confirm': 'web', 'deleteFolder': True})
        self.assertFalse(self.folder.exists())
        self.assertEqual(self.compose_calls(), [])

    def test_a_stack_without_containers_is_not_deleted_without_the_folder(self):
        with self.assertRaises(self.ui.NotDeployed):
            self.ui.stack_action('web', 'delete', {'confirm': 'web'})
        self.assertTrue(self.folder.is_dir())

    def test_the_name_must_be_typed(self):
        self.deployed()
        for data in ({}, {'confirm': 'we'}, {'confirm': 'web ', 'deleteFolder': True}):
            with self.assertRaises(ValueError):
                self.ui.stack_action('web', 'delete', data)
        self.assertTrue(self.folder.is_dir())
        self.assertEqual(self.compose_calls(), [])

    def test_a_failed_down_keeps_the_folder(self):
        self.deployed()
        os.environ['FAKE_COMPOSE_FAIL_ON'] = 'down'
        with self.assertRaises(st.StackError):
            self.ui.stack_action('web', 'delete', {'confirm': 'web', 'deleteFolder': True})
        self.assertTrue(self.folder.is_dir())

    def test_a_protected_stack_keeps_its_folder(self):
        self.patch(self.ui, 'PROTECTED', {'web'})
        for deleted in (True, False):
            with self.assertRaises(ValueError):
                self.ui.stack_action('web', 'delete', {'confirm': 'web', 'deleteFolder': deleted})
        self.assertTrue(self.folder.is_dir())

    def test_a_link_to_a_folder_elsewhere_is_not_followed(self):
        outside = self.tmp / 'elsewhere'
        outside.mkdir()
        (outside / 'compose.yaml').write_text(compose_yaml('lnk'))
        (outside / 'keep.txt').write_text('mine')
        try:
            os.symlink(outside, self.stacks / 'lnk', target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('this system cannot make symbolic links')
        with self.assertRaises(ValueError):
            self.ui.stack_action('lnk', 'delete', {'confirm': 'lnk', 'deleteFolder': True})
        self.assertTrue((outside / 'keep.txt').is_file())

    def test_only_a_plain_folder_of_the_stacks_dir_is_removed(self):
        outside = self.tmp / 'elsewhere'
        outside.mkdir()
        (outside / 'keep.txt').write_text('mine')
        for target in (outside, self.stacks, self.stacks / 'missing', self.folder / 'compose.yaml'):
            with self.assertRaises(ValueError):
                self.ui.remove_stack_folder(target)
        self.assertTrue((outside / 'keep.txt').is_file())
        self.assertTrue(self.stacks.is_dir())
        self.assertTrue((self.folder / 'compose.yaml').is_file())

    def test_our_own_folders_do_not_count_as_application_data(self):
        (self.folder / '.history').mkdir()
        (self.folder / st.DEPLOYED).mkdir()
        self.assertFalse(self.ui.stack_folder('web')['hasFolders'])

    def test_the_folder_listing_shows_files_and_folders_with_sizes(self):
        (self.folder / 'data').mkdir()
        (self.folder / 'data' / 'db.bin').write_bytes(b'x' * 1000)
        (self.folder / '.history').mkdir()
        r = self.ui.stack_folder('web')
        entries = {e['name']: e for e in r['entries']}
        self.assertEqual(entries['data'], {'name': 'data', 'dir': True, 'size': 1000, 'complete': True})
        self.assertTrue(entries['.history']['dir'])
        self.assertFalse(entries['compose.yaml']['dir'])
        self.assertTrue(r['hasFolders'])
        self.assertEqual([e['name'] for e in r['entries']], sorted(entries))

    def test_a_folder_listing_without_subfolders_says_so(self):
        self.assertFalse(self.ui.stack_folder('web')['hasFolders'])
