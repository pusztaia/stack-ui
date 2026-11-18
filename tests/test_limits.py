"""The warning thresholds live in one place, health.py: the server uses them, /api/health sends them, and the page
builds its checks and its words from what it got (it has no number of its own)."""
import time
import urllib.request

from tests.support import Base, fresh_server


class LimitsTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = fresh_server()
        from stack_ui import activity, health
        self.act, self.hl = activity, health

    def test_the_health_answer_carries_them(self):
        self.assertEqual(self.hl.health([], 4)['limits'],
                         {'nearLimit': self.hl.NEAR_LIMIT, 'nearHoldSeconds': self.hl.NEAR_HOLD,
                          'loopRestarts': self.hl.LOOP_RESTARTS, 'loopWindowSeconds': self.hl.LOOP_WINDOW})
        self.assertEqual((self.hl.NEAR_LIMIT, self.hl.NEAR_HOLD, self.hl.LOOP_RESTARTS, self.hl.LOOP_WINDOW),
                         (0.9, 30, 3, 600))

    def test_the_watcher_uses_them(self):
        self.assertFalse(hasattr(self.act, 'NEAR_LIMIT'), 'activity keeps its own threshold')
        self.patch(self.hl, 'NEAR_LIMIT', 0.5)
        self.mock.add_container('app', project='web')
        self.mock.stats = [{'ContainerID': 'id-app', 'Name': 'app', 'CPUNano': 1, 'SystemNano': 1,
                            'MemUsage': 600, 'MemLimit': 1000, 'PIDs': 1}]
        self.act.watch_once()
        self.act.watch_once()
        self.assertEqual([e['kind'] for e in self.act.listing({})['entries']], ['memory-near'])

    def test_the_restart_loop_words_follow_the_window(self):
        self.patch(self.hl, 'LOOP_WINDOW', 1200)
        row = self.mock.add_container('app', project='web')
        row.update(Restarts=3, StartedAt=int(time.time()) - 900)       # within 20 minutes, not within 10
        self.act.watch_once()
        [e] = self.act.listing({})['entries']
        self.assertIn('the last within 20 minutes', e['message'])

    def test_the_page_has_no_number_of_its_own(self):
        self.ui.Config.password_hash = ''
        import threading
        from http.server import ThreadingHTTPServer
        server = ThreadingHTTPServer(('127.0.0.1', 0), self.ui.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with urllib.request.urlopen(f'http://127.0.0.1:{server.server_address[1]}/', timeout=10) as r:
            page = r.read().decode('utf-8')
        for fixed in ('10 minutes', 'below 90 %', 'const NEAR_LIMIT', 'NEAR_HOLD = 30000'):
            self.assertNotIn(fixed, page, fixed)
        for name in ('function limits', 'loopWindowSeconds', 'nearHoldSeconds', 'nearLimit'):
            self.assertIn(name, page, name)
