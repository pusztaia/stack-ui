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
SHA256 = re.compile(r'^[0-9a-f]{64}$')
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
    seen = raw.get('deployed') if isinstance(raw.get('deployed'), dict) else {}
    return {
        'deployed': {n: {f: h for f, h in files.items() if isinstance(f, str) and isinstance(h, str) and SHA256.match(h)}
                     for n, files in seen.items() if _name_ok(n) and isinstance(files, dict)},
        'enabled': raw['enabled'] if isinstance(raw.get('enabled'), bool) else True,
        'cleanup': raw['cleanup'] if isinstance(raw.get('cleanup'), bool) else True,
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


def cleanup_on() -> bool:
    """An update removes the images only the replaced rollback point kept (rollback.release)."""
    with LOCK:
        return state()['cleanup']


def configure(enabled, at_time, now: datetime, cleanup=None) -> None:
    """Save the setting. It never starts a run by itself: a time that has already passed today counts from
    tomorrow (Run now is the way to run at once). cleanup: None leaves it as it is."""
    if not isinstance(enabled, bool):
        raise ValueError('enabled must be true or false')
    if cleanup is not None and not isinstance(cleanup, bool):
        raise ValueError('cleanup must be true or false')
    if not isinstance(at_time, str) or not TIME.match(at_time):
        raise ValueError('the time must look like 04:00 (00:00 to 23:59)')
    with LOCK:
        s = state()
        s['enabled'], s['time'] = enabled, at_time
        if cleanup is not None:
            s['cleanup'] = cleanup
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
        if name in s['auto'] or name in s['failed'] or name in s['deployed']:
            s['deployed'].pop(name, None)
            s['auto'] = [n for n in s['auto'] if n != name]
            s['failed'].pop(name, None)
            store()


def fingerprint(folder: Path, compose_file: str, env_file: str | None) -> dict | None:
    """{file name: SHA-256} of the stack's files (compose, env, the others), or None when one of them is not a
    regular file of a size a stack file may have (a link, a FIFO, too large): such a folder is never approved."""
    return st.hashes(st.snapshot(folder, compose_file, env_file))


def approve_hashes(name: str, found: dict | None) -> None:
    """A deploy from the page (a person's decision) ran with the files of these hashes: remember them. Kept here,
    in the page's data folder, because a container that mounts its stack's folder can write everything in it."""
    with LOCK:
        s = state()
        if found is None:
            s['deployed'].pop(name, None)
        else:
            s['deployed'][name] = dict(found)
        store()


def approve(folder: Path, compose_file: str, env_file: str | None) -> None:
    approve_hashes(folder.name, fingerprint(folder, compose_file, env_file))


def matches(name: str, found: dict | None) -> bool:
    """These hashes are the ones of the stack's last deploy from the page."""
    with LOCK:
        return found is not None and state()['deployed'].get(name) == found


def approved(folder: Path, compose_file: str, env_file: str | None, name: str | None = None) -> bool:
    """The folder holds exactly the files of the last deploy from the page (name: the stack, when folder is not
    its own folder)."""
    return matches(name or folder.name, fingerprint(folder, compose_file, env_file))


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
            if due(now, s):         # the thread starts it within its next look
                next_run = now.strftime('%Y-%m-%dT%H:%M')
        return {'enabled': s['enabled'], 'cleanup': s['cleanup'], 'time': s['time'], 'serverTime': now.strftime('%Y-%m-%dT%H:%M'),
                'lastRun': s['lastRun'], 'nextRun': next_run, 'running': RUN_LOCK.locked(), 'auto': list(s['auto']),
                'alerts': [dict(a) for a in s['alerts']], 'runs': json.loads(json.dumps(s['runs'])),
                'saveError': SAVE_ERROR}


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
