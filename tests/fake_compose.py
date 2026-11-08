"""Stands in for podman-compose in the tests: records its arguments and working directory, prints a line, and
exits 3 when FAKE_COMPOSE_FAIL_ON names one of its arguments (e.g. "up").

FAKE_COMPOSE_HANG="up:30" makes it sleep 30 s on that command, with a sleeping child of its own (like the podman
processes the real one starts); FAKE_COMPOSE_PIDS names a file that gets "<pid> <child pid>" appended;
FAKE_COMPOSE_IGNORE_TERM=1 makes both ignore SIGTERM (only SIGKILL ends them)."""
import json
import os
import signal
import subprocess
import sys
import time

log = os.environ.get('FAKE_COMPOSE_LOG')
if log:
    with open(log, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}) + '\n')

hang_command, _, hang_seconds = os.environ.get('FAKE_COMPOSE_HANG', '').partition(':')
if hang_command and hang_command in sys.argv[1:]:
    ignore = bool(os.environ.get('FAKE_COMPOSE_IGNORE_TERM'))
    if ignore:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([sys.executable, '-c', 'import signal, time\n'
                              + ('signal.signal(signal.SIGTERM, signal.SIG_IGN)\n' if ignore else '')
                              + f'time.sleep({float(hang_seconds)})'],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # (the child must not keep the parent's output pipe open: without a process group, as on Windows, the caller
    # would otherwise wait for it after the parent was killed)
    pids = os.environ.get('FAKE_COMPOSE_PIDS')
    if pids:
        with open(pids, 'a', encoding='utf-8') as fh:
            fh.write(f'{os.getpid()} {child.pid}\n')
    time.sleep(float(hang_seconds))

orphan_command, _, orphan_seconds = os.environ.get('FAKE_COMPOSE_ORPHAN', '').partition(':')
if orphan_command and orphan_command in sys.argv[1:]:
    # FAKE_COMPOSE_ORPHAN="up:25": the leader exits at once, a child that keeps our output pipe sleeps 25 s
    child = subprocess.Popen([sys.executable, '-c', f'import time\ntime.sleep({float(orphan_seconds)})'])
    pids = os.environ.get('FAKE_COMPOSE_PIDS')
    if pids:
        with open(pids, 'a', encoding='utf-8') as fh:
            fh.write(f'{os.getpid()} {child.pid}\n')
    print('fake podman-compose', ' '.join(sys.argv[1:]), flush=True)
    os._exit(0)

print('fake podman-compose', ' '.join(sys.argv[1:]))
fail_on = os.environ.get('FAKE_COMPOSE_FAIL_ON', '')
sys.exit(3 if fail_on and fail_on in sys.argv[1:] else 0)
