# SyncPlay

Multi-node synchronized network file player. One Python **conductor** owns the
music library and the reference clock; every speaker device — Windows PC, phone,
tablet — is a **node** that just opens a web page. No installs on nodes, ever.

The conductor continuously pings every node (NTP-style four-timestamp exchanges
over WebSocket), filters network jitter by keeping only low-RTT samples,
estimates each device's clock **offset** and **drift** (consumer crystals
disagree by 10–100 ppm ≈ 6–24 ms per song), and hands each node a
drift-projected start time for every track. Heavy resync bursts run in the idle
gaps between songs, where corrections are inaudible.

## Quick start

```
pip install aiohttp
python tools/make_test_tones.py        # optional: generate test audio
python -m syncplay --music-dir music
```

Then:

- **Every device** → open `http://<your-LAN-IP>:8927/` → tap **JOIN**
- **You** → open `http://<your-LAN-IP>:8927/control`

Watch each node's offset/RTT/drift settle in the NODES table (give it ~30 s for
a drift estimate), hit **🔔 beep test** — you should hear *one* beep, not a
flam — then play something.

## How the sync works

1. **Measure:** conductor sends `ping{t0}`; node echoes `pong{t0, c1, c2}`
   stamped with its `performance.now()`; conductor stamps `t3` on receipt.
   Offset = `((c1−t0)+(c2−t3))/2`, RTT = `(t3−t0)−(c2−c1)`.
2. **Filter:** only samples within a small tolerance of the window's best RTT
   count — low-RTT exchanges suffered the least queueing, so their symmetric-
   delay assumption is the least wrong. This absorbs Wi-Fi spikes.
3. **Drift:** least-squares slope of filtered offsets over a 10-minute sliding
   window ([timesync.py](syncplay/timesync.py) — pure stdlib, unit-tested
   against synthetic skewed clocks in [tests/](tests/test_timesync.py)). A slope
   needs ~30 s of span before it's trustworthy, so a node that has been here
   before starts from its **remembered skew** instead of from zero (see
   *Cadence & calibration*).
4. **Schedule:** "play track X at node-local time L", where L projects the
   offset forward along the drift slope. In the browser, L (a
   `performance.now()` value) is mapped onto the AudioContext timeline via
   `getOutputTimestamp()`, which bakes in the device's output latency, and the
   buffer starts sample-accurately via `source.start(when)`.
5. **Correct:** sparse pings keep the model fresh during playback; dense bursts
   run between songs; every track start is a fresh alignment, so error can't
   accumulate across a playlist.
6. **Servo (v2):** every 2 s the conductor sends each playing node a reference —
   *"at your local time L the song should be at position P"*. The node compares
   its true audio position (which runs on the **DAC's** crystal, a different
   oscillator than the one clock sync measures) and trims `playbackRate` by up
   to ±800 ppm (≈1.4 cents — inaudible) to null the error over ~15 s. Errors
   too big to slew (a suspended tab, a stall) hard re-anchor with an in-place
   restart. Each node reports its live error back — the **err ms** column on
   the control page — so you can watch every device hold lock in real time.

## Cadence & calibration

- Node joins → 16-ping burst converges its model in ~1 s.
- **Remembered skew.** A reconnecting node hands us a fresh clock epoch, so its
  offset must be re-learned from nothing. Its *crystal* is the same physical
  object it was last time, though, so the last **fitted** skew is persisted per
  device in `syncplay_state.json` and seeds the new model — sparing every
  returning node the first ~30 s of asserting zero drift (for a 20 ppm tablet,
  that's ~0.6 ms of avoidable error at the point it's least able to spare it).
  Only measured skews are banked, never an inherited one, so a bad reading
  can't echo forward; an unreadable value falls back to a cold start.
- During playback → 3 pings / 5 s (a few hundred bytes; inaudible in every sense).
- Song gap / manual **⇄ resync** → 10-ping bursts.
- Per-node **nudge** (ms, on the control page) compensates residual speaker/DAC
  latency: if one device sounds late, give it a negative nudge. Per-node
  **volume** is also pushable from the control page. Both persist in
  `syncplay_state.json`.
- The control page also shows a live **spectrum** per node — a graphic-EQ meter
  tapped from each node's own audio output (an internal signal tap, not a mic),
  handy for eyeballing at a glance that every device is actually playing.
- **📐 calibrate all** sweeps every speaker in turn (chirp → mic →
  cross-correlation → time-of-flight), medians several reps each, and proposes a
  nudge per node that aligns them all to the *latest*-arriving speaker. It only
  ever proposes — read the table, then set the nudges yourself. Needs exactly one
  node in mic mode and no playback. A single ToF is not a distance: it carries
  the mic's own input latency, which only cancels when speakers are differenced
  against each other. Mic capture needs a secure context, so today that means the
  conductor's own browser on `localhost` (which is also the most accurate mic
  node — its clock *is* the reference clock).
- The **queue** on the control page sets what plays next: **＋queue** on any
  track appends it, ↑/↓ reorder, ✕ removes, **clear** empties it. The queue is
  consumed from the head by auto-advance and ⏭ next; when it runs dry playback
  falls back to folder order. Queueing the same track twice is allowed. Playing
  a track explicitly is an override — it doesn't touch the queue. Whatever's
  next (queued or not) is prefetched to every node while the current song plays.
- A per-node **output EQ** (5 bands, ±12 dB) on the control page shapes each
  device's tone — pushed live, persisted, and bypassed by the beep. Additive to
  the audio path; it shifts timbre, not sync (the servo runs upstream of it).

## Notes & limits (v1)

- LAN only. No auth, no TLS — don't expose the port to the internet.
- Nodes decode whole files in memory (~85 MB per 4-min track); the cache holds
  the current + next track only. Phones are fine with this.
- Format support = whatever the node's browser decodes: MP3/AAC/WAV/FLAC work
  everywhere modern; OGG/Opus not on iOS Safari.
- Nodes must (re)load the player page after a server upgrade to pick up new
  player code — a stale page still plays, it just lacks the newest features
  (e.g. the servo shows "—" for err ms until refreshed).
- iPhones: silent-mode switch must be OFF for Web Audio to sound.

## Verifying sync with your ears and a mic

Play `music/Test Pulse A (440Hz).wav` on two devices in the same room — sharp
pulse trains make misalignment obvious as echo/flam. For numbers: record both
devices with a phone mic and measure the transient gap in any waveform editor.
