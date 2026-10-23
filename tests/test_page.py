import importlib
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from tests.support import Base
from stack_ui import server as srv


class PageTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = importlib.reload(srv)
        self.ui.Config.password_hash = ''
        server = ThreadingHTTPServer(('127.0.0.1', 0), self.ui.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f'http://127.0.0.1:{server.server_address[1]}'

    def test_the_page_names_the_running_version(self):
        with urllib.request.urlopen(self.base + '/', timeout=10) as r:
            page = r.read().decode('utf-8')
        self.assertIn(f'<title>Stack UI {srv.__version__}</title>', page)
        self.assertIn(f'>v{srv.__version__}</div>', page)
        self.assertNotIn('__VERSION__', page)
