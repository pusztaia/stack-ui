"""The health of the host the stacks run on: temperature, load, uptime, disks and what Podman holds.

Everything is read from the files the kernel keeps (/sys, /proc, which inside a container show the host's values) and
from the Podman API. A reading that is not there (no thermal zone in a virtual machine) is None: the rest still works."""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from . import podman_api as pa

THERMAL = Path('/sys/class/thermal/thermal_zone0/temp')   # millidegrees Celsius
LOADAVG = Path('/proc/loadavg')
UPTIME = Path('/proc/uptime')
THROTTLED = Path('/sys/devices/platform/soc/soc:firmware/get_throttled')   # the Raspberry Pi firmware's flags (hex)
THROTTLED_ROOT = Path('/sys/devices/platform')                            # where another firmware device is looked for
PROC_MEMINFO = Path('/proc/meminfo')

# a restart loop: at least LOOP_RESTARTS restarts by the restart policy and the last start less than LOOP_WINDOW
# seconds ago (a crashing container looks "running" between two crashes)
LOOP_RESTARTS, LOOP_WINDOW = 3, 600
OOM_EXIT = 137                      # a SIGKILL: what the kernel's out-of-memory killer sends
_OOM: dict[tuple[str, str], bool] = {}   # (container id, its last exit) -> killed for memory: one question per exit
TEMP_WARN, TEMP_BAD = 70.0, 80.0    # a Raspberry Pi 4 starts to slow itself down at 80 °C
DISK_WARN, DISK_BAD = 80.0, 90.0    # percent of the filesystem in use
RANK = {'ok': 0, 'warn': 1, 'bad': 2}
# (name, bit while it is so, bit when it has been so since boot) of the firmware's get_throttled value
POWER_FLAGS = (('undervoltage', 0, 16), ('freq_capped', 1, 17), ('throttled', 2, 18), ('soft_temp_limit', 3, 19))


def read_meminfo(path: Path | None = None) -> tuple[int, int]:
    """(MemTotal, MemTotal - MemAvailable) in bytes."""
    values = {}
    for line in (path or PROC_MEMINFO).read_text(encoding='utf-8').splitlines():
        key, _, rest = line.partition(':')
        number = rest.split()[:1]
        if number and number[0].isdigit():
            values[key] = int(number[0]) * 1024
    return values['MemTotal'], values['MemTotal'] - values['MemAvailable']


def host_memory() -> int | None:
    """The host's memory in bytes (None when it cannot be read). Podman gives it as the memory limit of a container
    that has none."""
    try:
        return read_meminfo()[0]
    except (OSError, KeyError, ValueError):
        return None


def read_temperature() -> float | None:
    try:
        return round(int(THERMAL.read_text(encoding='utf-8').strip()) / 1000, 1)
    except (OSError, ValueError):
        return None


def read_load() -> tuple[float, float, float] | None:
    try:
        one, five, fifteen = (float(x) for x in LOADAVG.read_text(encoding='utf-8').split()[:3])
        return one, five, fifteen
    except (OSError, ValueError):
        return None


