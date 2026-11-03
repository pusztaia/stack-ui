"""The health of the host the stacks run on: temperature, load, uptime, disks and what Podman holds.

Everything is read from the files the kernel keeps (/sys, /proc, which inside a container show the host's values) and
from the Podman API. A reading that is not there (no thermal zone in a virtual machine) is None: the rest still works."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import podman_api as pa

THERMAL = Path('/sys/class/thermal/thermal_zone0/temp')   # millidegrees Celsius
LOADAVG = Path('/proc/loadavg')
UPTIME = Path('/proc/uptime')

TEMP_WARN, TEMP_BAD = 70.0, 80.0    # a Raspberry Pi 4 starts to slow itself down at 80 °C
DISK_WARN, DISK_BAD = 80.0, 90.0    # percent of the filesystem in use
RANK = {'ok': 0, 'warn': 1, 'bad': 2}


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


def podman_info() -> dict:
    if not pa.configured():
        return {'error': f'no Podman socket at {pa.SOCKET}'}
    try:
        containers = pa.containers()
        images = pa.images()
        return {'version': pa.version(),
                'containers': {'running': sum(1 for c in containers if c.get('State') == 'running'), 'total': len(containers)},
                'images': {'count': len(images), 'size': sum(int(i.get('Size') or 0) for i in images)}}
    except pa.PodmanError as exc:
        return {'error': str(exc)}


def health(paths: list[str], cpus: int) -> dict:
    """{temperature, load, uptime, disks, podman, level}: level is the worst of the others (None: nothing to judge)."""
    celsius, load, uptime = read_temperature(), read_load(), read_uptime()
    temperature = {'celsius': celsius, 'level': temperature_level(celsius)} if celsius is not None else None
    loaded = ({'one': load[0], 'five': load[1], 'fifteen': load[2], 'cpus': cpus, 'level': load_level(load[0], cpus)}
              if load else None)
    rows = disks(paths)
    podman = podman_info()
    levels = [x['level'] for x in (temperature, loaded) if x] + [d['level'] for d in rows] + (['bad'] if 'error' in podman else [])
    return {'temperature': temperature, 'load': loaded, 'uptime': uptime, 'disks': rows, 'podman': podman,
            'level': worst(levels)}
