"""Podman objects on their own pages: containers (and their logs), networks, volumes, secrets."""
from __future__ import annotations

import json
import os
import re
import threading

import yaml

from . import core
from . import podman_api as pa
from . import stacks as st
from .core import Busy, NoContainer, operation, require_podman, self_projects
from .image_ops import unix_time

PROTECTED_NETWORKS = {x.strip() for x in os.environ.get('STACK_UI_PROTECTED_NETWORKS', 'podman').split(',') if x.strip()}


# the page's own password secret (and STACK_UI_PROTECTED_SECRETS) is never removed from the Secrets page
PROTECTED_SECRETS = {'stack-ui-password'} | {x.strip() for x in os.environ.get('STACK_UI_PROTECTED_SECRETS', '').split(',') if x.strip()}
LOG_TAILS = (200, 500, 2000)
MAX_LOG_STREAMS = 4         # log streams open at once: each holds a thread and a connection
LOG_LINE_MAX = 8 * 1024     # a longer log line is cut
LOG_HEARTBEAT = 15.0        # seconds of quiet before a heartbeat line (it also finds a client that left)
LOG_SINCE = re.compile(r'^[0-9TZ:.+-]{1,40}$')     # a time stamp, as Podman writes them
LOG_STREAMS = threading.BoundedSemaphore(MAX_LOG_STREAMS)


def stack_label(c: dict) -> str:
    labels = c.get('Labels') or {}
    return str(labels.get(st.PROJECT_LABEL) or labels.get('com.docker.compose.project') or '')


CONTAINER_ACTIONS = ('start', 'stop', 'restart', 'remove')


def is_protected(c: dict, own_projects: set[str]) -> bool:
    """This page's own container and its compose project, and STACK_UI_PROTECTED (names or compose projects):
    never stopped, restarted or removed here."""
    labels = c.get('Labels') or {}
    name, cid = st.cname(c), str(c.get('Id') or '')
    own = bool(core.SELF) and (name == core.SELF or cid.startswith(core.SELF))
    projects = {labels.get(x) for x in st.COMPOSE_PROJECT_LABELS} - {None}
    return own or name in core.PROTECTED or bool(projects & (core.PROTECTED | own_projects))


def published_ports(c: dict) -> list[dict]:
    return [{'host': str(p.get('host_ip') or ''), 'port': int(p.get('host_port') or 0),
             'container': int(p.get('container_port') or 0), 'protocol': str(p.get('protocol') or 'tcp')}
            for p in c.get('Ports') or [] if int(p.get('host_port') or 0) > 0]


def container_table() -> dict:
    """Every container: its stack (a module it belongs to, else its compose project), whether it is part of a
    stack module, whether it is protected, and its published ports."""
    require_podman()
    mods = [(m.name, st.container_names((m / c).read_text(encoding='utf-8'))) for m, c, _ in st.discover()]
    names = {n for n, _ in mods}
    own_projects = self_projects()
    rows = []
    for c in pa.containers():
        owner = next((n for n, cnames in mods if st.belongs(c, n, cnames)), '')
        stack = owner or stack_label(c)
        rows.append({'name': st.cname(c), 'id': str(c.get('Id') or ''), 'state': c.get('State'),
                     'status': c.get('Status'), 'image': c.get('Image'), 'created': unix_time(c.get('Created')),
                     'stack': stack, 'ports': published_ports(c), 'inStack': bool(owner) or stack in names,
                     'protected': is_protected(c, own_projects)})
    return {'containers': sorted(rows, key=lambda r: (r['stack'] or '~', r['name']))}


def find_container(name: str) -> dict:
    row = next((r for r in container_table()['containers'] if r['name'] == name), None)
    if row is None:
        raise ValueError(f'no container "{name}"')
    return row


# wider than SECRET_VAR (which checks env files): anywhere in the name, for what the page shows
SECRET_NAME = re.compile(r'SECRET|PASSW|TOKEN|API_?KEY|PRIVATE|CREDENTIAL', re.IGNORECASE)
URL_PASSWORD = re.compile(r'(://[^/\s:@]*:)[^@\s/]+@')
MASK = '••••'


