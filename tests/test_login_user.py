import json
import threading
import urllib.error
import urllib.request

from tests.support import Base, fresh_server


class LoginUserTest(Base):
    """STACK_UI_USERNAME: the sign-in asks for a user name too; every sign-in is written to the Activity log."""

    def setUp(self):
        super().setUp()
        self.ui = fresh_server()
        self.ui.Config.password_hash = self.ui.hash_password('right-password', 1000)
        self.ui.Config.username = 'admin'
        self.addCleanup(setattr, self.ui.Config, 'username', '')
        self.patch(self.ui.time, 'sleep', lambda _: None)
        server = self.ui.Server(('127.0.0.1', 0), self.ui.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f'http://127.0.0.1:{server.server_address[1]}'

    def login(self, body):
        req = urllib.request.Request(self.base + '/login', json.dumps(body).encode(), {'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def logins(self):
        return [(e['kind'], e['target'], e['result']) for e in self.ui.activity.entries() if e['type'] == 'login']

    def test_the_right_name_and_password_sign_in(self):
        self.assertEqual(self.login({'username': 'admin', 'password': 'right-password'})[0], 200)

    def test_a_wrong_or_missing_name_is_refused_like_a_wrong_password(self):
        wrong_name = self.login({'username': 'root', 'password': 'right-password'})
        no_name = self.login({'password': 'right-password'})
        wrong_password = self.login({'username': 'admin', 'password': 'no'})
        self.assertEqual([wrong_name[0], no_name[0], wrong_password[0]], [401, 401, 401])
        self.assertEqual(wrong_name[1], wrong_password[1])   # nothing tells which of the two was wrong
        self.assertEqual(no_name[1], wrong_password[1])
        self.assertIn('user name or password', wrong_name[1]['error'])

    def test_without_a_name_set_the_password_alone_signs_in(self):
        self.ui.Config.username = ''
        self.assertEqual(self.login({'password': 'right-password'})[0], 200)
        self.assertEqual(self.login({'username': 'anything', 'password': 'right-password'})[0], 200)

    def test_the_sign_in_page_asks_for_the_name_only_when_one_is_set(self):
        with urllib.request.urlopen(self.base + '/', timeout=10) as r:
            self.assertIn('autocomplete="username"', r.read().decode())
        self.ui.Config.username = ''
        with urllib.request.urlopen(self.base + '/', timeout=10) as r:
            self.assertNotIn('autocomplete="username"', r.read().decode())

    def test_each_sign_in_is_in_the_activity_log_without_what_was_typed(self):
        self.login({'username': 'admin', 'password': 'right-password'})
        self.login({'username': 'my-secret-typed-here', 'password': 'no'})
        self.assertEqual(self.logins(), [('sign-in', '127.0.0.1', 'ok'), ('sign-in', '127.0.0.1', 'failed')])
        raw = (self.ui.st.DATA / 'activity.jsonl').read_text(encoding='utf-8')
        self.assertNotIn('my-secret-typed-here', raw)
        self.assertNotIn('right-password', raw)

    def test_a_lock_out_is_written_once_however_often_it_refuses(self):
        for _ in range(5):
            self.login({'username': 'admin', 'password': 'no'})
        codes = [self.login({'username': 'admin', 'password': 'no'})[0] for _ in range(10)]
        self.assertEqual(codes, [429] * 10)
        self.assertEqual([r for _, _, r in self.logins()], ['failed'] * 5 + ['throttled'])

    def test_a_new_lock_out_after_the_old_one_ended_is_written_again(self):
        for _ in range(6):
            self.login({'username': 'admin', 'password': 'no'})
        self.ui.FAILURES.clear()   # the window passed
        for _ in range(6):
            self.login({'username': 'admin', 'password': 'no'})
        self.assertEqual([r for _, _, r in self.logins()].count('throttled'), 2)

    def test_sign_ins_have_their_own_filter(self):
        self.login({'username': 'admin', 'password': 'no'})
        shown = self.ui.activity.listing({'show': ['sign-ins']})['entries']
        self.assertEqual([e['type'] for e in shown], ['login'])
        self.assertEqual(self.ui.activity.listing({'show': ['changes']})['entries'], [])
