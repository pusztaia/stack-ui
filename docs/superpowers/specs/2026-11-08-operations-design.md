# Running operation: status, cancel, time limit: design

Date: 2026-11-08 · Target version: 1.0.20

## Goal

A stack's `podman-compose up` can hang (typically a dependency that never becomes healthy). Today that holds the
page's one change lock for up to 15 minutes, every other change (including *Stop* of that very stack) waits behind it
without a word, and nothing can end it short of restarting the page's container.

Success: while a change runs the page says which one and for how long; any further change is refused at once with that
information; the running `podman-compose` operation can be cancelled from the page without a restart; and an `up` that
does not finish ends after a few minutes with an error that names the containers that are not healthy.

## Decisions made with the user

- **Busy means refused, not queued.** A second request while one runs gets `409` at once, naming the running
  operation. One change at a time stays (no per-stack locks, no background jobs).
- **Cancel stops the command only.** It ends the `podman-compose` process and its children and frees the lock. The
  containers the command already created stay; *Stop* or *Delete* work as usual afterwards. No extra clean-up.
- **Approach A:** a small registry around the existing lock. Background jobs (a different API) and "only time limits"
  were set aside.

## What exists today

- `server.DEPLOY_LOCK` (a `threading.Lock`) is taken blocking, `with DEPLOY_LOCK:`, in 15 functions: `prune`,
  `image_pull`, `image_tag`, `images_remove`, `image_remove`, `container_do`, `network_create`, `network_delete`,
  `volume_create`, `volume_delete`, `secret_save`, `secret_delete`, `run_deploy`, `stack_action`, `restart`.
- `stacks.run_compose()` runs `podman-compose` with `subprocess.run(..., timeout=LONG_TIMEOUT)` (900 s) for every
  command (`pull`, `up`, `start`, `stop`, `down`); on a timeout `subprocess.run` kills only the direct child.
- `image_pull` is one libpod request of up to 900 s.
- Every call is one HTTP request held open for the whole operation (thread per connection, at most 64).
- Shutdown (SIGTERM) waits up to `SHUTDOWN_WAIT` (8 s) for `DEPLOY_LOCK`.

## Approach

`DEPLOY_LOCK` stays the one lock, but it is taken through a context manager that does not wait and that records what
holds it. The `podman-compose` call learns about the running operation through a thread-local handle, so it can
register its process and honour a cancel. Two new routes expose and cancel the operation; the page shows a bar.

## Server

### Registry and `operation()`

In `server.py`:

- `class Occupied(Exception)` carries the running operation's description; `Handler.api()` answers it with `409` and
  `{"error": "<message>", "operation": {...}}` (placed with the other `except` clauses, before `LookupError`).
- `OPERATION = None` (module state, guarded by the lock's own holder: it is only written by the holder, read by anyone
  through a copy) and a small class `Operation` with `kind`, `target`, `started` (epoch seconds), `cancellable`,
  `cancelled` (bool), `proc` (the `Popen` or `None`).
- `operation(kind: str, target: str = '', cancellable: bool = False)` is a context manager: `DEPLOY_LOCK.acquire(
  blocking=False)` or `raise Occupied(describe(OPERATION))`; sets `OPERATION`, sets the thread-local handle in
  `stacks` (below), yields the `Operation`, and in `finally` clears both and releases the lock.
- Kinds and targets of the 15 sites: `deploy` (stack), `stack-start` / `stack-stop` / `stack-delete` (stack),
  `restart` (container), `image-pull` (reference), `image-tag` / `image-remove` / `images-remove` / `images-prune`
  (image ids, or a count), `container` (container name and action), `network` / `volume` / `secret` (name).
  `cancellable=True` only for `deploy` and the three `stack-*` kinds.
- `describe(op)` is the human message: "a deploy of crownsec has been running for 3 minutes", "stopping crownsec has
  been running for 40 seconds", "pulling docker.io/library/nginx:latest has been running for ...". The `409` text is
  `"Busy: <description>. Wait for it to finish<, or cancel it on the page>."`.
- A short helper formats seconds ("40 seconds", "3 minutes").

### `stacks.run_compose()`

- A thread-local `stacks.RUNNING` (`threading.local()`), set by `operation()` to the `Operation` for its thread.
- `run_compose` starts the command with `subprocess.Popen(..., stdout=PIPE, stderr=STDOUT,
  start_new_session=True)` on POSIX (a process group of its own) and stores the process in the operation (`op.proc`).
  It waits with `proc.communicate(timeout=...)`.
- `kill_tree(proc, grace=3.0)`: POSIX: `os.killpg(proc.pid, SIGTERM)`, wait up to `grace`, then `SIGKILL` the group;
  Windows (development only): `proc.kill()`. Errors from an already finished process are ignored.
