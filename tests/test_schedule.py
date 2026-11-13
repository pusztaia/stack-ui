import json
import os
import unittest
from datetime import datetime

from tests.support import Base
from stack_ui import schedule
from tests.support import compose_yaml, make_stack


def at(text):
    return datetime.strptime(text, '%Y-%m-%d %H:%M')


class StateTest(Base):
    def file(self):
        return self.tmp / 'data' / 'schedule.json'

    def test_defaults_without_a_file(self):
        with schedule.LOCK:
            s = schedule.state()
        self.assertEqual((s['enabled'], s['time'], s['lastRun'], s['auto']), (True, '04:00', '', []))

    def test_a_damaged_or_oversized_file_is_the_defaults(self):
        self.file().parent.mkdir(parents=True)
        self.file().write_text('{not json', encoding='utf-8')
        with schedule.LOCK:
            self.assertEqual(schedule.state()['time'], '04:00')
        schedule.reset()
        self.file().write_text(json.dumps({'time': '05:00', 'pad': 'x' * schedule.MAX_STATE}), encoding='utf-8')
        with schedule.LOCK:
            self.assertEqual(schedule.state()['time'], '04:00')

    def test_wrong_parts_are_dropped_one_by_one(self):
        s = schedule.clean({'enabled': 'yes', 'time': '25:00', 'lastRun': 7, 'auto': ['web', '../x', 5, 'web'],
                            'failed': {'web': {'at': '2026-11-13', 'digests': {'r:1': ['sha256:a', 3]}}, '../x': {}},
                            'alerts': [{'stack': 'web', 'at': 't', 'result': 'rolled-back', 'reason': 'r'}, 'x'],
                            'runs': 'none'})
        self.assertEqual((s['enabled'], s['time'], s['lastRun'], s['auto']), (True, '04:00', '', ['web']))
        self.assertEqual(s['failed'], {'web': {'at': '2026-11-13', 'digests': {'r:1': ['sha256:a']}}})
        self.assertEqual(len(s['alerts']), 1)
        self.assertEqual(s['runs'], [])
        self.assertEqual(schedule.clean({'time': '23:59', 'enabled': False})['time'], '23:59')

    def test_runs_and_logs_are_capped(self):
        run = {'started': 'a', 'finished': 'b', 'trigger': 'schedule', 'checked': 1, 'updates': ['web'], 'error': '',
               'results': [{'stack': 'web', 'result': 'updated', 'reason': '', 'log': ['x' * 900] * 90}]}
        s = schedule.clean({'runs': [run] * 25})
        self.assertEqual(len(s['runs']), schedule.MAX_RUNS)
        log = s['runs'][0]['results'][0]['log']
        self.assertEqual((len(log), len(log[0])), (schedule.MAX_LOG, schedule.MAX_LINE))

    def test_the_file_is_written_private_and_read_back(self):
        schedule.configure(True, '05:30', at('2026-11-13 03:00'))
        schedule.set_auto('web', True)
        self.assertTrue(self.file().is_file())
        if os.name == 'posix':
            self.assertEqual(self.file().stat().st_mode & 0o777, 0o600)
        schedule.reset()
        self.assertEqual(schedule.view(at('2026-11-13 03:00'))['time'], '05:30')
        self.assertTrue(schedule.is_auto('web'))

    def test_a_folder_that_cannot_be_written_keeps_the_state_in_memory(self):
        blocker = self.tmp / 'blocker'
        blocker.write_text('x', encoding='utf-8')
        self.patch(schedule, 'DATA', blocker)
        schedule.reset()
        schedule.configure(True, '06:00', at('2026-11-13 03:00'))
        v = schedule.view(at('2026-11-13 03:00'))
        self.assertEqual(v['time'], '06:00')
        self.assertTrue(v['saveError'])


