"""The Activity log: every change that holds the change lock is written down when it ends (what, on what, the result,
how long, from where), and a watcher writes down what happens to the containers while nobody looks: a restart loop
starting and ending, an out-of-memory kill, memory near the limit. Kept in STACK_UI_DATA/activity.jsonl, the newest
entries only."""
import http.client
import json
import threading
import time
from http.server import ThreadingHTTPServer

from tests.support import Base, compose_yaml, fresh_server, make_stack
from stack_ui import lifecycle


class ActivityBase(Base):
    def setUp(self):
        super().setUp()
        self.ui = fresh_server()
        from stack_ui import activity
        self.act = activity

    def entries(self, **q):
        return self.act.listing(q)['entries']


class RecordTest(ActivityBase):
    def test_a_change_is_written_when_it_ends(self):
        with self.ui.core.operation('image-pull', 'docker.io/library/nginx:latest'):
            pass
        [e] = self.entries()
        self.assertEqual({k: e[k] for k in ('type', 'kind', 'target', 'result', 'source')},
                         {'type': 'change', 'kind': 'image-pull', 'target': 'docker.io/library/nginx:latest',
                          'result': 'ok', 'source': 'schedule'})
        self.assertIsInstance(e['seconds'], (int, float))
        self.assertTrue(e['at'].endswith('Z'))

    def test_a_failure_is_written_with_its_message_and_still_raised(self):
        with self.assertRaises(ValueError):
            with self.ui.core.operation('network', 'network lan'):
                raise ValueError('no such subnet')
        [e] = self.entries()
        self.assertEqual((e['result'], e['message']), ('failed', 'no such subnet'))

    def test_what_did_not_get_the_lock_is_not_written(self):
        with self.ui.core.operation('deploy', 'web'):
            with self.assertRaises(self.ui.core.Conflict):
                with self.ui.core.operation('deploy', 'other'):
                    pass
        self.assertEqual([e['target'] for e in self.entries()], ['web'])

    def test_a_noted_result_and_a_cancel(self):
        with self.ui.core.operation('deploy', 'web') as op:
            op.note('failed', 'podman-compose up failed')
        with self.ui.core.operation('stack-stop', 'web', cancellable=True) as op:
            op.cancelled = True
        self.assertEqual([(e['kind'], e['result'], e['message']) for e in self.entries()],
                         [('stack-stop', 'cancelled', ''), ('deploy', 'failed', 'podman-compose up failed')])

    def test_the_stack_of_an_entry(self):
        for kind, target in (('deploy', 'web'), ('restart', 'app of web'), ('auto-update', 'web'),
                             ('image-pull', 'nginx'), ('rollback', 'web')):
            with self.ui.core.operation(kind, target):
                pass
        self.assertEqual([e['stack'] for e in reversed(self.entries())], ['web', 'web', 'web', '', 'web'])

    def test_a_deploy_that_fails_is_written_as_failed(self):
        make_stack(self.stacks, 'web', compose_yaml('web'))
        import os
        os.environ['FAKE_COMPOSE_FAIL_ON'] = 'up'
        r = self.ui.run_deploy('web', pull=False, remove=[])
        self.assertFalse(r['ok'])
        [e] = self.entries()
        self.assertEqual((e['kind'], e['result']), ('deploy', 'failed'))
        self.assertIn('exit code 3', e['message'])

    def test_only_the_newest_are_kept(self):
        self.patch(self.act, 'KEEP', 5)
        self.patch(self.act, 'TRIM_BYTES', 0)               # trim at every write
        for i in range(12):
            self.act.record({'type': 'change', 'kind': 'image-pull', 'target': f'n{i}', 'result': 'ok'})
        self.assertEqual([e['target'] for e in self.entries()], ['n11', 'n10', 'n9', 'n8', 'n7'])

    def test_a_broken_line_or_an_unwritable_folder_breaks_nothing(self):
        self.act.record({'type': 'change', 'kind': 'image-pull', 'target': 'a', 'result': 'ok'})
        with open(self.act.path(), 'ab') as f:
            f.write(b'{not json\n[1,2]\n')
        self.act.record({'type': 'change', 'kind': 'image-pull', 'target': 'b', 'result': 'ok'})
        self.assertEqual([e['target'] for e in self.entries()], ['b', 'a'])
        self.patch(self.act.st, 'DATA', self.tmp / 'a-file')
        (self.tmp / 'a-file').write_bytes(b'x')
        with self.ui.core.operation('image-pull', 'c'):         # the change itself is not disturbed
            pass

    def test_filters(self):
        rows = [{'type': 'change', 'kind': 'deploy', 'target': 'web', 'stack': 'web', 'result': 'ok'},
                {'type': 'change', 'kind': 'deploy', 'target': 'db', 'stack': 'db', 'result': 'failed'},
                {'type': 'warning', 'kind': 'restart-loop', 'target': 'web-app', 'stack': 'web', 'result': 'started'},
                {'type': 'change', 'kind': 'auto-update', 'target': 'db', 'stack': 'db', 'result': 'rolled-back'}]
        for r in rows:
            self.act.record(r)
        self.assertEqual(len(self.entries()), 4)
        self.assertEqual([e['kind'] for e in self.entries(show='warnings')], ['restart-loop'])
        self.assertEqual([e['result'] for e in self.entries(show='changes')], ['rolled-back', 'failed', 'ok'])
        self.assertEqual([e['result'] for e in self.entries(show='failed')], ['rolled-back', 'failed'])
        self.assertEqual([e['target'] for e in self.entries(stack='web')], ['web-app', 'web'])
        self.assertEqual(len(self.entries(limit='1')), 1)
        for bad in ({'show': 'nope'}, {'limit': 'x'}, {'limit': '0'}):
            with self.assertRaises(ValueError):
                self.act.listing(bad)

    def test_the_backup_holds_the_log(self):
        import io
        import tarfile
        self.act.record({'type': 'change', 'kind': 'image-pull', 'target': 'a', 'result': 'ok'})
        from stack_ui import backups
        from datetime import datetime
        _, data = backups.build(datetime.now())
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tar:
            self.assertIn('activity.jsonl', tar.getnames())


