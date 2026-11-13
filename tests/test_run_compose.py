import os
import subprocess
import sys
import threading
import time
import types
import unittest

from tests.support import Base, compose_yaml, make_stack
from stack_ui import stacks as st

POSIX = hasattr(os, 'killpg')


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


class RunComposeTest(Base):
    def setUp(self):
        super().setUp()
        self.folder = make_stack(self.stacks, 'web', compose_yaml('web'))
        self.pids = self.tmp / 'pids'
        os.environ['FAKE_COMPOSE_PIDS'] = str(self.pids)
        self.addCleanup(os.environ.pop, 'FAKE_COMPOSE_PIDS', None)
        self.addCleanup(os.environ.pop, 'FAKE_COMPOSE_HANG', None)
        self.addCleanup(os.environ.pop, 'FAKE_COMPOSE_IGNORE_TERM', None)

    def run_up(self, **kw):
        log = []
        st.run_compose(self.folder, 'compose.yaml', '.env', ['up', '-d'], log.append, **kw)
        return log

    def hang(self, spec):
        os.environ['FAKE_COMPOSE_HANG'] = spec

    def processes(self):
        pairs = [tuple(map(int, line.split())) for line in self.pids.read_text().splitlines()]
        return [pid for pair in pairs for pid in pair]

    def wait_for_pids(self, seconds=5):
        deadline = time.time() + seconds
        while time.time() < deadline and not (self.pids.exists() and self.pids.read_text().strip()):
            time.sleep(0.02)
        self.assertTrue(self.pids.exists() and self.pids.read_text().strip(), 'the fake command never started hanging')

    def wait_gone(self, pids, seconds=5):
        deadline = time.time() + seconds
        while time.time() < deadline and any(alive(p) for p in pids):
            time.sleep(0.05)
        return [p for p in pids if alive(p)]

    def test_a_normal_command_still_logs_and_a_failing_one_still_raises(self):
        self.assertIn('fake podman-compose', ' '.join(self.run_up()))
        os.environ['FAKE_COMPOSE_FAIL_ON'] = 'up'
        with self.assertRaisesRegex(st.StackError, r'failed \(exit code 3\)'):
            self.run_up()

    def test_up_ends_at_the_time_limit_and_names_what_is_not_ready(self):
        self.patch(st, 'OP_TIMEOUT', 1)
        self.hang('up:30')
        self.mock.add_container('web-db', project='web', state='Up 2 minutes (unhealthy)')
        self.mock.add_container('web-app', project='web', state='Up 3 seconds (starting)')
        self.mock.add_container('web-cache', project='web', state='Up 5 minutes (healthy)')
        started = time.time()
        with self.assertRaises(st.StackError) as ctx:
            self.run_up()
        message = str(ctx.exception)
        self.assertLess(time.time() - started, 10)
        self.assertIn('podman-compose up did not finish in 1 second', message)
        self.assertIn('web-db (unhealthy)', message)
        self.assertIn('web-app (starting)', message)
        self.assertNotIn('web-cache', message)

    def test_what_a_command_printed_before_its_time_limit_is_in_the_log(self):
        self.patch(st, 'OP_TIMEOUT', 1)
        self.hang('up:30')
        log = []
        with self.assertRaisesRegex(st.StackError, 'did not finish'):
            st.run_compose(self.folder, 'compose.yaml', '.env', ['up', '-d'], log.append)
        self.assertTrue(any('fake podman-compose started' in line for line in log), log)

    def test_only_the_last_lines_of_a_stopped_command_are_kept(self):
        self.assertEqual(st.MAX_OUTPUT_LINES, 200)
        log = []
        st.emit_tail(chr(10).join(f'line {i}' for i in range(500)), log.append)
        self.assertEqual((len(log), log[0], log[-1]), (200, 'line 300', 'line 499'))

    def test_what_a_cancelled_command_printed_is_in_the_log(self):
        self.hang('up:30')
        op = types.SimpleNamespace(proc=None, cancelled=False)
        log, result = [], {}

        def worker():
            st.RUNNING.op = op
            try:
                st.run_compose(self.folder, 'compose.yaml', '.env', ['up', '-d'], log.append)
            except st.StackError as exc:
                result['error'] = str(exc)
        t = threading.Thread(target=worker)
        t.start()
        deadline = time.time() + 5
        while op.proc is None and time.time() < deadline:
            time.sleep(0.02)
        self.wait_for_pids()
        op.cancelled = True
        st.kill_tree(op.proc)
        t.join(10)
        self.assertTrue(result['error'].startswith('cancelled'), result)
        self.assertTrue(any('fake podman-compose started' in line for line in log), log)

    def test_the_time_limit_without_a_container_list_still_ends_cleanly(self):
        self.patch(st, 'OP_TIMEOUT', 1)
        self.hang('up:30')
        self.patch(st.pa, 'API_URL', 'http://127.0.0.1:9')       # nothing answers there
        with self.assertRaisesRegex(st.StackError, r'did not finish in 1 second$'):
            self.run_up()

    def test_pull_keeps_the_long_limit_and_the_others_get_the_short_one(self):
        self.patch(st, 'OP_TIMEOUT', 1)
        self.patch(st, 'LONG_TIMEOUT', 30)
        self.hang('pull:2')
        log = []
        st.run_compose(self.folder, 'compose.yaml', '.env', ['pull'], log.append)     # 2 s > OP_TIMEOUT, within 30
        self.assertIn('fake podman-compose', ' '.join(log))
        self.hang('start:2')
        with self.assertRaises(st.StackError):
            st.run_compose(self.folder, 'compose.yaml', '.env', ['start'], [].append)

    @unittest.skipUnless(POSIX, 'process groups are POSIX')
    def test_a_timeout_ends_the_command_and_its_child(self):
        self.patch(st, 'OP_TIMEOUT', 1)
        self.hang('up:30')
        with self.assertRaises(st.StackError):
            self.run_up()
        self.assertEqual(self.wait_gone(self.processes()), [])

    @unittest.skipUnless(POSIX, 'process groups are POSIX')
    def test_a_child_that_keeps_the_pipe_after_the_leader_exited_does_not_hang_the_time_limit(self):
        self.patch(st, 'OP_TIMEOUT', 1)
        os.environ['FAKE_COMPOSE_ORPHAN'] = 'up:25'
        self.addCleanup(os.environ.pop, 'FAKE_COMPOSE_ORPHAN', None)
        started = time.time()
        with self.assertRaisesRegex(st.StackError, 'did not finish'):
            self.run_up()
        self.assertLess(time.time() - started, 10)             # not the 25 s the child sleeps
        self.assertEqual(self.wait_gone(self.processes()), [])

    @unittest.skipUnless(POSIX, 'process groups are POSIX')
    def test_cancel_also_ends_a_child_that_outlived_the_leader(self):
        os.environ['FAKE_COMPOSE_ORPHAN'] = 'up:25'
        self.addCleanup(os.environ.pop, 'FAKE_COMPOSE_ORPHAN', None)
        op = types.SimpleNamespace(proc=None, cancelled=False)
        result = {}

        def worker():
            st.RUNNING.op = op
            try:
                self.run_up()
            except st.StackError as exc:
                result['error'] = str(exc)
        t = threading.Thread(target=worker)
        t.start()
        deadline = time.time() + 5
        while op.proc is None and time.time() < deadline:
            time.sleep(0.02)
        self.wait_for_pids()
        time.sleep(0.4)                                        # the leader has exited by now, the child has not
        started = time.time()
        op.cancelled = True
        st.kill_tree(op.proc)
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertLess(time.time() - started, 8)
        self.assertTrue(result['error'].startswith('cancelled'), result)
        self.assertEqual(self.wait_gone(self.processes()), [])

    @unittest.skipUnless(POSIX, 'process groups are POSIX')
    def test_a_command_that_ignores_sigterm_is_killed_after_the_grace(self):
        self.patch(st, 'OP_TIMEOUT', 1)
        self.patch(st, 'KILL_GRACE', 0.3)
        self.hang('up:30')
        os.environ['FAKE_COMPOSE_IGNORE_TERM'] = '1'
        with self.assertRaises(st.StackError):
            self.run_up()
        self.assertEqual(self.wait_gone(self.processes()), [])

    def test_cancel_while_it_runs(self):
        self.hang('up:30')
        op = types.SimpleNamespace(proc=None, cancelled=False)
        result = {}

        def worker():
            st.RUNNING.op = op
            try:
                self.run_up()
            except st.StackError as exc:
                result['error'] = str(exc)
        t = threading.Thread(target=worker)
        t.start()
        deadline = time.time() + 5
        while op.proc is None and time.time() < deadline:
            time.sleep(0.02)
        self.assertIsNotNone(op.proc, 'the command never registered its process')
        self.wait_for_pids()                                  # it must really hang before it is cancelled
        started = time.time()
        op.cancelled = True
        st.kill_tree(op.proc)
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertLess(time.time() - started, 8)
        self.assertTrue(result['error'].startswith('cancelled'), result)
        if POSIX:
            self.assertEqual(self.wait_gone(self.processes()), [])

    def test_cancel_before_the_process_is_registered_ends_it_at_once(self):
        self.hang('up:30')
        op = types.SimpleNamespace(proc=None, cancelled=True)
        st.RUNNING.op = op
        self.addCleanup(setattr, st.RUNNING, 'op', None)
        started = time.time()
        with self.assertRaisesRegex(st.StackError, '^cancelled'):
            self.run_up()
        self.assertLess(time.time() - started, 8)

    def test_a_cancel_after_the_command_finished_is_harmless(self):
        op = types.SimpleNamespace(proc=None, cancelled=False)
        st.RUNNING.op = op
        self.addCleanup(setattr, st.RUNNING, 'op', None)
        self.run_up()
        self.assertIsNone(op.proc)                        # unregistered when the command ended
        done = subprocess.Popen([sys.executable, '-c', 'pass'])
        done.wait()
        st.kill_tree(done)                                # an already finished process, its group gone: nothing happens

    def test_duration_reads_naturally(self):
        self.assertEqual([st.duration(s) for s in (0, 1, 40, 60, 125, 3 * 60)],
                         ['0 seconds', '1 second', '40 seconds', '1 minute', '2 minutes', '3 minutes'])


