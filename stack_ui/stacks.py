"""Compose stacks on a Podman host: which folders are stacks, and podman-compose on them.

A stack is a direct subfolder of STACKS_DIR named like a podman-compose project (lowercase letters, digits,
- and _) that holds a compose file: the one stack.json names ({"compose": ..., "env": ...}), else the first of
compose.yaml, compose.yml, docker-compose.yml, docker-compose.yaml, else its only *.yaml / *.yml. The env file
is stack.json's, else .env, else the only *.env, else none. STACK_UI_IGNORE (comma separated) skips folders.

A stack runs as the podman-compose project of its folder's name (label io.podman.compose.project), with the
folder's files as they are, from the folder (relative paths resolve there: mount STACKS_DIR at the same path
in the container as on the host). A deploy keeps the files it used in STACK_UI_DATA/deployed/<stack>/ (drift;
not in the stack's folder, which a container may mount and write). Compose
secrets marked external are Podman secrets; a deploy refuses to start while one is missing. PODMAN_COMPOSE is
the podman-compose command, PODMAN_PATH the podman it runs (only the bare name podman or a full path work).
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import threading
from pathlib import Path

import yaml

from . import podman_api as pa
from . import preflight

STACKS_DIR = Path(os.environ['STACKS_DIR']).resolve() if os.environ.get('STACKS_DIR') else None
IGNORE = {x.strip() for x in os.environ.get('STACK_UI_IGNORE', '').split(',') if x.strip()}
STACK_NAME = re.compile(r'^[a-z0-9][a-z0-9_-]{0,62}$')
MANIFEST = 'stack.json'
ALLOWED_SUFFIXES = ('.yaml', '.yml', '.env', '.sh', '.md', '.txt', '.json')
FILE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$')
MAX_FILE = 512 * 1024
COMPOSE_NAMES = ('compose.yaml', 'compose.yml', 'docker-compose.yml', 'docker-compose.yaml')
PROJECT_LABEL = 'io.podman.compose.project'
COMPOSE_PROJECT_LABELS = ('com.docker.compose.project', 'io.podman.compose.project')
DATA = Path(os.environ.get('STACK_UI_DATA') or '/data')     # the page's own folder (server.DATA)
DEPLOYED = '.deployed'          # where the record was kept until 1.0.24, in the stack's folder: never read, removed on sight
COMPOSE_CMD = os.environ.get('PODMAN_COMPOSE', 'podman-compose')
PODMAN_PATH = os.environ.get('PODMAN_PATH', 'podman')
LONG_TIMEOUT = 900          # pull
try:                        # up, start, stop, down: a command that does not finish in this time is ended
    OP_TIMEOUT = max(30, int(os.environ.get('STACK_UI_UP_TIMEOUT') or 300))
except ValueError:
    OP_TIMEOUT = 300
KILL_GRACE = 3.0            # seconds between SIGTERM and SIGKILL when a command is ended
CANCELLED = 'cancelled: the command was stopped on request; what it had already created is still there'
MAX_OUTPUT_LINES = 200      # of a command that was stopped (cancelled, or at its time limit): its last lines are logged
RUNNING = threading.local()   # .op: the operation (server.py) the calling thread runs; run_compose registers with it
chown = getattr(os, 'chown', None)


class StackError(Exception):
    pass


# ---------- which folders are stacks ----------

def plain_file(path: Path) -> bool:
    """A regular file that is not a link: a link in a stack's folder (planted by a container that mounts it) must
    not make the page read or write a file elsewhere."""
    return path.is_file() and not path.is_symlink()


def read_plain(path: Path, limit: int | None = None) -> bytes | None:
    """The bytes of a regular file of at most limit bytes (MAX_FILE), read through one open that follows no link;
    None for anything else (missing, a link, a folder, a FIFO, larger). Checking a path and then opening it would
    let a container swap the file for a link in between."""
    limit = MAX_FILE if limit is None else limit
    try:
        if path.is_symlink():           # (where O_NOFOLLOW does not exist)
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
                     | getattr(os, 'O_BINARY', 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        data = b''
        while len(data) <= limit:
            chunk = os.read(fd, limit + 1 - len(data))
            if not chunk:
                break
            data += chunk
        return data if len(data) <= limit else None
    except OSError:
        return None
    finally:
        os.close(fd)


def snapshot(folder: Path, compose_file: str, env_file: str | None) -> dict[str, bytes | None]:
    """{file name: bytes} of the stack's files (compose, env, the others), each read once; None for one that is
    no regular file of a size a stack file may have."""
    try:
        names = stack_file_names(folder, compose_file, env_file)
    except OSError:
        names = []
    return {fname: read_plain(folder / fname) for fname in (compose_file, env_file, *names) if fname}


def hashes(snap: dict[str, bytes | None]) -> dict[str, str] | None:
    """{file name: SHA-256} of a snapshot; None when a file of it could not be read."""
    if not snap or any(data is None for data in snap.values()):
        return None
    return {fname: hashlib.sha256(data).hexdigest() for fname, data in snap.items()}


def plain_name(name: str) -> bool:
    """A bare file name of the stack's own folder (no path, so stack.json cannot point outside it)."""
    return FILE_NAME.match(name) is not None or name == '.env'


