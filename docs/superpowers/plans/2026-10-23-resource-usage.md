# Resource usage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show the current CPU and memory of every running container and of the host: in the Stacks list, on a stack's Summary tab and on a new Resources page, refreshed at an interval chosen under Settings.

**Architecture:** One server endpoint, `GET /api/resources`, reads Podman's `containers/stats` (and `/proc/meminfo`, `/proc/stat`) on each call, keeps the previous sample of each container in memory and returns current percentages computed from the difference. The page only fetches it, keeps the last answer in `RES` and draws it in three places; a timer refreshes it while a page that shows it is visible.

**Tech Stack:** Python standard library only (plus PyYAML, already used), plain JavaScript in `stack_ui/page.html`, `unittest` with the mock libpod API in `tests/`.

**Spec:** `docs/superpowers/specs/2026-10-23-resource-usage-design.md`

## Global Constraints

- No new dependency; the server stays standard library + PyYAML, the page stays one HTML file without libraries.
- Python 3.9 compatible (`from __future__ import annotations` is already at the top of the modules; no `match`, no `Path.write_text(newline=)`).
- Files are written with LF line ends.
- Every `localStorage` read and write is in `try/catch`; the page works without it (default refresh: 10 s).
- Page texts are generic English; nothing from the user's own `stacks/` folder (it is gitignored and never committed or quoted in docs, tests or the spec) appears in any file.
- Sizes: MB below 1 GB, else GB with one decimal. Memory share of the limit: from 80 % `t-warn`, from 95 % `t-bad`, else `t-ok`; no limit: no colour.
- Commit messages end with the line `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Target version `1.0.7`.

## Review Focus

- A container restarted between two readings (its `CPUNano` goes down): no negative or garbage CPU %; it is measured again. Pinned in Task 2.
- Two readings less than a second apart (two tabs, the reload button pressed twice): the last value is repeated, not a noisy percentage from a few milliseconds. Pinned in Task 2.
- A container with no memory limit (`MemLimit` 0, or as large as the host's memory): "no limit", no colour, and a stack with such a container shows usage only. Pinned in Tasks 2 and 3.
- Podman's stats call failing, or `/proc` unreadable: the call still answers with `error` and whatever could be read; the page does not break. Pinned in Tasks 2 and 4.
- A stack with no running containers, or not deployed: `—`, not `0 MB` or a crash. Pinned in Task 3.

---

### Task 1: Podman stats in the API client and the mock

**Files:**
- Modify: `tests/mock_libpod.py` (the `MockPodman.__init__` attributes and `route()`)
- Modify: `stack_ui/podman_api.py` (after `container_action`)
- Create: `tests/test_usage.py`

**Interfaces:**
- Produces: `pa.container_stats() -> list[dict]`, one entry per running container with the keys `Name`, `CPUNano`, `SystemNano`, `MemUsage`, `MemLimit`, `PIDs`.
- Produces (test support): `MockPodman.stats: list[dict]` (what the next `GET /containers/stats` answers) and `MockPodman.stats_next: list[list[dict]]` (after each answer, `stats` becomes the first item of this queue, if any). `tests/test_usage.py` helpers `stat(name, cpu_ns, sys_ns, mem=100_000_000, limit=268_435_456, pids=3) -> dict`, `proc_stat(user, system, idle) -> str` and the constants `MEMINFO`.

- [ ] **Step 1: Add the stats endpoint to the mock**

In `tests/mock_libpod.py`, in `MockPodman.__init__`, after the line `self.pulled: list[str] = []`, add:

```python
        self.stats: list[dict] = []        # what GET /containers/stats answers now (running containers only)
        self.stats_next: list[list[dict]] = []   # after an answer, stats becomes the first item of this queue
```

In `route()`, directly after the `if seg == ['containers', 'json']:` block (before `if seg[0] == 'containers' and len(seg) >= 2:`), add:

```python
    if seg == ['containers', 'stats']:
        answer = {'Error': None, 'Stats': mock.stats}
        if mock.stats_next:
            mock.stats = mock.stats_next.pop(0)
        return h.reply(200, answer)
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_usage.py`:

```python
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
```

- [ ] **Step 3: Run it to verify it fails**

Run: `python -m unittest tests.test_usage -v 2>&1 | tail -15`
Expected: 3 errors, `AttributeError: module 'stack_ui.podman_api' has no attribute 'container_stats'`.

- [ ] **Step 4: Implement `container_stats`**

In `stack_ui/podman_api.py`, directly after `container_action` (before `def remove_container`), add:

```python
def container_stats() -> list[dict]:
    """One reading of every running container's resource use. The CPU fields are totals since the container
    started (CPUNano: its CPU time, SystemNano: the time of the reading), so a percentage needs two readings."""
    return (call('GET', '/containers/stats', query={'stream': 'false'}) or {}).get('Stats') or []
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m unittest tests.test_usage -v 2>&1 | tail -8`
Expected: `Ran 3 tests`, `OK`.

- [ ] **Step 6: Commit**

```bash
git add tests/mock_libpod.py tests/test_usage.py stack_ui/podman_api.py
git commit -m "Podman API: container_stats (one reading of every running container's resource use)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `resources()` and `GET /api/resources`

**Files:**
- Modify: `stack_ui/server.py` (a new section before `# ---------- images: what is on this host, ...`, and one route in `Handler.api`)
- Modify: `tests/test_usage.py` (append two classes)
- Modify: `docs/superpowers/specs/2026-10-23-resource-usage-design.md` (one bullet)

