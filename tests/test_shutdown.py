import os
import signal
import subprocess
import sys
import time
import unittest

from tests.support import ROOT, Base


@unittest.skipIf(os.name == 'nt', 'SIGTERM is a hard kill on Windows: this runs on Linux')
class ShutdownTest(Base):
    def start(self):
        env = {**os.environ, 'STACKS_DIR': str(self.stacks), 'STACK_UI_DATA': str(self.tmp / 'data'),
               'PODMAN_SOCKET': str(self.tmp / 'none.sock'), 'PYTHONPATH': str(ROOT)}
        proc = subprocess.Popen([sys.executable, '-m', 'stack_ui', '--no-browser', '--port', '0'], cwd=ROOT, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(proc.kill)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.stderr.close)
        line = proc.stdout.readline()          # printed (and flushed) once the server listens
        self.assertIn('Stack UI', line)
        return proc

    def test_sigterm_stops_it_at_once_with_exit_code_0(self):
        proc = self.start()
        proc.send_signal(signal.SIGTERM)
        started = time.time()
        self.assertEqual(proc.wait(timeout=5), 0)
        self.assertLess(time.time() - started, 3)