class DueTest(Base):
    def state(self, **kw):
        return schedule.clean(kw)

    def test_due_at_or_after_the_time_once_a_day(self):
        s = self.state(time='04:00')
        self.assertFalse(schedule.due(at('2026-11-13 03:59'), s))
        self.assertTrue(schedule.due(at('2026-11-13 04:00'), s))
        self.assertTrue(schedule.due(at('2026-11-13 17:20'), s))          # the server was down at 04:00
        s['lastRun'] = '2026-11-13'
        self.assertFalse(schedule.due(at('2026-11-13 17:20'), s))
        self.assertTrue(schedule.due(at('2026-11-14 04:00'), s))

    def test_not_due_when_switched_off(self):
        self.assertFalse(schedule.due(at('2026-11-13 05:00'), self.state(enabled=False)))

    def test_saving_a_time_that_has_passed_does_not_start_a_run_today(self):
        schedule.configure(True, '04:00', at('2026-11-13 10:00'))
        with schedule.LOCK:
            s = schedule.state()
        self.assertFalse(schedule.due(at('2026-11-13 10:01'), s))
        self.assertTrue(schedule.due(at('2026-11-14 04:00'), s))

    def test_saving_a_later_time_runs_today(self):
        schedule.configure(True, '23:00', at('2026-11-13 10:00'))
        with schedule.LOCK:
            self.assertTrue(schedule.due(at('2026-11-13 23:00'), schedule.state()))

    def test_configure_refuses_a_bad_time(self):
        for bad in ('4:00', '24:00', '04:60', '', None, 400):
            with self.assertRaises(ValueError):
                schedule.configure(True, bad, at('2026-11-13 10:00'))
        with self.assertRaises(ValueError):
            schedule.configure('on', '04:00', at('2026-11-13 10:00'))

    def test_view_names_the_next_run(self):
        schedule.configure(True, '04:00', at('2026-11-13 03:00'))
        self.assertEqual(schedule.view(at('2026-11-13 03:00'))['nextRun'], '2026-11-13T04:00')
        schedule.configure(True, '04:00', at('2026-11-13 10:00'))
        self.assertEqual(schedule.view(at('2026-11-13 10:00'))['nextRun'], '2026-11-14T04:00')
        schedule.configure(False, '04:00', at('2026-11-13 10:00'))
        self.assertEqual(schedule.view(at('2026-11-13 10:00'))['nextRun'], '')

    def test_a_run_that_is_due_is_next_now(self):
        self.assertEqual(schedule.view(at('2026-11-13 10:00'))['nextRun'], '2026-11-13T10:00')


class SwitchTest(Base):
    def test_the_switch_of_a_stack(self):
        schedule.set_auto('web', True)
        schedule.set_auto('db', True)
        schedule.set_auto('web', True)
        self.assertEqual(schedule.view(at('2026-11-13 03:00'))['auto'], ['db', 'web'])
        schedule.set_auto('web', False)
        self.assertFalse(schedule.is_auto('web'))
        with self.assertRaises(ValueError):
            schedule.set_auto('../x', True)

    def test_forget_drops_the_switch_and_the_failed_version(self):
        schedule.set_auto('web', True)
        with schedule.LOCK:
            schedule.state()['failed']['web'] = {'at': '2026-11-13', 'digests': {'r:1': ['sha256:a']}}
        self.assertIn('2026-11-13', schedule.note('web'))
        schedule.forget('web')
        self.assertFalse(schedule.is_auto('web'))
        self.assertEqual(schedule.note('web'), '')

    def test_alerts_are_listed_and_acknowledged(self):
        with schedule.LOCK:
            schedule.state()['alerts'].append({'stack': 'web', 'at': 't', 'result': 'rolled-back', 'reason': 'r'})
        self.assertEqual(len(schedule.alerts()), 1)
        schedule.acknowledge()
        self.assertEqual(schedule.alerts(), [])