**Interfaces:**
- Consumes: `pa.container_stats()` from Task 1; the mock's `stats` / `stats_next`; the test helpers `stat`, `proc_stat`, `MEMINFO`, `HOST_MEM`, `HOST_USED`.
- Produces: `resources() -> dict` with
  `{'host': {'memTotal': int, 'memUsed': int, 'cpu': float | None, 'cpus': int} | None, 'containers': {name: {'cpu': float | None, 'mem': int, 'limit': int | None, 'pids': int}}, 'error': str}`;
  module names `PROC_MEMINFO`, `PROC_STAT` (paths), `pause()` (sleeps `SECOND_READING_WAIT`), `SAMPLES`, `SHARES`.
  The route `GET /api/resources`, open to the page session and to the API token like the other GET endpoints.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_usage.py`:

```python
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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_usage 2>&1 | tail -6`
Expected: errors, `AttributeError: module 'stack_ui.server' has no attribute 'PROC_MEMINFO'` (or `resources`).

- [ ] **Step 3: Implement `resources()`**

In `stack_ui/server.py`, replace the line

```python
# ---------- images: what is on this host, and removing the untagged leftovers of pulls ----------
```

with this section followed by that same line:

```python
# ---------- resource usage: CPU and memory of the containers and of the host ----------

PROC_MEMINFO = Path('/proc/meminfo')   # inside the container these show the host's values
PROC_STAT = Path('/proc/stat')
SECOND_READING_WAIT = 1.2              # seconds between the two readings of a first call
MIN_INTERVAL_NS = 1_000_000_000        # readings closer than this repeat the last value
USER_HZ = 100                          # ticks per second in /proc/stat
RESOURCES_LOCK = threading.Lock()
SAMPLES: dict[str, tuple[int, int]] = {}   # container name -> (CPUNano, SystemNano) of its last reading
SHARES: dict[str, float] = {}              # container name -> its last CPU percent
HOST_SAMPLE: tuple[int, int] | None = None  # (busy, total) ticks of the last /proc/stat reading
HOST_LAST: float | None = None             # the host's last CPU percent


def pause() -> None:
    time.sleep(SECOND_READING_WAIT)


def read_meminfo() -> tuple[int, int]:
    """(MemTotal, MemTotal - MemAvailable) in bytes."""
    values = {}
    for line in PROC_MEMINFO.read_text(encoding='utf-8').splitlines():
        key, _, rest = line.partition(':')
        number = rest.split()[:1]
        if number and number[0].isdigit():
            values[key] = int(number[0]) * 1024
    return values['MemTotal'], values['MemTotal'] - values['MemAvailable']


def read_cpu_times() -> tuple[int, int, int]:
    """(busy ticks, total ticks, number of cores): the first line of /proc/stat is every core together."""
    lines = PROC_STAT.read_text(encoding='utf-8').splitlines()
    fields = [int(x) for x in lines[0].split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)   # idle + iowait
    total = sum(fields[:8])                                    # guest time is already in user
    return total - idle, total, sum(1 for line in lines if re.match(r'^cpu\d+ ', line))


def host_cpu_percent(now: tuple[int, int, int]) -> float | None:
    """The host's busy share since the last reading (None until there are two readings a second apart)."""
    global HOST_SAMPLE, HOST_LAST
    busy, total, cpus = now
    prev = HOST_SAMPLE
    if prev is not None and busy >= prev[0] and total - prev[1] >= cpus * USER_HZ:
        HOST_SAMPLE = (busy, total)
        HOST_LAST = round(100 * (busy - prev[0]) / (total - prev[1]), 1)
    elif prev is not None and busy >= prev[0] and 0 <= total - prev[1] < cpus * USER_HZ and HOST_LAST is not None:
        pass   # read again within a second: the last value stands
    else:
        HOST_SAMPLE, HOST_LAST = (busy, total), None
    return HOST_LAST


def cpu_shares(stats: list[dict], cpus: int) -> tuple[dict[str, float | None], bool]:
    """({container: CPU percent of the whole host or None}, whether one had no usable previous reading); the
    stored readings move on to these ones, and a container that is gone is forgotten."""
    out: dict[str, float | None] = {}
    missing = False
    names = set()
    for s in stats:
        name = str(s.get('Name') or '')
        if not name:
            continue
        names.add(name)
        now = (int(s.get('CPUNano') or 0), int(s.get('SystemNano') or 0))
        prev = SAMPLES.get(name)
        if prev and now[0] >= prev[0] and now[1] - prev[1] >= MIN_INTERVAL_NS:
            SAMPLES[name] = now
            SHARES[name] = out[name] = round(max(0.0, (now[0] - prev[0]) / (now[1] - prev[1]) / cpus * 100), 1)
        elif prev and now[0] >= prev[0] and 0 <= now[1] - prev[1] < MIN_INTERVAL_NS and name in SHARES:
            out[name] = SHARES[name]   # read again within a second: the last value stands
        else:                          # the first reading, or a restarted container (its CPU time went down)
            SAMPLES[name] = now
            SHARES.pop(name, None)
            out[name] = None
            missing = True
    for gone in set(SAMPLES) - names:
        SAMPLES.pop(gone, None)
        SHARES.pop(gone, None)
    return out, missing


def read_usage(errors: list[str]) -> tuple[list[dict] | None, tuple[int, int, int] | None]:
    """Podman's stats and the host's CPU ticks; None for what could not be read (and why, in errors)."""
    try:
        stats: list[dict] | None = pa.container_stats()
    except pa.PodmanError as exc:
        stats = None
        errors.append(str(exc))
    try:
        cpu_now: tuple[int, int, int] | None = read_cpu_times()
    except (OSError, ValueError, IndexError):
        cpu_now = None
        errors.append('the host CPU could not be read (/proc/stat)')
    return stats, cpu_now


