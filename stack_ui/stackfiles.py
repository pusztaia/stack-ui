"""A stack's files on the page: reading, checking (syntax, no secret literals), saving with a history,
uploading a new stack, the history and its restore."""
from __future__ import annotations

import difflib
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import core
from . import rollback
from . import schedule
from . import stacks as st
from .core import FILES_LOCK, find_module

FILE_NAME = st.FILE_NAME
UPLOAD_SUFFIXES = st.ALLOWED_SUFFIXES
SECRET_VAR = re.compile(r'(TOKEN|PASSWORD|PASSWD|SECRET|API_KEY|APIKEY)$', re.IGNORECASE)


def check_compose(text: str) -> None:
    if yaml is None:
        if not re.search(r'^services:\s*$', text, flags=re.MULTILINE):
            raise ValueError('the compose file has no top-level "services:"')
        return
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f'the compose file is not valid YAML: {exc}') from None
    if not isinstance(data, dict) or not isinstance(data.get('services'), dict) or not data['services']:
        raise ValueError('the compose file needs a top-level "services:" mapping with at least one service')


def check_env(text: str) -> None:
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line or not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', line.split('=', 1)[0].strip()):
            raise ValueError(f'env line {n} is not KEY=value: {line[:60]}')
        key, value = (x.strip() for x in line.split('=', 1))
        if SECRET_VAR.search(key) and value.strip('"'):
            raise ValueError(f'env line {n}: {key} holds a secret; secrets never go into an env file '
                             '(use a Podman secret: Secrets)')


REFERENCE = ('$__file{', '${', '$__env{', '/run/secrets/', '!secret', '!env_var', '!include')
ENV_REFERENCE = re.compile(r'^\$[A-Za-z_]\w*$')       # $VAR (Grafana provisioning)
NOT_A_VALUE = {'', 'null', '~', '|', '>', '|-', '>-', '|+', '>+'}   # empty, or a block scalar's first line


class TagLoader(yaml.SafeLoader):
    """safe_load that accepts tags it does not know (Home Assistant: !secret, !include, !env_var)."""


TagLoader.add_multi_constructor('!', lambda loader, suffix, node: None)


KEY_VALUE = re.compile(r'^\s*(?:-\s*)?["\']?([A-Za-z_][\w.-]*)["\']?\s*[:=]\s*(.*?)\s*$')


def literal(value: str) -> str:
    """A YAML/env value without its quotes, or without a trailing # comment when it is not quoted."""
    value = value.strip()
    if value[:1] in ('"', "'"):
        end = value.find(value[0], 1)
        return value[1:end] if end > 0 else value[1:]
    return value.split(' #', 1)[0].strip()


def is_secret_literal(key, value) -> bool:
    if not SECRET_VAR.search(str(key)) or not isinstance(value, str):
        return False
    value = value.strip()
    return value not in NOT_A_VALUE and not value.startswith(REFERENCE) and not ENV_REFERENCE.match(value)


def secret_lines(name: str, text: str) -> list[str]:
    """Where a stack file gives a secret-looking key a literal value: ['file:line' or 'file: key.path']."""
    found = []
    if name.endswith('.json'):
        def walk(node, path):
            if isinstance(node, dict):
                for k, v in node.items():
                    if is_secret_literal(k, v):
                        found.append(f'{name}: {path}{k}')
                    walk(v, f'{path}{k}.')
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, f'{path}{i}.')
        walk(json.loads(text), '')
    elif name.endswith(('.yaml', '.yml', '.env', '.txt')):
        for n, line in enumerate(text.splitlines(), 1):
            m = KEY_VALUE.match(line)
            if m and is_secret_literal(m.group(1), literal(m.group(2))):
                found.append(f'{name}:{n}')
    return found