class SourceTest(ActivityBase):
    """page: signed in on the page; script: the API token; schedule: no request (the daily check, Run now)."""

    def setUp(self):
        super().setUp()
        self.ui.Config.password_hash = ''
        self.patch(self.ui.auth, 'SETTINGS_FILE', self.tmp / 'settings.json')
        server = ThreadingHTTPServer(('127.0.0.1', 0), self.ui.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]
        self.folder = make_stack(self.stacks, 'web', compose_yaml('web'))
        self.mock.add_container('web', project='web')
        lifecycle.deploy(self.folder, 'compose.yaml', '.env', pull=False, approve=lambda c: True, log=lambda _: None)

    def call(self, method, path, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=15)
        self.addCleanup(conn.close)
        conn.request(method, path, body='{}' if method == 'POST' else None,
                     headers=headers or {'X-UI-Token': self.ui.Config.local_token, 'Content-Type': 'application/json'})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b'{}')

    def test_page_and_script(self):
        self.assertEqual(self.call('POST', '/api/stacks/web/stop')[0], 200)
        token = self.ui.api_token({})['token']
        bearer = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        self.assertEqual(self.call('POST', '/api/stacks/web/start', bearer)[0], 200)
        with self.ui.core.operation('image-pull', 'x'):        # this thread: no request
            pass
        self.assertEqual([(e['kind'], e['source']) for e in self.entries()],
                         [('image-pull', 'schedule'), ('stack-start', 'script'), ('stack-stop', 'page')])

    def test_the_route_is_open_to_the_api_token_too(self):
        self.assertEqual(self.call('POST', '/api/stacks/web/stop')[0], 200)
        code, body = self.call('GET', '/api/activity?show=changes&limit=10')
        self.assertEqual((code, [e['kind'] for e in body['entries']]), (200, ['stack-stop']))
        token = self.ui.api_token({})['token']
        code, body = self.call('GET', '/api/activity', {'Authorization': f'Bearer {token}'})
        self.assertEqual(code, 200)
        self.assertEqual(self.call('GET', '/api/activity?show=nope')[0], 400)


