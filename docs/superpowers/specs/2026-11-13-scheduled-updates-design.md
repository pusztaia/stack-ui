# Scheduled update check and automatic update: design

Date: 2026-11-13. Roadmap step 4 (the last one not started). Agreed in the brainstorm:

- the check runs once a day at a settable time (default 04:00), on every stack, read-only;
- per stack a two-state switch: **Off** (default) / **Auto-update**;
- an automatic update that fails is rolled back by itself, and that image version is not tried again;
- **no outgoing notification** (no ntfy, Gotify, webhook): the result is shown on the page only.

## Goal

The owner runs about 12 stacks on a Raspberry Pi 4 and opens the page now and then. Today the registries are asked
only when *Check for updates* is pressed. After this change the page knows every morning which stacks have a newer
image, the stacks switched to Auto-update are already updated, and a stack whose update went wrong runs its previous
image again, with a bar on every page saying so until it is acknowledged.

Success: with nobody at the page, (1) the list shows the result of last night's check, (2) an Auto-update stack with
a newer image runs it, (3) one that did not come up healthy runs what it ran before and is reported, (4) nothing is
ever deployed that the owner has not saved and deployed by hand once (no unsaved edits, no new stacks, no removals).

## Parts

### `stack_ui/schedule.py` (new)

Holds the state, the "is it due" rule and the run loop. It does not import `server`: the server hands it the
functions it needs (check, per-stack update), so it is tested without HTTP and without threads.

State file: `STACK_UI_DATA/schedule.json`, written atomically, mode 0600, under its own lock. Not in a stack's
folder (a container may write there) and not in `settings.json` (that one holds the token hash and changes rarely).

```json
{"enabled": true, "time": "04:00", "lastRun": "2026-11-13",
 "auto": ["immich", "jellyfin"],
 "failed": {"immich": {"at": "2026-11-13", "digests": {"ghcr.io/immich-app/immich-server:release": ["sha256:..."]}}},
 "alerts": [{"stack": "immich", "at": "2026-11-13T04:03:10Z", "result": "rolled-back", "reason": "..."}],
 "runs": [{"started": "...", "finished": "...", "trigger": "schedule", "checked": 12,
           "updates": ["immich"], "results": [{"stack": "immich", "result": "rolled-back",
           "reason": "...", "log": ["..."]}]}]}
```

A file that is missing, damaged, too large (over 256 KiB) or of the wrong types is read as the defaults
(`enabled: true`, `time: "04:00"`, nothing else); wrong parts are dropped one by one, not the whole file where that
is possible (`time` must match `^([01]\d|2[0-3]):[0-5]\d$`, `auto` is a list of valid stack names, and so on).
`runs` keeps the last 10; a result's `log` keeps its last 40 lines, each cut at 500 characters.

**Due rule** (`due(now, state)`, `now` a local `datetime`): enabled, `now`'s date is not `lastRun`, and `now`'s
time is at or after `time`. So a server that was down at 04:00 runs the check when it starts later that day, and
never twice a day. `lastRun` is written when a run *starts*: a crash in the middle does not loop.
Saving the settings never starts a run by itself: when the new time has already passed today and today has no run
yet, `lastRun` is set to today (the first run is tomorrow; *Run now* is the way to run at once).

**Thread**: `main()` starts one daemon thread that wakes every 30 seconds, and starts a run when it is due. A run
that raises is logged to stderr and recorded as a failed run; the thread goes on. Times are the process's local time
(`TZ` in the compose file); the Settings page shows the server's clock next to the setting.

### A run

1. `updates(check=True)`: the existing check (anonymous HEAD requests, 4 at a time). It needs no change lock.
   Registry errors are part of its result, as today.
2. For every stack in `auto` whose status is `update`, in name order, one after the other: decide, then update.
3. The run is recorded (`runs`), alerts are added.

Only one run at a time; *Run now* during a run answers 409. A run started by *Run now* does not count as the
day's run (the scheduled one still happens). A run that is due shows as "next: now".

