# Plan — the three shapes from the 2026-09-11 capture

Status: **slice 1 built 2026-09-11 (both halves, one commit); slices 3 and 2
not started.** Three slices, three commits,
each a `git revert` from the last. Matthew asked for the plan first; the
slices run afterwards, one per "continue".

Where it comes from: the evening WORKLOG entry for 2026-09-11 — ten hours of
`logs/trace-20260911-104631.jsonl` on four nodes. Steady state (every ack
past 15 s of a source) was sub-millisecond on three nodes and 5 ms on the
tablet, with not one ack over 25 ms; every excursion of the day lived in the
first 15 s after a start, and the three shapes below are all of them. None
of the slices changes what a steady node does. Slice 1 is instrumentation,
slice 2 is a servo rule (evidence-gated), slice 3 is event hygiene.

## What exists today (surveyed 2026-09-11)

| thing | where | what it says | what it can't say |
|---|---|---|---|
| `err` | `player.js: onSteer` — `posAt(targetCtx) - msg.posMs/1000`, + = ahead | how far the node's *bookkeeping* is from the conductor's target | which input moved: `nudgeMs`, the perf→ctx map, the anchors, or the target itself |
| steer trace line | `conductor.py: kind == "steerAck"` → `_trace("steer", …)` | `errMs`, `rate`, `runS`, the model (`offsetMs`, `skewPpm`, `trustMs`, `nUsed`, `lastRttMs`), `mapMs` | what the conductor *sent* (`t_ref`, `pos_ms`, `atNodeMs`) — it logs the model at ack time, not the target at send time |
| `nudge` / `volume` / `eq` commands | `_on_control_cmd` | a `config` message to the node; `nudge` persists via `_save_state` | nothing to the ring or the trace — `nudge` doesn't even toast |
| fault restart | `onSteer`: `Math.abs(errS) > REANCHOR_S` (0.2 s) | restart in place, reason `fault`, on **one** ack | whether the reading was real; a false one moves good audio 750 ms and the next ack moves it back (the phone, 11:12:56) |
| patience restart | `onSteer`: `slewSince` older than `SLEW_PATIENCE_S` (10 s) | a smaller error the servo lost to | — (it already waits; it is the model for slice 2) |
| cadence event | `_note_boost`: `round(boost*4) != round(was*4)` | the ping boost moved a quarter-step | a node sitting *on* a quarter-step boundary (the pc at 1.125) chatters across it every cycle — 100 of the day's 109 cadence events |

## Two rules (unchanged from the telemetry plan)

1. **Telemetry must not perturb the measurement.** Slice 1's node half adds
   a few numbers to a message that already exists (`steerAck`, every ~2 s),
   nothing new on the wire. The conductor half adds no traffic at all.
2. **A servo change is evidence-gated and harness-proven.** Slice 2 touches
   `onSteer`; it waits for a second mirror pair in a trace, runs old-vs-new in
   `tools/reanchor_harness.js` against the shipped function, and states its
   audible trade-off before it goes in.

## Slice 1 — a step names its input

*Built 2026-09-11.* One naming change from the plan: the sent triple's
third column is `sentLeadS` (target instant minus ack time, ~+0.3 s), not
an age — the target sits *ahead* of the ack. The MIRRORS line went in with
it. Run against the day's trace, the STEPS table already split the shapes:
the phone's and tablet's start transients are in the `-map` column (their
output-timestamp mapping moved 60–150 ms), the laptop/pc Shape A steps show
map 0 and everything in `rest`. And a live +60 ms nudge on a throwaway
reproduced Shape A's signature exactly — the step, the 0.8 ms/s slew, the
patience restart 10 s later — with the table reading `nudge +60.0`.

**Problem.** Shape A: laptop and pc stepped +60/+71 then +84/+73 ms together
2–4 s after a start, twice, and slewed at exactly 0.8 ms/s until patience
fired. `err` has no audio in it, so an input stepped — and every input the
trace logs was steady. The instrument is blind to the one that moved.

**Conductor half (no reload).**

- Remember what was sent: in `_steer_all`, per node, `node.last_steer =
  (t_ref, pos_ms, at_node_ms)`. In the `steerAck` branch, add to the trace
  line `sentPosMs`, `sentAtNodeMs`, `sentAgeS = now() - t_ref`. Old pages
  benefit immediately: a step in `err` with `sentPosMs`/`sentAtNodeMs`
  advancing at 1.000 says the conductor's target didn't move.
- A `config` event from the three commands. `nudge`: level info, text
  `nudge <node> +0 -> +60 ms`, fields `nudgeMs`, `was`. `volume` / `eq`:
  level debug, fields `volume` / `eqDb`. Same `event()` helper, same ring,
  same trace. Also on the config pushed at join (wherever `hello` replays the
  persisted nudge), so a page that joins *with* a nudge is on record.
- Nothing else: no new message types, no cadence change.

**Node half (reload).** `steerAck` carries `nudgeMs`, `targetCtx`,
`anchorCtx`, `anchorPos` (four numbers; `rate` and `mapMs` are already
there). `_clean_*` clamps for each on the conductor, as `_clean_map_ms` does;
None from a page too old to send them. Trace columns: `nudgeMs`,
`targetCtx`, `anchorCtx`, `anchorPos`.

**Tool half.** `tools/trace_report.py` gains a STEPS table: every ack where
`|Δerr| > 30 ms` against the previous ack on the same source (runS
increasing), with the delta of each input alongside — `ΔsentPos/ΔsentAt`
(should be 1.000), `Δmap`, `Δnudge`, `Δanchor` — so the column that jumped
is the answer. Today's trace already yields the first two columns once the
conductor half lands; the rest fill in after the reload.

**Verification.** `tests/test_trace.py`: a steer line after a fake ack
carries the sent triple; a `nudge` command lands a `config` event in the
ring and the trace with `was`/`nudgeMs`; hostile ack fields (strings, NaN,
1e300) come through as None. `tools/reanchor_harness.js`: every steerAck
carries the four node fields and they agree with `posAt` (the harness calls
the shipped function; do not reimplement `posAt`). `node --check`.
`trace_report.py` on today's file prints the STEPS table with the two
conductor columns and `-` for the rest.

**Audible trade-off.** None. Reporting only.

## Slice 2 — a fault is confirmed before it restarts

**Problem.** Shape C: the phone read −749 at runS 104 after 100 s of 0, the
fault rule restarted on that one ack, the audio was then genuinely 750 ms
off, the next ack read +750, and a second restart put it back. Two audible
discontinuities from one bad sample. Shape B shows the same false readings
at start below the threshold (phone +101 → 0, −129 → 0, no restart) — a
single ack is not a measurement.

**Gate.** Build when a trace shows a **second** mirror pair (an ack past
`REANCHOR_S`, a restart, then an ack of the opposite sign and similar size
within one steer interval), or on Matthew's say-so. Slice 1's STEPS table
makes the pair a one-line find; `trace_report.py` can count them
explicitly (a MIRRORS line under STARTS) as part of slice 1 if wanted.

**Change.** In `onSteer`, the fault branch requires two consecutive acks past
`REANCHOR_S` with the same sign: `current.faultAt` (the ctx time of the first
offender) set on the first, cleared the moment an ack comes back inside;
restart only when set and this ack is also past. Same shape as `slewSince`.
The `state` cause gains `acks: 2` so the trace says it was confirmed. No
change to the patience rule, the seek arithmetic, or the constants.

**Option, not recommended.** A hard bound (`HARD_FAULT_S`, ~2 s) that still
restarts on one ack, for a suspended context coming back. Not needed: a real
2 s error is still 2 s wrong on the next ack and restarts then; the 2 s of
extra wrongness is the same cost as any other real fault under this rule.

