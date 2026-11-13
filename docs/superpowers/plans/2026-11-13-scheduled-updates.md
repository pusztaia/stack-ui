# Scheduled update check and automatic update: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The page asks the registries once a day by itself, updates the stacks switched to Auto-update, rolls back an update that did not come up, and shows the result on the page.

**Architecture:** A new module `stack_ui/schedule.py` owns the state file (`STACK_UI_DATA/schedule.json`), the due rule and the run loop; it never imports `server` and gets `check` and `update` as functions. `server.py` supplies those two (`scheduled_check`, `auto_update`, built from `updates`, `st.deploy`, `rollback.roll_back`), the routes and the thread start. `page.html` gets a Settings section, a per-stack switch, a list mark and an alert bar.

**Tech Stack:** Python 3.9+ standard library, PyYAML (already used), `unittest`, the repo's mock libpod and `fake_compose`.

**Spec:** `docs/superpowers/specs/2026-11-13-scheduled-updates-design.md`

## Global Constraints

- Standard library and PyYAML only; the code must import on Python 3.9 (`from __future__ import annotations`, no `match`).
- Tests never touch the real Pi, real Podman or a real registry: mock libpod + `tests/fake_compose.py`; registry answers are injected (`ui.updates` / `ui.REMOTE` patched).
- No commit, push or tag in this plan: those happen only on the user's word. No version bump either.
- Nothing from `stacks/` is touched or committed.
- English code, comments and page text; no notifications of any kind leave the host.
- The state file lives in the page's data folder only, mode 0600; nothing in a stack's folder is read for this feature.
- Run the suite with `python -m unittest discover -s tests > <file> 2>&1` and read the `Ran` / `OK` lines from the file (the summary is not at the end of a piped output). Linux run: PowerShell `wsl bash <scratchpad>/linux_suite.sh`.

## Review Focus

- A stopped server at the set time: the run happens after the start that day, once (Task 1 `test_due_*`).
- A container that starts and dies right after `up -d` returns 0: verify must catch it (Task 3 `test_verify_*`).
- A failed pull must not trigger a rollback to an old, unrelated point (Task 3 `test_a_failed_pull_changes_nothing`).
- The same failed image version must not be retried every night (Task 2 `test_a_failed_version_is_not_tried_again`).
- The API token of the deploy scripts must not switch automatic updates on (Task 4 `test_the_api_token_cannot_use_the_schedule`).

---

### Task 1: `schedule.py`: state file, due rule, settings

**Files:**
- Create: `stack_ui/schedule.py`
- Modify: `tests/support.py` (Base.setUp)
- Test: `tests/test_schedule.py`

**Interfaces:**
- Produces: `schedule.DATA`, `schedule.AUTOSTART`, `schedule.reset()`, `schedule.state() -> dict` (call with `schedule.LOCK` held), `schedule.store()`, `schedule.clean(raw) -> dict`, `schedule.due(now: datetime, state: dict) -> bool`, `schedule.configure(enabled, time, now) -> None` (ValueError on bad input), `schedule.set_auto(name: str, on: bool)`, `schedule.is_auto(name) -> bool`, `schedule.forget(name)`, `schedule.note(name) -> str`, `schedule.alerts() -> list[dict]`, `schedule.acknowledge()`, `schedule.view(now) -> dict`.
- State shape: `{'enabled': bool, 'time': 'HH:MM', 'lastRun': 'YYYY-MM-DD' | '', 'auto': [names], 'failed': {name: {'at': 'YYYY-MM-DD', 'digests': {ref: [digest]}}}, 'alerts': [{'stack','at','result','reason'}], 'runs': [run]}`.

- [ ] **Step 1: tests/support.py: every test gets its own schedule state and no thread**

In `Base.setUp`, after the two `rollback` lines:

```python
        from stack_ui import schedule
        self.patch(schedule, 'DATA', self.tmp / 'data')
        self.patch(schedule, 'AUTOSTART', False)
        schedule.reset()
        self.addCleanup(schedule.reset)
```

- [ ] **Step 2: Write the failing tests** (`tests/test_schedule.py`)

```python
import json
import os
import unittest
from datetime import datetime

from tests.support import Base
from stack_ui import schedule


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


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 3: Run to see them fail**

Run: `python -m unittest tests.test_schedule`
Expected: an ImportError / AttributeError (`stack_ui.schedule` does not exist).

- [ ] **Step 4: Write `stack_ui/schedule.py`** (this task's part; Task 2 adds the run)

```python
"""The daily update check and the automatic update of the stacks switched to it: the state (when, which stacks,
what failed, the last runs), the rule for "is it due", and the run. The state is kept in the page's own data
folder (STACK_UI_DATA/schedule.json), never in a stack's folder. This module does not know the server: the check
and the update of one stack are handed to it as functions.
See docs/superpowers/specs/2026-11-13-scheduled-updates-design.md."""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import stacks as st

DATA = Path(os.environ.get('STACK_UI_DATA') or '/data')     # the page's own folder (server.DATA)
FILE = 'schedule.json'
AUTOSTART = True                # the tests switch the thread off
MAX_STATE = 256 * 1024          # bytes of a state file that is still read
MAX_RUNS, MAX_LOG, MAX_LINE, MAX_ALERTS = 10, 40, 500, 50
TIME = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')
DAY = re.compile(r'^\d{4}-\d\d-\d\d$')
RESULTS = ('updated', 'skipped', 'failed', 'rolled-back', 'rollback-failed', 'cancelled')
LOCK = threading.RLock()        # the state
RUN_LOCK = threading.Lock()     # one run at a time
SAVE_ERROR = ''                 # why the state could not be written last time ('' when it could)
_state: dict | None = None


