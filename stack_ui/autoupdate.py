"""The page's side of the daily check and the automatic update (schedule.py runs them): the check, the
update of one stack with its verify and roll back, and the routes of the schedule."""
from __future__ import annotations

import re
import time
from datetime import datetime

from . import core
from . import lifecycle
from . import podman_api as pa
from . import registry
from . import rollback
from . import schedule
from . import stacks as st
from .core import Conflict, deployed, find_module, operation, require_podman_state, stack_protected
from .stackops import rollback_check, rollback_writer

SERVICE_LABELS = ('io.podman.compose.service', 'com.docker.compose.service')


def service_of(c: dict) -> str:
    labels = c.get('Labels') or {}
    return next((str(labels[k]) for k in SERVICE_LABELS if labels.get(k)), '')


VERIFY_SETTLE = 15.0    # seconds before the first look: a container that dies at once has died by then
VERIFY_WAIT = 120.0     # how long a health check may stay "starting" before the look that counts
VERIFY_STEP = 5.0
VERIFY_CONFIRM = 10.0   # seconds between the two looks that count: a container in a restart loop shows in one of them


def scheduled_check() -> dict:
    """Ask the registries, and say per stack whether a newer image exists and at which digests."""
    found = registry.updates(check=True)['stacks']
    out = {}
    for name, info in found.items():
        refs = sorted({i['image'] for i in info['images'] if i['status'] == 'update'})
        out[name] = {'update': info['status'] == 'update',
                     'digests': {ref: list(registry.REMOTE.get(ref, ((), ''))[0]) for ref in refs}}
    return out


def verify(name: str, was_running: set[str]) -> str:
    """After an `up`: '' when every container that ran before runs, and none is unhealthy; else what is wrong.
    (podman-compose up -d returns 0 for a container that starts and dies.) A health check still starting is waited
    for; one that is still starting at the end counts as fine (a long start_period is not a failure). The look
    that counts is taken twice, VERIFY_CONFIRM apart: a container that restarts in a loop runs at one moment and
    not at the next, or shows a new start time."""
    def cancelled() -> bool:
        op = core.CURRENT
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
    first = rows
    nap(VERIFY_CONFIRM)
    if cancelled():
        return 'cancelled'
    rows = st.project_containers(name)
    health = {st.cname(c): str(c.get('Status') or '').lower() for c in rows}
    running = ({st.cname(c) for c in first if c.get('State') == 'running'}
               & {st.cname(c) for c in rows if c.get('State') == 'running'})
    started = {st.cname(c): c.get('StartedAt') for c in first}
    problems = [f'{n} is not running' for n in sorted(was_running - running)]
    problems += [f'{st.cname(c)} restarted during the check' for c in sorted(rows, key=st.cname)
                 if st.cname(c) in running and c.get('StartedAt') and started.get(st.cname(c))
                 and c.get('StartedAt') != started[st.cname(c)]]
    problems += [f'{n} is unhealthy' for n, h in sorted(health.items()) if 'unhealthy' in h]
    return '; '.join(problems)


