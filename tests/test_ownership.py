import importlib
from pathlib import Path

from tests.support import Base, compose_yaml, make_stack
from stack_ui import podman_api as pa, server as srv, stacks as st


class OwnerTest(Base):
    """stack-ui runs as root; what it writes into the stacks directory keeps the owner of its parent (SMB)."""

    def setUp(self):
        super().setUp()
        self.ui = importlib.reload(srv)
        self.chowned: list[str] = []
        self.patch(st, '_is_root', lambda: True)
        self.patch(st, 'chown', lambda path, uid, gid: self.chowned.append(Path(path).name))

    def test_save_keeps_owner(self):
        make_stack(self.stacks, 'web', compose_yaml('web'))
        self.ui.save_files('web', {'composeText': compose_yaml('web'), 'envText': 'TZ=UTC\n'})
        self.assertIn('.history', self.chowned)
        self.assertIn('..env.tmp', self.chowned)
        self.assertTrue(any(n.startswith('.env.') for n in self.chowned))

    def test_upload_keeps_owner(self):
        self.ui.upload({'name': 'web', 'files': {'compose.yaml': compose_yaml('web'), '.env': 'TZ=UTC\n'}})
        self.assertTrue(any(n.startswith('.upload-web-') for n in self.chowned))
        self.assertTrue({'compose.yaml', '.env', 'stack.json'} <= set(self.chowned))

    def test_nothing_changes_when_not_root(self):
        self.patch(st, '_is_root', lambda: False)
        make_stack(self.stacks, 'web', compose_yaml('web'))
        self.ui.save_files('web', {'composeText': compose_yaml('web'), 'envText': 'TZ=UTC\n'})
        self.assertEqual(self.chowned, [])
