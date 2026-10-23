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