**Verification.** `tools/reanchor_harness.js` old-vs-new on the shipped
`onSteer`: one −400 ack → no restart, ack sent with err; −400 then −400 →
one restart, reason `fault`, `acks: 2`; −400 then +5 → no restart, `faultAt`
cleared (the phone's case); −400 then +400 → no restart (opposite sign; the
mirror of a restart that didn't happen); the patience path byte-for-byte as
before; the existing 20 checks still pass. `node --check`. Then live on a
throwaway :8931 with the in-app browser: a forced bad ack via the console
does not restart, a second one does.

**Audible trade-off, stated plainly.** A *real* fault (Shape B's laptop at
−410 on a cold start, twice today) is corrected one steer interval later —
about 2 s more of being wrong, once per such start. A *false* one costs
nothing instead of two restarts. "Silent-then-right beats wrong-then-right"
was the last call of this kind; this one is "wrong-for-2-s-then-right beats
wrong-twice". Matthew's call.

## Slice 3 — a cadence event with a deadband

**Problem.** `_note_boost` compares quarter-step *buckets* cycle to cycle.
The pc's boost sits at 1.125 — on the boundary — so it reported
`1.12x -> 1.13x` and back every few seconds for hours: ~100 of the day's
109 cadence events, and most of the debug rows in the ring.

**Change.** Compare against the last value an event *announced*, not the
last cycle: `node.ping_boost_said`, updated only when
`abs(boost - said) >= 0.25`, which is when the event fires. `node.ping_boost`
keeps updating every cycle (the pulse and the control page read it).
Conductor-only, no reload, no cadence change — the boost itself is untouched,
only when it is mentioned.

**Verification.** `tests/test_ping_cadence.py`: the sequence 1.0, 1.12, 1.13,
1.12, 1.13 announces nothing; 1.0 → 1.3 announces once; 1.3 → 1.13 nothing;
→ 1.0 once; a monotonic climb 1.0 → 4.0 in 0.1 steps announces exactly 12
times. Then `trace_report.py` on the next day's trace: cadence events in the
tens, not hundreds.

**Audible trade-off.** None.

## Commit ladder

1. **`feat(trace): a step names its input — sent target, config events,
   ack anchors`** (slice 1). Conductor half is useful alone and needs no
   reload; the node half rides with the next reload Matthew does anyway.
   Can be split in two if the reload is far off.
2. **`feat(conductor): a cadence event only when the boost actually
   moved`** (slice 3, out of order on purpose — trivial, no gate).
3. **`feat(player): a fault is two acks, not one`** (slice 2). After the
   gate; after slice 1 so its own effect is readable in the STEPS table.

Each conductor step needs a restart of :8927 — Matthew's action. Slices 1
(node half) and 2 need a page reload on every node; ⟳ in the NODES table
says which haven't.

## Risks

- **Slice 1 ack growth** — four floats per ~2 s per node; the trace grows by
  maybe 15 %. Well under the 3 MB/h it already writes.
- **Slice 1 `config` at join** — must not double-log with the `join` event;
  put the nudge on the join line if that is simpler.
- **Slice 2 masks a real fault for one interval** — stated above; the one
  place a 2 s delay could be audible is a cold start that lands 400 ms late,
  which is Shape B, and Shape B may itself be a false reading (slice 1 will
  say).
- **Slice 2 harness drift** — the harness stubs `startSource`'s
  dependencies; `faultAt` lives on `current`, which the harness builds. Grow
  the stub with the field, as the memory says for `logLine`.
- **Slice 3 hides a slow drift** — a boost creeping 1.0 → 1.24 never
  announces. Acceptable: the control page shows the live number; the event
  is for the trail, and a quarter-step is the trail's resolution by design.

## Not in this plan

Shape B itself (the −410 cold-start reading — slice 1 decides whether it is
real before anything is built for it), the tablet's clock-fit thread, the
`outputLatency` 56/0 ms flip on the laptop and 296/280/80 on the phone
(noted; a join-line column at most, later), the mesh reasons, party upload,
the chirp. Nothing in `_steer_all`'s cadence or the ping schedule.

## Reuse points

- `Conductor.event`, `_trace`, the `steerAck` branch and `_clean_map_ms` —
  the pattern every new field follows.
- `_steer_all` — the one place the target is made; `node.last_steer` hangs
  there.
- `_on_control_cmd` `nudge` / `volume` / `eq` — three call sites, one
  `config` event.
- `_note_boost` and `Node.ping_boost` — slice 3 in one function.
- `player.js`: `onSteer` (`slewSince` is the template for `faultAt`),
  `posAt`, the `steerAck` `send`.
- `tools/reanchor_harness.js` (`steerAt`, `starts`, `check`),
  `tests/test_trace.py`, `tests/test_ping_cadence.py`, the `bare` fixture in
  `tests/conftest.py` (add `last_steer` / `ping_boost_said` there — a new
  Node/Conductor attribute means adding it to the fixture, or 26 tests fall
  over again).
