# Plan — telemetry for feedback, debug and datalogging

Status: **planned 2026-09-02, nothing built.** Three slices, three commits, each
one a `git revert` from the last. Matthew asked for all three as separate
slices, and for the plan before any code.

Where it sits in the order agreed the same evening (worklog 2026-09-02):
slices 1 and 2 need no fleet reload and go **before the bring-up evening**,
because they are what makes that evening readable — every defer, catch-up and
restart lands on the page and on disk instead of in a scrollback. Slice 3
changes `player.js` and goes **after** the bring-up, riding with the next
reload, so the reload already owed carries only the four harness-proven player
fixes and nothing muddles attribution if one of them misbehaves. If saving a
reload cycle matters more, slice 3 can go first with the same harness
treatment; that is the one sequencing call left open.

## What exists today (surveyed 2026-09-02)

| surface | what it says | what it forgets |
|---|---|---|
| control toast (`Conductor.toast`, 29 call sites) | one line, for 3.5 s | everything, 3.5 s later |
| control NODES table + pills | the current numbers; ⬇ % / decoding / ready / ♪ | any history; how many restarts; why |
| player page | dot + status line; activity bar (idle / downloading / decoding / arming / playing); countdown; offset / rtt / skew | that it was deferred, and why it is silent |
| conductor log (`logging.basicConfig`, stdout only) | joins, leaves, play, catch-up success, boost changes, drift banked / refused, build mismatch | dies with the console window; none of it reaches the page |
| captures (scratch observers on `/ws/control`) | per-node mean / sd / crossings, spread, restarts | gone with the session; not comparable a month later |

Silent today, and each has cost a session at some point: a catch-up that gives
up (`_catchup` just returns at its deadline), a source restart and its cause
(the conductor sees a non-null `state` and nothing else), a mesh pair reaped
from `mesh_seen`, an AudioContext that suspended because the phone slept, a
page that went hidden.

## Two rules

1. **Telemetry must not perturb the measurement.** The phone and the tablets
   keep roughly 5–15 % of their ping samples; anything that adds node→conductor
   traffic on those radios changes the thing being observed. Everything below
   rides on messages that already flow, plus rare events (a suspend, a
   visibility change, a restart). No new periodic traffic from any node.
2. **A diagnostic must never be the reason nobody can play.** The rule the
   build stamp already obeys. A full disk, a closed file, a hostile payload:
   the trace disables itself and logs once, the event ring is bounded, and
   every new field from a node is clamped like the client data it is.

## Slice 1 — events: one helper, a bounded ring, an EVENTS card

*Feedback and debug. Conductor + control page. No reload; the running
conductor needs a restart to pick it up.*

**The helper.** `Conductor.event(kind, text, *, node=None, level="info",
toast=False, **fields)` does four things: logs the line at `level`, appends
`{t, wall, kind, level, node, name, text, ...fields}` to a
`deque(maxlen=EVENT_RING)` (300), pushes `{"type": "event", ...}` to every
control socket, and toasts if asked. `toast(text)` becomes
`event("toast", text, toast=True)` — **all 29 call sites unchanged**, so the
slice is behaviour-preserving by construction, and the card fills with what
the page already showed for 3.5 s at a time.

**History on connect, not in the snapshot.** `handle_control_ws` sends the
snapshot and then `{"type": "events", "items": [...ring...]}` once. The ring
does *not* ride the 1 Hz snapshot — that would be 300 rows a second to every
open page for no reason. A page opened late still sees the evening.

**What becomes an event**, beyond the toasts:

| where | today | event |
|---|---|---|
| `handle_player_ws` join / leave | log | `join` (a fresh page and a reconnect told apart by `hello.build` and a known clientId), `leave` |
| `note_build` | warning | `stale-build`, once per hello |
| `_transport_play`, the started list | log | `play` with the node list, seek and lead |
| `_catchup` success | log | `catchup` with the node and how long it waited |
| `_catchup` deadline | **silent** | `catchup-timeout`, warning, **toasted** — a speaker that will not play this track deserves a line on the page |
| `state` non-null | nothing | `start` per node, plus a per-node `restarts` counter for the current playback, reset on track change, in `stats()` and in the err cell's tooltip |
| `startRefused`, `loadError`, straggler cut, arming | toast / log | events carrying their numbers |
| `_ping_loop` boost change | log | `cadence`, level `debug` |
| `remember_skew` banked / refused | log | `drift-banked` / `drift-refused`, with the numbers |
| mesh: first sample for a pair; pair reaped from `mesh_seen` | **silent** | `mesh-up` / `mesh-lost`, level `debug` |
| pause / resume / seek / stop / next / queue edits / rescan | some toast | events |

`debug` rows render dim; nothing is filtered yet. A node filter and a level
toggle are a later cosmetic if the card turns out noisy.

