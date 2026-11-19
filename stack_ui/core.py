"""What the other parts share: the settings read from the environment, the one change lock and the change
that holds it (Operation, operation(), Conflict), finding a stack, and which stacks are protected."""
from __future__ import annotations

import contextlib
import ipaddress
import os
import threading
import time
from pathlib import Path

from . import activity
from . import podman_api as pa
from . import stacks as st

SELF = os.environ.get('STACK_UI_SELF') or os.environ.get('HOSTNAME', '')
PROTECTED = {x.strip() for x in os.environ.get('STACK_UI_PROTECTED', '').split(',') if x.strip()}


# the Stacks page lists only the stacks that have containers (the API still lists every folder)
HIDE_UNDEPLOYED = os.environ.get('STACK_UI_HIDE_UNDEPLOYED', '').strip().lower() in ('1', 'true', 'yes')
DEPLOY_LOCK = threading.Lock()   # one change (deploy, start, stop, delete, restart, pull, ...) at a time
FILES_LOCK = threading.Lock()    # one save or upload at a time


class NotDeployed(LookupError):
    pass


class NoContainer(LookupError):
    pass


class Busy(Exception):
    pass


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
            'volume': 'the change of {t}', 'secret': 'the change of {t}',
            'rollback': 'the rollback of {t}', 'rollback-discard': 'discarding the rollback point of {t}',
            'auto-update': 'the automatic update of {t}', 'backup': 'the backup {t}'}

    def __init__(self, kind: str, target: str = '', cancellable: bool = False):
        self.kind, self.target, self.cancellable = kind, target, cancellable
        self.started = time.time()
        self.cancelled = False
        self.proc = None                    # the podman-compose process, set by stacks.run_compose
        self.result, self.message = '', ''  # what the change noted (note()) for the Activity log; '' when it ran through
        self.progress: dict | None = None   # what it is doing now (stacks.set_progress): the page's progress bar

    def note(self, result: str, message: str = '') -> None:
        """The result of a change that does not raise when it fails (a deploy answers {ok: False}) or ends in another
        way (skipped, updated, rolled-back): what the Activity log writes instead of "ok"."""
        self.result, self.message = result, message

    def text(self) -> str:
        what = self.TEXT.get(self.kind, '{t}').format(t=self.target)
        return f'{what} has been running for {st.duration(time.time() - self.started)}'

    def view(self) -> dict:
        return {'kind': self.kind, 'target': self.target, 'since': int(self.started),
                'seconds': int(time.time() - self.started), 'cancellable': self.cancellable, 'text': self.text(),
                'progress': self.progress}


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
    raised = None
    try:
        yield op
    except BaseException as exc:
        raised = exc
        raise
    finally:
        try:
            activity.change(op, raised)          # written while the lock is held: the entries keep their order
        finally:
            CURRENT = None
            st.RUNNING.op = None
            DEPLOY_LOCK.release()


def current_operation() -> dict | None:
    op = CURRENT
    return op.view() if op is not None else None


def cancel_operation(data: dict | None = None) -> dict:
    """End the running podman-compose command (and what it started); what it already created stays. data may name
    the change the caller saw ({since, kind, target}): another one now running is left alone."""
    op = CURRENT
    if op is None:
        raise Conflict('nothing is running')
    want = data or {}
    if want and (want.get('since') != int(op.started) or want.get('kind') != op.kind or want.get('target') != op.target):
        raise Conflict(f'that change has already finished; now running: {op.text()}', op.view())
    if not op.cancellable:
        raise Conflict(f'{op.text()}: it is a quick change and cannot be cancelled', op.view())
    op.cancelled = True
    if op.proc is not None:            # not yet started: run_compose sees op.cancelled right after it starts
        st.kill_tree(op.proc)
    return {'cancelled': op.view()}


def find_module(name: str) -> tuple[Path, str, str | None]:
    for stack in st.discover():
        if stack[0].name == name:
            return stack
    raise LookupError(name)


def snapshot() -> dict:
    """What Podman runs now; podmanError when its API does not answer."""
    snap = {'containers': None, 'podmanError': ''}
    try:
        snap['containers'] = pa.containers()
    except pa.PodmanError as exc:
        snap['podmanError'] = str(exc)
    return snap


def deployed(name: str, snap: dict) -> bool:
    """A podman-compose project of that name exists (running or not)."""
    return snap['containers'] is not None and name in st.projects(snap['containers'])


def stack_protected(name: str) -> bool:
    """This page's own compose project, or a stack named in STACK_UI_PROTECTED: no stop, delete or redeploy here
    (podman-compose would remove the very container it runs in)."""
    return name in PROTECTED or name in self_projects()


def refuse_protected(name: str) -> None:
    if stack_protected(name):
        raise ValueError(f'"{name}" is protected (this page itself, or STACK_UI_PROTECTED): it is not stopped, '
                         'deleted or redeployed from here')


def require_podman_state() -> dict:
    snap = snapshot()
    if snap['podmanError']:
        raise st.StackError(snap['podmanError'])
    return snap


def require_podman() -> None:
    if not pa.configured():
        raise st.StackError('the Podman API is not available here (no socket at PODMAN_SOCKET)')


def self_projects() -> set[str]:
    """The compose projects of this page's own container (STACK_UI_SELF / HOSTNAME, by name or id)."""
    if not SELF:
        return set()
    try:
        own = [c for c in pa.containers() if st.cname(c) == SELF or str(c.get('Id') or '').startswith(SELF)]
    except pa.PodmanError:
        return set()
    return {(c.get('Labels') or {}).get(x) for c in own for x in st.COMPOSE_PROJECT_LABELS} - {None}


SOURCE = 'host'  # how the diff names this side (--source-name)


def plain_address(text: str):
    """An ip_address of text (an IPv4-mapped IPv6 one as IPv4); None when it is not an address."""
    try:
        addr = ipaddress.ip_address(text.strip())
    except ValueError:
        return None
    return addr.ipv4_mapped if addr.version == 6 and addr.ipv4_mapped else addr
