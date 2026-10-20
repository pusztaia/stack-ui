#!/usr/bin/env python3
"""Deploy one compose folder through Stack UI's API.

    stack-ui-client.py deploy <folder>             # save its files in Stack UI, then create or update the stack
    stack-ui-client.py deploy <folder> --dry-run   # only show the differences and the plan
    stack-ui-client.py deploy <folder> --no-pull   # do not pull the images first

The folder is the source: its compose and env file (found like Stack UI finds them) are saved in Stack UI (the
previous version goes to .history/ there); a folder Stack UI does not have yet is uploaded (its compose, env,
.sh, .md and .txt files). Then Stack UI deploys it with podman-compose. Containers in a new stack's way are
removed only after a yes here. The stack's name is the folder's name.

STACK_UI_URL (required, e.g. http://my-host:8770) and the API token from the page's Settings: STACK_UI_API_TOKEN,
or --token-file (one line). Python 3.9+, standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

UPLOAD_SUFFIXES = ('.yaml', '.yml', '.env', '.sh', '.md', '.txt')
MANIFEST = 'stack.json'
COMPOSE_NAMES = ('compose.yaml', 'compose.yml', 'docker-compose.yml', 'docker-compose.yaml')
TOKEN_FILE = ''


class ClientError(Exception):
    pass


def stack_files(folder: Path) -> tuple[str, str | None] | None:
    """(compose file, env file or None) of a folder, the same rule as Stack UI's (stacks.py)."""
    manifest = folder / MANIFEST
    if manifest.is_file():
        try:
            m = json.loads(manifest.read_text(encoding='utf-8'))
            compose, env = str(m['compose']), m.get('env')
        except (ValueError, KeyError, TypeError):
            return None
        return (compose, str(env) if env else None) if (folder / compose).is_file() else None
    compose = next((n for n in COMPOSE_NAMES if (folder / n).is_file()), None)
    if compose is None:
        yamls = sorted(p.name for p in folder.iterdir() if p.is_file() and p.suffix in ('.yaml', '.yml'))
        if len(yamls) != 1:
            return None
        compose = yamls[0]
    if (folder / '.env').is_file():
        return compose, '.env'
    envs = sorted(p.name for p in folder.glob('*.env') if p.is_file())
    return compose, envs[0] if len(envs) == 1 else None


def token() -> str:
    t = os.environ.get('STACK_UI_API_TOKEN', '').strip()
    if not t and TOKEN_FILE and Path(TOKEN_FILE).is_file():
        t = (Path(TOKEN_FILE).read_text(encoding='utf-8').strip().splitlines() or [''])[0].strip()
    if not t:
        raise ClientError("no API token: generate one in the page's Settings and pass it as STACK_UI_API_TOKEN "
                          'or --token-file')
    return t


def call(method: str, path: str, body: dict | None = None, timeout: float = 2400) -> dict:
    base = os.environ.get('STACK_UI_URL', '').rstrip('/')
    if not base:
        raise ClientError('set STACK_UI_URL, e.g. http://my-host:8770')
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={'Authorization': f'Bearer {token()}', 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read().decode('utf-8')).get('error') or ''
        except ValueError:
            message = ''
        raise ClientError(f'Stack UI {method} {path}: HTTP {exc.code} {message}'.strip()) from None
    except (urllib.error.URLError, OSError) as exc:
        raise ClientError(f'Stack UI at {base} cannot be reached: {exc}') from None


def package(folder: Path) -> dict[str, str]:
    """The folder's files Stack UI accepts in an upload."""
    return {p.name: p.read_text(encoding='utf-8') for p in sorted(folder.iterdir())
            if p.is_file() and p.name.endswith(UPLOAD_SUFFIXES) and (p.name == '.env' or not p.name.startswith('.'))}


def ask_terminal() -> bool:
    if not sys.stdin.isatty():
        print('  Not asked (no terminal), so it stays.')
        return False
    return input('  Stop and remove it now? [y/N] ').strip().lower() in ('y', 'yes')


def deploy(folder: Path, *, dry_run: bool = False, pull: bool = True, log=print, ask=None) -> bool:
    """Save the folder's files in Stack UI and deploy them; True when the deploy finished."""
    folder = Path(folder)
    files = stack_files(folder) if folder.is_dir() else None
    if files is None:
        raise ClientError(f'{folder} is not a compose folder (no compose file found)')
    compose_file, env_file = files
    name = folder.name
    n = quote(name, safe='')
    texts = {'composeText': (folder / compose_file).read_text(encoding='utf-8'),
             'envText': (folder / env_file).read_text(encoding='utf-8') if env_file else None}
    if not any(s['name'] == name for s in call('GET', '/api/stacks')['stacks']):
        if dry_run:
            log(f'would upload "{name}" as a new stack, then create it')
            return True
        call('POST', '/api/stacks', {'name': name, 'files': package(folder), 'compose': compose_file, 'env': env_file})
        log(f'uploaded "{name}"')
    elif dry_run:
        d = call('POST', f'/api/stacks/{n}/files/preview', texts)
        log(d['compose'] or f'{compose_file}: the same in Stack UI')
        if env_file:
            log(d['env'] or f'{env_file}: the same in Stack UI')
    else:
        saved = call('POST', f'/api/stacks/{n}/files', texts)['saved']
        log('saved: ' + (', '.join(saved) or 'nothing changed'))
    p = call('GET', f'/api/stacks/{n}/plan')
    log(f'{p["action"]} with podman-compose')
    if p.get('missingSecrets'):
        raise ClientError('missing Podman secrets: ' + ', '.join(p['missingSecrets']) + ' (create them on the page, Secrets)')
    blocking = p.get('blocking') or []
    for c in blocking:
        log(f'  in the way: "{c["name"]}" ({c.get("image")}, {c.get("state")}); its data on the host is not touched')
    if dry_run:
        return True
    if blocking and not (ask or ask_terminal)():
        raise ClientError('not deployed: the containers in its way are still there')
    r = call('POST', f'/api/stacks/{n}/deploy', {'pull': pull, 'remove': [c['name'] for c in blocking]})
    for line in r.get('log') or []:
        log(line)
    return bool(r.get('ok'))


def main(argv: list[str] | None = None) -> int:
    global TOKEN_FILE
    parser = argparse.ArgumentParser(description='Deploy a compose folder through Stack UI.')
    sub = parser.add_subparsers(dest='command', required=True)
    d = sub.add_parser('deploy', help='save the folder in Stack UI and create or update its stack')
    d.add_argument('folder', type=Path)
    d.add_argument('--dry-run', action='store_true', help='only show the differences and the plan')
    d.add_argument('--no-pull', action='store_true', help='do not pull the images first')
    d.add_argument('--token-file', default='', help='a file holding the API token (one line)')
    args = parser.parse_args(argv)
    TOKEN_FILE = args.token_file
    try:
        ok = deploy(args.folder, dry_run=args.dry_run, pull=not args.no_pull)
    except ClientError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\ninterrupted', file=sys.stderr)
        return 130
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
