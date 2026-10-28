#!/usr/bin/env python3
"""Install (or update) Stack UI on a Podman host over SSH, from Windows PowerShell, macOS or Linux.

    python tools\\install-pi.py ubuntu@192.168.50.3 --dry-run     # only show the plan and the script it would run
    python tools\\install-pi.py ubuntu@192.168.50.3               # install; asks the page's password here
    python tools\\install-pi.py ubuntu@192.168.50.3 --build       # build the image on the host from the repository
    python tools\\install-pi.py ubuntu@192.168.50.3 --update      # pull and recreate an install made by this script

It uses the ssh of your computer (Windows 11 has it); ssh asks for the host's password unless you set up a key.
The page's password is hashed here (PBKDF2, the same format Stack UI checks) and only the hash is sent: the host
keeps it as the Podman secret stack-ui-password. Leave the password empty to keep the secret that is there.
It changes nothing when a container named stack-ui already runs (use --update for an install made by this script,
or update your own compose file the usual way). Python 3.9+, standard library only.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import re
import secrets
import shlex
import shutil
import subprocess
import sys

ITERATIONS = 600_000
REMOTE_SCRIPT = '/tmp/stack-ui-install.sh'
TARGET = re.compile(r'^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+$')
IMAGE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$')
HOSTS = re.compile(r'^[A-Za-z0-9._:,\[\]-]+$')
NETWORKS = re.compile(r'^[A-Za-z0-9._:,/\[\]-]+$')   # also CIDR networks
PATH = re.compile(r'^(/|\$HOME/)[A-Za-z0-9._/-]*$')


class InstallError(Exception):
    pass


def hash_password(password: str) -> str:
    """The line Stack UI's --hash-password prints (pbkdf2_sha256$iterations$salt$digest)."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, ITERATIONS)
    return f'pbkdf2_sha256${ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}'


def render_compose(image: str, bind: str, port: int, hosts: str, proxies: str, hardening: bool) -> str:
    """The compose file; @SOCK@ and @STACKS_DIR@ are filled in on the host (they depend on its Podman and user)."""
    text = f'''# Written by tools/install-pi.py. Edit freely. To update: podman-compose -f compose.yaml pull, then up -d.
services:
  stack-ui:
    image: {image}
    container_name: stack-ui
    restart: always
    ports:
      - "{bind}:{port}:8770/tcp"
    environment:
      STACKS_DIR: @STACKS_DIR@
      STACK_UI_ALLOWED_HOSTS: {hosts}
      STACK_UI_SELF: stack-ui
'''
    if proxies:
        text += f'      STACK_UI_TRUSTED_PROXIES: {proxies}\n'
    text += '''    volumes:
      - @STACKS_DIR@:@STACKS_DIR@
      - stack-ui-data:/data
      - @SOCK@:/run/podman/podman.sock
    secrets:
      - stack-ui-password
    security_opt:
      - no-new-privileges:true
'''
    if hardening:
        text += '''    cap_drop:
      - ALL
    cap_add:
      - CHOWN
      - DAC_OVERRIDE
      - FOWNER
    read_only: true
    tmpfs:
      - /tmp
      - /run/secrets
'''
    text += '''    mem_limit: 192m
    cpus: 0.5
    pids_limit: 256
    logging:
      driver: k8s-file
      options:
        max-size: 5m

volumes:
  stack-ui-data:

secrets:
  stack-ui-password:
    external: true
'''
    return text