def stack_files(folder: Path) -> tuple[str, str | None] | None:
    """(compose file, env file or None) of a folder, or None when it is not a stack."""
    manifest = folder / MANIFEST
    if manifest.is_file():
        try:
            m = json.loads(manifest.read_text(encoding='utf-8'))
            compose, env = str(m['compose']), m.get('env')
        except (ValueError, KeyError, TypeError):
            return None
        env = str(env) if env else None
        if not plain_name(compose) or (env is not None and not plain_name(env)):
            return None
        return (compose, env) if plain_file(folder / compose) else None
    compose = next((n for n in COMPOSE_NAMES if plain_file(folder / n)), None)
    if compose is None:
        yamls = sorted(p.name for p in folder.iterdir() if plain_file(p) and p.suffix in ('.yaml', '.yml'))
        if len(yamls) != 1:
            return None
        compose = yamls[0]
    if plain_file(folder / '.env'):
        return compose, '.env'
    envs = sorted(p.name for p in folder.glob('*.env') if plain_file(p))
    return compose, envs[0] if len(envs) == 1 else None


def is_stack_file_name(name: str) -> bool:
    """A name a stack folder's file may have: flat, allowed suffix, not the manifest."""
    return ((FILE_NAME.match(name) is not None or name == '.env') and name.endswith(ALLOWED_SUFFIXES)
            and name != MANIFEST)


def stack_file_names(folder: Path, compose_file: str, env_file: str | None) -> list[str]:
    """The stack's other files (scripts, configs, dashboards): every regular file with an allowed name, except
    the compose and env file."""
    return sorted(p.name for p in folder.iterdir() if plain_file(p) and is_stack_file_name(p.name)
                  and p.name not in (compose_file, env_file) and not p.name.startswith('.'))


def discover() -> list[tuple[Path, str, str | None]]:
    """Every stack of STACKS_DIR: (folder, compose file, env file or None)."""
    if STACKS_DIR is None or not STACKS_DIR.is_dir():
        raise StackError(f'STACKS_DIR is not set or not a directory ({STACKS_DIR})')
    found = []
    for folder in sorted(p for p in STACKS_DIR.iterdir() if p.is_dir()):
        if folder.name in IGNORE or not STACK_NAME.match(folder.name):
            continue
        try:
            files = stack_files(folder)
            if files and (files[1] is None or plain_file(folder / files[1])):
                found.append((folder, *files))
        except OSError:  # an unreadable folder (e.g. another user's 0700) is not a stack; the others still are
            continue
    return found


# ---------- containers of a stack ----------

def container_names(compose: str) -> list[str]:
    return re.findall(r'^\s+container_name:\s*["\']?([\w.-]+)', compose, flags=re.MULTILINE)


def cname(c: dict) -> str:
    return (c.get('Names') or ['?'])[0].lstrip('/')


def belongs(c: dict, stack: str, names: list[str]) -> bool:
    """The container has one of the stack's container names, or belongs to the compose project of that name."""
    labels = c.get('Labels') or {}
    return cname(c) in names or any(labels.get(label) == stack for label in COMPOSE_PROJECT_LABELS)


def project_containers(name: str, containers: list[dict] | None = None) -> list[dict]:
    containers = pa.containers() if containers is None else containers
    return [c for c in containers if (c.get('Labels') or {}).get(PROJECT_LABEL) == name]


def projects(containers: list[dict]) -> set[str]:
    return {str((c.get('Labels') or {}).get(PROJECT_LABEL) or '') for c in containers} - {''}


def blocking(name: str, compose_text: str, containers: list[dict] | None = None) -> list[dict]:
    """Containers holding one of the stack's container names that are not of its podman-compose project."""
    names = container_names(compose_text)
    containers = pa.containers() if containers is None else containers
    return [c for c in containers if cname(c) in names and (c.get('Labels') or {}).get(PROJECT_LABEL) != name]