class AlreadyRunning(Exception):
    pass


def reset() -> None:
    """Forget the state in memory (the next use reads the file again)."""
    global _state, SAVE_ERROR
    with LOCK:
        _state, SAVE_ERROR = None, ''


def _name_ok(name) -> bool:
    return isinstance(name, str) and bool(st.STACK_NAME.match(name))


def _text(value, limit: int = MAX_LINE) -> str:
    return value[:limit] if isinstance(value, str) else ''


def _result(raw) -> dict | None:
    if not isinstance(raw, dict) or not _name_ok(raw.get('stack')) or raw.get('result') not in RESULTS:
        return None
    log = raw.get('log') if isinstance(raw.get('log'), list) else []
    return {'stack': raw['stack'], 'result': raw['result'], 'reason': _text(raw.get('reason')),
            'log': [_text(x) for x in log if isinstance(x, str)][-MAX_LOG:]}


def _run_record(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    results = raw.get('results') if isinstance(raw.get('results'), list) else []
    updates = raw.get('updates') if isinstance(raw.get('updates'), list) else []
    return {'started': _text(raw.get('started'), 40), 'finished': _text(raw.get('finished'), 40),
            'trigger': raw.get('trigger') if raw.get('trigger') in ('schedule', 'manual') else 'schedule',
            'checked': raw['checked'] if isinstance(raw.get('checked'), int) and not isinstance(raw['checked'], bool) else 0,
            'updates': [n for n in updates if _name_ok(n)], 'error': _text(raw.get('error')),
            'results': [r for r in map(_result, results) if r]}


def _failed(raw) -> dict | None:
    if not isinstance(raw, dict) or not isinstance(raw.get('digests'), dict):
        return None
    digests = {ref: [d for d in found if isinstance(d, str)] for ref, found in raw['digests'].items()
               if isinstance(ref, str) and isinstance(found, list)}
    day = raw['at'] if isinstance(raw.get('at'), str) and DAY.match(raw['at']) else ''
    return {'at': day, 'digests': digests} if digests else None


def clean(raw) -> dict:
    """A state of the right types from whatever was read: a wrong part becomes its default, the rest stays."""
    raw = raw if isinstance(raw, dict) else {}
    failed = raw.get('failed') if isinstance(raw.get('failed'), dict) else {}
    alerts = raw.get('alerts') if isinstance(raw.get('alerts'), list) else []
    runs = raw.get('runs') if isinstance(raw.get('runs'), list) else []
    return {
        'enabled': raw['enabled'] if isinstance(raw.get('enabled'), bool) else True,
        'time': raw['time'] if isinstance(raw.get('time'), str) and TIME.match(raw['time']) else '04:00',
        'lastRun': raw['lastRun'] if isinstance(raw.get('lastRun'), str) and DAY.match(raw['lastRun']) else '',
        'auto': sorted({n for n in raw['auto'] if _name_ok(n)}) if isinstance(raw.get('auto'), list) else [],
        'failed': {n: f for n, f in ((n, _failed(f)) for n, f in failed.items()) if _name_ok(n) and f},
        'alerts': [{'stack': a['stack'], 'at': _text(a.get('at'), 40), 'result': a['result'], 'reason': _text(a.get('reason'))}
                   for a in alerts if isinstance(a, dict) and _name_ok(a.get('stack')) and a.get('result') in RESULTS][-MAX_ALERTS:],
        'runs': [r for r in map(_run_record, runs) if r][:MAX_RUNS]}


def state() -> dict:
    """The state (read once; call with LOCK held, and store() after a change)."""
    global _state
    if _state is None:
        raw = None
        try:
            path = DATA / FILE
            if st.plain_file(path) and path.stat().st_size <= MAX_STATE:
                raw = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raw = None
        _state = clean(raw)
    return _state


def store() -> None:
    """Write the state (atomically, 0600). When it cannot be written it stays in memory and SAVE_ERROR says why:
    the daily run must not repeat every half minute because its day could not be kept."""
    global SAVE_ERROR
    try:
        path = DATA / FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f'.{FILE}.tmp')
        if tmp.is_symlink():
            tmp.unlink()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(_state, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        SAVE_ERROR = ''
    except OSError as exc:
        SAVE_ERROR = str(exc)


def due(now: datetime, s: dict) -> bool:
    """Switched on, no run yet on now's day, and the time has come (now: the local time)."""
    return bool(s['enabled']) and s['lastRun'] != now.date().isoformat() and now.strftime('%H:%M') >= s['time']


def configure(enabled, at_time, now: datetime) -> None:
    """Save the setting. It never starts a run by itself: a time that has already passed today counts from
    tomorrow (Run now is the way to run at once)."""
    if not isinstance(enabled, bool):
        raise ValueError('enabled must be true or false')
    if not isinstance(at_time, str) or not TIME.match(at_time):
        raise ValueError('the time must look like 04:00 (00:00 to 23:59)')
    with LOCK:
        s = state()
        s['enabled'], s['time'] = enabled, at_time
        if due(now, s):
            s['lastRun'] = now.date().isoformat()
        store()


def set_auto(name: str, on: bool) -> None:
    if not _name_ok(name):
        raise ValueError('not a stack name')
    with LOCK:
        s = state()
        s['auto'] = sorted((set(s['auto']) | {name}) if on else (set(s['auto']) - {name}))
        if not on:
            s['failed'].pop(name, None)
        store()


def is_auto(name: str) -> bool:
    with LOCK:
        return name in state()['auto']


def forget(name: str) -> None:
    """The stack is gone (its folder was deleted, or a new one of that name was uploaded): no switch, no memory."""
    with LOCK:
        s = state()
        if name in s['auto'] or name in s['failed']:
            s['auto'] = [n for n in s['auto'] if n != name]
            s['failed'].pop(name, None)
            store()


def note(name: str) -> str:
    """Why an update of the stack is not taken, when a version of it failed here."""
    with LOCK:
        known = state()['failed'].get(name)
    return f'the automatic update failed on {known["at"]}: waiting for a newer image' if known else ''


def alerts() -> list[dict]:
    with LOCK:
        return [dict(a) for a in state()['alerts']]


def acknowledge() -> None:
    with LOCK:
        state()['alerts'] = []
        store()


def view(now: datetime) -> dict:
    with LOCK:
        s = state()
        next_run = ''
        if s['enabled']:
            hour, minute = (int(x) for x in s['time'].split(':'))
            day = now.date() + timedelta(days=1 if s['lastRun'] == now.date().isoformat() else 0)
            next_run = f'{day.isoformat()}T{hour:02d}:{minute:02d}'
        return {'enabled': s['enabled'], 'time': s['time'], 'serverTime': now.strftime('%Y-%m-%dT%H:%M'),
                'lastRun': s['lastRun'], 'nextRun': next_run, 'running': RUN_LOCK.locked(), 'auto': list(s['auto']),
                'alerts': [dict(a) for a in s['alerts']], 'runs': json.loads(json.dumps(s['runs'])),
                'saveError': SAVE_ERROR}
```

(`sys`, `time`, `timezone` are used by Task 2.)

- [ ] **Step 5: Run the tests**

Run: `python -m unittest tests.test_schedule`
Expected: OK.

---

### Task 2: `schedule.py`: the run

**Files:**
- Modify: `stack_ui/schedule.py` (append)
- Test: `tests/test_schedule.py` (append a class before `if __name__`)

**Interfaces:**
- Consumes: Task 1.
- Produces: `schedule.run(check, update, trigger='schedule', now=None) -> dict` (the run record; raises `AlreadyRunning`), `schedule.start(check, update, trigger='manual') -> threading.Thread` (raises `AlreadyRunning`), `schedule.tick(check, update, now) -> bool`, `schedule.start_thread(check, update, wait=30.0) -> threading.Thread | None`.
- `check() -> {stack: {'update': bool, 'digests': {ref: [digest]}}}`; `update(name) -> {'result': one of RESULTS, 'reason': str, 'log': [str], 'alert': bool}`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run to see them fail**

Run: `python -m unittest tests.test_schedule.RunTest`
Expected: AttributeError: module 'stack_ui.schedule' has no attribute 'run' (and 'start', 'tick', 'start_thread').

- [ ] **Step 3: Append to `stack_ui/schedule.py`**

```python
def _stamp() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _same_version(known: dict, digests: dict) -> bool:
    """Every image that has an update now is at the digests that failed before."""
    return bool(digests) and all(sorted(found) == sorted(known.get(ref) or []) for ref, found in digests.items())


def _run(check, update, trigger: str, now: datetime) -> dict:
    record = {'started': _stamp(), 'finished': '', 'trigger': trigger, 'checked': 0, 'updates': [], 'error': '',
              'results': []}
    if trigger == 'schedule':
        with LOCK:              # before anything else: a run that dies is not started again today
            state()['lastRun'] = now.date().isoformat()
            store()
    try:
        found = check()
        record['checked'] = len(found)
        record['updates'] = sorted(n for n, f in found.items() if f.get('update'))
        with LOCK:
            wanted = [n for n in state()['auto'] if n in record['updates']]
        for name in wanted:
            digests = found[name].get('digests') or {}
            with LOCK:
                known = state()['failed'].get(name)
            if known and _same_version(known['digests'], digests):
                res = {'result': 'skipped', 'log': [], 'alert': False,
                       'reason': f'this image version failed here on {known["at"]}: waiting for a newer one'}
            else:
                try:
                    res = update(name)
                except Exception as exc:  # noqa: BLE001 - one stack must not end the run
                    res = {'result': 'failed', 'reason': f'{type(exc).__name__}: {exc}', 'log': [], 'alert': True}
            with LOCK:
                s = state()
                if res['result'] == 'updated':
                    s['failed'].pop(name, None)
                if res['result'] in ('rolled-back', 'rollback-failed') and digests:
                    s['failed'][name] = {'at': now.date().isoformat(), 'digests': digests}
                if res.get('alert'):
                    s['alerts'] = (s['alerts'] + [{'stack': name, 'at': _stamp(), 'result': res['result'],
                                                   'reason': _text(res.get('reason'))}])[-MAX_ALERTS:]
                store()
            record['results'].append({'stack': name, 'result': res['result'], 'reason': res.get('reason') or '',
                                      'log': list(res.get('log') or [])})
    except Exception as exc:  # noqa: BLE001 - recorded, the thread goes on
        record['error'] = f'{type(exc).__name__}: {exc}'
    record['finished'] = _stamp()
    kept = _run_record(record)
    with LOCK:
        s = state()
        s['runs'] = ([kept] + s['runs'])[:MAX_RUNS]
        store()
    return kept


def run(check, update, trigger: str = 'schedule', now: datetime | None = None) -> dict:
    """One run: the check, then the update of every stack switched to it that has a newer image. One at a time."""
    if not RUN_LOCK.acquire(blocking=False):
        raise AlreadyRunning('a scheduled run is already in progress')
    try:
        return _run(check, update, trigger, now or datetime.now())
    finally:
        RUN_LOCK.release()


def start(check, update, trigger: str = 'manual') -> threading.Thread:
    """A run in its own thread (Run now); AlreadyRunning when one is in progress."""
    if not RUN_LOCK.acquire(blocking=False):
        raise AlreadyRunning('a scheduled run is already in progress')

    def work():
        try:
            _run(check, update, trigger, datetime.now())
        finally:
            RUN_LOCK.release()
    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread


def tick(check, update, now: datetime) -> bool:
    """Start the day's run when it is due; True when one ran."""
    with LOCK:
        is_due = due(now, state())
    if not is_due:
        return False
    try:
        run(check, update, 'schedule', now)
    except AlreadyRunning:
        return False
    return True


def start_thread(check, update, wait: float = 30.0) -> threading.Thread | None:
    """The thread that looks at the clock every wait seconds (first after one wait)."""
    if not AUTOSTART:
        return None

    def loop():
        while True:
            time.sleep(wait)
            try:
                tick(check, update, datetime.now())
            except Exception as exc:  # noqa: BLE001
                print(f'scheduled check: {exc!r}', file=sys.stderr, flush=True)
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread
```

- [ ] **Step 4: Run the tests**

Run: `python -m unittest tests.test_schedule`
Expected: OK.

---

### Task 3: `server.py`: `scheduled_check`, `verify`, `auto_update`

**Files:**
- Modify: `stack_ui/server.py` (import, `Operation.TEXT`, new functions after `discard_rollback`, `run_rollback` uses the shared writer)
- Test: `tests/test_auto_update.py` (new)

**Interfaces:**
- Consumes: `schedule` (Task 1-2), `rollback.roll_back`, `rollback.load_record`, `rollback.exists`, `st.deploy`, `st.plan`, `st.deployed_diff`, `st.has_differences`, `st.project_containers`, `operation`, `Conflict`.
- Produces: `server.VERIFY_SETTLE = 15.0`, `server.VERIFY_WAIT = 120.0`, `server.VERIFY_STEP = 5.0`, `server.scheduled_check() -> dict` (the `check` of Task 2), `server.verify(name: str, was_running: set[str]) -> str` ('' when fine, else the reason), `server.auto_update(name: str) -> dict` (the `update` of Task 2), `server.rollback_writer(folder, compose_file, env_file)`.

- [ ] **Step 1: Write the failing tests** (`tests/test_auto_update.py`)

```python
import os
import threading
import unittest

from tests.test_rollback import REF, V1, V2, RollbackBase
from stack_ui import rollback, stacks as st


class AutoUpdateBase(RollbackBase):
    def setUp(self):
        super().setUp()
        self.patch(self.ui, 'VERIFY_SETTLE', 0.0)
        self.patch(self.ui, 'VERIFY_WAIT', 0.3)
        self.patch(self.ui, 'VERIFY_STEP', 0.05)

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


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run to see them fail**

Run: `python -m unittest tests.test_auto_update`
Expected: errors: `module 'stack_ui.server' has no attribute 'VERIFY_SETTLE'`.

- [ ] **Step 3: Implement in `stack_ui/server.py`**

a) Import: change `from . import rollback` to two lines `from . import rollback` and `from . import schedule`.

