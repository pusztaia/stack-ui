import os
import threading
import unittest

from tests.test_rollback import REF, V1, V2, RollbackBase
from stack_ui import rollback, schedule, stacks as st


class AutoUpdateBase(RollbackBase):
    def setUp(self):
        super().setUp()
        self.patch(self.ui, 'VERIFY_SETTLE', 0.0)
        self.patch(self.ui, 'VERIFY_WAIT', 0.3)
        self.patch(self.ui, 'VERIFY_STEP', 0.05)
        schedule.approve(self.folder, 'compose.yaml', '.env')       # as a deploy from the page leaves it

    def commands(self):
        return [c[-1] if c[-1] != '--force-recreate' else 'up' for c in self.compose_calls()]


class VerifyTest(AutoUpdateBase):
    def test_fine_when_what_ran_still_runs(self):
        self.assertEqual(self.ui.verify('web', {'web'}), '')

    def test_a_container_that_died_after_up_is_caught(self):
        self.container['State'] = 'exited'
        self.assertIn('web', self.ui.verify('web', {'web'}))

    def test_a_container_that_did_not_run_before_need_not_run_now(self):
        self.mock.add_container('init', project='web', state='exited')
        self.assertEqual(self.ui.verify('web', {'web'}), '')

    def test_unhealthy_is_caught(self):
        self.container['Status'] = 'unhealthy'
        self.assertIn('unhealthy', self.ui.verify('web', {'web'}))

    def test_starting_is_waited_for(self):
        self.container['Status'] = 'starting'
        self.patch(self.ui, 'VERIFY_WAIT', 5.0)
        threading.Timer(0.2, self.container.__setitem__, ('Status', 'unhealthy')).start()
        self.assertIn('unhealthy', self.ui.verify('web', {'web'}))

    def test_still_starting_at_the_end_of_the_wait_passes_when_it_runs(self):
        self.container['Status'] = 'starting'
        self.assertEqual(self.ui.verify('web', {'web'}), '')