- After `communicate` returns, if the operation was cancelled the call raises `StackError('cancelled: the command was
  stopped on request; what it had already created is still there')`.
- Timeout: `kill_tree`, then `StackError` naming the command and, when it can be found, the containers of the project
  that are not ready: those whose libpod `Status` contains `unhealthy` or `starting` ("crownsec_db (unhealthy)").
  If the container list cannot be read the message stays without the list.

### Time limits

`LONG_TIMEOUT = 900` stays for `pull`. New `OP_TIMEOUT` (default 300 s, `STACK_UI_UP_TIMEOUT`, at least 30) for `up`,
`start`, `stop` and `down`. `check_config` keeps its 60 s. `run_compose(timeout=...)` still overrides.

### Routes

- `GET /api/operation` → `{"operation": null}` or `{"operation": {"kind", "target", "since": <epoch>, "seconds",
  "cancellable", "text": "<description>"}}`. Read-only, same sign-in as the other routes.
- `POST /api/operation/cancel` → `{"cancelled": {...description...}}`. `409` `"nothing is running"` when there is no
  operation; `409` `"<description> is a quick change and cannot be cancelled"` when it is not cancellable. Cancelling
  sets `op.cancelled = True` and calls `kill_tree(op.proc)`; it returns once the process is gone (at most the grace
  period plus a second). The cancelled request returns its error to its own caller and the lock is released by its
  `finally`. Both sign-ins work, the page's and the API token's.

## Page

- A bar under the banner (`#opbar`, `role="status"`): "crownsec: a deploy has been running for 3 minutes" with a
  *Cancel* button when cancellable. Refreshed every 3 s while the tab is visible and signed in (a single timer,
  stopped on `visibilitychange`); empty when nothing runs. Text from the server's `text`.
- *Cancel* opens a dialog: "Stop the deploy of crownsec? The command is stopped; the containers it already created
  stay. Use Stop or Delete on the stack afterwards." with *Keep running* and *Cancel the deploy*. On success a toast
  "Cancelled", the bar clears; the deploy dialog of the cancelled request shows the server's `cancelled` error.
- A `409` Busy from any action appears as the existing error (dialog or toast) with the full text. No retry.
- All text goes through `esc()` / `textContent`.

## Edge cases

- Cancel races with a finishing operation: whichever comes second gets `409` "nothing is running".
- The cancelled request's handler is a normal request thread: its `finally` clears `OPERATION` and releases the lock.
- Shutdown (SIGTERM) is unchanged: wait up to 8 s for the lock, then exit; the container's process tree dies with it.
- A stack that Stack UI refuses to touch (its own, `STACK_UI_PROTECTED`) is refused before `operation()` as today.
- Quick changes now get `409` while a deploy runs instead of waiting; the message says why.
- `restart` and `container_do` (podman API calls with a 30 s stop) are not cancellable.

## Tests

The fake `podman-compose` (`tests/fake_compose.py`) gets `FAKE_COMPOSE_HANG=<seconds>` (sleep before answering, and
start a sleeping grandchild) for chosen commands.

- `operation()`: refuses at once with `Occupied` carrying the description; clears itself after a normal exit and after
  an exception; the handle is per thread.
- Route tests with a hanging `up`: the second change gets `409` with the text; `GET /api/operation` describes it
  (`cancellable`); a quick change (secret) gets `409` while it runs.
- Cancel: `POST /api/operation/cancel` ends the process **and the grandchild** (POSIX), the first request returns the
  `cancelled` error, the lock is free, and a `stop` of the same stack then runs. `409` for "nothing is running" and
  for a non-cancellable operation. Works with the API token.
- Timeout: a hanging `up` with `STACK_UI_UP_TIMEOUT` patched short ends with the error; with mock containers whose
  `Status` says `unhealthy` / `starting` the message names them; with an unreadable list it still ends cleanly.
- The existing deploy, start/stop and shutdown tests keep passing; the client prints a `409` message.
- Page: `node --check`, a page test for `#opbar` and the Cancel dialog, and a look in a real browser with a hanging
  test `up` (bar appears, Cancel frees it, Stop works, mobile width).

## Docs and release

README (the Stacks bullet; the configuration table gets `STACK_UI_UP_TIMEOUT`), CHANGELOG, version 1.0.20.

## Out of scope

- Cancelling an image pull (the libpod request needs its own handle) and the other quick changes.
- Per-stack locks or background jobs.
- Finding out why a particular stack's container is unhealthy.
- Clean-up of what a cancelled command left behind.
