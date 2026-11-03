"""Checks made before a stack is deployed, from its compose file, its env file and what runs on the host.

check() returns {'errors': [...], 'warnings': [...]}. An error is a deploy that would fail (an external network that does
not exist, a host port another container holds); a warning is something likely to go wrong (an image name without a
registry, a variable with no value). The compose file is read the way podman-compose reads it: the env file's values
and the ${VAR:-default} forms are applied first. Anything that does not parse is left to podman-compose to report."""
from __future__ import annotations

import re

import yaml

WILD = {'', '0.0.0.0', '::'}
VAR = re.compile(r'\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?+])([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)')
PORT = re.compile(r'^(?:(?P<ip>\[[^\]]+\]|[\d.]+):)?(?P<host>\d+):\d+(?:-\d+)?$')


def parse_env(text: str | None) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (text or '').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.removeprefix('export ').partition('=')
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        else:
            value = re.split(r'\s+#', value, maxsplit=1)[0].strip()
        values[key.strip()] = value
    return values


def _interpolate(value: str, env: dict[str, str], unset: dict[str, None]) -> str:
    def one(m: re.Match) -> str:
        if m.group(0) == '$$':
            return '$'
        name, op, arg = (m.group(1), m.group(2), m.group(3)) if m.group(1) else (m.group(4), None, None)
        have = name in env
        if op == ':-':
            return env[name] if have and env[name] else arg
        if op == '-':
            return env[name] if have else arg
        if op == ':+':
            return arg if have and env[name] else ''
        if op == '+':
            return arg if have else ''
        if not have:
            unset[name] = None   # a plain ${NAME}, or ${NAME:?error} / ${NAME?error}: nothing to fall back on
        return env.get(name, '')
    return VAR.sub(one, value)


def _resolve(node, env: dict[str, str], unset: dict[str, None]):
    if isinstance(node, str):
        return _interpolate(node, env, unset)
    if isinstance(node, list):
        return [_resolve(x, env, unset) for x in node]
    if isinstance(node, dict):
        return {k: _resolve(v, env, unset) for k, v in node.items()}
    return node


def _cname(c: dict) -> str:
    return (c.get('Names') or ['?'])[0].lstrip('/')


def _external_networks(data: dict) -> list[str]:
    declared = data.get('networks')
    names = []
    for key, spec in (declared.items() if isinstance(declared, dict) else []):
        if not isinstance(spec, dict) or not spec.get('external'):
            continue
        external = spec.get('external')
        name = spec.get('name') or (external.get('name') if isinstance(external, dict) else None) or key
        names.append(str(name))
    return names


def _published(service: dict) -> list[tuple[str, int, str]]:
    """(host address, host port, protocol) of every port the service publishes with a single host port."""
    found = []
    for item in service.get('ports') or []:
        ip, port, proto = '', 0, 'tcp'
        if isinstance(item, dict):
            ip, proto = str(item.get('host_ip') or ''), str(item.get('protocol') or 'tcp').lower()
            port = int(item['published']) if str(item.get('published') or '').isdigit() else 0
        elif isinstance(item, str):
            spec, _, p = item.partition('/')
            m = PORT.match(spec.strip())
            if m:
                ip, port, proto = (m.group('ip') or '').strip('[]'), int(m.group('host')), (p or 'tcp').lower()
        if port:
            found.append((ip, port, proto))
    return found


def _holds(containers: list[dict]) -> list[tuple[str, int, str, str]]:
    held = []
    for c in containers:
        if c.get('State') != 'running':
            continue
        for p in c.get('Ports') or []:
            if int(p.get('host_port') or 0) > 0:
                held.append((str(p.get('host_ip') or ''), int(p['host_port']), str(p.get('protocol') or 'tcp').lower(), _cname(c)))
    return held


def _short_image(image: str) -> str | None:
    """The full name to write, when the image has no registry in front of it."""
    ref = image.split('@', 1)[0]
    first = ref.split('/', 1)[0] if '/' in ref else ''
    if first and ('.' in first or ':' in first or first == 'localhost'):
        return None
    return ('docker.io/' if '/' in ref else 'docker.io/library/') + ref


def check(compose_text: str, env_text: str | None, containers: list[dict], networks: list[str]) -> dict:
    """containers: the containers that are not this stack's own; networks: the names of the host's networks."""
    errors: list[str] = []
    warnings: list[str] = []
    try:
        data = yaml.safe_load(compose_text)
    except yaml.YAMLError:
        return {'errors': errors, 'warnings': warnings}
    if not isinstance(data, dict):
        return {'errors': errors, 'warnings': warnings}
    unset: dict[str, None] = {}
    data = _resolve(data, parse_env(env_text), unset)

    for name in _external_networks(data):
        if name not in networks:
            errors.append(f'the external network "{name}" does not exist: create it first (Networks)')

    held = _holds(containers)
    services = data.get('services') if isinstance(data.get('services'), dict) else {}
    for service in services.values():
        if not isinstance(service, dict):
            continue
        for ip, port, proto in _published(service):
            for h_ip, h_port, h_proto, owner in held:
                if h_port == port and h_proto == proto and (ip in WILD or h_ip in WILD or ip == h_ip):
                    msg = f'port {port}/{proto} is already used by the container "{owner}"'
                    if msg not in errors:
                        errors.append(msg)
        image = str(service.get('image') or '')
        if image and service.get('pull_policy') != 'never' and 'build' not in service:
            full = _short_image(image)
            if full:
                warnings.append(f'the image "{image}" has no registry in front of it, so Podman may not find it: '
                                f'write "{full}"')
    for name in unset:
        warnings.append(f'${{{name}}} has no value: the compose file uses it without a default and the env file '
                        f'does not set it')
    return {'errors': errors, 'warnings': warnings}
