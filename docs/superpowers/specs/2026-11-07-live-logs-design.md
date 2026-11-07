# Live logs: design

Date: 2026-11-07 · Target version: 1.0.19

## Goal

On the Logs page, see a container's new log lines the moment they are written, without the page reloading the whole
tail every few seconds, and keep reading comfortably while they arrive: stay at the bottom, stop when scrolling up,
pause, find and spot what matters, and save the log to a file.

Success: with *Follow* on, a line a container prints shows up on the page within a second; the scroll position does
not jump; a pause freezes the view without losing lines; the visible log can be saved as a text file. No new
dependency, no shell and no write access to containers (read-only, like the rest of the Logs page).

## Decisions made with the user

- **Transport:** a real stream from the server (option A of three: stream, incremental polling, WebSocket).
  WebSocket is not needed for logs (one direction only) and stays with the parked terminal.
- **Wanted:** lines at once without reloading; scroll-follow, pause, highlighting; download to a file.
  **Not wanted now:** one combined log of all containers of a stack.
- **The terminal is parked.** A shell over plain http on the LAN is too big a security risk. It returns only with a
  secure channel (https or a tunnel); nothing in this design prepares it except that the stream code is reusable.

## What the Page and Podman have today

- `GET /api/containers/<name>/logs?tail=200|500|2000` returns `{container, lines}` (timestamped lines) through
  `podman_api.logs()`: one libpod call, whole answer in memory, `demux()` strips Podman's 8-byte frame headers.
- The page's *Every 5 s* tick re-fetches and redraws the whole tail.
- `Handler` speaks HTTP/1.0 (`BaseHTTPRequestHandler` default), one thread per connection, at most
  `MAX_CONNECTIONS` (64), a 30 s socket timeout, and sends security headers in `send()`.
- libpod's `logs?follow=true` keeps the connection open and sends new frames as they are written; the answer is
  chunked, framed (8-byte header per chunk) unless the container has a TTY, when it is raw.

## Approach

A new read-only endpoint streams the log as newline-delimited JSON. Because the server answers HTTP/1.0, a response
without `Content-Length` simply lasts until the connection closes: no chunked encoding is needed. The page reads it
with `fetch` and a stream reader (not `EventSource`: it cannot send the `X-UI-Token` header, and the token must not
go into a URL).

## Server

### `podman_api.stream_logs(cid, tail, since=None)`

A generator. Opens its own connection (timeout 60 s; a read timeout means "quiet", not an error), requests
`/containers/<id>/logs` with `follow=true&stdout=true&stderr=true&timestamps=true&tail=<n>` (and `since` when
given), reads with `read1(65536)` and yields complete lines as `str`. Its pieces:

- `LogFrames`, an incremental demultiplexer: it keeps unread bytes between reads, so a frame header or body that is
  split across two reads is joined correctly (the existing `demux()` needs the whole answer). Whether the answer is
  framed shows in its first byte (0, 1 or 2 is a frame header, as `demux()` decides); without frames (a TTY
  container) the bytes are text as they are. No inspect call is needed.
- Line assembly: bytes are split at `\n`; an incomplete last line waits for the next chunk; invalid UTF-8 is
  replaced.
- The connection is closed when the generator is closed (the caller stops iterating).

### `GET /api/containers/<name>/logs/stream?tail=N&since=T`

Handled in `Handler.api()` next to the other container routes, after the same sign-in and token checks (a page
session with `X-UI-Token`, or the API token, so `curl -N` works from a script). `tail` is one of `LOG_TAILS`
(default 200); `since` is optional, a timestamp from a previous line (reconnecting).

Before the first byte every problem is an ordinary JSON error as now (unknown container `404`, bad `tail` `400`,
Podman unreachable `502`, too many streams `429`). Then:

- status 200, `Content-Type: application/x-ndjson; charset=utf-8`, the usual security headers, `Cache-Control:
  no-store`, `X-Accel-Buffering: no` (a reverse proxy must not buffer it);
- one JSON object per line: `{"l": "<one log line, timestamp first>"}`; `{"hb": 1}` every 15 s when nothing was
  written; at the end `{"end": "container stopped"}`, or `{"error": "<message>"}` if reading failed;
