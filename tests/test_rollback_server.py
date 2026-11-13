import http.client
import json
import os
import shutil
import threading
from http.server import ThreadingHTTPServer

from tests.support import compose_yaml, make_stack
from tests.test_rollback import REF, V1, V2, RollbackBase
from stack_ui import rollback, stacks as st


class ServerRollbackTest(RollbackBase):
    def setUp(self):
        super().setUp()
        self.ui.Config.password_hash = ''
        server = ThreadingHTTPServer(('127.0.0.1', 0), self.ui.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=15)
        self.addCleanup(conn.close)
        conn.request(method, path, body=json.dumps(body if body is not None else {}) if method == 'POST' else None,
                     headers={'X-UI-Token': self.ui.Config.local_token, 'Content-Type': 'application/json'})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b'{}')

    def compose_text(self):
        return (self.folder / 'compose.yaml').read_text(encoding='utf-8')

    def test_the_overview_says_which_stacks_have_a_point(self):
        def row():
            return next(r for r in self.ui.overview()['stacks'] if r['name'] == 'web')
        self.assertFalse(row()['rollback'])
        self.update_to_v2()
        self.assertTrue(row()['rollback'])

    def test_the_view_route(self):
        self.assertEqual(self.call('GET', '/api/stacks/web/rollback'), (200, {'available': False}))
        self.update_to_v2()
        status, body = self.call('GET', '/api/stacks/web/rollback')
        self.assertEqual(status, 200)
        self.assertTrue(body['available'])
        self.assertEqual(body['images'][0]['previous'], 'old1')
        self.assertEqual(self.call('GET', '/api/stacks/nope/rollback')[0], 404)

    def test_the_run_route_rolls_back(self):
        self.update_to_v2()
        status, body = self.call('POST', '/api/stacks/web/rollback')
        self.assertEqual((status, body['ok']), (200, True), body)
        self.assertEqual(self.compose_text(), V1)
        self.assertEqual(self.tags()['old1'], [REF])

    def test_without_a_point_the_run_is_a_failed_result(self):
        status, body = self.call('POST', '/api/stacks/web/rollback')
        self.assertEqual((status, body['ok']), (200, False))
        self.assertIn('no rollback point', ' '.join(body['log']))

    def test_the_protected_stack_is_refused(self):
        self.update_to_v2()
        self.patch(self.ui, 'PROTECTED', {'web'})
        r = self.ui.run_rollback('web')
        self.assertFalse(r['ok'])
        self.assertIn('protected', ' '.join(r['log']))
        self.assertEqual(self.compose_text(), V2)
        with self.assertRaises(ValueError):
            self.ui.discard_rollback('web')

    def test_a_busy_page_answers_409(self):
        self.update_to_v2()
        with self.ui.operation('deploy', 'other', cancellable=True):
            status, body = self.call('POST', '/api/stacks/web/rollback')
            self.assertEqual(status, 409)
            self.assertIn('Busy', body['error'])
            self.assertEqual(self.call('POST', '/api/stacks/web/rollback/discard')[0], 409)
        self.assertEqual(self.compose_text(), V2)
        self.assertTrue(rollback.exists(self.folder))

    def test_discard_removes_the_point(self):
        self.update_to_v2()
        self.assertEqual(self.call('POST', '/api/stacks/web/rollback/discard'), (200, {'discarded': True}))
        self.assertFalse(rollback.exists(self.folder))
        self.assertEqual(self.call('GET', '/api/stacks/web/rollback'), (200, {'available': False}))

    def test_the_operation_bar_can_name_the_two_changes(self):
        self.assertIn('rollback of', self.ui.Operation.TEXT['rollback'])
        with self.ui.operation('rollback', 'web') as op:
            self.assertIn('the rollback of web has been running', op.text())
        with self.ui.operation('rollback-discard', 'web') as op:
            self.assertIn('web', op.text())

    def test_a_failed_deploy_says_whether_there_is_something_to_roll_back_to(self):
        os.environ['FAKE_COMPOSE_FAIL_ON'] = 'up'
        first = self.ui.run_deploy('web', pull=False, remove=[])
        self.assertEqual((first['ok'], first['rollback']), (False, True))      # this update made a point
        shutil.rmtree(self.pdir)
        os.environ['FAKE_COMPOSE_FAIL_ON'] = 'pull'
        second = self.ui.run_deploy('web', pull=True, remove=[])
        self.assertEqual((second['ok'], second['rollback']), (False, False))   # the pull failed first: no point

    def test_the_images_a_point_needs_are_kept(self):
        self.update_to_v2()
        rows = {r['id']: r for r in self.ui.images()['images']}
        self.assertEqual(rows['old1']['keptFor'], ['web'])
        self.assertFalse(rows['old1']['removable'])
        self.assertEqual(rows['new1']['keptFor'], [])
        self.assertEqual(self.ui.images()['reclaimable'], 0)
        out = self.ui.images_remove(['old1'])
        self.assertEqual(out['removed'], [])
        self.assertIn('kept for the rollback of web', out['errors'][0])
        self.assertEqual(self.ui.prune({'ids': ['old1']})['removed'], [])
        self.assertIn('old1', self.tags())
        self.ui.discard_rollback('web')                                         # discarding frees it
        self.assertEqual(self.ui.images_remove(['old1'])['removed'], ['old1'])

    def test_an_image_two_points_need_stays_kept_until_both_are_discarded(self):
        make_stack(self.stacks, 'web2', compose_yaml('web2'))
        folder2 = self.stacks / 'web2'
        self.mock.add_container('web2', project='web2', image=REF, image_id='old1')
        st.save_deployed(folder2, 'compose.yaml', '.env')
        rollback.write_point(folder2, rollback.capture(folder2, 'compose.yaml', '.env'))
        self.update_to_v2()
        row = next(r for r in self.ui.images()['images'] if r['id'] == 'old1')
        self.assertEqual(row['keptFor'], ['web', 'web2'])
        self.ui.discard_rollback('web')
        row = next(r for r in self.ui.images()['images'] if r['id'] == 'old1')
        self.assertEqual(row['keptFor'], ['web2'])
        self.assertFalse(row['removable'])

    def test_the_point_is_not_the_applications_data(self):
        self.update_to_v2()
        self.assertFalse(self.ui.stack_folder('web')['hasFolders'])

    def test_removing_one_image_through_the_api_also_respects_the_rollback_point(self):
        self.update_to_v2()
        with self.assertRaisesRegex(ValueError, 'kept for the rollback of web'):
            self.ui.image_remove({'id': 'old1'})
        self.assertIn('old1', self.tags())
        status, body = self.call('POST', '/api/images/delete', {'id': 'old1'})
        self.assertEqual(status, 400)
        self.assertIn('old1', self.tags())
        self.ui.discard_rollback('web')
        self.assertEqual(self.ui.image_remove({'id': 'old1'})['removed'], 'old1')

    def test_a_point_file_that_fails_the_checks_of_a_save_stops_the_roll_back(self):
        self.update_to_v2()
        (self.pdir / 'files' / 'compose.yaml').write_text('not: [valid', encoding='utf-8')
        code, r = self.call('POST', '/api/stacks/web/rollback')
        self.assertEqual((code, r['ok']), (200, False), r)
        self.assertIn('compose.yaml', r['log'][-1])
        self.assertEqual(self.compose_text(), V2)
