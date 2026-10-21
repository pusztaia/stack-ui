# Stack UI

A small web page for the compose stacks of a [Podman](https://podman.io) host. Every folder with a compose file is a
stack; Stack UI runs it with [podman-compose](https://github.com/containers/podman-compose) through the Podman socket.
One container, Python standard library + PyYAML, no database.

- **Stacks**: list with state and containers; edit the compose and env file (every save keeps the previous
  version in `.history/`, a save shows its diff first); create, update (pull + recreate), start, stop, delete;
  compare a stack with what was deployed last; upload a new stack; update several stacks at once.
- **A stack's other files**: the scripts, JSON/YAML configs and dashboards its compose file mounts with
  `./name` are saved with it, checked (syntax, no secret literals), versioned in `.history/`, compared with the
  last deploy, viewable and deletable on the page.
- **Image updates**: asks the registries (anonymous HEAD requests) whether a newer image exists for each running
  tag and marks the stacks.
- **Containers**: every container, its stack, state and published ports (as links); start, stop, restart, logs,
  details (the inspect, secret-looking values masked), remove (only outside a stack).
- **Images**: sizes and users; pull, tag, remove (only unused), clean up the untagged leftovers of pulls.
- **Logs**, **Networks**, **Volumes** and **Secrets** (Podman secrets: a value goes in and is never shown again).
- An **API token** for scripts, and `client/stack-ui-client.py` that deploys a folder through the API.

## How stacks are found

A stack is a direct subfolder of `STACKS_DIR` named like a podman-compose project (lowercase letters, digits, `-`
and `_`) that holds a compose file:

1. the one `stack.json` names: `{"compose": "my.yaml", "env": "my.env"}` (`env` may be `null`);
2. else the first of `compose.yaml`, `compose.yml`, `docker-compose.yml`, `docker-compose.yaml`;
3. else its only `*.yaml` / `*.yml` (two or more unknown YAML files and no `stack.json`: not a stack).

The env file is `stack.json`'s, else `.env`, else the only `*.env`, else none.

A stack runs **without a pod**, like Docker Compose (so `userns_mode: keep-id` and the like work), unless its
compose file says otherwise: `x-podman: {in_pod: true}` (or a pod name).

A stack's **other files** are the rest of its folder with one of the suffixes `.yaml .yml .env .sh .md .txt
.json` (flat: no subfolders; at most 512 KiB each). A save or an upload checks them: JSON and YAML must parse,
and a secret-looking key (`*TOKEN`, `*PASSWORD`, `*SECRET`, `*API_KEY`, …) must not hold a literal value —
references are fine: `${VAR}`, `$__file{/run/secrets/x}`, `$__env{VAR}`, `/run/secrets/x`. `.sh` files are made
executable. Hidden folders and the names in
`STACK_UI_IGNORE` are skipped. A stack runs as the podman-compose project of its folder's name, from the folder
(relative paths in the compose file resolve there).

## Install

On the Podman host:

```bash
# 1. the Podman socket (rootful)
sudo systemctl enable --now podman.socket
# 2. the page's password (at least 10 characters): only its hash goes into a Podman secret
read -rs -p 'Stack UI password: ' PW; echo
printf '%s\n' "$PW" | sudo podman run --rm -i ghcr.io/pusztaia/stack-ui:1 --hash-password | sudo podman secret create stack-ui-password -
unset PW
# 3. the example compose file
curl -LO https://raw.githubusercontent.com/pusztaia/stack-ui/main/compose.example.yaml
# 4. where your stacks are, and how you open the page
export STACKS_DIR=/srv/stacks STACK_UI_ALLOWED_HOSTS=127.0.0.1,localhost
# 5. start it
sudo -E podman-compose -f compose.example.yaml up -d
```

Open `http://127.0.0.1:8770` on that host (or through an SSH tunnel). To open it on the LAN, change the port line
to your host's address (e.g. `192.168.1.10:8770:8770`) and add that address to `STACK_UI_ALLOWED_HOSTS`; better,
put it behind a reverse proxy with HTTPS.

**Rootless:** the same without `sudo`: `systemctl --user enable --now podman.socket`, and in the compose file use
the commented socket line (`/run/user/<your UID>/podman/podman.sock`).

Updates: `podman-compose -f compose.example.yaml pull && podman-compose -f compose.example.yaml up -d`. The tag
`:1` follows every 1.x release; pin `:1.0.0` for a fixed version.

## Rootful or rootless?

| | Rootful | Rootless |
|---|---|---|
| Who reaches the socket gets | root on the host | your user's rights |
| Ports below 1024 | yes | needs `net.ipv4.ip_unprivileged_port_start` |
| `privileged`, devices, `network_mode: host` | full | limited |
| Files of bind mounts on the host | the container's UIDs | shifted UIDs (subuid) |
| Networking | native | slower (pasta/slirp) |

Prefer **rootless** where your stacks allow it. Use **rootful** when a stack needs `privileged` containers,
devices or ports below 1024 — and then remember that this page is as powerful as root on that host.

## Security

- Whoever controls the page controls your containers: with a rootful socket that is root on the host.
- Off loopback the page needs a password (its PBKDF2 hash in the `stack-ui-password` Podman secret), a session
  cookie (HttpOnly, SameSite=Strict, 12 h), the page's token, and an allowed Host header
  (`STACK_UI_ALLOWED_HOSTS`). Five wrong passwords lock an address out for a few minutes.
- The port binds to `127.0.0.1` by default. Use HTTPS (a reverse proxy) when you open it on a network.
- Env files must not hold secrets: a save or an upload with a non-empty `*TOKEN` / `*PASSWORD` / `*SECRET` /
  `*API_KEY` variable is refused. Give a stack a secret as a Podman secret: in its compose file
  `secrets: {my_secret: {external: true}}` (and under the service), created on the *Secrets* page.
- The page's own container (`STACK_UI_SELF`) and its compose project, and `STACK_UI_PROTECTED`, are never stopped,
  restarted, redeployed or removed from the page; the `stack-ui-password` secret (and `STACK_UI_PROTECTED_SECRETS`)
  is never removed.
- Running as root (rootful), files the page writes into `STACKS_DIR` get the owner of their folder, so the stacks
  stay editable by their owner.

## Configuration

| Variable | Default | |
|---|---|---|
| `STACKS_DIR` | — (required) | the stacks folder; mount it at the same path in the container |
| `STACK_UI_ALLOWED_HOSTS` | `127.0.0.1,localhost` | the host names / addresses the page answers to |
| `STACK_UI_PASSWORD_FILE` | `/run/secrets/stack-ui-password` | the password hash (`--hash-password`) |
| `STACK_UI_SELF` | the container's `HOSTNAME` | this page's container name (protected) |
| `STACK_UI_PROTECTED` | — | more containers or compose projects that are never stopped or removed |
| `STACK_UI_PROTECTED_NETWORKS` | `podman` | networks that are never removed |
| `STACK_UI_PROTECTED_SECRETS` | — | Podman secrets that are never removed (besides `stack-ui-password`) |
| `STACK_UI_IGNORE` | — | folders of `STACKS_DIR` that are not stacks |
| `STACK_UI_DATA` | `/data` | where the settings (the API token's hash) are kept |
| `PODMAN_SOCKET` | `/run/podman/podman.sock` | the Podman socket inside the container |
| `STACK_UI_BIND`, `STACK_UI_PORT` | `0.0.0.0`, `8770` | where the page listens in the container |

## Deploy from a script

Generate an API token in *Settings*, then from any machine:

```bash
export STACK_UI_URL=http://my-host:8770 STACK_UI_API_TOKEN=...
python client/stack-ui-client.py deploy ./my-app --dry-run   # the differences and the plan
python client/stack-ui-client.py deploy ./my-app             # save the files there, then create or update
```

It sends the stack's other files too, and lists the files that exist only on the host (delete them on the page
if they are not needed).

The token opens the API except the settings, never the page.

## Development

```bash
pip install pyyaml
python -m unittest discover -s tests -v
```

The tests use a mock libpod API and a fake podman-compose, never a real Podman.

## License

MIT
