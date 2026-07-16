# SyncPlay worklog

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
