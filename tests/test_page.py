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

    def test_the_logs_page_follows_the_log_instead_of_polling(self):
        with urllib.request.urlopen(self.base + '/', timeout=10) as r:
            page = r.read().decode('utf-8')
        for name in ('lg-follow', 'lg-pause', 'lg-dl', 'lg-clear', 'lg-latest', 'logs/stream', 'stopLogStream', 'LOG_MAX'):
            self.assertIn(name, page)
        self.assertNotIn('Every 5 s', page)
        self.assertNotIn('LOG_TIMER', page)

    def test_a_bar_shows_the_running_operation_with_cancel(self):
        with urllib.request.urlopen(self.base + '/', timeout=10) as r:
            page = r.read().decode('utf-8')
        for name in ('id="opbar"', 'op-cancel', 'pollOperation', 'showCancelOperation', '/api/operation/cancel',
                     'since: op.since'):
            self.assertIn(name, page)

    def test_the_images_page_has_tick_boxes_instead_of_a_clean_up_button(self):
        with urllib.request.urlopen(self.base + '/', timeout=10) as r:
            page = r.read().decode('utf-8')
        for name in ('data-img-sel', 'data-img-all', 'img-selbar', 'img-untagged', 'showImagesRemove'):
            self.assertIn(name, page)
        self.assertNotIn('id="img-clean"', page)

    def test_the_deploy_dialog_shows_the_preflight_errors_and_warnings(self):
        with urllib.request.urlopen(self.base + '/', timeout=10) as r:
            page = r.read().decode('utf-8')
        for name in ('p.errors?.length', 'p.warnings?.length', 'warn-note'):
            self.assertIn(name, page)
        # an error leaves no way to go on: the dialog returns before the deploy button is built
        start = page.index('async function startDeploy')
        self.assertLess(page.index('p.errors?.length', start), page.index("id=\"pull\"", start))

    def test_the_health_page_and_the_chips_in_the_stacks_header(self):
        with urllib.request.urlopen(self.base + '/', timeout=10) as r:
            page = r.read().decode('utf-8')
        for name in ('data-nav="health"', "['health', 'Health']", 'function viewHealth', 'function renderHealth',
                     '/api/health', 'function healthChips', 'let HEALTH'):
            self.assertIn(name, page)
        # the numbers come with the resources reading, and the page is redrawn when they arrive
        self.assertIn("ROUTE.view === 'health'", page)

    def test_the_power_row_and_the_power_chip(self):
        with urllib.request.urlopen(self.base + '/', timeout=10) as r:
            page = r.read().decode('utf-8')
        for name in ('h.power', 'function powerLines', 'Under-voltage', 'throttled'):
            self.assertIn(name, page)

    def test_the_rollback_button_dialog_and_the_kept_images_tag(self):
        with urllib.request.urlopen(self.base + '/', timeout=10) as r:
            page = r.read().decode('utf-8')
        for name in ('data-act="rollback"', 'function showRollback', '/rollback/discard', 'id="rb-go"', 'rb-discard',
                     'rb-open', 'r.rollback', 'keptFor', 'Kept for rollback', 's.rollback'):
            self.assertIn(name, page)

    def test_the_scheduled_check_the_switch_and_the_alert_bar(self):
        with urllib.request.urlopen(self.base + '/', timeout=10) as r:
            page = r.read().decode('utf-8')
        for name in ('id="alertbar"', 'function renderAlerts', '/api/schedule/ack', '/api/schedule/run',
                     'Scheduled check', 'id="sch-time"', 'id="sch-on"', 'id="sch-run"', 'function loadSchedule',
                     'id="auto-sel"', '/auto', 's.autoNote', 'Automatic update', 'scheduleRunning',
                     'rolled back', 'needs you'):
            self.assertIn(name, page, name)

    def test_the_filter_and_the_bulk_switch_of_the_automatic_update(self):
        with urllib.request.urlopen(self.base + '/', timeout=10) as r:
            page = r.read().decode('utf-8')
        for name in ('sel-auto-on', 'sel-auto-off', "['auto', 'Automatic update'", '/api/stacks/auto', 'function autoSelected'):
            self.assertIn(name, page, name)

    def test_the_clean_up_switch_is_on_the_settings_page(self):
        with urllib.request.urlopen(self.base + '/', timeout=10) as r:
            page = r.read().decode('utf-8')
        for name in ('id="sch-clean"', 'cleanup:', 'sc.cleanup'):
            self.assertIn(name, page, name)
