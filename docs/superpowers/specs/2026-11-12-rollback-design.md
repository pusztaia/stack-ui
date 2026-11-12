# Roll back a stack to its previous deploy: design

Date: 2026-11-12 · Target version: 1.0.22

## Goal

After an update a stack may not come up healthy. Today the page keeps only the files of the *last* deploy
(`.deployed/`) and nothing about the images, so the state that worked is gone: the compose file may have been edited,
and with a floating tag (`ghcr.io/pusztaia/stack-ui:1`) the pull has moved the tag to the new image.

Success: after any deploy of an already deployed stack, one button on the stack's page puts back the images and the
files the stack ran with before that deploy, without a download, also when the deploy itself failed, timed out or was
cancelled.

## Decisions made with the user

- **Only on a button.** Nothing rolls back by itself. The button is on the stack's page whenever a rollback point
  exists, and next to the error of a failed deploy.
- **The folder's files are written back.** The compose and env file (and the other stack files) in the stack's folder
  return to the previous deploy's content; what they held goes to `.history/` as with every save. After a rollback the
  stack shows no difference from what is deployed.
- **Approach A: tag the old image back.** The old image gets its tag again and the stack is recreated without a pull.
  Set aside: pinning image ids in a generated compose file (the tag would stay on the new image, the next plain
  deploy would bring the bad version back); pulling the old digest from the registry (needs the network, the registry
  may no longer have it).
- **One step back.** One rollback point per stack; the next deploy replaces it.

## What exists today

- `stacks.deploy()`: plan, optional `pull`, remove the containers in the way, `up -d`, then `save_deployed()` copies
  the compose file, env file and stack files into `<stack>/.deployed/`.
- `stacks.stack_action(..., 'delete')` runs `down` and removes `.deployed/`.
- `server.run_deploy()` wraps the deploy in `operation('deploy', name, cancellable=True)` and returns `{ok, log}`.
- `server.write_file()` keeps the previous version in `.history/` and replaces a file atomically.
- `podman_api.tag_image(iid, repo, tag)`, `podman_api.containers()` (each with `Image`, `ImageID`),
  `podman_api.images()`.
- `server.image_table()` marks an image `removable` when it has no tag and no container uses it; `images_remove()`
  refuses images a container uses. `server.OWN_FOLDERS` names the page's own folders in a stack's folder.

## The rollback point

A folder `<stack>/.previous/`, written by a new module `stack_ui/rollback.py`:

```
.previous/
  rollback.json     {"taken": "2026-11-12T18:04:11Z",
                     "compose": "compose.yaml", "env": ".env",
                     "images": [{"container": "web", "ref": "docker.io/library/nginx:latest", "id": "<64 hex>"}]}
  files/            a copy of .deployed/ as it was (compose, env, stack files)
```

- `images`: one entry per container of the stack at that moment: its name, the image reference it was created from
  (`Image`) and the image id (`ImageID`).
- **When it is written:** in `stacks.deploy()`, for an `update` only (the stack has containers) and only when
  `.deployed/` holds the compose file (the stack was deployed from the page before). It is captured in memory before
  the pull and written to disk after a successful pull, just before the containers in the way are removed and `up`
  runs. So a failed pull leaves the existing point alone (nothing changed), and a failed, timed-out or cancelled `up`
  already has its point.
- It is written to `.previous.new/` and swapped in (rename), with the owners matched like `.deployed/`.
- A deploy that cannot write a point (no `.deployed/` record, a first create) logs one line saying so and removes a
  stale `.previous/`: a point must never describe an older state than "before the last deploy".
- `delete` of the stack removes `.previous/`. `.previous` joins `OWN_FOLDERS`.

## Rolling back

`rollback.roll_back(folder, compose_file, env_file, write_file, log)`:

1. Read the point. Refuse (`StackError`) when: there is none; its `compose` / `env` names differ from the stack's
   current ones (`stack.json` changed since); or an image id of the point is no longer on the host. The message names
   what is missing. Nothing has been changed at this point.
2. Capture a new point of the current state (the same function the deploy uses) in memory.
3. Write the point's files into the folder through `write_file` (so each replaced version lands in `.history/`), only
   those whose content differs. Files that exist in the folder but not in the point are left alone.
4. Tag each image id back to its reference (`podman_api.tag_image`), skipping entries whose reference has a digest
   (`@`), and those whose image already carries the tag.
5. Write the new point (it replaces the one being used), then run `stacks.deploy(..., pull=False, recreate=True,
   point=False)`: `up -d --force-recreate` with the usual 5-minute limit, then `save_deployed()`. `point=False` keeps
   the deploy from writing a point of its own. Containers in the way are not removed (`approve` answers no): a
   rollback never deletes foreign containers.

