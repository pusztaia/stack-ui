"""The Activity log: what happened on the host, newest first.

Two kinds of entries, one JSON object a line in STACK_UI_DATA/activity.jsonl (the page's own folder, which no stack
mounts), cut back to the newest KEEP:
- change: every change that held the change lock (core.operation), written when it ends: what, on what, its stack,
  the result (ok, failed, cancelled, or what the change noted: skipped, updated, rolled-back, ...), how long it took,
  and where it came from (page: signed in on the page; script: the API token; schedule: no request, the daily check);
- warning: what the watcher saw happen to the containers, also while no page is open: a restart loop starting and
  ending, an out-of-memory kill, memory near the limit starting and ending. Written when it starts and when it ends,
  not at every look; what it saw is kept in STACK_UI_DATA/activity-watch.json across a restart of the page.

Writing never fails a change: a folder that cannot be written loses the entry, and says so on stderr."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import health as hl
from . import podman_api as pa
from . import stacks as st

FILE, WATCH_FILE = 'activity.jsonl', 'activity-watch.json'
KEEP = 2000                # entries kept
TRIM_BYTES = 1_000_000     # the file is cut back to KEEP entries when it grows past this (an entry is ~250 bytes)
READ_MAX = 4 * 1024 * 1024
MESSAGE_MAX = 500
AUTOSTART = True           # the tests switch the watcher thread off
WATCH_EVERY = 60.0         # seconds between two looks
SHOW = ('all', 'changes', 'warnings', 'failed', 'sign-ins')
NOT_OK = {'failed', 'cancelled', 'rolled-back', 'rollback-failed'}
STACK_KINDS = {'deploy', 'rollback', 'rollback-discard', 'auto-update', 'stack-start', 'stack-stop', 'stack-delete'}
PROJECT_LABELS = ('io.podman.compose.project', 'com.docker.compose.project')

LOCK = threading.Lock()
ACTOR = threading.local()  # .source while a request runs: 'page' or 'script' (server.Handler.api)
_WATCH: dict | None = None  # what the watcher saw last (loaded from WATCH_FILE once)


def path() -> Path:
    return st.DATA / FILE


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def source() -> str:
    return getattr(ACTOR, 'source', '') or 'schedule'


def stack_of(kind: str, target: str) -> str:
    if kind in STACK_KINDS:
        return target
    if kind == 'restart' and ' of ' in target:      # "<container> of <stack>"
        return target.rsplit(' of ', 1)[1]
    return ''


def record(entry: dict) -> None:
    row = {'at': _now(), 'type': 'change', 'kind': '', 'target': '', 'stack': '', 'result': '', 'message': '',
           'seconds': None, 'source': ''}
    row.update(entry)
    row['message'] = str(row['message'] or '')[:MESSAGE_MAX]
    line = json.dumps(row, ensure_ascii=False) + '\n'
    try:
        with LOCK:
            st.DATA.mkdir(parents=True, exist_ok=True)
            with open(path(), 'a', encoding='utf-8', newline='\n') as f:
                f.write(line)
            if path().stat().st_size > TRIM_BYTES:
                _trim()
    except OSError as exc:
        print(f'activity: not written ({exc})', file=sys.stderr, flush=True)


def _trim() -> None:
    lines = path().read_text(encoding='utf-8').splitlines(keepends=True)[-KEEP:]
    tmp = path().with_name(FILE + '.new')
    st.write_lf(tmp, ''.join(lines))
    os.replace(tmp, path())


def sign_in(client: str, result: str, message: str = '') -> None:
    """A sign-in to the page: ok, failed or throttled, and from where (never what was typed)."""
    record({'type': 'login', 'kind': 'sign-in', 'target': client, 'result': result, 'message': message,
            'source': 'page'})


def change(op, exc: BaseException | None) -> None:
    """The entry of a change that held the lock (core.operation): op is its core.Operation, exc what it raised."""
    if exc is not None:
        result, message = ('cancelled' if op.cancelled else 'failed'), (str(exc) or type(exc).__name__)
    elif op.result:
        result, message = op.result, op.message
        if result == 'failed' and op.cancelled:
            result = 'cancelled'
    else:
        result, message = ('cancelled' if op.cancelled else 'ok'), ''
    record({'type': 'change', 'kind': op.kind, 'target': op.target, 'stack': stack_of(op.kind, op.target),
            'result': result, 'message': message, 'seconds': round(time.time() - op.started, 1), 'source': source()})


def entries() -> list[dict]:
    """Every entry, oldest first (a line that is not an entry is left out)."""
    raw = st.read_plain(path(), READ_MAX)
    out = []
    for line in (raw or b'').decode('utf-8', 'replace').splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _one(query: dict, key: str, default: str) -> str:
    value = query.get(key, default)
    return str((value[0] if value else default) if isinstance(value, list) else value)


def listing(query: dict) -> dict:
    """{entries: newest first, total}; query: show (all, changes, warnings, failed), stack, limit (default 200)."""
    show, stack, limit = _one(query, 'show', 'all'), _one(query, 'stack', ''), _one(query, 'limit', '200')
    if show not in SHOW:
        raise ValueError(f'show: one of {", ".join(SHOW)}')
    if not limit.isdigit() or int(limit) < 1:
        raise ValueError('limit: a number from 1')
    rows = entries()[::-1]
    if show == 'changes':
        rows = [r for r in rows if r.get('type') == 'change']
    elif show == 'warnings':
        rows = [r for r in rows if r.get('type') == 'warning']
    elif show == 'sign-ins':
        rows = [r for r in rows if r.get('type') == 'login']
    elif show == 'failed':
        rows = [r for r in rows if r.get('type') == 'change' and r.get('result') in NOT_OK]
    if stack:
        rows = [r for r in rows if r.get('stack') == stack]
    return {'entries': rows[:int(limit)], 'total': len(rows)}


# ---------- the watcher ----------

def _watch_state() -> dict:
    global _WATCH
    if _WATCH is None:
        try:
            data = json.loads((st.read_plain(st.DATA / WATCH_FILE) or b'{}').decode('utf-8'))
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        _WATCH = {k: [str(x) for x in data.get(k) or [] if isinstance(x, str)] for k in ('looping', 'over', 'near', 'oom')}
    return _WATCH


def forget_watch_memory() -> None:
    """Drop what the watcher holds in memory (it reads its file again), as a restart of the page does."""
    global _WATCH
    _WATCH = None


def _save_watch(state: dict) -> None:
    try:
        st.DATA.mkdir(parents=True, exist_ok=True)
        tmp = st.DATA / (WATCH_FILE + '.new')
        tmp.write_text(json.dumps(state), encoding='utf-8')
        os.replace(tmp, st.DATA / WATCH_FILE)
    except OSError as exc:
        print(f'activity: the watcher state was not written ({exc})', file=sys.stderr, flush=True)


def _stack_label(c: dict) -> str:
    labels = c.get('Labels') or {}
    return next((str(labels[k]) for k in PROJECT_LABELS if labels.get(k)), '')


def _mib(n: float) -> str:
    return f'{n / 1024 ** 3:.1f} GB' if n >= 1024 ** 3 else f'{n / 1024 ** 2:.0f} MB'


def watch_once() -> None:
    """One look at the containers: write what started or ended since the last look."""
    try:
        containers = pa.containers()
    except pa.PodmanError:
        return
    try:
        stats: list[dict] | None = pa.container_stats()
    except pa.PodmanError:
        stats = None
    state = _watch_state()
    stack = {st.cname(c): _stack_label(c) for c in containers}

    def warn(kind: str, name: str, result: str, message: str = '') -> None:
        record({'type': 'warning', 'kind': kind, 'target': name, 'stack': stack.get(name, ''), 'result': result,
                'message': message, 'source': 'watch'})

    looping = {st.cname(c): hl.restarts(c) for c in containers if hl.looping(c)}
    for name in sorted(set(looping) - set(state['looping'])):
        warn('restart-loop', name, 'started', f'{looping[name]} restarts by its restart policy, the last within '
                                              f'{hl.LOOP_WINDOW // 60} minutes')
    for name in sorted(set(state['looping']) - set(looping)):
        warn('restart-loop', name, 'ended')
    state['looping'] = sorted(looping)

    for c in containers:
        if hl.oom_killed(c):
            key = f'{c.get("Id")}@{c.get("ExitedAt")}'
            if key not in state['oom']:
                warn('oom', st.cname(c), 'killed', 'the kernel killed it for memory: it reached its memory limit; '
                                                    'raise its mem_limit in the env file')
                state['oom'] = (state['oom'] + [key])[-200:]

    if stats is not None:
        over = {}
        host = hl.host_memory()      # a container without a limit has the host's memory as its "limit": not one
        for s in stats:
            limit, used = int(s.get('MemLimit') or 0), int(s.get('MemUsage') or 0)
            if limit > 0 and not (host and limit >= host) and used / limit >= hl.NEAR_LIMIT:
                over[str(s.get('Name') or '')] = (used, limit)
        for name in sorted(set(over) & set(state['over']) - set(state['near'])):     # two looks in a row
            used, limit = over[name]
            warn('memory-near', name, 'started', f'{round(100 * used / limit)} % of its {_mib(limit)} memory limit: '
                                                 'raise its mem_limit in the env file before the kernel kills it')
            state['near'].append(name)
        for name in sorted(set(state['near']) - set(over)):
            warn('memory-near', name, 'ended')
            state['near'].remove(name)
        state['over'] = sorted(over)
    _save_watch(state)


def start_thread(every: float | None = None) -> threading.Thread | None:
    """The watcher: a look every WATCH_EVERY seconds (the first after one wait)."""
    if not AUTOSTART:
        return None

    def loop():
        while True:
            time.sleep(every or WATCH_EVERY)
            try:
                watch_once()
            except Exception as exc:  # noqa: BLE001 - the watcher keeps watching
                print(f'activity watcher: {exc!r}', file=sys.stderr, flush=True)
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread
