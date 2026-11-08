# Running operation: status, cancel, time limit: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A hung `podman-compose` change no longer freezes the page silently: the running change is visible, further changes are refused at once with its description, the command can be cancelled from the page, and an `up` that does not finish ends after 5 minutes naming the containers that are not ready.

**Architecture:** The one change lock stays, taken through a non-waiting context manager `operation()` that records what holds it (`Operation`). `stacks.run_compose` starts its command with `Popen` in a process group of its own and registers the process with the running operation through a thread-local handle; `kill_tree` ends the whole group. Two routes (`GET /api/operation`, `POST /api/operation/cancel`) and a bar on the page expose and cancel it.

**Tech Stack:** Python 3.9+ standard library only, the page's plain JavaScript (`page.html`), `unittest` with the repo's mock libpod and fake `podman-compose`.

**Spec:** `docs/superpowers/specs/2026-11-08-operations-design.md`

## Global Constraints

- No new dependency; standard library only (PyYAML is already used); runs on Python 3.9 (CI runs 3.9 and 3.13): no `match`, no `X | Y` outside annotations (the modules have `from __future__ import annotations`), no `Path.write_text(..., newline=)`.
- One change at a time stays: a second change while one runs is refused at once with `409` and the running operation's description; it never waits.
- `LONG_TIMEOUT` = 900 s stays for `pull`; `OP_TIMEOUT` (`STACK_UI_UP_TIMEOUT`, default 300 s, at least 30 from the environment) for `up`, `start`, `stop`, `down`; `check_config` keeps 60 s.
- Cancel ends the command and everything it started (POSIX: its process group: SIGTERM, then SIGKILL after `KILL_GRACE` = 3 s); it does not touch the containers the command already created. Only `podman-compose` operations (`deploy`, `stack-start`, `stack-stop`, `stack-delete`) are cancellable; image pulls and quick changes are not.
- `GET /api/operation` and `POST /api/operation/cancel` use the same sign-in as every `/api/` route (page session + `X-UI-Token`, or the API token).
- All text from the server reaches the DOM through `esc()` or `textContent`; user-visible words are English, plain, sentence case.
- Files in the repo use LF line ends and UTF-8; the repo stays English.
- **Git:** the user commits, pushes and tags only on their own explicit word. The "Commit" steps below give the message for when they say so; do not run them on your own. A commit message ends with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- The checkout is on the branch the user chose (check `git branch --show-current`; at the time of writing `main`). Version is not bumped in this plan: the CHANGELOG bullet goes under a new `## Unreleased` section; the release (1.0.20) is the user's step.
- The spec calls the 409 exception `Occupied`; this plan calls it `Conflict` because it also covers the two refusals of cancel ("nothing is running", "cannot be cancelled"). Behaviour is the spec's.

## Review Focus

1. The command has children (podman-compose starts `podman` processes): cancel and timeout must end the whole group, a command that ignores SIGTERM must still die (SIGKILL after the grace), and nothing may be left running (Task 1 and Task 3 tests, POSIX).
2. Cancel arrives before the process is registered, while it runs, or after it finished: all three end cleanly (cancelled at once, cancelled, `409` "nothing is running"), never a hang or a kill of a later operation (Task 1 and Task 3 tests).
3. The lock must be released on every exit of an operation: normal return, exception, cancel, timeout (Task 2 and Task 3 tests).
4. Behaviour change for quick changes: while a deploy runs, a secret/network/volume/image/container change gets `409` with the reason instead of waiting; the page and the client show that text (Task 2, Task 4, Task 5).
5. A timeout when Podman cannot list the containers, or lists none that are unready: the error still ends the command cleanly without a bogus list (Task 1 test).

---

### Task 1: `run_compose` with `Popen`, process-group kill, time limits and cancel hook

**Files:**
- Modify: `stack_ui/stacks.py` (imports; constants near `LONG_TIMEOUT`; `run_compose`; new `kill_tree`, `not_ready`, `duration`, `RUNNING`)
- Modify: `tests/fake_compose.py` (hang, grandchild, ignore SIGTERM)
- Create: `tests/test_run_compose.py`

**Interfaces:**
- Produces:
  - `stacks.RUNNING` (`threading.local`): `RUNNING.op` is the operation of the calling thread or absent. An operation object needs the attributes `proc` (a `Popen` or `None`) and `cancelled` (`bool`).
  - `stacks.OP_TIMEOUT: int`, `stacks.KILL_GRACE: float`, `stacks.duration(seconds) -> str` ("40 seconds", "3 minutes"), `stacks.kill_tree(proc, grace=None) -> None`, `stacks.not_ready(project: str) -> list[str]` (e.g. `['web-db (unhealthy)']`).
  - `stacks.run_compose(folder, compose_file, env_file, args, log, timeout=None)`: `timeout=None` means `LONG_TIMEOUT` for `pull`, else `OP_TIMEOUT`. Raises `StackError` ("... did not finish in N ...", with "; not ready: ..." when known) on timeout and `StackError('cancelled: ...')` when the registered operation was cancelled.
  - Fake compose environment: `FAKE_COMPOSE_HANG=<command>:<seconds>` (sleep on that command and start a sleeping child), `FAKE_COMPOSE_PIDS=<file>` (appends `"<pid> <child pid>"`), `FAKE_COMPOSE_IGNORE_TERM=1` (ignore SIGTERM, also in the child).