b) `Operation.TEXT`: add `'auto-update': 'the automatic update of {t}'`.

c) Extract the writer from `run_rollback` and use it there (replace the inner `put` and pass `rollback_writer(folder, compose_file, env_file)`):

```python
def rollback_writer(folder: Path, compose_file: str, env_file: str | None):
    """How a roll back writes a file of the stack: like a save on the page (history, the manifest)."""
    def put(path: Path, text: str) -> None:
        with FILES_LOCK:
            if path.name not in (compose_file, env_file):
                pin_manifest(folder, compose_file, env_file)
            write_file(path, text)
    return put
```

d) After `discard_rollback`:

```python
# ---------- the scheduled check and the automatic update (schedule.py runs them) ----------

VERIFY_SETTLE = 15.0    # seconds before the first look: a container that dies at once has died by then
VERIFY_WAIT = 120.0     # how long a health check may stay "starting" before the look that counts
VERIFY_STEP = 5.0


def scheduled_check() -> dict:
    """Ask the registries, and say per stack whether a newer image exists and at which digests."""
    found = updates(check=True)['stacks']
    out = {}
    for name, info in found.items():
        refs = sorted({i['image'] for i in info['images'] if i['status'] == 'update'})
        out[name] = {'update': info['status'] == 'update',
                     'digests': {ref: list(REMOTE.get(ref, ((), ''))[0]) for ref in refs}}
    return out


def verify(name: str, was_running: set[str]) -> str:
    """After an `up`: '' when every container that ran before runs, and none is unhealthy; else what is wrong.
    (podman-compose up -d returns 0 for a container that starts and dies.) A health check still starting is waited
    for; one that is still starting at the end counts as fine (a long start_period is not a failure)."""
    def cancelled() -> bool:
        op = CURRENT
        return op is not None and op.cancelled

    def nap(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end and not cancelled():
            time.sleep(min(0.5, max(0.0, end - time.monotonic())))

    nap(VERIFY_SETTLE)
    deadline = time.monotonic() + VERIFY_WAIT
    while True:
        if cancelled():
            return 'cancelled'
        rows = st.project_containers(name)
        health = {st.cname(c): str(c.get('Status') or '').lower() for c in rows}
        if not any('starting' in h for h in health.values()) or time.monotonic() >= deadline:
            break
        nap(VERIFY_STEP)
    running = {st.cname(c) for c in rows if c.get('State') == 'running'}
    problems = [f'{n} is not running' for n in sorted(was_running - running)]
    problems += [f'{n} is unhealthy' for n, h in sorted(health.items()) if 'unhealthy' in h]
    return '; '.join(problems)


def auto_update(name: str) -> dict:
    """The update of one stack by the schedule: {result, reason, log, alert}. Like Update on the page (pull,
    recreate, rollback point), but nothing that needs a decision is done, the result is verified, and an update
    that did not come up is rolled back. One change from start to end: nothing else runs in between."""
    log: list[str] = []

    def out(result: str, reason: str = '', alert: bool = False) -> dict:
        return {'result': result, 'reason': reason, 'log': log, 'alert': alert}

    try:
        folder, compose_file, env_file = find_module(name)
    except LookupError:
        return out('skipped', 'no folder of that name any more')
    try:
        with operation('auto-update', name, cancellable=True) as op:
            snap = require_podman_state()
            if stack_protected(name):
                return out('skipped', 'protected (this page itself, or STACK_UI_PROTECTED)')
            if not deployed(name, snap):
                return out('skipped', 'not deployed')
            d = st.deployed_diff(folder, compose_file, env_file, SOURCE)
            if d is None:
                return out('skipped', 'no record of its last deploy: update it on the page once')
            if st.has_differences(d):
                return out('skipped', 'its files differ from the last deploy: update it on the page')
            p = st.plan(folder, compose_file, env_file)
            needs = (p['errors'] + [f'missing secret {s}' for s in p['missing_secrets']]
                     + [f'{st.cname(c)} is in its way' for c in p['blocking']])
            if needs:
                return out('skipped', 'it needs a decision on the page: ' + '; '.join(needs))
            was_running = {st.cname(c) for c in st.project_containers(name, snap['containers'])
                           if c.get('State') == 'running'}
            finished = False
            try:
                st.deploy(folder, compose_file, env_file, pull=True, approve=lambda c: False, log=log.append)
                finished = True
                problem = verify(name, was_running)
            except (st.StackError, pa.PodmanError, ValueError) as exc:
                problem = str(exc)
            if not problem:
                return out('updated')
            log.append(f'ERROR: {problem}')
            if op.cancelled:
                return out('cancelled', problem, alert=True)
            point = rollback.load_record(folder)
            if point is None or not (finished or point.get('pending')):
                # the pull failed (nothing was changed), or there is nothing to go back to
                return out('failed', problem, alert=finished)
            try:
                rollback.roll_back(folder, compose_file, env_file, rollback_writer(folder, compose_file, env_file),
                                   log.append)
                again = verify(name, was_running)
            except (st.StackError, pa.PodmanError, ValueError) as exc:
                again = str(exc)
            if again:
                log.append(f'ERROR: roll back: {again}')
                return out('rollback-failed', f'{problem}; the roll back failed too: {again}', alert=True)
            log.append(f'Stack "{name}" rolled back.')
            return out('rolled-back', problem, alert=True)
    except Conflict as exc:
        return out('skipped', str(exc))
    except (st.StackError, pa.PodmanError, ValueError, OSError) as exc:
        return out('failed', str(exc))
```