def read_uptime() -> int | None:
    try:
        return int(float(UPTIME.read_text(encoding='utf-8').split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def read_throttled() -> int | None:
    """The firmware's get_throttled value; None when this host has no such file (not a Raspberry Pi, or not shown)."""
    candidates = [THROTTLED]
    try:
        candidates += sorted(THROTTLED_ROOT.glob('*/get_throttled'))
    except OSError:
        pass
    for path in candidates:
        try:
            return int(path.read_text(encoding='utf-8').strip(), 16)
        except (OSError, ValueError):
            continue
    return None


def power(value: int) -> dict:
    """What the flags say: now (bad), or only since boot (warn). A flag that is on now is not listed twice."""
    now = [name for name, bit, _ in POWER_FLAGS if value >> bit & 1]
    since = [name for name, _, bit in POWER_FLAGS if value >> bit & 1 and name not in now]
    return {'value': value, 'now': now, 'since_boot': since, 'level': 'bad' if now else 'warn' if since else 'ok'}


def temperature_level(celsius: float) -> str:
    return 'bad' if celsius >= TEMP_BAD else 'warn' if celsius >= TEMP_WARN else 'ok'


def disk_level(percent: float) -> str:
    return 'bad' if percent >= DISK_BAD else 'warn' if percent >= DISK_WARN else 'ok'


def load_level(one: float, cpus: int) -> str:
    """The 1-minute load against the number of cores: from 1 per core the host is busy, from 2 it is overloaded."""
    if cpus <= 0:
        return 'ok'
    ratio = one / cpus
    return 'bad' if ratio >= 2 else 'warn' if ratio >= 1 else 'ok'


def worst(levels: list[str]) -> str | None:
    return max(levels, key=RANK.__getitem__) if levels else None


def disk_usage(path: str) -> dict:
    """What df shows: used against used + available (blocks reserved for root count as used)."""
    total, used, avail = shutil.disk_usage(path)   # statvfs on Linux: used = blocks - free blocks, free = available to users
    percent = round(100 * used / (used + avail), 1) if used + avail else 0.0
    return {'path': path, 'total': total, 'used': used, 'free': avail, 'percent': percent, 'level': disk_level(percent)}


def disks(paths: list[str]) -> list[dict]:
    """The usage of the filesystem each path is on, once per filesystem; a path that cannot be read is skipped."""
    seen: set[int] = set()
    out = []
    for path in paths:
        try:
            device = os.stat(path).st_dev
            if device in seen:
                continue
            row = disk_usage(path)
        except OSError:
            continue
        seen.add(device)
        out.append(row)
    return out


def restarts(c: dict) -> int:
    """How often the container's restart policy started it again (libpod's Restarts; 0 when an older Podman has none)."""
    try:
        return max(0, int(c.get('Restarts') or 0))
    except (TypeError, ValueError):
        return 0


def looping(c: dict, now: float | None = None) -> bool:
    """Whether the container restarts in a loop: LOOP_RESTARTS or more restarts, and restarting now or started again
    within LOOP_WINDOW. A stopped container is not (whatever its count)."""
    if restarts(c) < LOOP_RESTARTS:
        return False
    if c.get('State') == 'restarting':
        return True
    try:
        started = int(c.get('StartedAt') or 0)
    except (TypeError, ValueError):
        return False
    now = time.time() if now is None else now
    return c.get('State') == 'running' and started > 0 and now - started < LOOP_WINDOW


def oom_killed(c: dict) -> bool:
    """Whether the container's last exit was the kernel's out-of-memory kill (its mem_limit was reached). Podman sets
    OOMKilled once and never clears it, so it counts only when the last exit code (libpod's list) is a kill, 137; only
    such a container is inspected, once for each exit."""
    try:
        if int(c.get('ExitCode') or 0) != OOM_EXIT:
            return False
    except (TypeError, ValueError):
        return False
    key = (str(c.get('Id') or ''), str(c.get('ExitedAt') or ''))
    if key not in _OOM:
        try:
            state = pa.inspect_container(key[0]).get('State') or {}
        except pa.PodmanError:
            return False
        if len(_OOM) > 500:
            _OOM.clear()
        _OOM[key] = bool(state.get('OOMKilled'))
    return _OOM[key]


def podman_info() -> dict:
    if not pa.configured():
        return {'error': f'no Podman socket at {pa.SOCKET}'}
    try:
        containers = pa.containers()
        images = pa.images()
        return {'version': pa.version(),
                'containers': {'running': sum(1 for c in containers if c.get('State') == 'running'), 'total': len(containers)},
                'looping': [{'name': (c.get('Names') or ['?'])[0].lstrip('/'), 'restarts': restarts(c), 'oom': oom_killed(c)}
                            for c in containers if looping(c)],
                'images': {'count': len(images), 'size': sum(int(i.get('Size') or 0) for i in images)}}
    except pa.PodmanError as exc:
        return {'error': str(exc)}


def health(paths: list[str], cpus: int) -> dict:
    """{temperature, load, power, uptime, disks, podman, level}: level is the worst of the others (None: nothing to judge)."""
    celsius, load, uptime = read_temperature(), read_load(), read_uptime()
    throttled = read_throttled()
    supply = power(throttled) if throttled is not None else None
    temperature = {'celsius': celsius, 'level': temperature_level(celsius)} if celsius is not None else None
    loaded = ({'one': load[0], 'five': load[1], 'fifteen': load[2], 'cpus': cpus, 'level': load_level(load[0], cpus)}
              if load else None)
    rows = disks(paths)
    podman = podman_info()
    levels = [x['level'] for x in (temperature, loaded, supply) if x] + [d['level'] for d in rows] + (['bad'] if 'error' in podman else [])
    levels += ['warn'] if podman.get('looping') else []
    return {'temperature': temperature, 'load': loaded, 'power': supply, 'uptime': uptime, 'disks': rows,
            'podman': podman, 'level': worst(levels)}