def auto_update(name: str) -> dict:
    """The update of one stack by the schedule: {result, reason, log, alert}. Like Update on the page (pull,
    recreate, rollback point), but nothing that needs a decision is done, the result is verified, and an update
    that did not come up is rolled back. One change from start to end: nothing else runs in between."""
    log: list[str] = []
    running: list = []              # the change, once it holds the lock: its result goes into the Activity log

    def out(result: str, reason: str = '', alert: bool = False) -> dict:
        if running:
            running[0].note(result, reason)
        return {'result': result, 'reason': reason, 'log': log, 'alert': alert}

    try:
        folder, compose_file, env_file = find_module(name)
    except LookupError:
        return out('skipped', 'no folder of that name any more')
    try:
        with operation('auto-update', name, cancellable=True) as op:
            running.append(op)
            snap = require_podman_state()
            if stack_protected(name):
                return out('skipped', 'protected (this page itself, or STACK_UI_PROTECTED)')
            if not deployed(name, snap):
                return out('skipped', 'not deployed')
            # only what a person deployed from the page before, unchanged: judged by the hashes kept in the data
            # folder, which nothing inside a container can write
            if not schedule.approved(folder, compose_file, env_file):
                d = st.deployed_diff(folder, compose_file, env_file, core.SOURCE)
                if d is not None and st.has_differences(d):
                    return out('skipped', 'its files differ from the last deploy: update it on the page')
                return out('skipped', 'its files are not the ones last deployed from the page: update it on the page once')
            if st.deployed_diff(folder, compose_file, env_file, core.SOURCE) is None:    # (nothing to roll back to)
                return out('skipped', 'no record of its last deploy: update it on the page once')
            p = st.plan(folder, compose_file, env_file)
            needs = (p['errors'] + [f'missing secret {s}' for s in p['missing_secrets']]
                     + [f'{st.cname(c)} is in its way' for c in p['blocking']])
            if needs:
                return out('skipped', 'it needs a decision on the page: ' + '; '.join(needs))
            was_running = {st.cname(c) for c in st.project_containers(name, snap['containers'])
                           if c.get('State') == 'running'}
            if not was_running:
                return out('skipped', 'stopped: an update would start it')
            rows = st.project_containers(name, snap['containers'])
            services = None
            if any(c.get('State') != 'running' for c in rows):      # partly stopped: what is stopped stays stopped
                services = sorted({service_of(c) for c in rows if c.get('State') == 'running'})
                if '' in services or not all(re.match(r'^[A-Za-z0-9][A-Za-z0-9._-]*$', s) for s in services):
                    return out('skipped', 'partly stopped, and a running container names no compose service: '
                                          'update it on the page')
                log.append('partly stopped: only ' + ', '.join(services) + ' is updated')
            stage = {'changed': False, 'tampered': False}

            def before_up(found) -> None:   # after the pull, which may take long: still the approved files?
                if not schedule.matches(name, found):
                    stage['tampered'] = True
                    raise st.StackError('its files changed during the update: nothing was started')
                stage['changed'] = True

            try:
                lifecycle.deploy(folder, compose_file, env_file, pull=True, approve=lambda c: False, trusted=False,
                          before_up=before_up, services=services, log=log.append)
                problem = verify(name, was_running)
            except (st.StackError, pa.PodmanError, ValueError, OSError) as exc:
                problem = str(exc) or type(exc).__name__
            if not problem:
                return out('updated')
            log.append(f'ERROR: {problem}')
            if op.cancelled:
                return out('cancelled', problem, alert=True)
            if not stage['changed']:        # the pull failed, or the files changed meanwhile: nothing was touched,
                return out('failed', problem, alert=stage['tampered'])      # and no point of an older update is used
            if rollback.load_record(folder) is None:
                return out('failed', f'{problem}; there is no rollback point to go back to', alert=True)
            try:
                lifecycle.roll_back(folder, compose_file, env_file, rollback_writer(folder, compose_file, env_file),
                                   log.append, check=rollback_check(compose_file, env_file), services=services)
                again = verify(name, was_running)
            except (st.StackError, pa.PodmanError, ValueError, OSError) as exc:
                again = str(exc) or type(exc).__name__
            if again:
                log.append(f'ERROR: roll back: {again}')
                return out('rollback-failed', f'{problem}; the roll back failed too: {again}', alert=True)
            log.append(f'Stack "{name}" rolled back.')
            return out('rolled-back', problem, alert=True)
    except Conflict as exc:
        return out('skipped', str(exc))
    except (st.StackError, pa.PodmanError, ValueError, OSError) as exc:
        return out('failed', str(exc))


def schedule_view() -> dict:
    return schedule.view(datetime.now())


def schedule_save(data: dict) -> dict:
    schedule.configure(data.get('enabled'), data.get('time'), datetime.now(), data.get('cleanup'))
    return schedule_view()


def schedule_run() -> dict:
    try:
        schedule.start(scheduled_check, auto_update)
    except schedule.AlreadyRunning as exc:
        raise Conflict(str(exc)) from None
    return {'started': True}


def schedule_ack(data: dict | None = None) -> dict:
    """Dismiss the alerts the page showed ({alerts: [{stack, at}]}); without a list, all of them."""
    shown = (data or {}).get('alerts')
    if shown is not None and not isinstance(shown, list):
        raise ValueError('alerts: a list of {stack, at}')
    schedule.acknowledge(shown)
    return {'alerts': schedule.alerts()}


def stack_auto(name: str, data: dict) -> dict:
    find_module(name)
    on = data.get('auto')
    if not isinstance(on, bool):
        raise ValueError('auto must be true or false')
    if on and stack_protected(name):
        raise ValueError(f'"{name}" is protected (this page itself, or STACK_UI_PROTECTED): it is not updated from here')
    schedule.set_auto(name, on)
    return {'auto': on}


def stacks_auto(data: dict) -> dict:
    names, on = data.get('names'), data.get('auto')
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names) or len(names) > 200:
        raise ValueError('names: a list of stack names')
    if not isinstance(on, bool):
        raise ValueError('auto must be true or false')
    known = {folder.name for folder, _, _ in st.discover()}
    changed, skipped = [], []
    for name in dict.fromkeys(names):
        if name not in known:
            skipped.append({'name': name, 'reason': 'no such stack'})
        elif on and stack_protected(name):
            skipped.append({'name': name, 'reason': 'protected'})
        else:
            schedule.set_auto(name, on)
            changed.append(name)
    return {'changed': changed, 'skipped': skipped}
