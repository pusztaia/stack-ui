import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from tests.support import ROOT

spec = importlib.util.spec_from_file_location('release_notes', ROOT / 'tools' / 'release-notes.py')
rn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rn)

CHANGELOG = '''# Changelog

## 1.0.3

- three, line one
  line two

## 1.0.2-rc1

- a release candidate

## 1.0.2

- two

## 1.0.1

- one
'''


class ReleaseNotesTest(unittest.TestCase):
    def test_the_section_of_a_version_is_its_text_up_to_the_next_one(self):
        self.assertEqual(rn.section(CHANGELOG, '1.0.2'), '- two')
        self.assertEqual(rn.section(CHANGELOG, '1.0.3'), '- three, line one\n  line two')
        self.assertEqual(rn.section(CHANGELOG, '1.0.1'), '- one')
        self.assertEqual(rn.section(CHANGELOG, '1.0.2-rc1'), '- a release candidate')

    def test_a_version_without_a_section_is_an_error(self):
        for version in ('1.0.4', '1.0', '1.0.', ''):
            with self.assertRaises(LookupError, msg=version):
                rn.section(CHANGELOG, version)

    def test_an_empty_section_is_an_error(self):
        with self.assertRaises(LookupError):
            rn.section('## 1.0.5\n\n## 1.0.4\n\n- x\n', '1.0.5')

    def test_the_tag_names_the_version(self):
        self.assertEqual(rn.version('v1.0.27'), '1.0.27')
        self.assertEqual(rn.version('refs/tags/v1.0.2-rc1'), '1.0.2-rc1')
        for bad in ('1.0.27', 'v1', 'vx.y.z', 'refs/heads/main'):
            with self.assertRaises(ValueError, msg=bad):
                rn.version(bad)

    def test_the_command_writes_the_notes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            log, out = Path(tmp, 'CHANGELOG.md'), Path(tmp, 'notes.md')
            log.write_text(CHANGELOG, encoding='utf-8')
            r = subprocess.run([sys.executable, str(ROOT / 'tools' / 'release-notes.py'), 'v1.0.2', str(log), str(out)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(out.read_text(encoding='utf-8'), '- two\n')
            r = subprocess.run([sys.executable, str(ROOT / 'tools' / 'release-notes.py'), 'v9.9.9', str(log), str(out)],
                               capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('9.9.9', r.stderr)

    def test_the_current_version_has_its_section(self):
        from stack_ui import __version__
        text = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
        self.assertTrue(rn.section(text, __version__))

    def test_the_release_workflow_publishes_the_notes_after_the_image(self):
        jobs = yaml.safe_load((ROOT / '.github' / 'workflows' / 'release.yml').read_text(encoding='utf-8'))['jobs']
        notes = jobs['notes']
        self.assertEqual(notes['needs'], 'image')
        self.assertEqual(notes['permissions'], {'contents': 'write'})
        script = '\n'.join(str(s.get('run', '')) for s in notes['steps'])
        self.assertIn('tools/release-notes.py', script)
        self.assertIn('gh release', script)


if __name__ == '__main__':
    unittest.main()