- [ ] **Step 1: Teach the fake `podman-compose` to hang**

Replace the whole of `tests/fake_compose.py` with:

```python
"""Stands in for podman-compose in the tests: records its arguments and working directory, prints a line, and
exits 3 when FAKE_COMPOSE_FAIL_ON names one of its arguments (e.g. "up").

FAKE_COMPOSE_HANG="up:30" makes it sleep 30 s on that command, with a sleeping child of its own (like the podman
processes the real one starts); FAKE_COMPOSE_PIDS names a file that gets "<pid> <child pid>" appended;
FAKE_COMPOSE_IGNORE_TERM=1 makes both ignore SIGTERM (only SIGKILL ends them)."""
import json
import os
import signal
import subprocess
import sys
import time

log = os.environ.get('FAKE_COMPOSE_LOG')
if log:
    with open(log, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}) + '\n')

hang_command, _, hang_seconds = os.environ.get('FAKE_COMPOSE_HANG', '').partition(':')
if hang_command and hang_command in sys.argv[1:]:
    ignore = bool(os.environ.get('FAKE_COMPOSE_IGNORE_TERM'))
    if ignore:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([sys.executable, '-c', 'import signal, time\n'
                              + ('signal.signal(signal.SIGTERM, signal.SIG_IGN)\n' if ignore else '')
                              + f'time.sleep({float(hang_seconds)})'],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # (the child must not keep the parent's output pipe open: without a process group, as on Windows, the caller
    # would otherwise wait for it after the parent was killed)
    pids = os.environ.get('FAKE_COMPOSE_PIDS')
    if pids:
        with open(pids, 'a', encoding='utf-8') as fh:
            fh.write(f'{os.getpid()} {child.pid}\n')
    time.sleep(float(hang_seconds))

print('fake podman-compose', ' '.join(sys.argv[1:]))
fail_on = os.environ.get('FAKE_COMPOSE_FAIL_ON', '')
sys.exit(3 if fail_on and fail_on in sys.argv[1:] else 0)
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_run_compose.py`:

```python
import os
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
        st.kill_tree(types.SimpleNamespace(poll=lambda: 0))   # an already finished process: nothing happens

    def test_duration_reads_naturally(self):
        self.assertEqual([st.duration(s) for s in (0, 1, 40, 60, 125, 3 * 60)],
                         ['0 seconds', '1 second', '40 seconds', '1 minute', '2 minutes', '3 minutes'])


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m unittest tests.test_run_compose -v`
Expected: the new tests fail or error (`AttributeError: ... 'OP_TIMEOUT'`, `... 'RUNNING'`, `... 'kill_tree'`, `... 'duration'`); `test_a_normal_command_still_logs_and_a_failing_one_still_raises` may already pass.

- [ ] **Step 4: Implement in `stack_ui/stacks.py`**

1. In the imports add `import signal` (after `import shutil`) and `import threading` (after `import subprocess`).
2. Replace the line `LONG_TIMEOUT = 900` with:

```python
LONG_TIMEOUT = 900          # pull
try:                        # up, start, stop, down: a command that does not finish in this time is ended
    OP_TIMEOUT = max(30, int(os.environ.get('STACK_UI_UP_TIMEOUT') or 300))
except ValueError:
    OP_TIMEOUT = 300
KILL_GRACE = 3.0            # seconds between SIGTERM and SIGKILL when a command is ended
RUNNING = threading.local()   # .op: the operation (server.py) the calling thread runs; run_compose registers with it
```

3. Replace the whole `run_compose` function with:

```python
def duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f'{seconds} second{"" if seconds == 1 else "s"}'
    minutes = seconds // 60
    return f'{minutes} minute{"" if minutes == 1 else "s"}'


def kill_tree(proc, grace: float | None = None) -> None:
    """End a command and everything it started: POSIX: its process group (it has one of its own): SIGTERM, a
    moment, then SIGKILL for whatever is left; elsewhere (development) the process itself."""
    if proc.poll() is not None:
        return
    if not hasattr(os, 'killpg'):
        proc.kill()
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(KILL_GRACE if grace is None else grace)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)         # what outlived the SIGTERM (or the command's own children)
    except (ProcessLookupError, PermissionError):
        pass


def not_ready(project: str) -> list[str]:
    """The project's containers that are unhealthy or still starting, as 'name (unhealthy)'; [] when unknown."""
    try:
        rows = project_containers(project)
    except (pa.PodmanError, OSError):
        return []
    found = []
    for c in rows:
        status = str(c.get('Status') or '').lower()
        if 'unhealthy' in status:
            found.append(f'{cname(c)} (unhealthy)')
        elif 'starting' in status:
            found.append(f'{cname(c)} (starting)')
    return found


def run_compose(folder: Path, compose_file: str, env_file: str | None, args: list[str], log,
                timeout: int | None = None) -> None:
    cmd = [*shlex.split(COMPOSE_CMD), '--podman-path', PODMAN_PATH, *in_pod_args(folder, compose_file),
           '-p', folder.name, '-f', compose_file, *(['--env-file', env_file] if env_file else []), *args]
    if timeout is None:
        timeout = LONG_TIMEOUT if args[0] == 'pull' else OP_TIMEOUT
    log('$ podman-compose ' + ' '.join(args))
    op = getattr(RUNNING, 'op', None)
    try:
        proc = subprocess.Popen(cmd, cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding='utf-8', errors='replace',
                                **({'start_new_session': True} if hasattr(os, 'killpg') else {}))
    except FileNotFoundError:
        raise StackError(f'podman-compose is not installed ({COMPOSE_CMD})') from None
    if op is not None:
        op.proc = proc                  # cancel ends it from another thread
        if op.cancelled:                # cancelled before it had started
            kill_tree(proc)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        proc.communicate()
        waiting = not_ready(folder.name)
        raise StackError(f'podman-compose {args[0]} did not finish in {duration(timeout)}'
                         + (f'; not ready: {", ".join(waiting)}' if waiting else '')) from None
    except BaseException:
        kill_tree(proc)
        raise
    finally:
        if op is not None:
            op.proc = None
    if op is not None and op.cancelled:
        raise StackError('cancelled: the command was stopped on request; what it had already created is still there')
    for line in out.splitlines():
        log(line)
    if proc.returncode:
        raise StackError(f'podman-compose {args[0]} failed (exit code {proc.returncode})')
```

