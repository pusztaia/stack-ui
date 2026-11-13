# Deploy record in the data folder, rollback fixes, automatic-update comfort, operations: design

Date: 2026-11-13. Three parts, done in this order, each releasable on its own (A: 1.0.25, B: 1.0.26, C: 1.0.27;
the versions are the owner's call). Agreed in the brainstorm: existing records are taken over only when they
match the hashes 1.0.24 stored; clean-up removes the images of the rollback point that is replaced.

## Goal

A container that mounts its stack's folder can write everything in it. After 1.0.23 (rollback point) and 1.0.24
(hashes for the automatic update) one thing in that folder is still trusted: `.deployed/`, the copy of the last
deploy's files. *Differs*, the Diff tab, the *Changed* marks and the content of the next rollback point are read
from it. Part A moves it to the page's data folder and fixes the five rollback defects left from the 1.0.22
review. Part B makes the automatic update comfortable on a host with a dozen stacks and a small SD card. Part C
is two small operational fixes.

## Part A: the deploy record, and the rollback fixes

### The record

- New place: `STACK_UI_DATA/deployed/<stack>/` (`stacks.record_dir(folder)`), the same files as before. Written
  whole or not at all (`<stack>.new`, then swapped in), regular files only, links never followed, no owner matching.
- Read from there by: `deployed_diff` (drift, the Diff tab, the *Changed* marks of the file list) and
  `rollback.capture`. Nothing in a stack's folder is read as a record.
- A record that cannot be written (the data folder is missing or read-only) does not fail a deploy that already
  ran: the old record is removed, the log says so, the stack shows *Unknown*.
- `.deployed/` in a stack's folder is a leftover: the stack's next deploy removes it (a link is unlinked, not
  followed), and deleting the stack does too. It stays in `OWN_FOLDERS` so the folder-size view ignores it.
- Deleting a stack's folder and uploading a new stack of an old name drop the record.

**Taking over existing records** (once, at start, `server.adopt_records()`; harmless to repeat): for every stack
without a record in the data folder whose `.deployed/` holds exactly the files whose hashes 1.0.24 stored
(`schedule.approved(folder/.deployed, ..., name)`), the files are copied to the new place. Every other stack shows
*Unknown* until its next *Update* from the page; its rollback point, if any, is untouched.

The hashes of 1.0.24 stay: the automatic update still checks the folder against them before it starts and again
after the pull. Its second check (that `.deployed/` matches too) goes away, the record is trusted now.

### The five rollback fixes

1. **No containers:** a roll back of a stack without containers cannot describe "what runs now", so today it
   drops the point it has just used. It keeps that point instead (the log says so).
2. **A name that could not be given back:** when tagging fails midway, the names already moved are put back on
   the images that held them (best effort) before the error is raised, so the newer images do not end up
   untagged and removable. Order becomes: checks, tags, files, new point, containers.
3. **The dialog's diff** lists only the files the roll back writes (those of the point). A file that exists only
   in the folder is kept by a roll back and is no longer shown as deleted.
4. **Checked like a save:** before anything is changed, every file the roll back would write goes through the
   checks of a save on the page (`check_compose`, `check_env`, `check_stack_file`). One that fails stops the roll
   back with the file's name and the reason; nothing has been changed then.
5. **`sha256:<hex>`** as a container's image reference is no name: `split_ref` returns None for it.

## Part B: comfort for the automatic update

- **Filter and bulk switch.** The stack list gets the filter *Automatic update*. The selection bar gets
  *Auto-update on* / *off* for the ticked stacks: `POST /api/stacks/auto` `{names, auto}` ->
  `{changed: [names], skipped: [{name, reason}]}` (protected and unknown names are skipped with the reason).
  Page session only, like the per-stack switch.
- **Image clean-up.** When an update writes a new rollback point over an old one, the images only the old point
  kept are removed, each only if it has no name, no container uses it and no stack's point keeps it. The log names
  each image and its size; a removal that fails is logged and nothing else happens. On for updates from the page
  and automatic ones; a switch on *Settings* (*Remove the images a replaced rollback point kept*, on by default,
  kept in `schedule.json` as `cleanup`). Discarding a point by hand removes nothing.
- **A partly stopped stack.** The automatic update recreates only the services whose containers run
  (`podman-compose pull <services>`, `up -d --force-recreate <services>`); what was stopped stays stopped unless a
  running service depends on it (podman-compose starts dependencies). The service of a container is read from its
  `io.podman.compose.service` / `com.docker.compose.service` label; when a running container has none, the stack
  is skipped with that reason. A stack whose containers all run is updated as before, without service names.

## Part C: operations

- **An init process in the image** (changed during the implementation, was `init: true` in the compose files):
  the image installs `tini` and starts the page under it, so the children of a cancelled `podman-compose` are
  reaped. `init: true` needs `catatonit` on the host and a hand edit of every existing compose file; an image
  that brings its own init needs neither, an existing install gets it with the next pull.
- **Output is kept.** When a `podman-compose` command is cancelled or runs into its time limit, what it printed
  until then goes to the change's log (the last 200 lines) before the error.

Also decided during the implementation: the automatic update still skips a stack that has no record of its last
deploy (there would be nothing to roll back to); the bulk route `/api/stacks/auto` is refused to the API token
like the per-stack one.

## Revision 2026-11-13 (final review)

- **One read per file.** Stack files are read through one open that follows no link, regular files up to 512 KiB
  only (`stacks.read_plain`); checking a path and then copying it would let a container swap in a link between the
  two. The record, the hashes and the adoption of old records work on the bytes read once.
- **The record is what was there before `up`.** `deploy` reads the stack's files once right before the containers
  are brought up, hands their hashes to the automatic update's check, and writes the record and the hashes from
  those bytes after `up`: what a container puts into the folder during `up` does not get into the record. A file
  over 512 KiB is left out of the record (the log says so).
- **Roll back:** a target in the folder that is a link or no regular file stops it before a name is moved; any
  error while writing puts the moved names back; a folder file that is not text or is huge no longer blocks it.
- **Kept images** are read from every point in the data folder, also of a folder that is no stack at the moment.

Known and left as they are (minor): the undo of a failed roll back does not take a name away from an image when no
image held that name before; a partial update starts a stopped service a running one depends on.

## What it does not do

No migration of records that do not match the stored hashes; no pruning of images that no point ever kept; no
per-service switch; no change to the web terminal (parked) or to notifications (dropped).

## Tests

Mock libpod and `fake_compose` only. A: record written to and read from the data folder; a planted `.deployed/`
changes neither drift nor the next point; leftover removed by the next deploy; unwritable data folder; adoption
(matching, not matching, already present, link); folder delete and upload; each of the five fixes. B: the route
(changed / skipped, token refused), filter and buttons in the page; clean-up removes only unnamed, unused, unkept
images, logs, survives a failing removal, respects the switch; partial stack recreates only running services,
skips without labels. C: the Dockerfile installs `tini` and starts the page under it; cancelled and timed-out commands leave
their output in the log. Whole suite on Windows and Linux (WSL).