class WatchTest(ActivityBase):
    """The watcher looks every minute; it writes when something starts and when it ends, not at every look, and it
    remembers what it saw across a restart of the page (its state is in the data folder)."""

    def setUp(self):
        super().setUp()
        self.now = int(time.time())

    def container(self, name, **kw):
        row = self.mock.add_container(name, project='web')
        row.update(kw)
        return row

    def warnings(self):
        return [(e['kind'], e['target'], e['result']) for e in reversed(self.entries(show='warnings'))]

    def test_a_restart_loop_starts_and_ends_once_each(self):
        row = self.container('web', Restarts=5, StartedAt=self.now - 20)
        self.act.watch_once()
        self.act.watch_once()
        self.assertEqual(self.warnings(), [('restart-loop', 'web', 'started')])
        self.assertEqual(self.entries(show='warnings')[0]['stack'], 'web')
        row['StartedAt'] = self.now - 3600                      # it has run for an hour: no longer looping
        self.act.watch_once()
        self.assertEqual(self.warnings(), [('restart-loop', 'web', 'started'), ('restart-loop', 'web', 'ended')])

    def test_what_was_seen_survives_a_restart_of_the_page(self):
        self.container('web', Restarts=5, StartedAt=self.now - 20)
        self.act.watch_once()
        self.act.forget_watch_memory()                          # as after a restart: only the file is left
        self.act.watch_once()
        self.assertEqual(len(self.warnings()), 1)

    def test_an_out_of_memory_kill_is_written_once_per_exit(self):
        row = self.container('web', Restarts=1, StartedAt=self.now - 20, ExitCode=137, ExitedAt=self.now - 21, MockOOM=True)
        self.act.watch_once()
        self.act.watch_once()
        self.assertEqual(self.warnings(), [('oom', 'web', 'killed')])
        row.update(ExitedAt=self.now - 5)                      # killed again
        self.act.watch_once()
        self.assertEqual(self.warnings(), [('oom', 'web', 'killed'), ('oom', 'web', 'killed')])

    def test_memory_near_the_limit_after_two_looks_and_its_end(self):
        self.container('web')
        stat = {'ContainerID': 'id-web', 'Name': 'web', 'CPUNano': 1, 'SystemNano': 1, 'MemUsage': 950, 'MemLimit': 1000, 'PIDs': 1}
        self.mock.stats = [stat]
        self.act.watch_once()
        self.assertEqual(self.warnings(), [])                  # one look is not enough
        self.act.watch_once()
        self.assertEqual(self.warnings(), [('memory-near', 'web', 'started')])
        self.assertIn('95 %', self.entries(show='warnings')[0]['message'])
        self.act.watch_once()
        stat['MemUsage'] = 500
        self.act.watch_once()
        self.assertEqual(self.warnings(), [('memory-near', 'web', 'started'), ('memory-near', 'web', 'ended')])

    def test_podman_not_answering_writes_nothing(self):
        self.patch(self.ui.pa, 'API_URL', 'http://127.0.0.1:9')
        self.act.watch_once()
        self.assertEqual(self.entries(), [])


class PageTest(ActivityBase):
    def test_the_activity_page(self):
        page = (self.ui.PAGE).read_text(encoding='utf-8')
        for name in ('data-nav="activity"', 'function viewActivity', 'function renderActivity', '/api/activity?',
                     'act-show', 'act-stack', 'function activityRows', 'Recent activity'):
            self.assertIn(name, page, name)
