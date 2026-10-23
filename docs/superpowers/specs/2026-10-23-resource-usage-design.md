# Resource usage: design

Date: 2026-10-23 · Target version: 1.0.7

## Goal

On a 4 GB Raspberry Pi 4 running about a dozen stacks with `mem_limit` on every service, see at a glance which
containers use how much memory and CPU, and which are close to their memory limit (out-of-memory kill), without a
new dependency.

Success: the Stacks list, a stack's Summary tab and a new Resources page show current CPU and memory per container
and for the host, refreshed at an interval the user picks, and a container near its limit stands out.

## What Podman gives (measured on a Raspberry Pi 4, Podman 5.7, 10 containers)

- `GET /libpod/containers/stats?stream=false` answers for every running container in one call, in 0.12–0.28 s.
  Each entry has 17 keys, among them `Name`, `CPU`, `CPUNano`, `SystemNano`, `MemUsage`, `MemLimit`, `PIDs`.
- `CPU` is the **average since the container started** (two calls seconds apart differ in the fourth decimal), so it
  cannot show the current load. The current load comes from two samples:
  `(CPUNano₂ − CPUNano₁) / (SystemNano₂ − SystemNano₁)`.
- `MemLimit` equals the host's total memory (3 962 322 944 bytes there) when the container has no limit.
- One container was already at 91 % of its limit (152 / 168 MB): the case this page is for.

## Approach

The server computes, the page only shows. One endpoint, `GET /api/resources`, reads the stats once per call,
keeps the previous sample per container in memory and returns current values. Rejected: the page computing CPU
deltas (every open tab keeps its own sample; the first value only after the second refresh) and a streaming
stats thread (runs when nobody looks).

## Server

`podman_api.py`

- `container_stats() -> list[dict]`: `GET /containers/stats?stream=false` (`all` not needed: stopped containers
  have no stats); the `Stats` list of the answer.

`server.py`

- `resources() -> dict`, behind `GET /api/resources`:

  ```json
  {"host": {"memTotal": 3962322944, "memUsed": 3100000000, "cpu": 12.5, "cpus": 4},
   "containers": {"app": {"cpu": 3.1, "mem": 430325760, "limit": 2684354560, "pids": 23}},
   "error": ""}
  ```

  - **Container CPU** in percent of the whole host (all cores together are 100 %), from the difference to the
    previous sample of the same container: `ΔCPUNano / ΔSystemNano / cpus × 100` (`CPUNano` is the CPU time the
    container used, `SystemNano` the wall-clock time of the sample, both in nanoseconds). Rounded to one decimal,
    never below 0. A container without a previous sample, or whose `CPUNano` went down (it was restarted), is
    measured against the second sample taken for it (see *First call*).
  - **First call**, or a call where a running container has no usable previous sample: a second stats call 1 s
    later, so the page has numbers at once. Otherwise the stored sample is used, however old.
  - **Limit**: `MemLimit`, or `null` when it is 0 or at least the host's total memory (no limit set).
  - **Host memory** from `/proc/meminfo`: `memTotal` = `MemTotal`, `memUsed` = `MemTotal − MemAvailable` (bytes).
    Inside the container `/proc/meminfo` and `/proc/stat` show the host's values.
  - **Host CPU** from the first line of `/proc/stat`: busy share of the difference to the previous reading
    (`idle` + `iowait` are idle), in percent, one decimal; `cpus` from the number of `cpuN` lines. The first call
    takes its second reading together with the second stats call.
  - The previous samples (container and host) live in module variables guarded by a lock; a container gone from
    the stats is dropped from them.
  - **Errors**: a Podman error or an unreadable `/proc` file does not fail the call: `error` says what failed and
    the parts that could be read are returned (`containers: {}` or `host: null`).
- Login and token rules are those of the other `GET /api/...` endpoints (the API token opens it).

## Page

Shared

- `RES` holds the last `/api/resources` answer; `loadResources()` fetches it and redraws the visible view.
- Refresh: a timer with the interval from Settings; it does nothing while `document.hidden` and fetches at once
  when the tab becomes visible again. Also fetched with every `load()` (the ↻ button).
- Colours of a memory share (usage ÷ limit): from 80 % `t-warn`, from 95 % `t-bad`, else `t-ok`; no limit: no
  colour, usage only.
- Sizes as MB below 1 GB, else GB with one decimal.

Stacks list

- Above the table, a line: `RAM 3.1 / 3.7 GB · CPU 12 %` (the RAM part coloured by the same thresholds).
- A **Memory** column after *Containers*: the sum of the stack's running containers' usage over the sum of their
  limits (`1.9 / 2.7 GB`, coloured); when one of them has no limit, only the usage. The tooltip lists each
  container: `app: 430 MB / 2.5 GB · CPU 3.1 %`. A stack without running containers: `—`.

A stack's Summary tab

- The containers table gets **CPU** and **Memory** columns (`430 MB / 2.5 GB`, coloured; `—` when not running).

Resources page (new, in the left menu after *Containers*, route `#/resources`)

- On top: host RAM (bar, used / total) and CPU %.
- A table of the running containers, sorted by memory use, largest first: *Container*, *Stack* (a link to its
  page; empty outside a stack), *CPU %*, *Memory* (a bar coloured by its share, `MB` / `GB` text), *Limit*
  (`no limit` when none), *PIDs*.
- `error`: a banner above the table; what could be read is still shown.

Settings

- *Resource refresh*: `Off`, `5 s`, `10 s` (default), `30 s`, `60 s`. Kept in `localStorage` (per browser; reads
  and writes in try/catch, default when unavailable). `Off`: only the ↻ button and page loads fetch.

## Tests

- `tests/mock_libpod.py`: `GET /containers/stats` answering `{"Stats": [...]}` from a per-container list the test
  sets (`CPUNano`, `SystemNano`, `MemUsage`, `MemLimit`, `PIDs`).
- `resources()`:
  - CPU from two samples (e.g. 0.5 s of CPU over 1 s on 4 cores → 12.5 %);
  - the first call takes a second sample (the 1 s wait patched out);
  - a restarted container (`CPUNano` went down) does not give a negative value;
  - `limit` is `null` for 0 and for a limit ≥ host memory;
  - `/proc/meminfo` and `/proc/stat` parsing from fixture text (the paths are module constants the test points at
    temp files);
  - a stats error gives `error` and still the host part.
- `podman_api.container_stats()` against the mock.
- Browser check (headless Chrome, mocks): the line above the Stacks list, the Memory column and its colours, the
  Summary tab columns, the Resources page order, the Settings choice stopping the timer.

## Out of scope

Network and disk I/O, history or charts over time, alerts (step 4 of the roadmap: notifications), stopped
containers' numbers.
