import contextlib
import importlib.util
import io
import os
import shutil
import stat
import subprocess
import sys
import unittest
from pathlib import Path

import yaml

from tests.support import ROOT, Base
from stack_ui import server as srv

spec = importlib.util.spec_from_file_location('install_pi', ROOT / 'tools' / 'install-pi.py')
ip = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ip)

# the host's side, run with a real bash and stand-ins for sudo, podman, podman-compose, systemctl ...
FAKES = {
    'sudo': '#!/bin/bash\nif [ "$1" = -E ]; then shift; fi\nexec "$@"\n',
    'podman': '#!/bin/bash\necho "podman $*" >> "$FAKE_LOG"\n'
              'case "$1 $2" in\n'
              '  "container exists") [ -n "${FAKE_EXISTS:-}" ] && exit 0 || exit 1;;\n'
              '  "secret inspect") [ -n "${FAKE_SECRET:-}" ] && exit 0 || exit 1;;\n'
              '  "secret create") cat > "$FAKE_DIR/secret-value"; exit 0;;\n'
              'esac\n[ "$1" = ps ] && echo "${FAKE_PS:-}"\nexit 0\n',
    'podman-compose': '#!/bin/bash\necho "compose $*" >> "$FAKE_LOG"\n',
    'systemctl': '#!/bin/bash\necho "systemctl $*" >> "$FAKE_LOG"\n',
    'loginctl': '#!/bin/bash\nexit 0\n',
    'curl': '#!/bin/bash\necho 200\n',
}


class InstallPiTest(Base):
    def args(self, *extra):
        return ip.parse(['ubuntu@192.168.50.3', *extra])

    def test_hash_is_what_stack_ui_checks(self):
        h = ip.hash_password('a-long-password')
        self.assertTrue(srv.check_password('a-long-password', h))
        self.assertFalse(srv.check_password('another-password', h))
        self.assertEqual(h.split('$')[1], str(srv.HASH_ITERATIONS))

    def test_compose_is_valid_yaml_and_matches_the_example(self):
        text = ip.render_compose('ghcr.io/pusztaia/stack-ui:1', '192.168.50.3', 8770, '127.0.0.1,localhost', '', True)
        mine = yaml.safe_load(text.replace('@SOCK@', '/s').replace('@STACKS_DIR@', '/x'))['services']['stack-ui']
        example = yaml.safe_load((ROOT / 'compose.example.yaml').read_text(encoding='utf-8'))['services']['stack-ui']
        for key in ('cap_drop', 'cap_add', 'read_only', 'tmpfs', 'security_opt', 'mem_limit', 'cpus', 'pids_limit',
                    'secrets', 'restart', 'container_name'):
            self.assertEqual(mine[key], example[key], key)
        self.assertEqual(mine['ports'], ['192.168.50.3:8770:8770/tcp'])
        plain = ip.render_compose('i:1', '127.0.0.1', 8770, 'localhost', '', False)
        self.assertNotIn('cap_drop', yaml.safe_load(plain.replace('@SOCK@', '/s').replace('@STACKS_DIR@', '/x'))['services']['stack-ui'])
        # an unquoted heredoc runs backticks and expands $: the text may hold only the two values the host fills in
        self.assertNotIn('`', text)
        self.assertNotIn('$', text.replace('@SOCK@', '').replace('@STACKS_DIR@', ''))

    def test_arguments(self):
        a = self.args()
        self.assertEqual((a.bind, a.image, a.mode, a.hosts), ('192.168.50.3', 'ghcr.io/pusztaia/stack-ui:1', 'auto',
                                                              '127.0.0.1,localhost,192.168.50.3'))
        self.assertEqual(self.args('--loopback').hosts, '127.0.0.1,localhost')
        self.assertEqual(self.args('--build').image, 'localhost/stack-ui:dev')
        self.assertEqual(self.args('--rootful').mode, 'rootful')
        self.assertEqual(self.args('--trusted-proxies', '127.0.0.1,172.18.0.0/16').trusted_proxies, '127.0.0.1,172.18.0.0/16')
        for bad in (['--image', 'x; rm -rf /'], ['--allowed-hosts', 'a b'], ['--trusted-proxies', '$(id)'],
                    ['--stacks-dir', 'relative/dir'], ['--stacks-dir', '/a b'], ['--port', '0'], ['--bind', 'a;b']):
            with self.assertRaises(ip.InstallError, msg=bad):
                self.args(*bad)
        for target in ('nohost', 'a b@c', 'u@h;x', '@h'):
            with self.assertRaises(ip.InstallError, msg=target):
                ip.parse([target])

    def test_dry_run_connects_to_nothing_and_leaves_the_hash_out(self):
        self.patch(ip.subprocess, 'run', lambda *a, **k: self.fail('ssh must not run'))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ip.main(['ubuntu@192.168.50.3', '--dry-run']), 0)
        self.assertIn('podman-compose', out.getvalue())
        self.assertNotIn('pbkdf2', out.getvalue())

    def test_hash_goes_through_stdin_only(self):
        calls = []

        def fake_run(cmd, input=None, check=False):
            calls.append((cmd, input))
            return subprocess.CompletedProcess(cmd, 0)
        self.patch(ip.subprocess, 'run', fake_run)
        self.patch(ip.shutil, 'which', lambda _: 'ssh')
        self.patch(ip, 'ask_password', lambda: 'a-long-password')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ip.main(['ubuntu@192.168.50.3', '--yes']), 0)
        (upload, data), (run, none) = calls
        self.assertIsNone(none)
        self.assertIn(b'pbkdf2_sha256$600000$', data)
        self.assertNotIn(b'a-long-password', data)
        self.assertNotIn(b'\r', data)
        for cmd in (upload, run):
            self.assertNotIn('pbkdf2', ' '.join(cmd))
        self.assertIn('-t', run)
        self.assertNotIn('-t', upload)


