# Deploy record in the data folder, rollback fixes, automatic-update comfort, operations: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nothing in a stack's folder is trusted as the record of its last deploy any more; the five rollback defects are fixed; the automatic update gets a filter, a bulk switch, image clean-up and partial-stack handling; cancelled commands keep their output and the page's container reaps zombies.

**Architecture:** `stacks.py` gets `DATA` and `record_dir()`; `save_deployed` / `deployed_diff` / `rollback.capture` use it. `server.adopt_records()` copies matching legacy records once at start. Rollback fixes stay in `rollback.py` (`roll_back` gains a `check` callback). Clean-up lives in `rollback.keep()` (`release()`), switched by `schedule` state. `auto_update` passes service names to `st.deploy(services=...)`.

**Tech Stack:** Python 3.9+ standard library, PyYAML, `unittest`, mock libpod, `tests/fake_compose.py`.

**Spec:** `docs/superpowers/specs/2026-11-13-record-rollback-comfort-design.md`

## Global Constraints

- Standard library and PyYAML only; importable on Python 3.9.
- Tests never touch the real Pi, Podman or a registry.
- No commit, push, tag or version bump: only on the user's word. Parts A, B, C are separate commits when asked.
- Nothing from `stacks/` is touched (the owner's own `stack-ui` compose file included).
- Every bug fix and feature: failing test first, watched failing.
- Suite: `python -m unittest discover -s tests > <file> 2>&1`, read `Ran` / `OK` from the file; Linux: PowerShell `wsl bash <scratchpad>/linux_suite.sh`.

## Review Focus

- A `.deployed/` planted or edited in a stack's folder after this change: drift, Diff, *Changed* marks and the next rollback point must not see it (Task 1).
- Adoption must not follow a link and must not adopt a record whose hashes do not match (Task 2).
- Clean-up must never remove an image that has a name, that a container uses, or that another stack's point keeps (Task 5).
- A roll back stopped by a file check or a failed tag must leave names and files as they were (Task 3).
- A partial update must not start a stopped service that no running service depends on (Task 6; podman-compose starts dependencies, the spec says so).

---

## Part A

### Task 1: the record lives in `STACK_UI_DATA/deployed/<stack>/`

**Files:** Modify `stack_ui/stacks.py`, `stack_ui/rollback.py`, `stack_ui/server.py`, `tests/support.py`, and the tests that build a record by path (`tests/test_rollback.py`, `tests/test_stacks.py`, `tests/test_stack_delete.py`, `tests/test_auto_update.py`). Test: `tests/test_record.py` (new).

**Interfaces — Produces:** `st.DATA`, `st.record_dir(folder) -> Path`, `st.drop_record(folder)` (data record and the legacy `.deployed/`), `st.drop_legacy_record(folder)`, `st.save_deployed(folder, compose_file, env_file, log=None)` (never raises OSError), `st.deployed_diff(...)` unchanged signature. `st.DEPLOYED` keeps the value `'.deployed'` and now names the leftover only.

- [ ] **Step 1: `tests/support.py`**: in `Base.setUp`, next to the rollback/schedule patches: `self.patch(st, 'DATA', self.tmp / 'data')`.

- [ ] **Step 2: failing tests** (`tests/test_record.py`, class `RecordTest(RollbackBase)`; `RollbackBase.setUp` already calls `st.save_deployed`):

```python
    def rec(self):
        return st.record_dir(self.folder)

    def test_the_record_is_written_into_the_data_folder(self):
        self.assertEqual(self.rec(), self.tmp / 'data' / 'deployed' / 'web')
        self.assertEqual((self.rec() / 'compose.yaml').read_text(encoding='utf-8'), V1)
        self.assertFalse((self.folder / '.deployed').exists())

    def test_a_record_planted_in_the_stack_folder_changes_nothing(self):
        (self.folder / 'compose.yaml').write_bytes(V2.encode('utf-8'))
        planted = self.folder / '.deployed'
        planted.mkdir()
        (planted / 'compose.yaml').write_bytes(V2.encode('utf-8'))
        (planted / '.env').write_bytes((self.folder / '.env').read_bytes())
        self.assertEqual(self.ui.drift()['web']['state'], 'differs')
        self.assertEqual(rollback.capture(self.folder, 'compose.yaml', '.env')['files']['compose.yaml'], V1)

    def test_no_record_is_unknown_even_with_one_in_the_stack_folder(self):
        shutil.rmtree(self.rec())
        planted = self.folder / '.deployed'
        planted.mkdir()
        for name in ('compose.yaml', '.env'):
            (planted / name).write_bytes((self.folder / name).read_bytes())
        self.assertEqual(self.ui.drift()['web']['state'], 'unknown')
        self.assertIsNone(rollback.capture(self.folder, 'compose.yaml', '.env'))

    def test_the_next_deploy_removes_a_leftover_and_does_not_follow_a_link(self):
        elsewhere = self.tmp / 'elsewhere'
        elsewhere.mkdir()
        (elsewhere / 'keep.txt').write_text('x', encoding='utf-8')
        try:
            os.symlink(elsewhere, self.folder / '.deployed', target_is_directory=True)
        except (OSError, NotImplementedError):
            (self.folder / '.deployed').mkdir()
        self.deploy()
        self.assertFalse(os.path.lexists(self.folder / '.deployed'))
        self.assertTrue((elsewhere / 'keep.txt').is_file())

    def test_a_deploy_goes_on_when_the_record_cannot_be_written(self):
        blocker = self.tmp / 'blocker'
        blocker.write_text('x', encoding='utf-8')
        self.patch(st, 'DATA', blocker)
        said = []
        st.deploy(self.folder, 'compose.yaml', '.env', approve=lambda c: True, pull=False, point=False, log=said.append)
        self.assertTrue(any('record' in line and 'could not be written' in line for line in said), said)

    def test_deleting_the_folder_and_uploading_drop_the_record(self):
        self.mock.containers.clear()
        self.ui.stack_action('web', 'delete', {'confirm': 'web', 'deleteFolder': True})
        self.assertFalse(self.rec().exists())
        left = self.tmp / 'data' / 'deployed' / 'up'
        left.mkdir(parents=True)
        (left / 'compose.yaml').write_text('old', encoding='utf-8')
        self.ui.upload({'name': 'up', 'files': {'compose.yaml': compose_yaml('up')}})
        self.assertFalse(left.exists())

    def test_the_file_list_marks_changes_against_the_record_in_the_data_folder(self):
        (self.folder / 'notes.txt').write_text('a', encoding='utf-8')
        self.deploy()
        (self.folder / 'notes.txt').write_text('b', encoding='utf-8')
        row = next(f for f in self.ui.files('web')['stackFiles'] if f['name'] == 'notes.txt')
        self.assertTrue(row['changed'])
```

Run: `python -m unittest tests.test_record` — Expected: AttributeError `record_dir`.

- [ ] **Step 3: implement.** `stacks.py`:

```python
DATA = Path(os.environ.get('STACK_UI_DATA') or '/data')     # the page's own folder (server.DATA)
DEPLOYED = '.deployed'          # where the record was kept until 1.0.24, in the stack's folder: never read, removed on sight


def record_dir(folder: Path) -> Path:
    """Where the files of the stack's last deploy are kept: in the page's data folder, since a container that
    mounts its stack's folder can write everything in it."""
    return DATA / 'deployed' / folder.name


def _remove(path: Path) -> None:
    try:
        if path.is_symlink():
            path.unlink()               # never followed
        elif path.exists():
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def drop_legacy_record(folder: Path) -> None:
    _remove(folder / DEPLOYED)


def drop_record(folder: Path) -> None:
    final = record_dir(folder)
    _remove(final)
    _remove(final.with_name(final.name + '.new'))
    drop_legacy_record(folder)


def save_deployed(folder: Path, compose_file: str, env_file: str | None, log=None) -> None:
    """Keep the files this deploy used (whole or not at all). A record that cannot be written is no reason to fail
    a deploy that has run: there is none then, and log says so."""
    final = record_dir(folder)
    new = final.with_name(final.name + '.new')
    try:
        final.parent.mkdir(parents=True, exist_ok=True)
        _remove(new)
        new.mkdir()
        for fname in (compose_file, env_file, *stack_file_names(folder, compose_file, env_file)):
            if fname and plain_file(folder / fname):
                shutil.copyfile(folder / fname, new / fname)
        _remove(final)
        os.replace(new, final)
    except OSError as exc:
        _remove(new)
        _remove(final)
        if log is not None:
            log(f'warning: the record of this deploy could not be written ({exc}): the stack shows Unknown')
    drop_legacy_record(folder)
```

`deployed_diff`: `record = record_dir(folder)`; `is_file()` -> `plain_file(...)` for the two existence checks and in `read()`. `deploy()`: `save_deployed(folder, compose_file, env_file, log)`. `stack_action('delete')`: replace `shutil.rmtree(folder / DEPLOYED, ...)` by `drop_record(folder)`. `rollback.capture`: `record = st.record_dir(folder)`. `server.py`: after each `rollback.discard(folder)` / `rollback.discard(module)` in `stack_action` and `upload` add `st.drop_record(folder)` / `st.drop_record(module)`; in `auto_update` drop the second `schedule.approved(folder / st.DEPLOYED, ...)` condition. Update the module docstring line of `stacks.py` and the comment in `auto_update`.

- [ ] **Step 4: existing tests that build or read the record by path**: replace `self.folder / st.DEPLOYED` / `folder / '.deployed'` by `st.record_dir(self.folder)` wherever the test means "the record"; keep the literal where it means "something planted in the folder" (then the expected outcome follows this task: ignored). `test_auto_update.ReviewFixTest.test_files_and_a_record_written_from_inside_the_stack_folder_are_not_deployed` keeps planting in the folder.

- [ ] **Step 5:** Run `python -m unittest tests.test_record tests.test_rollback tests.test_rollback_server tests.test_stacks tests.test_stack_delete tests.test_auto_update tests.test_stack_files tests.test_server` — Expected: OK.

### Task 2: taking over the records that match

**Files:** Modify `stack_ui/server.py` (`adopt_records`, call in `main` before the server starts). Test: `tests/test_record.py` (class `AdoptTest(RollbackBase)`).

- [ ] **Step 1: failing tests**

```python
    def legacy(self, text=V1):
        shutil.rmtree(st.record_dir(self.folder))
        old = self.folder / '.deployed'
        old.mkdir()
        (old / 'compose.yaml').write_bytes(text.encode('utf-8'))
        (old / '.env').write_bytes((self.folder / '.env').read_bytes())
        return old

    def test_a_record_matching_the_stored_hashes_is_taken_over(self):
        schedule.approve(self.folder, 'compose.yaml', '.env')
        self.legacy()
        self.assertEqual(self.ui.adopt_records(), ['web'])
        self.assertEqual(self.ui.drift()['web']['state'], 'same')
        self.assertEqual(self.ui.adopt_records(), [])                        # already there

    def test_a_record_that_does_not_match_or_has_no_hashes_is_not(self):
        self.legacy()
        self.assertEqual(self.ui.adopt_records(), [])                        # nothing stored for it
        schedule.approve(self.folder, 'compose.yaml', '.env')
        (self.folder / '.deployed' / 'compose.yaml').write_bytes(V2.encode('utf-8'))
        self.assertEqual(self.ui.adopt_records(), [])
        self.assertEqual(self.ui.drift()['web']['state'], 'unknown')

    def test_a_linked_record_is_not_followed(self):
        schedule.approve(self.folder, 'compose.yaml', '.env')
        old = self.legacy()
        moved = self.tmp / 'moved'
        old.rename(moved)
        try:
            os.symlink(moved, old, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('no symlinks here')
        self.assertEqual(self.ui.adopt_records(), [])
```

- [ ] **Step 2: implement**

```python
def adopt_records() -> list[str]:
    """Once, after the upgrade that moved the record of the last deploy into the data folder: a stack whose old
    record (.deployed/ in its folder) holds exactly the files a deploy from the page stored the hashes of gets it
    copied there. Any other old record is not trusted: that stack shows Unknown until its next Update."""
    adopted = []
    for folder, compose_file, env_file in st.discover():
        old = folder / st.DEPLOYED
        if st.record_dir(folder).exists() or old.is_symlink() or not old.is_dir():
            continue
        if not schedule.approved(old, compose_file, env_file, folder.name):
            continue
        final = st.record_dir(folder)
        try:
            final.parent.mkdir(parents=True, exist_ok=True)
            final.mkdir()
            for fname in (compose_file, env_file, *st.stack_file_names(old, compose_file, env_file)):
                if fname and st.plain_file(old / fname):
                    shutil.copyfile(old / fname, final / fname)
        except OSError:
            shutil.rmtree(final, ignore_errors=True)
            continue
        adopted.append(folder.name)
    return adopted
```

`main()`: before `server = Server(...)`: `adopted = adopt_records()` inside `try/except Exception` (a start must not fail on it), printing `Note: took over the deploy record of: ...` to stderr when not empty.

- [ ] **Step 3:** Run `python -m unittest tests.test_record tests.test_shutdown` — Expected: OK.

### Task 3: the five rollback fixes

**Files:** Modify `stack_ui/rollback.py`, `stack_ui/server.py` (`run_rollback`, `auto_update` pass `check`). Test: `tests/test_rollback.py` (class `MinorFixTest(RollbackBase)`), `tests/test_rollback_server.py`.

**Interfaces — Produces:** `rollback.roll_back(folder, compose_file, env_file, write_file, log, check=None)`; `check(fname, text)` raises `ValueError`. `server.rollback_check(compose_file, env_file) -> callable`.

- [ ] **Step 1: failing tests**

```python
    def test_a_roll_back_without_containers_keeps_its_point(self):
        self.update_to_v2()
        taken = rollback.load_record(self.folder)['taken']
        self.mock.containers.clear()
        said = []
        rollback.roll_back(self.folder, 'compose.yaml', '.env', self.ui.write_file, said.append)
        self.assertEqual(rollback.load_record(self.folder)['taken'], taken)
        self.assertTrue(any('kept' in line for line in said), said)

    def test_names_already_moved_are_put_back_when_a_tag_fails(self):
        # two containers, two images under two names; the second tag fails
        self.mock.images.append({'Id': 'oldb', 'RepoTags': ['docker.io/library/redis:7'], 'RepoDigests': [], 'Size': 5, 'Created': 1})
        self.mock.add_container('cache', project='web', image='docker.io/library/redis:7', image_id='oldb')
        self.update_to_v2()
        self.mock.images[1]['RepoTags'] = None                               # oldb lost its name to a newer image
        self.mock.images.append({'Id': 'newb', 'RepoTags': ['docker.io/library/redis:7'], 'RepoDigests': [], 'Size': 6, 'Created': 2})
        real, calls = pa.tag_image, []

        def flaky(iid, repo, tag):
            calls.append(iid)
            if len(calls) == 2:
                raise pa.PodmanError('tag failed')
            return real(iid, repo, tag)
        self.patch(pa, 'tag_image', flaky)
        before = self.tags()
        with self.assertRaisesRegex(st.StackError, 'could not give the name'):
            rollback.roll_back(self.folder, 'compose.yaml', '.env', self.ui.write_file, lambda _: None)
        self.assertEqual(self.tags(), before)
        self.assertEqual((self.folder / 'compose.yaml').read_text(encoding='utf-8'), V2)     # no file was written

    def test_the_diff_does_not_list_files_a_roll_back_keeps(self):
        self.update_to_v2()
        (self.folder / 'later.txt').write_text('added after the point', encoding='utf-8')
        point = rollback.load(self.folder)
        self.assertNotIn('later.txt', rollback.diff(self.folder, 'compose.yaml', '.env', point)['files'])

    def test_a_file_that_fails_the_check_stops_the_roll_back_before_anything_changes(self):
        self.update_to_v2()
        before = self.tags()

        def check(fname, text):
            if fname == 'compose.yaml':
                raise ValueError('compose.yaml: not allowed')
        with self.assertRaisesRegex(st.StackError, 'compose.yaml: not allowed'):
            rollback.roll_back(self.folder, 'compose.yaml', '.env', self.ui.write_file, lambda _: None, check=check)
        self.assertEqual(self.tags(), before)
        self.assertEqual((self.folder / 'compose.yaml').read_text(encoding='utf-8'), V2)

    def test_a_digest_like_reference_is_no_name(self):
        self.assertIsNone(rollback.split_ref('sha256:' + 'a' * 64))
        self.assertIsNone(rollback.split_ref('sha256:abc'))
```

Server test (`ServerRollbackTest`): a point whose compose text fails `check_compose` (write the point's `files/compose.yaml` as `'not: [valid'` after `update_to_v2`) -> `POST /api/stacks/web/rollback` answers `ok: False` with the file named, compose file unchanged.

(If `update_to_v2` with two containers does not fit the mock, build the second image/container directly in the test and write the point with `rollback.write_point`; the behaviour under test is "the first name moved is put back".)

- [ ] **Step 2: implement in `rollback.py`**

`split_ref`: first line `if not ref or '@' in ref or ref.startswith('sha256:') or HEX_ID.match(ref) or not REF_OK.match(ref): return None`.

`diff`: `others = set(point['files']) - {compose_file, env_file}`.

`roll_back` (replace the body after `blocked`):

```python
    changed = {fname: text for fname, text in point['files'].items()
               if not st.plain_file(folder / fname) or (folder / fname).read_text(encoding='utf-8') != text}
    if check is not None:
        for fname, text in changed.items():      # like a save on the page, before anything is changed
            try:
                check(fname, text)
            except ValueError as exc:
                raise st.StackError(f'cannot roll back: {exc}') from None
    newer = capture(folder, compose_file, env_file)
    names = {short_id(i.get('Id')): set(i.get('RepoTags') or []) for i in pa.images()}
    holder = {tag: iid for iid, tags in names.items() for tag in tags}
    moved = []                                  # (repo, tag, the image that held the name)
    try:
        for img in point['images']:
            parts = split_ref(str(img.get('ref') or ''))
            if parts is None or f'{parts[0]}:{parts[1]}' in names.get(img['id'], set()):
                continue
            name = f'{parts[0]}:{parts[1]}'
            try:
                pa.tag_image(img['id'], *parts)
            except pa.PodmanError as exc:
                raise st.StackError(f'could not give the name {img["ref"]} back to the image {img["id"][:12]}: {exc}') from None
            moved.append((parts[0], parts[1], holder.get(name)))
            names.setdefault(img['id'], set()).add(name)
            log(f'  {name} is the image {img["id"][:12]} again')
        for fname, text in changed.items():
            write_file(folder / fname, text)
            log(f'  restored {fname}')
    except (st.StackError, OSError):
        for repo, tag, previous in reversed(moved):     # the newer images get their names back: not left unnamed
            if previous:
                try:
                    pa.tag_image(previous, repo, tag)
                except pa.PodmanError:
                    pass
        raise
    if newer is None:
        log('the rollback point is kept: the stack has no containers, so there is no newer state to keep instead')
    else:
        keep(folder, newer, log)
```

then the existing `log('recreating ...')` and `st.deploy(...)`. An `OSError` from `write_file` must reach the callers as an error they handle: wrap as `raise st.StackError(f'could not write {fname}: {exc}')` inside the file loop instead of letting OSError out, and catch only `st.StackError` in the undo.

`server.py`:

```python
def rollback_check(compose_file: str, env_file: str | None):
    """The checks of a save on the page, for a file a roll back would write."""
    def check(fname: str, text: str) -> None:
        if fname == compose_file:
            check_compose(text)
        elif fname == env_file:
            check_env(text)
        else:
            check_stack_file(fname, text)
    return check
```

and pass `check=rollback_check(compose_file, env_file)` in `run_rollback` and in `auto_update`'s `rollback.roll_back(...)`. (If `check_compose` / `check_env` do not name the file in their message, prefix it in `check`: `raise ValueError(f'{fname}: {exc}')`.)

- [ ] **Step 3:** Run `python -m unittest tests.test_rollback tests.test_rollback_server tests.test_auto_update` — Expected: OK.

- [ ] **Step 4 (Part A done):** README (the Stacks bullet / `STACK_UI_DATA` row: the record of the last deploy is kept there too), CHANGELOG `## Unreleased` entry for Part A, whole suite Windows + WSL.

## Part B

### Task 4: filter and bulk switch

**Files:** Modify `stack_ui/server.py` (`stacks_auto`, route, bearer refusal), `stack_ui/page.html`. Test: `tests/test_schedule_server.py`, `tests/test_page.py`.

- [ ] **Step 1: failing tests**

```python
    def test_the_switch_of_several_stacks_at_once(self):
        make_stack(self.stacks, 'db', compose_yaml('db'))
        self.patch(self.ui, 'PROTECTED', {'db'})
        code, r = self.call('POST', '/api/stacks/auto', {'names': ['web', 'db', 'nosuch'], 'auto': True})
        self.assertEqual((code, r['changed']), (200, ['web']))
        self.assertEqual([s['name'] for s in r['skipped']], ['db', 'nosuch'])
        self.assertTrue(schedule.is_auto('web'))
        code, r = self.call('POST', '/api/stacks/auto', {'names': ['web', 'db'], 'auto': False})
        self.assertEqual((r['changed'], r['skipped']), (['web', 'db'], []))
        self.assertEqual(self.call('POST', '/api/stacks/auto', {'names': 'web', 'auto': True})[0], 400)
        self.assertEqual(self.call('POST', '/api/stacks/auto', {'names': ['web'], 'auto': 1})[0], 400)
```

and add `('POST', '/api/stacks/auto')` to the bearer-refusal test's list. Page test: `'sel-auto-on'`, `'sel-auto-off'`, `"['auto', 'Automatic update'"`, `'/api/stacks/auto'` are in the page.

- [ ] **Step 2: implement.** `server.py`:

```python
def stacks_auto(data: dict) -> dict:
    names, on = data.get('names'), data.get('auto')
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names) or len(names) > 200:
        raise ValueError('names: a list of stack names')
    if not isinstance(on, bool):
        raise ValueError('auto must be true or false')
    known = {folder.name for folder, _, _ in st.discover()}
    changed, skipped = [], []
    for name in dict.fromkeys(names):
        if name not in known:
            skipped.append({'name': name, 'reason': 'no such stack'})
        elif on and stack_protected(name):
            skipped.append({'name': name, 'reason': 'protected'})
        else:
            schedule.set_auto(name, on)
            changed.append(name)
    return {'changed': changed, 'skipped': skipped}
```

Route in `Handler.api` before the `POST /api/stacks` upload line: `if method == 'POST' and parts == ['api', 'stacks', 'auto']: return self.json(200, stacks_auto(data))`. Bearer refusal: also `where == ['api', 'stacks', 'auto']`. (`auto` is then no usable stack name for the 3-part route; a stack named `auto` keeps its 4-part routes.)

`page.html`: in `FILTERS` after the `updates` entry: `['auto', 'Automatic update', (s) => !!s.auto],`. Selection bar: after the Clear button `<button data-act="sel-auto-on">Auto-update on</button><button data-act="sel-auto-off">Auto-update off</button>`. In the click dispatcher next to `sel-update`: `if (a === 'sel-auto-on' || a === 'sel-auto-off') return autoSelected([...SELECTED], a === 'sel-auto-on');` and

```js
async function autoSelected(names, on) {
  try {
    const r = await api('/api/stacks/auto', { method: 'POST', body: JSON.stringify({ names, auto: on }) });
    if (r.changed.length) toast(`Automatic update ${on ? 'on' : 'off'}: ${r.changed.join(', ')}`);
    if (r.skipped.length) toast(`Not changed: ${r.skipped.map((s) => `${s.name} (${s.reason})`).join(', ')}`, 'bad');
  } catch (e) { toast(e.message, 'bad'); }
  load();
}
```

- [ ] **Step 3:** Run `python -m unittest tests.test_schedule_server tests.test_page`; `node --check` on the page script — Expected: OK.

### Task 5: image clean-up

**Files:** Modify `stack_ui/rollback.py` (`release`, `keep`), `stack_ui/schedule.py` (`cleanup` in the state, `configure`, `view`, `cleanup_on()`), `stack_ui/stacks.py` (`deploy` passes it), `stack_ui/server.py` (`schedule_save`), `stack_ui/page.html` (Settings switch). Test: `tests/test_rollback.py` (class `CleanupTest`), `tests/test_schedule.py`, `tests/test_schedule_server.py`, `tests/test_page.py`.

**Interfaces — Produces:** `rollback.release(ids: set[str], log) -> None`; `rollback.keep(folder, point, log=None, cleanup=False)`; `schedule.cleanup_on() -> bool`; `schedule.configure(enabled, at_time, now, cleanup=None)`; view key `cleanup`; `POST /api/schedule` accepts optional `cleanup: bool`.

- [ ] **Step 1: failing tests** (`CleanupTest(RollbackBase)`; `update_to_v2` leaves `old1` unnamed and kept by the point, `new1` running)

```python
    def second_update(self, **kw):
        """A further update: new1 loses its name to new2, the point moves on to new1."""
        (self.folder / 'compose.yaml').write_bytes((V2 + '# v3' + chr(10)).encode('utf-8'))
        self.deploy(**kw)
        return [i['Id'] for i in self.mock.images]

    def test_the_images_of_the_replaced_point_are_removed(self):
        self.update_to_v2()
        said = []
        st.deploy(self.folder, 'compose.yaml', '.env', approve=lambda c: True, pull=False, log=said.append)
        self.assertNotIn('old1', [i['Id'] for i in self.mock.images])
        self.assertTrue(any('removed the old image old1' in line for line in said), said)

    def test_not_when_switched_off(self):
        self.update_to_v2()
        schedule.configure(True, '04:00', datetime(2026, 11, 13, 3, 0), cleanup=False)
        self.assertIn('old1', self.second_update())

    def test_an_image_with_a_name_in_use_or_kept_elsewhere_stays(self):
        self.update_to_v2()
        self.mock.images[0]['RepoTags'] = ['localhost/keep:1']               # it has a name again
        self.assertIn('old1', self.second_update())
        # (the same for "a container uses it" and "another stack's point keeps it": two more tests built the same way)

    def test_a_removal_that_fails_is_logged_and_the_deploy_goes_on(self):
        self.update_to_v2()

        def refuse(iid, force=False):
            raise pa.PodmanError('image is in use')
        self.patch(pa, 'remove_image', refuse)
        said = []
        st.deploy(self.folder, 'compose.yaml', '.env', approve=lambda c: True, pull=False, log=said.append)
        self.assertTrue(any('was not removed' in line for line in said), said)
```

(The second deploy must be an update with a changed file only if `deploy` needs one; `capture` works from the record and the running containers, so a plain second `deploy()` writes a new point over the old one. If the mock's `DELETE /images/<id>` does not remove an unnamed image by id, extend `tests/mock_libpod.py` accordingly.) Schedule tests: `clean({})['cleanup'] is True`, `clean({'cleanup': 'no'})['cleanup'] is True`, `configure(..., cleanup=False)` is saved and in `view()`, `configure(..., cleanup='x')` raises ValueError, omitting it keeps the value. Server test: `POST /api/schedule {'enabled': True, 'time': '04:00', 'cleanup': False}` -> view `cleanup` False. Page test: `'id="sch-clean"'`, `'cleanup'`.

- [ ] **Step 2: implement.** `rollback.py`:

```python
def release(ids: set[str], log) -> None:
    """Remove the images a replaced point kept, each only when nothing else needs it: it has no name, no container
    was created from it, and no stack's point keeps it. A failure is logged; it stops nothing."""
    if not ids:
        return
    try:
        images = pa.images()
        used = {short_id(c.get('ImageID')) for c in pa.containers()}
    except pa.PodmanError:
        return
    kept = kept_images()
    for image in images:
        iid = short_id(image.get('Id'))
        if iid not in ids or image.get('RepoTags') or iid in used or iid in kept:
            continue
        try:
            pa.remove_image(iid)
        except pa.PodmanError as exc:
            if log is not None:
                log(f'  the old image {iid[:12]} was not removed: {exc}')
            continue
        if log is not None:
            size = image.get('Size')
            log(f'  removed the old image {iid[:12]}' + (f' ({size / 1e6:.0f} MB)' if isinstance(size, int) else ''))
```

`keep(folder, point, log=None, cleanup=False)`: before `write_point`, `old = {i['id'] for i in (load_record(folder) or {}).get('images', [])}`; after a successful `write_point`: `if cleanup: release(old - {i['id'] for i in point['images']}, log)`. `stacks.deploy`: `rollback.keep(folder, before and {...}, log if ... else None, cleanup=schedule.cleanup_on())` (the `schedule` import is already in `deploy`); the clean-up line is logged also when the "no point" log is off, so pass a `log` for release separately if needed: `keep(..., log=..., cleanup=..., say=log)` is NOT wanted; simply let `release` use `log` only when `keep` got one, and give `keep` the deploy's `log` whenever `before` is not None (an update). `roll_back` keeps calling `keep(folder, newer, log)` without clean-up.

`schedule.py`: `clean()` adds `'cleanup': raw['cleanup'] if isinstance(raw.get('cleanup'), bool) else True`; `cleanup_on()`; `configure(..., cleanup=None)` validates bool-or-None; `view()` adds `cleanup`. `server.schedule_save`: `schedule.configure(data.get('enabled'), data.get('time'), datetime.now(), data.get('cleanup'))`.

`page.html` `loadSchedule`: a field under the time: `<label class="field"><span>Remove the images a replaced rollback point kept</span><select id="sch-clean" style="width:auto"><option value="on" ...>On</option><option value="off" ...>Off</option></select></label>`, sent as `cleanup: $('sch-clean').value === 'on'` in Save.

- [ ] **Step 3:** Run `python -m unittest tests.test_rollback tests.test_schedule tests.test_schedule_server tests.test_page tests.test_images` — Expected: OK.

### Task 6: a partly stopped stack

**Files:** Modify `stack_ui/stacks.py` (`deploy(services=None)`), `stack_ui/server.py` (`auto_update`). Test: `tests/test_auto_update.py` (class `PartialTest(AutoUpdateBase)`), `tests/test_stacks.py`.

- [ ] **Step 1: failing tests**

```python
    SERVICE = 'io.podman.compose.service'

    def two(self, labels=True):
        self.container['Labels'][self.SERVICE] = 'app'
        return self.mock.add_container('side', project='web', state='exited',
                                       labels={self.SERVICE: 'side'} if labels else None)

    def test_only_the_running_services_are_recreated(self):
        self.two()
        r = self.ui.auto_update('web')
        self.assertEqual(r['result'], 'updated', r)
        calls = self.compose_calls()
        self.assertEqual(calls[-2][-2:], ['pull', 'app'])
        self.assertEqual(calls[-1][-4:], ['up', '-d', '--force-recreate', 'app'])

    def test_a_fully_running_stack_gets_no_service_names(self):
        self.container['Labels'][self.SERVICE] = 'app'
        self.ui.auto_update('web')
        self.assertEqual(self.compose_calls()[-1][-3:], ['up', '-d', '--force-recreate'])

    def test_skipped_when_a_running_container_names_no_service(self):
        self.mock.add_container('side', project='web', state='exited')
        r = self.ui.auto_update('web')                                       # web itself has no service label
        self.assertEqual(r['result'], 'skipped', r)
        self.assertIn('service', r['reason'])
        self.assertEqual(self.commands(), [])
```

(`commands()` of `AutoUpdateBase` maps a call to its last argument; with service names the last argument is the service, so these tests look at `compose_calls()` directly.) `tests/test_stacks.py`: `st.deploy(..., services=['a', 'b'])` ends its `up` with `'--force-recreate', 'a', 'b'` when `pull=True`.

- [ ] **Step 2: implement.** `stacks.deploy(..., services: list[str] | None = None)`: `run_compose(..., ['pull', *(services or [])], log)` and `['up', '-d', *(['--force-recreate'] if pull or recreate else []), *(services or [])]`. `server.py`:

```python
SERVICE_LABELS = ('io.podman.compose.service', 'com.docker.compose.service')


def service_of(c: dict) -> str:
    labels = c.get('Labels') or {}
    return next((str(labels[k]) for k in SERVICE_LABELS if labels.get(k)), '')
```

In `auto_update`, after the `was_running` / stopped check:

```python
            rows = st.project_containers(name, snap['containers'])
            services = None
            if any(c.get('State') != 'running' for c in rows):      # partly stopped: what is stopped stays stopped
                services = sorted({service_of(c) for c in rows if c.get('State') == 'running'})
                if '' in services or not all(re.match(r'^[A-Za-z0-9][A-Za-z0-9._-]*$', s) for s in services):
                    return out('skipped', 'partly stopped, and a running container names no compose service: '
                                          'update it on the page')
                log.append('partly stopped: only ' + ', '.join(services) + ' is updated')
```

and `st.deploy(..., services=services, ...)`. The roll back after a failed partial update recreates the whole stack (`roll_back` -> `deploy` without services), which would start the stopped services: pass the services through: `rollback.roll_back(..., services=services)` -> `st.deploy(..., services=services)` (new optional parameter of `roll_back`, default None). Test: a partial update that fails verify rolls back with `up ... app` only.

- [ ] **Step 3:** Run `python -m unittest tests.test_auto_update tests.test_stacks tests.test_rollback` — Expected: OK.

- [ ] **Step 4 (Part B done):** README (Image updates bullet: partly stopped stacks, clean-up, bulk switch), CHANGELOG entry, spec kept in line, whole suite Windows + WSL, browser look with the mock demo (filter, bulk buttons, Settings switch; mobile width; console).

## Part C

### Task 7: `init: true`, and the output of a stopped command

**Files:** Modify `compose.example.yaml`, `tools/install-pi.py`, `stack_ui/stacks.py` (`run_compose`), `tests/fake_compose.py`, `README.md`. Test: `tests/test_install_pi.py`, `tests/test_run_compose.py` (or `tests/test_operations.py`, where the hang tests are).

- [ ] **Step 1: failing tests.** (a) the compose text `tools/install-pi.py` writes and `compose.example.yaml` both contain a line `    init: true` inside the service (read the file / call the function the existing install tests use). (b) with `FAKE_COMPOSE_HANG='up:30'` and a short `timeout`, `st.run_compose(...)` raises the "did not finish" StackError and the log holds the line the fake printed before it hung; (c) the same for a cancelled command (`op.cancelled` set from a timer thread, as the existing cancel test does): the log holds the line, then `CANCELLED` is raised.

`tests/fake_compose.py`: in the hang branch, before sleeping: `print('fake podman-compose started', ' '.join(sys.argv[1:]), flush=True)`.

- [ ] **Step 2: implement.** `run_compose`:

```python
    def emit(text) -> None:             # what the command printed, also when it was stopped: the last lines
        for line in (text or '').splitlines()[-MAX_OUTPUT_LINES:]:
            log(line)
```

`MAX_OUTPUT_LINES = 200` next to the timeouts. Timeout branch: `try: out, _ = proc.communicate(timeout=KILL_GRACE + 2)` / `except subprocess.TimeoutExpired: proc.stdout.close(); out = ''`, then `emit(out)` before either raise. After the normal return: `emit(out)` first (replacing the `for line in out.splitlines(): log(line)` loop, without the cap for a command that finished: keep the loop for the normal case and call `emit` only on the cancelled path), then the `CANCELLED` / exit-code raises.

`compose.example.yaml` and the template in `tools/install-pi.py`: add `    init: true` under `restart: always` with the comment `# reaps what a cancelled podman-compose leaves behind`. README: one sentence in the install section: an existing install gets the line by hand (or by `install-pi.py --update` if that rewrites the compose file; check what `--update` does and say what is true).

- [ ] **Step 3:** Run `python -m unittest tests.test_install_pi tests.test_run_compose tests.test_operations` — Expected: OK.

- [ ] **Step 4 (Part C done):** CHANGELOG entry, whole suite Windows + WSL. Then the final whole-change review by a fresh reviewer, one fix pass (each fix RED -> GREEN), suite again.

No commit: the user says when.
