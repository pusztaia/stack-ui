"""Shared test helpers: the package on sys.path, stack folders in a temp directory, the mocks."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stack_ui import podman_api as pa  # noqa: E402
from tests.mock_libpod import MockPodman  # noqa: E402

FAKE_COMPOSE = Path(__file__).resolve().parent / 'fake_compose.py'


def compose_yaml(container: str, secrets: tuple[str, ...] = ()) -> str:
    text = f'services:\n  app:\n    image: docker.io/library/nginx:latest\n    container_name: {container}\n'
    if secrets:
        text += '    secrets:\n' + ''.join(f'      - {s}\n' for s in secrets)
        text += 'secrets:\n' + ''.join(f'  {s}:\n    external: true\n' for s in secrets)
    return text


def make_stack(base: Path, name: str, compose: str, env: str | None = 'TZ=Europe/Budapest\n',
               compose_name: str = 'compose.yaml', env_name: str = '.env') -> Path:
    folder = base / name
    folder.mkdir(parents=True)
    (folder / compose_name).write_bytes(compose.encode('utf-8'))
    if env is not None:
        (folder / env_name).write_bytes(env.encode('utf-8'))
    return folder


SERVER_PARTS = ('core', 'backups', 'auth', 'stackfiles', 'registry', 'usage', 'image_ops', 'podman_objects', 'stackops',
                'autoupdate', 'server')


def fresh_server():
    """The server with every part of it loaded again, in order: each test starts from clean state (no running
    change, no sessions, no registry answers), as it did when the server was one module."""
    import importlib
    from stack_ui import server  # noqa: F401 - imports them all
    for name in SERVER_PARTS:
        module = importlib.reload(importlib.import_module(f'stack_ui.{name}'))
    return module


class Base(unittest.TestCase):
    """A temp STACKS_DIR, the mock libpod API and the fake podman-compose."""

    def setUp(self):
        from stack_ui import stacks as st  # imported here: test_podman_api runs without it
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stacks = self.tmp / 'stacks'
        self.stacks.mkdir()
        self.mock = MockPodman()
        url = self.mock.start()
        self.addCleanup(self.mock.stop)
        self.patch(pa, 'API_URL', url)
        self.patch(st, 'STACKS_DIR', self.stacks)
        self.patch(st, 'IGNORE', set())
        self.patch(st, 'DATA', self.tmp / 'data')
        # the real data folder is never written: the server parts read these when they are loaded again
        # (fresh_server), and the ones loaded already are pointed here too
        for var, value in (('STACK_UI_DATA', self.tmp / 'data'),
                           ('STACK_UI_SETTINGS_FILE', self.tmp / 'data' / 'settings.json')):
            if var in os.environ:
                self.addCleanup(os.environ.__setitem__, var, os.environ[var])
            else:
                self.addCleanup(os.environ.pop, var, None)
            os.environ[var] = str(value)
        from stack_ui import auth, core
        self.patch(auth, 'SETTINGS_FILE', self.tmp / 'data' / 'settings.json')
        self.patch(core, 'DATA', self.tmp / 'data')
        from stack_ui import rollback
        self.patch(rollback, 'DATA', self.tmp / 'data')
        from stack_ui import schedule
        self.patch(schedule, 'DATA', self.tmp / 'data')
        self.patch(schedule, 'AUTOSTART', False)
        schedule.reset()
        self.addCleanup(schedule.reset)
        self.patch(st, 'COMPOSE_CMD', ' '.join(shlex.quote(Path(x).as_posix()) for x in (sys.executable, FAKE_COMPOSE)))
        self.compose_log = self.tmp / 'compose.log'
        os.environ['FAKE_COMPOSE_LOG'] = str(self.compose_log)
        os.environ.pop('FAKE_COMPOSE_FAIL_ON', None)
        self.addCleanup(os.environ.pop, 'FAKE_COMPOSE_LOG', None)
        self.addCleanup(os.environ.pop, 'FAKE_COMPOSE_FAIL_ON', None)

    def patch(self, obj, name, value):
        old = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(setattr, obj, name, old)

    def compose_calls(self) -> list[list[str]]:
        if not self.compose_log.is_file():
            return []
        return [json.loads(line)['argv'] for line in self.compose_log.read_text(encoding='utf-8').splitlines()]
