# CLAUDE.md

Guidance for Claude Code in this repository: **Stack UI**, a web page that manages the podman-compose stacks of a
Podman host (the owner runs it on a Raspberry Pi 4 with rootful Podman). Python 3.9+, standard library and PyYAML
only; the page is one HTML file with inline CSS and JavaScript, no build step. `stacks/` (the Pi's stack modules)
has its own `stacks/CLAUDE.md` and is gitignored: never commit, push or publish anything from it.

## Working agreements

- The owner writes in Hungarian: answer in Hungarian. Everything in the repository (code, comments, docs, commit
  messages) stays English.
- Commit, push and tag only when the owner says so; they say each one separately. A version bump, a release, a
  change to the repository's GitHub settings and anything sent to GitHub (PR comments, closing PRs) also need their word.
- No `Co-Authored-By` (or any other Claude attribution) line in commit messages.
- Never test against the real Pi or a real Podman, not even in WSL: the tests use a mock libpod and a fake
  podman-compose. Commands for the Pi go to the owner to run.
- No secrets in compose or env files (the page refuses them). Do not bring up HTTPS / a reverse proxy for the page
  unless asked; the in-browser container terminal is parked; outgoing notifications were dropped.

## Layout

`stack_ui/`, lowest layer first; a module imports only from its own row or the rows above it:

| module | |
|---|---|
| `podman_api.py` | the libpod REST API over the socket |
| `preflight.py`, `health.py` | compose checks before a deploy; host health numbers |
| `stacks.py` | which folders are stacks, podman-compose (`run_compose`), the record of the last deploy (`STACK_UI_DATA/deployed/`), drift |
| `rollback.py`, `schedule.py`, `activity.py` | the rollback point (`STACK_UI_DATA/rollback/`); the daily check's state, the approved hashes (`schedule.json`); the Activity log and its watcher (`activity.jsonl`; `core.operation` writes every change) |
| `lifecycle.py` | deploy, start / stop / delete, roll back: everything around them |
| `core.py` | the shared settings, the one change lock (`operation()`, `Conflict`, `CURRENT`), protected stacks |
| `backups.py` | the backup of the data folder (download, or `STACK_UI_BACKUP_DIR`) |
| `auth.py`, `stackfiles.py`, `stackops.py`, `registry.py`, `usage.py`, `image_ops.py`, `podman_objects.py`, `autoupdate.py` | the parts behind the routes |
| `server.py` | HTTP: sign-in, sessions, the route table `ROUTES`, start-up |

`page.html` is the whole front end. `client/stack-ui-client.py` deploys from a script (API token). `tools/`:
`install-pi.py` (SSH installer), `release-notes.py` (a version's CHANGELOG section). `docs/superpowers/`: specs
and plans of the larger changes.

Rules the code relies on:

- **A value that changes while the page runs, or that a test sets, is read through its module** (`core.CURRENT`,
  `core.SOURCE`, `core.PROTECTED`, `auth.SETTINGS_FILE`, `registry.updates`, ...), never copied with
  `from .x import name`. `server.py` star-imports the parts only so the handler and the tests find the names.
- **Threat model:** a container that mounts its stack's folder can write everything in it, and the page acts as
  root. Nothing in a stack's folder is trusted as the page's own state: the deploy record, rollback points, hashes
  and settings live in `STACK_UI_DATA`. Files are read once, without following a link (`stacks.read_plain`).
- **Routes** are lines of `ROUTES` in `server.py`; `page_only=True` keeps the scripts' API token out (settings,
  schedule, backup, the automatic-update switches). `tests/test_routes.py` lists them.
- Every change of Podman state holds the one change lock (`core.operation`); a second one gets 409 at once.

## Tests

```bash
python -m unittest discover -s tests          # the whole suite, about 6 minutes; read the Ran / OK lines
python -m unittest tests.test_backup          # one module
```

- Run the whole suite after every change and again after a version bump, on Windows **and** on Linux (WSL: copy
  the checkout without `.git` and `stacks/` to a temp folder there and run `python3 -m unittest discover -s tests`).
  Windows skips the link and FIFO tests; Linux runs them. From Git Bash, call WSL through PowerShell
  (`wsl bash ...`): Git Bash rewrites `/mnt/...` paths.
- `tests/support.py`: `Base` gives each test its own stacks folder, data folder (`STACK_UI_DATA` and the settings
  file point into it, so no test can write the real `/data`), mock libpod and fake podman-compose.
  `fresh_server()` reloads every server part in order; use it instead of `importlib.reload`.
- Write the failing test first and watch it fail. In tests write files with `write_bytes` when line endings
  matter (`write_text` gives CRLF on Windows).
- The page: `tests/test_page.py` checks strings in it; check its script with `node --check`; look at a change in
  the browser with a mock demo (a small script that starts `server.Handler` on the mock libpod). A click on a
  download button in the browser pane saves a real file into the owner's Downloads folder: delete it afterwards.

## Release

1. `CHANGELOG.md`: the changes under `## Unreleased`, then that heading becomes `## X.Y.Z`.
2. `stack_ui/__init__.py`: `__version__ = 'X.Y.Z'`; run the whole suite again.
3. Commit, push `main`, then tag `vX.Y.Z` and push the tag (each on the owner's word).
4. The tag runs `.github/workflows/release.yml`: tests, the image for amd64 and arm64 on `ghcr.io/pusztaia/stack-ui`
   (tags `X.Y.Z`, `X.Y`, `X`, `latest`), then the GitHub release with the version's CHANGELOG section, marked latest.
   `test.yml` runs the tests on Python 3.9 and 3.14 and builds and starts the image on every push and pull request.

`main` has a ruleset: no force push, no deletion. Only `main` exists; branches of merged or closed work are deleted.