def resources() -> dict:
    """{host: {memTotal, memUsed, cpu, cpus} | None, containers: {name: {cpu, mem, limit, pids}}, error}. CPU is a
    percent of the whole host (every core together is 100), from two readings: the stored one, or, on a first
    call or for a container without a usable stored reading, a second one taken a moment later. limit is None
    for a container without a memory limit."""
    errors: list[str] = []
    with RESOURCES_LOCK:
        stats, cpu_now = read_usage(errors)
        cpus = cpu_now[2] if cpu_now else (os.cpu_count() or 1)
        shares, missing = cpu_shares(stats, cpus) if stats is not None else ({}, False)
        host_cpu = host_cpu_percent(cpu_now) if cpu_now else None
        if missing or (cpu_now and host_cpu is None):
            pause()
            again, cpu_again = read_usage(errors)
            if again is not None:
                stats, (shares, _) = again, cpu_shares(again, cpus)
            if cpu_again:
                cpu_now, host_cpu = cpu_again, host_cpu_percent(cpu_again)
    try:
        mem_total, mem_used = read_meminfo()
    except (OSError, ValueError, KeyError):
        mem_total = mem_used = None
        errors.append('the host memory could not be read (/proc/meminfo)')
    host = ({'memTotal': mem_total, 'memUsed': mem_used, 'cpu': host_cpu, 'cpus': cpus}
            if mem_total is not None and cpu_now else None)
    containers = {}
    for s in stats or []:
        name = str(s.get('Name') or '')
        if not name:
            continue
        limit = int(s.get('MemLimit') or 0)
        containers[name] = {'cpu': shares.get(name), 'mem': int(s.get('MemUsage') or 0),
                            'limit': limit if 0 < limit and not (host and limit >= host['memTotal']) else None,
                            'pids': int(s.get('PIDs') or 0)}
    return {'host': host, 'containers': containers, 'error': '; '.join(dict.fromkeys(errors))}


# ---------- images: what is on this host, and removing the untagged leftovers of pulls ----------
```

- [ ] **Step 4: Add the route**

In `Handler.api`, directly after

```python
            if method == 'GET' and parts == ['api', 'drift']:
                return self.json(200, drift())
```

add:

```python
            if method == 'GET' and parts == ['api', 'resources']:
                return self.json(200, resources())
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m unittest tests.test_usage -v 2>&1 | tail -20`
Expected: `Ran 12 tests`, `OK`.

- [ ] **Step 6: Record the one-second rule in the spec**

In `docs/superpowers/specs/2026-10-23-resource-usage-design.md`, after the bullet that starts `  - **First call**, or a call where a running container has no usable previous sample:` (it ends with `however old.`), add this bullet:

```markdown
  - **Readings closer than 1 s** (two open tabs, the reload button pressed twice): the stored sample is kept and
    the last value is repeated, so a few milliseconds never give a noisy percentage. The same for the host's CPU
    (less than one second of ticks, `USER_HZ` = 100 per core).
```

- [ ] **Step 7: Run the whole suite and commit**

Run: `python -m unittest discover -s tests 2>&1 | grep -E "^(Ran|OK|FAILED|FAIL:)"`
Expected: `Ran 141 tests` and `OK`.

```bash
git add stack_ui/server.py tests/test_usage.py docs/superpowers/specs/2026-10-23-resource-usage-design.md
git commit -m "Server: GET /api/resources (CPU and memory per container and for the host)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The page: data, Stacks list, Summary tab, Settings

**Files:**
- Modify: `stack_ui/page.html`
- Create (outside the repository, never committed): `<scratch>/e2e_usage.py`, where `<scratch>` is any temporary directory

**Interfaces:**
- Consumes: `GET /api/resources` from Task 2.
- Produces (page globals, used by Task 4): `RES`, `REFRESH`, `REFRESH_CHOICES`, `fmtMem(n)`, `fmtCpu(v)`, `memClass(used, limit) -> 't-ok' | 't-warn' | 't-bad' | ''`, `BAR_CLASS`, `usageBar(used, total, cls)`, `usageOf(name)`, `memTag(u)`, `usesResources()`, `loadResources()`, `scheduleResources()`. CSS classes `.hostline`, `.bar` with `.b-ok .b-warn .b-bad .b-idle`.

All edits below are in `stack_ui/page.html`. "Replace X with Y" means one exact-text replacement.

- [ ] **Step 1: State and the refresh setting**

Replace

```js
let FILTER = 'all'; let QUERY = '';
```

with

```js
let FILTER = 'all'; let QUERY = '';
let RES = null;                                  // /api/resources: {host, containers, error}
let RES_BUSY = false; let RES_TIMER = null;
const REFRESH_CHOICES = [0, 5, 10, 30, 60];      // seconds between resource readings; 0: only when asked
let REFRESH = 10;
try {
  const saved = localStorage.getItem('stack-ui-refresh');
  if (saved !== null && REFRESH_CHOICES.includes(Number(saved))) REFRESH = Number(saved);
} catch { /* storage unavailable */ }
```

- [ ] **Step 2: The helpers, the fetch and the timer**

Replace the comment line

```js
// ---------- stacks list ----------
```

with

