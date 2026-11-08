import http.client
import importlib
import json
import os
import threading
import time
from http.server import ThreadingHTTPServer

from tests.support import Base, compose_yaml, make_stack
from stack_ui import server as srv
from stack_ui import stacks as st


class OperationTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = importlib.reload(srv)

    def test_a_second_change_is_refused_at_once_with_the_description(self):
        with self.ui.operation('deploy', 'web', cancellable=True):
            with self.assertRaises(self.ui.Conflict) as ctx:
                with self.ui.operation('stack-stop', 'web', cancellable=True):
                    self.fail('must not run')
        message = str(ctx.exception)
        self.assertTrue(message.startswith('Busy: the deploy of web has been running for '), message)
        self.assertTrue(message.endswith(', or cancel it on the page.'), message)
        self.assertEqual((ctx.exception.operation['kind'], ctx.exception.operation['target']), ('deploy', 'web'))
        self.assertTrue(ctx.exception.operation['cancellable'])

    def test_a_quick_change_says_it_cannot_be_cancelled(self):
        with self.ui.operation('secret', 'secret db'):
            with self.assertRaises(self.ui.Conflict) as ctx:
                with self.ui.operation('network', 'network n'):
                    pass
        message = str(ctx.exception)
        self.assertIn('the change of secret db has been running for', message)
        self.assertTrue(message.endswith('Wait for it to finish.'), message)
        self.assertNotIn('cancel', message)

    def test_the_lock_is_free_again_after_a_return_an_exception_and_each_change(self):
        with self.ui.operation('deploy', 'web'):
            pass
        self.assertIsNone(self.ui.CURRENT)
        with self.assertRaises(ValueError):
            with self.ui.operation('deploy', 'web'):
                raise ValueError('boom')
        self.assertIsNone(self.ui.CURRENT)
        with self.ui.operation('stack-start', 'web') as op:
            self.assertIs(self.ui.CURRENT, op)

    def test_the_handle_for_run_compose_is_per_thread(self):
        seen = {}

        def other():
            seen['other'] = getattr(st.RUNNING, 'op', None)
        with self.ui.operation('deploy', 'web') as op:
            self.assertIs(st.RUNNING.op, op)
            t = threading.Thread(target=other)
            t.start()
            t.join()
        self.assertIsNone(seen['other'])
        self.assertIsNone(st.RUNNING.op)

    def test_the_description_of_every_kind_reads_naturally(self):
        cases = {'deploy': ('web', 'the deploy of web'), 'stack-start': ('web', 'starting web'),
                 'stack-stop': ('web', 'stopping web'), 'stack-delete': ('web', 'deleting web'),
                 'restart': ('db of web', 'restarting db of web'),
                 'container': ('web stop', 'the container action "web stop"'),
                 'image-pull': ('docker.io/library/nginx:1', 'pulling docker.io/library/nginx:1'),
                 'image-tag': ('abc', 'tagging image abc'), 'image-remove': ('abc', 'removing image abc'),
                 'images-remove': ('3 images', 'removing 3 images'), 'images-prune': ('2 images', 'removing 2 images'),
                 'network': ('network n', 'the change of network n'), 'volume': ('volume v', 'the change of volume v'),
                 'secret': ('secret s', 'the change of secret s')}
        for kind, (target, expected) in cases.items():
            self.assertTrue(self.ui.Operation(kind, target).text().startswith(expected + ' has been running for '), kind)

    def test_every_change_site_is_refused_while_one_runs(self):
        make_stack(self.stacks, 'web', compose_yaml('web'))
        self.mock.add_container('web', project='web')
        self.mock.images.append({'Id': 'free1', 'RepoTags': ['localhost/a:1'], 'RepoDigests': [], 'Size': 1, 'Created': 1})
        calls = [
            lambda: self.ui.secret_save({'name': 's', 'value': 'v'}),
            lambda: self.ui.secret_delete({'name': 's'}),
            lambda: self.ui.network_create({'name': 'n'}),
            lambda: self.ui.network_delete({'name': 'n'}),
            lambda: self.ui.volume_create({'name': 'v'}),
            lambda: self.ui.volume_delete({'names': ['v']}),
            lambda: self.ui.image_pull({'reference': 'docker.io/library/nginx:1'}),
            lambda: self.ui.image_tag({'id': 'free1', 'repo': 'localhost/a', 'tag': 'x'}),
            lambda: self.ui.image_remove({'id': 'free1'}),
            lambda: self.ui.images_remove(['free1']),
            lambda: self.ui.prune({'ids': ['free1']}),
            lambda: self.ui.container_do({'name': 'web', 'action': 'restart'}),
            lambda: self.ui.restart('web', {'container': 'web'}),
            lambda: self.ui.run_deploy('web', pull=False, remove=[]),
            lambda: self.ui.stack_action('web', 'stop', {}),
        ]
        with self.ui.operation('deploy', 'other', cancellable=True):
            for i, call in enumerate(calls):
                with self.assertRaises(self.ui.Conflict, msg=f'site {i}'):
                    call()
        self.assertEqual(self.compose_calls(), [])            # nothing ran behind the refusal

    def test_the_change_sites_still_work_when_nothing_runs(self):
        make_stack(self.stacks, 'web', compose_yaml('web'))
        self.assertTrue(self.ui.run_deploy('web', pull=False, remove=[])['ok'])
        self.assertEqual(self.ui.secret_save({'name': 's1', 'value': 'v'})['name'], 's1')
        self.assertIsNone(self.ui.CURRENT)


class OperationRoutesTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = importlib.reload(srv)
        self.ui.Config.password_hash = ''
        make_stack(self.stacks, 'web', compose_yaml('web'))
        self.pids = self.tmp / 'pids'
        os.environ['FAKE_COMPOSE_PIDS'] = str(self.pids)
        for name in ('FAKE_COMPOSE_PIDS', 'FAKE_COMPOSE_HANG', 'FAKE_COMPOSE_IGNORE_TERM'):
            self.addCleanup(os.environ.pop, name, None)
        server = ThreadingHTTPServer(('127.0.0.1', 0), self.ui.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=15)
        self.addCleanup(conn.close)
        conn.request(method, path, body=json.dumps(body if body is not None else {}) if method == 'POST' else None,
                     headers={'X-UI-Token': self.ui.Config.local_token, 'Content-Type': 'application/json',
                              **(headers or {})})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b'{}')

    def start_deploy(self, hang='up:30'):
        """A deploy of web whose `up` hangs, running in a thread; waits until it is the current operation."""
        os.environ['FAKE_COMPOSE_HANG'] = hang
        result = {}

        def worker():
            result['answer'] = self.ui.run_deploy('web', pull=False, remove=[])
        t = threading.Thread(target=worker)
        t.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            op = self.ui.CURRENT
            if op is not None and op.proc is not None:
                break
            time.sleep(0.02)
        self.assertIsNotNone(self.ui.CURRENT, 'the deploy never started')
        self.assertIsNotNone(self.ui.CURRENT.proc, 'its command never started')
        deadline = time.time() + 5                            # and the fake command really hangs (it wrote its pids)
        while time.time() < deadline and not (self.pids.exists() and self.pids.read_text().strip()):
            time.sleep(0.02)
        return t, result

    def alive(self, pid):
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    def test_nothing_runs_nothing_to_cancel(self):
        self.assertEqual(self.call('GET', '/api/operation'), (200, {'operation': None}))
        status, body = self.call('POST', '/api/operation/cancel')
        self.assertEqual((status, body['error']), (409, 'nothing is running'))

    def test_a_hanging_deploy_is_visible_blocks_other_changes_and_can_be_cancelled(self):
        t, result = self.start_deploy()
        status, body = self.call('GET', '/api/operation')
        op = body['operation']
        self.assertEqual((status, op['kind'], op['target'], op['cancellable']), (200, 'deploy', 'web', True))
        self.assertIn('the deploy of web has been running for', op['text'])
        status, body = self.call('POST', '/api/secrets', {'name': 's', 'value': 'v'})
        self.assertEqual(status, 409)
        self.assertTrue(body['error'].startswith('Busy: the deploy of web'), body)
        self.assertEqual(body['operation']['kind'], 'deploy')
        status, body = self.call('POST', '/api/stacks/web/stop')
        self.assertEqual(status, 409)
        started = time.time()
        status, body = self.call('POST', '/api/operation/cancel')
        self.assertEqual(status, 200)
        self.assertEqual(body['cancelled']['kind'], 'deploy')
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertLess(time.time() - started, 10)
        self.assertFalse(result['answer']['ok'])
        self.assertTrue(any('cancelled' in line for line in result['answer']['log']), result)
        self.assertEqual(self.call('GET', '/api/operation'), (200, {'operation': None}))
        if hasattr(os, 'killpg'):                              # the command and its child are both gone
            pids = [int(x) for line in self.pids.read_text().splitlines() for x in line.split()]
            deadline = time.time() + 5
            while time.time() < deadline and any(self.alive(p) for p in pids):
                time.sleep(0.05)
            self.assertEqual([p for p in pids if self.alive(p)], [])
        os.environ.pop('FAKE_COMPOSE_HANG')                    # and the stack can be stopped now
        self.mock.add_container('web', project='web')
        status, body = self.call('POST', '/api/stacks/web/stop')
        self.assertEqual((status, body['ok']), (200, True))

    def test_cancelling_a_stop_gives_a_502_with_the_reason_and_frees_the_lock(self):
        self.mock.add_container('web', project='web')
        os.environ['FAKE_COMPOSE_HANG'] = 'stop:30'
        answer = {}

        def worker():
            answer['status'], answer['body'] = self.call('POST', '/api/stacks/web/stop')
        t = threading.Thread(target=worker)
        t.start()
        deadline = time.time() + 10
        while time.time() < deadline and not (self.ui.CURRENT and self.ui.CURRENT.proc):
            time.sleep(0.02)
        self.assertEqual(self.call('POST', '/api/operation/cancel')[0], 200)
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertEqual(answer['status'], 502)
        self.assertIn('cancelled', answer['body']['error'])
        self.assertIsNone(self.ui.CURRENT)

    def test_a_quick_change_cannot_be_cancelled(self):
        with self.ui.operation('secret', 'secret s'):
            status, body = self.call('POST', '/api/operation/cancel')
            self.assertEqual(status, 409)
            self.assertIn('is a quick change and cannot be cancelled', body['error'])
            self.assertEqual(body['operation']['kind'], 'secret')
            self.assertFalse(self.ui.CURRENT.cancelled)         # nothing was touched
        self.assertIsNone(self.ui.CURRENT)

    def test_a_cancel_names_the_change_it_means_and_a_different_one_is_left_alone(self):
        with self.ui.operation('deploy', 'web', cancellable=True) as op:
            seen = self.ui.current_operation()
            for wrong in ({'since': seen['since'] - 100, 'kind': 'deploy', 'target': 'web'},
                          {'since': seen['since'], 'kind': 'deploy', 'target': 'other'},
                          {'since': seen['since'], 'kind': 'stack-stop', 'target': 'web'}):
                status, body = self.call('POST', '/api/operation/cancel', wrong)
                self.assertEqual(status, 409, wrong)
                self.assertIn('already finished', body['error'])
                self.assertEqual(body['operation']['target'], 'web')
                self.assertFalse(op.cancelled)
            status, body = self.call('POST', '/api/operation/cancel',
                                     {'since': seen['since'], 'kind': 'deploy', 'target': 'web'})
            self.assertEqual(status, 200)
            self.assertTrue(op.cancelled)

    def test_a_late_cancel_after_the_deploy_ended_is_a_409(self):
        self.assertTrue(self.ui.run_deploy('web', pull=False, remove=[])['ok'])
        status, body = self.call('POST', '/api/operation/cancel')
        self.assertEqual((status, body['error']), (409, 'nothing is running'))

    def test_the_api_token_can_see_and_cancel_too(self):
        self.patch(self.ui, 'SETTINGS_FILE', self.tmp / 'settings.json')
        self.ui.Config.password_hash = self.ui.hash_password('right-password', 1000)
        token = self.ui.api_token({})['token']
        auth = {'Authorization': f'Bearer {token}', 'X-UI-Token': ''}
        with self.ui.operation('deploy', 'web', cancellable=True) as op:
            status, body = self.call('GET', '/api/operation', headers=auth)
            self.assertEqual((status, body['operation']['kind']), (200, 'deploy'))
            status, body = self.call('POST', '/api/operation/cancel', headers=auth)
            self.assertEqual(status, 200)
            self.assertTrue(op.cancelled)
        status, _ = self.call('GET', '/api/operation', headers={'X-UI-Token': ''})
        self.assertEqual(status, 401)                          # without sign-in nothing is shown
