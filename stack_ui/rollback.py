"""The rollback point of a stack: what it ran with before its last update from this page, kept in <stack>/.previous/
(rollback.json and a copy of that deploy's files), and the roll back that puts it back: the files, the image names
and the containers. One point per stack; the next update replaces it. See
docs/superpowers/specs/2026-11-12-rollback-design.md."""
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

POINT_FILE = 'rollback.json'
HEX_ID = re.compile(r'^[0-9a-f]{12,64}$')


def short_id(image_id) -> str:
    return str(image_id or '').split(':', 1)[-1]


def capture(folder: Path, compose_file: str, env_file: str | None) -> dict | None:
    """The state the stack runs with now, as a point in memory: the files of its last deploy (.deployed/) and the
    images its containers were created from. None when there is nothing to go back to: no record of a deploy from
    this page, or no containers."""
    record = folder / st.DEPLOYED
    if not st.plain_file(record / compose_file):
        return None
    containers = st.project_containers(folder.name)
    if not containers:
        return None
    files = {}
    for fname in (compose_file, env_file, *st.stack_file_names(record, compose_file, env_file)):
        if fname and st.plain_file(record / fname):
            files[fname] = (record / fname).read_text(encoding='utf-8')
    return {'taken': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'compose': compose_file,
            'env': env_file, 'files': files,
            'images': [{'container': st.cname(c), 'ref': str(c.get('Image') or ''), 'id': short_id(c.get('ImageID'))}
                       for c in containers]}


def write_point(folder: Path, point: dict) -> None:
    """Write the point whole or not at all: into .previous.new/, then swapped in."""
    new, final = folder / (st.PREVIOUS + '.new'), folder / st.PREVIOUS
    for path in (new, final):
        if path.is_symlink():            # planted by a container that mounts the folder: removed, never followed
            path.unlink()
    shutil.rmtree(new, ignore_errors=True)
    (new / 'files').mkdir(parents=True)
    for fname, text in point['files'].items():
        st.write_lf(new / 'files' / fname, text)
    meta = {key: point[key] for key in ('taken', 'compose', 'env', 'images')}
    if point.get('pending'):
        meta['pending'] = True
    st.write_lf(new / POINT_FILE, json.dumps(meta, indent=2) + '\n')
    st.match_owner(new, new / 'files', new / POINT_FILE, *(new / 'files' / f for f in point['files']))
    shutil.rmtree(final, ignore_errors=True)
    os.replace(new, final)


def load(folder: Path) -> dict | None:
    """The stack's rollback point, or None when there is none or it is damaged. Only files with the names a stack
    file may have are read (a hand-edited .previous/ cannot name a path or a script)."""
    base = folder / st.PREVIOUS
    if base.is_symlink() or (base / 'files').is_symlink():
        return None
    try:
        point = json.loads((base / POINT_FILE).read_text(encoding='utf-8'))
        if not isinstance(point, dict) or not valid(point):
            return None
        wanted = (point['compose'], point.get('env'))
        point['files'] = {p.name: p.read_text(encoding='utf-8') for p in sorted((base / 'files').iterdir())
                          if st.plain_file(p) and (p.name in wanted or (st.is_stack_file_name(p.name)
                                                                         and not p.name.startswith('.')))}
    except (OSError, ValueError):
        return None
    return point if point['compose'] in point['files'] else None


def valid(point: dict) -> bool:
    """The types of a rollback.json read from disk (a hand-edited or damaged one must not break other code)."""
    images = point.get('images')
    return (isinstance(point.get('compose'), str) and bool(point['compose'])
            and (point.get('env') is None or isinstance(point['env'], str))
            and isinstance(point.get('taken'), str) and isinstance(images, list)
            and all(isinstance(i, dict) and all(isinstance(i.get(k), str) and i.get(k) for k in ('container', 'ref', 'id'))
                    for i in images))


def exists(folder: Path) -> bool:
    return not (folder / st.PREVIOUS).is_symlink() and st.plain_file(folder / st.PREVIOUS / POINT_FILE)


def settle(folder: Path) -> None:
    """The deploy that wrote the point finished: it is no longer pending (see stacks.deploy)."""
    point = load(folder)
    if point is None or not point.get('pending'):
        return
    meta = {key: point[key] for key in ('taken', 'compose', 'env', 'images')}
    path = folder / st.PREVIOUS / POINT_FILE
    st.write_lf(path, json.dumps(meta, indent=2) + chr(10))
    st.match_owner(path)


def discard(folder: Path) -> None:
    for path in (folder / st.PREVIOUS, folder / (st.PREVIOUS + '.new')):
        if path.is_symlink():
            path.unlink()
        else:
            shutil.rmtree(path, ignore_errors=True)


def keep(folder: Path, point: dict | None, log=None) -> None:
    """Make the point the stack's one (replacing the old one). Without a point the old one is dropped, since it would
    describe an older state than the one just before this deploy; log says so when given."""
    if point is not None:
        write_point(folder, point)
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
        point = load(folder)
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
    keep(folder, newer)
    log('recreating the containers from the previous images')
    st.deploy(folder, compose_file, env_file, approve=lambda containers: False, pull=False, recreate=True,
              point=False, log=log)
