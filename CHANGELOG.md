# Changelog

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