**A stack is skipped** (result `skipped`, with the reason, no alert) when:

| reason | why |
|---|---|
| protected (the page itself, `STACK_UI_PROTECTED`) | redeploying it would remove the container the page runs in |
| not deployed, or no folder of that name any more | nothing is created by the schedule |
| its files differ from the last deploy, or there is no record of it (drift `differs` / `unknown`) | edits nobody deployed are not put live by a timer |
| the plan has errors, missing secrets, or containers in its way | these need a person's decision |
| every image with an update has the digests remembered in `failed` | this version already failed here |
| another change holds the lock | the page is in use; the stack is tried again tomorrow |

**The update** runs as one operation (`auto-update`, cancellable, shown in the bar like any change), holding the
change lock from start to end so nothing else slips in between its steps:

1. note which of the stack's containers are running;
2. `stacks.deploy(pull=True, approve=lambda c: False)`: exactly what *Update* on the page does, including the
   rollback point;
3. **verify**: wait 15 s (a container that dies at once has died by then), then poll the stack's containers every
   5 s, for at most 120 s, until none has a health check still `starting`; then every container that was running
   in step 1 must be running, and none may be `unhealthy`. A health check still `starting` at the end counts as
   fine when the container runs (a long `start_period` is not a failure).
   (`podman-compose up -d` returns 0 for a container that starts and dies at once; this is what catches it.);
4. success: result `updated`; the stack's entry in `failed` is removed.

**When it fails**:

- the pull failed (nothing was changed; the point on disk is not `pending`): result `failed`, reason from the log,
  no rollback, no alert; tried again tomorrow.
- it was cancelled on the page: result `cancelled`, no rollback (the person at the page decides), alert.
- anything after the pull (the `up`, or the verify): `rollback.roll_back(...)` with the point this deploy wrote,
  inside the same operation. Result `rolled-back`; the registry digests of the images that had an update are stored
  in `failed[stack]`; alert. The roll back is verified the same way as an update; when it fails or does not verify:
  result `rollback-failed`, alert, and nothing more is tried.
- no point exists (the stack was never deployed from this page: cannot happen after the drift rule, but the code
  must not assume it): result `failed`, alert.

After a rollback the tag is back on the old image, so the list keeps showing "update available" for the stack, and
its row says why it is not taken ("failed on 2026-11-13, waiting for a newer image").

### Server (`server.py`)

- `auto_update(name)`: the update above, built from the existing pieces (`operation`, `refuse_protected`,
  `require_podman_state`, `drift`, `st.plan`, `st.deploy`, `rollback.roll_back` with the same `put` as
  `run_rollback`). `Operation.TEXT` gets `'auto-update': 'the automatic update of {t}'`.
- `overview()` rows get `auto: bool` and `autoNote: str` (the "failed on ..." text, else empty).
- Deleting a stack's folder and uploading a new stack of an old name remove the name from `auto` and `failed`
  (a new stack does not inherit the switch).
- The answer every page already polls for the running change also carries `alerts` (the unacknowledged ones) and
  whether a scheduled run is in progress.

Routes (page session only: the API token of the deploy scripts is refused on all of them, as on the settings):

| route | does |
|---|---|
| `GET /api/schedule` | `{enabled, time, serverTime, lastRun, nextRun, running, auto, alerts, runs}` |
| `POST /api/schedule` `{enabled, time}` | saves the setting (validated; 400 on a bad time) |
| `POST /api/schedule/run` | starts a run in a thread and returns at once; 409 when one is running |
| `POST /api/schedule/ack` | clears the alerts |
| `POST /api/stacks/<name>/auto` `{auto: bool}` | the stack's switch; 400 for a protected stack or an unknown name |

### Page (`page.html`)

- **Settings**: a *Scheduled check* section: on/off, the time, the server's clock, last and next run, *Run now*,
  and the last runs (per run: when, how many stacks were checked, which had updates, and per stack the result and
  reason; the log opens on a click).