```js
// ---------- resource usage (/api/resources: the CPU and memory numbers) ----------
const fmtMem = (n) => (n < 1024 ** 3 ? `${Math.round(n / 1024 ** 2)} MB` : `${(n / 1024 ** 3).toFixed(1)} GB`);
const fmtCpu = (v) => (v === null || v === undefined ? '—' : `${v.toFixed(1)} %`);
// the share of the memory limit in use: a warning from 80 %, bad from 95 %; without a limit no colour
function memClass(used, limit) {
  if (!limit) return '';
  const r = used / limit;
  return r >= 0.95 ? 't-bad' : r >= 0.8 ? 't-warn' : 't-ok';
}
const BAR_CLASS = { 't-ok': 'b-ok', 't-warn': 'b-warn', 't-bad': 'b-bad', '': 'b-idle' };
function usageBar(used, total, cls) {
  const pct = total ? Math.min(100, Math.round((used / total) * 100)) : 0;
  return `<span class="bar ${cls}" role="img" aria-label="${pct} %"><i style="width:${pct}%"></i></span>`;
}
const usageOf = (name) => (RES && RES.containers[name]) || null;
function memTag(u) {   // u: {mem, limit} (limit 0: none) or null
  if (!u) return '<span class="muted small">—</span>';
  const cls = memClass(u.mem, u.limit);
  return `<span class="${cls ? `tag ${cls}` : 'small'}">${fmtMem(u.mem)}${u.limit ? ` / ${fmtMem(u.limit)}` : ''}</span>`;
}
// a stack's running containers: their use over their limits (the limits only count when every one has one)
function stackUsage(s) {
  const rows = s.containers.map((c) => usageOf(c.name)).filter(Boolean);
  if (!rows.length) return null;
  return { mem: rows.reduce((a, r) => a + r.mem, 0), limit: rows.every((r) => r.limit) ? rows.reduce((a, r) => a + r.limit, 0) : 0 };
}
function memCell(s) {
  const u = stackUsage(s);
  if (!u) return `<span class="muted small">${RES ? '—' : '…'}</span>`;
  const title = s.containers.map((c) => {
    const r = usageOf(c.name);
    return r ? `${c.name}: ${fmtMem(r.mem)}${r.limit ? ` / ${fmtMem(r.limit)}` : ' (no limit)'} · CPU ${fmtCpu(r.cpu)}` : '';
  }).filter(Boolean).join('\n');
  return `<span title="${esc(title)}">${memTag(u)}</span>`;
}
function hostLine() {
  const h = RES && RES.host;
  if (!h) return '';
  const cls = memClass(h.memUsed, h.memTotal);
  return `<span>RAM <span class="${cls ? `tag ${cls}` : 'small'}">${fmtMem(h.memUsed)} / ${fmtMem(h.memTotal)}</span></span>
    <span>CPU ${fmtCpu(h.cpu)} of ${h.cpus} cores</span>`;
}
const usesResources = () => ROUTE.view === 'stacks' || ROUTE.view === 'resources' || (ROUTE.view === 'detail' && ROUTE.tab === 'summary');
async function loadResources() {
  if (RES_BUSY) return;
  RES_BUSY = true;
  try { RES = await api('/api/resources'); } catch (e) { RES = { host: null, containers: {}, error: e.message }; }
  RES_BUSY = false;
  refreshViews();
}
function scheduleResources() {
  clearInterval(RES_TIMER);
  RES_TIMER = REFRESH ? setInterval(() => { if (!document.hidden && usesResources()) loadResources(); }, REFRESH * 1000) : null;
}
document.addEventListener('visibilitychange', () => { if (!document.hidden && REFRESH && usesResources()) loadResources(); });

// ---------- stacks list ----------
```

- [ ] **Step 3: Fetch with every load, on every navigation, and start the timer**

Replace

```js
  loadDrift();
  loadUpdates();
}
```

with

```js
  loadDrift();
  loadUpdates();
  if (usesResources()) loadResources();
}
```

Replace

```js
  if (previous !== ROUTE.view) window.scrollTo(0, 0);
}
```

with

```js
  if (previous !== ROUTE.view) window.scrollTo(0, 0);
  if (usesResources()) loadResources();
}
```

At the very end of the script, replace the last two lines (`render();` and `load();`, each on a line of its own) with:

```js
scheduleResources();
render();
load();
```

- [ ] **Step 4: Stacks list: host line, Memory column**

Replace

```js
    <div class="chips" id="chips" role="toolbar" aria-label="Show"></div>
    <div class="table stacks" id="table"></div>
```

with

```js
    <div class="chips" id="chips" role="toolbar" aria-label="Show"></div>
    <div class="hostline" id="hostline"></div>
    <div class="table stacks" id="table"></div>
```

Replace

```js
  $('chips').innerHTML = FILTERS.map(
```

with

```js
  $('hostline').innerHTML = hostLine();
  $('chips').innerHTML = FILTERS.map(
```

Replace `<span>Containers</span><span>Deployed</span>` with `<span>Containers</span><span>Memory</span><span>Deployed</span>`.

Replace

```js
    <span class="c-cont">${conts}</span>
    <span class="c-drift">${driftCell(s)}</span>
```

with

```js
    <span class="c-cont">${conts}</span>
    <span class="c-mem">${memCell(s)}</span>
    <span class="c-drift">${driftCell(s)}</span>
```

- [ ] **Step 5: Summary tab: CPU and Memory columns**

Replace `<span>Name</span><span>Image</span><span>Status</span><span>Image update</span><span></span></div>${rows}` with `<span>Name</span><span>Image</span><span>Status</span><span>CPU</span><span>Memory</span><span>Image update</span><span></span></div>${rows}`.

Replace

```js
      <span class="k-stat small muted">${esc(c.state)} ${healthTag(c.status)}</span>
```

with

