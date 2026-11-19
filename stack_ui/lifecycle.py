"""What happens to a stack as a whole, with everything around it: deploy (with its rollback point, the record of the
deploy and the hashes the schedule trusts), start / stop / delete, and the roll back to the rollback point.

It sits above stacks.py (podman-compose, the files), rollback.py (the rollback point) and schedule.py (the hashes,
the clean-up switch), so none of those has to import another one back."""
from __future__ import annotations

import os
from pathlib import Path

from . import podman_api as pa
from . import rollback
from . import schedule
from . import stacks as st


def deploy(folder: Path, compose_file: str, env_file: str | None, *, approve, dry_run: bool = False,
           pull: bool = True, recreate: bool = False, point: bool = True, trusted: bool = True, before_up=None,
           services: list[str] | None = None, log=print) -> str:
    """Create or update the stack. approve(containers) decides about containers in its way. pull pulls first
    (before anything is removed) and recreates every container; recreate does it without a pull (after a
    secret changed). point: keep what the stack ran with as its rollback point (an update of a stack that was
    deployed from here before; rollback.py). trusted: a person decided this deploy, so its files are the ones the
    schedule may deploy again by itself (schedule.approve_hashes). before_up(hashes) is called after the pull, right
    before anything is changed, with the SHA-256 of the files about to be used (None when one could not be read);
    it may raise StackError. services: only these compose services are pulled and brought up
    (the others are left as they are). Returns 'create' or 'update'."""
    p = st.plan(folder, compose_file, env_file)
    if p['missing_secrets']:
        raise st.StackError('these Podman secrets do not exist yet: ' + ', '.join(p['missing_secrets'])
                         + ' (create them under Secrets first)')
    if p['errors']:
        raise st.StackError('not deployed: ' + '; '.join(p['errors']))
    for warning in p['warnings']:
        log('warning: ' + warning)
    if dry_run:
        log(f'would {p["action"]} the stack "{folder.name}"'
            + (f'; first it asks to remove {", ".join(st.cname(c) for c in p["blocking"])}' if p['blocking'] else ''))
        return p['action']
    if p['blocking'] and not approve(p['blocking']):
        raise st.StackError('not deployed: the containers in its way are still there')
    # a point still pending (the update that wrote it never finished) is the last good state: a retry must not
    # replace it with the half-updated one the containers show now
    unfinished = bool(point and (rollback.load(folder) or {}).get('pending'))
    before = rollback.capture(folder, compose_file, env_file) if point and not unfinished and p['action'] == 'update' else None
    if pull:  # before anything is removed: a failed pull leaves the old containers running
        track = st.PullProgress(st.pull_count(folder, compose_file, services))
        st.set_progress(track.step, '', 0, track.total)
        st.run_compose(folder, compose_file, env_file, ['pull', *(services or [])], log, on_line=track)
    if point and not unfinished:   # after the pull (a failed pull changed nothing, its old point stays)
        rollback.keep(folder, before and {**before, 'pending': True}, log if p['action'] == 'update' else None,
                      cleanup=schedule.cleanup_on())
    # what the containers are brought up with, read once, now: the record and the hashes are made from these
    # bytes, not from what a container may have put into the folder by the time `up` has finished
    snap = st.snapshot(folder, compose_file, env_file)
    if before_up is not None:
        before_up(st.hashes(snap))
    for c in p['blocking']:
        pa.remove_container(str(c['Id']))
        log(f'  removed {st.cname(c)}')
    # `up` downloads (and builds) what is not on the host yet, which may take long: it keeps the long limit then;
    # with everything present a command that does not finish soon is hung (an unhealthy dependency)
    up_limit = None if pull or st.images_ready(folder, compose_file) else st.LONG_TIMEOUT
    st.set_progress('Recreating containers')
    st.run_compose(folder, compose_file, env_file,
                ['up', '-d', *(['--force-recreate'] if pull or recreate else []), *(services or [])], log,
                timeout=up_limit)
    st.save_deployed(folder, compose_file, env_file, log, snap)
    if trusted:
        schedule.approve_hashes(folder.name, st.hashes(snap))
    if point:
        rollback.settle(folder)
    log(f'Stack "{folder.name}" {"created" if p["action"] == "create" else "updated"}.')
    return p['action']


