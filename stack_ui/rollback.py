"""The rollback point of a stack: what it ran with before its last update from this page (rollback.json and a copy of
that deploy's files), and the roll back that puts it back: the files, the image names and the containers. One point
per stack; the next update replaces it.

The point is kept in the page's own data folder, STACK_UI_DATA/rollback/<stack>/, not in the stack's folder: a
container that mounts its stack's folder could otherwise plant or change one, and the page acts on a point as root
(it writes files, gives image names away, recreates containers). Nothing in a stack's folder is read as a point.
See docs/superpowers/specs/2026-11-12-rollback-design.md."""
from __future__ import annotations

import difflib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import podman_api as pa
from . import stacks as st

DATA = Path(os.environ.get('STACK_UI_DATA') or '/data')     # the page's own folder (server.DATA)
POINT_FILE = 'rollback.json'
MAX_RECORD = 256 * 1024         # bytes of a rollback.json that is still read
LEGACY = '.previous'            # where 1.0.22 kept the point, in the stack's folder: never read, removed on sight
HEX_ID = re.compile(r'^[0-9a-f]{12,64}$')


def short_id(image_id) -> str:
    return str(image_id or '').split(':', 1)[-1]


def point_dir(folder: Path) -> Path:
    """Where the stack's point is kept (a stack's name has no dot or slash: st.STACK_NAME)."""
    return DATA / 'rollback' / folder.name


def small_text(path: Path, limit: int) -> str | None:
    """The text of a regular file (no link, no FIFO) of at most limit bytes; None otherwise."""
    try:
        if not st.plain_file(path) or path.stat().st_size > limit:
            return None
        return path.read_text(encoding='utf-8')
    except (OSError, ValueError):
        return None


def capture(folder: Path, compose_file: str, env_file: str | None) -> dict | None:
    """The state the stack runs with now, as a point in memory: the files of its last deploy (.deployed/) and the
    images its containers were created from. None when there is nothing to go back to: no record of a deploy from
    this page, or no containers. A file larger than a stack file may be is left out (the compose or env file: no
    point)."""
    record = folder / st.DEPLOYED
    if not st.plain_file(record / compose_file):
        return None
    containers = st.project_containers(folder.name)
    if not containers:
        return None
    files = {}
    for fname in (compose_file, env_file, *st.stack_file_names(record, compose_file, env_file)):
        if not fname or not st.plain_file(record / fname):
            continue
        text = small_text(record / fname, st.MAX_FILE)
        if text is not None:
            files[fname] = text
        elif fname in (compose_file, env_file):
            return None
    return {'taken': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'compose': compose_file,
            'env': env_file, 'files': files,
            'images': [{'container': st.cname(c), 'ref': str(c.get('Image') or ''), 'id': short_id(c.get('ImageID'))}
                       for c in containers]}


def record_of(point: dict) -> str:
    meta = {key: point[key] for key in ('taken', 'compose', 'env', 'images')}
    if point.get('pending'):
        meta['pending'] = True
    return json.dumps(meta, indent=2) + chr(10)


def write_point(folder: Path, point: dict) -> None:
    """Write the point whole or not at all: into <stack>.new/, then swapped in."""
    final = point_dir(folder)
    new = final.with_name(final.name + '.new')
    final.parent.mkdir(parents=True, exist_ok=True)
    for path in (new, final):
        if path.is_symlink():            # never followed
            path.unlink()
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir()                          # (fails when something is there again: nothing is written into it)
    (new / 'files').mkdir()
    for fname, text in point['files'].items():
        st.write_lf(new / 'files' / fname, text)
    st.write_lf(new / POINT_FILE, record_of(point))
    shutil.rmtree(final, ignore_errors=True)
    os.replace(new, final)


def valid(point: dict) -> bool:
    """The types of a rollback.json read from disk (a damaged one must not break other code)."""
    images = point.get('images')
    return (isinstance(point.get('compose'), str) and bool(point['compose'])
            and (point.get('env') is None or isinstance(point['env'], str))
            and isinstance(point.get('taken'), str) and isinstance(images, list)
            and all(isinstance(i, dict) and all(isinstance(i.get(k), str) and i.get(k) for k in ('container', 'ref', 'id'))
                    for i in images))