```js
      <span class="k-stat small muted">${esc(c.state)} ${healthTag(c.status)}</span>
      <span class="k-cpu small">${usageOf(c.name) ? fmtCpu(usageOf(c.name).cpu) : '—'}</span>
      <span class="k-mem">${memTag(usageOf(c.name))}</span>
```

- [ ] **Step 6: Settings: the refresh choice**

In `viewSettings`, replace

```js
        ${st.tokenSet ? '<button id="tok-revoke" class="danger">Revoke</button>' : ''}<button id="tok-new">${st.tokenSet ? 'Generate a new one' : 'Generate'}</button></div>
    </section>`;
```

with

```js
        ${st.tokenSet ? '<button id="tok-revoke" class="danger">Revoke</button>' : ''}<button id="tok-new">${st.tokenSet ? 'Generate a new one' : 'Generate'}</button></div>
    </section>
    <section class="panel" style="max-width:640px">
      <h2>Resource numbers</h2>
      <p class="muted small">How often the CPU and memory numbers are read while a page that shows them is open (never while this tab is hidden). Kept in this browser.</p>
      <label class="field"><span>Refresh</span><select id="res-refresh">${REFRESH_CHOICES.map((v) =>
        `<option value="${v}" ${v === REFRESH ? 'selected' : ''}>${v ? `every ${v} s` : 'Off (only the reload buttons)'}</option>`).join('')}</select></label>
    </section>`;
  $('res-refresh').addEventListener('change', (e) => {
    REFRESH = Number(e.target.value);
    try { localStorage.setItem('stack-ui-refresh', String(REFRESH)); } catch { /* storage unavailable */ }
    scheduleResources();
    toast(REFRESH ? `Resource numbers: every ${REFRESH} s` : 'Resource numbers: only when reloaded');
  });
```

- [ ] **Step 7: CSS**

Replace

```css
  .tr { display: grid; grid-template-columns: 36px 34px minmax(150px, 1.3fr) minmax(170px, 1.7fr) 104px 140px 124px;
```

with

```css
  .tr { display: grid; grid-template-columns: 36px 34px minmax(150px, 1.3fr) minmax(170px, 1.7fr) 150px 104px 140px 124px;
```

Replace

```css
  .conts .tr { grid-template-columns: minmax(130px, 1fr) minmax(170px, 1.6fr) minmax(110px, .9fr) 130px 170px; }
```

with

```css
  .conts .tr { grid-template-columns: minmax(130px, 1fr) minmax(170px, 1.6fr) minmax(110px, .9fr) 70px 150px 130px 170px; }
```

Replace

```css
  .tag.hl { font-size: 11px; padding: 0 6px; }
```

with

```css
  .tag.hl { font-size: 11px; padding: 0 6px; }
  .hostline { display: flex; flex-wrap: wrap; gap: 4px 22px; align-items: center; margin: 0 0 12px; font-size: 13px; color: var(--muted); }
  .hostline:empty { display: none; }
  .bar { display: inline-block; width: 90px; height: 6px; border-radius: 3px; background: var(--line); overflow: hidden; vertical-align: middle; }
  .bar > i { display: block; height: 100%; }
  .b-ok > i { background: var(--ok); } .b-warn > i { background: var(--warn); } .b-bad > i { background: var(--bad); } .b-idle > i { background: var(--idle); }
```

In the `@media (max-width: 760px)` block, replace

```css
                  grid-template-areas: "sel state name act" "sel state cont cont" "sel state drift img"; gap: 6px 10px; }
```

with

```css
                  grid-template-areas: "sel state name act" "sel state cont cont" "sel state mem mem" "sel state drift img"; gap: 6px 10px; }
```

Replace

```css
    .c-drift { grid-area: drift; } .c-img { grid-area: img; justify-self: start; } .c-act { grid-area: act; }
    .c-drift > .muted, .c-img > .muted { display: none; }
```

with

```css
    .c-drift { grid-area: drift; } .c-img { grid-area: img; justify-self: start; } .c-act { grid-area: act; } .c-mem { grid-area: mem; }
    .c-drift > .muted, .c-img > .muted, .c-mem > .muted { display: none; }
```

Replace

```css
    .conts .tr { grid-template-columns: minmax(0, 1fr) auto; grid-template-areas: "kname kact" "kimg kimg" "kstat kupd"; gap: 4px 10px; }
    .k-name { grid-area: kname; } .k-img { grid-area: kimg; } .k-stat { grid-area: kstat; } .k-upd { grid-area: kupd; } .k-act { grid-area: kact; }
```

with

```css
    .conts .tr { grid-template-columns: minmax(0, 1fr) auto; grid-template-areas: "kname kact" "kimg kimg" "kstat kupd" "kcpu kmem"; gap: 4px 10px; }
    .k-name { grid-area: kname; } .k-img { grid-area: kimg; } .k-stat { grid-area: kstat; } .k-upd { grid-area: kupd; } .k-act { grid-area: kact; }
    .k-cpu { grid-area: kcpu; } .k-mem { grid-area: kmem; justify-self: end; }
```

- [ ] **Step 8: Syntax check and the existing tests**

Run (from the repository root; `<scratch>` as above):

```bash
python -c "import re,pathlib;t=pathlib.Path('stack_ui/page.html').read_text(encoding='utf-8');pathlib.Path(r'<scratch>/page.js').write_text('\n'.join(re.findall(r'<script[^>]*>(.*?)</script>',t,re.S)),encoding='utf-8')"
node --check "<scratch>/page.js" && echo SYNTAX OK
python -m unittest discover -s tests 2>&1 | grep -E "^(Ran|OK|FAILED|FAIL:)"
```

