"""The page's own logic, run in Node (tests/page_logic.js): what tests/test_page.py cannot see by looking for strings
(when memory near the limit warns, what a tag says, that an error message is escaped, which route a hash is).
Skipped where Node is not installed; the CI installs it before the tests."""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support import ROOT


def page_script() -> str:
    html = (ROOT / 'stack_ui' / 'page.html').read_text(encoding='utf-8')
    js = re.search(r'<script>(.*)</script>', html, re.S).group(1)
    return js.replace("'__UI_TOKEN__'", "''").replace('__AUTH__', 'false').replace("'__SOURCE__'", "''")


@unittest.skipUnless(shutil.which('node'), 'needs Node')
class PageLogicTest(unittest.TestCase):
    def test_the_page_logic(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'page.js'
            script.write_bytes(page_script().encode('utf-8'))
            r = subprocess.run(['node', str(ROOT / 'tests' / 'page_logic.js'), str(script)],
                               capture_output=True, timeout=60)
        out = r.stdout.decode('utf-8', 'replace') + r.stderr.decode('utf-8', 'replace')
        self.assertEqual(r.returncode, 0, out)
        self.assertRegex(out, r'\nOK \d+\s*$')


if __name__ == '__main__':
    unittest.main()
