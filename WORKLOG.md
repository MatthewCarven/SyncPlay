# SyncPlay worklog

## 2026-07-27 — play queue on the control page

Matthew wanted a queue button to adjust what plays next. TODO.md had already
designed this as the prerequisite commit for party mode, so it got built that
way: full queue, control page only.

The whole feature turns on splitting one old function. `_next_track()` was
called from three places that meant two different things — the prefetch after a
play (*"what's next, so I can decode it early"*) and the two real advances
(auto-advance at end of track, ⏭ next). Same question, but only the latter
should consume a queue entry. So it became `_peek_next()` (queue head if any,
else the old folder-order walk, renamed `_folder_next`) and `_take_next()`
(identical choice, pops what it used). Every remaining behaviour falls out of
which one a call site picks. With an empty queue both collapse to exactly the
old code path, which is why this can't regress folder playback.

Decisions worth remembering:

- **Duplicates are allowed**, so the control page addresses queue entries by
  **index**, not track id — position is the only unambiguous handle when the
  same song appears twice. `unqueue`/`queueMove` both validate the index (and
  the destination) server-side and no-op on anything out of range.
- **Not persisted.** Nudges/volumes/EQs live in `syncplay_state.json` because
  they're calibration; a queue is a mood. It dies with the process, and it's
  pruned on rescan so a deleted file can't sit in it as a ghost id.
- **Explicit `play {trackId}` is an override** — it plays that track and leaves
  the queue intact for afterwards. A *bare* `play` (what ▶ now sends from a
  standstill) starts the queue head and consumes it. ⏭ next does the same.
- Any queue mutation re-runs `_prefetch_next()`, so re-ordering mid-song
  immediately gets the new next track decoding on every node rather than
  waiting for the gap.

Zero changes to `player.js`, the scheduler, or anything in the timing path —
the queue only decides *which* Track gets handed to the unchanged
`_transport_play()`.

Verified: `tests/test_queue.py` (new, 17 cases) drives the control-command
surface — add/dupe/move/remove/clear, malformed edits are no-ops, peek doesn't
consume, next does, fallback to folder order when empty, explicit-play spares
the queue, bare-play consumes it, rescan pruning — plus a regression guard that
with an empty queue `_peek_next`/`_take_next` are exactly the old circular
folder walk. 29 tests pass (12 pre-existing timesync + 17 new). Live over
`:8931`: control page + JS serve, queue add/move/clear round-trip through the
snapshot, and the server shrugs off malformed commands (bad ids, non-numeric
index/delta, missing fields). Control rendering checked against a stub DOM:
correct ▸/next-up markers, `queued×2` on the duplicated track, ↑/↓ disabled at
the ends, empty-state copy, and HTML escaping intact.

**Left for meatthread0:** the real-fleet click test — the queue is control-page
logic and never touches audio, but worth watching a queued track actually take
over at the gap with three nodes up. Nodes don't need to reload for this one
(no player protocol change); the *control* page does.

## 2026-07-22 — seek bar + ⏮ restart on the control page

Matthew wanted to jump the currently-playing track back to the top (or anywhere)
when a take goes wrong mid-song. It was nearly free: `_transport_play(track,
seek_ms)` is already the seek primitive — the exact call `resume` uses — and the
player already honors `seekMs` in a play command, so no `player.js` change and no
new timing logic. Added a `seek` control command that re-plays the current track
(`self.playing or self.paused`) at an absolute `positionMs`, clamped to
`[0, duration-250ms]`, via that same synced path — the track's already decoded on
every node so the load gate clears instantly, and `dispatch()` supersedes rapid
re-seeks. On the control page the read-only position bar became click-to-seek
(wrapped in a padded `#posHit` hit area, bar bumped 4→8 px, optimistic fill snap
so it feels instant), plus a ⏮ restart button in the transport row for the no-aim
"take it from the top" mash (just `seek → 0`).

Each seek is a ~1.8 s *coordinated* re-start (the PLAY_LEAD everyone schedules
against), not a live scrub — which is the whole point: the fleet stays locked.
Verified over `:8931`: click-to-seek at 50 % landed `seekMs=105296` of 210814 with
node sync **0.047 ms**; ⏮ restart landed `seekMs=0` at **−0.095 ms**; playback
continued through both, no server errors. Sub-ms sync held right through the seeks
because it's the same coordinated-start path as play/resume.

## 2026-07-21 — per-node download % in the control pill

When you hit play on a track no node has cached yet, the file has to stream from
the conductor before the load gate opens — and the control page showed nothing
but a stale "idle" for that whole wait. Now each node reports its byte-download
progress and the control page shows it in the node's status pill:
`idle → ⬇ 47% → ready → ♪ playing`.