REMOTE_TEMPLATE = r'''set -eu
say() { printf '\n== %s\n' "$*"; }
MODE=__MODE__
STACKS_DIR=__STACKS_DIR__
INSTALL_DIR=__INSTALL_DIR__
PWHASH=__PWHASH__
BUILD=__BUILD__
UPDATE=__UPDATE__
BIND=__BIND__
PORT=__PORT__
IMAGE=__IMAGE__
: "${STACKS_DIR:=$HOME/stacks}"
: "${INSTALL_DIR:=$HOME/stack-ui}"
case "$STACKS_DIR" in '$HOME/'*) STACKS_DIR="$HOME/${STACKS_DIR#'$HOME/'}" ;; esac
case "$INSTALL_DIR" in '$HOME/'*) INSTALL_DIR="$HOME/${INSTALL_DIR#'$HOME/'}" ;; esac
command -v podman >/dev/null || { echo 'ERROR: podman is not installed on this machine'; exit 1; }
if [ "$MODE" = auto ]; then
  sys=$(sudo podman ps -q 2>/dev/null | wc -l || true)
  usr=$(podman ps -q 2>/dev/null | wc -l || true)
  if [ "$sys" -gt "$usr" ]; then MODE=rootful; else MODE=rootless; fi
  say "Podman mode: $MODE ($sys containers rootful, $usr rootless; override with --rootful / --rootless)"
fi
if [ "$MODE" = rootful ]; then
  P="sudo podman"; C="sudo podman-compose"; SOCK=/run/podman/podman.sock
else
  P=podman; C=podman-compose; SOCK="/run/user/$(id -u)/podman/podman.sock"
fi
if $P container exists stack-ui 2>/dev/null; then
  if [ "$UPDATE" != 1 ] || [ ! -f "$INSTALL_DIR/compose.yaml" ]; then
    echo "A container named stack-ui already runs here: nothing was changed."
    echo "Update your own install with its compose file (podman-compose pull && up -d), or pass --update for one"
    echo "made by this script ($INSTALL_DIR/compose.yaml)."
    exit 2
  fi
fi
say "Podman socket"
if [ "$MODE" = rootful ]; then
  sudo systemctl enable --now podman.socket
else
  systemctl --user enable --now podman.socket
  loginctl enable-linger "$USER" 2>/dev/null || echo "note: could not enable linger; the socket may stop when you log out"
fi
[ -S "$SOCK" ] || { echo "ERROR: no Podman socket at $SOCK"; exit 1; }
if ! command -v podman-compose >/dev/null; then
  printf 'podman-compose is missing. Install it with apt (sudo)? [y/N] '; read -r answer
  [ "$answer" = y ] || { echo 'podman-compose is needed: nothing more was done.'; exit 1; }
  sudo apt-get install -y podman-compose
fi
say "Folders"
mkdir -p "$STACKS_DIR" "$INSTALL_DIR"
say "Password"
if [ -n "$PWHASH" ]; then
  $P secret rm stack-ui-password >/dev/null 2>&1 || true
  printf '%s\n' "$PWHASH" | $P secret create stack-ui-password - >/dev/null
  echo "the Podman secret stack-ui-password was set"
else
  $P secret inspect stack-ui-password >/dev/null 2>&1 || { echo 'ERROR: no stack-ui-password secret yet: run again and give a password'; exit 1; }
  echo "kept the existing secret stack-ui-password"
fi
say "Compose file"
if [ -f "$INSTALL_DIR/compose.yaml" ]; then cp "$INSTALL_DIR/compose.yaml" "$INSTALL_DIR/compose.yaml.bak-$(date +%Y%m%d%H%M%S)"; fi
cat > "$INSTALL_DIR/compose.yaml" <<COMPOSE_EOF
__COMPOSE__
COMPOSE_EOF
echo "$INSTALL_DIR/compose.yaml (socket $SOCK, stacks $STACKS_DIR)"
cd "$INSTALL_DIR"
if [ "$BUILD" = 1 ]; then
  say "Build the image here"
  if [ -d src/.git ]; then git -C src pull --ff-only; else git clone --depth 1 https://github.com/pusztaia/stack-ui.git src; fi
  $P build -t "$IMAGE" src
else
  case "$IMAGE" in
    localhost/*) echo "$IMAGE is a local image: no pull" ;;
    *) say "Pull the image"; $C -f compose.yaml pull ;;
  esac
fi
say "Start"
$C -f compose.yaml up -d
code=000
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://$BIND:$PORT/session" || true)
  [ "$code" = 200 ] && break
  sleep 1
done
echo "the page answered: HTTP $code"
[ "$code" = 200 ] || { echo "Look at: $P logs stack-ui"; exit 1; }
say "Done: http://$BIND:$PORT/ (plain http: for the LAN put a reverse proxy with https in front)"
'''


def build_remote_script(args, pwhash: str, compose: str) -> str:
    values = {
        '__MODE__': args.mode, '__STACKS_DIR__': shlex.quote(args.stacks_dir or ''),
        '__INSTALL_DIR__': shlex.quote(args.install_dir or ''), '__PWHASH__': shlex.quote(pwhash),
        '__BUILD__': '1' if args.build else '0', '__UPDATE__': '1' if args.update else '0',
        '__BIND__': shlex.quote(args.bind), '__PORT__': str(args.port), '__IMAGE__': shlex.quote(args.image),
    }
    script = REMOTE_TEMPLATE
    for key, value in values.items():
        script = script.replace(key, value)
    # the compose text goes in last: it holds no shell syntax of its own except the two values the host fills in
    compose = compose.replace('@SOCK@', '$SOCK').replace('@STACKS_DIR@', '$STACKS_DIR')
    return script.replace('__COMPOSE__', compose.rstrip('\n'))


def parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description='Install Stack UI on a Podman host over SSH.')
    p.add_argument('target', help='user@host, e.g. ubuntu@192.168.50.3')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--rootful', dest='mode', action='store_const', const='rootful', help='the host runs rootful Podman')
    mode.add_argument('--rootless', dest='mode', action='store_const', const='rootless', help='the host runs rootless Podman')
    p.set_defaults(mode='auto')
    p.add_argument('--stacks-dir', help='the stacks folder on the host (default ~/stacks)')
    p.add_argument('--install-dir', help='where the compose file goes on the host (default ~/stack-ui)')
    p.add_argument('--image', help='default ghcr.io/pusztaia/stack-ui:1 (localhost/stack-ui:dev with --build)')
    p.add_argument('--build', action='store_true', help='build the image on the host from the repository')
    p.add_argument('--update', action='store_true', help='pull and recreate an install made by this script')
    p.add_argument('--bind', help='the host address the page listens on (default: the host of the target)')
    p.add_argument('--loopback', action='store_true', help='listen on 127.0.0.1 only (use an SSH tunnel or a proxy)')
    p.add_argument('--port', type=int, default=8770)
    p.add_argument('--allowed-hosts', default='', help='more names the page is opened as (comma separated)')
    p.add_argument('--trusted-proxies', default='', help='reverse proxy addresses / networks (STACK_UI_TRUSTED_PROXIES)')
    p.add_argument('--no-hardening', action='store_true', help='leave out cap_drop / read_only / tmpfs')
    p.add_argument('--dry-run', action='store_true', help='show the plan and the script; connect to nothing')
    p.add_argument('--yes', action='store_true', help='do not ask "Proceed?"')
    args = p.parse_args(argv)
    host = args.target.split('@', 1)[-1]
    if not TARGET.match(args.target):
        raise InstallError('the target must look like user@host')
    args.bind = '127.0.0.1' if args.loopback else (args.bind or host)
    args.image = args.image or ('localhost/stack-ui:dev' if args.build else 'ghcr.io/pusztaia/stack-ui:1')
    if not 1 <= args.port <= 65535:
        raise InstallError('the port must be 1-65535')
    if not IMAGE.match(args.image):
        raise InstallError('this is not an image reference')
    if not re.match(r'^[A-Za-z0-9._:\[\]-]+$', args.bind):
        raise InstallError('--bind must be an address')
    for name, pattern in (('allowed_hosts', HOSTS), ('trusted_proxies', NETWORKS)):
        if getattr(args, name) and not pattern.match(getattr(args, name)):
            raise InstallError(f'--{name.replace("_", "-")}: only names, addresses (networks) and commas')
    for name in ('stacks_dir', 'install_dir'):
        if getattr(args, name) and not PATH.match(getattr(args, name)):
            raise InstallError(f'--{name.replace("_", "-")} must be an absolute path (or start with $HOME/)')
    hosts = ['127.0.0.1', 'localhost', host if not args.loopback else '', args.bind if args.bind != '127.0.0.1' else '']
    hosts += [h for h in args.allowed_hosts.split(',') if h]
    args.hosts = ','.join(dict.fromkeys(h for h in hosts if h))
    return args


def ask_password() -> str:
    while True:
        first = getpass.getpass("Stack UI page password (at least 10 characters; empty = keep the host's current one): ")
        if not first:
            return ''
        if len(first) < 10:
            print('At least 10 characters.')
            continue
        if getpass.getpass('Again: ') == first:
            return first
        print('The two entries differ.')


def run_ssh(args, remote_args: list[str], stdin: str | None = None, tty: bool = False) -> int:
    cmd = ['ssh', '-o', 'ServerAliveInterval=30', *(['-t'] if tty else []), args.target, *remote_args]
    data = stdin.encode('utf-8') if stdin is not None else None   # bytes: text mode would send \r\n on Windows
    return subprocess.run(cmd, input=data, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse(argv)
    except InstallError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2
    compose = render_compose(args.image, args.bind, args.port, args.hosts, args.trusted_proxies, not args.no_hardening)
    print(f'Stack UI on {args.target}: image {args.image}, page at http://{args.bind}:{args.port}/, '
          f'Podman {args.mode}, {"build here, " if args.build else ""}{"update" if args.update else "install"}')
    if args.dry_run:
        print('\n--- script that would run on the host (the password hash is left out) ---\n')
        print(build_remote_script(args, '', compose))
        return 0
    if not shutil.which('ssh'):
        print('ERROR: no ssh command found (Windows: Settings > Apps > Optional features > OpenSSH Client)', file=sys.stderr)
        return 1
    password = ask_password()
    if not args.yes and input(f'Proceed on {args.target}? [y/N] ').strip().lower() != 'y':
        print('Nothing was done.')
        return 1
    script = build_remote_script(args, hash_password(password) if password else '', compose)
    print('\nUploading the script (ssh may ask for the host password) ...')
    if run_ssh(args, [f'umask 077; cat > {REMOTE_SCRIPT}'], stdin=script):
        print('ERROR: could not reach the host', file=sys.stderr)
        return 1
    print('Running it (ssh may ask again; sudo asks for the host password) ...')
    return run_ssh(args, [f'bash {REMOTE_SCRIPT}; rc=$?; rm -f {REMOTE_SCRIPT}; exit $rc'], tty=True)


if __name__ == '__main__':
    sys.exit(main())
