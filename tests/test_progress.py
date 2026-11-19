"""The progress of a running change, for the page's bar: what step it is on (pulling images, recreating the
containers, waiting for health checks), how many of how many, and a detail line. Podman gives no byte counts when
it does not write to a terminal, so the bar counts images done (an image is done at its id line) and the detail
counts the layers copied. podman-compose may pull several images at once: their lines interleave."""
import os
import time

from tests.support import Base, compose_yaml, fresh_server, make_stack
from stack_ui import lifecycle
from stack_ui import stacks as st

# what Podman 5.7 printed on the Pi for one pull (the install's output), the ids made up
ONE_PULL = """Trying to pull ghcr.io/example/app:1...
Getting image source signatures
Copying blob sha256:1111111111111111111111111111111111111111111111111111111111111111
Copying blob sha256:2222222222222222222222222222222222222222222222222222222222222222
Copying blob sha256:3333333333333333333333333333333333333333333333333333333333333333
Copying config sha256:4444444444444444444444444444444444444444444444444444444444444444
Writing manifest to image destination
4444444444444444444444444444444444444444444444444444444444444444"""


class Recorder:
    """Stands in for core.Operation where a test needs no lock: keeps every progress it was given."""
    cancelled = False
    proc = None

    def __init__(self):
        self.seen = []

    @property
    def progress(self):
        return self.seen[-1] if self.seen else None

    @progress.setter
    def progress(self, value):
        self.seen.append(value)


class ProgressBase(Base):
    def setUp(self):
        super().setUp()
        self.op = Recorder()
        st.RUNNING.op = self.op
        self.addCleanup(setattr, st.RUNNING, 'op', None)

    def steps(self):
        return [p['step'] for p in self.op.seen]


class PullLinesTest(ProgressBase):
    def test_one_image_its_layers_then_done(self):
        track = st.PullProgress(1)
        for line in ONE_PULL.splitlines():
            track(line)
        self.assertEqual(self.op.seen[0], {'step': 'Pulling images', 'detail': 'ghcr.io/example/app:1', 'done': 0, 'total': 1})
        self.assertEqual(self.op.progress, {'step': 'Pulling images', 'detail': 'ghcr.io/example/app:1, 3 layers so far', 'done': 1, 'total': 1})

    def test_images_pulled_at_once_count_by_their_ids(self):
        track = st.PullProgress(2)
        a, b = ONE_PULL.splitlines(), ONE_PULL.replace('app:1', 'db:2').replace('4444', '5555').splitlines()
        for line in [a[0], b[0], a[2], b[2], b[3], b[-1], a[3], a[-1]]:        # interleaved
            track(line)
        self.assertEqual([(p['done'], p['total']) for p in self.op.seen][-1], (2, 2))
        self.assertEqual(max(p['done'] for p in self.op.seen), 2)

    def test_more_images_than_counted_and_lines_it_does_not_know(self):
        track = st.PullProgress(1)
        for line in ONE_PULL.splitlines() + ONE_PULL.splitlines() + ['podman pull x', '', 'Error: whatever']:
            track(line)
        self.assertEqual((self.op.progress['done'], self.op.progress['total']), (2, 2))

    def test_without_an_operation_nothing_breaks(self):
        st.RUNNING.op = None
        st.PullProgress(1)(ONE_PULL.splitlines()[0])
        st.set_progress('Recreating containers')

    def test_the_images_a_pull_fetches(self):
        folder = make_stack(self.stacks, 'web', 'services:\n'
                            '  a:\n    image: docker.io/library/nginx:1\n'
                            '  b:\n    image: docker.io/library/nginx:1\n'        # the same image once
                            '  c:\n    image: docker.io/library/redis:7\n'
                            '  d:\n    build: .\n    image: localhost/mine:1\n'   # built here
                            '  e:\n    image: localhost/kept:1\n    pull_policy: never\n')
        self.assertEqual(st.pull_count(folder, 'compose.yaml'), 2)
        self.assertEqual(st.pull_count(folder, 'compose.yaml', ['c']), 1)
        (folder / 'compose.yaml').write_bytes(b'not: [yaml')
        self.assertIsNone(st.pull_count(folder, 'compose.yaml'))


