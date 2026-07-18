# SyncPlay worklog

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
