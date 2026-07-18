# Phase 1 plan — mic-based auto-nudge (time-of-flight per node)

Status: **planned, not started.** Buildable spec so a future session can execute
without re-deriving. Obeys the house rule: additive, observable, revertible, one
commit per sub-step, `git revert` always an exit.

## Goal

Automatically measure each speaker-node's acoustic delay to a reference mic
position and set its `nudge_ms` so every speaker *arrives* aligned at that spot —
replacing the by-ear nudging. Single mic position for this slice; multi-point
joint optimization is a later phase.

## The key leverage (why this is cheap here)

The conductor emits a stimulus on speaker S at a **clock-synced** instant and the
mic-device's clock is already sub-ms aligned, so time-of-flight reads out
directly — no loopback needed. The playback sync doubles as measurement
infrastructure. And because nudge alignment is **relative** between speakers, the
mic's own constant input latency cancels — we never need to calibrate it.

## Algorithm

1. Idle only (not during music). For each speaker-node S, one at a time (others
   silent):
   - Conductor schedules a stimulus emit on S at synced node-time `T_S` (same
     path as the beep).
   - Mic-node records continuously; cross-correlates the capture against the
     known stimulus; the correlation peak → arrival sample → arrival time `A_S`
     in conductor-time (via the mic-node's own clock model).
   - Time-of-flight `ToF_S = A_S − T_S` (speaker output latency + air path +
     [constant mic latency]).
2. Align to the slowest: `target = max(ToF_S)`. For each S,
   `nudge_S = target − ToF_S` (≥ 0 — delay the earlier speakers to match the
   latest; positive nudge = later emission, per `perfToCtx(atNodeMs + nudgeMs)`).
3. Apply via the **existing** nudge command → persisted, pushed, revertible.

## Stimulus

Log sine sweep (ESS) or MLS, ~1–2 s, matched-filter/cross-correlated for high SNR
and a sharp sub-ms peak (48 kHz → ~20 µs resolution). Average 2–3 reps. Take the
**first** strong peak (direct path) and time-gate out later reflections. Emit it
through `master` (bypassing the output EQ, like the beep) so the path is clean.

## Reuse points (files)

- **Scheduled emit:** model on `onBeep` in [web/player.js](web/player.js) +
  `_transport_beep` in [syncplay/conductor.py](syncplay/conductor.py) — a new
  `measure` message carrying `atNodeMs`, played via `perfToCtx` exactly like the
  beep.
- **Result reporting:** model on the mesh path — mic-node sends
  `{type:"measureResult", speaker:S, tofMs}`; conductor aggregates (like
  `meshSample`), computes nudges, applies through the existing `nudge` handler in
  `_on_control_cmd` (persist + `config` push already done there).
- **Clock mapping:** `perfToCtx` (output) already exists; input side needs an
  arrival→conductor-time map using the node's `ClockModel` estimate.
- **Control trigger:** a "📏 calibrate" button beside beep/resync in
  [web/control.html](web/control.html) / [web/control.js](web/control.js).

## New pieces (the actual work)

- **Mic capture** on the mic-node: `getUserMedia({audio})` → AudioWorklet (or
  MediaRecorder) into a timestamped ring buffer. New "use this device's mic as
  the calibration reference" mode on the player page.
- **Cross-correlation in JS:** small FFT-based matched filter (~100 lines, or an
  `OfflineAudioContext` convolution trick). Returns peak lag → ToF.
- **Calibrate flow + review UI:** show measured ToF and *proposed* nudges per
  node; human approves before apply (observable); "reset nudges" is the revert.

## Prerequisite

`getUserMedia` needs a **secure context** — blocked on plain-http LAN, so the
real fleet needs the **HTTPS story** first (self-signed cert or tunnel; also
unlocks Wake Lock + `randomUUID`). Build/verify the DSP on `localhost` (secure)
first, flip HTTPS on for the fleet.

## Suggested commit ladder (small, testable)

1. **[DONE 2026-07-18]** Mic-capture plumbing + "use as calibration mic" button +
   RMS level meter on control. Verified sans real mic (graceful getUserMedia fail;
   full micMode/micLevel relay + meter math). Real-capture + HTTPS test pending on
   hardware.
2. **[DONE 2026-07-18]** Chirp emit on one node + time-domain cross-correlation on
   the mic-node → ToF readout on control. Verified sans real mic (sample-exact
   correlator, exact ToF round-trip math, capture worklet loads, command wiring).
   Real acoustic loop + HTTPS pending on hardware.
3. Sequence all nodes; compute relative nudges; show proposed values on control.
4. Apply → set nudges via the existing path. Persisted, revertible. Ship.

## Risks / unknowns to watch

- Browser mic-input latency/timestamp precision (mitigated: relative-across-
  speakers cancels the constant part; AudioWorklet for tight capture).
- Room SNR / reflections (mitigated: sweep + matched filter + first-peak gating +
  averaging).
- Mic-node clock model must be warm at measure time (run a join-style ping burst
  first).
- HTTPS gate for the real fleet (above).

## Not in this slice

Per-node magnitude auto-EQ (Phase 2) and multi-point joint delay/gain/EQ
optimization across the phones-as-sensor-grid (Phase 3). Actuators already exist:
nudge = delay, volume = gain, eq = magnitude.
