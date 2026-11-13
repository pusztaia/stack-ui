import os
import shutil
import unittest

from tests.support import compose_yaml
from tests.test_rollback import V1, V2, RollbackBase
from stack_ui import rollback, schedule, stacks as st


class RecordTest(RollbackBase):
    """The files of the last deploy are kept in the page's data folder; nothing in a stack's folder is read as them."""

    def rec(self):
        return st.record_dir(self.folder)

    def test_the_record_is_written_into_the_data_folder(self):
        self.assertEqual(self.rec(), self.tmp / 'data' / 'deployed' / 'web')
        self.assertEqual((self.rec() / 'compose.yaml').read_text(encoding='utf-8'), V1)
        self.assertFalse((self.folder / '.deployed').exists())

    def test_a_record_planted_in_the_stack_folder_changes_nothing(self):
        (self.folder / 'compose.yaml').write_bytes(V2.encode('utf-8'))
        planted = self.folder / '.deployed'
        planted.mkdir()
        (planted / 'compose.yaml').write_bytes(V2.encode('utf-8'))
        (planted / '.env').write_bytes((self.folder / '.env').read_bytes())
        self.assertEqual(self.ui.drift()['web']['state'], 'differs')
        self.assertEqual(rollback.capture(self.folder, 'compose.yaml', '.env')['files']['compose.yaml'], V1)

    def test_no_record_is_unknown_even_with_one_in_the_stack_folder(self):
        shutil.rmtree(self.rec())
        planted = self.folder / '.deployed'
        planted.mkdir()
        for name in ('compose.yaml', '.env'):
            (planted / name).write_bytes((self.folder / name).read_bytes())
        self.assertEqual(self.ui.drift()['web']['state'], 'unknown')
        self.assertIsNone(rollback.capture(self.folder, 'compose.yaml', '.env'))

    def test_the_next_deploy_removes_a_leftover_and_does_not_follow_a_link(self):
        elsewhere = self.tmp / 'elsewhere'
        elsewhere.mkdir()
        (elsewhere / 'keep.txt').write_text('x', encoding='utf-8')
        try:
            os.symlink(elsewhere, self.folder / '.deployed', target_is_directory=True)
        except (OSError, NotImplementedError):
            (self.folder / '.deployed').mkdir()
        self.deploy()
        self.assertFalse(os.path.lexists(self.folder / '.deployed'))
        self.assertTrue((elsewhere / 'keep.txt').is_file())

    def test_a_deploy_goes_on_when_the_record_cannot_be_written(self):
        blocker = self.tmp / 'blocker'
        blocker.write_text('x', encoding='utf-8')
        self.patch(st, 'DATA', blocker)
        said = []
        st.deploy(self.folder, 'compose.yaml', '.env', approve=lambda c: True, pull=False, point=False, log=said.append)
        self.assertTrue(any('record' in line and 'could not be written' in line for line in said), said)

    def test_deleting_the_folder_and_uploading_drop_the_record(self):
        self.mock.containers.clear()
        self.ui.stack_action('web', 'delete', {'confirm': 'web', 'deleteFolder': True})
        self.assertFalse(self.rec().exists())
        left = self.tmp / 'data' / 'deployed' / 'up'
        left.mkdir(parents=True)
        (left / 'compose.yaml').write_text('old', encoding='utf-8')
        self.ui.upload({'name': 'up', 'files': {'compose.yaml': compose_yaml('up')}})
        self.assertFalse(left.exists())

    def test_the_file_list_marks_changes_against_the_record_in_the_data_folder(self):
        (self.folder / 'notes.txt').write_text('a', encoding='utf-8')
        self.deploy()
        (self.folder / 'notes.txt').write_text('b', encoding='utf-8')
        row = next(f for f in self.ui.files('web')['stackFiles'] if f['name'] == 'notes.txt')
        self.assertTrue(row['changed'])