def check_stack_file(name: str, text: str) -> None:
    """A stack file's name, size, syntax and secrets (raises ValueError naming the file)."""
    if not st.is_stack_file_name(name):
        raise ValueError(f'"{name}": only flat {", ".join(st.ALLOWED_SUFFIXES)} files (not stack.json)')
    if len(text.encode('utf-8')) > st.MAX_FILE:
        raise ValueError(f'"{name}" is larger than {st.MAX_FILE // 1024} KiB')
    try:
        if name.endswith('.json'):
            json.loads(text)
        elif name.endswith(('.yaml', '.yml')):
            yaml.load(text, Loader=TagLoader)  # noqa: S506 - a SafeLoader subclass
    except (ValueError, yaml.YAMLError) as exc:
        raise ValueError(f'"{name}" does not parse: {exc}') from None
    found = secret_lines(name, text)
    if found:
        raise ValueError(f'{found[0]} holds a secret; use a Podman secret (Secrets) and mount it')


def incoming_files(data: dict, compose_file: str, env_file: str | None) -> dict[str, str]:
    """The request's stack files ({name: text}), every one checked (nothing is written when one fails)."""
    given = data.get('files')
    if given is None:
        return {}
    if not isinstance(given, dict):
        raise ValueError('files must be an object {name: text}')
    out = {}
    for name, text in given.items():
        name = str(name)
        if name in (compose_file, env_file):
            raise ValueError(f'"{name}" is the stack\'s compose or env file: send it as composeText / envText')
        if name == '.env':
            raise ValueError('".env" would become the stack\'s env file: give the file another name')
        out[name] = clean_text(text)
        check_stack_file(name, out[name])
    return out


def clean_text(text) -> str:
    if not isinstance(text, str):
        raise ValueError('file contents must be text')
    text = text.replace('\r\n', '\n').replace('\r', '\n')  # LF only: a CR would end up in every value
    return text if text.endswith('\n') else text + '\n'


def backup(path: Path) -> None:
    """Keep the file's current version in .history/ (two saves in one second keep both)."""
    history = path.parent / '.history'
    if not history.is_dir():
        history.mkdir()
        st.match_owner(history)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    target, n = history / f'{path.name}.{stamp}', 1
    while target.exists():
        n += 1
        target = history / f'{path.name}.{stamp}-{n}'
    target.write_bytes(path.read_bytes())
    st.match_owner(target)


def write_file(path: Path, text: str) -> None:
    """Keep the previous version in .history/, then replace the file atomically (owners: see st.match_owner);
    a .sh file is executable."""
    if path.is_symlink():
        raise ValueError(f'"{path.name}" is a link: remove the link on the host first')
    if path.is_file():
        backup(path)
    st.write_atomic(path, text)
    if path.name.endswith('.sh'):
        os.chmod(path, 0o755)


def files(name: str) -> dict:
    """The compose and env file's text, and the stack's other files: [{name, size, changed}] (changed since the
    last deploy; None without a record)."""
    module, compose_file, env_file = find_module(name)
    d = st.deployed_diff(module, compose_file, env_file, core.SOURCE)
    listing = [{'name': n, 'size': (module / n).stat().st_size, 'changed': None if d is None else bool(d['files'].get(n))}
               for n in st.stack_file_names(module, compose_file, env_file)]
    # errors='replace': a file in another encoding opens (with the replacement character), and saving it writes UTF-8
    return {'compose': compose_file, 'composeText': (module / compose_file).read_text(encoding='utf-8', errors='replace'),
            'env': env_file, 'envText': (module / env_file).read_text(encoding='utf-8', errors='replace') if env_file else None,
            'stackFiles': listing}


def pin_manifest(folder: Path, compose_file: str, env_file: str | None) -> None:
    """Write stack.json naming the stack's compose and env file, when there is none: another .yaml or .env file
    in the folder would otherwise change which files discovery picks (or make it no stack at all)."""
    manifest = folder / st.MANIFEST
    if manifest.is_file():
        return
    st.write_atomic(manifest, json.dumps({'compose': compose_file, 'env': env_file}, indent=2) + '\n')


