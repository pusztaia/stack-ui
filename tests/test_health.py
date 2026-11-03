import importlib
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from tests.support import Base
from stack_ui import health as hl
from stack_ui import podman_api as pa
from stack_ui import server as srv


def fake_disk_usage(blocks, free, avail=None):
    """shutil.disk_usage as statvfs numbers give it: (total, used, available to users), 4 KiB blocks."""
    return lambda _path: (blocks * 4096, (blocks - free) * 4096, (free if avail is None else avail) * 4096)


class ReaderTest(Base):
    def write(self, name, text):
        f = self.tmp / name
        f.write_text(text, encoding='utf-8')
        return f

    def test_temperature_is_in_millidegrees(self):
        self.patch(hl, 'THERMAL', self.write('temp', '54321\n'))
        self.assertEqual(hl.read_temperature(), 54.3)

    def test_an_unreadable_temperature_is_none(self):
        self.patch(hl, 'THERMAL', self.tmp / 'missing')
        self.assertIsNone(hl.read_temperature())
        self.patch(hl, 'THERMAL', self.write('junk', 'warm\n'))
        self.assertIsNone(hl.read_temperature())

    def test_load_and_uptime(self):
        self.patch(hl, 'LOADAVG', self.write('load', '0.52 0.58 0.59 1/456 12345\n'))
        self.patch(hl, 'UPTIME', self.write('up', '12345.67 54321.00\n'))
        self.assertEqual(hl.read_load(), (0.52, 0.58, 0.59))
        self.assertEqual(hl.read_uptime(), 12345)
        self.patch(hl, 'LOADAVG', self.tmp / 'missing')
        self.patch(hl, 'UPTIME', self.tmp / 'missing')
        self.assertIsNone(hl.read_load())
        self.assertIsNone(hl.read_uptime())


class LevelTest(Base):
    def test_temperature(self):
        self.assertEqual([hl.temperature_level(t) for t in (40, 69.9, 70, 79.9, 80, 90)],
                         ['ok', 'ok', 'warn', 'warn', 'bad', 'bad'])

    def test_disk(self):
        self.assertEqual([hl.disk_level(p) for p in (10, 79.9, 80, 89.9, 90, 100)],
                         ['ok', 'ok', 'warn', 'warn', 'bad', 'bad'])

    def test_load_is_per_core(self):
        self.assertEqual([hl.load_level(x, 4) for x in (0.5, 3.9, 4, 7.9, 8)], ['ok', 'ok', 'warn', 'warn', 'bad'])
        self.assertEqual(hl.load_level(1.5, 0), 'ok')   # no core count: nothing to judge by

    def test_the_worst_level_wins(self):
        self.assertEqual(hl.worst(['ok', 'warn', 'ok']), 'warn')
        self.assertEqual(hl.worst(['ok', 'bad', 'warn']), 'bad')
        self.assertEqual(hl.worst(['ok']), 'ok')
        self.assertIsNone(hl.worst([]))


class DiskTest(Base):
    def test_usage_is_what_df_shows(self):
        self.patch(hl.shutil, 'disk_usage', fake_disk_usage(1000, 100))   # 900 of 1000 blocks used
        d = hl.disk_usage(str(self.tmp))
        self.assertEqual((d['total'], d['used'], d['free']), (1000 * 4096, 900 * 4096, 100 * 4096))
        self.assertEqual((d['percent'], d['level']), (90.0, 'bad'))

    def test_root_reserved_blocks_count_as_used(self):
        self.patch(hl.shutil, 'disk_usage', fake_disk_usage(1000, 100, avail=50))   # df: used / (used + available)
        self.assertEqual(hl.disk_usage(str(self.tmp))['percent'], round(100 * 900 / 950, 1))

    def test_one_filesystem_is_listed_once_and_a_missing_path_is_skipped(self):
        sub = self.tmp / 'sub'
        sub.mkdir()
        rows = hl.disks([str(self.tmp), str(sub), str(self.tmp / 'missing')])
        self.assertEqual([r['path'] for r in rows], [str(self.tmp)])
        self.assertGreater(rows[0]['total'], 0)