- [ ] **Step 4: Run the tests**

Run: `python -m unittest tests.test_auto_update tests.test_rollback tests.test_rollback_server`
Expected: OK. If `test_skipped_when_the_plan_needs_a_decision` fails because the plan does not see `web2` as blocking, read `st.plan` and make the test's foreign container match its rule (a container of that name without the project's label); the behaviour under test is "blocking -> skipped".

---

### Task 4: routes, overview, the poll answer, clean-up, the thread

**Files:**
- Modify: `stack_ui/server.py` (`overview`, `stack_action`, `upload`, `Handler.dispatch`, `Handler.api`, `main`)
- Test: `tests/test_schedule_server.py` (new)

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: `GET /api/schedule`, `POST /api/schedule`, `POST /api/schedule/run`, `POST /api/schedule/ack`, `POST /api/stacks/<name>/auto`; overview rows `auto: bool`, `autoNote: str`; `GET /api/operation` -> `{operation, alerts, scheduleRunning}`; `server.schedule_view()`, `server.schedule_save(data)`, `server.schedule_run()`, `server.schedule_ack()`, `server.stack_auto(name, data)`.

- [ ] **Step 1: Write the failing tests** (`tests/test_schedule_server.py`)

```python
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
                             ('POST', '/api/schedule/ack'), ('POST', '/api/stacks/web/auto')):
            self.assertEqual(self.call(method, path, {'auto': True}, bearer)[0], 403, path)
        self.assertEqual(self.call('GET', '/api/stacks', None, bearer)[0], 200)
        self.assertFalse(schedule.is_auto('web'))


if __name__ == '__main__':
    unittest.main()
```