class RunTest(Base):
    FOUND = {'web': {'update': True, 'digests': {'r:1': ['sha256:new']}},
             'db': {'update': False, 'digests': {}},
             'cache': {'update': True, 'digests': {'c:1': ['sha256:c']}}}

    def run_it(self, results, found=None, trigger='schedule', day='2026-11-13 04:00'):
        called = []

        def update(name):
            called.append(name)
            r = results[name]
            if isinstance(r, Exception):
                raise r
            return {'reason': '', 'log': ['line'], 'alert': False, **r}
        record = schedule.run(lambda: found or self.FOUND, update, trigger, at(day))
        return record, called

    def test_only_the_switched_stacks_with_an_update_are_updated(self):
        schedule.set_auto('web', True)
        schedule.set_auto('db', True)
        record, called = self.run_it({'web': {'result': 'updated'}})
        self.assertEqual(called, ['web'])
        self.assertEqual((record['checked'], record['updates']), (3, ['cache', 'web']))
        self.assertEqual(record['results'], [{'stack': 'web', 'result': 'updated', 'reason': '', 'log': ['line']}])
        v = schedule.view(at('2026-11-13 04:01'))
        self.assertEqual(v['lastRun'], '2026-11-13')
        self.assertEqual(v['runs'][0]['trigger'], 'schedule')
        self.assertEqual(v['alerts'], [])

    def test_a_failed_version_is_not_tried_again(self):
        schedule.set_auto('web', True)
        _, called = self.run_it({'web': {'result': 'rolled-back', 'reason': 'web is not running', 'alert': True}})
        self.assertEqual(called, ['web'])
        self.assertEqual(schedule.alerts()[0]['result'], 'rolled-back')
        self.assertIn('2026-11-13', schedule.note('web'))
        record, called = self.run_it({}, day='2026-11-14 04:00')            # the same digests the next day
        self.assertEqual(called, [])
        self.assertEqual(record['results'][0]['result'], 'skipped')
        self.assertIn('2026-11-13', record['results'][0]['reason'])
        newer = {'web': {'update': True, 'digests': {'r:1': ['sha256:newer']}}}
        _, called = self.run_it({'web': {'result': 'updated'}}, found=newer, day='2026-11-15 04:00')
        self.assertEqual(called, ['web'])
        self.assertEqual(schedule.note('web'), '')

    def test_a_skip_or_a_failed_pull_raises_no_alert_and_is_tried_again(self):
        schedule.set_auto('web', True)
        self.run_it({'web': {'result': 'failed', 'reason': 'pull failed'}})
        self.assertEqual(schedule.alerts(), [])
        self.assertEqual(schedule.note('web'), '')
        _, called = self.run_it({'web': {'result': 'skipped', 'reason': 'busy'}}, day='2026-11-14 04:00')
        self.assertEqual(called, ['web'])

    def test_an_update_that_raises_is_a_failed_result_with_an_alert(self):
        schedule.set_auto('web', True)
        schedule.set_auto('cache', True)
        record, called = self.run_it({'cache': RuntimeError('boom'), 'web': {'result': 'updated'}})
        self.assertEqual(called, ['cache', 'web'])                           # the others still run
        self.assertEqual(record['results'][0]['result'], 'failed')
        self.assertIn('boom', record['results'][0]['reason'])
        self.assertEqual([a['stack'] for a in schedule.alerts()], ['cache'])

    def test_a_check_that_raises_is_a_failed_run(self):
        def check():
            raise OSError('no podman')
        record = schedule.run(check, lambda n: None, 'schedule', at('2026-11-13 04:00'))
        self.assertIn('no podman', record['error'])
        self.assertEqual(schedule.view(at('2026-11-13 04:01'))['lastRun'], '2026-11-13')      # not repeated today

    def test_a_manual_run_does_not_count_as_the_day_s_run(self):
        self.run_it({}, trigger='manual', day='2026-11-13 02:00')
        v = schedule.view(at('2026-11-13 02:01'))
        self.assertEqual((v['lastRun'], v['runs'][0]['trigger']), ('', 'manual'))

    def test_only_one_run_at_a_time(self):
        import threading
        inside, leave = threading.Event(), threading.Event()

        def check():
            inside.set()
            leave.wait(10)
            return {}
        t = schedule.start(check, lambda n: None)
        self.assertTrue(inside.wait(10))
        self.assertTrue(schedule.view(at('2026-11-13 04:00'))['running'])
        with self.assertRaises(schedule.AlreadyRunning):
            schedule.run(lambda: {}, lambda n: None)
        with self.assertRaises(schedule.AlreadyRunning):
            schedule.start(lambda: {}, lambda n: None)
        leave.set()
        t.join(10)
        self.assertFalse(schedule.view(at('2026-11-13 04:00'))['running'])

    def test_tick_runs_when_due_and_only_then(self):
        calls = []
        self.assertFalse(schedule.tick(lambda: calls.append(1) or {}, lambda n: None, at('2026-11-13 03:00')))
        self.assertTrue(schedule.tick(lambda: calls.append(1) or {}, lambda n: None, at('2026-11-13 04:00')))
        self.assertFalse(schedule.tick(lambda: calls.append(1) or {}, lambda n: None, at('2026-11-13 04:01')))
        self.assertEqual(len(calls), 1)

    def test_no_thread_when_switched_off_for_the_tests(self):
        self.assertIsNone(schedule.start_thread(lambda: {}, lambda n: None))