The key design call was *where* the number lives. It's tempting to hang it on the
playlist row, but a track is pulled by N nodes at N different speeds — one row,
no single %. A download is a **per-node** thing, so it belongs on the node row,
where each row has exactly one current fetch: nothing to select, the row *is* the
selector. `player.js` swaps the one-shot `resp.arrayBuffer()` for a
`resp.body.getReader()` loop that counts bytes against `Content-Length` and sends
`loadProgress {trackId, pct}` (throttled ~every 2%), then reassembles the chunks
and decodes exactly as before — falls back to a plain read if the length or
ReadableStream is missing. The conductor stashes `load_pct`/`load_track` per node
and exposes `loadPct` in the snapshot (non-null only while a not-yet-decoded track
is streaming; cleared on `loaded`). Control renders it in the pill, masked by
`♪ playing`. Fully outside the audio path — rides the 1 Hz snapshot, never pushes,
zero effect on decode, scheduling, or the servo.

Verified hard over `:8931` with the 59.6 MB Tubular Bells: the live control pill
walked `idle → ⬇ 99% → ready → ♪ playing`, the server logged the duration + play
with **no `loadError`** (so the streamed-and-reassembled bytes decode fine and
playback is intact), and an instrumented run of the progress loop against the same
file emitted a clean throttled climb — `0, 2, 5, 7 … 96, 98, 100` (38 steps).
`Content-Length` is already present on the `FileResponse`, so the % needed no
server-side change to get its denominator.

Deferred (the obvious next slice if it feels chunky): a targeted live relay like
`micLevel`, so the pill ticks up faster than the 1 Hz snapshot. Snapshot-only was
chosen first because a fast LAN pull *should* just flip to "ready" — the % is for
the slow case, which is exactly where 1 Hz is plenty.

## 2026-07-21 (later) — make the download % live (relay + target-track gate)

The snapshot-only version above lost to its own caveat in the field: a ~30 MB
file on the real fleet downloads well inside one 1 Hz snapshot, so the pill just
blinked `idle → playing` with nothing between — the middle was never sampled.
Promoted it to a targeted live relay, exactly like `micLevel`/`spectrum`: the
`loadProgress` handler now `_broadcast_control`s each step straight to the control
page, and `control.js` paints the node's status cell directly (`setNodePill`, via
a new `data-node` row attr + `nodeState` class) instead of waiting for the
snapshot. The 1 Hz snapshot field stays as a fallback for a late-opening page; the
relay just makes it tick. A `done` relay on `loaded` retires the ⬇% the instant
decode finishes rather than lingering at 99%.

Verifying it surfaced a real glitch, which the relay made visible: right after the
current track loads, the conductor prefetches the *next* one, and that background
download repainted the just-loaded node's pill (`⬇ 99% → ready → ⬇ 38% → playing`)
before the "playing" snapshot landed. Fixed at the source with a `target_track`
field (set at the top of `_transport_play`, cleared on stop): the relay only fires
for the track we're actively bringing up, never the silent next-track prefetch.

Re-verified over `:8931` with Tubular Bells: the pill now walks a live climb
(`idle → ⬇13% → 40% → 84% → 99% → ready → ♪ playing`) with a clean tail — 16
samples across the 0.8 s ready→playing window caught zero prefetch repaints — and
the node ends `playing / loadedCurrent / loadPct=null`, no server errors.

## 2026-07-18 (later, iv) — chirp time-of-flight measurement (auto-nudge step 2)

The measurement engine from [AUTONUDGE_PLAN.md](AUTONUDGE_PLAN.md) step 2: a
speaker emits a short swept-sine chirp at a clock-synced instant, the mic node
captures a window and cross-correlates it against the same reference → the
correlation peak is the direct-sound arrival, and arrival − synced-emit =
time-of-flight. The speaker plays a 40 ms, 1–8 kHz chirp straight to destination
at a fixed gain (bypasses EQ + volume + nudge — we measure the *raw* path). The
mic captures via a Blob-loaded AudioWorklet that forwards frame-stamped input
blocks only while armed; a normalized time-domain xcorr (short chirp + short
window → no FFT) finds the lag; the arrival ctx-time maps to perf-time via
`getOutputTimestamp` and is reported. The conductor picks `t_emit`, converts it
to each node's local clock (like the beep), and on the mic's reply maps arrival
back with `to_conductor_time` → ToF, shown on a new "measure" readout in the
CALIBRATION card. The ToF carries a constant per-mic offset (input latency +
mapping bias); only *differences* between speakers are physical — exactly what
step 3 will difference into nudges.