def mask_list(items: list) -> list:
    """KEY=value (also --flag=value), and the word after a secret-looking --flag, masked; passwords in URLs too."""
    out = []
    for n, item in enumerate(items):
        text = str(item)
        prev = str(items[n - 1]) if n else ''
        key, sep, _ = text.partition('=')
        if sep and SECRET_NAME.search(key.lstrip('-')):
            text = f'{key}={MASK}'
        elif prev.startswith('-') and '=' not in prev and SECRET_NAME.search(prev):
            text = MASK
        out.append(URL_PASSWORD.sub(rf'\g<1>{MASK}@', text))
    return out


def container_details(name: str) -> dict:
    """The container's inspect; secret-looking values masked in the environment and the command lines
    (Config.Env, Config.CreateCommand, Config.Cmd, Config.Entrypoint, Args)."""
    info = pa.inspect_container(find_container(name)['id'])
    config = info.get('Config') if isinstance(info.get('Config'), dict) else {}
    for holder, field in ((config, 'Env'), (config, 'CreateCommand'), (config, 'Cmd'), (config, 'Entrypoint'),
                          (info, 'Args')):
        if isinstance(holder.get(field), list):
            holder[field] = mask_list(holder[field])
        elif isinstance(holder.get(field), str):
            holder[field] = mask_list([holder[field]])[0]
    return info


def container_do(data: dict) -> dict:
    """{name, action}: start, stop, restart or remove one container. Remove only outside the stack modules;
    this page's own container and STACK_UI_PROTECTED are never stopped, restarted or removed here."""
    name, action = str(data.get('name') or ''), str(data.get('action') or '')
    if action not in CONTAINER_ACTIONS:
        raise ValueError(f'unknown container action "{action}"')
    with operation('container', f'{name} {action}'):
        row = find_container(name)
        if row['protected'] and action != 'start':
            raise ValueError(f'"{name}" is protected (this page itself, or STACK_UI_PROTECTED): it is not stopped, '
                             'restarted or removed here')
        if action == 'remove':
            if row['inStack']:
                raise ValueError(f'"{name}" belongs to the stack {row["stack"]}: delete or update the stack instead')
            pa.remove_container(row['id'])
        else:
            pa.container_action(row['id'], action)
    return {'ok': True, 'action': action, 'name': name}


def container_logs(name: str, tail: int) -> dict:
    require_podman()
    if tail not in LOG_TAILS:
        raise ValueError(f'lines: one of {", ".join(map(str, LOG_TAILS))}')
    found = next((c for c in pa.containers() if st.cname(c) == name), None)
    if found is None:
        raise ValueError(f'no container "{name}"')
    return {'container': name, 'lines': pa.logs(str(found['Id']), tail)}


def open_log_stream(name: str, tail: int, since: str) -> tuple[pa.LogStream, threading.BoundedSemaphore]:
    """Follow a container's log: the arguments checked, a stream slot taken, Podman asked. Returns the stream and
    the semaphore the slot came from; the caller closes the stream and releases that slot. ValueError /
    NoContainer / Busy / PodmanError before any byte is sent."""
    require_podman()
    if tail not in LOG_TAILS:
        raise ValueError(f'lines: one of {", ".join(map(str, LOG_TAILS))}')
    if since and not LOG_SINCE.match(since):
        raise ValueError('since: a time stamp like 2026-11-07T10:00:00Z')
    found = next((c for c in pa.containers() if st.cname(c) == name), None)
    if found is None:
        raise NoContainer(name)
    slots = LOG_STREAMS
    if not slots.acquire(blocking=False):
        raise Busy(f'too many log streams are open (at most {MAX_LOG_STREAMS}): close another one first')
    try:
        return pa.LogStream(str(found['Id']), tail, since), slots
    except BaseException:
        slots.release()
        raise


def cut_line(line: str) -> str:
    return line if len(line) <= LOG_LINE_MAX else line[:LOG_LINE_MAX - 1] + '\u2026'


def json_line(obj: dict) -> bytes:
    return (json.dumps(obj) + '\n').encode('utf-8')


def network_list() -> dict:
    require_podman()
    containers = pa.containers()
    rows = []
    for n in pa.networks():
        name = str(n.get('name') or '')
        rows.append({'name': name, 'driver': n.get('driver'), 'subnets': [s.get('subnet') for s in n.get('subnets') or []],
                     'usedBy': sorted(st.cname(c) for c in containers if name in (c.get('Networks') or [])),
                     'protected': name in PROTECTED_NETWORKS})
    return {'networks': sorted(rows, key=lambda r: r['name'])}


