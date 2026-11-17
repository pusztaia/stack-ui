"""A backup of the page's own state: its data folder (STACK_UI_DATA) as a .tar.gz, downloaded to the browser or
saved into a backup folder on the host (STACK_UI_BACKUP_DIR, the last STACK_UI_BACKUP_KEEP of them kept).

What is in it: settings.json (the API token's hash), schedule.json, deployed/ (the files of each stack's last
deploy) and rollback/ (the rollback points), plus stack-ui-backup.json naming the version and the time. Only
regular files, read without following a link; half-written leftovers (*.new, *.tmp) stay out. It is taken under
the one change lock, so it never holds a deploy half way. Restoring is done by hand (README: Backup)."""
from __future__ import annotations

import io
import json
import os
import re
import stat
import tarfile
from datetime import datetime
from pathlib import Path

from . import __version__
from . import core
from . import stacks as st

BACKUP_DIR = os.environ.get('STACK_UI_BACKUP_DIR') or ''        # in the container; '' = only the download
KEEP = max(1, int(os.environ.get('STACK_UI_BACKUP_KEEP') or 10))
PARTS = ('settings.json', 'schedule.json', 'activity.jsonl', 'deployed', 'rollback')
MAX_TOTAL = 256 * 1024 * 1024       # bytes read into one archive at most
NAME = re.compile(r'^stack-ui-backup-\d{8}-\d{6}(?:-\d+)?\.tar\.gz$')
MANIFEST = 'stack-ui-backup.json'


def now() -> datetime:
    return datetime.now()


def files_of(data: Path) -> list[tuple[str, Path]]:
    """(name in the archive, path) of what is backed up: regular files only, no link (of a file or of a folder)
    followed, no half-written leftover (a folder ending in .new, a file starting with a dot)."""
    found = []
    for part in PARTS:
        top = data / part
        if top.is_symlink():
            continue
        if part.endswith(('.json', '.jsonl')):
            if st.plain_file(top):
                found.append((part, top))
            continue
        if not top.is_dir():
            continue
        for root, dirs, names in os.walk(top):      # (os.walk does not go into a linked folder)
            dirs[:] = sorted(d for d in dirs if not d.endswith('.new') and not d.startswith('.')
                             and not (Path(root) / d).is_symlink())
            for fname in sorted(names):
                path = Path(root) / fname
                if fname.startswith('.') or not st.plain_file(path):
                    continue
                found.append((path.relative_to(data).as_posix(), path))
    return found


def build(when: datetime) -> tuple[str, bytes]:
    """The archive (its file name, its bytes) of the data folder as it is now."""
    data = st.DATA
    buf = io.BytesIO()
    names, total = [], 0
    stamp = int(when.timestamp())
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for arcname, path in files_of(data):
            body = st.read_plain(path, limit=MAX_TOTAL - total)
            if body is None:            # gone meanwhile, turned into a link, or over the limit
                if path.exists() and not path.is_symlink() and path.stat().st_size > MAX_TOTAL - total:
                    raise ValueError(f'the data folder is larger than {MAX_TOTAL // (1024 * 1024)} MiB: no backup made')
                continue
            total += len(body)
            info = tarfile.TarInfo(arcname)
            info.size, info.mode = len(body), 0o600
            info.mtime = int(path.stat().st_mtime) if path.exists() else stamp
            tar.addfile(info, io.BytesIO(body))
            names.append(arcname)
        manifest = json.dumps({'stackUi': __version__, 'created': when.isoformat(timespec='seconds'),
                               'files': names}, indent=2).encode('utf-8') + b'\n'
        info = tarfile.TarInfo(MANIFEST)
        info.size, info.mode, info.mtime = len(manifest), 0o600, stamp
        tar.addfile(info, io.BytesIO(manifest))
    return f'stack-ui-backup-{when:%Y%m%d-%H%M%S}.tar.gz', buf.getvalue()


def download() -> tuple[str, bytes]:
    with core.operation('backup', 'for download'):
        return build(now())


def folder() -> Path:
    if not BACKUP_DIR:
        raise ValueError('no backup folder on the server: mount one into the container and set STACK_UI_BACKUP_DIR '
                         'to it (the download works without it)')
    path = Path(BACKUP_DIR)
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f'the backup folder {BACKUP_DIR} does not exist in the container (mount it)')
    return path


def listing() -> list[dict]:
    """The page's own backups in the backup folder, newest first; [] without one."""
    try:
        path = folder()
        entries = list(path.iterdir())
    except (ValueError, OSError):
        return []
    out = []
    for p in entries:
        if not NAME.match(p.name):
            continue
        try:
            info = p.lstat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            out.append({'name': p.name, 'size': info.st_size, 'modified': int(info.st_mtime)})
    return sorted(out, key=lambda b: b['name'], reverse=True)


def save() -> dict:
    """Write a backup into the backup folder (whole or not at all, readable by its owner only), then remove the
    page's own backups beyond the newest KEEP. Nothing else in the folder is touched."""
    target = folder()
    with core.operation('backup', 'to the server'):
        name, body = build(now())
        base, n = name[:-len('.tar.gz')], 1
        while (target / name).exists() or (target / name).is_symlink():
            name, n = f'{base}-{n}.tar.gz', n + 1
        tmp = target / f'.{name}.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0),
                     0o600)
        try:
            with os.fdopen(fd, 'wb') as fh:
                fh.write(body)
            os.chmod(tmp, 0o600)
            os.replace(tmp, target / name)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        removed = []
        for old in listing()[KEEP:]:
            try:
                (target / old['name']).unlink()
                removed.append(old['name'])
            except OSError:
                pass
    return {'file': name, 'size': len(body), 'removed': sorted(removed), 'backups': listing()}


def view() -> dict:
    ready = True
    try:
        folder()
    except ValueError:
        ready = False
    return {'folder': BACKUP_DIR, 'ready': ready, 'keep': KEEP, 'backups': listing()}
