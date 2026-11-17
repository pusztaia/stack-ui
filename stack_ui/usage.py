"""Resource usage (CPU and memory of the containers and of the host) and the Health page."""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

from . import health as hl
from . import podman_api as pa
from . import stacks as st

PROC_MEMINFO = Path('/proc/meminfo')   # inside the container these show the host's values
PROC_STAT = Path('/proc/stat')
SECOND_READING_WAIT = 1.2              # seconds between the two readings of a first call
MIN_INTERVAL_NS = 1_000_000_000        # readings closer than this repeat the last value
USER_HZ = 100                          # ticks per second in /proc/stat
RESOURCES_LOCK = threading.Lock()
SAMPLES: dict[str, tuple[str, int, int]] = {}   # container name -> (its ID, CPUNano, SystemNano) of its last reading
SHARES: dict[str, float] = {}              # container name -> its last CPU percent
HOST_SAMPLE: tuple[int, int] | None = None  # (busy, total) ticks of the last /proc/stat reading
HOST_LAST: float | None = None             # the host's last CPU percent


def pause() -> None:
    time.sleep(SECOND_READING_WAIT)


def read_meminfo() -> tuple[int, int]:
    """(MemTotal, MemTotal - MemAvailable) in bytes (health.read_meminfo of this module's PROC_MEMINFO)."""
    return hl.read_meminfo(PROC_MEMINFO)


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
        cid = str(s.get('ContainerID') or '')
        now = (int(s.get('CPUNano') or 0), int(s.get('SystemNano') or 0))
        prev = SAMPLES[name][1:] if name in SAMPLES and SAMPLES[name][0] == cid else None   # a new container of that name: none
        if prev and now[0] >= prev[0] and now[1] - prev[1] >= MIN_INTERVAL_NS:
            SAMPLES[name] = (cid, *now)
            SHARES[name] = out[name] = round(max(0.0, (now[0] - prev[0]) / (now[1] - prev[1]) / cpus * 100), 1)
        elif prev and now[0] >= prev[0] and 0 <= now[1] - prev[1] < MIN_INTERVAL_NS and name in SHARES:
            out[name] = SHARES[name]   # read again within a second: the last value stands
        else:                          # the first reading, a restarted or recreated container
            SAMPLES[name] = (cid, *now)
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
                            # no limit: Podman gives the host's memory (known even when the CPU could not be read)
                            'limit': limit if 0 < limit and not (mem_total and limit >= mem_total) else None,
                            'pids': int(s.get('PIDs') or 0)}
    return {'host': host, 'containers': containers, 'error': '; '.join(dict.fromkeys(errors))}


def health_info() -> dict:
    """The host's health (see health.py). The disks: the root file system, the stacks folder and every path named in
    STACK_UI_HEALTH_PATHS (comma separated); a file system shows once."""
    paths = ['/', *([str(st.STACKS_DIR)] if st.STACKS_DIR else [])]
    paths += [x.strip() for x in os.environ.get('STACK_UI_HEALTH_PATHS', '').split(',') if x.strip()]
    return hl.health(paths, os.cpu_count() or 1)
