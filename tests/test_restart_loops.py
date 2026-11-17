"""A container that crashes and is started again by its restart policy looks "running" between two crashes. libpod's
Restarts (how often the policy restarted it) and StartedAt (its last start) show it: at least LOOP_RESTARTS restarts
and a start within LOOP_WINDOW (or the state "restarting") is a restart loop, shown on the stacks, the containers, and
the Health page, whose level it raises to warn."""
import time
import urllib.request

from tests.support import Base, compose_yaml, fresh_server, make_stack


class RestartLoopTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = fresh_server()
        from stack_ui import health
        self.hl = health
        health._OOM.clear()                  # the answers are kept per exit for the life of the page
        self.now = int(time.time())

    def c(self, restarts, started_ago=None, state='running', name='app'):
        return {'Names': [name], 'State': state, 'Restarts': restarts,
                'StartedAt': self.now - started_ago if started_ago is not None else 0}

    def test_what_is_a_loop(self):
        hl, w = self.hl, self.hl.LOOP_WINDOW
        self.assertEqual((hl.LOOP_RESTARTS, w), (3, 600))
        self.assertTrue(hl.looping(self.c(3, 30), self.now))
        self.assertTrue(hl.looping(self.c(12, w - 1), self.now))
        self.assertTrue(hl.looping(self.c(3, state='restarting'), self.now))
        self.assertFalse(hl.looping(self.c(2, 30), self.now))              # too few restarts
        self.assertFalse(hl.looping(self.c(9, w + 60), self.now))           # restarted long ago, runs since
        self.assertFalse(hl.looping(self.c(9, 30, state='exited'), self.now))   # stopped: no longer restarting
        self.assertFalse(hl.looping({'Names': ['old'], 'State': 'running'}, self.now))   # an older Podman: no field
        self.assertEqual([hl.restarts(x) for x in ({'Restarts': 4}, {}, {'Restarts': None}, {'Restarts': 'x'})], [4, 0, 0, 0])

    def test_the_health_page_names_the_loops_and_warns(self):
        self.mock.containers += [self.c(5, 20, name='crashy'), self.c(1, 20, name='calm')]
        h = self.hl.health([], 4)
        self.assertEqual(h['podman']['looping'], [{'name': 'crashy', 'restarts': 5, 'oom': False}])
        self.assertIn(h['level'], ('warn', 'bad'))
        self.mock.containers[:] = [self.c(1, 20, name='calm')]
        self.assertEqual(self.hl.health([], 4)['podman']['looping'], [])

    def test_the_stacks_and_the_containers_carry_the_count(self):
        make_stack(self.stacks, 'web', compose_yaml('web'))
        row = self.mock.add_container('web', project='web')
        row.update(Restarts=7, StartedAt=self.now - 5)
        stack = next(s for s in self.ui.overview()['stacks'] if s['name'] == 'web')
        self.assertEqual({k: stack['containers'][0][k] for k in ('restarts', 'looping')}, {'restarts': 7, 'looping': True})
        table = next(r for r in self.ui.container_table()['containers'] if r['name'] == 'web')
        self.assertEqual((table['restarts'], table['looping']), (7, True))

    def oomc(self, name='app', exit_code=137, oom=True, exited_at=1000, state='running', restarts=5, started_ago=30):
        row = self.mock.add_container(name, state=state)
        row.update(Restarts=restarts, StartedAt=self.now - started_ago, ExitCode=exit_code, ExitedAt=exited_at, MockOOM=oom)
        return row

    def test_an_out_of_memory_kill_is_read_from_the_last_exit(self):
        hl = self.hl
        killed = self.oomc('killed')
        self.assertTrue(hl.oom_killed(killed))
        # Podman never clears OOMKilled: a later exit that was not a kill (exit code 1) is not "out of memory"
        self.assertFalse(hl.oom_killed(self.oomc('later', exit_code=1, oom=True)))
        self.assertFalse(hl.oom_killed(self.oomc('plain', exit_code=137, oom=False)))   # killed, not for memory
        self.assertEqual(self.mock.inspected, ['id-killed', 'id-plain'])   # exit code 1: not even asked

    def test_one_question_per_exit(self):
        row = self.oomc('killed')
        self.assertTrue(self.hl.oom_killed(row))
        self.assertTrue(self.hl.oom_killed(row))
        self.assertEqual(self.mock.inspected, ['id-killed'])
        row.update(ExitedAt=2000, MockOOM=False)       # a new exit is asked again
        self.assertFalse(self.hl.oom_killed(row))
        self.assertEqual(self.mock.inspected, ['id-killed', 'id-killed'])

    def test_podman_not_answering_is_no_oom(self):
        self.assertFalse(self.hl.oom_killed({'Id': 'gone', 'Names': ['gone'], 'State': 'exited', 'ExitCode': 137, 'ExitedAt': 5}))

    def test_the_rows_and_the_health_page_carry_it(self):
        make_stack(self.stacks, 'web', compose_yaml('web'))
        row = self.oomc('web')
        row['Labels']['io.podman.compose.project'] = 'web'
        stack = next(s for s in self.ui.overview()['stacks'] if s['name'] == 'web')
        self.assertTrue(stack['containers'][0]['oom'])
        self.assertTrue(next(r for r in self.ui.container_table()['containers'] if r['name'] == 'web')['oom'])
        self.assertEqual(self.hl.health([], 4)['podman']['looping'], [{'name': 'web', 'restarts': 5, 'oom': True}])

    def test_the_page_shows_them(self):
        from http.server import ThreadingHTTPServer
        import threading
        self.ui.Config.password_hash = ''
        server = ThreadingHTTPServer(('127.0.0.1', 0), self.ui.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with urllib.request.urlopen(f'http://127.0.0.1:{server.server_address[1]}/', timeout=10) as r:
            page = r.read().decode('utf-8')
        for name in ('function restartTag', 'restartTag(c)', '<dt>Restart loops</dt>', 'p.looping', 'c.looping',
                     'Restart loop', 'Out of memory', 'c.oom', 'l.oom', 'mem_limit'):
            self.assertIn(name, page, name)
