# SyncPlay worklog

## 2026-07-27 — auto-nudge step 3: sweep every speaker, propose nudges

Matthew asked what HTTPS would take. Answering it properly turned up something
better: I'd previously called HTTPS the critical path for the acoustic half of
the roadmap, and that was wrong. `localhost` is already a secure context — which
is *why* steps 1–2 could be built at all — so step 3 needed no certificates
whatsoever. Better still, the conductor's own machine is the ideal mic node: its
clock **is** the reference clock, so the mic's own offset error is zero. HTTPS is
only needed to put the mic on a phone. We built step 3 instead of buying a domain.

The sweep: one speaker at a time (others silent), `CAL_REPS` chirps each, a gap
between probes so reflections die, then `plan_nudges()` turns the readings into
one proposal per speaker. Progress broadcasts per rep — a 15-second silent wait
is indistinguishable from a hang.

Three judgement calls, all of them in a pure function so they could be tested
without a microphone:

- **Median, not mean.** One mis-picked correlation peak — a reflection, a door —
  is a wild outlier, and a mean smears it across the answer. The median ignores
  it. That is the entire reason for taking reps, and the test that pins it plants
  a 410 ms rep among 10 ms ones and demands the answer stay 10.4.
- **Align to the latest arrival.** `target = max(median ToF)`, so every proposal
  is ≥ 0. You can delay a speaker; you cannot make a distant one arrive sooner,
  so the furthest speaker sets the pace.
- **A speaker we couldn't hear is excluded, not guessed.** Too few clean reps →
  no proposal, and critically it's left out of the target too, so one deaf
  measurement can't re-time the whole room. Absurd spacing (>500 ms ≈ 170 m of
  air) is flagged rather than silently clamped: a clamp would look like a
  successful calibration.

Read-only on purpose. The table proposes; a human applies. A bad peak should
cost a re-run, not a wrecked calibration you then un-tune by ear. Step 4 is
cheap when it comes — the control page can just fire the existing per-node
`nudge` command per accepted row, needing no new server surface.

`_measure_one` and the sweep now share one `_probe_once` (awaitable, resolves a
future from the `measureResult` handler), so the 📏 single-shot button behaves
exactly as before and remains the debugging tool when a sweep looks wrong.

**Verified — and be precise about what that means.** `tests/test_calibration.py`
(10 cases) pins the arithmetic, including a test that adds a constant 137 ms to
every reading and demands the proposals not move, since the mic's input latency
is exactly such a constant. Then a harness of *simulated* player sockets — real
WebSockets, real ping cadence, real clock models, real sequencing — answered
`measureArm`/`measureEmit` with planted time-of-flights of 8/19.5/33 ms plus that
137 ms mic latency, and the conductor proposed 25.0/13.5/0.0: the planted
differences exactly, constant cancelled. A deliberately deaf speaker got no
proposal and did not shift the target. 39 tests pass.

**What is still unproven: everything acoustic.** No chirp has crossed actual air
in this project yet. The orchestration and the maths are right; whether the
correlator finds a real direct-path peak in a real room with real speakers is
untested, and it's the only question that matters now.