class AdoptTest(RollbackBase):
    """Records written into the stack folders before the move are taken over only when they match the hashes a
    deploy from the page stored."""

    def legacy(self, text=V1):
        shutil.rmtree(st.record_dir(self.folder))
        old = self.folder / '.deployed'
        old.mkdir()
        (old / 'compose.yaml').write_bytes(text.encode('utf-8'))
        (old / '.env').write_bytes((self.folder / '.env').read_bytes())
        return old

    def test_a_record_matching_the_stored_hashes_is_taken_over(self):
        schedule.approve(self.folder, 'compose.yaml', '.env')
        self.legacy()
        self.assertEqual(self.ui.adopt_records(), ['web'])
        self.assertEqual(self.ui.drift()['web']['state'], 'same')
        self.assertEqual(self.ui.adopt_records(), [])                        # already there

    def test_a_record_that_does_not_match_or_has_no_hashes_is_not(self):
        self.legacy()
        self.assertEqual(self.ui.adopt_records(), [])                        # nothing stored for it
        schedule.approve(self.folder, 'compose.yaml', '.env')
        (self.folder / '.deployed' / 'compose.yaml').write_bytes(V2.encode('utf-8'))
        self.assertEqual(self.ui.adopt_records(), [])
        self.assertEqual(self.ui.drift()['web']['state'], 'unknown')

    def test_a_linked_record_is_not_followed(self):
        schedule.approve(self.folder, 'compose.yaml', '.env')
        old = self.legacy()
        moved = self.tmp / 'moved'
        old.rename(moved)
        try:
            os.symlink(moved, old, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('no symlinks here')
        self.assertEqual(self.ui.adopt_records(), [])


class SnapshotTest(RollbackBase):
    """The record and the hashes are made from the bytes read once, before `up`: not from the folder afterwards."""

    def test_the_record_is_what_was_there_before_up_not_what_is_there_after(self):
        real = st.run_compose

        def tamper(folder, compose_file, env_file, args, log, **kw):
            if args[0] == 'up':
                (self.folder / 'compose.yaml').write_bytes(V2.encode('utf-8'))
            return real(folder, compose_file, env_file, args, log, **kw)
        self.patch(st, 'run_compose', tamper)
        self.deploy()
        self.assertEqual((st.record_dir(self.folder) / 'compose.yaml').read_text(encoding='utf-8'), V1)
        self.assertFalse(schedule.approved(self.folder, 'compose.yaml', '.env'))     # the folder is not what was deployed
        self.assertEqual(self.ui.drift()['web']['state'], 'differs')

    def test_before_up_gets_the_hashes_of_the_files_about_to_be_used(self):
        seen = []
        self.deploy(before_up=seen.append)
        self.assertEqual(seen, [schedule.fingerprint(self.folder, 'compose.yaml', '.env')])

    def test_an_oversized_file_is_left_out_of_the_record_and_the_log_says_so(self):
        with open(self.folder / 'big.json', 'wb') as fh:
            fh.truncate(st.MAX_FILE + 1)
        said = []
        st.deploy(self.folder, 'compose.yaml', '.env', approve=lambda c: True, pull=False, log=said.append)
        self.assertFalse((st.record_dir(self.folder) / 'big.json').exists())
        self.assertTrue((st.record_dir(self.folder) / 'compose.yaml').is_file())
        self.assertTrue(any('big.json' in line for line in said), said)

    def test_read_plain_reads_regular_files_within_the_limit_only(self):
        path = self.folder / 'compose.yaml'
        self.assertEqual(st.read_plain(path), V1.encode('utf-8'))
        self.assertIsNone(st.read_plain(path, limit=3))
        self.assertIsNone(st.read_plain(self.folder))
        self.assertIsNone(st.read_plain(self.folder / 'missing'))
        link = self.folder / 'link.yaml'
        try:
            os.symlink(path, link)
        except (OSError, NotImplementedError):
            return
        self.assertIsNone(st.read_plain(link))

    def test_a_linked_file_inside_an_old_record_is_not_taken_over(self):
        schedule.approve(self.folder, 'compose.yaml', '.env')
        shutil.rmtree(st.record_dir(self.folder))
        old = self.folder / '.deployed'
        old.mkdir()
        (old / '.env').write_bytes((self.folder / '.env').read_bytes())
        try:
            os.symlink(self.folder / 'compose.yaml', old / 'compose.yaml')
        except (OSError, NotImplementedError):
            self.skipTest('no symlinks here')
        self.assertEqual(self.ui.adopt_records(), [])

if __name__ == '__main__':
    unittest.main()