class LiveLinesTest(ProgressBase):
    """run_compose hands each line over while the command runs, not when it has ended."""

    def setUp(self):
        super().setUp()
        self.folder = make_stack(self.stacks, 'web', compose_yaml('web'))
        out = self.tmp / 'out.txt'
        out.write_bytes(ONE_PULL.encode())
        for k, v in (('FAKE_COMPOSE_OUTPUT', str(out)), ('FAKE_COMPOSE_LINE_DELAY', '0.15')):
            os.environ[k] = v
            self.addCleanup(os.environ.pop, k, None)

    def test_lines_arrive_while_it_runs_and_the_log_is_unchanged(self):
        seen, log = [], []
        started = time.monotonic()
        st.run_compose(self.folder, 'compose.yaml', '.env', ['pull'], log.append,
                       on_line=lambda line: seen.append((time.monotonic() - started, line)))
        ended = time.monotonic() - started
        lines = [line for _, line in seen]
        self.assertEqual(lines[:8], ONE_PULL.splitlines())
        self.assertLess(seen[0][0], ended - 0.5, 'the first line came only at the end')
        self.assertEqual(log[0], '$ podman-compose pull')
        self.assertEqual(log[1:9], ONE_PULL.splitlines())

    def test_a_failing_progress_does_not_fail_the_command(self):
        def boom(line):
            raise RuntimeError('no')
        st.run_compose(self.folder, 'compose.yaml', '.env', ['pull'], [].append, on_line=boom)


class StepsTest(ProgressBase):
    def setUp(self):
        super().setUp()
        self.ui = fresh_server()
        self.folder = make_stack(self.stacks, 'web', compose_yaml('web'))
        out = self.tmp / 'out.txt'
        out.write_bytes(ONE_PULL.encode())
        os.environ['FAKE_COMPOSE_OUTPUT'] = str(out)
        self.addCleanup(os.environ.pop, 'FAKE_COMPOSE_OUTPUT', None)

    def test_an_update_pulls_then_recreates(self):
        lifecycle.deploy(self.folder, 'compose.yaml', '.env', approve=lambda c: True, pull=True, log=lambda _: None)
        self.assertEqual(self.steps()[0], 'Pulling images')
        self.assertEqual(self.steps()[-1], 'Recreating containers')
        pulled = [p for p in self.op.seen if p['step'] == 'Pulling images']
        self.assertEqual((pulled[-1]['done'], pulled[-1]['total']), (1, 1))

    def test_without_a_pull_it_only_recreates(self):
        lifecycle.deploy(self.folder, 'compose.yaml', '.env', approve=lambda c: True, pull=False, log=lambda _: None)
        self.assertEqual(self.steps(), ['Recreating containers'])

    def test_the_operation_sends_it(self):
        with self.ui.core.operation('deploy', 'web') as op:
            self.assertIsNone(op.view()['progress'])
            st.RUNNING.op.progress = {'step': 'Pulling images', 'detail': 'x', 'done': 1, 'total': 3}
            self.assertEqual(op.view()['progress'], {'step': 'Pulling images', 'detail': 'x', 'done': 1, 'total': 3})

    def test_an_image_pull_on_the_images_page(self):
        # the pull registers its own operation: what it is told is caught on its way
        self.patch(st, 'set_progress', lambda step, detail='', done=None, total=None, op=None: self.op.seen.append(
            {'step': step, 'detail': detail, 'done': done, 'total': total}))
        r = self.ui.image_pull({'reference': 'docker.io/library/nginx:1'})
        self.assertEqual(r['pulled'], 'docker.io/library/nginx:1')
        rows = [p for p in self.op.seen if p['step'] == 'Pulling image']
        self.assertTrue(rows, 'no progress while pulling')
        self.assertEqual((rows[-1]['done'], rows[-1]['total']), (1, 1))
        self.assertIn('2 layers', rows[-1]['detail'])