**The card.** `EVENTS` between NODES and SPECTRUM in `control.html`: newest
first, `HH:MM:SS · node · text`, kind → class for colour (warning amber,
restart red, debug dim), a page-local clear button, capped at the ring size.
Rendered from the page's own array like `specLatest`, not from the snapshot.

**Verified by:** `tests/test_events.py` — the ring is bounded; a control
socket gets history, then live events; `toast()` still produces a toast *and*
an event (a count-before / count-after guard on the deferral flow that
`test_start_ready` already drives); the catch-up deadline now produces a
warning event. Control rendering against a stub DOM as before: order, cap,
escaping through `esc()`, classes. End-to-end on a throwaway conductor on
:8999, never :8927.

Size: ~120 lines conductor, ~60 control, tests. One new constant.

## Slice 2 — trace: a JSONL sidecar and a report tool

*Datalogging. Conductor only. No reload.*

**On by default, one flag off.** The whole point of a trace that outlives the
process is that it exists on the evening nobody planned to measure. So:
`logs/trace-YYYYMMDD-HHMMSS.jsonl` under the working directory (git-ignored,
next to `syncplay_state.json`), one file per conductor start, `--no-trace` to
disable, `--trace-dir` to move it. Wired in `__main__` / `build_app`, never in
`Conductor.__init__`, so no test writes a file unless it asks to.

**Lines.** Every line carries `t` (conductor seconds — the same clock as every
number in the snapshot) and `wall` (`HH:MM:SS.mmm`, for humans and for lining
up against what Matthew heard). Kinds:

- `start` — header, once: wall time, serving build, music dir, and the
  conductor constants that matter (`PLAY_LEAD`, `CATCHUP_WAIT_S`,
  `MIN_JOIN_SAMPLES`). The player-side servo constants arrive with slice 3's
  `hello`.
- `event` — every slice 1 event, all fields.
- `steer` — per `steerAck`: node, `errMs`, `rate`, `runS`, `offsetMs`,
  `trustMs`, `skewPpm`, `nUsed`, `lastRttMs`. The TODO's "~10-line JSONL
  sidecar per steerAck", exactly.
- `node` — every 10 s per connected node: the `stats()` dict as it is.
- `mesh` — every 10 s: the `_mesh_snapshot()` rows. Endpoint costs can then
  be fitted offline instead of from a screenshot.
- `sample` — **opt-in tier** (`--trace-samples`): raw `t0, c1, c2, t3` per
  pong, star and mesh, hooked at the two `model.add(PingSample(...))` sites.
  About four lines a second per node. What it unlocks: **replaying real
  samples through a different `filter_best`** — the parked re-tune stops
  needing a live experiment and becomes an offline argument in ± ms, which is
  how that item said it wanted to be argued.

Rate: five nodes ≈ 3 MB/hour without samples, ≈ 12 MB/hour with. Pruning is
manual; the file name says when.

**Writer.** Handlers append to a list; one task drains it to disk every 2 s
with a single write and flush. Any `OSError` logs once and turns the trace off
for the rest of the run. Nothing in the message path ever awaits the disk.

**`tools/trace_report.py`** — the tables built by hand this fortnight, from
any trace: per node `n · mean · sd · min · max · zero-crossings` of `err`;
fleet spread of the per-node means and its metres of air; restarts per node
(with cause once slice 3 exists); defers and catch-ups with seconds waited;
sample survival; audio-clock ppm and credibility from the `node` lines; worst
and best mesh closure per pair; the warnings timeline. `--since` / `--until`
on wall time, `--node`, and `--csv` to dump the `steer` lines for a
spreadsheet. Any evening becomes comparable with any other — the question the
tablet thread could never answer.

**Verified by:** `tests/test_trace.py` — header first; each kind serialises;
the writer survives a closed file and a full-disk `OSError` without touching
playback (a play still goes out); `--no-trace` writes nothing; the ten-second
sampler runs and stops with the app. The report tool runs on a synthetic trace
with planted values — three nodes with known means and sds and one planted
restart — and must print the planted numbers (the `plan_nudges` pattern). Then
the real one: the trace from the bring-up evening.

Size: ~150 lines conductor + `__main__`, ~200 tool, tests.

## Slice 3 — the node says what it is, and what it did

*Feedback on the device in your hand; debug of transitions. `player.js` +
conductor + control. Needs a fleet reload — after the bring-up, riding with
the next reload. Old pages ignore every new message type, so a half-reloaded
fleet is safe.*

- **Device facts in `hello`.** `sampleRate`, `baseLatency`, `outputLatency`
  (`null` where the browser has none) — `ctx` is created on JOIN before
  `connect()` runs, so they ride the message that already goes. Plus the
  player's own servo constants (`REANCHOR_S`, `SLEW_LIMIT_S`,
  `SLEW_PATIENCE_S`, `MAX_RATE_TRIM`, `STEER_HORIZON_S`), so a trace says
  which servo it was watching. Control shows rate and latency in the
  node-name tooltip; the trace gets them on the `join` event. The TODO's first
  dashboard row, delivered.