class AutoUpdateTest(AutoUpdateBase):
    def test_an_update_pulls_recreates_and_keeps_a_point(self):
        r = self.ui.auto_update('web')
        self.assertEqual((r['result'], r['alert']), ('updated', False), r)
        self.assertEqual(self.commands(), ['pull', 'up'])
        self.assertTrue(rollback.exists(self.folder))
        self.assertIsNone(self.ui.current_operation())

    def test_a_failed_pull_changes_nothing(self):
        self.update_to_v2()                                                  # an older point exists
        taken = rollback.load_record(self.folder)['taken']
        os.environ['FAKE_COMPOSE_FAIL_ON'] = 'pull'
        r = self.ui.auto_update('web')
        self.assertEqual((r['result'], r['alert']), ('failed', False), r)
        self.assertEqual(self.commands(), ['pull'])                          # no up, no roll back
        self.assertEqual(rollback.load_record(self.folder)['taken'], taken)
        self.assertEqual((self.folder / 'compose.yaml').read_text(encoding='utf-8'), V2)

    def test_a_failed_up_is_rolled_back(self):
        real, seen = st.run_compose, []

        def flaky(folder, compose_file, env_file, args, log, **kw):
            seen.append(args[0])
            if args[0] == 'up' and seen.count('up') == 1:
                raise st.StackError('podman-compose up failed (exit code 3)')
            return real(folder, compose_file, env_file, args, log, **kw)
        self.patch(st, 'run_compose', flaky)
        r = self.ui.auto_update('web')
        self.assertEqual((r['result'], r['alert']), ('rolled-back', True), r)
        self.assertIn('up failed', r['reason'])
        self.assertEqual(seen, ['pull', 'up', 'up'])
        self.assertTrue(any('ERROR' in line for line in r['log']))

    def test_an_update_that_does_not_verify_is_rolled_back(self):
        answers = iter(['web is not running', ''])
        self.patch(self.ui, 'verify', lambda name, was: next(answers))
        r = self.ui.auto_update('web')
        self.assertEqual((r['result'], r['reason']), ('rolled-back', 'web is not running'), r)
        self.assertEqual(self.commands(), ['pull', 'up', 'up'])

    def test_a_roll_back_that_does_not_verify_is_reported(self):
        self.patch(self.ui, 'verify', lambda name, was: 'web is not running')
        r = self.ui.auto_update('web')
        self.assertEqual((r['result'], r['alert']), ('rollback-failed', True), r)
        self.assertIn('roll back failed too', r['reason'])

    def test_a_cancelled_update_is_not_rolled_back(self):
        def cancelled(name, was):
            self.ui.CURRENT.cancelled = True
            return 'cancelled'
        self.patch(self.ui, 'verify', cancelled)
        r = self.ui.auto_update('web')
        self.assertEqual((r['result'], r['alert']), ('cancelled', True), r)
        self.assertEqual(self.commands(), ['pull', 'up'])

    def test_skipped_while_another_change_runs(self):
        with self.ui.operation('deploy', 'other'):
            r = self.ui.auto_update('web')
        self.assertEqual((r['result'], r['alert']), ('skipped', False), r)
        self.assertIn('Busy', r['reason'])
        self.assertEqual(self.commands(), [])

    def test_skipped_when_the_files_differ_from_the_last_deploy(self):
        (self.folder / 'compose.yaml').write_bytes(V2.encode('utf-8'))
        r = self.ui.auto_update('web')
        self.assertEqual(r['result'], 'skipped', r)
        self.assertIn('differ', r['reason'])
        self.assertEqual(self.commands(), [])

    def test_skipped_without_a_record_of_the_last_deploy(self):
        import shutil
        shutil.rmtree(self.folder / st.DEPLOYED)
        self.assertEqual(self.ui.auto_update('web')['result'], 'skipped')
        self.assertEqual(self.commands(), [])

    def test_skipped_when_protected_not_deployed_or_gone(self):
        self.patch(self.ui, 'PROTECTED', {'web'})
        r = self.ui.auto_update('web')
        self.assertEqual(r['result'], 'skipped', r)
        self.assertIn('protected', r['reason'])
        self.patch(self.ui, 'PROTECTED', set())
        self.mock.containers.clear()
        self.assertIn('not deployed', self.ui.auto_update('web')['reason'])
        self.assertIn('no folder', self.ui.auto_update('nosuch')['reason'])
        self.assertEqual(self.commands(), [])

    def test_skipped_when_the_plan_needs_a_decision(self):
        self.mock.add_container('web2', project='')                          # a foreign container of that name
        text = V1.replace('container_name: web', 'container_name: web2')
        (self.folder / 'compose.yaml').write_bytes(text.encode('utf-8'))
        st.save_deployed(self.folder, 'compose.yaml', '.env')
        schedule.approve(self.folder, 'compose.yaml', '.env')
        r = self.ui.auto_update('web')
        self.assertEqual(r['result'], 'skipped', r)
        self.assertEqual(self.commands(), [])

    def test_podman_not_answering_is_a_failed_result(self):
        self.mock.stop()
        r = self.ui.auto_update('web')
        self.assertEqual((r['result'], r['alert']), ('failed', False), r)


class ScheduledCheckTest(AutoUpdateBase):
    def test_the_check_names_the_stacks_with_an_update_and_their_digests(self):
        fake = {'checkedAt': 't', 'stacks': {
            'web': {'status': 'update', 'images': [{'container': 'web', 'image': REF, 'status': 'update', 'message': ''},
                                                   {'container': 'side', 'image': 'x:1', 'status': 'current', 'message': ''}]},
            'db': {'status': 'current', 'images': []}}}
        self.patch(self.ui, 'updates', lambda check: fake if check else None)
        self.patch(self.ui, 'REMOTE', {REF: (('sha256:idx', 'sha256:arm'), ''), 'x:1': (('sha256:x',), '')})
        self.assertEqual(self.ui.scheduled_check(), {
            'web': {'update': True, 'digests': {REF: ['sha256:idx', 'sha256:arm']}},
            'db': {'update': False, 'digests': {}}})


