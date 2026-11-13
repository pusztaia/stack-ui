# Changelog

## 1.0.24

- *Scheduled update check and automatic update.* The registries are asked once a day (Settings: on/off, the time,
  *Run now*, the last runs). Per stack an *Automatic update* switch (Summary tab, off by default): after the check
  a newer image is pulled and the containers are recreated, then watched (what ran before must run, nothing may be
  unhealthy); an update that does not come up is rolled back to its rollback point, the failed image version is
  not tried again, and a bar on every page reports it until dismissed. Held back, with the reason in the run's
  record: files that are not exactly the ones last deployed from the page (their hashes are kept in the data
  folder, so nothing written from inside a container is deployed by the schedule; a stack deployed before this
  version needs one *Update* from the page first), a stopped stack, a plan that needs a decision, a protected
  stack, another change in progress. No notification leaves the host. The API token of the scripts cannot read or change the schedule.

## 1.0.23

- *Security: the rollback point moved out of the stack's folder.* 1.0.22 kept it in `.previous/` of the stack's
  folder. A container that mounts its stack's folder could write there, and the page acts on a point as root: a
  planted point could have another image given a stack's image name on *Roll back*, a FIFO in its place blocked
  every change of the page, and a large file in it was read into memory with every listing of the images. The point
  is now kept in the page's own data folder (`STACK_UI_DATA/rollback/<stack>/`); nothing in a stack's folder is
  read as one, and a `.previous/` left there is removed by the stack's next deploy (rollback points written by
  1.0.22 are not carried over: the next update makes a new one). Files of a point and of the last deploy are read
  only as regular files up to 512 KiB. A point that cannot be written (no writable data folder) no longer stops a
  deploy: it says so in the log. Deleting a stack's folder, and uploading a new stack of an old name, drop the point.

## 1.0.22

- *Roll back an update.* Every update of a stack that was deployed from the page keeps what the stack ran with before
  it as a rollback point in `.previous/` of the stack's folder (the previous deploy's files and the images its
  containers were created from). *Roll back…* on the stack's page, and in the dialog of a deploy that failed, puts
  the files and the image names back and recreates the containers without a download; what the files held is kept
  in `.history/`. The state rolled back from becomes the new point, so the same button leads forward again. One
  point per stack; the images it needs are marked *Kept for rollback* on the Images page and cannot be removed until
  the point is discarded (or the stack deleted). Volumes and host data are not rolled back.

## 1.0.21

- *New look: an operator console.* Near-black slate with one cyan signal colour; green, amber and red only mean state.
  Identifiers, numbers, table headings and tabs are monospace, the current page is marked by a bar on the menu's edge,
  notes and the operation bar carry a coloured edge. A matching light theme; no external fonts or files.

## 1.0.20

- *A change that hangs no longer freezes the page.* Only one change runs at a time, as before, but a second one is
  now refused at once with the running one named ("Busy: the deploy of crownsec has been running for 3 minutes ...")
  instead of waiting behind it without a word. A bar on every page shows what runs and for how long, and a
  `podman-compose` command (deploy, start, stop, delete) can be cancelled from it: the command and the processes it
  started are stopped, the containers it already created stay, and *Stop* or *Delete* work again. A command that does
  not finish in 5 minutes (`STACK_UI_UP_TIMEOUT`; a pull still 15) is ended, and the error names the containers
  that are not healthy yet. API: `GET /api/operation`, `POST /api/operation/cancel`; a refused change answers `409`.
  Quick changes (a secret, a network, a volume, an image, a container) also get the `409` while a deploy runs.

## 1.0.19

- *Logs*: follow a container's log as it is written. *Follow* (on by default) streams new lines at once instead of
  reloading the whole tail every 5 seconds; *Pause* freezes the view (with a count of the lines that came in),
  scrolling up stops the follow (*Jump to latest* goes back), the filter marks its matches, error lines are red and
  warnings yellow, and *Download* saves what the page holds (at most 5000 lines) as a `.log` file. A stopped or
  restarted container is picked up again by itself. At most 4 logs can be followed at once. API:
  `GET /api/containers/<name>/logs/stream` (newline-delimited JSON; read-only, the same sign-in as the rest).
- *Health*: a Raspberry Pi's power and throttling flags (from the firmware's `get_throttled`). Under-voltage, a capped
  CPU speed, throttling or the soft temperature limit right now is red, one that only happened since boot is yellow;
  the sentences say which, and the Stacks list shows a *Power problem* / *Power dipped* tag only then. Where the file
  is not there (not a Raspberry Pi, or not shown in the container) the row says so.