class ApprovalTest(Base):
    """What a deploy from the page ran with, kept in the data folder: only that is updated automatically."""

    def setUp(self):
        super().setUp()
        self.folder = make_stack(self.stacks, 'web', compose_yaml('web'))

    def test_approved_until_a_file_changes_appears_or_goes(self):
        self.assertFalse(schedule.approved(self.folder, 'compose.yaml', '.env'))
        schedule.approve(self.folder, 'compose.yaml', '.env')
        self.assertTrue(schedule.approved(self.folder, 'compose.yaml', '.env'))
        schedule.reset()                                                     # it is in the file
        self.assertTrue(schedule.approved(self.folder, 'compose.yaml', '.env'))
        (self.folder / 'extra.json').write_text('x', encoding='utf-8')
        self.assertFalse(schedule.approved(self.folder, 'compose.yaml', '.env'))
        (self.folder / 'extra.json').unlink()
        self.assertTrue(schedule.approved(self.folder, 'compose.yaml', '.env'))
        (self.folder / '.env').write_text('TZ=UTC', encoding='utf-8')
        self.assertFalse(schedule.approved(self.folder, 'compose.yaml', '.env'))

    def test_a_link_or_an_oversized_file_is_never_approved(self):
        from stack_ui import stacks as st
        with open(self.folder / 'big.json', 'wb') as fh:
            fh.truncate(st.MAX_FILE + 1)
        schedule.approve(self.folder, 'compose.yaml', '.env')
        self.assertFalse(schedule.approved(self.folder, 'compose.yaml', '.env'))
        (self.folder / 'big.json').unlink()
        schedule.approve(self.folder, 'compose.yaml', '.env')
        target = self.tmp / 'elsewhere.yaml'
        target.write_bytes((self.folder / 'compose.yaml').read_bytes())
        (self.folder / 'compose.yaml').unlink()
        try:
            os.symlink(target, self.folder / 'compose.yaml')
        except (OSError, NotImplementedError):
            self.skipTest('no symlinks here')
        self.assertFalse(schedule.approved(self.folder, 'compose.yaml', '.env'))

    def test_forget_drops_the_approval_and_clean_keeps_only_hashes(self):
        schedule.approve(self.folder, 'compose.yaml', '.env')
        schedule.forget('web')
        self.assertFalse(schedule.approved(self.folder, 'compose.yaml', '.env'))
        s = schedule.clean({'deployed': {'web': {'compose.yaml': 'a' * 64, 'x': 'short', 3: 'b' * 64}, '../x': {}, 'db': 'no'}})
        self.assertEqual(s['deployed'], {'web': {'compose.yaml': 'a' * 64}})


class CleanupSettingTest(Base):
    def test_on_by_default_and_saved_only_when_given(self):
        self.assertTrue(schedule.clean({})['cleanup'])
        self.assertTrue(schedule.clean({'cleanup': 'no'})['cleanup'])
        self.assertTrue(schedule.cleanup_on())
        schedule.configure(True, '04:00', at('2026-11-13 03:00'), cleanup=False)
        self.assertFalse(schedule.cleanup_on())
        self.assertFalse(schedule.view(at('2026-11-13 03:00'))['cleanup'])
        schedule.configure(True, '05:00', at('2026-11-13 03:00'))            # not given: it stays
        self.assertFalse(schedule.cleanup_on())
        with self.assertRaises(ValueError):
            schedule.configure(True, '05:00', at('2026-11-13 03:00'), cleanup='x')

if __name__ == '__main__':
    unittest.main()