class HealthTest(Base):
    def setUp(self):
        super().setUp()
        self.patch(hl, 'THERMAL', self.tmp / 'no-temp')
        self.patch(hl, 'LOADAVG', self.tmp / 'no-load')
        self.patch(hl, 'UPTIME', self.tmp / 'no-uptime')

    def test_what_cannot_be_read_is_none_and_the_rest_works(self):
        h = hl.health([str(self.tmp)], cpus=4)
        self.assertIsNone(h['temperature'])
        self.assertIsNone(h['load'])
        self.assertIsNone(h['uptime'])
        self.assertEqual(len(h['disks']), 1)
        self.assertIn(h['level'], ('ok', 'warn', 'bad'))

    def test_a_hot_pi_makes_the_whole_health_bad(self):
        (self.tmp / 'hot').write_text('82000\n', encoding='utf-8')
        self.patch(hl, 'THERMAL', self.tmp / 'hot')
        self.patch(hl.shutil, 'disk_usage', fake_disk_usage(1000, 900))
        h = hl.health([str(self.tmp)], cpus=4)
        self.assertEqual(h['temperature'], {'celsius': 82.0, 'level': 'bad'})
        self.assertEqual(h['level'], 'bad')

    def test_load_carries_its_level(self):
        (self.tmp / 'load').write_text('9.00 5.00 2.00 1/1 1\n', encoding='utf-8')
        self.patch(hl, 'LOADAVG', self.tmp / 'load')
        h = hl.health([], cpus=4)
        self.assertEqual(h['load'], {'one': 9.0, 'five': 5.0, 'fifteen': 2.0, 'cpus': 4, 'level': 'bad'})

    def test_podman_counts(self):
        self.mock.add_container('a', state='running')
        self.mock.add_container('b', state='exited')
        self.mock.images.append({'Id': 'i1', 'RepoTags': ['x:1'], 'RepoDigests': [], 'Size': 1500, 'Created': 1})
        self.mock.images.append({'Id': 'i2', 'RepoTags': ['y:1'], 'RepoDigests': [], 'Size': 500, 'Created': 1})
        p = hl.health([], cpus=4)['podman']
        self.assertEqual(p['version'], '5.4.2')
        self.assertEqual(p['containers'], {'running': 1, 'total': 2})
        self.assertEqual(p['images'], {'count': 2, 'size': 2000})
        self.assertNotIn('error', p)

    def test_an_unreachable_podman_is_reported_not_raised(self):
        self.patch(pa, 'API_URL', '')
        self.patch(pa, 'SOCKET', str(self.tmp / 'no.sock'))
        h = hl.health([], cpus=4)
        self.assertIn('error', h['podman'])
        self.assertIn(h['level'], ('ok', 'warn', 'bad', None))


class ApiTest(Base):
    def test_the_api_answer(self):
        ui = importlib.reload(srv)
        (self.tmp / 'temp').write_text('61000\n', encoding='utf-8')
        self.patch(hl, 'THERMAL', self.tmp / 'temp')
        h = ui.health_info()
        self.assertEqual(h['temperature']['celsius'], 61.0)
        self.assertEqual(h['level'] in ('ok', 'warn', 'bad'), True)
        self.assertTrue(h['disks'])
        self.assertIn('podman', h)

    def test_the_route_needs_the_token_and_answers_json(self):
        ui = importlib.reload(srv)
        self.patch(ui, 'SETTINGS_FILE', self.tmp / 'settings.json')
        ui.Config.password_hash = ''
        server = ThreadingHTTPServer(('127.0.0.1', 0), ui.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f'http://127.0.0.1:{server.server_address[1]}/api/health'
        token = ui.api_token({})['token']
        with urllib.request.urlopen(urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'}), timeout=10) as r:
            body = json.loads(r.read().decode('utf-8'))
        self.assertEqual(sorted(body), ['disks', 'level', 'load', 'podman', 'temperature', 'uptime'])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(url, timeout=10)
        ctx.exception.close()
        self.assertEqual(ctx.exception.code, 403)
