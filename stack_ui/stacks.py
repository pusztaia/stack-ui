"""Compose stacks on a Podman host: which folders are stacks, and podman-compose on them.

A stack is a direct subfolder of STACKS_DIR named like a podman-compose project (lowercase letters, digits,
- and _) that holds a compose file: the one stack.json names ({"compose": ..., "env": ...}), else the first of
compose.yaml, compose.yml, docker-compose.yml, docker-compose.yaml, else its only *.yaml / *.yml. The env file
is stack.json's, else .env, else the only *.env, else none. STACK_UI_IGNORE (comma separated) skips folders.

A stack runs as the podman-compose project of its folder's name (label io.podman.compose.project), with the
folder's files as they are, from the folder (relative paths resolve there: mount STACKS_DIR at the same path
in the container as on the host). A deploy keeps the files it used in <folder>/.deployed/ (drift). Compose
secrets marked external are Podman secrets; a deploy refuses to start while one is missing. PODMAN_COMPOSE is
the podman-compose command, PODMAN_PATH the podman it runs (only the bare name podman or a full path work).
"""
from __future__ import annotations

import difflib
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import yaml

from . import podman_api as pa

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
DEPLOYED = '.deployed'
COMPOSE_CMD = os.environ.get('PODMAN_COMPOSE', 'podman-compose')
PODMAN_PATH = os.environ.get('PODMAN_PATH', 'podman')
LONG_TIMEOUT = 900
chown = getattr(os, 'chown', None)


class StackError(Exception):
    pass


# ---------- which folders are stacks ----------

def plain_file(path: Path) -> bool:
    """A regular file that is not a link: a link in a stack's folder (planted by a container that mounts it) must
    not make the page read or write a file elsewhere."""
    return path.is_file() and not path.is_symlink()


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

def plan(folder: Path, compose_file: str) -> dict:
    compose = (folder / compose_file).read_text(encoding='utf-8')
    containers = pa.containers()
    return {'action': 'update' if project_containers(folder.name, containers) else 'create',
            'blocking': blocking(folder.name, compose, containers),
            'missing_secrets': missing_secrets(compose)}


def in_pod_args(folder: Path, compose_file: str) -> list[str]:
    """No pod per stack (like Docker Compose and Portainer: userns_mode and friends work), unless the compose file
    decides itself with `x-podman: {in_pod: ...}` (the command line would override that)."""
    try:
        data = yaml.safe_load((folder / compose_file).read_text(encoding='utf-8')) or {}
    except (OSError, ValueError, yaml.YAMLError):
        data = {}
    x_podman = data.get('x-podman') if isinstance(data, dict) else None
    return [] if isinstance(x_podman, dict) and 'in_pod' in x_podman else ['--in-pod', 'false']


def run_compose(folder: Path, compose_file: str, env_file: str | None, args: list[str], log,
                timeout: int = LONG_TIMEOUT) -> None:
    cmd = [*shlex.split(COMPOSE_CMD), '--podman-path', PODMAN_PATH, *in_pod_args(folder, compose_file),
           '-p', folder.name, '-f', compose_file, *(['--env-file', env_file] if env_file else []), *args]
    log('$ podman-compose ' + ' '.join(args))
    try:
        r = subprocess.run(cmd, cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           encoding='utf-8', errors='replace', timeout=timeout, check=False)
    except FileNotFoundError:
        raise StackError(f'podman-compose is not installed ({COMPOSE_CMD})') from None
    except subprocess.TimeoutExpired:
        raise StackError(f'podman-compose {args[0]} did not finish in {timeout // 60} minutes') from None
    for line in r.stdout.splitlines():
        log(line)
    if r.returncode:
        raise StackError(f'podman-compose {args[0]} failed (exit code {r.returncode})')


def check_config(folder: Path, compose_file: str, env_file: str | None) -> None:
    """podman-compose reads the compose and env file (read-only, `config`); StackError when it cannot."""
    out: list[str] = []
    try:
        run_compose(folder, compose_file, env_file, ['config'], out.append, timeout=60)
    except StackError as exc:
        raise StackError(f'{exc}: ' + ' | '.join(out[-5:])) from None


def save_deployed(folder: Path, compose_file: str, env_file: str | None) -> None:
    record = folder / DEPLOYED
    shutil.rmtree(record, ignore_errors=True)
    record.mkdir()
    match_owner(record)
    for fname in (compose_file, env_file, *stack_file_names(folder, compose_file, env_file)):
        if fname:
            shutil.copyfile(folder / fname, record / fname)
            match_owner(record / fname)


def deploy(folder: Path, compose_file: str, env_file: str | None, *, approve, dry_run: bool = False,
           pull: bool = True, recreate: bool = False, log=print) -> str:
    """Create or update the stack. approve(containers) decides about containers in its way. pull pulls first
    (before anything is removed) and recreates every container; recreate does it without a pull (after a
    secret changed). Returns 'create' or 'update'."""
    p = plan(folder, compose_file)
    if p['missing_secrets']:
        raise StackError('these Podman secrets do not exist yet: ' + ', '.join(p['missing_secrets'])
                         + ' (create them under Secrets first)')
    if dry_run:
        log(f'would {p["action"]} the stack "{folder.name}"'
            + (f'; first it asks to remove {", ".join(cname(c) for c in p["blocking"])}' if p['blocking'] else ''))
        return p['action']
    if p['blocking'] and not approve(p['blocking']):
        raise StackError('not deployed: the containers in its way are still there')
    if pull:  # before anything is removed: a failed pull leaves the old containers running
        run_compose(folder, compose_file, env_file, ['pull'], log)
    for c in p['blocking']:
        pa.remove_container(str(c['Id']))
        log(f'  removed {cname(c)}')
    run_compose(folder, compose_file, env_file, ['up', '-d', *(['--force-recreate'] if pull or recreate else [])], log)
    save_deployed(folder, compose_file, env_file)
    log(f'Stack "{folder.name}" {"created" if p["action"] == "create" else "updated"}.')
    return p['action']


def stack_action(folder: Path, compose_file: str, env_file: str | None, action: str, log=print) -> None:
    """start, stop or delete (podman-compose down: the containers go, the folder and the host data stay)."""
    if action not in ('start', 'stop', 'delete'):
        raise ValueError(f'unknown stack action {action}')
    run_compose(folder, compose_file, env_file, ['down' if action == 'delete' else action], log)
    if action == 'delete':
        shutil.rmtree(folder / DEPLOYED, ignore_errors=True)


def deployed_diff(folder: Path, compose_file: str, env_file: str | None, label: str) -> dict | None:
    """The last deploy's files against the folder's (deployed -> label): {'compose', 'env', 'files': {name: diff}}
    (a file new or removed since the deploy diffs from / to empty); None without a record."""
    record = folder / DEPLOYED
    if not (record / compose_file).is_file() or (env_file and not (record / env_file).is_file()):
        return None

    def read(path: Path) -> list[str]:
        return path.read_text(encoding='utf-8', errors='replace').splitlines() if path.is_file() else []

    def udiff(fname: str | None) -> str:
        if not fname:
            return ''
        return '\n'.join(difflib.unified_diff(read(record / fname), read(folder / fname), f'deployed: {fname}',
                                              f'{label}: {fname}', lineterm=''))

    names = set(stack_file_names(folder, compose_file, env_file)) | set(stack_file_names(record, compose_file, env_file))
    return {'compose': udiff(compose_file), 'env': udiff(env_file), 'files': {n: udiff(n) for n in sorted(names)}}


def has_differences(d: dict) -> bool:
    return bool(d['compose'] or d['env'] or any(d['files'].values()))