**For meatthread0:** the first hardware run. Conductor machine's own browser at
`http://localhost:8927/` → JOIN → "use as calibration mic", two other devices as
speakers, stop playback, hit **📐 calibrate all**. What to watch: `peak` should
be well clear of 0.15 (that's the gate), `±` spread should be small — a wide
spread means the correlator is picking different reflections each rep — and the
proposals should be in the low tens of ms for a normal room. If the numbers look
mad, hit 📏 on a single speaker and read the raw ToF.

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

---

## 2026-07-28 — cold-join accuracy: remembered skew

**Prompt:** four nodes locked and closing to ≤0.22 ms on the mesh, but the
tablet sits at −3.8 ms err with n=25 samples, and it's worst right after it
joins. Question was whether anything can be done about the weakest moment in a
node's life, when its calibration data barely exists.

**Diagnosis (three separate things, only one fixed today)**

1. *All join calibration is drawn from a single ~1 s window.* `BURST_JOIN =
   (16, 0.06)` fires 16 pings inside one second. They are not 16 independent
   samples — tablets power-save the Wi-Fi radio aggressively, so one bad beacon
   window corrupts the whole burst at once, and min-RTT filtering cannot rescue
   a window where `best` is itself inflated.
2. *`filter_best`'s tolerance is backwards for slow nodes.* `cutoff = best +
   0.002 + 0.25×best`. For the tablet (best 3.8 ms) that admits samples up to
   1.8× best, each carrying up to ±3.3 ms of path asymmetry — which is roughly
   the error being observed. For the laptop (best 0.2 ms) the same formula is
   11× best, but 11× of nothing is nothing. The 2 ms absolute floor only bites
   the node least able to absorb it.
3. *Skew was discarded on every reconnect.* `begin_session()` rebuilt a bare
   `ClockModel()`. Correct for offset — `performance.now()` restarts with the
   page — but skew belongs to the **crystal**, not the page-life.

**Shipped: #3.** Chosen first for being the largest win against the least risk;
the persistence plumbing (nudges, volumes, EQs keyed by `clientId`) already
existed and skew slots straight into it.

- `timesync.py` — `ClockModel(prior_skew=...)`, clamped to `max_skew` on the
  way in. Used only while the window can't fit its own slope; the short-window
  branch now de-trends by the prior before medianing, so the anchor offset is a
  clean value at `latest_mid` rather than a smear. `ClockEstimate.skew_fitted`
  distinguishes a measured slope from an inherited one. With no prior supplied
  the behaviour is byte-for-byte the old one (slope 0.0, median offset).
- `conductor.py` — `Node.prior_skew` + `Node.remember_skew()`, which banks a
  skew **only if `skew_fitted`**. That guard is the whole safety story: an
  inherited prior can never launder itself into a measurement, so one bad
  reading cannot outlive the next good session. `end_session()` banks and
  `_save_state()` also refreshes from live models, so a conductor killed
  mid-session still remembers. `_clean_skew()` rejects non-finite values and
  anything past ±500 ppm on load, so a corrupt state file degrades to today's
  cold start rather than to a confidently wrong join. Join log now prints
  `[seeded +19.9 ppm]` when a prior is in play.

**Tests** — 66 passing (was 45). New `tests/test_persistence.py` covers
`remember_skew` refusing to bank a prior, banking a real fit, `end_session`
capture, the full "wrong prior cannot outlive one good session" sequence, and
`_clean_skew` against junk input. New cases in `test_timesync.py` pin that the
prior is used while span is short, that a real fit overrides even a badly wrong
prior, that the anchor isn't biased by de-trending, and the payoff: after a 16-
ping/1 s join burst, projecting 60 s ahead, a cold model eats the full
80 ppm × 60 s ≈ 4.8 ms while a seeded one stays under 1 ms.

**Verified end-to-end** against a copy of the real `syncplay_state.json`: a
legacy file with no `skews` key loads clean, a fitted 19.9 ppm writes and
survives a reload, seeds the next `ClockModel` through a 1 s join burst
(`skew_fitted=False`, as it should be), and a hand-corrupted file loads empty.

**Left on the table (see TODO)** — #1 and #2 above. #1 is the real fix for the
underlying physics and should land next; #2 is a two-line tuning change that
wants its own test and its own commit, since it changes behaviour for every
node rather than just returning ones.

**Commit hygiene note (the split didn't hold).** The two bodies of work above
were meant to land as separate commits — auto-nudge step 3, then remembered
skew — per the house rule that one commit per feature keeps `git revert` as an
exit. `git add -p` lost the fight with the index and took all four shared files
(`conductor.py`, README, TODO, this file) wholesale into the first commit. The
result as pushed:

- `d292140` "Auto-nudge step 3" — also contains the entire *conductor* half of
  the skew work (`Node.prior_skew`, `remember_skew()`, `_clean_skew()`, the
  state-file plumbing). It does **not** contain `timesync.py`, so this commit
  cannot run: the first node to join hits `ClockModel(prior_skew=...)` as a
  `TypeError`. Treat it as a broken rung when bisecting.
- `e146c7f` "Remember each node's skew across reconnects" — only `timesync.py`
  and the two test files.

So neither commit reverts cleanly: reverting the first strips the conductor
side while leaving `prior_skew` and `test_persistence.py` behind, and the suite
fails. HEAD itself is correct and green (66 passing) — this is a history-quality
problem, not a correctness one. Left unrewritten deliberately: the branch was
already pushed, and a force-push to tidy commit boundaries is a worse trade than
this note. Worth an extra beat on the staging step next time the working tree
holds two features at once.

---

## 2026-07-28 (later) — armed cold start

**Prompt:** "I'm happy for a countdown from like 5 or 10 seconds on all clients
if you started blank... we could wait until all clients have the file loaded
before playback like?" — 70 MB of mp3 to transfer, read for timing data, seek
and decode, on three devices at once.

**First finding: the load gate already existed.** `_transport_play` has always
broadcast the preload and then blocked up to `LOAD_GATE_TIMEOUT` until every
connected node reports the track decoded. So "wait for all clients" was already
the behaviour — it was just invisible, which is why it didn't feel like it.

**Second finding, worth recording because it redirects the optimising.** The
sync maths is *not* the load at playback. A ClockModel fit is a least-squares
over at most a few hundred floats and it runs on the conductor; a node's job at
start is one `getOutputTimestamp()` mapping and a `source.start(when)`. The cost
is transfer and decode, which is exactly what the gate already waits on.

**The good trade.** That wait is dead time we were already paying, and during it
`self.playing is None`, so the ping loop is on the idle cadence. A cold start
therefore *hands us* a multi-second calibration window for free — the very thing
the "spread the join burst" TODO wanted to buy with added join latency. Making
the wait visible and giving it a floor turns it into the spread burst.

**Shipped**
- `plan_start(playing_now, loaded) -> (arm, gate)` — pure, so the rule is
  testable without a fleet. Cold is a **conjunction**: nothing sounding *and*
  at least one connected node still loading. Both halves matter — the first is
  "can the room perceive this wait", the second is "does the wait exist". Warm
  covers resume, seek, next and auto-advance, which start instantly.
- `LOAD_GATE_COLD = 20.0` (was a flat 12.0, kept for warm), `ARM_SECONDS = 6.0`.
- Arming phase in `_transport_play`: broadcast `arm`, `request_burst` across the
  window, then hold until *both* everyone has decoded and the countdown has
  expired. A fast decode no longer short-circuits the number on screen.
- The countdown targets `t_arm_end + PLAY_LEAD` — the real start — not the end
  of arming. A timer that hits zero and then sits silent for 1.8 s is a timer
  nobody trusts twice.
- `_send_arm` dates the target on each node's own clock via its model, so every
  screen in the room reaches zero together rather than each reaching it at the
  same instant *plus its own clock error*. A node too fresh to have an estimate
  gets the raw duration — refusing it a countdown would punish precisely the
  node with the longest wait ahead of it.
- `_disarm()` on stop, on a superseding start, and on the no-node-loaded bail.
- Player: countdown card, local ticking (a countdown is reassurance, not a
  scheduling primitive — the real start still arrives as `play` with its own
  sample-accurate time). Overrun shows "starting…" rather than freezing on 0.
- Control: `arming` in the snapshot, interpolated between snapshots in
  `renderNowLine` so it moves smoothly instead of stepping once a second.

**Tests** — 85 passing, up from 66. `tests/test_arming.py` pins the conjunction
from both sides (dead start with one straggler arms; skip-to-unprefetched
mid-playlist does not), resume and auto-advance staying warm, the empty-fleet
case, and that the constants leave room for each other (`ARM_SECONDS <
LOAD_GATE_COLD`, cold gate never shorter than warm).

**Smoke-tested** against a fake fleet: cold start arms at t=0.03 s with the
countdown targeting ARM+LEAD, holds the full 6 s even though both nodes
reported decoded at 0.4 s, play goes out at 6.05 s; a warm start returns in
0.00 s and never arms; stop mid-arming emits `disarm` then `stop`.

**Still open** — the join burst spread for nodes arriving *mid-song*, which
takes the `_catchup` path and still gets 16 pings in one second. The arming
work covers the cold-start half of that problem only.

---

## 2026-07-28 (later still) — player status bar, wider control page

Small, additive, no server changes.

- **Activity bar on the player page.** A composite state line each device shows
  for itself: idle / arming / downloading N% / decoding / playing / playing +
  downloading next file N%. Everything is derived from state the player already
  holds, so the bar can't claim a phase the audio path isn't in.
- **Download and decode are tracked separately** (`dl` map, `decoding` set).
  From outside they feel identical — a wait — but they're nothing alike: one is
  the network, the other is ~85 MB of PCM on a busy main thread. On a phone the
  decode half is often the longer one, and a bar that called it "downloading"
  would send you looking at the Wi-Fi.
- Cleanup lives in `loadTrack`'s `finally`, so an abandoned or failed transfer
  can't strand the bar on "downloading" forever.
- Control page `.wrap` 980px -> 1080px.

Walked all ten states through the real `renderActivity` against a fake DOM
rather than eyeballing them in a browser: idle, unknown-size transfer, download
with %, armed+downloading, armed+decoding, armed alone, playing, playing +
prefetch, playing + decoding next, stopped.

---

## 2026-07-28 (evening) — adaptive ping cadence

**Prompt:** the control page, mid-song, four nodes. The number that mattered
wasn't `err`, it was **samples**:

| node    | n_used / n_samples | survival |
|---------|--------------------|----------|
| Laptop1 | 723/726            | 99.6%    |
| pc      | 545/726            | 75%      |
| phone   | 84/729             | 11.5%    |
| tablet  | 39/726             | 5.4%     |

The tablet's window is **not** starved — it holds as many samples as the laptop.
`filter_best` is discarding 95% of them, correctly: its RTT tail is long enough
that almost nothing lands near best. So the model fits on a twentieth of the
evidence for identical traffic.

**This corrects an earlier diagnosis in this log.** On 2026-07-28 morning I
wrote that `filter_best`'s 2 ms floor made the gate too *generous* for slow
nodes. In ratio terms it is (1.8× best for the tablet), but the observed tail is
so long that the gate still cuts 95%. The tablet doesn't need a different
filter; it needs more pings, because only one in twenty is usable. The
"re-tune filter_best" TODO item should be read with that in mind.

**Shipped**
- `ping_boost(n_used, n_samples)` — reciprocal of the survival rate, so a node
  needs 1/survival times the packets to end up with what a perfect link gets.
  Clamped at `PING_BOOST_MAX = 4.0`, and the clamp is the honest part: you can't
  ping a bad link into being a good one, and a node dropping 95% is telling you
  its radio is busy — more traffic past some point is just more queueing, which
  worsens the very tail that caused the problem. `PING_JUDGE_MIN = 40` samples
  before the rate means anything, so a bad first second can't lock in a boost.
- `boost_count` / `boost_interval` — sqrt each, so the product is exactly the
  boost while buying as much *spread* as depth. Splitting matters: all-depth
  gets more samples from the same instant, which is the failure we're escaping
  (a burst inside one bad power-save window is uniformly bad however big);
  all-frequency thins each burst until min-RTT filtering has nothing to choose.
- Recomputed every loop cycle, so it relaxes the moment a link does. This is a
  response to conditions, not a label on a device.
- `_note_boost` logs on quarter-steps (no chatter at a threshold, but a
  degrading or recovering link leaves a trail readable against the sync
  numbers). `pingBoost` in the snapshot; control tags the samples cell.

**Tests** — 106 passing, up from 85. `tests/test_ping_cadence.py` uses the four
real fleet numbers above as fixtures, so the tuning is anchored to a room that
exists, and pins the limits: ceiling, floor, monotonicity in survival, the
young-node guard, the sqrt split conserving the boost, the interval floor, and
a stated worst-case traffic bound (6 pings / 2.5 s, under 3/s).

**Smoke-tested** two simulated nodes with the observed survival rates through
the real `_ping_loop`, steady state after the join burst: laptop 0.45 pings/s at
1.00×, tablet 1.95/s at 4.00×. Usable-sample ratio between them moves from
0.05× to 0.23× — the tablet still gets less evidence than the laptop, which is
the honest outcome, but four times more than before.

**Sandbox note for future sessions.** The Linux sandbox has only Python 3.10,
where `asyncio.TimeoutError` is *not* the builtin `TimeoutError`, so
`_ping_loop`'s `except TimeoutError` doesn't catch and the loop exits after one
cycle. `pyproject.toml` correctly requires >=3.11 and the real conductor runs
3.14, so this is an artifact of the test environment, not a bug — but any async
smoke test run here must shim `asyncio.wait_for` to re-raise the builtin, or it
will silently measure one iteration and lie about the result. The first run of
this feature's smoke test did exactly that and reported a 1.16× ratio.

## 2026-08-02 — span beats count for the skew fit

No code. One number that changes how the idle time before a party should be
read, written down because it existed only in a conversation.

**The claim.** For the least-squares slope in `ClockModel.estimate()`, the
standard error of the fitted skew is

```
SE(skew) = σ / sqrt( Σ (tᵢ − t̄)² )  =  σ / ( s_t · √n )
```

where `σ` is the per-sample offset noise, `s_t` the spread of the sample
*timestamps*, and `n` the count. **Spread enters linearly; count enters as its
square root.** Doubling the baseline halves the error; matching that with
samples alone takes four times as many.

So 20 pings spread over 3 minutes fit a better slope than 200 crammed into 10
seconds — **5.7× better**, on a tenth of the traffic. Ten times fewer packets
and six times less error, because the only thing a burst can't buy is time.

**Why it matters here.** `min_slope_span = 30.0` and `min_slope_samples = 8`
are both guards, but they are not equal partners: the span guard is doing most
of the work, and the count guard is close to a formality. A node's skew
estimate improves with *time since join* far more than with how hard we ping
it.

Matthew's habit of parking a node on the join screen for 5–10 minutes before
hitting play turns out to sit exactly on the knee. Going from 1 minute of
history to 10 improves the skew error about **31×** (10× the baseline, ~10× the
samples). Past 10 minutes there is no further gain at all — `window = 600.0`
starts evicting from the tail, so the span stops growing and the estimate
plateaus. Five to ten minutes was arrived at by feel and is the right answer;
fifteen buys nothing.

**Playing time is calibration time too** — Matthew's observation, and it holds.
Idle cadence is ~3.7 pings/s (`BURST_GAP` every `INTERVAL_IDLE`), playback is
~0.57/s (`BURST_PLAYING` every `INTERVAL_PLAYING`), so a song accumulates about
6.5× fewer samples. But it covers the *same* 600 s baseline, and baseline is the
term that dominates: a window filled purely during playback is only ~2.6× worse
than one filled purely idle, not 6.5×. A long playlist keeps every node's fit
alive; nodes really do drift into better alignment as the night goes on, up to
the window ceiling.

**This is NOT an argument against the sqrt/sqrt cadence split.** Worth stating
plainly so a future session doesn't "fix" something that isn't broken.
`boost_interval` spreads bursts *within* a window that is already 600 s long —
its job is decorrelating samples across Wi-Fi power-save cycles, which attacks
the `σ` term (measurement noise). The finding above is about `s_t`, the
baseline, which cadence cannot extend because the window length sets it. Two
different terms of the same formula; the 2026-07-28 split stands.

**Parked, deliberately doing nothing:** a client-side "calibrated for N minutes"
readout on the player page. Display only — no gating, no policy, no colour that
means anything. It would simply make the one quantity that most determines a
node's skew quality visible to the person deciding when to hit play, and later
give something to compare a precision claim against. Not scheduled.

## 2026-08-02 (later) — straggler load gate

**Prompt:** this fell out of the static/dynamic discussion and outlived it. The
feature that started that conversation was solving a problem the star topology
had already solved; underneath it was a real one nobody had named. At a party,
the wandering phone can't corrupt anyone's clock — but it can absolutely make
everyone *wait*.

`_transport_play` gated on **all** connected nodes having decoded, up to
`LOAD_GATE_TIMEOUT` (12 s) or `LOAD_GATE_COLD` (20 s). And `plan_start` reads
`cold = not playing_now and not all(loaded)`, so one straggler also forced a 6 s
countdown on everybody else.

**The measurement that settled the design.** Running the *old* rule against a
simulated fleet with one node that never arrives: it waited the full 20 s and
then started without that node **anyway**. The wait bought nothing. It wasn't a
trade between "with" and "without" — it was twenty seconds of silence followed
by the same outcome. That is what makes this cheap to fix rather than delicate:
a node left behind is not left out, because it sends `loaded` on decode and
`_catchup` drops it into the song from there, the same path a mid-song joiner
already uses.

**Shipped**
- `hold_gate(n_ready, n_connected, quiet_for)` — pure, so the rule that decides
  how long a room stands in silence can be pinned without a network or a party.
  A **quiet period, not a deadline**: keep waiting while nodes are still
  arriving, stop once nobody new has for `STRAGGLER_GRACE = 4.0`.
- Why a quiet period and not a fixed grace. Two failure modes killed the simpler
  rules. Measured from the *first* node ready, a laptop holding a cached copy
  starts the clock at t=0 and strands the other three. Measured from the start
  of the wait, a fleet that is merely uniformly slow gets cut for no reason.
  Waiting on *progress* handles both, because it responds to what the fleet is
  doing rather than to a clock.
- The floor: below half the fleet ready we hold regardless of silence. That
  isn't a straggler, it's the room — starting for a minority is playing to an
  empty house. The hard timeout stays the outer bound in every branch.
- The countdown is still a floor no gate may undercut. A timer that hits zero
  early is a timer nobody trusts, which is the same rule the armed cold start
  was built on.
- `_transport_play` now tracks *arrivals* rather than the current ready count,
  so a node dropping off can't read as somebody turning up.
- Left-behind nodes are named in the log and in a control-page toast. A speaker
  that starts three seconds after the others is a visible event and wants a
  reason attached, or it looks like a fault.

**Tests** — 187 passing, up from 118. `tests/test_load_gate.py` pins the floor,
the grace boundary, both monotonicity properties (a node arriving may never
*extend* the wait; more silence may never turn a release back into a hold), and
one test per scenario the rule was designed against — the cached node, the
uniformly slow fleet, the lone straggler, the two-node pair.

**Smoke-tested** the real `_transport_play` against simulated fleets on scripted
load timings, old rule vs new:

| scenario                          | old     | new    |
|-----------------------------------|---------|--------|
| cold start, one straggler         | 20.03 s | 6.02 s |
| warm skip to unprefetched, ditto  | 12.04 s | 4.66 s |
| warm skip, uniformly slow fleet   | 4.06 s  | 4.06 s |
| cold start, cached node + 3 slow  | —       | 6.02 s, all 4 in |

The third row is the one worth staring at: identical to the character, which is
the point. A slow fleet is not a straggler and must not be treated as one. The
fourth confirms the floor does its job — the cached node cannot run off with the
music. Both straggler rows start the same 3 of 4 nodes the old rule started
after its full timeout.

**Note.** Cold starts all land at ~6.0 s because `ARM_SECONDS` is the binding
constraint once the gate stops being one — so the cold-start saving is capped at
`LOAD_GATE_COLD − ARM_SECONDS` and any further gain there has to come from
shortening the countdown, which is a different argument (that window is also the
calibration burst). Warm starts — ⏭ to an unprefetched track, seek, resume — have
no countdown, so they take the full benefit.

## 2026-08-02 (later still) — gate retune, and what the frozen 100% actually was

**Retune.** Matthew asked for the straggler wait to be longer: cold 6 → 15 s,
warm 4.7 → 10 s. A flat `STRAGGLER_GRACE = 4.0` was too eager for a real tablet
on a real radio — a simulator's straggler either arrives or doesn't, but his
arrives *late*, and 4 s of quiet was cutting a node that was still coming.

The grace is now `STRAGGLER_GRACE_FRAC = 0.75` of **that start's own gate**:
15 s of a cold start's 20, 9 s of a warm start's 12. Tied to the gate rather
than fixed, because the two starts are different bets — a cold start has already
committed the room to a wait, so patience is cheap there, while a warm skip
interrupts music that was playing and every second of silence is felt.
`hold_gate` now takes the grace as an argument instead of reading a global, so
the rule stays pure and the tuning lives beside the timeouts it scales against.

Re-measured, same scenarios: cold straggler **15.67 s** (was 6.02, originally
20.03), warm skip **9.63 s** (was 4.66, originally 12.04). The cases that were
already right are untouched — uniformly slow fleet 6.04 s / 4.06 s with all four
nodes in, cached-node-plus-three-slow 6.02 s with all four. 188 tests.

Honest note on the trade: the cold-start saving is now 4.3 s rather than 14, so
most of what the gate buys has moved to the warm path — ⏭ to an unprefetched
track, seek, resume. That is Matthew's call about his own house and his own
tablet, and the knob is one number.

**The screenshot.** Four nodes mid-song: pc, phone and laptop all playing at
2:56 with err 0.5/0.5/0.0, and the tablet showing a frozen `100%` with `—` for
both err and position. Downloaded, apparently fine, actually sitting the song
out. Matthew's own guess — "I often rebalance the queue and the next file
preload suffers because I do" — turned out to be exactly it.

Every queue edit calls `_prefetch_next()`, which broadcasts a `preload`. The
player handles that with `case "preload": loadTrack(msg.trackId, msg.url);` —
**fire and forget, not awaited, never cancelled**. The `loading` set only
dedupes the *same* track id, so reordering the queue several times starts
several concurrent 30 MB fetches and several concurrent `decodeAudioData` calls,
each yielding ~85 MB of PCM. On a tablet that is an out-of-memory decode
rejection; `loadError` fires and the node is out of the song entirely.

The frozen `100%` was a second bug on top: `loadError` cleared nothing, so
`load_track`/`load_pct` kept their last values forever and the pill went on
claiming a finished download. Fixed here — a failed load now retires the whole
pill, so a node that gave up reads as not ready instead of as ready-and-idle.
That is the display half. **The cause is still live**, laddered in TODO: one
prefetch at a time via `AbortController`, serialised decodes as the real OOM
guard, an `unloaded` message so `node.loaded` stops claiming buffers the player
has evicted, and one retry on failure. Those are `player.js`, so they need a
fleet reload and a party rather than a simulator to verify.

## 2026-08-02 (evening) — loader hardening: one load, one decode

**Prompt:** the screenshot from earlier today, and Matthew's own read of it —
"I often rebalance the queue and the next file preload suffers because I do".
He was right, and the code says so plainly.

`_prefetch_next()` is called by every queue edit — `queue`, `unqueue`,
`queueMove`, `queueClear`, `rescan` — and broadcasts a `preload`. The player
handled that with `case "preload": loadTrack(msg.trackId, msg.url);`. Not
awaited. Never cancelled. `loading` dedupes only the *same* track id, so N
rebalances started N concurrent 30 MB fetches and N concurrent
`decodeAudioData` calls, each yielding ~85 MB of PCM.

**Shipped (steps 2 and 3 of the ladder)**
- **A guess is cancellable; the real thing is not.** `preload` now carries
  `prefetch: true/false`. Only `_prefetch_next` sets it — the preload
  `_transport_play` sends, and the one a joining node gets for the active track,
  are demand loads. The player keeps at most one speculative load in flight and
  aborts it the moment a newer preload arrives, *including a demand one*, which
  is exactly the "rebalance the queue then hit play" case: the guess must never
  compete with the file the room is waiting on.
- **Promotion.** A guess for track X followed by a demand load for the same X is
  promoted rather than restarted — it stops being cancellable, so a later guess
  can't kill the load that is now load-bearing. This is the case that made a
  simple "newest wins" rule wrong, and it has its own check.
- **Serialised decodes.** `decodeSerial` chains every `decodeAudioData` behind
  the last, and re-checks the abort signal before spending the memory — a load
  cancelled while queued must not decode anyway. This is the actual OOM guard;
  cancelling fetches only stops the pile-up forming.
- **An abort is not an error.** `AbortError` no longer sends `loadError`, or the
  conductor would toast a failure every time the queue was reordered and mark a
  node bad for doing what it was told.
- An older player page ignores the new field and behaves exactly as before, so
  the upgrade is safe to roll through the fleet one reload at a time.

**Verified sans party.** A harness brace-matches the shipped `onPreload`,
`loadTrack`, `decodeSerial` and `fetchWithProgress` straight out of `player.js`
and runs them in a VM against a stubbed abortable fetch and an instrumented
decode, so it exercises the real code rather than a paraphrase. Old vs new on
identical checks:

| scenario                          | old            | new            |
|-----------------------------------|----------------|----------------|
| 5 queue rebalances                 | 5 tracks loaded, **peak 5 concurrent decodes** | 1 loaded, 4 aborted, **peak 1** |
| 4 overlapping demand loads         | peak 4 decodes | **peak 1**, all 4 still load |
| guess superseded by another guess  | both loaded    | old aborted, no `loadError` |
| guess superseded by a demand load  | not cancelled  | aborted |
| guess promoted to demand           | (n/a)          | survives a later guess |

Five concurrent decodes is ~425 MB of PCM on a device that has no business
holding one. That is the tablet's out-of-memory failure, reproduced and then
removed.

**Caveat, stated plainly.** This is `player.js`, so every node needs a page
reload before it takes effect, and the honest test is a party rather than a
simulator — the harness proves the concurrency arithmetic, not that a real
tablet survives a real evening. Ladder steps 4 (an `unloaded` message, so
`node.loaded` stops claiming buffers the player has evicted) and 5 (one retry
with backoff) are still open.

## 2026-08-02 (late) — loader ladder finished: retry, and telling the truth about evictions

Steps 4 and 5, closing out the loader work started earlier this evening.

**Shipped**
- **`unloaded` on eviction.** The player keeps two decoded buffers and drops the
  rest; it had been doing that silently. `node.loaded` is the conductor's *only*
  view of what a node holds, and nothing else can contradict it — so a stale
  entry made the load gate count a node ready, skip it, and let the play command
  arrive at a node with no buffer, which then joined late through `_catchup`
  instead of starting with everyone. An eviction is a fact only the node knows;
  it now reports it. The playing track and the one just loaded are never
  candidates, so this can only ever retire a buffer nobody is using.
- **One retry, then give up and say so.** A decode can fail for reasons that
  pass — a transient memory squeeze, a blip mid-transfer — and giving up
  permanently on the first cost the node the whole song. Retrying forever would
  be worse: it turns one struggling device into a machine for hammering the link
  that is already the problem. So exactly one, after 1.5 s. An abort is never
  retried, because an abort is a decision rather than a fault.
- `loadTrack` split into a retry wrapper and `loadOnce`, so the policy lives in
  one place and an abort stays distinguishable from a real failure all the way
  up the stack.
- **The conductor reads a retry correctly.** Download progress is monotone
  *within* an attempt, so progress running backwards means exactly one thing: a
  retry restarted the transfer. `decode_since` now clears on that, or the pill
  would go on claiming a decode that had already failed and count the re-download
  as part of it.

**Verified** — 193 Python tests (up from 188) and 29 harness checks (up from 17):
retry loads on the second attempt with no `loadError`; two failures give up
exactly once and report exactly once; an aborted guess is never retried; an
eviction is reported, never for the playing track, and leaves the cache at two.

**One bug the harness caught on itself**, worth recording because it nearly
passed silently: the sandbox had no `setTimeout` of its own. The stubs closed
over Node's timers so every earlier check ran fine, but the retry backoff is the
first code to call a timer from *inside* the evaluated `player.js`, and it threw
`ReferenceError` the moment it was exercised. A harness that only tests the paths
it already had stubs for will report health it hasn't measured.

**Still true, and still the real test:** this is `player.js`, so every node needs
a reload, and none of it has met a party. The harness proves the arithmetic of
concurrency and failure handling; it does not prove a tablet survives an evening.

## 2026-08-22 — the ± column: every node's offset now carries its own certificate

Three weeks of parked questions had the same shape — *how much should I trust
this node's numbers?* — and the answer was already sitting in the data. A ping
measures the offset with an error equal to half the path asymmetry, and
asymmetry cannot exceed the round trip: `|d_out − d_ret| <= rtt`. So every
sample arrives carrying a **certificate of its own worst-case error**,
`|offset error| <= rtt/2`. No model, no assumption about the noise — a bound the
measurement proves about itself. The node table now renders it as one cell:
`± 0.76`, sitting immediately right of the offset it bounds.

**The one real judgement call: `worst_rtt/2`, not `best_rtt/2`.** The TODO wrote
this item as "±rtt/2" and min-RTT filtering makes `best` the tempting reading —
but the estimate is a median (or a least-squares fit) over *every* sample that
survived `filter_best`, not over the best one. It therefore lies among the
survivors and inherits the bound of the **widest** one admitted. Quoting
`best_rtt/2` would flatter exactly the node whose filter gate is widest open,
which is the node the column exists to catch. Both numbers ship: the honest
bound is the cell, the floor (`best_rtt/2`) is in the tooltip, and the gap
between them is precisely what the filter's tolerance costs.

That gap is not theoretical. A loopback node on the bench: `best rtt 0.54 ms`
→ floor **±0.27**, but 56 of 56 samples admitted with a worst of `1.51 ms` →
the true bound is **±0.76**, 2.8× the flattering number. The 2 ms absolute term
in `cutoff = best + 0.002 + 0.25·best` admits everything when best is small,
and now that shows up as a number instead of a hunch — which hands the parked
`filter_best` re-tune the scoreboard it was missing. A test pins that too: same
samples, two tolerances, and the loose one must report a wider bound while both
quote the same floor.

**How to read it, and what it settles.** Compare `err ms` against `±`:
- `err` **inside** the bound → the servo is chasing measurement noise. A nudge
  would be biasing against a number that averages to zero. Fix is more/better
  samples (the adaptive cadence already does this).
- `err` **outside** the bound → the offset is not the explanation; something
  real is displacing this node, i.e. output latency the `getOutputTimestamp`
  mapping isn't catching. Fix is a nudge.

That is the tablet's `+6.5 ms` question from 2026-07-28, which has sat parked
with two competing hypotheses and no way to separate them. One glance now
separates them, because the tablet's `best rtt 3.8 ms` implies a bound around
±2 ms — and 6.5 is well outside it. Re-measure before concluding: that reading
predates the adaptive cadence.

**Honest limits.** The bound is on the offset at its anchor `at`; projecting it
forward by `skew` adds error this number does not model. And it bounds the
*clock estimate*, not the whole chain — the ± is not a claim about speaker
latency, buffer scheduling, or air.

**Shipped:** `ClockEstimate` gains `worst_rtt` (one `max()` next to the existing
`min()`) plus three read-only properties — `trust_s`/`trust_ms`/`floor_ms` —
alongside `skew_ppm`. No call site changed, no code path branches on any of
them: this is arithmetic *about* the estimate, and the timing core computes
exactly what it computed yesterday. `stats()` exposes `trustMs`/`floorMs`/
`worstRttMs`; the control page adds a column and three CSS colours (good < 1 ms,
fair < 3 ms, poor beyond). `best rtt` stays where it was — nothing was removed.

**Conductor + control only. `player.js` is untouched, so no fleet reload** —
which was the point of picking this one: the fleet already owes a reload for
the loader work, and this shouldn't add a second reason.

**Verified:** 201 tests (up from 193). Seven new in `tests/test_trust.py`, and
the one that matters is a randomized adversarial search — 1000 windows, random
round trips, and each leg split anywhere from symmetric to entirely one-sided —
asserting the estimate's *actual* error never exceeds the bound. A worst-case
window (every sample all-outbound) proves it's reachable and not merely safe: a
bound nothing can approach would be useless. Plus: a spike rejected by the
filter cannot widen the bound; a node with no surviving samples reports `None`,
never `±0.00` (a node nobody has timed must not own the most confident cell on
the page). Live over `:8931` with a real joined node: the numbers above, all
four cell states rendered from an injected snapshot (—/good/fair/poor), tooltip
correct, no console errors.

**For meatthread0:** control page reload only, nodes can stay as they are. Worth
one minute at the next play: read the tablet's `±` against its `err`.

## 2026-08-23 — a fitted drift now has to clear its own error bound to be banked

`remember_skew` would carry any fit it could get into the node's next session,
and `MAX_PERSISTED_SKEW` (500 ppm) was the only thing standing in the way — which
is to say nothing was, since every plausible impostor is well inside 500 ppm. The
TODO asked for "a minimum fit quality (residual RMS, or R²)". I built something
else, because **the dangerous bad fit is not a noisy one.**

**The premise, and it's now a test.** A device carried across the room has a
perfect clock and a changing *path*. Asymmetry sweeps as it moves, the offsets
follow it, and the result is a straight line: over a 60 s window, an apparent
**83 ppm** of drift at **R² = 1.00000**. Ordinary-crystal territory, textbook fit.
Every residual- or R²-based quality gate waves that through — enthusiastically,
because it is the cleanest data in the file. Then it seeds the node's next
session, where nobody is watching.

**What actually separates them is the certificate from yesterday's ± column.**
Perturbing each offset by `e_i` moves the least-squares slope by
`Σ(t_i − t̄)·e_i / Σ(t_i − t̄)²`, and every `|e_i|` is bounded by `rtt_i/2` — so
the worst slope that measurement error alone could manufacture is exactly
`Σ|t_i − t̄|·(rtt_i/2) / Σ(t_i − t̄)²`, computed from the same two sums the fit
already needs. Call it `skew_bound`. A slope that **exceeds** it cannot be
asymmetry, so it has to be the crystal. A slope **inside** it has proved nothing,
however straight it looks.

That is a proof obligation rather than a heuristic, and it catches every
impostor, not just the walking kind — which is why it beats the movement flag
that was considered and dropped back on 2026-08-02.

**The numbers.** The walk above: 83.33 ppm against a bound of ±120.97 — ratio
0.69, refused. A real 20 ppm tablet on a 1 ms link over ten minutes: bound
±2.44 ppm, ratio **8.2**, banked. And the impostor cannot win by lasting longer:
the same walk over 300 s and 600 s reads 16.7 and 8.3 ppm against bounds of 24.2
and 12.5 — **ratio 0.69, 0.67, invariant**, because asymmetry is capped by the
round trip so the fiction it can support shrinks exactly as fast as the bound
does. What *does* earn a tighter bound is span: four times the window, a quarter
of the bound, since the error is fixed and the lever arm isn't.

**Scope, deliberately narrow.** This gates **persistence only**. The live model
goes on using its fit either way, so a refusal cannot change how any node is
timed this session — it decides what the node inherits *next* time, which is the
value nobody is watching when it turns out to be wrong. Cost of refusing wrongly:
one session of re-learning a drift. Cost of accepting wrongly: a wrong number
seeding every session after it. Hence `MIN_SKEW_SNR = 2.0`, modest on purpose.

**Made visible.** `drift ppm` now renders **dim** when the fit hasn't cleared its
bound (or no slope has been fitted at all — a prior, or a young node's flat 0.0),
bright when it has. Only a bright number is one the node will inherit. The
tooltip gives the fit, the bound, and which side of the line it fell. The
conductor also logs every refusal with its numbers, so a node that never banks
anything says why.

**Verified:** 211 tests, up from 201. The maths: an adversary is *constructed* —
every offset pushed by its own full `rtt/2` in the direction that tilts the line
hardest — and the resulting slope must equal `skew_bound` to 1e-9, so the bound
is neither a lie (exceeded) nor slack (loose enough to admit an impostor). Then
the policy: a walk is crystal-sized and R² 1.0 *and* refused; refused at three
different spans; a real 20 ppm crystal still banked; a refusal leaves the
previous value alone rather than clearing it; and an unfitted estimate is never
credible (not "0 ≥ 0"). A zero-rtt window certifies any slope, which is right —
with no measurement error there is nothing to explain a trend away — and is also
what keeps every pre-existing synthetic fixture passing.

Live on `:8931` with a joined node, and it produced the right answer on its own:
a loopback node — whose "crystal" *is* the conductor's clock, so its true drift
is zero — fitted **-0.2 ppm against a bound of +/-5.8 ppm** over a 225 s window,
rendered dim, refused, and wrote **no skews entry**. All four cell states checked
(no fit / walker / crystal / no data), and the conductor logged
`bench-b: not banking -0.2 ppm - inside its own error bound (+/-5.0 ppm over
225s, 821 samples); keeping nothing`.

**One thing the live check caught that 211 green tests could not:** the first
draft of the render carried a JS syntax error — a string literal broken across a
line — and `control.js` did not parse at all. Every Python test still passed,
because none of them load the page. `node --check web/control.js` is a one-second
guard and belongs on every control-page slice; it is now how this one was
confirmed before the browser ever saw it.

**Conductor + control only. `player.js` untouched, so no fleet reload.**

**Still open, and now more visible than it was:** `_state["skews"]` has no clear
path (`nudges` and `eqs` both delete their entry when cleared), so a bad value
banked *before* this gate existed still outlives every session. Next slice.

## 2026-08-23 (later) — a way out: forgetting a remembered drift

Yesterday's gate stops a bad drift getting *in*. It does nothing about the ones
already on disk — and `_state["skews"]` had no exit at all. `nudges` and `eqs`
both `pop()` their entry when cleared; skews only ever grew, so one wrong value
seeded that node's every future session with no way to say otherwise short of
hand-editing the state file. `_clean_skew` filters at load, but only for junk and
absurd magnitudes: a plausible-looking wrong number sails straight through.

**Three things had to be cleared, and they are genuinely three.** The value the
node will bank (`Node.prior_skew`), the entry on disk (`_state["skews"]`), and
the prior the *live* `ClockModel` is coasting on. Clearing only the first two
leaves the node steering on the bad number for the rest of the session, which is
the very thing you clicked the button about. So `ClockModel.forget_prior()`
drops it and invalidates the cached estimate — and it is inherently a no-op once
the window fits a slope of its own, because a fit outranks a prior anyway. It
bites in exactly one case: a node still coasting. That is the case worth fixing.

**The behaviour worth knowing about before it surprises you.** `_save_state()`
re-banks from live models, so forgetting on a node that is *right now* measuring
a credible drift puts a value straight back. That is correct — a measurement
beats a memory, and the new number was just verified against its own error bound
— but it means the button looks like it did nothing on the best-behaved node in
the room. So it says what happened out loud, in the toast and the log:
`Forgot tablet's drift (was 130.0 ppm) - re-learned 20.1 ppm from this session.`
Both outcomes are pinned by their own test rather than left to be discovered.

**On the control page** the `⌫` appears in the drift cell *only* when there is
something to forget — `rememberedSkewPpm` is non-null. That is deliberately a
different number from the live fit next to it: a node can be measuring one drift
and remembering another, and that divergence is precisely the state you want to
be able to see and clear.

**Known limit, and it's honest to state it:** you can only forget a node you can
see. An entry for a device that never reconnects stays in the file — harmless,
since a skew only ever seeds the node it belongs to, but not reachable from the
page either.

**Verified:** 217 tests, up from 211. Six new, driven through the real control-
command surface with `STATE_FILE` monkeypatched to a tmp path (the live state
file is never a test fixture): the entry actually leaves the file when the prior
is cleared — the bug itself, pinned; forget clears all three places and the live
estimate drops to 0.0 skew; a credible fit replaces rather than empties; an
uncredible one leaves the node clean; an unknown node id is shrugged off without
touching anyone else's entry; and `forget_prior` is a no-op once fitted.

Live on `:8931`: the button is absent on a node with nothing remembered, appears
with the right tooltip once `rememberedSkewPpm` is set, and emits exactly
`{cmd: "forgetSkew", nodeId}`. End to end against the running conductor, the
command logged `bench-c: forgot remembered drift (was nothing); nothing
remembered now`. The real `syncplay_state.json` was backed up first and came
through content-identical — skews, nudges, eqs and volumes all unchanged.

`node --check web/control.js` earned its place again: the first draft of the
button had the same broken-string-literal fault as yesterday's, and the syntax
check caught it before the browser did.

**Conductor + control only. `player.js` untouched, so no fleet reload.**

## 2026-08-23 (evening) — two silences given a voice

Both of these were on the small-fixes list, both are the same bug in different
clothes: the system knew something had gone wrong and didn't say so.

**A start that reached nobody.** `_transport_play` ends by sending play to every
ready node and logging who got it — `", ".join(started) or "nobody"`. That
`"nobody"` is a real outcome: `_send_play` refuses a node with no clock estimate,
so if *every* ready node is untimed, nothing starts. `self.playing` is set, the
countdown is retired, the page says a track is playing, and the room is silent.
The no-load path immediately above it toasts; this one only logged, so the single
failure that leaves state lying was the one with no signal on the page. It now
toasts and logs at warning, and the message says what to do — the condition is
transient, so "give it a few seconds and press play again" is genuinely the fix.

**What I deliberately did not do:** tear the playback state back down. It is
tempting, since the conductor is claiming something untrue — but a node that
later reports `loaded` gets pulled in by `_catchup`, which waits up to 5 s for its
own estimate to converge. Keeping `self.playing` is what leaves that door open;
clearing it would close the recovery path to fix the cosmetics. So: say it out
loud, leave the machinery alone.

**A probe that could eat a rep.** `_measure_pending` holds exactly one probe, and
whoever writes it last wins — the loser waits out `MEASURE_TIMEOUT` and is
dropped. `_measure_all` guarded itself against a second sweep; `_measure_one`
guarded nothing. So a 📏 pressed during a sweep silently cost that sweep a rep,
which is precisely what `_calibrating` exists to prevent, and the sweep would go
on to report a median over fewer readings than it claimed. Two rapid 📏 clicks
collided the same way, with no flag involved at all.

Three guards, because the hole is symmetric: `_measure_one` refuses during a
sweep, `_measure_one` refuses while another probe is in flight, and `_measure_all`
refuses while a manual probe is in flight. That last one is slightly past the
literal TODO item and is included on purpose — a sweep starting on top of a
manual probe loses its own *first* rep, which is the same fault read backwards.

**Verified:** 224 tests, up from 217. Seven new in `tests/test_guards.py`, driven
through the real `_transport_play` and `_measure_*` with the load gate shrunk to
a blink: a play that times nobody toasts and names the track; a normal start says
nothing extra (the guard must not fire on the happy path); the no-load failure
keeps its own distinct message rather than being absorbed into the new one; each
of the three measurement collisions is refused with the in-flight probe intact;
and an ordinary probe with nothing in flight still arms, so the guards don't lock
out the case they exist to protect.

Live on `:8931`, playing a real track on a joined node: `play "02 - Out Of Time"
at T+1.8s seek=0ms -> bench-d`, no spurious toast, node state `♪ playing`. Its
`err ms` came in at 41 and the servo hauled it to 28 then 18.5 while sitting on
its -800 ppm rail — that opening offset is a sandbox-browser artefact (this tab's
audio clock is not a real output device) and the convergence is the servo doing
exactly its job. Nothing here touches that path: the only change inside
`_transport_play` is which branch runs *after* the play commands have gone out.

## 2026-08-23 (late) — auto-nudge: making a failed measurement diagnosable

Starting on auto-nudge again, and the first slice is not step 4. Step 4 is
*apply*, and applying proposals that have never met air is building on a floor
nobody has stood on. The thing actually blocking this ladder is one hardware run
— and the last attempt at one, back in July, produced a month of ambiguity
instead of an answer. That is the fixable part, so that is what got fixed.

**The flaw, and it is a real one.** `crossCorrelate` returns a *normalized*
correlation: `dot / sqrt(sigE * refE)`. Normalizing by the signal's own energy is
right for finding a shape regardless of level — and it means the peak carries
**no information about whether the microphone heard anything at all**. Silence
correlates against the chirp just as willingly as a chirp does; it simply
correlates badly. So `peak 0.02` was reported for a dead input, and `peak 0.02`
would be reported for a live mic in a room where the chirp never arrived. Two
faults, opposite fixes — one is Windows, one is the room — and one number that
cannot tell them apart. In July we spent the difference.

**The fix is to report the level, because nothing else can.** `finishCapture()`
now also computes the capture's RMS and peak in dBFS plus the fraction of samples
at full scale, and sends them with the result. A failed probe is then named by
its cause:

- `mic heard nothing (-74 dBFS) - check the input, not the room`
- `input clipping (14% of the window) - turn the mic gain down`
- `no usable capture` — kept, and now it *means* something: the level was
  healthy and the correlator genuinely missed. That one is a room problem.

The threshold is -60 dBFS. A working mic in a quiet room still picks up its own
noise floor and the building; July's dead reading was -74, and the same laptop
had read -43 when it was alive. -60 sits in the gap with room on both sides.

**Level never decides whether a reading is believed.** `CAL_MIN_PEAK` remains the
only gate, and there is a test asserting that a quiet-but-clean capture still
proposes exactly what it proposed before. This slice adds a diagnosis, not a
policy — a quiet capture that correlates at 0.9 is a measurement, and treating it
as suspect would be inventing a rule to solve a problem we do not have.

The level shows on every row, not just failures, because a sweep working at
-55 dBFS is one bad evening from not working, and the `in level` column is where
you would see that coming.

**Verified:** 248 Python tests, up from 224 (18 new), plus a 13-check JS harness
now kept at `tools/level_harness.js` rather than in a scratch directory — the
loader harness taught that lesson. It runs the **shipped** `captureLevel` pulled
straight out of `player.js` and holds it to levels known by hand: digital silence
reads the -120 floor; a full-scale square reads 0 dBFS RMS, 0 dBFS peak, 100%
clipped; a 0.1-amplitude sine reads -23.0 RMS against -20.0 peak (that 3 dB gap
is the proof the two are genuinely computed separately, not one copied into the
other); an empty capture stays finite instead of going NaN. Then the two numbers
that matter: a synthesized -43 dBFS room clears the silence gate, and a -74 dBFS
one is caught by it. Those are July's actual readings, alive and dead.

On the Python side: each of the three failures gets its own name, a successful
sweep is unchanged, level never moves a proposal, and a player page too old to
report a level degrades to the old wording rather than erroring. The dBFS and
percentage both arrive over a socket, so both get clamping validators with their
own parametrized tests — 12 dBFS is impossible and is clamped rather than
rejected, NaN and rubbish become None.

Live on `:8931`: all four row states rendered (healthy / silent / clipping / no
data), the right colours, and the single-shot 📏 readout carries the level too.
No mic in the sandbox, so what was checked is the plumbing and the arithmetic —
the acoustic loop remains untested, which is the entire point of this slice.

**This one touches `player.js`, so the fleet does need a reload** — the first of
these slices that does. It rides along with the reload already owed for the
loader work.

**For meatthread0 — this changes what the first hardware run tells you.** Run it
exactly as before (conductor's own browser at `localhost`, JOIN, "use as
calibration mic", two devices as speakers, stop playback, 📐 calibrate all). The
new column is `in level`. If it reads red and says *silent*, stop: it is the
Windows input, not SyncPlay, and no amount of re-running will help. If it reads
green and the peaks are still low, that is a genuine acoustic miss and worth
chasing — placement, speaker volume, a door. Either way you now get an answer
rather than a mystery.


## 2026-08-27 — the tablet's `err`, re-measured: the ± test doesn't decide it

Matthew posted a live control panel and two mesh-truth snapshots, with one
observation: the tablet "tends to get most of its samples during file transfer,
like timing data". That line is what made the session worth having — it sent me
to check whether sample clustering was corrupting the tablet's fit, and on the
way there I found that the test the TODO had been saving up for this moment
cannot answer the question it was built for.

**The mesh certifies every drift figure, including the tablet's.** The two
snapshots are one measurement with six constraints and a single free parameter
(the interval between them). Fitting that one number gives 467 s and leaves
residuals of at most 0.25 ms across all six pairs:

    laptop<->pc      10.75 measured   10.55 predicted   +0.20
    phone<->pc        6.52             6.77             -0.25
    laptop<->phone    3.73             3.78             -0.05
    pc<->tablet      -0.99            -1.17             +0.18
    phone<->tablet    5.64             5.60             +0.04
    laptop<->tablet   9.36             9.38             -0.02

The pair `ClockModel`s share no conductor-clock data — WebRTC DataChannel pings,
peer to peer — so this is the independent referee the sync-engine item wanted,
used here for the first time in anger. It pins the tablet's slope to well under
1 ppm, which retires the clustering worry directly: `trust_s` bounds the offset
at `t_mean` and says so in its own docstring, so a node whose samples bunch up
pays an extrapolation cost of `skew_bound x (now - t_mean)` that the ± column
does not show. For this tablet that term is under 0.5 ms even across the whole
7.8 minutes. The slope is fine. The clustering costs survival rate, not accuracy.

**`err ms` is nudge-invariant, and that is the finding.** `nudgeMs` enters
through `perfToCtx(atNodeMs + nudgeMs)` in both `startBuffer` and `onSteer`, and
falls straight out of the difference:

    posAt(targetCtx) - ideal = seekS + (C2 - C1 + (A2 - A1)/1000) * rate - ideal

No `N`. This is correct and deliberate — a nudge is a fixed acoustic offset and
the servo must not spend its rate trim fighting one — but it means the TODO's
hypothesis 1 ("parks at +6.5 -> constant output latency, fix is a nudge") was
never coherent as written. A nudge moves when sound leaves the speaker; `err`
is computed in a frame where that move is invisible. So the ±-column test —
*outside the bound -> it's real latency -> nudge it* — decides nothing, because
the second arrow doesn't exist. **Do not nudge the tablet.** There would be no
way to verify it and the number would go on reading 6.

**What survived the cancellation is the better lead.** `C2 - C1` is the drift of
the node's own `getOutputTimestamp` mapping between two steers, and it enters
`err` at full weight. `err ms` is the *last* steerAck, not an average, and at
6 ms the servo pulls 400 ppm against an 800 ppm cap — not saturated, and enough
to null 6 ms in ~15 s. So something re-injects it faster than the servo removes
it, and a coarsely-quantised output timestamp on an Android tablet is exactly
the shape of thing that would. Hypothesis, not a finding: it needs the mapping
logged, not reasoned about.

Nothing was changed on the fleet and no code was touched. The TODO item has been
rewritten to say what it now knows.

## 2026-08-27 (cont.) — what the mesh RTTs are actually made of

Matthew's read of the mesh table: phone<->tablet is the worst pair because both
are wireless ARM devices, so a round trip is device -> AP -> device -> AP ->
device, and neither has spare threads for interrupts — where the laptop is also
wireless but has 16.

The additive shape is right and the table has enough constraints to check it:
six pairs over four nodes. Three corrections came out of doing so.

**The DataChannel is not free, and it is separable without any fitting.** Same
pair, two transports: laptop<->pc reads 2.65 ms on the mesh vs 1.90 ms on the
conductor WebSocket; laptop<->phone reads 3.95 vs 3.10. Two independent pairs
agreeing to 0.1 ms, so ~0.8 ms of every mesh cell is SCTP/DTLS on the two ends.
The mesh `rtt ms` column and the `best rtt` column beside it are therefore *not*
directly comparable, which is worth a tooltip at some point.

**The ARM penalty is real but small.** Fitting one-way endpoint costs against
only the well-sampled constraints (n_used >= 65) gives laptop 0.87, pc 1.16,
phone 2.34, tablet 2.33 ms. The two ARM devices land on the *same* number and
sit ~1.4 ms above the laptop — not the 3x the raw table suggests. (The tablet's
term is pinned by a single constraint and is the weakest of the four.)

**Most of the "massive" 7.9 ms is small-n, not physics.** `rtt ms` is
`best_rtt`, a *minimum*, and the tablet's mesh pairs carry n_used 14-26 against
laptop<->pc's 115. A minimum over 20 draws sits well above a minimum over 115.
Against the fitted floors: pc<->tablet +0.86, laptop<->tablet +2.05,
phone<->tablet +3.23. That last pair has the fewest samples *and* the two
slowest nodes, so it eats both. Its 6.9 -> 8.9 swing between the two snapshots
is the same effect; laptop<->pc moved 2.6 -> 2.7.

**The part that matters for timing.** Asymmetry decomposes as
`(A_tx - A_rx) - (B_tx - B_rx)` — a node's total endpoint cost *cancels*, and
only its tx-vs-rx imbalance reaches the offset. Slow-CPU cost is roughly
symmetric and buys no offset error. Wi-Fi power-save is receive-side only (the
AP buffers downlink to the next beacon) and is pure asymmetry. Those separate
along exactly the line the ± column already draws: `best_rtt` is a minimum so it
catches the awake windows and is dominated by the symmetric floor, while
`worst_rtt` catches the parked ones and is dominated by the asymmetric tail.
`trust_s` uses `worst_rtt`. It was aimed at the right quantity, and now has a
mechanical reason rather than only the bounding argument.

Where it does bite is `filter_best`: `cutoff = best + 0.002 + 0.25 x best`
scales the gate off `best`, i.e. off the component that contains no asymmetry at
all. The re-tune item now has that argument written into it. Nothing was
changed — this is all reading, and the ± column should keep its certificate.

## 2026-08-27 (cont.) — `err ms` is not a fault report, it is a measurement

Continuing the tablet. A ten-agent pass (four mappers, four adversarial lenses,
a designer and a completeness critic) was pointed at one hypothesis, and the
notable result is that **0 of 4 lenses refuted it — including the one whose only
instruction was to refute it.**

**The law.** The servo is proportional-only: `rate = 1 - errS/STEER_HORIZON_S`
is an assignment from the instantaneous error, and `current.rate` is written in
exactly two places in `player.js` (line 441, `rate: 1` at source start, and line
484). No accumulator exists anywhere. The re-anchor `anchorPos = posAt(nowCtx)`
looks like integration and is not — it carries the *plant's* state across a rate
change, losslessly, and never sees `errS`. So the integrator is the plant, the
controller is proportional, and the loop is type 1: zero steady-state error to a
step, standing error to a ramp.

    de/dt = (rate - 1) + eps = -e/H + eps   =>   e_settled = H * eps

With H = 15 s, a settled 6.0 ms error means eps = 400 ppm, every time. One lens
verified this by transcribing `posAt`/`anchorPos`/`anchorCtx` line-for-line and
simulating: a 6 ms *step* decays to 0.0003 ms in 200 s, a 400 ppm *ramp* settles
at 5.9976 ms and stays. Cadence-invariant at dt = 1, 2 and 4 s, because dt
cancels. That is the whole reason +6.5 (July, 1.0x cadence) and +6.0 (August,
4.0x) are consistent readings rather than a coincidence.

**Two things I had wrong, both now dead.**

- *"Coarsely-quantised getOutputTimestamp"* (previous entry) is refuted outright.
  Both fields of the pair are written from the same audio callback, so the pair
  is self-consistent and staleness costs staleness*eps — 40 microseconds at
  400 ppm. Quantisation of a correlated pair cannot produce 6 ms. Only the
  *fallback* branch quantises harmfully, and its sawtooth would be tens of ms
  and would trip `REANCHOR_S` constantly, which is not observed.
- *"HAL resampling at a rounded ratio"* is wrong by roughly four orders of
  magnitude: AudioFlinger-class resamplers carry a 32-bit phase increment, so
  ratio quantisation is ~0.01 ppm, not hundreds.

**The estimator is exonerated, three independent ways.** This matters because it
is the one hypothesis that would have made the fix server-side.

1. *Arithmetic.* eps = eps_C + fitted_skew. The tablet's fitted slope is
   20.1 ppm, so it supplies 0.30 ms of the 6.0.
2. *The code's own ceiling.* The largest slope that measurement error can fake
   is `skew_bound = worst_rtt/span`, and `min_slope_span = 30 s` is a hard
   floor — so estimator-driven droop cannot exceed
   `H*rtt/span = 15*6.04ms/30s = 3.02 ms`, which is *identically this node's own
   trust bound*. Observed 6.0 ms is 1.99x that ceiling. A pleasing result: the
   ± column turns out to bound the servo too, not just the offset.
3. *Simulation.* Fed the real `ClockModel` an adversarial stream (5.4% survival,
   sparse awake windows, per-episode asymmetry flips, `filter_best` cutoff moved
   to mass-evict clusters): worst single refit jump 8.94 ms, worst projection
   error 10.66 ms, and yet mean err held +0.287 ms with 0 of 7008 samples past
   +5.5 ms across 12 seeds. Refit steps are the derivative of a bounded signal
   and are zero-mean; they cannot hold a mean.

Also established: a *constant* offset-estimate error is entirely invisible to
`err`. Errors of 0 / 3 / 6 / 50 / 200 ms all give identical steady err, because
the projection's lever arm cancels in d/dt. A big projection error is absorbed
as silent acoustic misplacement — never reported. Worth remembering.

**So eps_C is about 380 ppm**, and that is the node's AudioContext clock against
its own `performance.now()` — a pair of oscillators *nothing in this system has
ever measured*. Every ping timestamp and every mesh timestamp is
`performance.now()` at both ends (player.js:876, 894), so all of it is CPU-vs-CPU
and none of it constrains this. The mesh's 20.1 ppm places no bound on it at all.

**The magnitude is still the weak point, honestly.** 380 ppm is 18 Hz on a
nominal 48 kHz, 4-20x outside the spec class consumer audio crystals are built
to. The credible routes are (a) a platform defect — contextTime derived from a
frame counter divided by the *nominal* rate while the device consumes frames at
a materially different one, or a deep-buffer/offload path (selected by
`latencyHint: "playback"`, player.js:79) reporting from another clock domain; or
(b) **the tablet is not on its internal speaker.** On Bluetooth A2DP, USB or
HDMI, 380 ppm is unremarkable and would be the expected answer. Nobody has
established which, and it is the single most informative unknown left.

**And one hypothesis that has to be excluded before any of that, because it is
free.** `sync_err_ms` had no expiry: it is only overwritten by a fresh ack or
cleared on stop, so a node whose ack stream dies mid-song displays its last
reading forever. A frozen number and a settled one are indistinguishable, and
the tablet is the node most likely to freeze. **If the ack stream had died, both
observations are void.** That is what this commit makes visible.

**Shipped: `errAgeS` / `errStale` / `runS` / `audioClockPpm`.** Conductor and
control page only — **no fleet reload.** The attribution is
`-(rate - 1)*1e6 - skewPpm`, and note it needs no mirror of `STEER_HORIZON_S`:
the standing rate trim *is* the disturbance, so the horizon never enters. It
refuses to answer unless the reading is fresh *and* the run has been going
`ERR_SETTLE_S` (45 s, three time constants) — every source start resets the trim
to 1.0, and a reading taken mid-climb is a transient wearing a measurement's
clothes.

The run boundary is the critic's catch and the subtlest thing here: it is bumped
on **any non-null `state` message, not on a track change**. `player.js` sends
`state` from exactly one place (`startSource`), so non-null means "a source just
started at rate 1.0" — and a re-anchor restart, a seek and a mid-song catch-up
all do that under an *unchanged* trackId. Keying the boundary to the trackId
would have missed all three and averaged a fresh 45 s climb into the settled
figure, contaminating precisely the measurement this exists to make.

263 tests (15 new in `test_err_reading.py`). The control-page half was checked by
running the shipped `errCellFor` out of `control.js` under node against five
synthetic snapshots (settled / stale / mid-climb / healthy / stopped).

**A real bug found in passing, not fixed here.** `onSteer`'s re-anchor branch
calls `startSource`, which calls `stopCurrent()` (nulling `current`) and *then*
bails on `if (seekS >= buf.duration) return`. Control falls back to
player.js:489, `rate: current.rate`, on null — TypeError. The node goes silent,
`onended` was already detached so no `state` is sent, and the conductor goes on
steering a corpse for the rest of the track. Reachable exactly at end-of-track,
where the seek adjustment makes it likeliest. Same family as the two silent
failures fixed in b5de5b0. It needs `player.js`, so it needs a fleet reload and
its own commit.

**Corrections to the record:** the steer loop is **0.5 Hz**, not 1 Hz —
`_steer_all()` is gated behind `if tick % 2 == 0` (conductor.py:741); only
`push_state` runs at 1 Hz. And there is a third `err` observation nobody has
explained: **-3.8 ms** on 2026-07-28 (WORKLOG:448), a sign flip a constant eps_C
cannot produce — though it was taken right after a join, in a regime three
subsequent changes have since altered.

## 2026-08-27 (cont.) — closing the `err` thread: the droop is real and it does not matter

Matthew's call, and it is the right one: the tablet is an older device, slower
than anything the fleet will really be built around, so its results are parked
rather than chased. He asked whether a newer one needs borrowing to settle this.
It does not, and the arithmetic is the reason.

**What the droop costs on the fleet that matters.** Taking the three remaining
nodes at `err/H` (single snapshots, so provisional — the shipped tooltip refuses
to attribute anything before 45 s of settling, and these predate it):

    ID10TError-Laptop1   err  -0.0 ms  ->    -0 ppm
    ID10TError-pc        err  +0.5 ms  ->   +33 ppm
    Id10terror phone     err  -1.0 ms  ->   -67 ppm

    worst pair spread : 1.5 ms  =  0.51 m of air

Half a metre. Speakers do not get placed in a room to that accuracy, and
`plan_nudges` exists precisely to measure where they actually ended up. So the
standing droop on ordinary crystals sits **below the noise floor of the physical
layout**, and the integral term — which would cost an audio-path change and a
fleet reload — buys back less than one knock of a speaker stand. Not worth it.
Filed as considered-and-declined rather than pending, with the numbers, so it is
not re-derived in three months.

Note the ordering worry from the previous entry turned out to be a non-issue
anyway: an integral term would *not* have destroyed the measurement, because
`audioClockPpm` reads the standing trim rather than `err`, and with integral
action the trim still parks at the disturbance while `err` goes to zero. The
slice shipped this morning was already the prerequisite. Good to know if the
question ever comes back on a fleet where the spread is bigger.

**And no new hardware is needed to answer the Android question**, because it is
already answered by a device in the room: the *phone* is Android too, and reads
-67 ppm — an entirely ordinary crystal. Two Android devices, one ordinary and
one bad specimen. A third would confirm the tablet is unusual, which is not in
doubt. The genuinely open version of that question — do arbitrary guest phones
hold sync at a party — belongs to party mode, which does not exist yet.

**What stays available at zero cost:** the 60-second `chrome://inspect` console
measurement on the *existing* tablet (see TODO). It needs no new device and no
reload, and it is the only thing that would say what 380 ppm actually is. Left
as curiosity, not as a blocker.

**What this thread produced, for the record.** Two shipped commits (the mesh RTT
decomposition, and `err ms` gaining an expiry date and an attribution), one real
crash bug found and logged for the next reload, a sharper argument written into
the `filter_best` re-tune item, and three of my own hypotheses killed — coarse
`getOutputTimestamp` quantisation, HAL resampler ratio error, and the estimator
itself. The estimator's exoneration is the one worth remembering: the code's own
`min_slope_span` floor caps estimator-driven droop at `H*rtt/span` = 3.02 ms,
which is *identically* the node's own ± bound. The trust column turns out to
bound the servo as well as the offset, which nobody designed and which is now
the cheapest way to tell a servo problem from an estimator one.

## 2026-08-27 (cont.) — a saturated fit is not a crystal

Matthew's phone showed `drift ppm` of **-500.0** with a ⌫ beside it, then
recovered to +9.2 ppm a few minutes later once its sample count climbed from
31/59 to 493/1138. His read was right and is the whole mechanism: the phone had
been asleep. A suspended AudioContext puts a **step** in the offset series, and
a least-squares line through a step has an enormous slope — `max_skew` clamped
it to exactly 500 ppm.

The transient was harmless. **Banking it was not.** Two entries in the live
state file were `-500.000000 ppm`, exactly the clamp:

    id-1784195976462-d8hutijpl0j    -500.000 ppm
    id-925077ba338418ed5ab25cac816  -500.000 ppm

`timesync.py:282` already says what that number means — *"A 'drift' beyond
±max_skew is a broken fit, not a real crystal"* — clamps it, and then
`remember_skew` banked it anyway. **The credibility gate cannot catch this, and
it is worth being precise about why:** `skew_credible_at` tests
`abs(skew) >= MIN_SKEW_SNR * skew_bound`, which rejects slopes too *small* to
separate from their own error bound. A saturated slope is the opposite failure —
it dwarfs every bound and sails through the gate untouched. The gate built to
catch a device carried across the room is blind to a device that fell asleep.

Cost of the poison, had it stood: on each rejoin the prior seeds the model, and
until 30 s of span accumulates `slope = self.prior_skew`, so the node runs at
-500 ppm — `offset_at()` walking 0.5 ms per second, and 15 s x 500 ppm = 7.5 ms
of standing servo droop if playback starts in that window. Worse than the tablet
reading that started this whole thread.

**Two ends, both closed.**

- `ClockEstimate` gains `skew_saturated`, set when the fit hits the clamp, and
  `remember_skew` refuses on it with a warning naming the numbers. The clamp
  keeps a runaway slope out of *today's* timing; the flag is what keeps it out
  of tomorrow's.
- `_clean_skew` now refuses `abs(s) >= MAX_PERSISTED_SKEW` rather than `> `. The
  bug was that the load filter and the fit clamp are the *same number*, so the
  one value the filter most needed to refuse was precisely the one `>` admitted.
  This retires the two already on disk without editing anyone's state file.

**A pinned test was deliberately flipped**, and it deserves flagging rather than
burying: `test_clean_skew_accepts_plausible_crystals` had `MAX_PERSISTED_SKEW`
in its "good" list, on the reasonable-looking view that the bound should be
inclusive. It should not be. A real oscillator does not land on the bound to the
last significant digit; a saturated fit lands there every time. The parameter
moved into a new test that asserts the opposite, carrying the live evidence in
its docstring.

Note the live estimate is deliberately left alone — a clamped slope is still
better than an unclamped one for *today's* timing, and this changes only what
outlives the session. Same principle as the fit-quality gate in `e2689bc`.

269 tests (6 new). Conductor + timesync only — **no fleet reload.**

**Not Matthew's fault, and worth saying so in the record**, because "don't let it
sleep" is not a fix that survives contact with a party: a room full of guest
phones will sleep constantly. The HTTPS/Wake Lock item under Later/maybe is the
thing that would reduce the *frequency*; this commit is what makes the frequency
not matter.

## 2026-08-27 (cont.) — a live look at a hotspot fleet, and one thing I got wrong

Matthew put three devices up — laptop, phone, and a borrowed newer tablet ("Mums
tablet") — with **the phone acting as the access point** and the other two as its
clients. Conductor on the laptop. First time these tools have been pointed at a
live fleet from inside the sandbox: read-only observer on `/ws/control`, no
commands sent. Session ended when the tablet had to go back, so nothing was
played and `err ms` / `audioClockPpm` were never exercised on real hardware.

**What was established.**

- **`ID10TError-Laptop1 <-> Id10terror-phone` never formed a mesh pair.** Not
  slow — absent across ~10 minutes, while `tablet<->laptop` grew n=38 -> 59 and
  `tablet<->phone` grew n=12 -> 25 in the same window. So client-to-client
  worked (traffic transiting the phone) while client-to-**AP host** did not.
  That is the opposite of classic AP isolation and points at the phone being
  fine at *forwarding* UDP while unreachable as a WebRTC peer on its own hotspot
  interface. Matthew's own read — "my phone as access point dropping packets of
  specific types" — fits the evidence better than anything I had.
- **Mesh closure blew out to -185.72 / -186.09 ms on both tablet pairs**, having
  read -0.23 / +0.09 ms minutes earlier. Two orders of magnitude, on the pair
  models only; the star (WebSocket) path stayed healthy throughout. Unexplained,
  and worth catching properly next time.
- **The newer tablet was no better than the old one.** 167-171 used out of
  ~4676 in the window = **3.6% survival**, ping boost pinned at 4.0x. The old
  tablet ran 7.4%. Whatever costs a tablet its samples, it is not device age —
  which retires the theory the last session parked the whole thread on.
- **The phone still carries the -500 ppm prior.** Live fit +10.01 ppm with a
  bound of +/-36.04, so `skew_credible_at` says False — meaning `remember_skew`
  will *decline to replace it*, and the poison survives the session. The
  `_clean_skew` fix committed today refuses it on load, but the running
  conductor predates that commit. Either restart it or hit ⌫ on the phone;
  ⌫ genuinely empties here rather than replacing, precisely because the live fit
  is not credible.
- Nobody's drift was credible: laptop -0.01 (bound 2.70), phone +10.01 (36.04),
  tablet +13.22 (34.42). Bounds run `~rtt/span`, and with spans of 177-282 s and
  rtts of 2-8 ms that is 27-36 ppm. Correct behaviour, but it means a short
  session banks nothing at all.

**What I got wrong, and it is worth writing down because the reasoning was
seductive.** I reported the fleet was "barely pinging while idle" — laptop +6
samples in 75 s against an expected few hundred, phone and tablet +0. That was
wrong. `ClockModel(window=600.0)` is a **sliding 600-second window**, so a node
that has been up longer than that has a *saturated* counter: samples enter and
leave at the same rate and `len()` plateaus. The laptop's 2160 samples over a
599 s span works out to 3.6/s against an expected `BURST_GAP` cadence of 10
pings per 2.7 s = 3.7/s. It was pinging perfectly. I read a full buffer as a
stalled loop, and the lesson is that `nSamples` is only a rate proxy *before*
the window fills.

**One thing left genuinely unexplained.** Two control sockets observing the same
`_broadcast_control` fan-out disagreed about the mesh: a long-lived monitor
sampling every 15 s saw 2 pairs for its whole 300 s run, while short-lived
queries in the same period saw 0, twice. A later 95-second sample at 2 s
resolution found 0 pairs and **zero transitions**, so it was not flapping. The
tablet leaving explains the mesh being empty at the end — both pairs involved
it — but not two simultaneous observers disagreeing. Either the monitor's
comparison logic is subtly wrong, or the mesh table's contents can depend on
which socket is asking, and the second would be a real bug. Not chased; the
fleet went away first.

**The product finding underneath all of this: the mesh disappears silently.**
A pair that never connected, a pair with no samples yet, and a pair whose
channel died are all rendered identically — as no row at all. The mesh's entire
job is to be the independent referee, and it can vanish without saying so. On a
phone hotspot that is not a rare corner: it is Tuesday.

## 2026-08-27 (cont.) — five nodes playing, and the answer was in the TODO all along

Matthew put five devices up on the **local network** (not the hotspot) and played
a track: laptop, pc, phone, the old `Id10terror-tablet` and the borrowed
`Mums-Tablet`. Read-only observer throughout. This is the first time any of the
`err` instrumentation has been exercised against real hardware, and it earned
its keep by proving three sessions of my reasoning wrong.

**The hotspot was the cause of the missing mesh pair.** `ID10TError-Laptop1 <->
Id10terror-phone` — the pair that refused to form for ten minutes behind the
phone's access point — appears immediately on the local network (rtt 4.90,
n=16). Same two devices. Matthew's read was right and the TODO item can be
closed against a control rather than a theory.

**The endpoint-cost fit is finally over-determined and it holds.** 8 mesh pairs
plus 4 WebSocket constraints = 12 equations for 6 unknowns, **6 degrees of
freedom, rms residual 0.35 ms** over a 1.9-6.7 ms range:

    ID10TError-pc         0.22 ms one-way
    ID10TError-Laptop1    0.88
    Mums-Tablet           2.14
    Id10terror-tablet     2.16
    Id10terror-phone      2.55
    [DataChannel]         1.51 ms overhead

The laptop has now been fitted on three unrelated networks — router, phone
hotspot, local — at **0.87 / 0.85 / 0.88 ms**. That is a device constant, and it
is the strongest evidence yet that this measures hardware rather than curve-fits
a topology. The two tablets land at 2.16 and 2.14: old and new, indistinguishable.

**First live `audioClockPpm` readings, and they are all ordinary crystals:**

    Mums-Tablet        err -0.53   audio clock  -48 ppm
    Id10terror-phone   err -0.43                -38 ppm
    ID10TError-Laptop1 err +0.07                 +5 ppm
    ID10TError-pc      err +0.53                 +8 ppm

Within the +/-50-100 ppm class consumer audio hardware is built to, and
arithmetically self-consistent: Mums-Tablet at eps = -48 + 13.1 = -34.9 ppm
predicts err = 15 x -34.9e-6 = -0.52 ms against -0.53 measured. Four-node spread
1.06 ms = 0.36 m of air.

**And then the finding that overturns the whole thread.** Sampling the old
tablet against the laptop every ten seconds:

    tablet  +4.66  -4.90  +1.40  -6.18  +0.34  +5.32  +7.23  +4.28
    laptop  -0.04  -0.04  -0.03  +0.05  +0.03  +0.02  -0.07  -0.07

**It swings through zero.** Range -6.18 to +7.23, mean near +1.5, while
`runS` climbed 179 -> 249 in step with the wall clock (so it was *not*
restarting — an earlier claim of mine that held only for the window right after
Matthew rejoined it).

The July TODO called this exactly: *"Swings through zero to -5 or so -> its
offset estimate was wobbling and the servo was chasing a moving reference; a
nudge would be actively wrong because you'd be biasing against a number that
averages to zero."* Hypothesis 2. It was right, and it has been sitting there
since 2026-07-28 while I built an increasingly elaborate case for hypothesis 1.

Every reading this investigation rested on — +6.5 (July), +6.0 (August), +2.8
and +4.4 today — was **a single sample of a swinging signal**, read as a parked
value. The P-droop law is still correct physics and still explains the four
well-behaved nodes; it simply never applied to this one, because a node whose
reference is moving has no steady state to droop to. Context Matthew supplied
that fits: the device is **Android 6** with memory problems, and it drops out.

**The hole that exposed in this morning's commit, now closed.** `audioClockPpm`
gated on *fresh* and *settled* but not on *steady*, so a node swinging +/-6 ms
sailed through and got a confident-looking attribution made of noise. It now
keeps Welford accumulators over the run for the disturbance `-(rate-1)*1e6`,
reports the **mean** rather than the latest trim, and exposes `distSdPpm`,
`distN` and `audioClockCredible`.

The credibility test is deliberately the same shape as
`ClockEstimate.skew_credible_at` — `|mean| >= 2 x sem` — because it is the same
question: a number is only worth acting on when it clears its own uncertainty. A
node swinging through zero fails it however long you watch, which is the correct
answer and the one this thread spent three sessions failing to give. Refused is
not hidden: the mean and the spread stay readable, because the spread is the
evidence.

275 tests (6 new). Conductor only — **no fleet reload**, but the running
conductor needs a restart to pick it up.

**Also observed, worth keeping.** Sample survival on the local network: laptop
99.5%, pc 70-75%, phone 13-17%, both tablets 7-9%. The two tablets are
indistinguishable again. And a track change put the pc at **err -28.81 ms** at
`runS 19` — a large, real transient that the settling gate correctly refuses to
attribute, which is the first time that guard has visibly earned its place.

## 2026-08-27 (cont.) — the instrument validates against itself, and the tablet is characterised

Matthew restarted the conductor, which put today's commits into the running
process for the first time, and gave the fleet a clean 240 s window. Two
independent calculations of the same physical quantity, and they agree:

    node                 ext ppm   cond ppm   sd ppm   verdict
    ID10TError-pc           +9        +9          6    CREDIBLE
    ID10TError-Laptop1      +1        +0          3    not credible
    Mums-Tablet             +3        +4         36    CREDIBLE
    Id10terror-phone        +1        +2         40    CREDIBLE
    Id10terror-tablet      -12       -22        337    not credible

`ext` is computed outside the conductor from sampled `err` / STEER_HORIZON_S
minus the fitted drift; `cond` is the shipped path — Welford accumulators over
the servo's rate trim, minus the same drift. Different inputs, different code,
same answers. The pc lands on +9/+9 exactly.

**Every credibility verdict is correct, and for the right reasons.** The `sd`
column separates the fleet by an order of magnitude — 3, 6, 36, 40, **337** —
which is precisely the discrimination the whole thread lacked. The laptop is
refused because its audio clock genuinely *is* zero and cannot clear its own
noise (the same shape of answer `skew_credible_at` gives, and correct). The
Android 6 tablet is refused because it swings.

**The -500 ppm poison is confirmed gone**, on the first conductor start carrying
the `_clean_skew` fix:

    Mums-Tablet        remembered none      <- was -500.000 ppm
    Id10terror-phone   remembered none      <- was -500.000 ppm
    ID10TError-pc      remembered +21.9     live +21.7  (kept)
    Id10terror-tablet  remembered +20.4     live +20.1  (kept)
    ID10TError-Laptop1 remembered -0.0                  (kept)

Both saturated entries refused on load, all four genuine crystals preserved, and
the state file never edited by hand.

**The Android 6 tablet, now characterised five independent ways.** Across
separate captures it reads mean +0.38 / sd 5.08 / 66 crossings, mean -0.02 /
sd 4.99 / 25 crossings, and mean +0.12 / sd 5.06 / 46 crossings — while the
fleet around it holds 0.4-0.5 ms spread. **It averages to zero and swings
+/-10 ms.** Not parked at +6, not parked at anything. Three sessions of this
thread rested on single samples of exactly that signal.

**A refinement Matthew's last two observations forced.** A track change broke
the fleet once and then held perfectly through the next one. So a track change
is not inherently destructive — it is a *stress test that only a marginal model
fails*. The failing case had Mums-Tablet freshly reconnected with ~25 usable
samples and a drift fit thrashing +16 -> +2 ppm, and the old tablet frozen at
142/1304 gaining two samples per thirty seconds. The passing case had every
model converged and the fleet already at 0.40 ms.

That points the fix somewhere better than "exclude samples taken during a
transfer". The load gate asks whether a node has *loaded*; `_catchup` proceeds as
soon as `estimate() is not None`. Neither asks whether the estimate is any
**good**, though `n_used`, `span` and `trust_ms` are all sitting right there.
The TODO already wants this for `_catchup` ("gating on estimate quality rather
than existence") — the same gate belongs on a track change, and it addresses the
actual failure rather than one of its causes.

Fleet at the end: **0.46 ms spread = 0.16 m of air**, 10/10 mesh pairs, worst
closure 1.62 ms (on the n=9 pair; the n=114 pair reads -0.04).

## 2026-08-27 (cont.) — a refused start no longer kills the node

Found by reading, not by watching, while the hypothesis panel mapped the servo
path — and then Matthew lost `Mums-Tablet` **at a track change**, which is
exactly the window the bug lives in.

`startSource()` called `stopCurrent()` — which nulls `current` — and only *then*
bailed on `seekS >= buf.duration`. Everything follows from that order:

- the node is silent, because the source was already torn down;
- it never says so, because `onended` was detached during the teardown, so no
  `state` message is ever sent;
- the conductor goes on steering a node that has stopped, indefinitely;
- and `onSteer`'s own tail dereferences the null (`rate: current.rate`) and
  throws, killing the handler for good.

Reachable at end of track, where `+ (nowCtx + 0.08 - targetCtx)` makes the
re-anchor's seek adjustment largest, and on a short or truncated decode.
`_steer_all` declines only the last 400 ms, so the window is genuinely open.

**Fix:** refuse before tearing anything down, and give the refusal a voice. The
guard is written `!(seekS < buf.duration)` rather than `seekS >= buf.duration`
so a NaN seek is refused too — the one value that sails through the old
comparison and reaches `src.start()`. `startSource` now returns a boolean, and a
refusal costs nothing: whatever was playing keeps playing and ends naturally.
The node sends `startRefused` with its numbers; the conductor logs a warning
naming the node and toasts. Same instinct as `b5de5b0` — the silent version of
this cost a node a whole track.

`_clean_pos_ms` is new and separate from `_clean_err_ms` on purpose: the
latter's 60-second clamp is right for a servo error and very wrong for a track
length, and reusing it turned 181.40s into 60.00s in the first draft of the test.

**Verified before and after** in `tools/reanchor_harness.js`, which extracts and
evals the shipped `perfToCtx`/`stopCurrent`/`posAt`/`startSource`/`onSteer`
straight out of `player.js` (same approach as `tools/level_harness.js`) and runs
the old order beside them. Old: TypeError, node silent, nothing reported. New:
no throw, still playing, steer still acked, refusal voiced with the numbers.
Eight checks. 279 Python tests (2 new in `test_guards.py`).

**Needs a fleet reload** — `player.js`. Rides with the reload already owed.
