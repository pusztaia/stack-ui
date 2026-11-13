import importlib
import json
from datetime import datetime
import os
import shutil
import threading
import unittest

from tests.support import Base, compose_yaml, make_stack
from stack_ui import podman_api as pa, rollback, schedule, server as srv, stacks as st

V1 = compose_yaml('web')
V2 = compose_yaml('web') + '# v2\n'
REF = 'docker.io/library/nginx:latest'


class RollbackBase(Base):
    """A stack web that the page deployed before (record in .deployed/), running image old1 under the tag REF."""

    def setUp(self):
        super().setUp()
        self.ui = importlib.reload(srv)
        self.folder = make_stack(self.stacks, 'web', V1)
        self.mock.images.append({'Id': 'old1', 'RepoTags': [REF], 'RepoDigests': [], 'Size': 10, 'Created': 1})
        self.container = self.mock.add_container('web', project='web', image=REF, image_id='old1')
        st.save_deployed(self.folder, 'compose.yaml', '.env')

    def deploy(self, **kw):
        kw.setdefault('pull', False)
        return st.deploy(self.folder, 'compose.yaml', '.env', approve=lambda c: True, log=lambda _: None, **kw)

    def update_to_v2(self):
        """An update from the page: a new compose text and, as a pull and a recreate would do, a new image under
        the same tag (the fake podman-compose changes nothing on the mock itself)."""
        (self.folder / 'compose.yaml').write_bytes(V2.encode('utf-8'))
        self.deploy()
        self.mock.images[0]['RepoTags'] = None
        self.mock.images.append({'Id': 'new1', 'RepoTags': [REF], 'RepoDigests': [], 'Size': 11, 'Created': 2})
        self.container['ImageID'] = 'new1'
        if self.compose_log.exists():
            self.compose_log.unlink()

    def tags(self):
        return {i['Id']: i['RepoTags'] for i in self.mock.images}

    @property
    def pdir(self):
        return rollback.point_dir(self.folder)


