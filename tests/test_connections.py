import importlib
import socket
import threading
import time

from tests.support import Base
from stack_ui import server as srv


class ConnectionLimitsTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = importlib.reload(srv)
        self.ui.Config.password_hash = ''
        self.patch(self.ui.Handler, 'timeout', 1)

    def start(self):
        server = self.ui.Server(('127.0.0.1', 0), self.ui.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def request(self, port, head: bytes) -> bytes:
        with socket.create_connection(('127.0.0.1', port), timeout=5) as s:
            s.sendall(head)
            return s.recv(200)

    def test_bad_content_length_is_refused_at_once(self):
        port = self.start()
        for value in (b'-1', b'abc', b'2000000', b'1 2'):
            started = time.time()
            answer = self.request(port, b'POST /api/x HTTP/1.1\r\nHost: evil\r\nContent-Length: ' + value + b'\r\n\r\n')
            self.assertTrue(answer.startswith(b'HTTP/1.0 413'), (value, answer))
            self.assertLess(time.time() - started, 1)

    def test_a_silent_client_is_dropped_after_the_timeout(self):
        port = self.start()
        with socket.create_connection(('127.0.0.1', port), timeout=5) as s:
            s.sendall(b'POST /api/x HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 10\r\n\r\nabc')
            started = time.time()
            self.assertEqual(s.recv(200), b'')     # closed by the server, no answer
            self.assertLess(time.time() - started, 4)

    def test_open_connections_are_limited(self):
        self.patch(self.ui, 'MAX_CONNECTIONS', 2)
        self.patch(self.ui.Handler, 'timeout', 5)
        port = self.start()
        held = [socket.create_connection(('127.0.0.1', port), timeout=5) for _ in range(2)]
        self.addCleanup(lambda: [s.close() for s in held])
        time.sleep(0.3)
        with socket.create_connection(('127.0.0.1', port), timeout=5) as s:
            s.sendall(b'GET /session HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n')
            try:
                self.assertEqual(s.recv(200), b'')     # over the limit: closed
            except ConnectionError:                    # (Windows reports it as an abort)
                pass
        held.pop().close()
        time.sleep(0.3)
        self.assertTrue(self.request(port, b'GET /session HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n').startswith(b'HTTP/1.0 200'))
