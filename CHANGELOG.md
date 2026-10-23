# Changelog

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
