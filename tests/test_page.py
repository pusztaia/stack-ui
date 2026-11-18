import importlib
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from tests.support import Base, fresh_server
from stack_ui import server as srv


class PageTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = fresh_server()
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

    def test_dismiss_sends_the_alerts_it_showed(self):
        with urllib.request.urlopen(self.base + '/', timeout=10) as r:
            page = r.read().decode('utf-8')
        self.assertIn('alerts: ALERTS.map((a) => ({ stack: a.stack, at: a.at }))', page)

    def test_long_values_stay_in_their_table_cells(self):
        """A tag in a table is cut at its cell with an ellipsis (the whole text in its title), ports wrap, and the
        stacks list's phone layout names only its own cells (the Containers table has a c-name cell too: a named
        area it lacks collapsed its grid, the image column was 0 px wide)."""
        with urllib.request.urlopen(self.base + '/', timeout=10) as r:
            page = r.read().decode('utf-8')
        self.assertIn('.table .tag { max-width: 100%; overflow: hidden; text-overflow: ellipsis;', page)
        self.assertIn('.ports { display: flex; flex-wrap: wrap;', page)
        self.assertIn('.stacks .c-name { grid-area: name; }', page)
        self.assertNotRegex(page, r'\n\s*\.c-(sel|state|name|cont|drift|img|act|mem) \{ grid-area')
        self.assertIn('title="${esc(c.stack)}"', page)

    def test_a_failed_image_check_names_the_stack_and_the_reason(self):
        """The toast names each stack whose registry could not be asked and why; the stack's Summary shows the reason
        as text (a badge's hover title cannot be read on a phone)."""
        with urllib.request.urlopen(self.base + '/', timeout=10) as r:
            page = r.read().decode('utf-8')
        for name in ('function checkFailures', 'checkFailures(UPDATES.stacks)', '<dt>Why the check failed</dt>', 'id="upd-why"'):
            self.assertIn(name, page, name)
        self.assertNotIn('hover its badge for the reason', page)

    def test_memory_near_the_limit_is_warned_about(self):
        """A container whose readings show it at or above 90 % of its memory limit for 30 s (a spike is not enough; the
        page reads twice right after it opens) gets a yellow chip in the header and a row on the Health page, before
        the kernel kills it."""
        with urllib.request.urlopen(self.base + '/', timeout=10) as r:
            page = r.read().decode('utf-8')
        for name in ('function limits', 'function trackNear', 'function nearLimit', 'trackNear();',
                     'L.nearHoldSeconds * 1000', 'r.mem / r.limit >= L.nearLimit', 'Memory near limit', "rows.push(['Memory near limit'"):
            self.assertIn(name, page, name)
        # counted once per reading, right where a new reading arrives
        start = page.index('async function loadResources')
        self.assertLess(page.index('trackNear();', start), page.index('refreshResourceCells();', start))

    def test_the_backup_panel(self):
        with urllib.request.urlopen(self.base + '/', timeout=10) as r:
            page = r.read().decode('utf-8')
        for name in ('<h2>Backup</h2>', 'function loadBackup', 'id="bk-download"', 'id="bk-save"', "'/api/backup'",
                     "'/api/backup/download'", "'/api/backup/save'", 'STACK_UI_BACKUP_DIR', 'URL.createObjectURL'):
            self.assertIn(name, page, name)