def external_secrets(compose_text: str) -> list[str]:
    data = yaml.safe_load(compose_text) or {}
    declared = data.get('secrets') if isinstance(data, dict) else None
    if not isinstance(declared, dict):
        return []
    return sorted(str(k) for k, v in declared.items() if isinstance(v, dict) and (v.get('external') or v.get('name')))


def missing_secrets(compose_text: str) -> list[str]:
    wanted = external_secrets(compose_text)
    if not wanted:
        return []
    have = {s['name'] for s in pa.secrets()}
    return [s for s in wanted if s not in have]


# ---------- files written as root keep the folder's owner ----------

def write_lf(path: Path, text: str) -> None:
    """Write text with LF line ends on every OS (Path.write_text's newline= needs Python 3.10)."""
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text)


def _is_root() -> bool:
    return hasattr(os, 'geteuid') and os.geteuid() == 0


def match_owner(*paths: Path) -> None:
    """Running as root (a rootful container), what is written into STACKS_DIR gets the owner of its parent
    directory, so the stacks stay editable by their owner."""
    if not _is_root():
        return
    for path in paths:
        info = path.parent.stat()
        chown(path, info.st_uid, info.st_gid)


# ---------- podman-compose ----------

def plan(folder: Path, compose_file: str, env_file: str | None = None) -> dict:
    compose = (folder / compose_file).read_text(encoding='utf-8')
    try:
        env = (folder / env_file).read_text(encoding='utf-8') if env_file else None
    except OSError:
        env = None
    containers = pa.containers()
    names = container_names(compose)
    checks = preflight.check(compose, env, [c for c in containers if not belongs(c, folder.name, names)],
                             [str(n.get('name')) for n in pa.networks()])
    return {'action': 'update' if project_containers(folder.name, containers) else 'create',
            'blocking': blocking(folder.name, compose, containers),
            'missing_secrets': missing_secrets(compose), **checks}


def in_pod_args(folder: Path, compose_file: str) -> list[str]:
    """No pod per stack (like Docker Compose and Portainer: userns_mode and friends work), unless the compose file
    decides itself with `x-podman: {in_pod: ...}` (the command line would override that)."""
    try:
        data = yaml.safe_load((folder / compose_file).read_text(encoding='utf-8')) or {}
    except (OSError, ValueError, yaml.YAMLError):
        data = {}
    x_podman = data.get('x-podman') if isinstance(data, dict) else None
    return [] if isinstance(x_podman, dict) and 'in_pod' in x_podman else ['--in-pod', 'false']


def duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f'{seconds} second{"" if seconds == 1 else "s"}'
    minutes = seconds // 60
    return f'{minutes} minute{"" if minutes == 1 else "s"}'


def kill_tree(proc, grace: float | None = None) -> None:
    """End a command and everything it started. POSIX: its process group (it has one of its own), also when the
    leader has already exited: a child that outlived it keeps the group, and the output pipe, alive. SIGTERM, a
    moment for the leader, then SIGKILL for whatever is left. Elsewhere (development): the process itself."""
    if not hasattr(os, 'killpg'):
        if proc.poll() is None:
            proc.kill()
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return                                      # nothing of the group is left
    try:
        proc.wait(KILL_GRACE if grace is None else grace)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)         # what outlived the SIGTERM
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


def canonical(ref: str) -> str:
    """An image reference as Podman lists it: docker.io/library/nginx:latest for nginx."""
    first, _, rest = ref.partition('/')
    if not rest:
        ref = f'docker.io/library/{ref}'
    elif '.' not in first and ':' not in first and first != 'localhost':
        ref = f'docker.io/{ref}'
    return ref if ':' in ref.rsplit('/', 1)[-1] else ref + ':latest'


def images_ready(folder: Path, compose_file: str) -> bool:
    """Whether every service's image is already on the host, so that `up` has nothing to download or build. False
    when unsure (a variable or digest in the reference, build:, Podman not answering): `up` then gets the long limit."""
    try:
        data = yaml.safe_load((folder / compose_file).read_text(encoding='utf-8')) or {}
        have = {tag for image in pa.images() for tag in image.get('RepoTags') or []}
    except (OSError, ValueError, yaml.YAMLError, pa.PodmanError, AttributeError, TypeError):
        return False
    services = data.get('services') if isinstance(data, dict) else None
    if not isinstance(services, dict) or not services:
        return False
    for service in services.values():
        ref = service.get('image') if isinstance(service, dict) else None
        if not isinstance(ref, str) or '$' in ref or '@' in ref or service.get('build'):
            return False
        if canonical(ref) not in have:
            return False
    return True