## 1.0.18

- *Health*: a new page for the Pi in one place - CPU temperature, load against the cores, memory, uptime, disk use
  and what Podman holds. The Stacks list shows the temperature next to the host's RAM and CPU, and a disk that is
  filling up (yellow from 80 %, red from 90 %). The temperature is yellow from 70 °C and red from 80 °C.
- API: `GET /api/health`. Setting: `STACK_UI_HEALTH_PATHS` names more paths whose file system is shown.

## 1.0.17

- *Checks before a deploy*: the plan now lists `errors` and `warnings`. An external network that does not exist and
  a host port that another running container publishes stop the deploy with a plain message (the dialog, the API
  client and `--dry-run` alike); an image name without a registry and a `${VARIABLE}` with no value and no default
  only warn. The stack's own containers never count as holding its ports.
- API: `GET /api/stacks/<name>/plan` also answers `{errors, warnings}`.

## 1.0.16

- New look: slate-blue enamel with one signal-blue accent instead of purple (dark and light), a DIN-like face for titles
  and figures, a lit lamp marks the current page in the side bar, and a row's state runs down its left edge in the
  tables (running, partly running, stopped, failed). The sign-in page and the icon match. Layout, texts and
  behaviour are unchanged.
- `tools/install-pi.py` without arguments is interactive: it asks for the SSH user and host, install or update,
  rootful / rootless, the page's address and port, a build on the host and (optionally) the advanced settings, shows
  the equivalent command line and then logs in over SSH as before. The arguments still work.

## 1.0.15

- *Images*: remove several images at once. Every image no container uses has a tick box (and one in the header for
  all of them); a bar shows how many are ticked and their size, and *Delete…* lists them for a last check. An image
  that a container uses has no tick box, and it is checked again on the host when you confirm: a used one is
  skipped and named, the others are still removed. *Select untagged* ticks the old images that pulls leave
  behind; it replaces the *Clean up* button (the dialog after *Update several stacks* still offers it).
- API: `POST /api/images/delete` also takes `{ids: [...]}` and answers `{removed, freed, errors}`.

## 1.0.14

- The page now stops on SIGTERM (`podman stop`, `podman-compose up --force-recreate`, systemd) at once, and waits up
  to 8 seconds for a change in progress (a deploy, a pull) to finish. Before, a container's main process ignored
  SIGTERM, so every stop or recreate waited the full 10 seconds and ended with SIGKILL
  ("StopSignal SIGTERM failed to stop container stack-ui in 10 seconds").

## 1.0.13

- The session cookie is `Secure` when a trusted proxy (`STACK_UI_TRUSTED_PROXIES`) says the browser came in over
  https (`X-Forwarded-Proto`); on a direct plain-http connection it stays as it was.
