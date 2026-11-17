"""What is done to a stack as a whole: the overview and drift, deploy, roll back, start / stop / delete,
restart a container of it, its folder, and taking over old deploy records."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import core
from . import health as hl
from . import lifecycle
from . import podman_api as pa
from . import rollback
from . import schedule
from . import stacks as st
from .core import FILES_LOCK, HIDE_UNDEPLOYED, NotDeployed, deployed, find_module, operation, refuse_protected, require_podman_state, self_projects, snapshot
from .stackfiles import check_compose, check_env, check_stack_file, pin_manifest, write_file

def overview() -> dict:
    """Every stack with its containers, and whether Podman could be read."""
    snap = snapshot()
    containers = snap['containers'] or []
    own = self_projects()
    rows = []
    for folder, compose_file, env_file in st.discover():
        name = folder.name
        names = st.container_names((folder / compose_file).read_text(encoding='utf-8'))
        mine = [c for c in containers if st.belongs(c, name, names)]
        stack = None
        if deployed(name, snap):
            running = any(c.get('State') == 'running' for c in st.project_containers(name, containers))
            stack = {'status': 1 if running else 2}
        rows.append({'name': name, 'compose': compose_file, 'env': env_file, 'stack': stack,
                     'rollback': rollback.exists(folder),
                     'auto': schedule.is_auto(name), 'autoNote': schedule.note(name),
                     'protected': name in core.PROTECTED or name in own,
                     'containers': [{'name': st.cname(c), 'state': c.get('State'), 'status': c.get('Status'),
                                     'image': c.get('Image'), 'restarts': hl.restarts(c), 'looping': hl.looping(c), 'oom': hl.oom_killed(c)}
                                    for c in mine]})
    return {'stacks': rows, 'podmanError': snap['podmanError'], 'source': core.SOURCE, 'hideUndeployed': HIDE_UNDEPLOYED}


def diff(name: str) -> dict:
    """The stack's files against the ones its last deploy used."""
    folder, compose_file, env_file = find_module(name)
    if not deployed(name, snapshot()):
        return {'exists': False}
    d = st.deployed_diff(folder, compose_file, env_file, core.SOURCE)
    return {'exists': d is not None, **(d or {})}


def drift() -> dict:
    """{name: {state: 'same' | 'differs' | 'unknown', changes: [{file, added, removed}]}} for every deployed stack
    (unknown: no record of its last deploy; changes: the lines each differing file gained and lost since it)."""
    snap = snapshot()
    out = {}
    for folder, compose_file, env_file in st.discover():
        if deployed(folder.name, snap):
            d = st.deployed_diff(folder, compose_file, env_file, core.SOURCE)
            if d is None:
                out[folder.name] = {'state': 'unknown', 'changes': []}
                continue
            diffs = {compose_file: d['compose'], env_file: d['env'], **d['files']}
            changes = []
            for fname, text in sorted((n, t) for n, t in diffs.items() if n and t):
                lines = text.splitlines()
                changes.append({'file': fname,
                                'added': sum(1 for x in lines if x.startswith('+') and not x.startswith('+++')),
                                'removed': sum(1 for x in lines if x.startswith('-') and not x.startswith('---'))})
            out[folder.name] = {'state': 'differs' if changes else 'same', 'changes': changes}
    return out


def blocking_rows(containers: list[dict]) -> list[dict]:
    return [{'name': st.cname(c), 'state': c.get('State'), 'image': c.get('Image')} for c in containers]


def plan(name: str) -> dict:
    folder, compose_file, env_file = find_module(name)
    require_podman_state()
    p = st.plan(folder, compose_file, env_file)
    return {'action': p['action'], 'blocking': blocking_rows(p['blocking']), 'missingSecrets': p['missing_secrets'],
            'errors': p['errors'], 'warnings': p['warnings']}


def run_deploy(name: str, pull: bool, remove: list[str], only_update: bool = False, recreate: bool = False) -> dict:
    """Deploy with podman-compose; containers in the way go only if all of them are named in remove.
    only_update: nothing is created, a stack that is not deployed is skipped. recreate: recreate the containers
    without a pull (after a secret changed)."""
    folder, compose_file, env_file = find_module(name)
    log: list[str] = []

    def approve(containers: list[dict]) -> bool:
        wanted = {st.cname(c) for c in containers}
        if wanted <= set(remove):
            return True
        log.append('Not removed, because the page did not confirm: ' + ', '.join(sorted(wanted - set(remove))))
        return False

    with operation('deploy', name, cancellable=True) as op:
        try:
            refuse_protected(name)
            snap = require_podman_state()
            if only_update and not deployed(name, snap):
                op.note('skipped', 'not deployed yet')
                return {'ok': False, 'log': ['not deployed yet: skipped']}
            lifecycle.deploy(folder, compose_file, env_file, pull=pull, recreate=recreate, approve=approve, log=log.append)
            return {'ok': True, 'log': log}
        except (st.StackError, pa.PodmanError, ValueError) as exc:
            op.note('failed', str(exc))
            return {'ok': False, 'log': log + [f'ERROR: {exc}'], 'rollback': rollback.exists(folder)}


def rollback_view(name: str) -> dict:
    folder, compose_file, env_file = find_module(name)
    require_podman_state()
    return rollback.view(folder, compose_file, env_file)


def rollback_check(compose_file: str, env_file: str | None):
    """The checks of a save on the page, for a file a roll back would write."""
    def check(fname: str, text: str) -> None:
        try:
            if fname == compose_file:
                check_compose(text)
            elif fname == env_file:
                check_env(text)
            else:
                check_stack_file(fname, text)
        except ValueError as exc:
            raise ValueError(str(exc) if fname in str(exc) else f'{fname}: {exc}') from None
    return check