class PointFilesTest(RollbackBase):
    def test_capture_describes_the_state_of_the_last_deploy(self):
        (self.folder / 'compose.yaml').write_bytes(V2.encode('utf-8'))      # edited since: the record is what counts
        point = rollback.capture(self.folder, 'compose.yaml', '.env')
        self.assertEqual(point['files']['compose.yaml'], V1)
        self.assertEqual(point['files']['.env'], 'TZ=Europe/Budapest\n')
        self.assertEqual(point['images'], [{'container': 'web', 'ref': REF, 'id': 'old1'}])
        self.assertEqual((point['compose'], point['env']), ('compose.yaml', '.env'))
        self.assertRegex(point['taken'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')

    def test_nothing_to_go_back_to_without_a_record_or_without_containers(self):
        self.mock.containers.clear()
        self.assertIsNone(rollback.capture(self.folder, 'compose.yaml', '.env'))
        self.mock.containers.append(self.container)
        shutil.rmtree(st.record_dir(self.folder))
        self.assertIsNone(rollback.capture(self.folder, 'compose.yaml', '.env'))

    def test_a_point_is_written_and_read_back_whole(self):
        point = rollback.capture(self.folder, 'compose.yaml', '.env')
        rollback.write_point(self.folder, point)
        self.assertEqual(rollback.load(self.folder), point)
        self.assertTrue(rollback.exists(self.folder))
        self.assertFalse((self.pdir.with_name('web.new')).exists())
        rollback.write_point(self.folder, {**point, 'taken': '2030-01-01T00:00:00Z'})     # replaces the old one
        self.assertEqual(rollback.load(self.folder)['taken'], '2030-01-01T00:00:00Z')

    def test_a_damaged_point_reads_as_none(self):
        rollback.write_point(self.folder, rollback.capture(self.folder, 'compose.yaml', '.env'))
        (self.pdir / 'rollback.json').write_text('{', encoding='utf-8')
        self.assertIsNone(rollback.load(self.folder))
        (self.pdir / 'rollback.json').write_text(json.dumps({'images': 'x'}), encoding='utf-8')
        self.assertIsNone(rollback.load(self.folder))
        self.assertFalse(rollback.exists(self.folder / 'nowhere'))

    def test_odd_files_in_a_point_are_ignored(self):
        rollback.write_point(self.folder, rollback.capture(self.folder, 'compose.yaml', '.env'))
        files = self.pdir / 'files'
        (files / 'run.exe').write_text('x', encoding='utf-8')
        (files / 'a b.json').write_text('{}', encoding='utf-8')
        (files / '.hidden.yaml').write_text('x', encoding='utf-8')
        self.assertEqual(sorted(rollback.load(self.folder)['files']), ['.env', 'compose.yaml'])

    def test_a_point_without_its_compose_file_reads_as_none(self):
        rollback.write_point(self.folder, rollback.capture(self.folder, 'compose.yaml', '.env'))
        (self.pdir / 'files' / 'compose.yaml').unlink()
        self.assertIsNone(rollback.load(self.folder))

    def test_keep_writes_or_drops_a_stale_point(self):
        point = rollback.capture(self.folder, 'compose.yaml', '.env')
        rollback.keep(self.folder, point)
        self.assertTrue(rollback.exists(self.folder))
        said = []
        rollback.keep(self.folder, None, said.append)
        self.assertFalse(rollback.exists(self.folder))
        self.assertTrue(any('no rollback point' in line for line in said), said)
        rollback.keep(self.folder, point)
        rollback.keep(self.folder, None)                     # silently, without a log
        self.assertFalse(rollback.exists(self.folder))

    def test_discard_removes_the_point_and_a_half_written_one(self):
        rollback.write_point(self.folder, rollback.capture(self.folder, 'compose.yaml', '.env'))
        self.pdir.with_name('web.new').mkdir()
        rollback.discard(self.folder)
        self.assertFalse((self.pdir).exists())
        self.assertFalse((self.pdir.with_name('web.new')).exists())
        rollback.discard(self.folder)                        # nothing there: no error


class ProblemsTest(RollbackBase):
    def point(self):
        self.update_to_v2()
        return rollback.load(self.folder)

    def test_a_point_that_can_be_used_has_no_problems(self):
        self.assertEqual(rollback.problems(self.folder, 'compose.yaml', '.env', self.point()), [])

    def test_problems_names_what_blocks_a_rollback(self):
        point = self.point()
        names = rollback.problems(self.folder, 'compose.yml', None, point)
        self.assertEqual(len(names), 1)
        self.assertIn('named differently', names[0])
        self.mock.images[:] = [i for i in self.mock.images if i['Id'] != 'old1']
        gone = rollback.problems(self.folder, 'compose.yaml', '.env', point)
        self.assertEqual(len(gone), 1)
        self.assertIn('web', gone[0])
        self.assertIn(REF, gone[0])
        self.assertIn('no longer on this host', gone[0])

    def test_podman_not_answering_is_a_problem_not_a_crash(self):
        point = self.point()
        self.patch(pa, 'API_URL', 'http://127.0.0.1:9')
        out = rollback.problems(self.folder, 'compose.yaml', '.env', point)
        self.assertEqual(len(out), 1)
        self.assertIn('Podman', out[0])


class ViewTest(RollbackBase):
    def test_no_point_no_view(self):
        self.assertEqual(rollback.view(self.folder, 'compose.yaml', '.env'), {'available': False})

    def test_the_view_shows_images_files_and_problems(self):
        self.update_to_v2()
        v = rollback.view(self.folder, 'compose.yaml', '.env')
        self.assertTrue(v['available'])
        self.assertRegex(v['taken'], r'Z$')
        self.assertEqual(v['images'], [{'container': 'web', 'ref': REF, 'previous': 'old1', 'current': 'new1',
                                        'present': True}])
        self.assertIn('-# v2', v['diff']['compose'])          # the folder has it now, the point does not
        self.assertEqual(v['diff']['env'], '')
        self.assertEqual(v['diff']['files'], {})
        self.assertEqual(v['problems'], [])

    def test_a_container_that_is_gone_has_no_current_image(self):
        self.update_to_v2()
        self.mock.containers.clear()
        v = rollback.view(self.folder, 'compose.yaml', '.env')
        self.assertIsNone(v['images'][0]['current'])

    def test_a_missing_image_is_shown_and_listed_as_a_problem(self):
        self.update_to_v2()
        self.mock.images[:] = [i for i in self.mock.images if i['Id'] != 'old1']
        v = rollback.view(self.folder, 'compose.yaml', '.env')
        self.assertFalse(v['images'][0]['present'])
        self.assertEqual(len(v['problems']), 1)


class KeptImagesTest(RollbackBase):
    def test_the_images_of_the_points_are_named_with_their_stacks(self):
        self.assertEqual(rollback.kept_images(), {})
        self.update_to_v2()
        self.assertEqual(rollback.kept_images(), {'old1': ['web']})

    def test_short_id_drops_the_algorithm(self):
        self.assertEqual(rollback.short_id('sha256:abc'), 'abc')
        self.assertEqual(rollback.short_id('abc'), 'abc')
        self.assertEqual(rollback.short_id(None), '')


class DeployPointTest(RollbackBase):
    def test_an_update_keeps_the_previous_state_as_the_rollback_point(self):
        self.update_to_v2()
        point = rollback.load(self.folder)
        self.assertEqual(point['files']['compose.yaml'], V1)               # not the new text
        self.assertEqual(point['images'], [{'container': 'web', 'ref': REF, 'id': 'old1'}])
        self.assertFalse((self.pdir.with_name('web.new')).exists())
        self.assertEqual((st.record_dir(self.folder) / 'compose.yaml').read_text(encoding='utf-8'), V2)

    def test_a_first_create_writes_no_point(self):
        self.mock.containers.clear()
        shutil.rmtree(st.record_dir(self.folder))
        self.deploy()
        self.assertFalse((self.pdir).exists())

    def test_a_create_drops_a_stale_point(self):
        self.update_to_v2()
        self.mock.containers.clear()                                       # the containers were deleted outside
        self.deploy()
        self.assertFalse(rollback.exists(self.folder))

    def test_an_update_without_a_record_of_its_own_says_so(self):
        shutil.rmtree(st.record_dir(self.folder))
        said = []
        st.deploy(self.folder, 'compose.yaml', '.env', approve=lambda c: True, pull=False, log=said.append)
        self.assertFalse(rollback.exists(self.folder))
        self.assertTrue(any('no rollback point' in line for line in said), said)

    def test_a_failed_pull_keeps_the_old_point(self):
        self.update_to_v2()
        (self.folder / 'compose.yaml').write_bytes((V2 + '# v3' + chr(10)).encode('utf-8'))
        os.environ['FAKE_COMPOSE_FAIL_ON'] = 'pull'
        with self.assertRaises(st.StackError):
            self.deploy(pull=True)
        self.assertEqual(rollback.load(self.folder)['files']['compose.yaml'], V1)      # still the point of v1

    def test_a_failed_up_still_has_its_point(self):
        (self.folder / 'compose.yaml').write_bytes(V2.encode('utf-8'))
        os.environ['FAKE_COMPOSE_FAIL_ON'] = 'up'
        with self.assertRaises(st.StackError):
            self.deploy()
        self.assertEqual(rollback.load(self.folder)['files']['compose.yaml'], V1)

    def test_point_false_leaves_the_point_alone(self):
        self.update_to_v2()
        before = rollback.load(self.folder)
        (self.folder / 'compose.yaml').write_bytes((V2 + '# v3' + chr(10)).encode('utf-8'))
        self.deploy(point=False)
        self.assertEqual(rollback.load(self.folder), before)

    def test_deleting_the_stack_removes_the_point(self):
        self.update_to_v2()
        st.stack_action(self.folder, 'compose.yaml', '.env', 'delete', log=lambda _: None)
        self.assertFalse((self.pdir).exists())
        self.assertFalse(st.record_dir(self.folder).exists())


class SplitRefTest(RollbackBase):
    def test_split_ref(self):
        self.assertEqual(rollback.split_ref('docker.io/library/nginx:latest'), ('docker.io/library/nginx', 'latest'))
        self.assertEqual(rollback.split_ref('localhost/app:1'), ('localhost/app', '1'))
        self.assertEqual(rollback.split_ref('myreg:5000/img'), ('myreg:5000/img', 'latest'))
        self.assertEqual(rollback.split_ref('myreg:5000/img:2'), ('myreg:5000/img', '2'))
        for odd in ('', 'nginx@sha256:abcdef', 'docker.io/library/nginx@sha256:abcdef', 'abcdef012345',
                    '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef', 'Bad Ref', '<none>:<none>'):
            self.assertIsNone(rollback.split_ref(odd), odd)


class RollBackTest(RollbackBase):
    def setUp(self):
        super().setUp()
        self.said = []

    def roll_back(self):
        rollback.roll_back(self.folder, 'compose.yaml', '.env', self.ui.write_file, self.said.append)

    def compose_text(self):
        return (self.folder / 'compose.yaml').read_text(encoding='utf-8')

    def test_the_files_the_image_names_and_the_containers_go_back(self):
        self.update_to_v2()
        self.roll_back()
        self.assertEqual(self.compose_text(), V1)
        saved = sorted((self.folder / '.history').glob('compose.yaml.*'))      # the replaced version is kept
        self.assertEqual([p.read_text(encoding='utf-8') for p in saved], [V2])
        self.assertEqual(self.tags()['old1'], [REF])                            # the old image has its name again
        self.assertFalse(self.tags()['new1'])
        self.assertEqual([c[-3:] for c in self.compose_calls()], [['up', '-d', '--force-recreate']])   # no pull
        self.assertEqual((st.record_dir(self.folder) / 'compose.yaml').read_text(encoding='utf-8'), V1)

    def test_the_point_now_describes_the_state_rolled_back_from(self):
        self.update_to_v2()
        self.roll_back()
        point = rollback.load(self.folder)
        self.assertEqual(point['files']['compose.yaml'], V2)
        self.assertEqual(point['images'], [{'container': 'web', 'ref': REF, 'id': 'new1'}])

    def test_rolling_back_twice_returns_to_the_newer_version(self):
        self.update_to_v2()
        self.roll_back()
        self.container['ImageID'] = 'old1'                                       # what the recreate does
        self.roll_back()
        self.assertEqual(self.compose_text(), V2)
        self.assertEqual(self.tags()['new1'], [REF])
        self.assertFalse(self.tags()['old1'])

    def test_a_name_that_is_already_right_is_not_tagged_again(self):
        self.update_to_v2()
        self.mock.images[0]['RepoTags'] = [REF]                                  # old1 has its name again already
        self.mock.images[1]['RepoTags'] = None
        self.roll_back()
        tagged = [c for c in self.mock.calls if c[0] == 'POST' and c[1].endswith('/tag')]
        self.assertEqual(tagged, [])

    def test_the_other_files_of_the_stack_come_back_too_and_later_ones_stay(self):
        (self.folder / 'conf.json').write_bytes(b'{"a": 1}\n')
        st.save_deployed(self.folder, 'compose.yaml', '.env')
        (self.folder / 'conf.json').write_bytes(b'{"a": 2}\n')
        self.update_to_v2()
        (self.folder / 'added.json').write_bytes(b'{}\n')
        self.roll_back()
        self.assertEqual((self.folder / 'conf.json').read_text(encoding='utf-8'), '{"a": 1}\n')
        self.assertTrue((self.folder / 'added.json').is_file())

    def test_no_point_no_rollback(self):
        with self.assertRaisesRegex(st.StackError, 'no rollback point'):
            self.roll_back()

    def test_a_missing_image_refuses_before_any_change(self):
        self.update_to_v2()
        self.mock.images[:] = [i for i in self.mock.images if i['Id'] != 'old1']
        with self.assertRaisesRegex(st.StackError, 'no longer on this host'):
            self.roll_back()
        self.assertEqual(self.compose_text(), V2)
        self.assertEqual(self.tags()['new1'], [REF])
        self.assertEqual(self.compose_calls(), [])
        self.assertFalse((self.folder / '.history').exists())

    def test_a_failing_up_leaves_the_way_forward_open(self):
        self.update_to_v2()
        os.environ['FAKE_COMPOSE_FAIL_ON'] = 'up'
        with self.assertRaises(st.StackError):
            self.roll_back()
        self.assertEqual(self.compose_text(), V1)                                # files and names are the old ones
        self.assertEqual(self.tags()['old1'], [REF])
        self.assertEqual(rollback.load(self.folder)['files']['compose.yaml'], V2)    # the point leads forward
        self.assertEqual(rollback.load(self.folder)['images'][0]['id'], 'new1')

    def test_a_container_in_the_way_is_never_removed(self):
        self.update_to_v2()
        removed = []
        self.patch(pa, 'remove_container', removed.append)
        self.mock.add_container('web2', image=REF, image_id='x')                 # another name, not in the way
        self.roll_back()
        self.assertEqual(removed, [])


class ReviewFixTest(RollbackBase):
    """Findings of the final review: a retry must not replace the good point, a planted link must not redirect the
    writes, a damaged rollback.json must not break more than the rollback."""

    def point(self):
        return rollback.load(self.folder)

    def test_a_retry_after_a_failed_update_keeps_the_good_point(self):
        (self.folder / 'compose.yaml').write_bytes(V2.encode('utf-8'))
        os.environ['FAKE_COMPOSE_FAIL_ON'] = 'up'
        with self.assertRaises(st.StackError):
            self.deploy()                                                   # the update fails at up
        first = self.point()
        self.assertEqual(first['images'][0]['id'], 'old1')
        # up had recreated the container from a new image before it failed
        self.mock.images.append({'Id': 'new1', 'RepoTags': [REF], 'RepoDigests': [], 'Size': 11, 'Created': 2})
        self.mock.images[0]['RepoTags'] = None
        self.container['ImageID'] = 'new1'
        with self.assertRaises(st.StackError):
            self.deploy()                                                   # the retry fails too
        self.assertEqual(self.point()['images'][0]['id'], 'old1')           # not the mix of old files + new image
        self.assertEqual(self.point()['files']['compose.yaml'], V1)
        os.environ.pop('FAKE_COMPOSE_FAIL_ON')
        self.deploy()                                                       # the retry works
        self.assertEqual(self.point()['images'][0]['id'], 'old1')
        self.assertNotIn('pending', self.point())                           # the deploy is complete now

    def test_a_retry_after_up_removed_the_containers_keeps_the_point(self):
        (self.folder / 'compose.yaml').write_bytes(V2.encode('utf-8'))
        os.environ['FAKE_COMPOSE_FAIL_ON'] = 'up'
        with self.assertRaises(st.StackError):
            self.deploy()
        self.mock.containers.clear()                                        # up removed them and created none
        with self.assertRaises(st.StackError):
            self.deploy()
        self.assertEqual(self.point()['images'][0]['id'], 'old1')

    def test_a_finished_update_is_replaced_by_the_next_one(self):
        self.update_to_v2()
        (self.folder / 'compose.yaml').write_bytes((V2 + '# v3' + chr(10)).encode('utf-8'))
        self.deploy()
        self.assertEqual(self.point()['images'][0]['id'], 'new1')           # the point of the update to v3
        self.assertIn('# v2', self.point()['files']['compose.yaml'])

    def link_or_skip(self, link, target):
        try:
            os.symlink(str(target), str(link), target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symbolic links are not available here')

    def test_a_planted_link_does_not_redirect_the_writes(self):
        elsewhere = self.tmp / 'elsewhere'
        elsewhere.mkdir()
        self.pdir.parent.mkdir(parents=True)
        self.link_or_skip(self.pdir.with_name('web.new'), elsewhere)
        rollback.write_point(self.folder, rollback.capture(self.folder, 'compose.yaml', '.env'))
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertTrue(rollback.exists(self.folder))
        self.assertFalse((self.pdir.with_name('web.new')).exists())

    def test_a_link_in_place_of_the_point_is_replaced_not_followed(self):
        elsewhere = self.tmp / 'elsewhere'
        elsewhere.mkdir()
        self.pdir.parent.mkdir(parents=True)
        self.link_or_skip(self.pdir, elsewhere)
        rollback.write_point(self.folder, rollback.capture(self.folder, 'compose.yaml', '.env'))
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertFalse((self.pdir).is_symlink())
        self.assertTrue(rollback.exists(self.folder))

    def test_a_point_behind_a_link_is_not_read(self):
        rollback.write_point(self.folder, rollback.capture(self.folder, 'compose.yaml', '.env'))
        real = self.tmp / 'moved'
        (self.pdir).rename(real)
        self.link_or_skip(self.pdir, real)
        self.assertIsNone(rollback.load(self.folder))
        self.assertFalse(rollback.exists(self.folder))

    def test_a_damaged_rollback_json_breaks_nothing_else(self):
        rollback.write_point(self.folder, rollback.capture(self.folder, 'compose.yaml', '.env'))
        path = self.pdir / 'rollback.json'
        good = json.loads(path.read_text(encoding='utf-8'))
        for key, value in (('compose', ['x']), ('env', 5), ('taken', None), ('taken', 7),
                           ('images', [{'id': ['x'], 'container': 'web', 'ref': REF}]),
                           ('images', [{'id': 'old1', 'container': None, 'ref': REF}]),
                           ('images', [{'id': 'old1', 'container': 'web', 'ref': 3}]), ('images', ['x'])):
            path.write_text(json.dumps({**good, key: value}), encoding='utf-8')
            self.assertIsNone(rollback.load(self.folder), (key, value))
            self.assertEqual(rollback.kept_images(), {})
            self.assertEqual(rollback.view(self.folder, 'compose.yaml', '.env'), {'available': False})
            self.assertEqual(self.ui.images()['reclaimable'], 0)           # the Images page still works


class PointPlaceTest(RollbackBase):
    """The point is kept in the page's own data folder: a container that can write its stack's folder can neither
    plant nor change one, and nothing it leaves there is read as one."""

    def plant(self):
        base = self.folder / '.previous'
        (base / 'files').mkdir(parents=True)
        (base / 'files' / 'compose.yaml').write_bytes(V2.encode('utf-8'))
        (base / 'rollback.json').write_text(json.dumps({
            'taken': '2026-01-01T00:00:00Z', 'compose': 'compose.yaml', 'env': '.env',
            'images': [{'container': 'web', 'ref': 'ghcr.io/pusztaia/stack-ui:1', 'id': 'old1'}]}), encoding='utf-8')

    def test_the_point_is_written_into_the_data_folder(self):
        self.update_to_v2()
        self.assertTrue((self.tmp / 'data' / 'rollback' / 'web' / 'rollback.json').is_file())
        self.assertFalse((self.folder / '.previous').exists())

    def test_a_point_planted_in_the_stack_folder_is_not_one(self):
        self.plant()
        self.assertIsNone(rollback.load(self.folder))
        self.assertFalse(rollback.exists(self.folder))
        self.assertEqual(rollback.kept_images(), {})
        self.assertEqual(rollback.view(self.folder, 'compose.yaml', '.env'), {'available': False})
        with self.assertRaisesRegex(st.StackError, 'no rollback point'):
            rollback.roll_back(self.folder, 'compose.yaml', '.env', self.ui.write_file, lambda _: None)
        self.assertEqual(self.tags()['old1'], [REF])                        # nothing was tagged
        self.assertEqual((self.folder / 'compose.yaml').read_text(encoding='utf-8'), V1)

    def test_a_leftover_in_the_stack_folder_is_removed_by_the_next_deploy(self):
        self.plant()
        self.deploy()
        self.assertFalse((self.folder / '.previous').exists())

    def test_a_point_file_over_the_size_limit_makes_the_point_unusable(self):
        self.update_to_v2()
        with open(self.pdir / 'files' / 'big.json', 'wb') as fh:
            fh.truncate(st.MAX_FILE + 1)
        self.assertIsNone(rollback.load(self.folder))

    def test_an_oversized_record_is_not_read(self):
        self.update_to_v2()
        with open(self.pdir / 'rollback.json', 'ab') as fh:
            fh.write(b' ' * (rollback.MAX_RECORD + 1))
        self.assertIsNone(rollback.load(self.folder))
        self.assertFalse(rollback.exists(self.folder))
        self.assertEqual(rollback.kept_images(), {})

    def test_an_oversized_file_of_the_last_deploy_is_not_captured(self):
        with open(st.record_dir(self.folder) / 'big.json', 'wb') as fh:
            fh.truncate(st.MAX_FILE + 1)
        self.assertNotIn('big.json', rollback.capture(self.folder, 'compose.yaml', '.env')['files'])
        with open(st.record_dir(self.folder) / 'compose.yaml', 'wb') as fh:
            fh.truncate(st.MAX_FILE + 1)
        self.assertIsNone(rollback.capture(self.folder, 'compose.yaml', '.env'))

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFOs are POSIX')
    def test_a_fifo_in_place_of_the_record_does_not_hang(self):
        (self.pdir / 'files').mkdir(parents=True)
        os.mkfifo(self.pdir / 'rollback.json')
        out = {}

        def read():
            out['point'] = rollback.load(self.folder)
            out['kept'] = rollback.kept_images()
            out['exists'] = rollback.exists(self.folder)
        t = threading.Thread(target=read, daemon=True)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), 'reading the point blocks on a FIFO')
        self.assertEqual(out, {'point': None, 'kept': {}, 'exists': False})

    def test_kept_images_reads_only_the_small_record(self):
        self.update_to_v2()

        def boom(folder):
            raise AssertionError('kept_images must not read the point files')
        self.patch(rollback, 'load', boom)
        self.assertEqual(rollback.kept_images(), {'old1': ['web']})

    def test_a_deploy_goes_on_when_the_point_cannot_be_written(self):
        blocker = self.tmp / 'blocker'
        blocker.write_text('x', encoding='utf-8')
        self.patch(rollback, 'DATA', blocker)                               # a file where the data folder should be
        (self.folder / 'compose.yaml').write_bytes(V2.encode('utf-8'))
        said = []
        st.deploy(self.folder, 'compose.yaml', '.env', approve=lambda c: True, pull=False, log=said.append)
        self.assertTrue(any('no rollback point' in line for line in said), said)
        self.assertEqual((st.record_dir(self.folder) / 'compose.yaml').read_text(encoding='utf-8'), V2)

    def test_deleting_the_folder_of_a_stack_without_containers_drops_its_point(self):
        self.update_to_v2()
        self.mock.containers.clear()
        self.ui.stack_action('web', 'delete', {'confirm': 'web', 'deleteFolder': True})
        self.assertFalse(self.pdir.exists())

    def test_a_new_stack_of_an_old_name_does_not_inherit_a_point(self):
        self.update_to_v2()
        shutil.copytree(self.pdir, self.pdir.with_name('up'))               # what a folder removed by hand leaves
        self.ui.upload({'name': 'up', 'files': {'compose.yaml': compose_yaml('up')}})
        self.assertFalse(rollback.exists(self.stacks / 'up'))


class MinorFixTest(RollbackBase):
    def test_a_roll_back_without_containers_keeps_its_point(self):
        self.update_to_v2()
        taken = rollback.load_record(self.folder)['taken']
        self.mock.containers.clear()
        said = []
        rollback.roll_back(self.folder, 'compose.yaml', '.env', self.ui.write_file, said.append)
        self.assertEqual(rollback.load_record(self.folder)['taken'], taken)
        self.assertTrue(any('kept' in line for line in said), said)

    def test_names_already_moved_are_put_back_when_a_tag_fails(self):
        # two containers, two images under two names; the second tag fails
        self.mock.images.append({'Id': 'oldb', 'RepoTags': ['docker.io/library/redis:7'], 'RepoDigests': [], 'Size': 5, 'Created': 1})
        self.mock.add_container('cache', project='web', image='docker.io/library/redis:7', image_id='oldb')
        self.update_to_v2()
        self.mock.images[1]['RepoTags'] = None                               # oldb lost its name to a newer image
        self.mock.images.append({'Id': 'newb', 'RepoTags': ['docker.io/library/redis:7'], 'RepoDigests': [], 'Size': 6, 'Created': 2})
        real, calls = pa.tag_image, []

        def flaky(iid, repo, tag):
            calls.append(iid)
            if len(calls) == 2:
                raise pa.PodmanError('tag failed')
            return real(iid, repo, tag)
        self.patch(pa, 'tag_image', flaky)
        before = self.tags()
        with self.assertRaisesRegex(st.StackError, 'could not give the name'):
            rollback.roll_back(self.folder, 'compose.yaml', '.env', self.ui.write_file, lambda _: None)
        self.assertEqual(self.tags(), before)
        self.assertEqual((self.folder / 'compose.yaml').read_text(encoding='utf-8'), V2)     # no file was written

    def test_the_diff_does_not_list_files_a_roll_back_keeps(self):
        self.update_to_v2()
        (self.folder / 'later.txt').write_text('added after the point', encoding='utf-8')
        point = rollback.load(self.folder)
        self.assertNotIn('later.txt', rollback.diff(self.folder, 'compose.yaml', '.env', point)['files'])

    def test_a_file_that_fails_the_check_stops_the_roll_back_before_anything_changes(self):
        self.update_to_v2()
        before = self.tags()

        def check(fname, text):
            if fname == 'compose.yaml':
                raise ValueError('compose.yaml: not allowed')
        with self.assertRaisesRegex(st.StackError, 'compose.yaml: not allowed'):
            rollback.roll_back(self.folder, 'compose.yaml', '.env', self.ui.write_file, lambda _: None, check=check)
        self.assertEqual(self.tags(), before)
        self.assertEqual((self.folder / 'compose.yaml').read_text(encoding='utf-8'), V2)

    def test_a_digest_like_reference_is_no_name(self):
        self.assertIsNone(rollback.split_ref('sha256:' + 'a' * 64))
        self.assertIsNone(rollback.split_ref('sha256:abc'))


class CleanupTest(RollbackBase):
    """An update that writes a new point removes the images only the replaced point kept."""

    def second_update(self, **kw):
        """A further update: new1 loses its name to new2, the point moves on to new1."""
        (self.folder / 'compose.yaml').write_bytes((V2 + '# v3' + chr(10)).encode('utf-8'))
        self.deploy(**kw)
        return [i['Id'] for i in self.mock.images]

    def test_the_images_of_the_replaced_point_are_removed(self):
        self.update_to_v2()
        said = []
        st.deploy(self.folder, 'compose.yaml', '.env', approve=lambda c: True, pull=False, log=said.append)
        self.assertNotIn('old1', [i['Id'] for i in self.mock.images])
        self.assertTrue(any('removed the old image old1' in line for line in said), said)

    def test_not_when_switched_off(self):
        self.update_to_v2()
        schedule.configure(True, '04:00', datetime(2026, 11, 13, 3, 0), cleanup=False)
        self.assertIn('old1', self.second_update())

    def test_an_image_with_a_name_in_use_or_kept_elsewhere_stays(self):
        self.update_to_v2()
        self.mock.images[0]['RepoTags'] = ['localhost/keep:1']               # it has a name again
        self.assertIn('old1', self.second_update())

    def test_an_image_a_container_uses_stays(self):
        self.update_to_v2()
        self.mock.add_container('other', project='', image='x', image_id='old1')
        self.assertIn('old1', self.second_update())

    def test_an_image_another_stack_s_point_keeps_stays(self):
        self.update_to_v2()
        db = make_stack(self.stacks, 'db', compose_yaml('db'))
        rollback.write_point(db, {'taken': '2026-01-01T00:00:00Z', 'compose': 'compose.yaml', 'env': '.env',
                                  'files': {'compose.yaml': compose_yaml('db')},
                                  'images': [{'container': 'db', 'ref': REF, 'id': 'old1'}]})
        self.assertIn('old1', self.second_update())

    def test_a_roll_back_removes_nothing(self):
        self.update_to_v2()
        rollback.roll_back(self.folder, 'compose.yaml', '.env', self.ui.write_file, lambda _: None)
        self.assertEqual(sorted(i['Id'] for i in self.mock.images), ['new1', 'old1'])

    def test_a_removal_that_fails_is_logged_and_the_deploy_goes_on(self):
        self.update_to_v2()

        def refuse(iid, force=False):
            raise pa.PodmanError('image is in use')
        self.patch(pa, 'remove_image', refuse)
        said = []
        st.deploy(self.folder, 'compose.yaml', '.env', approve=lambda c: True, pull=False, log=said.append)
        self.assertTrue(any('was not removed' in line for line in said), said)

class ReviewFix2Test(RollbackBase):
    def point_with_notes(self):
        (self.folder / 'notes.txt').write_text('a', encoding='utf-8')
        self.deploy()
        self.update_to_v2()

    def test_a_target_that_is_no_plain_file_stops_the_roll_back_before_a_name_is_moved(self):
        self.point_with_notes()
        (self.folder / 'notes.txt').unlink()
        (self.folder / 'notes.txt').mkdir()                                  # (a link would do the same)
        before = self.tags()
        with self.assertRaisesRegex(st.StackError, 'notes.txt'):
            rollback.roll_back(self.folder, 'compose.yaml', '.env', self.ui.write_file, lambda _: None)
        self.assertEqual(self.tags(), before)
        self.assertEqual((self.folder / 'compose.yaml').read_text(encoding='utf-8'), V2)

    def test_a_write_that_fails_with_any_error_puts_the_names_back(self):
        self.update_to_v2()
        before = self.tags()

        def broken(path, text):
            raise ValueError(f'"{path.name}" is a link')
        with self.assertRaisesRegex(st.StackError, 'is a link'):
            rollback.roll_back(self.folder, 'compose.yaml', '.env', broken, lambda _: None)
        self.assertEqual(self.tags(), before)

    def test_a_folder_file_that_is_not_text_does_not_block_the_roll_back(self):
        self.update_to_v2()
        (self.folder / 'compose.yaml').write_bytes(bytes([0xff, 0xfe, 0x00, 0x80]))
        rollback.roll_back(self.folder, 'compose.yaml', '.env', self.ui.write_file, lambda _: None)
        self.assertEqual((self.folder / 'compose.yaml').read_text(encoding='utf-8'), V1)

    def test_a_point_is_kept_also_when_its_stack_is_not_found_now(self):
        self.update_to_v2()
        shutil.copytree(self.pdir, self.pdir.with_name('gone'))              # a point whose folder is no stack now
        (self.folder / 'compose.yaml').unlink()                              # and web is no stack either
        self.assertEqual(rollback.kept_images(), {'old1': ['gone', 'web']})

if __name__ == '__main__':
    import unittest
    unittest.main()