Because step 5 leaves a point of the state that was rolled back from, the same button returns to the newer version:
a rollback is reversible, and so is a rollback that itself fails in `up`.

Not covered, and said in the dialog: data in volumes and bind mounts is not rolled back. If the newer version
migrated a database, the older one may not start on it.

## Server

- `rollback_view(name)` for `GET /api/stacks/<name>/rollback`:
  `{available, taken, images: [{container, ref, previous, current, present}], diff: {compose, env, files}, problems}`.
  `previous` / `current` are image ids (`current` from the running containers, null when the container is gone),
  `present` says whether the previous image is still on the host, `diff` is folder → point (unified diffs, as
  `deployed_diff` builds them), `problems` lists the reasons a rollback would be refused (empty: it can run).
  Without a point: `{available: false}`.
- `run_rollback(name)` for `POST /api/stacks/<name>/rollback`: `refuse_protected(name)` (the page's own stack is not
  rolled back from the page), `operation('rollback', name, cancellable=True)`, file writes under `FILES_LOCK`,
  returns `{ok, log}` like a deploy; `StackError` / `PodmanError` / `ValueError` become `ok: false` with the log.
- `discard_rollback(name)` for `POST /api/stacks/<name>/rollback/discard` (the server has no DELETE routes): removes `.previous/` under
  `operation('rollback-discard', name)`; `{discarded: true}`.
- All three sit behind the same checks as the other stack routes (session or API token, `X-UI-Token` for the two
  changes); a busy page answers 409 as for every change. `busy_text()` learns the two new kinds.
- **Images kept for a rollback.** `image_table()` adds `keptFor: [stack, ...]` to an image whose id is in a stack's
  point and that no container uses; such an image is not `removable`, `images_remove()` refuses it ("kept for the
  rollback of web: discard the rollback point on the stack's page"), and `prune` skips it through `removable`.
- `run_deploy()` adds `rollback: true` to a failed result when a point exists.

## Page

- Stack detail: a **Roll back…** button in the head actions when the stack's row of `GET /api/stacks` says
  `rollback: true` (a point exists; no extra request for the button).
- The dialog: when the point was taken; a table container / image / previous id → current id (short ids, "same" when
  equal, "image no longer here" in red when not `present`); the file differences; the note about data; `problems`
  as an error note, in which case the confirm button is not built. Buttons: **Roll back** (primary), **Discard this
  rollback point** (asks once more), Cancel.
- While it runs the operation bar shows "the rollback of web"; the result is shown like a deploy's log.
- A failed deploy's result dialog gets a **Roll back…** button when the result says `rollback: true`.
- Images page: a "kept for rollback: web" tag on such rows; their tick box is disabled like a used image's.

## Errors

| Situation | Result |
|-----------|--------|
| No point | button absent; `POST` → `ok: false`, "no rollback point" |
| Previous image removed from the host | refused before any change, the image and container named |
| `stack.json` names changed since the point | refused before any change |
| `up` fails / times out / is cancelled during the rollback | files and tags are already the previous ones; the error is shown; the point of the newer state exists, so the button leads back |
| Page busy | 409 with the running operation, as everywhere |
| Podman not reachable | refused before any change |

## Tests

New `tests/test_rollback.py` (mock libpod + `fake_compose.py`, no real Podman), each written first and seen failing:

- a deploy of a deployed stack writes the point (files of the old `.deployed/`, the containers' images); a first
  create writes none; a failed pull keeps the old point; a failed `up` has the new point.
- rollback: files restored with the replaced versions in `.history/`; `tag_image` called for moved tags only (the
  mock gets a tag endpoint that moves a tag between images); `up -d --force-recreate` without `pull`; `.deployed/`
  equals the point's files; the new point describes the state rolled back from; a second rollback returns to it.
- refusals: no point, missing image, changed file names: nothing written, nothing tagged.
- routes: view, run, discard, 409 while busy, the protected stack refused, token required.
- images: `keptFor`, not removable, `images_remove` refuses, discard frees it.
- `tests/test_page.py`: the page contains the button, dialog and tag names.
- `delete` removes `.previous/`; `stack_folder()` does not count `.previous` as application data.

Full suite on Windows and in WSL (Python 3.9 path via `test_py39`).

## Out of scope

- Automatic rollback (comes with the scheduled update check and Auto-update).
- More than one point; rolling back volumes or data.
- A rollback of the page's own stack from the page.