def rollback_writer(folder: Path, compose_file: str, env_file: str | None):
    """How a roll back writes a file of the stack: like a save on the page (history, the manifest)."""
    def put(path: Path, text: str) -> None:
        with FILES_LOCK:
            if path.name not in (compose_file, env_file):
                pin_manifest(folder, compose_file, env_file)
            write_file(path, text)
    return put


def run_rollback(name: str) -> dict:
    """Put the stack back to its rollback point (files, image names, containers); {ok, log} like a deploy."""
    folder, compose_file, env_file = find_module(name)
    log: list[str] = []

    put = rollback_writer(folder, compose_file, env_file)
    with operation('rollback', name, cancellable=True) as op:
        try:
            refuse_protected(name)
            require_podman_state()
            lifecycle.roll_back(folder, compose_file, env_file, put, log.append,
                               check=rollback_check(compose_file, env_file))
            log.append(f'Stack "{name}" rolled back.')
            return {'ok': True, 'log': log}
        except (st.StackError, pa.PodmanError, ValueError) as exc:
            op.note('failed', str(exc))
            return {'ok': False, 'log': log + [f'ERROR: {exc}']}


def discard_rollback(name: str) -> dict:
    folder, _, _ = find_module(name)
    refuse_protected(name)
    with operation('rollback-discard', name):
        rollback.discard(folder)
    return {'discarded': True}


def adopt_records() -> list[str]:
    """Once, after the upgrade that moved the record of the last deploy into the data folder: a stack whose old
    record (.deployed/ in its folder) holds exactly the files a deploy from the page stored the hashes of gets it
    copied there. Any other old record is not trusted: that stack shows Unknown until its next Update."""
    adopted = []
    for folder, compose_file, env_file in st.discover():
        old = folder / st.DEPLOYED
        if st.record_dir(folder).exists() or old.is_symlink() or not old.is_dir():
            continue
        snap = st.snapshot(old, compose_file, env_file)     # read once: what is hashed is what is copied
        if not schedule.matches(folder.name, st.hashes(snap)):
            continue
        if not st.write_record(folder, snap, compose_file):
            continue
        adopted.append(folder.name)
    return adopted


OWN_FOLDERS = ('.history', st.DEPLOYED, rollback.LEGACY)   # Stack UI's own folders in a stack's folder: not the application's data


def dir_size(path: Path, limit: int = 50_000) -> tuple[int, bool]:
    """(bytes of the files below path, whether every file was counted: it stops after limit files)."""
    total = count = 0
    for root, _, files in os.walk(path):
        for fname in files:
            count += 1
            if count > limit:
                return total, False
            try:
                total += (Path(root) / fname).lstat().st_size
            except OSError:
                pass
    return total, True


def stack_folder(name: str) -> dict:
    """What deleting the stack's folder removes: its entries with sizes. hasFolders: it holds a folder besides
    Stack UI's own, and a compose file can mount such a folder (./data), so it may hold the application's data."""
    folder, _, _ = find_module(name)
    entries = []
    for p in sorted(folder.iterdir(), key=lambda x: x.name):
        if p.is_dir() and not p.is_symlink():
            size, complete = dir_size(p)
            entries.append({'name': p.name, 'dir': True, 'size': size, 'complete': complete})
        else:
            entries.append({'name': p.name, 'dir': False, 'size': p.lstat().st_size, 'complete': True})
    return {'entries': entries, 'hasFolders': any(e['dir'] and e['name'] not in OWN_FOLDERS for e in entries)}


def remove_stack_folder(folder: Path) -> None:
    """Delete a stack's folder: only a plain folder directly inside STACKS_DIR (never a link, never STACKS_DIR)."""
    if folder.is_symlink() or not folder.is_dir() or folder.resolve().parent != st.STACKS_DIR.resolve():
        raise ValueError(f'"{folder.name}" is not a plain folder of STACKS_DIR: it is not deleted')
    try:
        shutil.rmtree(folder)
    except OSError as exc:
        raise ValueError(f'the folder could not be deleted completely: {exc}') from None


def stack_action(name: str, action: str, data: dict) -> dict:
    """start, stop or delete the stack; delete needs {confirm: <name>} and, to remove the stack's folder too,
    {deleteFolder: true} (a stack without containers can only be deleted that way)."""
    folder, compose_file, env_file = find_module(name)
    if action == 'delete' and data.get('confirm') != name:
        raise ValueError(f'type the stack name "{name}" to delete it')
    refuse_protected(name)
    remove_folder = action == 'delete' and data.get('deleteFolder') is True
    with operation(f'stack-{action}', name, cancellable=True):
        live = deployed(name, require_podman_state())
        if not live and not remove_folder:
            raise NotDeployed(name)
        if live:
            lifecycle.stack_action(folder, compose_file, env_file, action, log=lambda _: None)
        if remove_folder:
            remove_stack_folder(folder)
            rollback.discard(folder)
            st.drop_record(folder)
            schedule.forget(name)
    return {'ok': True, 'action': action, 'folderRemoved': remove_folder}


def restart(name: str, data: dict) -> dict:
    """Restart one container of the stack, looked up here by name (an id from the browser is not trusted)."""
    folder, compose_file, _ = find_module(name)
    wanted = str(data.get('container') or '')
    names = st.container_names((folder / compose_file).read_text(encoding='utf-8'))
    with operation('restart', f'{wanted} of {name}'):
        found = next((c for c in pa.containers() if st.cname(c) == wanted and st.belongs(c, name, names)), None)
        if found is None:
            raise ValueError(f'"{wanted}" is not a container of {name}')
        pa.container_action(str(found['Id']), 'restart')
    return {'restarted': wanted}