- **Stack, Summary tab**: *Automatic update: Off / On*, with one sentence on what it does and when it holds back;
  disabled with the reason on a protected stack.
- **Stack list**: a small "auto" mark on the stacks switched on; `autoNote` as its tooltip and in the row when set.
- **Alert bar** on every page while there are alerts: "Automatic update of immich failed at 04:03 and was rolled
  back" (or "... and the roll back failed too: the stack needs you"), with *Details* (the run) and *Dismiss*.
  Styled like the existing notes (the `--bad` edge for a failed roll back, `--warn` otherwise).

## Revision 2026-11-13 (final review)

- **Approved files.** "Its files differ from the last deploy" is not judged by `.deployed/` (it is in the stack's
  folder, a container that mounts it can write it). Every deploy a person starts from the page stores the
  SHA-256 of the stack's files in the state (`deployed: {stack: {file: hash}}`); an automatic update runs only
  when both the folder and `.deployed/` hold exactly those files (regular files only), checks the folder again
  after the pull, right before anything is changed (changed meanwhile: result `failed`, alert, nothing started),
  and does not store hashes itself. A stack deployed before this version is skipped until it is updated from the
  page once.
- **A stopped stack** (none of its containers runs) is skipped: an update would start it.
- **Roll back only after the pull.** Whether to roll back is decided by "this update got past its pull", not by a
  `pending` point on disk (that may be left by an earlier, interrupted update).
- **Every failure after the pull raises an alert**, also when no point could be written, and an `OSError` during
  the update or the roll back is a result like any other error.
- The API token is refused by the parsed route (a trailing slash is the same route).

Known and left as they are (minor): verify decides from one last look, so a container that restarts in a loop can
look running; the memory of a failed version stays until a newer image or until the switch is turned off, also
when the owner updated that version by hand; a *Run now* still running at the set time is followed by the
scheduled run; *Dismiss* clears every alert, also one added since the page last asked.

## What it does not do

No notifications; no weekly schedule or maintenance window; no per-stack time; no automatic update of the page
itself; no image signature check (the switch means "I trust this tag": the README says so); no update of a stack
that needs any decision.

## Errors and edge cases

- The data folder is not writable: the setting cannot be saved (the POST says so); the daily check still runs with
  the defaults, and since `lastRun` cannot be kept, it is held in memory so the run does not repeat every 30 s.
- The Podman socket does not answer: the run is recorded as failed with that message; no stack is touched.
- The server is stopped during a run: the existing shutdown wait for the change lock applies; `lastRun` is already
  written, so the run is not repeated that day; a stack caught mid-update keeps its `pending` rollback point and
  shows *Roll back* as after any interrupted update.
- A stack is renamed or removed by hand: its name in `auto` matches no folder and is skipped ("no folder").
- Clock changes (DST, NTP step): the rule compares dates, so at most one run a day either way.

## Tests

Unit (no threads, injected clock and functions): the due rule (before / after the time, already run, disabled,
restart after the time, saving a passed time); state file (defaults, damaged, oversize, wrong types, 0600, caps on
runs and log); every skip reason; a run's record.

With the mock libpod and `fake_compose`: update succeeds; `up` fails -> rolled back, digests remembered, alert;
container exits right after `up` -> verify fails -> rolled back; unhealthy -> rolled back; health `starting` then
healthy -> success; pull fails -> no rollback, no alert; the same digests next day -> skipped; newer digests ->
tried again; roll back fails -> `rollback-failed`; lock held -> skipped; cancel -> no rollback; protected and
drifted stacks untouched.

Server: the routes (auth, validation, 409 on a second run), `auto` in the overview, alerts in the poll answer,
folder delete and upload drop the switch. Page: the new strings and controls exist (`test_page.py`).

Never against the real Pi, a real registry or real Podman (stacks/CLAUDE.md).