(`check_config` already passes `timeout=60`, so it keeps its limit.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m unittest tests.test_run_compose tests.test_stacks tests.test_server tests.test_preflight -v`
Expected: all `ok` (on Windows the POSIX-only tests are skipped).

- [ ] **Step 6: Commit (only when the user says so)**

```bash
git add stack_ui/stacks.py tests/fake_compose.py tests/test_run_compose.py
git commit -m "Operations: run_compose ends a hung command with its children (time limit 5 min, names the containers that are not ready); a cancel hook"
```

---

### Task 2: `operation()`, `Conflict` and the 15 change sites

**Files:**
- Modify: `stack_ui/server.py` (import `contextlib`; classes and `operation()` after `class Busy`; the 15 `with DEPLOY_LOCK:` sites; `api()` except chain)
- Create: `tests/test_operations.py`

**Interfaces:**
- Consumes: `stacks.RUNNING`, `stacks.duration` (Task 1).
- Produces:
  - `server.Conflict(message, operation=None)` with `.operation` (a dict or `None`).
  - `server.Operation(kind, target='', cancellable=False)` with `.kind .target .cancellable .started .cancelled .proc`, `.text() -> str` ("the deploy of web has been running for 3 minutes"), `.view() -> dict` (`kind, target, since, seconds, cancellable, text`).
  - `server.CURRENT: Operation | None`, `server.operation(kind, target='', cancellable=False)` (context manager yielding the `Operation`; raises `Conflict` at once when the lock is taken), `server.busy_text(op) -> str`.
  - `api()` answers `Conflict` with `409` and `{"error": <message>, "operation": <view>}` (the `operation` key only when known).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_operations.py`:

```python
import importlib
import threading

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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_operations -v`
Expected: errors `AttributeError: module 'stack_ui.server' has no attribute 'operation'` / `'Conflict'` / `'Operation'` (and `CURRENT`).

- [ ] **Step 3: Add the classes and `operation()`**

In `stack_ui/server.py` add `import contextlib` to the imports (before `import difflib`), and directly after `class Busy(Exception): pass` add:

```python
class Conflict(Exception):
    """A change cannot be made now (409): another change holds the lock, or there is nothing to cancel."""
    def __init__(self, message: str, operation: dict | None = None):
        super().__init__(message)
        self.operation = operation


class Operation:
    """The change that holds DEPLOY_LOCK: what it is, since when, and (for a podman-compose command) its process."""
    TEXT = {'deploy': 'the deploy of {t}', 'stack-start': 'starting {t}', 'stack-stop': 'stopping {t}',
            'stack-delete': 'deleting {t}', 'restart': 'restarting {t}', 'container': 'the container action "{t}"',
            'image-pull': 'pulling {t}', 'image-tag': 'tagging image {t}', 'image-remove': 'removing image {t}',
            'images-remove': 'removing {t}', 'images-prune': 'removing {t}', 'network': 'the change of {t}',
            'volume': 'the change of {t}', 'secret': 'the change of {t}'}

    def __init__(self, kind: str, target: str = '', cancellable: bool = False):
        self.kind, self.target, self.cancellable = kind, target, cancellable
        self.started = time.time()
        self.cancelled = False
        self.proc = None                    # the podman-compose process, set by stacks.run_compose

    def text(self) -> str:
        what = self.TEXT.get(self.kind, '{t}').format(t=self.target)
        return f'{what} has been running for {st.duration(time.time() - self.started)}'

    def view(self) -> dict:
        return {'kind': self.kind, 'target': self.target, 'since': int(self.started),
                'seconds': int(time.time() - self.started), 'cancellable': self.cancellable, 'text': self.text()}


CURRENT: Operation | None = None     # what holds DEPLOY_LOCK now


def busy_text(running: Operation | None) -> str:
    if running is None:
        return 'Busy: another change is running. Wait for it to finish.'
    return f'Busy: {running.text()}. Wait for it to finish' + (', or cancel it on the page.' if running.cancellable else '.')


@contextlib.contextmanager
def operation(kind: str, target: str = '', cancellable: bool = False):
    """Holds the one change lock for a change. It never waits: when the lock is taken, Conflict says what holds it.
    stacks.run_compose finds the operation through stacks.RUNNING, so it can be cancelled from another thread."""
    global CURRENT
    if not DEPLOY_LOCK.acquire(blocking=False):
        running = CURRENT
        raise Conflict(busy_text(running), running.view() if running else None)
    op = Operation(kind, target, cancellable)
    CURRENT = op
    st.RUNNING.op = op
    try:
        yield op
    finally:
        CURRENT = None
        st.RUNNING.op = None
        DEPLOY_LOCK.release()
```

- [ ] **Step 4: Replace the 15 `with DEPLOY_LOCK:` sites**

Save this script as `patch_sites.py` in the session scratchpad and run it from the repo root (`python <scratchpad>/patch_sites.py`); it asserts that every old text occurs exactly once:

```python
from pathlib import Path

p = Path('stack_ui/server.py')
s = p.read_text(encoding='utf-8').replace('\r\n', '\n')

SITES = [
    ("    with DEPLOY_LOCK:\n        for r in all_images():\n",
     "    with operation('images-prune', f'{len(wanted)} images'):\n        for r in all_images():\n"),
    ("    with DEPLOY_LOCK:\n        pa.pull_image(ref)\n",
     "    with operation('image-pull', ref):\n        pa.pull_image(ref)\n"),
    ("    with DEPLOY_LOCK:\n        row = find_image(str(data.get('id') or ''))\n        pa.tag_image(",
     "    with operation('image-tag', str(data.get('id') or '')):\n        row = find_image(str(data.get('id') or ''))\n        pa.tag_image("),
    ("    with DEPLOY_LOCK:\n        rows = {r['id']: r for r in all_images()}\n",
     "    with operation('images-remove', f'{len(ids)} images'):\n        rows = {r['id']: r for r in all_images()}\n"),
    ("    with DEPLOY_LOCK:\n        row = find_image(str(data.get('id') or ''))\n        if row['usedBy']:\n",
     "    with operation('image-remove', str(data.get('id') or '')):\n        row = find_image(str(data.get('id') or ''))\n        if row['usedBy']:\n"),
    ("    with DEPLOY_LOCK:\n        row = find_container(name)\n",
     "    with operation('container', f'{name} {action}'):\n        row = find_container(name)\n"),
    ("    with DEPLOY_LOCK:\n        pa.create_network(",
     "    with operation('network', f'network {data.get(\"name\") or \"\"}'):\n        pa.create_network("),
    ("    with DEPLOY_LOCK:\n        row = next((r for r in network_list()['networks'] if r['name'] == name), None)\n",
     "    with operation('network', f'network {name}'):\n        row = next((r for r in network_list()['networks'] if r['name'] == name), None)\n"),
    ("    with DEPLOY_LOCK:\n        pa.create_volume(",
     "    with operation('volume', f'volume {data.get(\"name\") or \"\"}'):\n        pa.create_volume("),
    ("    with DEPLOY_LOCK:\n        rows = {r['name']: r for r in volume_list()['volumes']}\n",
     "    with operation('volume', f'{len(wanted)} volumes'):\n        rows = {r['name']: r for r in volume_list()['volumes']}\n"),
    ("    with DEPLOY_LOCK:\n        pa.create_secret(",
     "    with operation('secret', f'secret {name}'):\n        pa.create_secret("),
    ("    with DEPLOY_LOCK:\n        pa.remove_secret(name)\n",
     "    with operation('secret', f'secret {name}'):\n        pa.remove_secret(name)\n"),
    ("    with DEPLOY_LOCK:\n        try:\n            refuse_protected(name)\n",
     "    with operation('deploy', name, cancellable=True):\n        try:\n            refuse_protected(name)\n"),
    ("    with DEPLOY_LOCK:\n        live = deployed(name, require_podman_state())\n",
     "    with operation(f'stack-{action}', name, cancellable=True):\n        live = deployed(name, require_podman_state())\n"),
    ("    with DEPLOY_LOCK:\n        found = next((c for c in pa.containers() if st.cname(c) == wanted and st.belongs(c, name, names)), None)\n",
     "    with operation('restart', f'{wanted} of {name}'):\n        found = next((c for c in pa.containers() if st.cname(c) == wanted and st.belongs(c, name, names)), None)\n"),
]
for old, new in SITES:
    assert s.count(old) == 1, old[:90]
    s = s.replace(old, new)

old = "        except Busy as exc:\n            return self.json(429, {'error': str(exc)})\n"
assert s.count(old) == 1
s = s.replace(old, """        except Conflict as exc:
            return self.json(409, {'error': str(exc), **({'operation': exc.operation} if exc.operation else {})})
""" + old)
p.write_text(s, encoding='utf-8', newline='\n')
print('patched; left:', s.count('with DEPLOY_LOCK'))
```

Expected output: `patched; left: 0`. (The only remaining mentions of `DEPLOY_LOCK` are its definition, `operation()`, and the shutdown wait.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m unittest tests.test_operations tests.test_server tests.test_stack_delete tests.test_review_fixes tests.test_images tests.test_containers tests.test_resources tests.test_live_logs -v`
Expected: all `ok`. If a pre-existing test relied on a change waiting for another, it fails here: read it, and fix the test only if it asserted the waiting itself (none is known).

- [ ] **Step 6: Commit (only when the user says so)**

```bash
git add stack_ui/server.py tests/test_operations.py
git commit -m "Operations: the change lock never waits (409 with what runs); every change records what it is"
```

---

### Task 3: the operation routes and cancel

**Files:**
- Modify: `stack_ui/server.py` (`current_operation()`, `cancel_operation()` after `operation()`; two routes in `Handler.api`)
- Modify: `tests/test_operations.py` (route and cancel tests)

**Interfaces:**
- Consumes: `server.operation`, `server.Operation`, `server.Conflict`, `stacks.kill_tree` (Tasks 1-2).
- Produces:
  - `server.current_operation() -> dict | None` (the running operation's `view()`).
  - `server.cancel_operation() -> dict`: `{'cancelled': view}`; raises `Conflict('nothing is running')` or `Conflict('<text>: it is a quick change and cannot be cancelled', view)`.
  - `GET /api/operation` → `{"operation": null | {...view}}`; `POST /api/operation/cancel` → `{"cancelled": {...}}` or `409` `{"error": ..., "operation"?: ...}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_operations.py` (add the imports `http.client`, `json`, `os`, `time` and `from http.server import ThreadingHTTPServer` at the top of the file, next to the existing ones):

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_operations.OperationRoutesTest -v`
Expected: failures: `404`/`{'error': 'not found'}` for the two routes (`AssertionError: 404 != 200`), and `AttributeError` for `current_operation`.

- [ ] **Step 3: Implement**

In `stack_ui/server.py`, directly after the `operation()` function add:

```python
def current_operation() -> dict | None:
    op = CURRENT
    return op.view() if op is not None else None


def cancel_operation() -> dict:
    """End the running podman-compose command (and what it started); what it already created stays."""
    op = CURRENT
    if op is None:
        raise Conflict('nothing is running')
    if not op.cancellable:
        raise Conflict(f'{op.text()}: it is a quick change and cannot be cancelled', op.view())
    op.cancelled = True
    if op.proc is not None:            # not yet started: run_compose sees op.cancelled right after it starts
        st.kill_tree(op.proc)
    return {'cancelled': op.view()}
```

In `Handler.api`, directly above the line `            if method == 'GET' and parts == ['api', 'stacks']:` add:

```python
            if method == 'GET' and parts == ['api', 'operation']:
                return self.json(200, {'operation': current_operation()})
            if method == 'POST' and parts == ['api', 'operation', 'cancel']:
                return self.json(200, cancel_operation())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_operations tests.test_run_compose tests.test_live_logs tests.test_token -v`
Expected: all `ok` (the process-group assertions run on Linux; run the same command in WSL as in Task 5 to see them).

- [ ] **Step 5: Commit (only when the user says so)**

```bash
git add stack_ui/server.py tests/test_operations.py
git commit -m "Operations: GET /api/operation and POST /api/operation/cancel end a hung podman-compose command with its children"
```

---

### Task 4: the bar and the Cancel dialog on the page

**Files:**
- Modify: `stack_ui/page.html` (`#opbar` element, CSS, the operation block next to `renderBanner`, start of polling at the end of the script)
- Modify: `tests/test_page.py`
- Create (throwaway, not committed): `ui_demo_ops.py` in the session scratchpad

**Interfaces:**
- Consumes: `GET /api/operation`, `POST /api/operation/cancel` (Task 3); existing `api()`, `esc()`, `openDialog()`, `closeDialog()`, `toast()`, `errBox()`, `$()`.
- Produces: ids `opbar`, `op-cancel`, `op-do`; functions `renderOperation()`, `pollOperation()`, `showCancelOperation()`; state `OPERATION`.

- [ ] **Step 1: Write the failing page test**

In `tests/test_page.py`, after `test_the_logs_page_follows_the_log_instead_of_polling`, add:

```python
    def test_a_bar_shows_the_running_operation_with_cancel(self):
        with urllib.request.urlopen(self.base + '/', timeout=10) as r:
            page = r.read().decode('utf-8')
        for name in ('id="opbar"', 'op-cancel', 'pollOperation', 'showCancelOperation', '/api/operation/cancel'):
            self.assertIn(name, page)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m unittest tests.test_page.PageTest.test_a_bar_shows_the_running_operation_with_cancel -v`
Expected: FAIL (`'id="opbar"' not found in page`).

- [ ] **Step 3: Implement**

Save this as `patch_opbar.py` in the session scratchpad and run it from the repo root (`python <scratchpad>/patch_opbar.py`):

```python
from pathlib import Path

p = Path('stack_ui/page.html')
s = p.read_text(encoding='utf-8').replace('\r\n', '\n')


def swap(old, new):
    global s
    assert s.count(old) == 1, old[:80]
    s = s.replace(old, new)


swap('    <div id="banner"></div>\n    <div id="view"></div>',
     '    <div id="banner"></div>\n    <div id="opbar"></div>\n    <div id="view"></div>')

swap("  .banner { padding: 10px 14px; border-radius: 8px; margin-bottom: 14px; background: var(--bad-soft); color: var(--bad); }\n",
     "  .banner { padding: 10px 14px; border-radius: 8px; margin-bottom: 14px; background: var(--bad-soft); color: var(--bad); }\n"
     "  .opbar { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; padding: 9px 14px; border-radius: 8px;\n"
     "           margin-bottom: 14px; background: var(--warn-soft); color: var(--warn); }\n"
     "  .opbar button { margin-left: auto; }\n")

swap("function renderBanner() {", r"""// ---- the running change: shown on every page, with Cancel when it is a podman-compose command ----
let OPERATION = null;
function renderOperation() {
  const bar = $('opbar');
  if (!OPERATION) { bar.innerHTML = ''; return; }
  const text = OPERATION.text.charAt(0).toUpperCase() + OPERATION.text.slice(1);
  bar.innerHTML = `<div class="opbar" role="status"><span>${esc(text)}</span>${
    OPERATION.cancellable ? '<button class="small" id="op-cancel">Cancel…</button>' : ''}</div>`;
  $('op-cancel')?.addEventListener('click', showCancelOperation);
}
async function pollOperation() {
  if (document.hidden) return;
  try { OPERATION = (await api('/api/operation')).operation; } catch { OPERATION = null; }
  renderOperation();
}
function showCancelOperation() {
  const op = OPERATION;
  if (!op) return;
  openDialog('Cancel the running command?', `<p>${esc(op.text)}. The command is stopped; what it already created stays.
      Use Stop or Delete on the stack afterwards.</p><div id="dlg-err"></div>`,
  '<button data-close>Keep running</button><button class="danger" id="op-do">Cancel the command</button>');
  $('op-do').addEventListener('click', async () => {
    $('op-do').disabled = true;
    try { await api('/api/operation/cancel', { method: 'POST', body: '{}' }); closeDialog(); toast('Cancelled'); }
    catch (e) { $('dlg-err').innerHTML = errBox(e.message); $('op-do').disabled = false; }
    pollOperation();
  });
}
setInterval(pollOperation, 3000);
document.addEventListener('visibilitychange', pollOperation);

function renderBanner() {""")

swap("scheduleResources();\nrender();\nload();\n", "scheduleResources();\nrender();\nload();\npollOperation();\n")
p.write_text(s, encoding='utf-8', newline='\n')
print('page patched')
```

- [ ] **Step 4: Check the script's syntax and run the page tests**

Run, from the repo root:

```bash
python -c "import re, pathlib; h = pathlib.Path('stack_ui/page.html').read_text(encoding='utf-8'); js = re.search(r'<script>(.*)</script>', h, re.S).group(1).replace(\"'__UI_TOKEN__'\", \"''\").replace('__AUTH__', 'false').replace(\"'__SOURCE__'\", \"''\"); pathlib.Path('page-check.js').write_text(js, encoding='utf-8')"
node --check page-check.js && echo "script syntax ok"
rm page-check.js
python -m unittest tests.test_page -v
```

Expected: `script syntax ok`, then all page tests `ok`.

- [ ] **Step 5: Look at it in a real browser**

Save `ui_demo_ops.py` in the session scratchpad:

```python
"""Stack UI on a mock libpod with a stack 'web' whose podman-compose `up` hangs, for looking at the operation bar."""
import os
import sys
import tempfile
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(r'C:\Users\puszt\Downloads\Python\stack-ui')
sys.path.insert(0, str(ROOT))
tmp = Path(tempfile.mkdtemp())
(tmp / 'stacks' / 'web').mkdir(parents=True)
(tmp / 'stacks' / 'web' / 'compose.yaml').write_text(
    'services:\n  app:\n    image: docker.io/library/nginx:latest\n    container_name: web\n', encoding='utf-8')
os.environ['STACKS_DIR'] = str(tmp / 'stacks')
os.environ['STACK_UI_DATA'] = str(tmp / 'data')
os.environ['PODMAN_COMPOSE'] = f'"{sys.executable}" "{ROOT / "tests" / "fake_compose.py"}"'
os.environ['FAKE_COMPOSE_HANG'] = 'up:600'

from tests.mock_libpod import MockPodman  # noqa: E402
from stack_ui import podman_api as pa, server as srv, stacks as st  # noqa: E402

if os.environ.get('DEMO_OP_TIMEOUT'):          # a short time limit, to see the timeout error
    st.OP_TIMEOUT = int(os.environ['DEMO_OP_TIMEOUT'])
mock = MockPodman()
pa.API_URL = mock.start()
mock.add_container('web-db', project='web', state='Up 2 minutes (unhealthy)')
srv.Config.password_hash = ''
srv.Config.local_token = 'demo-token-fixed'
print('demo on http://127.0.0.1:18781/', flush=True)
ThreadingHTTPServer(('127.0.0.1', 18781), srv.Handler).serve_forever()
```

Start it in the background (`python ui_demo_ops.py`), open `http://127.0.0.1:18781/` in the built-in browser and check, taking a screenshot as you go:

1. No bar on a quiet page. In the console run `api('/api/stacks/web/deploy', { method: 'POST', body: JSON.stringify({ pull: false, remove: [] }) })` (do not await it): within 3 s the warning bar appears: "The deploy of web has been running for N seconds" with *Cancel…*.
2. In the console run `await api('/api/secrets', { method: 'POST', body: JSON.stringify({ name: 's', value: 'v' }) }).catch((e) => e.message)`: the text starts "Busy: the deploy of web has been running for" and ends "or cancel it on the page.".
3. Click *Cancel…*: the dialog names the command and says what stays; *Keep running* closes it and the bar stays; *Cancel the command* shows the toast "Cancelled" and within 3 s the bar is gone.
4. `await api('/api/stacks/web/stop', { method: 'POST', body: '{}' })` now answers (the stack has containers in the mock): the lock is free.
5. Stop the demo and start it again as `DEMO_OP_TIMEOUT=5 python ui_demo_ops.py` (a 5-second limit instead of 300), run the deploy from the console as in check 1 and wait: after 5 seconds it returns `ok: false` with "ERROR: podman-compose up did not finish in 5 seconds; not ready: web-db (unhealthy)" in its log, the bar disappears and the page is free again (Review Focus 5).
6. Narrow the window to 375 px (`resize_window` preset mobile): the bar wraps, *Cancel…* stays reachable. Reset with preset desktop.

Stop the demo afterwards.

- [ ] **Step 6: Commit (only when the user says so)**

```bash
git add stack_ui/page.html tests/test_page.py
git commit -m "Operations: a bar on every page shows the running change, with Cancel for a podman-compose command"
```

---

### Task 5: docs, the client's 409 and the full run

**Files:**
- Modify: `README.md` (Stacks bullet; configuration table)
- Modify: `CHANGELOG.md` (a new `## Unreleased` section above `## 1.0.19`)
- Modify: `tests/test_client.py` (a test that the client prints the 409 text)

**Interfaces:**
- Consumes: everything above.
- Produces: the user-facing description; a green full run on Windows and Linux.

- [ ] **Step 1: Write the failing client test**

In `tests/test_client.py`, in class `ClientTest`, after `test_new_folder_is_uploaded_then_created`, add:

```python
    def test_a_busy_page_gives_the_409_text(self):
        with self.ui.operation('deploy', 'web', cancellable=True):
            with self.assertRaises(sc.ClientError) as ctx:
                sc.call('POST', '/api/secrets', {'name': 's', 'value': 'v'}, timeout=10)
        self.assertIn('HTTP 409', str(ctx.exception))
        self.assertIn('Busy: the deploy of web has been running for', str(ctx.exception))
```

Run: `python -m unittest tests.test_client.ClientTest.test_a_busy_page_gives_the_409_text -v`
Expected: PASS already (the client prints the server's `error` for any HTTP error, and Tasks 2-3 made the server answer 409): this test pins that behaviour. If it fails, read how `call()` in `client/stack-ui-client.py` builds its message and fix the test's expectation, not the client.

- [ ] **Step 2: README**

Save as `patch_docs.py` in the session scratchpad and run from the repo root:

```python
from pathlib import Path


def swap_in(path, old, new):
    p = Path(path)
    s = p.read_text(encoding='utf-8').replace('\r\n', '\n')
    assert s.count(old) == 1, (path, old[:70])
    p.write_text(s.replace(old, new), encoding='utf-8', newline='\n')


swap_in('README.md', "update several stacks at once.",
        "update several stacks at once. One change runs at a time: a bar on every page shows it\n"
        "  and, for a `podman-compose` command, offers *Cancel* (the command and what it started are stopped; the containers\n"
        "  it already created stay); another change meanwhile is refused with a message that names the running one. A\n"
        "  command that does not finish in 5 minutes (`up`, `start`, `stop`, `down`; a pull gets 15) is ended, and the error\n"
        "  names the containers that are not healthy yet.")
swap_in('README.md', "| `STACK_UI_IGNORE` |",
        "| `STACK_UI_UP_TIMEOUT` | `300` | seconds a `podman-compose` `up` / `start` / `stop` / `down` may run before it is ended (at least 30) |\n"
        "| `STACK_UI_IGNORE` |")
swap_in('CHANGELOG.md', "# Changelog\n\n## 1.0.19\n", """# Changelog

## Unreleased

- *A change that hangs no longer freezes the page.* Only one change runs at a time, as before, but a second one is
  now refused at once with the running one named ("Busy: the deploy of crownsec has been running for 3 minutes ...")
  instead of waiting behind it without a word. A bar on every page shows what runs and for how long, and a
  `podman-compose` command (deploy, start, stop, delete) can be cancelled from it: the command and the processes it
  started are stopped, the containers it already created stay, and *Stop* or *Delete* work again. A command that does
  not finish in 5 minutes (`STACK_UI_UP_TIMEOUT`; a pull still 15) is ended, and the error names the containers
  that are not healthy yet. API: `GET /api/operation`, `POST /api/operation/cancel`; a refused change answers `409`.
  Quick changes (a secret, a network, a volume, an image, a container) also get the `409` while a deploy runs.

## 1.0.19
""")
print('docs updated')
```

- [ ] **Step 3: Run the whole suite on Windows**

Run: `python -m unittest discover -s tests`
Expected: `OK` (read the last lines of the output: `Ran N tests ... OK (skipped=...)`; the skips are the POSIX-only and Linux-only ones). Any failure is named in the output: fix it before going on.

- [ ] **Step 4: Run the whole suite on Linux (WSL) and check Python 3.9 syntax**

```bash
python -c "import ast, pathlib; [ast.parse(pathlib.Path(f).read_text(encoding='utf-8'), feature_version=(3, 9)) for f in ('stack_ui/stacks.py', 'stack_ui/server.py', 'tests/fake_compose.py', 'tests/test_run_compose.py', 'tests/test_operations.py', 'tests/test_client.py')]; print('3.9 syntax ok')"
```

Then run the suite in a fresh temporary copy under WSL without any `rm` (write the script into the session scratchpad and feed it to bash):

```bash
D=$(mktemp -d /tmp/sui-ops.XXXXXX)
cd /mnt/c/Users/puszt/Downloads/Python/stack-ui
tar -c --exclude=.git --exclude=__pycache__ --exclude=stacks --exclude=.superpowers . | tar -x -C "$D"
cd "$D"
python3 -m unittest discover -s tests 2>&1 | grep -E '^(Ran|OK|FAILED|FAIL:)|^ERROR: test' || true
echo "copy left in: $D"
```

Expected: `3.9 syntax ok`, then `Ran N tests` and `OK` with no skips for the POSIX process-group tests.

- [ ] **Step 5: Commit (only when the user says so)**

```bash
git add README.md CHANGELOG.md tests/test_client.py
git commit -m "Operations: README, CHANGELOG, the client's 409"
```

The release (rename `## Unreleased` to the version, bump `stack_ui/__init__.py` to 1.0.20, push, tag) is the user's call, as always.