@unittest.skipUnless(os.name != 'nt' and shutil.which('bash'), 'the host script needs bash (run on Linux)')
class RemoteScriptTest(Base):
    """Runs the generated script with a real bash and stand-ins for the host's commands."""

    def setUp(self):
        super().setUp()
        self.home = self.tmp / 'home'
        self.bin = self.tmp / 'bin'
        self.fake = self.tmp / 'fake'
        for d in (self.home, self.bin, self.fake):
            d.mkdir()
        for name, body in FAKES.items():
            f = self.bin / name
            f.write_text(body, encoding='utf-8')
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        self.log = self.fake / 'log'
        self.log.write_text('')

    def run_script(self, *argv, pwhash='pbkdf2_sha256$1$c2FsdA==$ZGlnZXN0', **env):
        args = ip.parse(['ubuntu@192.168.50.3', *argv])
        compose = ip.render_compose(args.image, args.bind, args.port, args.hosts, args.trusted_proxies, True)
        text = ip.build_remote_script(args, pwhash, compose)
        guard = '[ -S "$SOCK" ] ||'
        self.assertEqual(text.count(guard), 1)
        text = text.replace(guard, 'true ||')       # a stand-in cannot make /run/podman/podman.sock
        script = self.tmp / 'install.sh'
        with open(script, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text)
        environment = {**os.environ, 'PATH': f'{self.bin}:{os.environ["PATH"]}', 'HOME': str(self.home),
                       'FAKE_LOG': str(self.log), 'FAKE_DIR': str(self.fake), **env}
        return subprocess.run(['bash', str(script)], env=environment, capture_output=True, text=True, timeout=60)

    def test_install_writes_the_compose_file_and_starts_it(self):
        r = self.run_script('--rootful', '--stacks-dir', '$HOME/mystacks', '--trusted-proxies', '172.18.0.0/16')
        text = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, text)
        compose = (self.home / 'stack-ui' / 'compose.yaml').read_text(encoding='utf-8')
        self.assertIn('/run/podman/podman.sock:/run/podman/podman.sock', compose)
        self.assertIn(f'{self.home}/mystacks:{self.home}/mystacks', compose)
        self.assertIn('STACK_UI_TRUSTED_PROXIES: 172.18.0.0/16', compose)
        self.assertEqual((self.fake / 'secret-value').read_text(), 'pbkdf2_sha256$1$c2FsdA==$ZGlnZXN0\n')
        self.assertTrue((self.home / 'mystacks').is_dir())
        log = self.log.read_text()
        self.assertIn('compose -f compose.yaml pull', log)
        self.assertIn('compose -f compose.yaml up -d', log)
        self.assertIn('systemctl enable --now podman.socket', log)

    def test_a_local_image_is_not_pulled(self):
        r = self.run_script('--rootless', '--image', 'localhost/stack-ui:mine')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn('compose -f compose.yaml pull', self.log.read_text())
        self.assertIn('compose -f compose.yaml up -d', self.log.read_text())

    def test_an_existing_stack_ui_container_is_left_alone(self):
        r = self.run_script('--rootless', FAKE_EXISTS='1')
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('nothing was changed', r.stdout)
        self.assertFalse((self.home / 'stack-ui').exists())
        self.assertNotIn('secret create', self.log.read_text())

    def test_no_password_and_no_secret_stops(self):
        r = self.run_script('--rootless', pwhash='')
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.fake / 'secret-value').exists())