def load_record(folder: Path) -> dict | None:
    """The point's rollback.json alone (no files read), or None when there is none or it is not usable: damaged,
    too large, not a regular file, or without its compose file."""
    base = point_dir(folder)
    if base.is_symlink() or (base / 'files').is_symlink():
        return None
    text = small_text(base / POINT_FILE, MAX_RECORD)
    if text is None:
        return None
    try:
        point = json.loads(text)
    except ValueError:
        return None
    if not isinstance(point, dict) or not valid(point) or not st.plain_file(base / 'files' / point['compose']):
        return None
    return point


def load(folder: Path) -> dict | None:
    """The stack's rollback point with its files, or None when there is none or it is not usable. Only files with
    the names a stack file may have are read, each at most as large as a stack file may be."""
    point = load_record(folder)
    if point is None:
        return None
    wanted = (point['compose'], point.get('env'))
    files = {}
    try:
        for p in sorted((point_dir(folder) / 'files').iterdir()):
            if not st.plain_file(p) or not (p.name in wanted or (st.is_stack_file_name(p.name)
                                                                 and not p.name.startswith('.'))):
                continue
            text = small_text(p, st.MAX_FILE)
            if text is None:
                return None
            files[p.name] = text
    except OSError:
        return None
    point['files'] = files
    return point if point['compose'] in files else None


def exists(folder: Path) -> bool:
    return load_record(folder) is not None


def settle(folder: Path) -> None:
    """The deploy that wrote the point finished: it is no longer pending (see stacks.deploy)."""
    point = load_record(folder)
    if point is None or not point.get('pending'):
        return
    del point['pending']
    try:
        st.write_lf(point_dir(folder) / POINT_FILE, record_of(point))
    except OSError:
        pass


def drop_legacy(folder: Path) -> None:
    """Remove what 1.0.22 (or anything else) left as .previous/ in the stack's folder; a link is removed, not
    followed."""
    for path in (folder / LEGACY, folder / (LEGACY + '.new')):
        try:
            if path.is_symlink():
                path.unlink()
            elif path.exists():
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def discard(folder: Path) -> None:
    final = point_dir(folder)
    for path in (final, final.with_name(final.name + '.new')):
        try:
            if path.is_symlink():
                path.unlink()
            else:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass
    drop_legacy(folder)


def keep(folder: Path, point: dict | None, log=None) -> None:
    """Make the point the stack's one (replacing the old one). Without a point the old one is dropped, since it would
    describe an older state than the one just before this deploy; log says so when given. A point that cannot be
    written (the data folder is missing or read-only) is no reason to stop a deploy: there is none then."""
    drop_legacy(folder)
    if point is not None:
        try:
            write_point(folder, point)
        except OSError as exc:
            discard(folder)
            if log is not None:
                log(f'no rollback point: it could not be written ({exc})')
        return
    discard(folder)
    if log is not None:
        log('no rollback point: this stack was not deployed from this page before')


def problems(folder: Path, compose_file: str, env_file: str | None, point: dict) -> list[str]:
    """What stops this point from being used now (empty: it can be)."""
    out = []
    if point['compose'] != compose_file or point.get('env') != env_file:
        out.append(f'the stack\'s files are named differently now (compose {compose_file}, env {env_file or "none"}) '
                   f'than at the point ({point["compose"]}, {point.get("env") or "none"})')
    try:
        have = {short_id(i.get('Id')) for i in pa.images()}
    except pa.PodmanError as exc:
        return out + [f'Podman does not answer: {exc}']
    for img in point['images']:
        if img.get('id') not in have:
            out.append(f'the image of {img.get("container")} ({img.get("ref")}) is no longer on this host')
    return out


def diff(folder: Path, compose_file: str, env_file: str | None, point: dict) -> dict:
    """The folder's files against the point's (now -> rollback), in the shape of stacks.deployed_diff."""
    def read(path: Path) -> list[str]:
        return path.read_text(encoding='utf-8', errors='replace').splitlines() if st.plain_file(path) else []

    def udiff(fname: str | None) -> str:
        if not fname:
            return ''
        then = point['files'][fname].splitlines() if fname in point['files'] else []
        return '\n'.join(difflib.unified_diff(read(folder / fname), then, f'now: {fname}', f'rollback: {fname}',
                                              lineterm=''))
    others = (set(st.stack_file_names(folder, compose_file, env_file)) | set(point['files'])) - {compose_file, env_file}
    files = {n: udiff(n) for n in sorted(others)}
    return {'compose': udiff(compose_file), 'env': udiff(env_file), 'files': {n: d for n, d in files.items() if d}}


def view(folder: Path, compose_file: str, env_file: str | None) -> dict:
    """What the rollback dialog shows: {available: False} or the point's images against the running ones, the file
    differences and the problems."""
    point = load(folder)
    if point is None:
        return {'available': False}
    running = {st.cname(c): short_id(c.get('ImageID')) for c in st.project_containers(folder.name)}
    try:
        have = {short_id(i.get('Id')) for i in pa.images()}
    except pa.PodmanError:
        have = set()
    images = [{'container': i.get('container'), 'ref': i.get('ref'), 'previous': i.get('id'),
               'current': running.get(i.get('container')), 'present': i.get('id') in have} for i in point['images']]
    return {'available': True, 'taken': point['taken'], 'images': images,
            'diff': diff(folder, compose_file, env_file, point),
            'problems': problems(folder, compose_file, env_file, point)}


def kept_images() -> dict[str, list[str]]:
    """{image id: the stacks whose rollback point needs it}."""
    kept: dict[str, list[str]] = {}
    for folder, _, _ in st.discover():
        point = load_record(folder)         # (the record alone: this runs for every listing of the images)
        for img in (point or {}).get('images', []):
            if img.get('id') and folder.name not in kept.setdefault(img['id'], []):
                kept[img['id']].append(folder.name)
    return kept


REF_OK = re.compile(r'^[a-z0-9][a-z0-9._/:-]{0,254}$')


def split_ref(ref: str) -> tuple[str, str] | None:
    """(repository, tag) of an image reference a container was created from; None when no name can be given back:
    a digest reference, an image id, or something that is no reference."""
    if not ref or '@' in ref or HEX_ID.match(ref) or not REF_OK.match(ref):
        return None
    last = ref.rsplit('/', 1)[-1]
    if ':' in last:
        repo, _, tag = ref.rpartition(':')
        return (repo, tag) if repo and tag else None
    return ref, 'latest'


def roll_back(folder: Path, compose_file: str, env_file: str | None, write_file, log) -> None:
    """Put the stack back to its rollback point: the files (write_file(path, text) keeps what it replaces in
    .history/), the image names (the old image ids get their references back) and the containers (recreated
    without a pull). The state rolled back from becomes the new point, so the same call leads forward again.
    StackError when there is no point or it cannot be used; nothing has been changed then."""
    point = load(folder)
    if point is None:
        raise st.StackError('there is no rollback point for this stack')
    blocked = problems(folder, compose_file, env_file, point)
    if blocked:
        raise st.StackError('cannot roll back: ' + '; '.join(blocked))
    newer = capture(folder, compose_file, env_file)
    for fname, text in point['files'].items():
        path = folder / fname
        if not path.is_file() or path.read_text(encoding='utf-8') != text:
            write_file(path, text)
            log(f'  restored {fname}')
    names = {short_id(i.get('Id')): set(i.get('RepoTags') or []) for i in pa.images()}
    for img in point['images']:
        parts = split_ref(str(img.get('ref') or ''))
        if parts is None or f'{parts[0]}:{parts[1]}' in names.get(img['id'], set()):
            continue
        try:
            pa.tag_image(img['id'], *parts)
        except pa.PodmanError as exc:
            raise st.StackError(f'could not give the name {img["ref"]} back to the image {img["id"][:12]}: {exc}') from None
        names.setdefault(img['id'], set()).add(f'{parts[0]}:{parts[1]}')
        log(f'  {parts[0]}:{parts[1]} is the image {img["id"][:12]} again')
    keep(folder, newer, log)
    log('recreating the containers from the previous images')
    st.deploy(folder, compose_file, env_file, approve=lambda containers: False, pull=False, recreate=True,
              point=False, log=log)
