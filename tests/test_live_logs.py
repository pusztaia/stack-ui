import http.client
import importlib
import json
import threading
import time
from http.server import ThreadingHTTPServer

from tests.support import Base
from stack_ui import server as srv


def frame(stream, text):
    return bytes([stream, 0, 0, 0]) + len(text).to_bytes(4, 'big') + text


class LiveLogsTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = importlib.reload(srv)
        self.ui.Config.password_hash = ''
        self.mock.add_container('web', project='web')
        server = ThreadingHTTPServer(('127.0.0.1', 0), self.ui.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]

    def open(self, path, headers=None, token=True):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request('GET', path, headers={**({'X-UI-Token': self.ui.Config.local_token} if token else {}),
                                           **(headers or {})})
        resp = conn.getresponse()
        self.addCleanup(resp.close)          # the answer owns the socket: closing it is what ends a stream
        return conn, resp

    @staticmethod
    def message(resp):
        return json.loads(resp.readline())

    def stream(self, query=''):
        return self.open('/api/containers/web/logs/stream' + query)

    def test_history_then_live_lines_then_the_end(self):
        self.mock.logs['id-web'] = frame(1, b'2026-11-07T10:00:00Z h1\n2026-11-07T10:00:01Z h2\n')
        _, resp = self.stream('?tail=200')
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader('Content-Type'), 'application/x-ndjson; charset=utf-8')
        self.assertEqual(resp.getheader('X-Accel-Buffering'), 'no')
        self.assertEqual(resp.getheader('Cache-Control'), 'no-store')
        self.assertEqual(resp.getheader('X-Content-Type-Options'), 'nosniff')
        self.assertEqual(self.message(resp), {'l': '2026-11-07T10:00:00Z h1'})
        self.assertEqual(self.message(resp), {'l': '2026-11-07T10:00:01Z h2'})
        self.mock.push_log('id-web', frame(1, b'2026-11-07T10:00:02Z live\n'))
        self.assertEqual(self.message(resp), {'l': '2026-11-07T10:00:02Z live'})
        self.mock.end_log('id-web')
        self.assertEqual(self.message(resp), {'end': 'the container stopped'})
        self.assertEqual(resp.readline(), b'')

    def test_since_and_tail_reach_podman(self):
        self.mock.end_log('id-web')
        _, resp = self.stream('?tail=500&since=2026-11-07T10:00:00Z')
        resp.read()
        call = [q for m, p, q in self.mock.calls if p == '/containers/id-web/logs'][-1]
        self.assertEqual((call['tail'], call['since'], call['follow']), ('500', '2026-11-07T10:00:00Z', 'true'))

    def test_a_quiet_log_gets_heartbeats(self):
        self.patch(self.ui, 'LOG_HEARTBEAT', 0.05)
        _, resp = self.stream()
        self.assertEqual(self.message(resp), {'hb': 1})

    def test_a_huge_line_is_cut_and_a_bad_byte_is_replaced(self):
        self.mock.logs['id-web'] = frame(1, b'x' * 10000 + b'\n') + frame(1, b'bad \xff\n')
        _, resp = self.stream()
        long_line = self.message(resp)['l']
        self.assertEqual(len(long_line), self.ui.LOG_LINE_MAX)
        self.assertTrue(long_line.endswith('…'))
        self.assertEqual(self.message(resp), {'l': 'bad �'})

    def test_errors_come_as_json_before_any_stream(self):
        for path, status in (('/api/containers/nope/logs/stream', 404),
                             ('/api/containers/web/logs/stream?tail=7', 400),
                             ('/api/containers/web/logs/stream?since=a%20b', 400),
                             ('/api/containers/web/logs/stream?since=' + '1' * 41, 400)):
            _, resp = self.open(path)
            self.assertEqual(resp.status, status, path)
            self.assertIn('error', json.loads(resp.read()))
        _, resp = self.open('/api/containers/nope/logs/stream')
        self.assertEqual(json.loads(resp.read())['error'], 'no container "nope"')

    def test_a_refused_podman_call_is_a_502_and_gives_the_slot_back(self):
        self.patch(self.ui, 'LOG_STREAMS', threading.BoundedSemaphore(1))
        self.mock.fail[('GET', '/containers/id-web/logs')] = (500, 'no log driver')
        _, resp = self.stream()
        self.assertEqual(resp.status, 502)
        self.assertIn('no log driver', json.loads(resp.read())['error'])
        del self.mock.fail[('GET', '/containers/id-web/logs')]
        self.mock.end_log('id-web')
        _, resp = self.stream()
        self.assertEqual(resp.status, 200)

    def test_the_stream_limit_and_a_slot_that_comes_back_when_the_client_leaves(self):
        self.patch(self.ui, 'LOG_STREAMS', threading.BoundedSemaphore(2))
        self.patch(self.ui, 'LOG_HEARTBEAT', 0.05)        # a write every 50 ms notices a client that left
        first, r1 = self.stream()
        second, r2 = self.stream()
        self.assertEqual((r1.status, r2.status), (200, 200))
        _, third = self.stream()
        self.assertEqual(third.status, 429)
        self.assertIn('close another', json.loads(third.read())['error'])
        r1.close()                                         # the answer owns the socket
        first.close()
        deadline = time.time() + 5
        while True:
            _, again = self.stream()
            if again.status == 200 or time.time() > deadline:
                break
            again.read()
            time.sleep(0.1)
        self.assertEqual(again.status, 200)

    def test_a_recreated_container_is_found_again_by_name(self):
        self.mock.end_log('id-web')
        _, resp = self.stream()
        resp.read()
        self.mock.containers[0]['Id'] = 'id-web2'          # the stack was recreated: same name, new id
        self.mock.logs['id-web2'] = frame(1, b'2026-11-07T11:00:00Z fresh\n')
        self.mock.end_log('id-web2')
        _, resp = self.stream()
        self.assertEqual(self.message(resp), {'l': '2026-11-07T11:00:00Z fresh'})

    def test_access_needs_a_session_with_its_token_or_the_api_token(self):
        self.patch(self.ui, 'SETTINGS_FILE', self.tmp / 'settings.json')
        self.ui.Config.password_hash = self.ui.hash_password('right-password', 1000)
        api_token = self.ui.api_token({})['token']
        sid, page_token = self.ui.new_session()
        self.mock.end_log('id-web')
        path = '/api/containers/web/logs/stream'
        _, resp = self.open(path, token=False)
        self.assertEqual(resp.status, 401)
        _, resp = self.open(path, {'Cookie': f'stack_ui_session={sid}'}, token=False)
        self.assertEqual(resp.status, 403)
        _, resp = self.open(path, {'Cookie': f'stack_ui_session={sid}', 'X-UI-Token': page_token}, token=False)
        self.assertEqual(resp.status, 200)
        self.mock.end_log('id-web')
        _, resp = self.open(path, {'Authorization': f'Bearer {api_token}'}, token=False)
        self.assertEqual(resp.status, 200)

    def test_the_one_shot_logs_route_is_unchanged(self):
        self.mock.logs['id-web'] = b'hello\nworld\n'
        _, resp = self.open('/api/containers/web/logs?tail=200')
        self.assertEqual(json.loads(resp.read()), {'container': 'web', 'lines': ['hello', 'world']})