Expected: `SYNTAX OK`, then `Ran 141 tests` and `OK`.

- [ ] **Step 9: The browser check**

Create `<scratch>/e2e_usage.py` (a throwaway: it is never committed):

```python
"""Throwaway browser check: the resource numbers in the page, against the mock libpod and fake files."""
import os, shlex, shutil, sys, tempfile, threading, time
from pathlib import Path

REPO = Path.cwd()
tmp = Path(tempfile.mkdtemp())
stacks = tmp / 'stacks'; stacks.mkdir()
os.environ.update(STACKS_DIR=str(stacks), STACK_UI_DATA=str(tmp / 'data'), FAKE_COMPOSE_LOG=str(tmp / 'compose.log'))
sys.path.insert(0, str(REPO))
from tests.support import compose_yaml, make_stack, FAKE_COMPOSE  # noqa
from tests.mock_libpod import MockPodman  # noqa
from tests.test_usage import MEMINFO, proc_stat, stat  # noqa
from stack_ui import podman_api as pa, stacks as st, server as srv  # noqa

MiB = 1024 ** 2
mock = MockPodman(); pa.API_URL = mock.start()
st.COMPOSE_CMD = ' '.join(shlex.quote(Path(x).as_posix()) for x in (sys.executable, FAKE_COMPOSE))
for name in ('web', 'api', 'mixed'):
    st.save_deployed(make_stack(stacks, name, compose_yaml(name)), 'compose.yaml', '.env')
make_stack(stacks, 'fresh', compose_yaml('fresh'))
for name, project in (('web', 'web'), ('api', 'api'), ('mixed-a', 'mixed'), ('mixed-b', 'mixed'), ('extra', '')):
    mock.add_container(name, project=project)
(tmp / 'meminfo').write_text(MEMINFO); (tmp / 'stat').write_text(proc_stat(100, 100, 800))
srv.PROC_MEMINFO = tmp / 'meminfo'; srv.PROC_STAT = tmp / 'stat'
srv.pause = lambda: (tmp / 'stat').write_text(proc_stat(200, 100, 1700))
mock.stats = [stat('web', 10**9, 10 * 10**9, 90 * MiB, 100 * MiB), stat('api', 10**9, 10 * 10**9, 96 * MiB, 100 * MiB),
              stat('mixed-a', 10**9, 10 * 10**9, 20 * MiB, 100 * MiB), stat('mixed-b', 10**9, 10 * 10**9, 5 * MiB, 0),
              stat('extra', 10**9, 10 * 10**9, 10 * MiB, 0)]
mock.stats_next = [[stat('web', 15 * 10**8, 11 * 10**9, 90 * MiB, 100 * MiB), stat('api', 10**9, 11 * 10**9, 96 * MiB, 100 * MiB),
                    stat('mixed-a', 10**9, 11 * 10**9, 20 * MiB, 100 * MiB), stat('mixed-b', 10**9, 11 * 10**9, 5 * MiB, 0),
                    stat('extra', 10**9, 11 * 10**9, 10 * MiB, 0)]]

threading.Thread(target=srv.main, args=(['--port', '8797', '--no-browser'],), daemon=True).start()
time.sleep(1)
from playwright.sync_api import sync_playwright  # noqa
BASE = 'http://127.0.0.1:8797/'
results = []
def check(label, ok, extra=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + label + (f'  {extra}' if not ok else ''))
def stats_calls(): return len([c for c in mock.calls if c[1] == '/containers/stats'])

with sync_playwright() as p:
    b = p.chromium.launch(channel='chrome'); pg = b.new_page()
    errors = []; pg.on('pageerror', lambda e: errors.append(str(e)))
    pg.on('dialog', lambda d: d.accept())
    pg.goto(BASE); pg.wait_for_selector('.hostline .tag')
    host = pg.inner_text('#hostline'); print('HOST LINE:', repr(host))
    check('host line: RAM and CPU', '2.9 GB / 3.8 GB' in host and 'CPU 10.0 % of 4 cores' in host, host)
    rows = pg.eval_on_selector_all('.tr[data-row]', 'rs => Object.fromEntries(rs.map(r => [r.dataset.row, [r.querySelector(".c-mem .tag")?.className || "", r.querySelector(".c-mem").innerText]]))')
    print('MEMORY CELLS:', rows)
    check('web 90 / 100 MB is a warning', 't-warn' in rows['web'][0] and rows['web'][1] == '90 MB / 100 MB', rows['web'])
    check('api 96 / 100 MB is bad', 't-bad' in rows['api'][0], rows['api'])
    check('a stack with an unlimited container: usage only, no colour', rows['mixed'][0] == '' and rows['mixed'][1] == '25 MB', rows['mixed'])
    check('a stack that is not deployed: —', rows['fresh'][1] == '—', rows['fresh'])
    pg.goto(BASE + '#/stacks/web/summary'); pg.wait_for_selector('.conts .tr:not(.th)')
    row = pg.inner_text('.conts .tr:not(.th)'); print('SUMMARY ROW:', repr(row))
    check('summary: CPU and memory of the container', '12.5 %' in row and '90 MB / 100 MB' in row, row)
    pg.goto(BASE + '#/settings'); pg.wait_for_selector('#res-refresh')
    check('settings: default 10 s', pg.input_value('#res-refresh') == '10')
    pg.select_option('#res-refresh', '5')
    check('settings: stored in the browser', pg.evaluate("localStorage.getItem('stack-ui-refresh')") == '5')
    pg.goto(BASE + '#/stacks'); time.sleep(1.5); n0 = stats_calls(); time.sleep(6.5)
    check('every 5 s: the numbers are read again', stats_calls() > n0, f'{n0} -> {stats_calls()}')
    pg.goto(BASE + '#/settings'); pg.wait_for_selector('#res-refresh'); pg.select_option('#res-refresh', '0')
    pg.goto(BASE + '#/stacks'); time.sleep(1.5); n0 = stats_calls(); time.sleep(6.5)
    check('Off: no reading on its own', stats_calls() == n0, f'{n0} -> {stats_calls()}')
    pg.click('#reload'); time.sleep(1.5)
    check('Off: the reload button reads them', stats_calls() > n0, f'{n0} -> {stats_calls()}')
    check('no page errors', not errors, errors)
    b.close()
mock.stop(); shutil.rmtree(tmp, True)
print('ALL PASS' if all(results) else 'SOME FAILED')
```