def file_text(name: str, fname: str) -> dict:
    module, compose_file, env_file = find_module(name)
    if fname not in st.stack_file_names(module, compose_file, env_file):
        raise ValueError(f'"{fname}" is not a file of the stack {name}')
    return {'name': fname, 'text': (module / fname).read_text(encoding='utf-8', errors='replace')}


def delete_file(name: str, data: dict) -> dict:
    """{file}: remove one stack file (not compose, env or stack.json); its last version stays in .history/."""
    module, compose_file, env_file = find_module(name)
    fname = str(data.get('file') or '')
    if fname not in st.stack_file_names(module, compose_file, env_file):
        raise ValueError(f'"{fname}" is not a file of the stack {name} (compose, env and stack.json stay)')
    with FILES_LOCK:
        backup(module / fname)
        (module / fname).unlink()
    return {'deleted': fname}


def save_files(name: str, data: dict) -> dict:
    module, compose_file, env_file = find_module(name)
    compose = clean_text(data.get('composeText'))
    check_compose(compose)
    pairs = [(compose_file, compose)]
    if env_file:  # a stack without an env file gets none
        env = clean_text(data.get('envText'))
        check_env(env)
        pairs.append((env_file, env))
    extra = incoming_files(data, compose_file, env_file)
    pairs += sorted(extra.items())
    with FILES_LOCK:
        for fname, _ in pairs:  # before anything is written
            if (module / fname).is_symlink():
                raise ValueError(f'"{fname}" is a link: remove the link on the host first')
        if extra:
            pin_manifest(module, compose_file, env_file)
        changed = []
        for fname, text in pairs:
            path = module / fname
            if st.read_plain(path) != text.encode('utf-8'):     # the bytes: a file that is no UTF-8 is never "the same"
                write_file(path, text)
                changed.append(fname)
    only = sorted(set(st.stack_file_names(module, compose_file, env_file)) - set(extra)) if 'files' in data else []
    return {'saved': changed, 'onlyOnHost': only}


def upload(data: dict) -> dict:
    """A stack package as a new module: {name, files: {filename: text}, compose?, env?}."""
    name = str(data.get('name') or '').strip()
    if not st.STACK_NAME.match(name):
        raise ValueError('the stack name: lowercase letters, digits, - and _ (it is also the podman-compose project name)')
    if name in st.IGNORE:
        raise ValueError(f'"{name}" is ignored (STACK_UI_IGNORE)')
    given = data.get('files')
    if not isinstance(given, dict) or not given:
        raise ValueError('no files')
    pkg = {}
    for fname, text in given.items():
        fname = Path(str(fname)).name
        if not (FILE_NAME.match(fname) or fname == '.env') or not fname.endswith(UPLOAD_SUFFIXES) or fname == st.MANIFEST:
            raise ValueError(f'"{fname}": only {", ".join(UPLOAD_SUFFIXES)} files')
        pkg[fname] = clean_text(text)
    yamls = [f for f in pkg if f.endswith(('.yaml', '.yml'))]
    envs = [f for f in pkg if f.endswith('.env')]
    compose = data.get('compose') or (yamls[0] if len(yamls) == 1 else None)
    env = data.get('env') or (envs[0] if len(envs) == 1 else None)
    if compose not in yamls:
        raise ValueError('choose the compose file' if yamls else 'the package needs a compose file (.yaml)')
    if env is not None and env not in envs:
        raise ValueError('choose the env file')
    check_compose(pkg[compose])
    if env:
        check_env(pkg[env])
    for fname, text in pkg.items():
        if fname not in (compose, env):
            check_stack_file(fname, text)
    with FILES_LOCK:
        module = st.STACKS_DIR / name
        if module.exists():
            raise FileExistsError(name)
        tmp = st.STACKS_DIR / f'.upload-{name}-{secrets.token_hex(4)}'
        tmp.mkdir(parents=True)
        st.match_owner(tmp)
        for fname, text in pkg.items():
            st.write_lf(tmp / fname, text)
            if fname.endswith('.sh'):
                os.chmod(tmp / fname, 0o755)
        (tmp / st.MANIFEST).write_text(json.dumps({'compose': compose, 'env': env}, indent=2) + '\n', encoding='utf-8')
        st.match_owner(*(tmp / f for f in [*pkg, st.MANIFEST]))
        os.replace(tmp, module)
    rollback.discard(module)        # a new stack does not inherit the point of an old one of that name
    st.drop_record(module)
    schedule.forget(name)
    return {'name': name, 'compose': compose, 'env': env, 'files': sorted(pkg)}