def network_create(data: dict) -> dict:
    require_podman()
    with operation('network', f'network {data.get("name") or ""}'):
        pa.create_network(str(data.get('name') or ''), str(data.get('subnet') or '').strip(),
                          str(data.get('gateway') or '').strip())
    return {'created': data.get('name')}


def network_delete(data: dict) -> dict:
    """Only an unused network that is not protected (STACK_UI_PROTECTED_NETWORKS)."""
    name = str(data.get('name') or '')
    with operation('network', f'network {name}'):
        row = next((r for r in network_list()['networks'] if r['name'] == name), None)
        if row is None:
            raise ValueError(f'no network "{name}"')
        if row['protected']:
            raise ValueError(f'"{name}" is protected: the stacks need it')
        if row['usedBy']:
            raise ValueError(f'"{name}" is used by {", ".join(row["usedBy"])}')
        pa.remove_network(name)
    return {'removed': name}


def volume_list() -> dict:
    require_podman()
    rows = [{'name': str(v.get('Name') or ''), 'driver': v.get('Driver'), 'mountpoint': v.get('Mountpoint'),
             'usedBy': pa.volume_users(str(v.get('Name') or ''))} for v in pa.volumes()]
    return {'volumes': sorted(rows, key=lambda r: r['name'])}


def volume_create(data: dict) -> dict:
    require_podman()
    with operation('volume', f'volume {data.get("name") or ""}'):
        pa.create_volume(str(data.get('name') or ''))
    return {'created': data.get('name')}


def volume_delete(data: dict) -> dict:
    """{names: [...]}: removes those that no container (running or stopped) uses, checked again here."""
    wanted = [str(x) for x in data.get('names') or []]
    removed, errors = [], []
    with operation('volume', f'{len(wanted)} volumes'):
        rows = {r['name']: r for r in volume_list()['volumes']}
        for name in wanted:
            row = rows.get(name)
            if row is None:
                errors.append(f'{name}: not found')
            elif row['usedBy']:
                errors.append(f'{name}: used by {", ".join(row["usedBy"])}')
            else:
                pa.remove_volume(name)
                removed.append(name)
    return {'removed': removed, 'errors': errors}


def secret_refs() -> dict[str, list[str]]:
    """{secret: [modules whose compose file names it as an external secret]}."""
    refs: dict[str, list[str]] = {}
    for module, compose_file, _ in st.discover():
        try:
            names = st.external_secrets((module / compose_file).read_text(encoding='utf-8'))
        except (OSError, ValueError, yaml.YAMLError):
            continue
        for s in names:
            refs.setdefault(s, []).append(module.name)
    return refs


def secret_list() -> dict:
    """The Podman secrets (never their values) and the stacks naming each; missing: named but not created."""
    require_podman()
    refs = secret_refs()
    have = sorted(pa.secrets(), key=lambda s: s['name'])
    names = {s['name'] for s in have}
    return {'secrets': [{'name': s['name'], 'created': s['created'], 'updated': s['updated'],
                         'usedBy': refs.get(s['name'], []), 'protected': s['name'] in PROTECTED_SECRETS} for s in have],
            'missing': sorted(set(refs) - names)}


def secret_save(data: dict) -> dict:
    """{name, value, replace}: create a secret, or replace its value. The value is never stored here, logged or
    sent back; a running container sees a new value only after it is recreated (the page offers it)."""
    require_podman()
    name = str(data.get('name') or '')
    value = data.get('value')
    if not isinstance(value, str):
        raise ValueError('the secret value must be text')
    with operation('secret', f'secret {name}'):
        pa.create_secret(name, value.encode('utf-8'), bool(data.get('replace')))
    return {'name': name, 'usedBy': secret_refs().get(name, [])}


def secret_delete(data: dict) -> dict:
    require_podman()
    name = str(data.get('name') or '')
    if name in PROTECTED_SECRETS:
        raise ValueError(f'"{name}" is protected: this page needs it (STACK_UI_PROTECTED_SECRETS)')
    used = secret_refs().get(name)
    if used:
        raise ValueError(f'"{name}" is used by {", ".join(used)}: remove it from their compose files first')
    with operation('secret', f'secret {name}'):
        pa.remove_secret(name)
    return {'removed': name}