Run: `python -X utf8 <scratch>/e2e_usage.py 2>&1 | grep -E "^(PASS|FAIL|HOST|MEMORY|SUMMARY|ALL|SOME)"`
Expected: every line `PASS` and `ALL PASS`. (About 20 seconds, from the two 8-second waits.)

- [ ] **Step 10: Commit**

```bash
git add stack_ui/page.html
git commit -m "Page: CPU and memory in the Stacks list and a stack's Summary tab; refresh set under Settings" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The Resources page

**Files:**
- Modify: `stack_ui/page.html`
- Modify: `<scratch>/e2e_usage.py` (the throwaway from Task 3)

**Interfaces:**
- Consumes: `RES`, `usageBar`, `BAR_CLASS`, `memClass`, `fmtMem`, `fmtCpu`, `loadResources` and the CSS `.bar` from Task 3; `STACKS`, `enc`, `esc`, `errBox`, `icon`, `$`, `ROUTE` from the existing page.
- Produces: the route `#/resources`, `viewResources()`, `renderResources()`, a "Resources" entry in the left menu.

- [ ] **Step 1: The icon, the route, the menu entry**

In the `ICON` object, replace

```js
  containers: '<rect x="3" y="7" width="18" height="12" rx="2"/><path d="M7 7V5h10v2M7 11v4M11 11v4M15 11v4"/>',
```

with

```js
  containers: '<rect x="3" y="7" width="18" height="12" rx="2"/><path d="M7 7V5h10v2M7 11v4M11 11v4M15 11v4"/>',
  resources: '<path d="M4 20V11M10 20V4M16 20v-6M22 20H2"/>',
```

In `parseHash`, replace

```js
  if (p[0] === 'containers') return { view: 'containers' };
```

with

```js
  if (p[0] === 'containers') return { view: 'containers' };
  if (p[0] === 'resources') return { view: 'resources' };
```

In `render`, replace `containers: viewContainers, logs: viewLogs,` with `containers: viewContainers, resources: viewResources, logs: viewLogs,`.

In `refreshViews`, replace

```js
  if (ROUTE.view === 'images') renderImages();
```

with

```js
  if (ROUTE.view === 'images') renderImages();
  if (ROUTE.view === 'resources') renderResources();
```

In the navigation, replace

```html
    <a class="nav-a" href="#/containers" data-nav="containers"></a>
```

with

```html
    <a class="nav-a" href="#/containers" data-nav="containers"></a>
    <a class="nav-a" href="#/resources" data-nav="resources"></a>
```

In the start section, replace `[['containers', 'Containers'], ['logs', 'Logs'],` with `[['containers', 'Containers'], ['resources', 'Resources'], ['logs', 'Logs'],`.

- [ ] **Step 2: The view**

Insert before the line `async function viewSettings() {`:

```js
function viewResources() {
  $('view').innerHTML = `
    <div class="page-head">
      <div><h1>Resources</h1><p class="sub">CPU and memory of the running containers, the largest memory use first. CPU is a share of the whole host: every core together is 100 %.</p></div>
      <div class="head-actions"><button class="icon-btn" id="res-reload" title="Reload" aria-label="Reload">${icon('refresh')}</button></div>
    </div>
    <div id="res-body"><p class="muted pad">Loading…</p></div>`;
  $('res-reload').addEventListener('click', loadResources);
  renderResources();
}
function renderResources() {
  if (ROUTE.view !== 'resources' || !$('res-body') || !RES) return;
  const h = RES.host;
  const owner = (name) => (STACKS.find((s) => s.containers.some((c) => c.name === name)) || {}).name || '';
  const rows = Object.entries(RES.containers).sort((a, b) => b[1].mem - a[1].mem || a[0].localeCompare(b[0]));
  const hostPanel = h ? `<section class="panel"><dl class="kv">
      <dt>Memory</dt><dd>${usageBar(h.memUsed, h.memTotal, BAR_CLASS[memClass(h.memUsed, h.memTotal)])} ${fmtMem(h.memUsed)} / ${fmtMem(h.memTotal)}</dd>
      <dt>CPU</dt><dd>${usageBar(h.cpu || 0, 100, 'b-idle')} ${fmtCpu(h.cpu)} of ${h.cpus} cores</dd></dl></section>` : '';
  const table = rows.length ? `<div class="table usage"><div class="tr th"><span>Container</span><span>Stack</span><span>CPU</span><span>Memory</span><span>Limit</span><span>PIDs</span></div>${
    rows.map(([name, r]) => {
      const stack = owner(name);
      return `<div class="tr"><span><b>${esc(name)}</b></span>
        <span>${stack ? `<a class="tag t-idle" href="#/stacks/${enc(stack)}/summary">${esc(stack)}</a>` : '<span class="muted small">—</span>'}</span>
        <span class="small">${fmtCpu(r.cpu)}</span>
        <span class="small">${usageBar(r.mem, r.limit || (h ? h.memTotal : 0), BAR_CLASS[memClass(r.mem, r.limit)])} ${fmtMem(r.mem)}</span>
        <span class="small">${r.limit ? fmtMem(r.limit) : 'no limit'}</span>
        <span class="small">${r.pids}</span></div>`;
    }).join('')}</div>` : '<div class="empty">No container is running.</div>';
  $('res-body').innerHTML = `${RES.error ? errBox(RES.error) : ''}${hostPanel}${table}`;
}

```