def emit_tail(text, log) -> None:
    """What a stopped command had printed: its last lines go to the log."""
    if isinstance(text, bytes):     # (what a TimeoutExpired carries may be undecoded)
        text = text.decode('utf-8', 'replace')
    for line in (text or '').splitlines()[-MAX_OUTPUT_LINES:]:
        log(line)


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
        try:
            out, _ = proc.communicate(timeout=KILL_GRACE + 2)   # bounded: a process outside the group may still hold the pipe
        except subprocess.TimeoutExpired as late:
            proc.stdout.close()
            out = late.output
        emit_tail(out, log)
        if op is not None and op.cancelled:
            raise StackError(CANCELLED) from None
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
        emit_tail(out, log)
        raise StackError(CANCELLED)
    for line in out.splitlines():
        log(line)
    if proc.returncode:
        raise StackError(f'podman-compose {args[0]} failed (exit code {proc.returncode})')

def check_config(folder: Path, compose_file: str, env_file: str | None) -> None:
    """podman-compose reads the compose and env file (read-only, `config`); StackError when it cannot."""
    out: list[str] = []
    try:
        run_compose(folder, compose_file, env_file, ['config'], out.append, timeout=60)
    except StackError as exc:
        raise StackError(f'{exc}: ' + ' | '.join(out[-5:])) from None


def record_dir(folder: Path) -> Path:
    """Where the files of the stack's last deploy are kept: in the page's data folder, since a container that
    mounts its stack's folder can write everything in it."""
    return DATA / 'deployed' / folder.name


def _remove(path: Path) -> None:
    try:
        if path.is_symlink():
            path.unlink()               # never followed
        elif path.exists():
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def drop_legacy_record(folder: Path) -> None:
    _remove(folder / DEPLOYED)


def drop_record(folder: Path) -> None:
    final = record_dir(folder)
    _remove(final)
    _remove(final.with_name(final.name + '.new'))
    drop_legacy_record(folder)


def write_record(folder: Path, snap: dict[str, bytes | None], compose_file: str, log=None) -> bool:
    """Keep these files as the ones the stack's last deploy used (whole or not at all). A file that could not be
    read (too large, no regular file) is left out, and log says so; without the compose file there is no record.
    A record that cannot be written is no reason to fail a deploy that has run: there is none then."""
    final = record_dir(folder)
    new = final.with_name(final.name + '.new')
    try:
        if snap.get(compose_file) is None:
            raise OSError(f'{compose_file} could not be read')
        final.parent.mkdir(parents=True, exist_ok=True)
        _remove(new)
        new.mkdir()
        for fname, data in snap.items():
            if data is None:
                if log is not None:
                    log(f'warning: {fname} is not kept in the record of this deploy (no regular file of at most '
                        f'{MAX_FILE // 1024} KiB)')
                continue
            (new / fname).write_bytes(data)
        _remove(final)
        os.replace(new, final)
        return True
    except OSError as exc:
        _remove(new)
        _remove(final)
        if log is not None:
            log(f'warning: the record of this deploy could not be written ({exc}): the stack shows Unknown')
        return False


def save_deployed(folder: Path, compose_file: str, env_file: str | None, log=None, snap=None) -> None:
    """Keep the files this deploy used: snap (what was read right before the containers were brought up), or the
    folder's files as they are now. The leftover of older versions in the stack's folder goes."""
    write_record(folder, snapshot(folder, compose_file, env_file) if snap is None else snap, compose_file, log)
    drop_legacy_record(folder)


