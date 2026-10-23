import importlib
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from tests.support import Base
from stack_ui import podman_api as pa, server as srv

MEMINFO = 'MemTotal:        4000000 kB\nMemFree:          500000 kB\nMemAvailable:    1000000 kB\n'
HOST_MEM = 4_096_000_000       # MemTotal in bytes
HOST_USED = 3_072_000_000      # MemTotal - MemAvailable in bytes


def stat(name, cpu_ns, sys_ns, mem=100_000_000, limit=268_435_456, pids=3):
    """One entry of Podman's containers/stats answer (the keys the page's server reads)."""
    return {'Name': name, 'CPUNano': cpu_ns, 'SystemNano': sys_ns, 'MemUsage': mem, 'MemLimit': limit, 'PIDs': pids}


def proc_stat(user, system, idle):
    """/proc/stat of a 4-core host: the first line is every core together."""
    return (f'cpu  {user} 0 {system} {idle} 0 0 0 0 0 0\n'
            + ''.join(f'cpu{i} 1 0 1 1 0 0 0 0 0 0\n' for i in range(4)) + 'intr 1\n')


class StatsApiTest(Base):
    def test_container_stats_lists_the_running_containers(self):
        self.mock.stats = [stat('app', 5, 10), stat('db', 6, 11)]
        self.assertEqual([s['Name'] for s in pa.container_stats()], ['app', 'db'])
        self.assertEqual(self.mock.calls[-1][:2], ('GET', '/containers/stats'))

    def test_no_container_is_an_empty_list(self):
        self.assertEqual(pa.container_stats(), [])

    def test_podman_error_is_raised(self):
        self.mock.fail[('GET', '/containers/stats')] = (500, 'down')
        with self.assertRaises(pa.PodmanError):
            pa.container_stats()


class UsageTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = importlib.reload(srv)
        self.meminfo = self.tmp / 'meminfo'
        self.proc_stat = self.tmp / 'stat'
        self.meminfo.write_text(MEMINFO)
        self.proc_stat.write_text(proc_stat(100, 100, 800))
        self.patch(self.ui, 'PROC_MEMINFO', self.meminfo)
        self.patch(self.ui, 'PROC_STAT', self.proc_stat)
        self.pauses = 0
        self.next_stat = None
        self.patch(self.ui, 'pause', self.pause)

    def pause(self):
        """The wait between two readings: counted, and the host's /proc/stat moves on meanwhile."""
        self.pauses += 1
        if self.next_stat is not None:
            self.proc_stat.write_text(self.next_stat)
            self.next_stat = None

    def first_call(self):
        """The first call takes two readings: 'app' used 0.5 s of CPU in 1 s on 4 cores, the host 100 of 1000 ticks."""
        self.mock.stats = [stat('app', 1_000_000_000, 10_000_000_000)]
        self.mock.stats_next = [[stat('app', 1_500_000_000, 11_000_000_000)]]
        self.next_stat = proc_stat(200, 100, 1700)
        return self.ui.resources()

    def test_first_call_takes_a_second_reading(self):
        r = self.first_call()
        self.assertEqual(self.pauses, 1)
        self.assertEqual(r['containers'], {'app': {'cpu': 12.5, 'mem': 100_000_000, 'limit': 268_435_456, 'pids': 3}})
        self.assertEqual(r['host'], {'memTotal': HOST_MEM, 'memUsed': HOST_USED, 'cpu': 10.0, 'cpus': 4})
        self.assertEqual(r['error'], '')

    def test_later_calls_use_the_stored_sample(self):
        self.first_call()
        self.pauses = 0
        self.mock.stats = [stat('app', 1_900_000_000, 12_000_000_000)]   # 0.4 s of CPU in the 1 s since
        r = self.ui.resources()
        self.assertEqual(r['containers']['app']['cpu'], 10.0)
        self.assertEqual(r['host']['cpu'], 10.0)
        self.assertEqual(self.pauses, 0)

    def test_two_readings_within_a_second_repeat_the_last_value(self):
        self.first_call()
        self.pauses = 0
        self.mock.stats = [stat('app', 1_600_000_000, 11_500_000_000)]   # half a second later
        r = self.ui.resources()
        self.assertEqual(r['containers']['app']['cpu'], 12.5)
        self.assertEqual(self.pauses, 0)

    def test_restarted_container_is_measured_again(self):
        self.first_call()
        self.pauses = 0
        self.mock.stats = [stat('app', 100, 12_000_000_000)]   # its CPU time went down: it was restarted
        self.mock.stats_next = [[stat('app', 400_000_100, 13_000_000_000)]]
        r = self.ui.resources()
        self.assertEqual(self.pauses, 1)
        self.assertEqual(r['containers']['app']['cpu'], 10.0)

    def test_no_limit_is_none(self):
        stats = [stat('free', 1, 2, limit=0), stat('whole', 1, 2, limit=HOST_MEM), stat('capped', 1, 2)]
        self.mock.stats = stats
        self.mock.stats_next = [stats]
        r = self.ui.resources()
        self.assertEqual({n: c['limit'] for n, c in r['containers'].items()},
                         {'free': None, 'whole': None, 'capped': 268_435_456})

    def test_a_container_that_is_gone_is_forgotten(self):
        self.first_call()
        self.mock.stats = []
        r = self.ui.resources()
        self.assertEqual(r['containers'], {})
        self.assertNotIn('app', self.ui.SAMPLES)

    def test_podman_error_keeps_the_host(self):
        self.mock.fail[('GET', '/containers/stats')] = (500, 'down')
        self.next_stat = proc_stat(200, 100, 1700)
        r = self.ui.resources()
        self.assertEqual(r['containers'], {})
        self.assertEqual(r['error'].count('down'), 1)
        self.assertEqual(r['host']['memTotal'], HOST_MEM)
        self.assertEqual(r['host']['cpu'], 10.0)

    def test_unreadable_proc_files_leave_the_host_out(self):
        self.patch(self.ui, 'PROC_MEMINFO', self.tmp / 'missing')
        self.mock.stats = [stat('app', 1_000_000_000, 10_000_000_000)]
        self.mock.stats_next = [[stat('app', 1_500_000_000, 11_000_000_000)]]
        r = self.ui.resources()
        self.assertIsNone(r['host'])
        self.assertIn('/proc/meminfo', r['error'])
        self.assertEqual(r['containers']['app']['cpu'], 12.5)
        self.patch(self.ui, 'PROC_MEMINFO', self.meminfo)
        self.patch(self.ui, 'PROC_STAT', self.tmp / 'missing')
        r = self.ui.resources()
        self.assertIsNone(r['host'])
        self.assertIn('/proc/stat', r['error'])


class UsageApiTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = importlib.reload(srv)
        self.patch(self.ui, 'SETTINGS_FILE', self.tmp / 'settings.json')
        (self.tmp / 'meminfo').write_text(MEMINFO)
        (self.tmp / 'stat').write_text(proc_stat(100, 100, 800))
        self.patch(self.ui, 'PROC_MEMINFO', self.tmp / 'meminfo')
        self.patch(self.ui, 'PROC_STAT', self.tmp / 'stat')
        self.patch(self.ui, 'pause', lambda: None)
        self.ui.Config.password_hash = ''
        server = ThreadingHTTPServer(('127.0.0.1', 0), self.ui.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f'http://127.0.0.1:{server.server_address[1]}'

    def test_the_api_token_opens_it(self):
        token = self.ui.api_token({})['token']
        self.mock.stats = [stat('app', 1, 2)]
        req = urllib.request.Request(self.base + '/api/resources', headers={'Authorization': f'Bearer {token}'})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        self.assertEqual(sorted(data), ['containers', 'error', 'host'])
        self.assertIn('app', data['containers'])
        bad = urllib.request.Request(self.base + '/api/resources', headers={'Authorization': 'Bearer wrong'})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(bad, timeout=10)
        self.assertEqual(ctx.exception.code, 401)
        ctx.exception.close()