(`api_token` writes `SETTINGS_FILE`; if the reloaded module's `SETTINGS_FILE` is not under the test's temp folder, patch it in `setUp`: `self.patch(self.ui, 'SETTINGS_FILE', self.tmp / 'data' / 'settings.json')`. Check how `tests/test_token.py` does it and do the same.)

- [ ] **Step 2: Run to see them fail**

Run: `python -m unittest tests.test_schedule_server`
Expected: failures: 404 on `/api/schedule`, `KeyError: 'auto'` in the overview.

- [ ] **Step 3: Implement in `stack_ui/server.py`**

a) `overview()`: in the row dict, after `'rollback': rollback.exists(folder),`:

```python
                     'auto': schedule.is_auto(name), 'autoNote': schedule.note(name),
```

b) `stack_action`: after `rollback.discard(folder)` add `schedule.forget(name)`. `upload`: after `rollback.discard(module)` add `schedule.forget(name)`.

c) After `auto_update`:

```python
def schedule_view() -> dict:
    return schedule.view(datetime.now())


def schedule_save(data: dict) -> dict:
    schedule.configure(data.get('enabled'), data.get('time'), datetime.now())
    return schedule_view()


def schedule_run() -> dict:
    try:
        schedule.start(scheduled_check, auto_update)
    except schedule.AlreadyRunning as exc:
        raise Conflict(str(exc)) from None
    return {'started': True}


def schedule_ack() -> dict:
    schedule.acknowledge()
    return {'alerts': []}


def stack_auto(name: str, data: dict) -> dict:
    find_module(name)
    on = data.get('auto')
    if not isinstance(on, bool):
        raise ValueError('auto must be true or false')
    if on and stack_protected(name):
        raise ValueError(f'"{name}" is protected (this page itself, or STACK_UI_PROTECTED): it is not updated from here')
    schedule.set_auto(name, on)
    return {'auto': on}
```