def deploy(folder: Path, compose_file: str, env_file: str | None, *, approve, dry_run: bool = False,
           pull: bool = True, recreate: bool = False, point: bool = True, trusted: bool = True, before_up=None,
           services: list[str] | None = None, log=print) -> str:
    """Create or update the stack. approve(containers) decides about containers in its way. pull pulls first
    (before anything is removed) and recreates every container; recreate does it without a pull (after a
    secret changed). point: keep what the stack ran with as its rollback point (an update of a stack that was
    deployed from here before; rollback.py). trusted: a person decided this deploy, so its files are the ones the
    schedule may deploy again by itself (schedule.approve_hashes). before_up(hashes) is called after the pull, right
    before anything is changed, with the SHA-256 of the files about to be used (None when one could not be read);
    it may raise StackError. services: only these compose services are pulled and brought up
    (the others are left as they are). Returns 'create' or 'update'."""
    from . import rollback      # (rollback imports this module)
    from . import schedule
    p = plan(folder, compose_file, env_file)
    if p['missing_secrets']:
        raise StackError('these Podman secrets do not exist yet: ' + ', '.join(p['missing_secrets'])
                         + ' (create them under Secrets first)')
    if p['errors']:
        raise StackError('not deployed: ' + '; '.join(p['errors']))
    for warning in p['warnings']:
        log('warning: ' + warning)
    if dry_run:
        log(f'would {p["action"]} the stack "{folder.name}"'
            + (f'; first it asks to remove {", ".join(cname(c) for c in p["blocking"])}' if p['blocking'] else ''))
        return p['action']
    if p['blocking'] and not approve(p['blocking']):
        raise StackError('not deployed: the containers in its way are still there')
    # a point still pending (the update that wrote it never finished) is the last good state: a retry must not
    # replace it with the half-updated one the containers show now
    unfinished = bool(point and (rollback.load(folder) or {}).get('pending'))
    before = rollback.capture(folder, compose_file, env_file) if point and not unfinished and p['action'] == 'update' else None
    if pull:  # before anything is removed: a failed pull leaves the old containers running
        run_compose(folder, compose_file, env_file, ['pull', *(services or [])], log)
    if point and not unfinished:   # after the pull (a failed pull changed nothing, its old point stays)
        rollback.keep(folder, before and {**before, 'pending': True}, log if p['action'] == 'update' else None,
                      cleanup=schedule.cleanup_on())
    # what the containers are brought up with, read once, now: the record and the hashes are made from these
    # bytes, not from what a container may have put into the folder by the time `up` has finished
    snap = snapshot(folder, compose_file, env_file)
    if before_up is not None:
        before_up(hashes(snap))
    for c in p['blocking']:
        pa.remove_container(str(c['Id']))
        log(f'  removed {cname(c)}')
    # `up` downloads (and builds) what is not on the host yet, which may take long: it keeps the long limit then;
    # with everything present a command that does not finish soon is hung (an unhealthy dependency)
    up_limit = None if pull or images_ready(folder, compose_file) else LONG_TIMEOUT
    run_compose(folder, compose_file, env_file,
                ['up', '-d', *(['--force-recreate'] if pull or recreate else []), *(services or [])], log,
                timeout=up_limit)
    save_deployed(folder, compose_file, env_file, log, snap)
    if trusted:
        schedule.approve_hashes(folder.name, hashes(snap))
    if point:
        rollback.settle(folder)
    log(f'Stack "{folder.name}" {"created" if p["action"] == "create" else "updated"}.')
    return p['action']


def stack_action(folder: Path, compose_file: str, env_file: str | None, action: str, log=print) -> None:
    """start, stop or delete (podman-compose down: the containers go, the folder and the host data stay)."""
    if action not in ('start', 'stop', 'delete'):
        raise ValueError(f'unknown stack action {action}')
    run_compose(folder, compose_file, env_file, ['down' if action == 'delete' else action], log)
    if action == 'delete':
        drop_record(folder)
        from . import rollback      # (rollback imports this module)
        rollback.discard(folder)


def deployed_diff(folder: Path, compose_file: str, env_file: str | None, label: str) -> dict | None:
    """The last deploy's files against the folder's (deployed -> label): {'compose', 'env', 'files': {name: diff}}
    (a file new or removed since the deploy diffs from / to empty); None without a record."""
    record = record_dir(folder)
    if not plain_file(record / compose_file) or (env_file and not plain_file(record / env_file)):
        return None

    def read(path: Path) -> list[str]:
        return path.read_text(encoding='utf-8', errors='replace').splitlines() if plain_file(path) else []

    def udiff(fname: str | None) -> str:
        if not fname:
            return ''
        return '\n'.join(difflib.unified_diff(read(record / fname), read(folder / fname), f'deployed: {fname}',
                                              f'{label}: {fname}', lineterm=''))

    names = set(stack_file_names(folder, compose_file, env_file)) | set(stack_file_names(record, compose_file, env_file))
    return {'compose': udiff(compose_file), 'env': udiff(env_file), 'files': {n: udiff(n) for n in sorted(names)}}


def has_differences(d: dict) -> bool:
    return bool(d['compose'] or d['env'] or any(d['files'].values()))