class UpLimitTest(Base):
    def setUp(self):
        super().setUp()
        self.folder = make_stack(self.stacks, 'web', compose_yaml('web'))      # image: docker.io/library/nginx:latest

    def have(self, *tags):
        for tag in tags:
            self.mock.images.append({'Id': f'i{len(self.mock.images)}', 'RepoTags': [tag], 'RepoDigests': [],
                                     'Size': 1, 'Created': 1})

    def compose(self, text):
        (self.folder / 'compose.yaml').write_text(text, encoding='utf-8')

    def test_images_ready_only_when_every_image_is_on_the_host(self):
        self.assertFalse(st.images_ready(self.folder, 'compose.yaml'))
        self.have('docker.io/library/nginx:latest')
        self.assertTrue(st.images_ready(self.folder, 'compose.yaml'))

    def test_short_names_mean_docker_hub_and_latest(self):
        self.have('docker.io/library/nginx:latest', 'docker.io/bitnami/redis:7', 'localhost/app:1', 'myreg:5000/img:2')
        for ref in ('nginx', 'nginx:latest', 'bitnami/redis:7', 'localhost/app:1', 'myreg:5000/img:2'):
            self.compose(f'services:\n  a:\n    image: {ref}\n')
            self.assertTrue(st.images_ready(self.folder, 'compose.yaml'), ref)
        self.compose('services:\n  a:\n    image: nginx:1.27\n')
        self.assertFalse(st.images_ready(self.folder, 'compose.yaml'))

    def test_when_unsure_the_answer_is_no(self):
        self.have('docker.io/library/nginx:latest')
        for text in ('services:\n  a:\n    image: nginx:${TAG:-latest}\n',
                     'services:\n  a:\n    build: .\n',
                     'services:\n  a:\n    image: nginx\n    build: .\n',
                     'services:\n  a:\n    image: nginx@sha256:abc\n',
                     'services:\n  a:\n    command: x\n',
                     'services: {}\n', 'not: compose\n', 'a: [\n'):
            self.compose(text)
            self.assertFalse(st.images_ready(self.folder, 'compose.yaml'), text)
        self.compose(compose_yaml('web'))
        self.patch(st.pa, 'API_URL', 'http://127.0.0.1:9')      # Podman cannot be asked
        self.assertFalse(st.images_ready(self.folder, 'compose.yaml'))

    def test_up_gets_the_long_limit_when_it_has_images_to_download(self):
        seen = []
        self.patch(st, 'run_compose', lambda folder, compose_file, env_file, args, log, timeout=None:
                   seen.append((args[0], timeout)))

        def deploy(pull):
            seen.clear()
            st.deploy(self.folder, 'compose.yaml', '.env', approve=lambda c: True, pull=pull, log=lambda _: None)
            return dict(seen)
        self.assertEqual(deploy(pull=False)['up'], st.LONG_TIMEOUT)         # the image is missing: up pulls it
        self.assertIsNone(deploy(pull=True)['up'])                          # pulled first: the short limit
        self.have('docker.io/library/nginx:latest')
        self.assertIsNone(deploy(pull=False)['up'])                         # nothing to download: the short limit


if __name__ == '__main__':
    unittest.main()