def stack_action(folder: Path, compose_file: str, env_file: str | None, action: str, log=print) -> None:
    """start, stop or delete (podman-compose down: the containers go, the folder and the host data stay)."""
    if action not in ('start', 'stop', 'delete'):
        raise ValueError(f'unknown stack action {action}')
    st.run_compose(folder, compose_file, env_file, ['down' if action == 'delete' else action], log)
    if action == 'delete':
        st.drop_record(folder)
        rollback.discard(folder)


def roll_back(folder: Path, compose_file: str, env_file: str | None, write_file, log, check=None,
              services: list[str] | None = None) -> None:
    """Put the stack back to its rollback point: the image names (the old image ids get their references back),
    the files (write_file(path, text) keeps what it replaces in .history/) and the containers (recreated without a
    pull). The state rolled back from becomes the new point, so the same call leads forward again.
    check(fname, text) is asked about every file that would be written and raises ValueError for one that may not
    be. StackError when there is no point, it cannot be used, a file fails the check or a name cannot be given
    back: no file has been written then, and the names already moved are put back. services: only these compose
    services are recreated (a partly stopped stack stays partly stopped)."""
    point = rollback.load(folder)
    if point is None:
        raise st.StackError('there is no rollback point for this stack')
    blocked = rollback.problems(folder, compose_file, env_file, point)
    if blocked:
        raise st.StackError('cannot roll back: ' + '; '.join(blocked))
    st.set_progress('Restoring the previous images and files')
    changed = {fname: text for fname, text in point['files'].items()
               if st.read_plain(folder / fname) != text.encode('utf-8')}
    for fname in changed:                        # before a name is moved: every target can be written
        if os.path.lexists(folder / fname) and not st.plain_file(folder / fname):
            raise st.StackError(f'cannot roll back: "{fname}" in the stack\'s folder is a link or no regular file '
                                '(remove it on the host first)')
    if check is not None:
        for fname, text in changed.items():      # like a save on the page, before anything is changed
            try:
                check(fname, text)
            except ValueError as exc:
                raise st.StackError(f'cannot roll back: {exc}') from None
    newer = rollback.capture(folder, compose_file, env_file)
    names = {rollback.short_id(i.get('Id')): set(i.get('RepoTags') or []) for i in pa.images()}
    holder = {tag: iid for iid, tags in names.items() for tag in tags}
    moved = []                                  # (repository, tag, the image that held the name)
    try:
        for img in point['images']:
            parts = rollback.split_ref(str(img.get('ref') or ''))
            name = f'{parts[0]}:{parts[1]}' if parts else ''
            if parts is None or name in names.get(img['id'], set()):
                continue
            try:
                pa.tag_image(img['id'], *parts)
            except pa.PodmanError as exc:
                raise st.StackError(f'could not give the name {img["ref"]} back to the image {img["id"][:12]}: {exc}') from None
            moved.append((parts[0], parts[1], holder.get(name), img['id']))
            names.setdefault(img['id'], set()).add(name)
            log(f'  {name} is the image {img["id"][:12]} again')
        for fname, text in changed.items():
            try:
                write_file(folder / fname, text)
            except (OSError, ValueError) as exc:
                raise st.StackError(f'could not write {fname}: {exc}') from None
            log(f'  restored {fname}')
    except st.StackError:
        for repo, tag, previous, given in reversed(moved):  # the newer images get their names back: not left unnamed
            try:
                if previous:
                    pa.tag_image(previous, repo, tag)
                else:                                       # nobody held it: it goes off again (the image stays)
                    pa.untag_image(given, repo, tag)
            except pa.PodmanError:
                pass
        raise
    if newer is None:
        log('the rollback point is kept: the stack has no containers, so there is no newer state to keep instead')
    else:
        rollback.keep(folder, newer, log)
    log('recreating the containers from the previous images')
    deploy(folder, compose_file, env_file, approve=lambda containers: False, pull=False, recreate=True,
              point=False, services=services, log=log)
