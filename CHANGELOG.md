# Changelog

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