- New password hashes use 600 000 PBKDF2 iterations (was 300 000, below today's recommendation). A hash made
  earlier keeps working with its own count; sign-in takes correspondingly longer on a small board.
- `client/stack-ui-client.py` warns once when `STACK_UI_URL` is plain http to another machine: the API token and
  the stack files would cross the network unencrypted.
- The image is built from pinned parts: the base image by digest, the Python packages by hash
  (`requirements.txt`, `pip --require-hashes`), and the release workflow's actions by commit; the release image
  carries a build provenance and an SBOM. Dependabot proposes updates to all three weekly.
- `tools/install-pi.py` installs (or updates) Stack UI on a Podman host over SSH from Windows PowerShell, macOS or
  Linux: socket, password secret (only a hash is sent), compose file, start; it changes nothing when a `stack-ui`
  container already runs. See the README.
- `compose.example.yaml` runs the page with all capabilities dropped except `CHOWN`, `DAC_OVERRIDE` and `FOWNER`
  (it writes into stack folders of other owners), a read-only root file system and tmpfs on `/tmp` and `/run/secrets` (a
  read-only root file system cannot mount a Podman secret without the latter). Tried with
  rootful Podman 5.7; with an existing install, add the lines to your copy of the compose file.

## 1.0.12

- *Check for updates*: a registry can no longer send the page somewhere else. The address it names for an anonymous
  pull token, and every redirect it answers with, must be https and either the registry's own host or a host that
  resolves only to public addresses; otherwise the check of that image fails with a message
  ("the registry named a token address that is not allowed"). Before, any URL was fetched (`file:`, plain http,
  localhost, the LAN, a cloud metadata address).

## 1.0.11

Security fixes from an audit.

- A connection can no longer hold a thread for good: a missing, negative or non-numeric `Content-Length` is refused
  at once (413), a connection silent for 30 seconds is dropped, and at most 64 connections are served at a time
  (the rest are closed), so a flood of open connections cannot use up the container's process limit.
- `stack.json` can only name plain files of the stack's own folder (`compose`, `env`): a path outside it makes the
  folder no stack. A link (symlink) in a stack's folder is never followed: it is not listed, read, or written
  over, and a save or restore that meets one stops before writing anything.
- The login throttle (5 wrong passwords, a few minutes) counts every attempt, also parallel ones. Behind a reverse
  proxy every client used to be the proxy's address; `STACK_UI_TRUSTED_PROXIES` names the proxies whose
  `X-Forwarded-For` is believed, so each client is counted on its own.

## 1.0.10

- *Check for updates*: an image built on the host (`localhost/…`, which Podman gives a digest too) or pinned to a
  digest shows *Built here* and is never asked of a registry; before, it was counted as a failed check ("The
  registry could not be asked for 1 stack").

## 1.0.9

- *Delete stack* can delete the stack's folder too: a tick box (on by default) in the dialog, which lists the
  folder's files and sizes first and warns when it holds subfolders (a compose file may mount one, e.g. `./data`,
  and that is the application's own data). Only a plain folder directly inside `STACKS_DIR` is removed, and only
  after the containers are gone. Data outside the folder stays.
- A stack without containers (a leftover folder) has *Delete stack folder…* too, which removes only the folder.

## 1.0.8

- The running version shows on every page: small under the menu (`v1.0.8`, with a tooltip) and in the browser
  tab's title (`Stack UI 1.0.8`), not only on the Settings page.

## 1.0.7

- *Resources*: the current CPU and memory of every running container and of the host, on a new page (largest
  memory use first), as a *Memory* column and a host line on the Stacks list, and on a stack's Summary tab.
  CPU is a share of the whole host, computed from two readings (Podman's own figure is an average since the
  container started). A container with no memory limit shows `no limit`.
- *Settings*: how often the numbers refresh (off, 5, 10, 30, 60 seconds; default 10; paused while the tab is hidden).
- API: `GET /api/resources`.

## 1.0.6

- Healthchecks: a container's `healthy` / `unhealthy` / `starting` state shows next to it (Stacks list, a stack's
  page, Containers), and the Stacks list has an *Unhealthy* filter.
- *Differs* says what differs: its tooltip lists each changed file with the lines added and removed since the last
  deploy (`/api/drift` gives `{state, changes}` per stack).
- Before the first *Check for updates* the Image column of a deployed stack says *Not checked* instead of `—`.

## 1.0.5

- *Files* tab: *Load from files…* reads a new compose and/or env file from your computer into the editors
  (whatever their names) and shows the difference to the stack's folder before anything is saved.
- A running stack's folder stays what runs: on the page every save, history restore and file delete of a deployed
  stack is followed by an update (without a pull); the plain *Save* is gone for it. A stack that is not deployed
  is only saved, as before. When the folder already differed from the last deploy, the dialog says so. The API
  and the client are unchanged.

## 1.0.4

- *Check for updates* no longer marks an image as newer forever when only its multi-arch index changed (another
  platform's image, attestations) while this host's image stays the same (e.g. `redis:8-alpine` on arm64): when
  the tag's digest is not a local one, the index is read once and its manifest for the host's platform counts.

## 1.0.3

- `STACK_UI_HIDE_UNDEPLOYED=true`: the Stacks page lists only the stacks that have containers (running or
  stopped). Folders without containers stay in `STACKS_DIR` and in the API; after *Add stack* the new stack's own
  page opens, where it is created.
- *Add stack*: the compose file, the env file (optional) and the other files are chosen in separate fields.

## 1.0.2

- Client: a folder with a `stack.json` can be deployed (the manifest itself is no longer sent as a file, which Stack UI refused).

## 1.0.1

- A stack runs without a pod by default, like Docker Compose: `userns_mode` (e.g. `keep-id`) failed with
  "--userns and --pod cannot be set together". A compose file can still ask for a pod with
  `x-podman: {in_pod: true}`. A stack started in a pod before keeps its old pod (`pod_<name>`) until it is
  removed by hand (`podman pod rm pod_<name>`); the next update recreates the containers without it.

## 1.0.0

First release: split out of a private home-server repository; Podman only.

- A stack's other files (scripts, JSON/YAML configs, dashboards it mounts) are saved, checked, versioned,
  compared with the last deploy and deletable; the client sends them.
