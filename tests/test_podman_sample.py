"""The page's readings on what a real Podman answered: tests/fixtures/podman-5.7/ is a Raspberry Pi's Podman 5.7.0
(13 containers), cut down to the fields the page reads and renamed (c1.., s1.., made-up ids, ports moved, a
documentation address). The mock libpod serves it, so the page's own code reads it the way it reads the Pi.

What it pins: the field types the restart-loop, out-of-memory, memory and port checks rely on (Restarts, StartedAt,
ExitCode, ExitedAt are numbers; a port range comes as one entry), and that a container without a memory limit
(Podman gives the host's memory as its limit) is not one "near its limit"."""
import json
import time
from pathlib import Path

from tests.support import Base, fresh_server

SAMPLE = Path(__file__).parent / 'fixtures' / 'podman-5.7'
HOST_MEMORY = 3962392576        # the sample host's MemTotal: the limit Podman gives a container that has none


def load(name):
    return json.loads((SAMPLE / name).read_text(encoding='utf-8'))


class PodmanSampleTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = fresh_server()
        from stack_ui import activity, health
        self.act, self.hl = activity, health
        health._OOM.clear()
        self.containers = load('containers.json')
        self.stats = load('stats.json')['Stats']
        self.mock.containers[:] = [dict(c, MockVolumes=[]) for c in self.containers]
        self.mock.stats = self.stats
        meminfo = self.tmp / 'meminfo'
        meminfo.write_bytes(f'MemTotal: {HOST_MEMORY // 1024} kB\nMemAvailable: 2000000 kB\n'.encode())
        self.patch(health, 'PROC_MEMINFO', meminfo)
        self.patch(self.ui.usage, 'PROC_MEMINFO', meminfo)

    def test_the_fields_are_the_types_the_checks_read(self):
        self.assertEqual(load('version.json')['Version'], '5.7.0')
        for c in self.containers:
            for key in ('Restarts', 'StartedAt', 'ExitCode', 'ExitedAt'):
                self.assertIsInstance(c[key], int, (c['Names'][0], key))
            self.assertIsInstance(c['Status'], str)
        for s in self.stats:
            for key in ('MemUsage', 'MemLimit', 'CPUNano', 'SystemNano', 'PIDs'):
                self.assertIsInstance(s[key], int, (s['Name'], key))

    def test_a_port_range_is_every_port_of_it(self):
        ranged = [p for c in self.containers for p in c['Ports'] if p['range'] > 1]
        self.assertTrue(ranged, 'the sample has a range')
        total = sum(p['range'] for c in self.containers for p in c['Ports'] if p['host_port'])
        self.assertEqual(sum(len(self.ui.pa.host_ports(c)) for c in self.containers), total)
        for p in ranged:
            owner = next(c for c in self.containers if p in c['Ports'])
            got = [x['host_port'] for x in self.ui.pa.host_ports(owner)]
            for i in range(p['range']):
                self.assertIn(p['host_port'] + i, got)

    def test_a_healthy_host_gives_no_warning(self):
        """Every container of the sample runs, none restarted, none was killed, the most used 48 % of its limit."""
        self.assertEqual([c['Names'][0] for c in self.containers if self.hl.looping(c)], [])
        self.assertEqual([c['Names'][0] for c in self.containers if self.hl.oom_killed(c)], [])
        self.act.watch_once()
        self.act.watch_once()
        self.assertEqual(self.act.listing({})['entries'], [])
        self.assertEqual(self.hl.health([], 4)['podman']['looping'], [])

    def test_the_container_without_a_limit(self):
        unlimited = [s['Name'] for s in self.stats if s['MemLimit'] == HOST_MEMORY]
        self.assertEqual(len(unlimited), 1)
        r = self.ui.resources()
        self.assertIsNone(r['containers'][unlimited[0]]['limit'])
        self.assertTrue(all(r['containers'][s['Name']]['limit'] for s in self.stats if s['Name'] not in unlimited))

    def test_a_container_without_a_limit_using_most_of_the_host_is_not_near_its_limit(self):
        """Its "limit" is the host's memory: raising a mem_limit it does not have is not the advice."""
        name = next(s['Name'] for s in self.stats if s['MemLimit'] == HOST_MEMORY)
        for s in self.stats:
            if s['Name'] == name:
                s['MemUsage'] = int(HOST_MEMORY * 0.95)
        self.act.watch_once()
        self.act.watch_once()
        self.assertEqual(self.act.listing({'show': 'warnings'})['entries'], [])

    def test_a_container_near_its_own_limit_is_warned_about(self):
        s = next(s for s in self.stats if 0 < s['MemLimit'] < HOST_MEMORY)
        s['MemUsage'] = int(s['MemLimit'] * 0.95)
        self.act.watch_once()
        self.act.watch_once()
        [e] = self.act.listing({'show': 'warnings'})['entries']
        self.assertEqual((e['kind'], e['target'], e['result']), ('memory-near', s['Name'], 'started'))

    def test_a_restart_loop_in_the_real_shape(self):
        c = self.mock.containers[0]
        c.update(Restarts=4, StartedAt=int(time.time()) - 30)
        self.assertTrue(self.hl.looping(c))
        self.act.watch_once()
        self.assertEqual([e['kind'] for e in self.act.listing({})['entries']], ['restart-loop'])

    def test_the_mock_lists_what_the_real_podman_lists(self):
        """Every list field the page reads is in the mock's containers too, so a test on the mock reads the same
        shape as the page on the Pi."""
        reads = {'Id', 'Names', 'Image', 'ImageID', 'State', 'Status', 'Labels', 'Ports', 'Created', 'Restarts',
                 'StartedAt', 'ExitCode', 'ExitedAt', 'Exited'}
        real = load('containers.json')[0]
        mocked = self.mock.add_container('m', project='p')
        self.assertEqual(sorted(reads - set(mocked)), [])
        self.assertEqual(sorted((reads - {'ImageID', 'Created'}) - set(real)), [])
        for key in ('Restarts', 'StartedAt', 'ExitCode', 'ExitedAt'):
            self.assertIsInstance(mocked[key], int, key)