- [ ] **Step 3: CSS**

After the line `  .b-ok > i { background: var(--ok); } ...` (added in Task 3), add:

```css
  .usage .tr { grid-template-columns: minmax(150px, 1.3fr) minmax(110px, 1fr) 80px minmax(190px, 1.6fr) 90px 60px; }
```

In the `@media (max-width: 760px)` block, after the line `    .res .tr { grid-template-columns: minmax(0, 1fr) auto; }` add:

```css
    .usage .tr { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
```

- [ ] **Step 4: Extend the browser check**

In `<scratch>/e2e_usage.py`, replace the line `    check('no page errors', not errors, errors)` with:

```python
    pg.goto(BASE + '#/resources'); pg.wait_for_selector('.usage .tr:not(.th)')
    order = pg.eval_on_selector_all('.usage .tr:not(.th)', 'rs => rs.map(r => r.querySelector("b").innerText)')
    print('RESOURCES ORDER:', order)
    check('resources: largest memory first', order == ['api', 'web', 'mixed-a', 'extra', 'mixed-b'], order)
    cells = pg.eval_on_selector_all('.usage .tr:not(.th)', 'rs => rs.map(r => [...r.children].map(c => c.innerText.trim()))')
    check('resources: stack link, CPU, limit', cells[1][1] == 'web' and cells[1][2] == '12.5 %' and cells[1][4] == '100 MB', cells[1])
    check('resources: no limit, no stack', cells[3][4] == 'no limit' and cells[3][1] == '—', cells[3])
    check('resources: host panel', '2.9 GB / 3.8 GB' in pg.inner_text('#res-body'))
    check('menu entry is highlighted', pg.get_attribute('[data-nav="resources"]', 'aria-current') == 'page')
    mock.fail[('GET', '/containers/stats')] = (500, 'down')
    pg.click('#res-reload'); pg.wait_for_selector('.banner')
    check('resources: a Podman error shows a banner and keeps the host', 'down' in pg.inner_text('.banner') and '3.8 GB' in pg.inner_text('#res-body'))
    check('no page errors', not errors, errors)
```

Run: `python -X utf8 <scratch>/e2e_usage.py 2>&1 | grep -E "^(PASS|FAIL|RESOURCES|ALL|SOME)"`
Expected: every line `PASS` and `ALL PASS`.

- [ ] **Step 5: Syntax check and the whole suite**

Run the Step 8 commands of Task 3 again.
Expected: `SYNTAX OK`, `Ran 141 tests`, `OK`.

- [ ] **Step 6: Commit**

```bash
git add stack_ui/page.html
git commit -m "Page: a Resources page with every running container's CPU and memory" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Documentation and the 1.0.7 release commit

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `stack_ui/__init__.py`
- Modify: `tests/test_server.py`

- [ ] **Step 1: README**

In `README.md`, replace

```markdown
- **Logs**, **Networks**, **Volumes** and **Secrets**
```

with

```markdown
- **Resources**: the CPU and memory of every running container and of the host; also on the Stacks list (a
  *Memory* column and a line with the host's RAM and CPU) and on a stack's Summary tab. A container near its
  memory limit turns yellow (80 %), then red (95 %). Refreshed every 10 seconds by default; *Settings* has the
  choice (off, 5, 10, 30, 60 seconds), kept in the browser. The page asks Podman for the numbers; nothing is
  stored.
- **Logs**, **Networks**, **Volumes** and **Secrets**
```

- [ ] **Step 2: CHANGELOG and version**

In `CHANGELOG.md`, replace the first line pair

```markdown
# Changelog

```

with

```markdown
# Changelog

## 1.0.7

- *Resources*: the current CPU and memory of every running container and of the host, on a new page (largest
  memory use first), as a *Memory* column and a host line on the Stacks list, and on a stack's Summary tab.
  CPU is a share of the whole host, computed from two readings (Podman's own figure is an average since the
  container started). A container with no memory limit shows `no limit`.
- *Settings*: how often the numbers refresh (off, 5, 10, 30, 60 seconds; default 10; paused while the tab is hidden).
- API: `GET /api/resources`.

```

In `stack_ui/__init__.py` replace `'1.0.6'` with `'1.0.7'`; in `tests/test_server.py` replace `'version': '1.0.6'` with `'version': '1.0.7'`.

- [ ] **Step 3: Run everything**

Run: `python -m unittest discover -s tests 2>&1 | grep -E "^(Ran|OK|FAILED|FAIL:)"`
Expected: `Ran 141 tests` and `OK`.
Run: `python -X utf8 <scratch>/e2e_usage.py 2>&1 | grep -E "^(FAIL|ALL|SOME)"`
Expected: `ALL PASS`.

- [ ] **Step 4: Commit**

```bash
git add README.md CHANGELOG.md stack_ui/__init__.py tests/test_server.py
git commit -m "1.0.7: Resources: CPU and memory of the containers and the host, on a page, in the Stacks list and on the Summary tab" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```
