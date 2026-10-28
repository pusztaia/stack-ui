import re
import unittest
from pathlib import Path

from tests.support import ROOT


class Python39Test(unittest.TestCase):
    """The code runs on Python 3.9: the newline argument of Path.write_text only exists from 3.10."""

    def test_no_write_text_newline(self):
        offenders = []
        for path in [*ROOT.glob('stack_ui/*.py'), *ROOT.glob('client/*.py'), *ROOT.glob('tools/*.py'), *ROOT.glob('tests/*.py')]:
            for n, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                if re.search(r'write_text\([^)]*newline=', line):
                    offenders.append(f'{path.name}:{n}')
        self.assertEqual(offenders, [])