def preview_files(name: str, data: dict) -> dict:
    """What a save would change (unified diffs, now -> new), after the same checks as the save."""
    module, compose_file, env_file = find_module(name)
    compose = clean_text(data.get('composeText'))
    check_compose(compose)
    env = None
    if env_file:
        env = clean_text(data.get('envText'))
        check_env(env)
    extra = incoming_files(data, compose_file, env_file)

    def udiff(fname: str | None, new: str | None) -> str:
        if not fname:
            return ''
        old = (module / fname).read_text(encoding='utf-8', errors='replace') if (module / fname).is_file() else ''
        return '\n'.join(difflib.unified_diff(old.splitlines(), new.splitlines(), f'now: {fname}', f'new: {fname}',
                                              lineterm=''))

    return {'compose': udiff(compose_file, compose), 'env': udiff(env_file, env),
            'files': {n: udiff(n, text) for n, text in sorted(extra.items())}}


HISTORY_NAME = re.compile(r'^(.+)\.(\d{8}T\d{6}Z)(?:-(\d+))?$')


def history(name: str) -> list[dict]:
    """Every saved version of the stack's compose, env and other files (also of a file deleted since), newest
    first: [{version, file, time}]."""
    module, compose_file, env_file = find_module(name)
    folder = module / '.history'
    found = []
    for p in folder.iterdir() if folder.is_dir() else []:
        m = HISTORY_NAME.match(p.name)
        if not m or not p.is_file():
            continue
        fname = m.group(1)
        if fname not in (compose_file, env_file) and not st.is_stack_file_name(fname):
            continue
        t = datetime.strptime(m.group(2), '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
        found.append((t, int(m.group(3) or 1), {'version': p.name, 'file': fname, 'time': t.isoformat()}))
    found.sort(key=lambda x: x[:2], reverse=True)
    return [x[2] for x in found]


def history_entry(name: str, version: str) -> tuple[Path, dict]:
    """The module directory and the history entry of version; only a listed version (no path tricks)."""
    entry = next((h for h in history(name) if h['version'] == version), None)
    if entry is None:
        raise ValueError(f'no saved version "{version}"')
    return find_module(name)[0], entry


def history_version(name: str, version: str) -> dict:
    """One saved version and its diff from the current file (what Restore would bring back)."""
    module, entry = history_entry(name, version)
    text = (module / '.history' / version).read_text(encoding='utf-8')
    current = (module / entry['file']).read_text(encoding='utf-8') if (module / entry['file']).is_file() else ''
    d = '\n'.join(difflib.unified_diff(current.splitlines(), text.splitlines(), f'now: {entry["file"]}', version,
                                       lineterm=''))
    return {'file': entry['file'], 'time': entry['time'], 'text': text, 'diff': d}


def restore(name: str, data: dict) -> dict:
    """Put a saved version back (checked like a save); the current file goes into .history/ first."""
    _, compose_file, env_file = find_module(name)
    module, entry = history_entry(name, str(data.get('version') or ''))
    text = clean_text((module / '.history' / entry['version']).read_text(encoding='utf-8'))
    if entry['file'] == compose_file:
        check_compose(text)
    elif entry['file'] == env_file:
        check_env(text)
    else:
        check_stack_file(entry['file'], text)
    path = module / entry['file']
    with FILES_LOCK:
        if entry['file'] not in (compose_file, env_file):
            pin_manifest(module, compose_file, env_file)
        if st.read_plain(path) != text.encode('utf-8'):
            write_file(path, text)
    return {'restored': entry['file']}
