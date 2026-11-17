"""tools/bump.py X.Y.Z: the version bump of a release in one step. The changelog's "## Unreleased" becomes
"## X.Y.Z" and stack_ui/__init__.py says X.Y.Z; refused (nothing written) when there is nothing to release, the
version does not go up, or it is not X.Y.Z."""
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.support import ROOT

spec = importlib.util.spec_from_file_location('bump', ROOT / 'tools' / 'bump.py')
bump = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bump)

CHANGELOG = '''# Changelog

## Unreleased

- *A thing.* It does more now.

## 1.0.40

- *Activity.* The log.
'''


class BumpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / 'stack_ui').mkdir()
        self.init = self.tmp / 'stack_ui' / '__init__.py'
        self.changelog = self.tmp / 'CHANGELOG.md'
        self.write(CHANGELOG, "\"\"\"Stack UI.\"\"\"\n__version__ = '1.0.40'\n")

    def write(self, changelog, init):
        self.changelog.write_bytes(changelog.encode('utf-8'))
        self.init.write_bytes(init.encode('utf-8'))

    def files(self):
        return self.changelog.read_bytes().decode('utf-8'), self.init.read_bytes().decode('utf-8')

    def test_the_bump(self):
        self.assertEqual(bump.main(['1.0.41'], root=self.tmp), 0)
        changelog, init = self.files()
        self.assertIn('\n## 1.0.41\n\n- *A thing.*', changelog)
        self.assertNotIn('Unreleased', changelog)
        self.assertIn('## 1.0.40', changelog)
        self.assertEqual(init, "\"\"\"Stack UI.\"\"\"\n__version__ = '1.0.41'\n")
        self.assertNotIn(b'\r', self.changelog.read_bytes())         # LF, also on Windows

    def test_refusals_write_nothing(self):
        cases = [
            (['v1.0.41'], CHANGELOG, 'X.Y.Z'),
            (['1.0'], CHANGELOG, 'X.Y.Z'),
            (['1.0.40'], CHANGELOG, 'higher than 1.0.40'),
            (['1.0.39'], CHANGELOG, 'higher than 1.0.40'),
            (['1.0.41'], CHANGELOG.replace('## Unreleased\n\n- *A thing.* It does more now.\n\n', ''), 'Unreleased'),
            (['1.0.41'], CHANGELOG.replace('- *A thing.* It does more now.\n', ''), 'empty'),
            (['1.0.41'], CHANGELOG + '\n## Unreleased\n\n- twice\n', 'Unreleased'),
            ([], CHANGELOG, 'usage'),
        ]
        for args, changelog, text in cases:
            self.write(changelog, "__version__ = '1.0.40'\n")
            before = self.files()
            errors = []
            self.assertEqual(bump.main(args, root=self.tmp, say=errors.append), 1 if args else 2, args)
            self.assertEqual(self.files(), before, args)
            self.assertIn(text, ' '.join(errors), (args, errors))

    def test_a_minor_or_major_step_is_fine(self):
        for version in ('1.1.0', '2.0.0'):
            self.write(CHANGELOG, "__version__ = '1.0.40'\n")
            self.assertEqual(bump.main([version], root=self.tmp), 0, version)

    def test_the_real_checkout_parses(self):
        """The repository's own files are in the form the tool reads (nothing is written)."""
        self.assertRegex(bump.current_version(ROOT), r'^\d+\.\d+\.\d+$')


if __name__ == '__main__':
    unittest.main()