- **A cause on every start.** `_send_play` gains `why` (`play`, `resume`,
  `seek`, `next`, `auto`, `catchup`), which the player stores and echoes in
  `state` as `cause`. The two re-anchor paths in `onSteer` report `cause:
  "reanchor"` with `reason: "fault" | "patience"` and the `errMs` that
  triggered it; the late-join branch of `startBuffer` adds how late. The
  conductor's `state` handler turns that into the event and the counter that
  slice 1 only knew as "a source started". "The tablet restarted twice
  tonight, both patience, at +31 and +27 ms" becomes a line you can read.
- **Two rare messages.** `ctxState` on `ctx.onstatechange` and `visibility`
  from the `visibilitychange` handler that already exists (it resumes the
  context). The "phone slept" signal we currently only infer from a hard
  re-anchor on wake. Allow-listed strings, clamped.
- **`notice` to a node.** Conductor → one node: `{type: "notice", text,
  ttlS}`. Sent on defer ("clock still settling — joining automatically"), on
  the catch-up play ("joining now"), on catch-up timeout ("could not join this
  track"), and by calibration ("measuring — keep still"). `renderActivity`
  gets a `notice` input that wins while fresh; `textContent`, never HTML. The
  silent phone finally says why it is silent.
- **`mapMs` on `steerAck`** — `performanceTime − contextTime × 1000` from the
  `getOutputTimestamp` pair, the mapping constant `perfToCtx` is built on. One
  number, already computed; a stepping output-timestamp mapping becomes
  visible in the trace. The one loose end of the closed tablet thread,
  answered for free if it ever matters.
- Optional, same slice or dropped: a collapsed `<details>` on the player page
  holding the last ~40 steer / restart / notice lines — debug for the device
  you are holding when it misbehaves at the far end of the house.

**Verified by:** `node --check`; `tools/reanchor_harness.js` extended — it
already drives `onSteer` through fault, patience and swinging, so it asserts
the `cause` / `reason` each path reports and that the swinging node reports
none; a stub-DOM check of `renderActivity` with a fresh and an expired notice;
`tests/test_node_reports.py` for `ctxState` / `visibility` / `state.cause`
with hostile payloads; end-to-end on :8999 with a `?id=x` dev-node tab — the
notice appears on defer, the tooltip shows the sample rate, the trace carries
the causes.

Size: ~90 lines player, ~80 conductor, ~30 control, harness + tests.

## Commit ladder

1. **`feat(control): an EVENTS card — what the toasts used to forget`**
   (slice 1). Ships value alone: the catch-up deadline gets a voice, restarts
   get counted.
2. **`feat(conductor): a trace that outlives the process, and a report for
   it`** (slice 2). Ships value alone: the report is the capture script,
   promoted to a tool.
3. **`feat(player): say what this device is, and why it restarted`**
   (slice 3). After the bring-up; rides with the next reload.

Each conductor step needs a restart of the live conductor — Matthew's action.
It drops the fleet's sockets for a second; the pages stay and the nodes
reconnect on their own.

## Risks

- **Measurement perturbation** — none by design (rule 1); the one change to a
  steady message is a few bytes on `steerAck`.
- **Disk** — ~3 MB/h; a forgotten conductor left up for a week is ~500 MB.
  Dated file names and a manual prune; a size cap can come later if it bites.
- **Card noise** — bounded ring, dim debug rows; a filter is cheap if needed.
- **Slice 3 touches `startSource` / `onSteer`** — the exact code the harness
  covers; run old-vs-new as for the two fixes before it. Reporting only: the
  servo maths does not change.
- **Hostile client data** — every new field clamped or allow-listed, as the
  spectrum and mic handlers already do.

## Not in this plan

A sparkline (parked; it becomes a read of the trace), a log viewer beyond the
card, any metrics server or external service, changes to ping or steer
cadence, per-node log download, HTTPS. The two-control-sockets mystery is not
chased here — but slice 1 makes it decidable: two observers of one ring cannot
disagree without it being a bug.

## Reuse points

- `Conductor.toast`, `_broadcast_control`, `handle_control_ws` — the event
  fan-out and history-on-connect.
- `Node.stats()`, `_mesh_snapshot()` — the periodic trace lines, verbatim.
- `_send_play`, `_catchup`, `_transport_play` — the cause and the notices.
- `player.js`: `hello` in `ws.onopen`; `startSource` / `onSteer`; the existing
  `visibilitychange` handler; `renderActivity`.
- `tools/reanchor_harness.js` and the `cond` fixture in
  `tests/test_build_stamp.py` — the verification patterns.