class ReviewFixTest(AutoUpdateBase):
    def test_files_and_a_record_written_from_inside_the_stack_folder_are_not_deployed(self):
        (self.folder / 'compose.yaml').write_bytes(V2.encode('utf-8'))
        (self.folder / st.DEPLOYED / 'compose.yaml').write_bytes(V2.encode('utf-8'))    # drift shows nothing
        r = self.ui.auto_update('web')
        self.assertEqual(r['result'], 'skipped', r)
        self.assertEqual(self.commands(), [])

    def test_a_stack_never_deployed_from_this_version_is_not_updated(self):
        schedule.forget('web')
        r = self.ui.auto_update('web')
        self.assertEqual(r['result'], 'skipped', r)
        self.assertIn('on the page', r['reason'])
        self.assertEqual(self.commands(), [])

    def test_a_deploy_from_the_page_approves_the_files_and_an_automatic_one_does_not(self):
        (self.folder / 'compose.yaml').write_bytes(V2.encode('utf-8'))
        self.assertFalse(schedule.approved(self.folder, 'compose.yaml', '.env'))
        self.deploy(trusted=False)
        self.assertFalse(schedule.approved(self.folder, 'compose.yaml', '.env'))
        self.deploy()
        self.assertTrue(schedule.approved(self.folder, 'compose.yaml', '.env'))

    def test_files_changed_during_the_pull_are_not_started(self):
        real, seen = st.run_compose, []

        def tamper(folder, compose_file, env_file, args, log, **kw):
            seen.append(args[0])
            if args[0] == 'pull':
                (self.folder / 'compose.yaml').write_bytes(V2.encode('utf-8'))
            return real(folder, compose_file, env_file, args, log, **kw)
        self.patch(st, 'run_compose', tamper)
        r = self.ui.auto_update('web')
        self.assertEqual((r['result'], r['alert']), ('failed', True), r)
        self.assertIn('changed during', r['reason'])
        self.assertEqual(seen, ['pull'])

    def test_a_failed_pull_does_not_roll_back_to_an_older_pending_point(self):
        point = rollback.capture(self.folder, 'compose.yaml', '.env')
        rollback.write_point(self.folder, {**point, 'pending': True})        # an earlier update never finished
        os.environ['FAKE_COMPOSE_FAIL_ON'] = 'pull'
        r = self.ui.auto_update('web')
        self.assertEqual((r['result'], r['alert']), ('failed', False), r)
        self.assertEqual(self.commands(), ['pull'])

    def test_a_stopped_stack_is_left_stopped(self):
        self.container['State'] = 'exited'
        r = self.ui.auto_update('web')
        self.assertEqual(r['result'], 'skipped', r)
        self.assertIn('stopped', r['reason'])
        self.assertEqual(self.commands(), [])

    def test_a_failed_up_without_a_point_is_reported(self):
        blocker = self.tmp / 'blocker'
        blocker.write_text('x', encoding='utf-8')
        self.patch(rollback, 'DATA', blocker)                                # no point can be written
        os.environ['FAKE_COMPOSE_FAIL_ON'] = 'up'
        r = self.ui.auto_update('web')
        self.assertEqual((r['result'], r['alert']), ('failed', True), r)

    def test_a_roll_back_that_cannot_write_is_reported(self):
        self.patch(self.ui, 'verify', lambda name, was: 'web is not running')

        def broken(*args, **kw):
            raise OSError('disk full')
        self.patch(rollback, 'roll_back', broken)
        r = self.ui.auto_update('web')
        self.assertEqual((r['result'], r['alert']), ('rollback-failed', True), r)
        self.assertIn('disk full', r['reason'])


if __name__ == '__main__':
    unittest.main()