- a log line longer than `LOG_LINE_MAX` (8 KiB) is cut and ends in an ellipsis.

The handler writes and flushes each line (`wfile` is unbuffered). A write that fails (the client closed the tab)
ends the loop, closes the generator and so the Podman connection, and frees the slot. The libpod read timeout wakes
the loop for the heartbeat; it never raises to the user.

### Limits

- `MAX_LOG_STREAMS = 4` open streams at once (a `BoundedSemaphore`, acquired before the Podman call, released in a
  `finally`): the 64-connection budget and the page container's 192 MB stay safe. The fifth gets `429` with "close
  another log stream first".
- No idle limit on the stream itself: a quiet container's stream lives while the tab does. A stopped container ends
  it. A server stop (SIGTERM) ends all of them with the process.

## Page (Logs)

The toolbar keeps the container and *Lines* selectors and the filter box. *Every 5 s* is replaced by:

- **Follow** (on by default): opens the stream for the selected container. Changing the container or *Lines* closes
  the old stream and opens a new one. Leaving the Logs view, or switching Follow off, closes it. With Follow off the
  page behaves as now (one fetch, *Reload* button).
- **Pause**: freezes the view; lines keep arriving into the buffer and the button shows "+N new"; *Resume* redraws
  and jumps to the end.
- **Download**: saves the whole buffer as `<container>-<yyyymmdd-hhmmss>.log`, one log line per file line,
  whatever the filter box says. Made in the browser (`Blob`): no server call.
- **Clear**: empties the buffer (the stream goes on).

Behaviour:

- **Buffer:** at most `LOG_BUFFER` (5000) lines; older ones are dropped and the page says "older lines were
  dropped" once. Rendering is batched (at most ten redraws a second) and appends to the end instead of rebuilding
  when no filter is active.
- **Scroll:** at the bottom the view sticks to the end; scrolling up stops sticking (Follow stays on, lines keep
  arriving) and a *Jump to latest* button appears.
- **Highlight:** the filter's matches get `<mark>`; lines matching `\b(error|fatal|panic|exception|failed)\b` are red,
  `\bwarn(ing)?\b` yellow (class on the line, no colour-only meaning: the word is in the text). All text goes through
  `esc()`.
- **Reconnect:** when the stream ends (the container stopped or restarted) the page says so and retries with a
  growing wait (2, 4, 8, 10 s) using `since=<timestamp of the last line>`; lines already shown at that timestamp are
  skipped. While the container is down every retry returns at once with `end`.
- **Errors:** a JSON error before the stream (404, 429, 502) appears in the log area; a dropped connection reconnects.

## Tests

Server (`tests/`, with the mock libpod extended to stream: frames in chosen chunk sizes, new lines added while the
connection is open, a TTY mode, a close):

- `LogFrames`: a frame split in the header, in the body and across several chunks; two frames in one chunk; TTY raw
  text; a line split across chunks; invalid UTF-8.
- The endpoint: history tail first, then a line added to the mock arrives; `since` is passed to Podman; the heartbeat
  (interval patched short); `end` when the mock closes; `{"error"}` when Podman fails mid-stream; a line over the
  limit is cut.
- Limits: the fifth stream gets `429`; closing the client frees its slot (a new stream then works); an unknown
  container gives `404` before any stream; a bad `tail` gives `400`.
- Access: without a session `401`; a page session without the token `403`; the API token works.
- Existing `GET .../logs` is unchanged.

Page: `node --check` of the script (CI does this already); the page test asserts the new controls exist and
*Every 5 s* is gone; and a look in a real browser on an instance with the mock libpod (follow, pause, scroll up,
highlight, download, reconnect), desktop and mobile width.

## Docs and release

README (the Logs bullet), CHANGELOG (with the new route, `GET /api/containers/<name>/logs/stream`, as 1.0.7 named
`GET /api/resources`), version 1.0.19. No new environment variable.

## Out of scope

- A combined log of all containers of a stack (not wanted now).
- Downloading the whole log from the server (the browser buffer only; a later, separate step if needed).
- Any write path to a container, and the terminal (parked until a secure channel exists).
- Changing what the existing one-shot logs endpoint returns.
