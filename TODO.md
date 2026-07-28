# SyncPlay — parked ideas (prune freely, most of this may never happen)

Working rule that keeps the fear away: every feature must be **additive,
observable, revertible** — dashboard/rendering first, conductor scheduling and
the player audio path only when unavoidable, one commit per feature so
`git revert` is always an exit.

## Done
- [x] Armed cold start with a synced countdown (2026-07-28) — starting from a
  standstill with an undecoded track now broadcasts a countdown before playback
  and holds the load gate 20 s instead of 12. `plan_start()` is pure and
  decides on a conjunction: nothing sounding **and** somebody still loading.
  Everything else (resume, seek, next, auto-advance) stays warm and instant.
  The countdown targets the actual start, not the end of arming — a timer that
  hits zero then sits silent through `PLAY_LEAD` is a timer nobody trusts
  twice. Doubles as the spread calibration window the item below wanted.
- [x] Remembered skew across reconnects (2026-07-28) — the last *fitted* skew
  per `clientId` persists in the state file and seeds the next `ClockModel`,
  so a returning node skips the ~30 s where it must assert zero drift. Offset
  still resets (the node's clock epoch does too); only the crystal carries
  over. Guarded so an inherited prior is never re-banked as a measurement.
- [x] Writable seek bar + ⏮ restart (2026-07-22) — `seek` control command
  re-plays the current track at an absolute `positionMs` through the same
  coordinated-start path as resume; click-to-seek on the position bar. Each
  seek is a ~1.8 s synced re-start, not a live scrub — the fleet stays locked.
- [x] Mic-based auto-nudge, steps 1–2 of 4 (2026-07-18) — calibration-mic
  plumbing + level meter, then chirp emit + cross-correlation → ToF readout.
  Both verified without a real mic; ladder continues in AUTONUDGE_PLAN.md.
- [x] Play queue on the control page (2026-07-27) — conductor grows
  `queue: [track ids]`; `_peek_next` (non-consuming, drives prefetch + the
  "next up" marker) and `_take_next` (consuming, used only by auto-advance and
  ⏭ next) split the one old `_next_track`. Empty queue = folder order, exactly
  as before. Duplicates allowed, so edits address entries by **index**, not id.
  Ephemeral by design (not in the state file); pruned on rescan. Explicit
  `play {trackId}` is an override and leaves the queue alone; a bare `play`
  from a standstill starts the queue head. This is the prerequisite commit the
  party-mode item below asked for — the upload endpoint can now land on its own.
- [x] Per-node output EQ pushed from control (2026-07-18) — 5-band biquad chain
  (80/250/1k/4k/12k Hz, ±12 dB) spliced `source -> eq -> master` on each node,
  vertical sliders per node on control, persisted in state like nudge/volume,
  beep bypasses it. Born flat (0 dB = transparent), click-free ramps. Verified
  per-node (boosting one left the other flat; 12 kHz shelf lifted the top bins)
  with the EQ'd node holding err −0.01 ms — timing untouched. "Flat" resets a
  node and drops it from the state file.
- [x] Per-node spectrum "equalizer" on the control page (2026-07-18) —
  AnalyserNode tapped off each node's `master` bus, 28 log-spaced bands relayed
  over the existing WebSocket (`_broadcast_control`), animated bar-rows on
  control. Additive, servo untouched (err held 0.3–0.8 ms during a live play).
  Shows the *digital* signal each node plays; the acoustic/mic version waits on
  HTTPS.
- [x] Read-only per-node position bar on the control page (2026-07-17) —
  ideal timeline + each node's reported err ms; zero server changes.
- [x] Client↔client "sync truth matrix" (2026-07-17) — WebRTC DataChannel
  pings, conductor as signaling relay, per-pair ClockModels server-side,
  triangle-closure column on the dashboard. First live result: two
  same-machine nodes, closure 0.12 ms.
- [x] Cache-Control: no-cache middleware — stale player pages after upgrades
  are extinct (found the hard way when a cached page lacked the mesh code).

## Next candidates

- [ ] **Closed-loop room correction (needs the HTTPS story first)**
  - Now that per-node output EQ exists (manual), the payoff is automating it:
    mic-capture each node's actual acoustic response (chirp/sweep), compute the
    per-band correction, and drive the same `eq` command the sliders use. `err
    ms` + spectrum + EQ are the pieces; the mic path is the missing input, and
    `getUserMedia` needs a secure context (see the HTTPS item under Later/maybe).
  - "the perfect sphere won't exist in perfect space" — this is the slice that
    measures the distortion of the space and bends the signal to cancel it.

- [ ] **Party mode — any node can submit a file to the playlist**
  - ~~The queue this needed~~ is done (see Done, 2026-07-27) — auto-advance
    already pops it, control already vetoes/reorders. What's left is the
    submission path.
  - `POST /upload` on the conductor (size cap ~100 MB, audio-extension
    whitelist, sanitized filename) into `music/party/`, auto-rescan, toast
    "X added by <node>" on control, then auto-`queue` the new track id.
  - Control page: per-submitter cap; a party-mode toggle that gates the whole
    endpoint (off by default, so an idle conductor accepts no uploads).
  - Player page: an "add a song" file input, hidden behind a party-mode
    toggle on the control page.
  - Risk is now contained: the scheduling half already landed and is tested,
    so the upload endpoint is pure HTTP + a `queue` call — no auto-advance
    changes, revertible on its own.

- [ ] **Mic-based auto-nudge — step 4 of 4** (the live thread)
  - Spec + ladder in [AUTONUDGE_PLAN.md](AUTONUDGE_PLAN.md). Steps 1–3 shipped;
    the sweep now proposes a nudge per speaker on the control page.
  - Remaining: **apply**. Cheap — the control page fires the existing per-node
    `nudge` command for each accepted proposal, so there's no new server
    surface. Wants an explicit confirm and an obvious undo (the old values are
    right there in the table).
  - **The real blocker is not code, it's a room.** Everything so far is verified
    against simulated nodes: the arithmetic and the sequencing are right, but no
    chirp has ever crossed actual air. First hardware run is the milestone.
  - No HTTPS needed for that: `localhost` is a secure context, so the conductor
    box can be the mic. HTTPS is only for putting the mic on a phone.

- [ ] **Spread the join burst across more than one radio window** (partly
      mitigated by the armed cold start; still open for mid-song joins)
  - `BURST_JOIN = (16, 0.06)` puts every join sample inside a single ~1 s
    window. Those 16 are not independent: a tablet parks its Wi-Fi radio
    between beacons, so one bad window corrupts all of them together, and
    min-RTT filtering can't help when `best` is itself inflated.
  - The arming phase already fixes this for the *cold start* case — 6 s of
    bursts across several power-save cycles, for free, inside a wait we were
    paying anyway. What's still exposed is a node joining **mid-song**, which
    gets the 1 s join burst and nothing else.
  - Shape: a fast phase to get *an* estimate quickly, then a spread phase
    (~250 ms spacing for a few seconds) so the window straddles several
    power-save cycles. Costs a couple of seconds of join latency.
  - Pairs with gating `_catchup` on estimate *quality* rather than existence —
    it currently proceeds as soon as `estimate() is not None`, i.e. after one
    surviving ping, though its `range(25)` loop is willing to wait 5 s.
    `n_used >= 8` would cost a late joiner about a second.

- [ ] **Re-tune `filter_best` so slow nodes aren't handed a wider gate**
  - `cutoff = best + 0.002 + 0.25×best`. At best=3.8 ms (tablet) that admits
    1.8× best, each admitted sample carrying up to ±3.3 ms of path asymmetry.
    At best=0.2 ms (loopback) it's 11× best, but 11× of nothing is nothing.
    The 2 ms absolute floor only bites the node least able to absorb it.
  - Wants its own commit and its own test: unlike remembered skew, this
    changes behaviour for *every* node, not just returning ones. Sim coverage
    in `test_timesync.py` should show it helps a jittery node without
    starving a quiet one of samples.

- [ ] **More timing info on the dashboard** (cheap → fancy)
  - per-node `outputLatency`/`baseLatency` + sample rate (explains *why* a
    node needs the nudge it needs)
  - err-ms sparkline per node (servo behavior over the last minute)
  - RTT p50/p95 per node (Wi-Fi quality at a glance; data already in the
    ClockModel window)
  - offset-residual RMS vs the regression line = live measurement-noise
    estimate ("how much should I trust this node's numbers")

## Later / maybe
- Shuffle + repeat modes. Both are now small: shuffle = seed the queue from a
  shuffled library; repeat-one = don't consume on advance. Drag-to-reorder is
  the nicer version of today's ↑/↓ buttons.
- Mesh prefetch — reuse the existing WebRTC DataChannels to relay track bytes
  node-to-node. Almost certainly never needed on a LAN; noted so the idea
  isn't re-derived.
- HTTPS story (self-signed or tunnel) if ever needed: also unlocks
  `crypto.randomUUID` and — more importantly — **Wake Lock on phones**,
  which silently no-ops on plain http today; a sleeping phone suspends its
  AudioContext and the servo hard re-anchors on wake.