Verified everything short of a physical mic (sandbox blocks capture): the
cross-correlator recovers planted delays to the **sample** (500/1500/4800/12000
→ exact, peak ~0.994, snr 62–92 through an 8% noise floor); the ToF round-trip
math is exact in Python (0/5/30 ms in → 0/5/30 out, 123 ms offset recovered);
`onMeasureEmit` schedules the chirp without error; the capture worklet
compiles/registers/instantiates; the `measure` command routes + guards ("needs
exactly one active mic node"); the readout renders. 12/12 tests green, no
console/server errors.

Pending on hardware: the real acoustic loop — a mic capturing a real chirp off a
speaker — and HTTPS for the fleet. Next (steps 3–4): sequence all speakers,
difference the ToFs into per-node nudges, show proposed, apply via the existing
nudge path.

## 2026-07-18 (later still) — calibration-mic plumbing (auto-nudge step 1)

First bite of the mic slice from [AUTONUDGE_PLAN.md](AUTONUDGE_PLAN.md): opt a
device into being a measurement microphone and prove the pipeline — no DSP yet.
Player page gets a "🎙️ use as calibration mic" button → `getUserMedia` (secure
context only; echo/noise/AGC off), RMS off an AnalyserNode every 120 ms, reported
as `micLevel`. The mic is a **sink only** — never routed to the speakers, so no
monitoring feedback. Conductor gains `micMode` (sets `node.mic`, snapshotted) and
`micLevel` (relayed to control via `_broadcast_control`; self-heals mic state on
reconnect). Control grows a CALIBRATION card: a live dBFS input meter per mic node.

Verified on the alt conductor (8931) — everything the sandbox allows without a
real mic. Secure context + `getUserMedia` present and invoked; capture blocked by
the pane → caught cleanly as "mic unavailable: NotAllowedError", button resets, no
crash or console errors (exactly the graceful-fail path that matters on plain-http
LAN before HTTPS lands). Plumbing proven by injecting the messages a mic would
send: `micMode` propagated to `node.mic` and rendered the meter row; `micLevel`
relayed through the conductor; meter math checks out across the range (rms 0.1 →
−20 dB/67%, 0.5 → −6 dB/90%, 0.002 → −54 dB/10%), decaying to "—" when stale.
12/12 tests green.

Untested here (needs real hardware): actual capture + RMS from a live mic, and the
HTTPS gate for the real fleet — meatthread0's bench test with the laptop array /
the 4-capsule unit. Next (plan steps 2–4): scheduled sweep emit + cross-correlation
→ time-of-flight → auto-nudge.

## 2026-07-18 (later) — per-node output EQ (shape them)

The "see them → shape them" sequel to the spectrum meter, and the first tool
that deliberately alters the node audio path. Five biquads per node in series
(lowshelf 80, peaking 250/1k/4k, highshelf 12k), spliced source→eq→master, so
the spectrum tap on master shows the *shaped* result. Control grows a
vertical-slider bank per node (±12 dB); a change pushes the whole 5-gain array
on release (like nudge/volume), the conductor clamps + persists in
`syncplay_state.json` + relays a `config`, and the node ramps each gain via
`setTargetAtTime` (click-free). Born flat (0 dB = transparent), so it ships as a
no-op; the beep skips the chain and stays a clean sync reference.

Safety held exactly as designed: filters shape tone, not position — the servo
reads playback position off `current.*` upstream of the chain and never sees it.
Verified on the alt conductor (8931) with two dev nodes mid-track: pushing
[+12,+12,0,0,0] to one node set its live BiquadFilter gains to exactly that
while the other stayed flat (per-node ✓); the 12 kHz shelf lifted the top
spectrum bins 3→12 (the low boost just pegged the analyser's 255 ceiling — this
track's bass already maxes it); `eqs.dev-a` persisted, then "flat" popped it
back out; and the EQ'd node held **err −0.01 ms** — timing untouched. 12/12
tests green, no console/server errors. One dev-only wrinkle: two `?id=` tabs
share `localStorage`, so a reconnect cross-named them — real devices don't.

Next: the mic/HTTPS path turns this manual EQ into closed-loop room correction —
measure the distortion of the space, bend the signal to cancel it.

## 2026-07-18 — per-node spectrum ("equalizer") on the control page

Matthew's idea: get audio *back* from the fleet and offer an equalizer. Scoped
it to the cheapest, safest slice that still lands the visual — a live per-node
spectrum on the control page fed by an **internal WebAudio tap, not mics**. No
HTTPS, no WebRTC audio, no new transport, and zero lines of timing code touched.
(Deferred, in order: output-shaping EQ pushed to nodes; then the mic/acoustic
path — which needs the parked HTTPS story first — unlocking room correction +
auto-nudge.)

Data path reuses the whole existing star. Each node hangs an AnalyserNode off
its `master` gain (the one bus every source *and* the beep flow through — a
read-only sink: no latency, invisible to the servo, which reads position off
`current.*` upstream of master), folds 128 FFT bins into 28 log-spaced bands,
and ships them ~12.5 fps over its player WebSocket *only while playing*. The
conductor relays each frame straight to any open control page (new
`_broadcast_control` helper, which also DRYs `push_state`/`toast`). Control
keeps one bar-row per connected node and eases the bars with attack/gravity so
the ~12 fps feed looks fluid. One additive commit; `git revert` is the exit.

Verified on the alt conductor (8931, isolated from the live 8927 fleet Matthew
was running) with two `?id=` dev nodes playing "02 - Out Of Time": frames
arriving from *both* nodes, 28 bands, fresh (age 11–73 ms), a real bass-heavy
shape (bars pinned at 1.0 in the low end, tapering to the 0.02 floor up top),
the two nodes reading independently. Crucially the **servo held sub-ms right
through it — err 0.3 / 0.8 ms** — proof the tap doesn't perturb timing. 12/12
tests green; no console or server errors. (The sandbox's screenshot tool was
wedged — timed out even on the static node page — so verified by DOM/data
inspection instead, which is the stronger proof anyway.)

**Real-fleet confirmation (same day):** Matthew restarted the live conductor,
reloaded the pages, and the spectrum lit up on all three real nodes at once
(phone, pc, Laptop1) mid-"Out Of Time" — each row its own independent spectral
shape, mesh closures still sub-ms green (−0.10 / 0.34 / 0.72). One caveat worth
logging: Laptop1 read err 46.9 ms vs ~1 ms on the others — not a sync-quality
issue (its clock is the best in the fleet: rtt 0.3, drift −0.5), just the servo
still slewing out the offset left by its reload catchup. 46.9 ms exceeds what
the ±800 ppm cap can null inside the 15 s horizon, so it rate-limits to
~0.8 ms/s (≈a minute to zero); a pause→resume re-anchors all three instantly.
Matthew: "cannot overstate the awesome."

## 2026-07-17 — v3: sync truth matrix (client↔client pings)

Matthew picked the mesh idea off the backlog. Design: WebRTC DataChannels
(unreliable/unordered ≈ UDP) between nodes, conductor as signaling relay;
lower clientId initiates; initiator relays raw four-timestamp samples to the
conductor, which runs a ClockModel per pair — nodes stay dumb, math stays in
one tested place. Dashboard gains MESH TRUTH: per-pair direct offset vs
star-implied offset; the difference (triangle closure) is the *measured*
end-to-end sync error.

Verified with two ?id= dev nodes on the alt conductor (8931): channel
connected through the relay, 42 filtered samples at 0.8 ms rtt, and the
headline number — **closure 0.12 ms**: direct measurement and star inference
agree to ~120 µs on one machine. Pair teardown on node leave works. Zero
console errors. Also shipped: Cache-Control no-cache middleware after
catching a browser serving pre-mesh player.js from cache (this also ends
the "refresh your phone to get new features" era — pages now revalidate
on every reload), and a ?id= URL override so several tabs in one browser
can act as distinct test nodes.

Next real-world data point: closure numbers across actual Wi-Fi devices —
expect single-digit ms, dominated by Wi-Fi asymmetry.

**Real-fleet results (same day):** 5:40 into a 30-min mix, three nodes all
playing — servo err: laptop −0.0 ms, pc 0.4 ms (its 22.2 ppm crystal means
~7.5 ms of drift already absorbed mid-song), phone 0.7 ms. Mesh closures all
green: laptop↔pc 0.10 ms (n=96), laptop↔phone 1.39 ms, phone↔pc −1.15 ms
(phone pairs young, n=1–2). Bonus consistency proof: the three *direct*
measurements close their own triangle to 0.14 ms
(−359,971.53 + 354,582.80 = −5,388.73 vs measured −5,388.87). Every device
in the house is synced to well under the width of a speaker cabinet, and the
system now proves its own honesty three independent ways. Matthew verdict:
"Epic Awesome".

## 2026-07-16 (late) — v2: mid-song playbackRate servo

The one-shot start left mid-song position at the mercy of each device's DAC
crystal (a different oscillator than the CPU clock we sync). Now the conductor
sends each playing node a 2 s-cadence reference ("at your local L, position
should be P"); nodes trim playbackRate ≤±800 ppm (inaudible) to null the error
over ~15 s, with a hard in-place re-anchor for >200 ms faults (tab suspend,
stall) — nodes self-heal. Live per-node error is reported back and shown as an
"err ms" column on the dashboard. Also: per-node volume pushable from control
(persisted with nudges), focus-guarded table renders so inputs aren't
clobbered mid-edit.

Verified live: browser node held err = 0.1 ms with a −2.2 ppm trim; volume
push landed (gain 0.3 + slider synced); 12/12 tests still green. Noted:
stale player pages (pre-upgrade) keep playing fine but show "—" for err ms
until refreshed. Meanwhile Matthew's fleet grew to 4+ real nodes; his
ID10TError-pc confesses 23 ppm drift — the servo's first real customer.

## 2026-07-16 (evening) — first real multi-device sync

Beep test passed across PC + phone on Wi-Fi (network "Public" profile;
firewall pre-checked — Python314 already had inbound Allow rules). Matthew
verdict: "awesome". v1's core promise — multiple physical devices sounding
as one — is confirmed on real hardware over a real network.

First real-hardware sync numbers (3 nodes):
- Phone (Wi-Fi): drift 6.7–6.8 ppm, best RTT 2.8 ms, 163/1176 samples
  surviving the min-RTT filter — jitter filtering working exactly as
  designed. 6.8 ppm ≈ 1.6 ms drift per 4-min song → between-song resync
  comfortably covers multi-room; rate correction only needed for 30 min+
  continuous tracks (v2).
- Laptop: drift −0.1 ppm, best RTT 0.3 ms, ~all samples used.
- Same-machine browser node: −0.0 ppm (control baseline, as expected).

## 2026-07-16 — v1 built and verified end-to-end (single machine)

Plan approved (see `~/.claude/plans/uh-i-m-dreaming-of-stateful-charm.md`).
Decision: one repo, Python conductor + browser nodes; timesync as a
dependency-free module. Built milestones 1–4 plus most of 5 in one session.

**Done**
- `syncplay/timesync.py` — four-timestamp offset/RTT, min-RTT jitter filter,
  sliding-window drift regression, exact conductor↔node projection.
  12 pytest tests, all passing: recovers 50 ppm skew ±5 ppm through simulated
  exponential Wi-Fi jitter; projects start times 120 s out within 2 ms.
- `syncplay/conductor.py` — aiohttp server: player/control pages, track
  serving, per-node ping cadence (join burst 16 / gap burst 10 / playing 3),
  load-gated drift-projected play scheduling, auto-advance with pre-end resync
  burst, synced pause/resume/stop, beep test, late-join catchup, per-node
  nudge with persistence (`syncplay_state.json`), rescan.
- `web/player.{html,js}` — join-tap audio unlock + wake lock, ping echo,
  prefetch + decode cache (2 tracks), performance.now→AudioContext mapping via
  getOutputTimestamp (bakes in output latency), scheduled starts w/ seek,
  reconnect with cached-buffer re-announce.
- `web/control.{html,js}` — live node table (offset/RTT/drift/nudge), playlist,
  transport, toasts.
- `tools/make_test_tones.py` — pulse-train test WAVs (sharp transients for
  alignment measurement).

**Verified in browser (loopback node)**
- Stats sane: offset ≈ −2.97e8 ms (perf_counter vs performance.now epochs),
  best RTT 0.3–0.5 ms, drift ~0.0 ppm (same crystal) ✓
- Beep test schedules cleanly; play → auto-advance cycled A→B→A→B at the
  expected 10 s cadence (8 s track + gap + 1.8 s lead); pause/resume/stop all
  behave; duration reported by node decode; no console errors.

**Fixed during verification**
- cp1252 console: non-ASCII (→, curly quotes) in banner/logs crashed or
  mangled on Windows — all console strings now ASCII.
- Pause during the pre-start lead window produced a negative resume position —
  now clamped to 0.

**Surfaced for later (v1.x / v2)**
- Real multi-device test: PC + phone on Wi-Fi (needs meatthread0's hardware).
- Wi-Fi RTT/offset numbers will be ~10–50× loopback; validate the filter there.
- Mid-song rate micro-correction via playbackRate (±100 ppm is inaudible).
- Volume control from the control page (per-node gain push).
- Optional: WebTransport datagrams for the sync channel; native PC player.

**For meatthread0**
- `git init` still pending — repo-worthy since the first commit of this session.
- Conductor left running on port 8927 for immediate phone testing.
