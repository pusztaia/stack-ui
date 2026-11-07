import json
import re
import shutil
import subprocess
import unittest

from tests.support import ROOT


@unittest.skipUnless(shutil.which('node'), 'needs node (the CI job has it)')
class LogDedupeTest(unittest.TestCase):
    """The Logs page skips the lines a reconnect replays, never the live flow: run the page's own code in node."""

    @classmethod
    def setUpClass(cls):
        page = (ROOT / 'stack_ui' / 'page.html').read_text(encoding='utf-8')
        m = re.search(r'// <log-dedupe>\n(.*?)// </log-dedupe>', page, re.S)
        assert m, 'the page has no // <log-dedupe> block'
        cls.source = m.group(1)

    def run_js(self, body: str):
        script = self.source + '\n' + body
        r = subprocess.run(['node', '-e', f'const out = (() => {{ {script} }})(); console.log(JSON.stringify(out));'],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_the_live_flow_keeps_identical_lines_of_one_second(self):
        out = self.run_js("const d = newLogDedupe(); return [d.accept('t1 ping'), d.accept('t1 ping'), d.accept('t1 ping')];")
        self.assertEqual(out, [True, True, True])

    def test_a_replay_skips_what_was_seen_at_the_last_time_stamp_and_then_ends(self):
        out = self.run_js("""
            const d = newLogDedupe();
            d.accept('t1 a'); d.accept('t2 b');
            d.startReplay();
            return [d.accept('t2 b'), d.accept('t3 c'), d.accept('t3 c'), d.stamp()];""")
        self.assertEqual(out, [False, True, True, 't3'])

    def test_the_replay_counts_repeats(self):
        out = self.run_js("""
            const d = newLogDedupe();
            d.accept('t2 b'); d.accept('t2 b');
            d.startReplay();
            return [d.accept('t2 b'), d.accept('t2 b'), d.accept('t2 b')];""")
        self.assertEqual(out, [False, False, True])           # the third is a new, identical line

    def test_a_new_line_at_the_same_stamp_ends_the_replay_and_later_repeats_show(self):
        out = self.run_js("""
            const d = newLogDedupe();
            d.accept('t2 b');
            d.startReplay();
            return [d.accept('t2 c'), d.accept('t2 b')];""")
        self.assertEqual(out, [True, True])

    def test_a_replay_with_nothing_seen_skips_nothing(self):
        out = self.run_js("const d = newLogDedupe(); d.startReplay(); return [d.accept('t1 a'), d.accept('t1 a')];")
        self.assertEqual(out, [True, True])

    def test_a_line_without_a_time_stamp_is_still_accepted(self):
        out = self.run_js("const d = newLogDedupe(); return [d.accept('nospace'), d.stamp()];")
        self.assertEqual(out, [True, ''])
