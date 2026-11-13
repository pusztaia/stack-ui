import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

from tests.support import compose_yaml, make_stack
from tests.test_rollback import RollbackBase
from stack_ui import schedule


class ScheduleServerTest(RollbackBase):
    def setUp(self):
        super().setUp()
        self.ui.Config.password_hash = ''
        self.patch(self.ui, 'SETTINGS_FILE', self.tmp / 'settings.json')
        server = ThreadingHTTPServer(('127.0.0.1', 0), self.ui.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=15)
        self.addCleanup(conn.close)
        conn.request(method, path, body=json.dumps(body if body is not None else {}) if method == 'POST' else None,
                     headers=headers or {'X-UI-Token': self.ui.Config.local_token, 'Content-Type': 'application/json'})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b'{}')

    def row(self):
        return next(r for r in self.ui.overview()['stacks'] if r['name'] == 'web')

    def test_the_setting_is_read_and_saved(self):
        code, v = self.call('GET', '/api/schedule')
        self.assertEqual((code, v['enabled'], v['time'], v['auto'], v['running']), (200, True, '04:00', [], False))
        self.assertRegex(v['serverTime'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d$')
        code, v = self.call('POST', '/api/schedule', {'enabled': False, 'time': '05:15'})
        self.assertEqual((code, v['enabled'], v['time'], v['nextRun']), (200, False, '05:15', ''))
        self.assertEqual(self.call('POST', '/api/schedule', {'enabled': True, 'time': '5'})[0], 400)
        self.assertEqual(self.call('POST', '/api/schedule', {'time': '05:15'})[0], 400)

    def test_the_switch_of_a_stack(self):
        self.assertEqual((self.row()['auto'], self.row()['autoNote']), (False, ''))
        self.assertEqual(self.call('POST', '/api/stacks/web/auto', {'auto': True}), (200, {'auto': True}))
        self.assertTrue(self.row()['auto'])
        self.assertEqual(self.call('POST', '/api/stacks/web/auto', {'auto': 'yes'})[0], 400)
        self.assertEqual(self.call('POST', '/api/stacks/nosuch/auto', {'auto': True})[0], 404)
        self.assertEqual(self.call('POST', '/api/stacks/web/auto', {'auto': False}), (200, {'auto': False}))

    def test_a_protected_stack_cannot_be_switched_on(self):
        self.patch(self.ui, 'PROTECTED', {'web'})
        code, r = self.call('POST', '/api/stacks/web/auto', {'auto': True})
        self.assertEqual(code, 400)
        self.assertIn('protected', r['error'])
        self.assertEqual(self.call('POST', '/api/stacks/web/auto', {'auto': False})[0], 200)

    def test_run_now_runs_in_the_background_and_refuses_a_second_one(self):
        inside, leave = threading.Event(), threading.Event()

        def check():
            inside.set()
            leave.wait(10)
            return {}
        self.patch(self.ui, 'scheduled_check', check)
        self.assertEqual(self.call('POST', '/api/schedule/run'), (200, {'started': True}))
        self.assertTrue(inside.wait(10))
        self.assertTrue(self.call('GET', '/api/operation')[1]['scheduleRunning'])
        self.assertEqual(self.call('POST', '/api/schedule/run')[0], 409)
        leave.set()
        for _ in range(100):
            if not self.call('GET', '/api/schedule')[1]['running']:
                break
            threading.Event().wait(0.05)
        v = self.call('GET', '/api/schedule')[1]
        self.assertEqual((v['running'], v['runs'][0]['trigger'], v['lastRun']), (False, 'manual', ''))

    def test_alerts_ride_on_the_poll_and_are_acknowledged(self):
        with schedule.LOCK:
            schedule.state()['alerts'].append({'stack': 'web', 'at': '2026-11-13T04:03:10Z', 'result': 'rolled-back',
                                               'reason': 'web is not running'})
        r = self.call('GET', '/api/operation')[1]
        self.assertEqual((r['operation'], r['alerts'][0]['stack'], r['scheduleRunning']), (None, 'web', False))
        self.assertEqual(self.call('POST', '/api/schedule/ack'), (200, {'alerts': []}))
        self.assertEqual(self.call('GET', '/api/operation')[1]['alerts'], [])

    def test_the_note_of_a_failed_version_is_in_the_overview(self):
        schedule.set_auto('web', True)
        with schedule.LOCK:
            schedule.state()['failed']['web'] = {'at': '2026-11-13', 'digests': {'r:1': ['sha256:a']}}
        self.assertIn('2026-11-13', self.row()['autoNote'])

    def test_deleting_the_folder_and_uploading_drop_the_switch(self):
        schedule.set_auto('web', True)
        self.mock.containers.clear()
        self.ui.stack_action('web', 'delete', {'confirm': 'web', 'deleteFolder': True})
        self.assertFalse(schedule.is_auto('web'))
        schedule.set_auto('up', True)                                        # left by a folder removed by hand
        self.ui.upload({'name': 'up', 'files': {'compose.yaml': compose_yaml('up')}})
        self.assertFalse(schedule.is_auto('up'))

    def test_the_api_token_cannot_use_the_schedule(self):
        token = self.ui.api_token({})['token']
        bearer = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        for method, path in (('GET', '/api/schedule'), ('POST', '/api/schedule'), ('POST', '/api/schedule/run'),
                             ('POST', '/api/schedule/ack'), ('POST', '/api/stacks/web/auto'), ('POST', '/api/stacks/auto'),
                             ('POST', '/api/stacks/web/auto/'), ('GET', '/api/schedule/'), ('POST', '/api/schedule/run/')):
            self.assertEqual(self.call(method, path, {'auto': True}, bearer)[0], 403, path)
        self.assertEqual(self.call('GET', '/api/stacks', None, bearer)[0], 200)
        self.assertFalse(schedule.is_auto('web'))


    def test_the_switch_of_several_stacks_at_once(self):
        make_stack(self.stacks, 'db', compose_yaml('db'))
        self.patch(self.ui, 'PROTECTED', {'db'})
        code, r = self.call('POST', '/api/stacks/auto', {'names': ['web', 'db', 'nosuch'], 'auto': True})
        self.assertEqual((code, r['changed']), (200, ['web']))
        self.assertEqual([s['name'] for s in r['skipped']], ['db', 'nosuch'])
        self.assertTrue(schedule.is_auto('web'))
        code, r = self.call('POST', '/api/stacks/auto', {'names': ['web', 'db'], 'auto': False})
        self.assertEqual((r['changed'], r['skipped']), (['web', 'db'], []))
        self.assertEqual(self.call('POST', '/api/stacks/auto', {'names': 'web', 'auto': True})[0], 400)
        self.assertEqual(self.call('POST', '/api/stacks/auto', {'names': ['web'], 'auto': 1})[0], 400)

    def test_the_clean_up_switch_is_saved(self):
        self.assertTrue(self.call('GET', '/api/schedule')[1]['cleanup'])
        code, v = self.call('POST', '/api/schedule', {'enabled': True, 'time': '04:00', 'cleanup': False})
        self.assertEqual((code, v['cleanup']), (200, False))
        self.assertEqual(self.call('POST', '/api/schedule', {'enabled': True, 'time': '04:00', 'cleanup': 'x'})[0], 400)

if __name__ == '__main__':
    unittest.main()