(`schedule_run` passes the module's `scheduled_check` by name lookup at call time, so the test's patch is used.)

d) `Handler.dispatch`, the bearer branch: extend the refusal:

```python
            if (path == '/api/settings' or path.startswith('/api/settings/') or path == '/api/schedule'
                    or path.startswith('/api/schedule/') or (path.startswith('/api/stacks/') and path.endswith('/auto'))):
                return self.json(403, {'error': 'the API token cannot read or change the settings'})
```

e) `Handler.api`: replace the `GET /api/operation` line and add the routes after it:

```python
            if method == 'GET' and parts == ['api', 'operation']:
                return self.json(200, {'operation': current_operation(), 'alerts': schedule.alerts(),
                                       'scheduleRunning': schedule.RUN_LOCK.locked()})
            if method == 'GET' and parts == ['api', 'schedule']:
                return self.json(200, schedule_view())
            if method == 'POST' and parts == ['api', 'schedule']:
                return self.json(200, schedule_save(data))
            if method == 'POST' and parts == ['api', 'schedule', 'run']:
                return self.json(200, schedule_run())
            if method == 'POST' and parts == ['api', 'schedule', 'ack']:
                return self.json(200, schedule_ack())
```

and in the `len(parts) == 4 and parts[1] == 'stacks'` block, before `return self.json(404...)` falls through:

```python
                if method == 'POST' and action == 'auto':
                    return self.json(200, stack_auto(name, data))
```

f) `main()`: right after the `print(f'Stack UI {__version__}: ...` line:

```python
    schedule.start_thread(scheduled_check, auto_update)
```

- [ ] **Step 4: Run the tests**

Run: `python -m unittest tests.test_schedule_server tests.test_operations tests.test_server tests.test_token tests.test_shutdown`
Expected: OK (a test that compares the whole `/api/operation` answer with `{'operation': ...}` must be updated to the new keys: that is the changed contract, not a regression).

---

### Task 5: the page

**Files:**
- Modify: `stack_ui/page.html`
- Test: `tests/test_page.py`

**Interfaces:**
- Consumes: the routes of Task 4.

- [ ] **Step 1: Write the failing test** (in `PageTest`)

```python
    def test_the_scheduled_check_the_switch_and_the_alert_bar(self):
        with urllib.request.urlopen(self.base + '/', timeout=10) as r:
            page = r.read().decode('utf-8')
        for name in ('id="alertbar"', 'function renderAlerts', '/api/schedule/ack', '/api/schedule/run',
                     'Scheduled check', 'id="sch-time"', 'id="sch-on"', 'id="sch-run"', 'function loadSchedule',
                     'id="auto-sel"', '/auto', 's.autoNote', 'Automatic update', 'scheduleRunning',
                     'rolled back', 'needs you'):
            self.assertIn(name, page, name)
```

- [ ] **Step 2: Run to see it fail**

Run: `python -m unittest tests.test_page`
Expected: FAIL: `'id="alertbar"' not found`.

- [ ] **Step 3: Implement**

a) HTML: after `<div id="banner"></div>` add `<div id="alertbar"></div>`.

b) CSS, after the `.opbar button` rule:

```css
  .alertbar { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; padding: 9px 14px; border-radius: var(--r);
              margin-bottom: 14px; background: var(--warn-soft); color: var(--warn); box-shadow: inset 3px 0 0 var(--warn); }
  .alertbar.bad { background: var(--bad-soft); color: var(--bad); box-shadow: inset 3px 0 0 var(--bad); }
  .alertbar ul { margin: 0; padding-left: 18px; flex: 1 1 320px; }
  .runs .tr { grid-template-columns: 170px 90px 1fr; }
```

c) JS, replace `pollOperation` and add the alert code before it:

```js
// ---- what the schedule did while nobody looked: shown on every page until dismissed ----
let ALERTS = [];
let SCHEDULE_RUNNING = false;
function alertText(a) {
  const when = a.at ? ` (${new Date(a.at).toLocaleString()})` : '';
  const what = { 'rolled-back': 'failed and was rolled back', 'rollback-failed': 'failed and the roll back failed too: the stack needs you',
    cancelled: 'was cancelled: check the stack', failed: 'failed' }[a.result] || a.result;
  return `Automatic update of ${a.stack} ${what}${when}${a.reason ? `: ${a.reason}` : ''}`;
}
function renderAlerts() {
  const bar = $('alertbar');
  const key = JSON.stringify(ALERTS);
  if (bar.dataset.key === key) return;            // unchanged: keep the buttons (a rebuilt one loses a click)
  bar.dataset.key = key;
  if (!ALERTS.length) { bar.innerHTML = ''; return; }
  const bad = ALERTS.some((a) => a.result === 'rollback-failed');
  bar.innerHTML = `<div class="alertbar${bad ? ' bad' : ''}" role="alert"><ul>${ALERTS.map((a) => `<li>${esc(alertText(a))}</li>`).join('')}</ul>
    <a class="small" href="#/settings">Details</a><button class="small" id="alert-ack">Dismiss</button></div>`;
  $('alert-ack').addEventListener('click', async () => {
    try { ALERTS = (await api('/api/schedule/ack', { method: 'POST', body: '{}' })).alerts; renderAlerts(); }
    catch (e) { toast(e.message, 'bad'); }
  });
}
async function pollOperation() {
  if (document.hidden) return;
  try {
    const r = await api('/api/operation');
    OPERATION = r.operation; ALERTS = r.alerts || [];
    const was = SCHEDULE_RUNNING;
    SCHEDULE_RUNNING = !!r.scheduleRunning;
    if (was && !SCHEDULE_RUNNING) { load(); if (ROUTE.view === 'settings') loadSchedule(); }   // a run ended: new data
  } catch { OPERATION = null; }
  renderOperation();
  renderAlerts();
}
```

(If the Settings route is not `#/settings`, use the href the nav link `[data-nav="settings"]` has.)

d) Stack list: in `imageCell(name)`, compute a mark first and append it to every returned string:

```js
  const s0 = stackOf(name);
  const auto = s0?.auto ? `<span class="tag ${s0.autoNote ? 't-warn' : 't-idle'}" title="${esc(s0.autoNote || 'Updated automatically after the daily check')}">auto</span>` : '';
```

Each `return \`...\`` of `imageCell` becomes `return \`...\` + auto;` (the `Not checked`, `—`, `Update available`, `Up to date`, `Built here`, `Check failed` returns). Note the variable must not be named `s.autoNote` only inside a template: the test looks for the text `s.autoNote`, which the Summary tab code below contains.

e) Summary tab (`tabSummary`): in the Details `<dl>`, after the `Last image check` row:

```js
        <dt>Automatic update</dt><dd>${s.protected ? '<span class="muted small">Not for a protected stack</span>'
          : `<select id="auto-sel" aria-label="Automatic update"><option value="off" ${s.auto ? '' : 'selected'}>Off</option><option value="on" ${s.auto ? 'selected' : ''}>On</option></select>
             <span class="muted small">${s.autoNote ? esc(s.autoNote.charAt(0).toUpperCase() + s.autoNote.slice(1)) + '.'
               : 'After the daily check a newer image is pulled and the containers are recreated; an update that does not come up is rolled back. Held back while the files differ from the last deploy.'}</span>`}</dd>
```

and after the `$('tab').innerHTML = ...;` assignment:

```js
  $('auto-sel')?.addEventListener('change', async (e) => {
    const on = e.target.value === 'on';
    try {
      await api(`/api/stacks/${enc(name)}/auto`, { method: 'POST', body: JSON.stringify({ auto: on }) });
      toast(on ? `${name}: updated automatically from now on` : `${name}: automatic update off`);
    } catch (err) { toast(err.message, 'bad'); }
    load();
  });
```

(`refreshViews` redraws the Summary tab on every `load()`; the select is rebuilt from `s.auto`, which is the saved value by then.)

f) Settings: in `viewSettings`, add a section after the API token section:

```js
    <section class="panel" style="max-width:820px">
      <h2>Scheduled check</h2>
      <div id="sch"><p class="muted">Loading…</p></div>
    </section>
```

call `loadSchedule();` at the end of `viewSettings`, and add next to it:

```js
const RESULT_TAG = { updated: 't-ok', skipped: 't-idle', failed: 't-warn', 'rolled-back': 't-warn', 'rollback-failed': 't-bad', cancelled: 't-warn' };
async function loadSchedule() {
  if (!$('sch')) return;
  let sc;
  try { sc = await api('/api/schedule'); }
  catch (e) { if ($('sch')) $('sch').innerHTML = errBox(e.message); return; }
  if (!$('sch')) return;
  const when = (t) => t ? esc(new Date(t).toLocaleString()) : '—';
  const runs = sc.runs.map((r, i) => `<div class="tr"><span class="small">${when(r.started)}</span>
      <span class="small">${r.trigger === 'manual' ? 'Run now' : 'Scheduled'}</span>
      <span class="small">${r.error ? `<span class="tag t-bad">Failed</span> ${esc(r.error)}`
        : `${plural(r.checked, 'stack')} checked, ${r.updates.length ? `newer image: ${esc(r.updates.join(', '))}` : 'no newer image'}`}
        ${r.results.map((x, j) => `<div><span class="tag ${RESULT_TAG[x.result] || 't-idle'}">${esc(x.result)}</span> <b>${esc(x.stack)}</b>
          ${esc(x.reason)} ${x.log.length ? `<button class="small" data-run="${i}" data-res="${j}">Log</button>` : ''}</div>`).join('')}</span></div>`).join('');
  $('sch').innerHTML = `
    <div id="sch-msg">${sc.saveError ? `<div class="bad-note">The setting could not be saved (${esc(sc.saveError)}): it is kept until the page restarts.</div>` : ''}</div>
    <p class="muted small">Once a day the registries are asked whether a newer image exists (as <i>Check for updates</i> does). Stacks whose
      <i>Automatic update</i> is on (the stack's Summary tab) are then updated; an update that does not come up is rolled back.
      Switched on now: ${sc.auto.length ? esc(sc.auto.join(', ')) : 'no stack'}.</p>
    <label class="field"><span>Daily check</span><select id="sch-on"><option value="on" ${sc.enabled ? 'selected' : ''}>On</option><option value="off" ${sc.enabled ? '' : 'selected'}>Off</option></select></label>
    <label class="field"><span>At (the server's clock, now ${esc(sc.serverTime.slice(11))})</span><input type="time" id="sch-time" value="${esc(sc.time)}" required></label>
    <dl class="kv"><dt>Last scheduled run</dt><dd>${sc.lastRun ? esc(sc.lastRun) : 'Not yet'}</dd>
      <dt>Next run</dt><dd>${sc.running ? 'Running now…' : sc.nextRun ? when(sc.nextRun) : 'Switched off'}</dd></dl>
    <div class="head-actions" style="justify-content:flex-end"><button id="sch-run" ${sc.running ? 'disabled' : ''}>Run now</button><button class="primary" id="sch-save">Save</button></div>
    <h4>Last runs</h4>
    ${sc.runs.length ? `<div class="table runs">${runs}</div>` : '<p class="muted small">No run yet.</p>'}`;
  $('sch-save').addEventListener('click', async () => {
    try {
      await api('/api/schedule', { method: 'POST', body: JSON.stringify({ enabled: $('sch-on').value === 'on', time: $('sch-time').value }) });
      toast('Saved');
      loadSchedule();
    } catch (e) { $('sch-msg').innerHTML = `<div class="bad-note">${esc(e.message)}</div>`; }
  });
  $('sch-run').addEventListener('click', async () => {
    $('sch-run').disabled = true;
    try { await api('/api/schedule/run', { method: 'POST', body: '{}' }); toast('The check is running'); pollOperation(); }
    catch (e) { $('sch-msg').innerHTML = `<div class="bad-note">${esc(e.message)}</div>`; $('sch-run').disabled = false; }
  });
  $('sch').querySelectorAll('[data-run]').forEach((b) => b.addEventListener('click', () => {
    const x = sc.runs[Number(b.dataset.run)].results[Number(b.dataset.res)];
    openDialog(`${x.stack}: ${x.result}`, `<pre>${esc(x.log.join('\n'))}</pre>`);
  }));
}
```

(`t-bad`, `plural`, `errBox`, `.field`, `.kv`, `.table` exist; if `.field` does not style an `<input type="time">` like a select, add `input[type=time]` to the selector that styles `.field select`.)

- [ ] **Step 4: Run the page tests**

Run: `python -m unittest tests.test_page`
Expected: OK.

- [ ] **Step 5: Look at it in the browser with a mock demo**

Copy the scratchpad's `ui_demo_rollback.py` to `ui_demo_schedule.py` (port 18783), patch `ui.scheduled_check` to return one stack with an update and `ui.REMOTE` accordingly, start it through the Browser pane, and check: Settings section loads, Save with a bad time shows the error, the Summary switch saves and survives a reload, the "auto" mark shows in the list, Run now updates the demo stack and the run appears under Last runs, an injected alert shows the bar on every page and Dismiss removes it; dark and light theme, 375 px width without horizontal scroll, no console errors.

---

### Task 6: documentation and the whole suite

**Files:**
- Modify: `README.md`, `CHANGELOG.md`, `docs/superpowers/specs/2026-11-13-scheduled-updates-design.md`

- [ ] **Step 1: README**: extend the *Image updates* bullet:

```markdown
- **Image updates**: asks the registries (anonymous HEAD requests) whether a newer image exists for each running
  tag and marks the stacks. Once a day (04:00 by the server's clock unless changed on *Settings*; set `TZ`) it
  asks by itself. A stack whose *Automatic update* is switched on (its Summary tab) is then updated like *Update*
  does; an update whose containers do not come up is rolled back, that image version is not tried again, and a
  bar on every page says so. Nothing that needs a decision is done automatically (files that differ from the
  last deploy, a container in the way, the page's own stack), and nothing is sent anywhere. Switch it on only for
  images you trust: the page does not verify signatures.
```

and in the `STACK_UI_DATA` row add "the schedule's state" next to the rollback points.

- [ ] **Step 2: CHANGELOG**: a new `## Unreleased` section on top:

```markdown
## Unreleased

- *Scheduled update check and automatic update.* The registries are asked once a day (Settings: on/off, the time,
  *Run now*, the last runs). Per stack an *Automatic update* switch (Summary tab, off by default): after the check
  a newer image is pulled and the containers are recreated, then watched (what ran before must run, nothing may be
  unhealthy); an update that does not come up is rolled back to its rollback point, the failed image version is
  not tried again, and a bar on every page reports it until dismissed. Held back, with the reason in the run's
  record: files that differ from the last deploy, a plan that needs a decision, a protected stack, another change
  in progress. No notification leaves the host. The API token of the scripts cannot read or change the schedule.
```

- [ ] **Step 3: Spec**: bring it in line with what was built: `failed[stack]` is `{at, digests}`; verify waits 15 s first and treats "still starting after 120 s" as fine; the API token is refused on the schedule routes; a manual run does not count as the day's run.

- [ ] **Step 4: The whole suite, Windows**

Run: `python -m unittest discover -s tests > "$TEMP/suite_sched.txt" 2>&1; grep -E "^(Ran |OK|FAILED)" "$TEMP/suite_sched.txt"`
Expected: `Ran N tests` (N >= 386 + the new ones) and `OK (skipped=16)`.

- [ ] **Step 5: The whole suite, Linux (WSL)**

Run (PowerShell): `wsl bash /mnt/c/Users/puszt/AppData/Local/Temp/claude/0b11014dacc2ea65/d041bd3b-cd9d-4cd3-8a27-afae25c9a484/scratchpad/linux_suite.sh`
Expected: the same count, `OK`, nothing skipped.

No commit: the user says when.
