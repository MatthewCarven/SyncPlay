# SyncPlay — parked ideas (prune freely, most of this may never happen)

Working rule that keeps the fear away: every feature must be **additive,
observable, revertible** — dashboard/rendering first, conductor scheduling and
the player audio path only when unavoidable, one commit per feature so
`git revert` is always an exit.

## Done
- [x] **Two silences given a voice** (2026-08-23) — a start that could time no
  node at all only ever logged `"nobody"` while `self.playing` said otherwise;
  it now toasts and logs at warning (state deliberately left standing, so
  `_catchup` can still recover it). And `_measure_pending` holds one probe, so a
  📏 during a sweep silently cost the sweep a rep — now guarded in all three
  directions. 224 tests (7 new in `test_guards.py`).
- [x] **Forgetting a remembered drift** (2026-08-23) — `_state["skews"]` had no
  exit: `nudges`/`eqs` pop their entry when cleared, skews only ever grew, so a
  value banked before the gate existed seeded that node forever. Now `_save_state`
  pops a cleared prior, and a `⌫` in the drift cell (shown only when there *is*
  something remembered) clears all three places it lives — the node's
  `prior_skew`, the state-file entry, and the live `ClockModel`'s inherited prior,
  which is what stops it coasting on the bad number for the rest of the session.
  Note `_save_state` re-banks from live models, so forgetting on a node currently
  measuring a *credible* drift replaces rather than empties — correct, and the
  toast says so. Limit: you can only forget a node you can see. 217 tests (6 new,
  `STATE_FILE` monkeypatched — the live state file is never a fixture).
- [x] **Fit-quality gate on `remember_skew`** (2026-08-23) — a fitted drift is
  only carried into the node's next session if it clears its own worst-case
  error by 2×. The bound is exact and falls out of the same sums the fit needs:
  bounded per-sample errors (`rtt_i/2`) move a least-squares slope by at most
  `Σ|t−t̄|·(rtt/2) / Σ(t−t̄)²`. **Why not R²:** a device carried across the room
  reads 83 ppm at **R² = 1.00000** — asymmetry sweeps as it walks, and the fit is
  the cleanest in the file. Residual checks bank it; the bound refuses it (ratio
  0.69) and still banks a real 20 ppm crystal (ratio 8.2). The impostor can't win
  by lasting longer — ratio is invariant with span, because asymmetry is capped
  by the round trip. **Persistence only:** the live model uses its fit either
  way, so a refusal can't change how anything is timed today. `drift ppm` renders
  dim until the fit clears its bound; refusals are logged with their numbers.
  No fleet reload. 211 tests (10 new), including a constructed worst-case
  adversary that must hit the bound exactly.
- [x] The **± trust column** (2026-08-22) — every node's offset now shows the
  worst-case error the samples themselves certify. Asymmetry is bounded by the
  round trip, so a sample of round trip `rtt` proves `|offset error| <= rtt/2`;
  no noise model, no assumption. The judgement call was **`worst_rtt/2`, not
  `best_rtt/2`**: the estimate is a median/fit over everything `filter_best`
  admitted, so it inherits the *widest* survivor's bound, and quoting the best
  would flatter precisely the node whose gate is widest open. Floor
  (`best_rtt/2`) lives in the tooltip; the gap between them is what the filter's
  tolerance costs. Bench loopback: floor ±0.27 but true bound ±0.76, 2.8×.
  `ClockEstimate` gains `worst_rtt` + three read-only properties; **nothing
  branches on them**, so the timing core computes what it always did. Conductor
  + control only — **no fleet reload**. 201 tests (7 new), including a 1000-trial
  randomized adversarial search over every possible leg split.
- [x] Battle-hardened the loader (2026-08-02) — all five rungs. **Symptom:** a
  tablet frozen on `100%` while the other three played, `err` and `position`
  both `—`; it had downloaded the file and then silently sat the song out.
  **Cause, which Matthew called before the code was read:** every queue edit
  runs `_prefetch_next()` → a `preload` broadcast, and the player handled that
  with fire-and-forget `loadTrack()`. `loading` dedupes only the *same* track,
  so N rebalances started N concurrent 30 MB fetches and N concurrent
  `decodeAudioData` calls at ~85 MB of PCM each — five of those is ~425 MB on a
  device with no business holding one.
  - `preload` now carries `prefetch: true/false`. Only a guess is cancellable;
    any newer preload supersedes it, a demand load included. A guess promoted to
    demand stops being cancellable, so a later guess can't kill the load the
    room is waiting on.
  - Decodes serialise through one chain, re-checking the abort signal before
    spending the memory. This is the actual OOM guard.
  - `AbortError` no longer reports `loadError` — an abort is a decision, and
    reporting it would toast a failure on every queue reorder.
  - One retry with backoff before giving up; the conductor reads progress
    running backwards as "downloading again" and restarts the decode clock.
  - `unloaded` on eviction, so `node.loaded` stops claiming buffers the player
    has dropped and the gate stops skipping a node that isn't ready.
  - `loadError` also clears `load_track`/`load_pct` — that leak was why the pill
    froze at `100%` instead of reading "not ready".
  - Measured old vs new on a harness that runs the shipped functions straight
    out of `player.js`: 5 rebalances, 5 concurrent decodes → 1; 4 overlapping
    demand loads, 4 → 1. 29 harness checks plus 193 Python tests. **Needs a
    fleet reload**, and the honest test is still a party.
- [x] Straggler load gate (2026-08-02) — one phone on a bad radio could hold the
  whole room in silence for up to 20 s, and measurably bought nothing by it: the
  old rule waited the full timeout and then started **without that node anyway**.
  `hold_gate()` replaces "wait for everyone or time out" with a **quiet period** —
  keep waiting while nodes are still arriving, stop once nobody new has for
  `STRAGGLER_GRACE_FRAC` of that start's own gate (15 s cold, 9 s warm;
  retuned up from a flat 4 s the same day, which was too eager for a real
  tablet on a real radio). A uniformly slow fleet arrives in a drip that keeps
  resetting it and is never cut; a single straggler stops the drip. Below half
  ready it holds regardless, so a node with a cached copy can't strand the other
  three (the failure a grace measured from the *first* ready node would have had).
  The countdown remains a floor no gate may undercut. Measured against simulated
  fleets: cold straggler 20.0 s → 15.7 s, warm skip 12.0 s → 9.6 s, uniformly
  slow 4.06 s → 4.06 s (identical, as intended). A cut node is deferred, not dropped —
  `_catchup` already owns that — and control now names who and why.
- [x] Real decode phase on the control page (2026-08-02) — the node pill froze
  on `⬇ 100%` for the whole decode, which is the longer half of a cold start on
  a tablet and the half the operator most wants to see moving. `decodeAudioData`
  is a single opaque promise with no progress events, so there is no percentage
  to be had; the conductor stamps when the last byte lands and reports **elapsed
  seconds** instead — honest, and it answers the only question a bar is asked
  (moving, or hung). Decode outranks download in the pill because `loadPct`
  stays at 100 throughout. Conductor + control page only: **no player-side
  change, so no fleet reload.** Every exit from the phase is tested (decoded,
  failed, retargeted, reconnected) because a timer that never stops is worse
  than the frozen 100% it replaced.
- [x] Adaptive ping cadence (2026-07-28) — burst size and frequency now scale
  with each node's *sample survival rate* (`n_used / n_samples`), because the
  clock model runs on what survives the RTT filter, not on what we sent. Split
  sqrt/sqrt between depth and spread, capped at 4×, with a young-node guard so
  a bad first second can't lock in a boost. Observed on the real fleet: laptop
  99.6% survival (no boost), pc 75% (1.33×), phone 11.5% and tablet 5.4% (both
  capped). Control page tags boosted nodes; conductor logs every change.
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
  - **2026-08-23: a failed probe now names its own cause.** The correlation is
    normalized, so `peak` measures shape, not level — it cannot distinguish a
    dead input from a missed chirp, which is exactly the ambiguity that cost a
    month in July. Capture RMS/peak dBFS + clipping now ride with every result;
    an `in level` column shows them, and the failure note says "mic heard
    nothing (-74 dBFS) - check the input, not the room" / "input clipping" /
    "no usable capture" (which now genuinely means the room). Diagnostics only —
    `CAL_MIN_PEAK` is still the only gate. **Needs a fleet reload** (`player.js`).
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

- [ ] **Modular sync engine, switchable while stopped** (scoped 2026-08-02,
      nothing built)
  - Full scope in [SYNC_ENGINE_PLAN.md](SYNC_ENGINE_PLAN.md). Verdict: possible,
    and the conductor side is cheap — `ClockEstimate` is already most of the
    interface (3 methods, 8 fields, 17 `estimate()` sites, **all 17 already
    guard against None** because that's the fresh-join state).
  - The boundary has to sit **above the wire**: an engine may change how a
    node-clock timestamp is computed, never what one means. Hold that and
    `player.js` never learns an engine exists, so the swap is hot. Break it (a
    swappable *servo*) and a swap costs a fleet reload.
  - Seam should be a `SyncEngine` owned by the Conductor, not a model owned by
    each Node — a mesh-consensus engine needs every node's samples at once, and
    that shape is expensive to retrofit across 17 call sites.
  - **Ordering matters:** the estimate-quality gate (below) must land *before*
    the swap button. `Node.loaded` is decoupled from the model, so post-swap
    `plan_start` sees a warm fleet, skips `request_burst`, and fires play 1.8 s
    later into empty models — `self.playing` set, nobody told to play, and no
    signal until auto-advance a whole track later.
  - Payoff isn't modularity, it's that the scoreboard already exists: triangle
    closure is an independent referee (and survives a swap — the pair models hold
    no conductor-clock data), `err ms` is the servo's own opinion, and
    `test_timesync.py` is a conformance suite in disguise. First second engine
    should be `flat` (v1 median-only, no drift) as the *control* — you already
    know what it must do to the numbers.

- [ ] **The tablet's `err`: measured 2026-08-27, and the question changed**
  - Re-measured mid-song at 4.0x cadence: tablet `err` **+6.0 ms** against
    **±3.02**, laptop/pc/phone all inside their bounds. July's reading was +6.5
    at 1.0x cadence, so the adaptive cadence did not move it.
  - **The ±-column test this item was waiting for does not decide it.** `err ms`
    is *nudge-invariant* — `nudgeMs` enters via `perfToCtx(atNodeMs + nudgeMs)`
    in **both** `startBuffer` and `onSteer` and cancels out of `errS` entirely.
    That is deliberate (the servo must not spend rate trim fighting a fixed
    acoustic offset), but it kills the old hypothesis 1 as written: a nudge
    shifts when sound leaves the speaker and leaves this number reading 6.0.
    So *outside the bound -> real output latency -> nudge it* has no second
    arrow. **Do not nudge the tablet** — unverifiable, and `err` would still
    accuse it.
  - **The drift is not the problem.** Two mesh-truth snapshots 467 s apart fit
    all four `drift ppm` figures across six pairs with residuals <= 0.25 ms
    (worklog 2026-08-27). The mesh pair models hold no conductor-clock data, so
    that is an independent referee, and it pins the tablet's 20.1 ppm to well
    under 1 ppm.
  - **Matthew's observation, which started this:** the tablet gets most of its
    surviving samples *during file transfer* (121/1636 = 7.4%). That explains
    the survival rate and the wide ±, not the 6.0 — `rtt/2` is a hard per-sample
    bound, so transfer-window asymmetry caps at 3.02 ms however systematic it
    is. Worth knowing anyway: `trust_s` bounds the offset **at `t_mean`**, and
    a clustered node pays an unshown `skew_bound x (now - t_mean)` on top. Here
    that is < 0.5 ms; on a node with a worse slope it would not be.
  - **Leading candidate now:** what survives the nudge cancellation is
    `C2 - C1`, the drift of the node's own `getOutputTimestamp` mapping between
    two steers, which enters `err` at full weight. `err ms` is the *last*
    steerAck, not an average, and at 6 ms the servo pulls 400 ppm against an
    800 ppm cap — so it is not saturated, and something re-injects the error
    faster than the ~15 s it needs to null it. A coarsely-quantised output
    timestamp on an Android tablet has that shape.
  - **Next test is no longer a stare.** Log the tablet's `C` (the `perfToCtx`
    mapping constant) per steer and see whether it steps. Pairs with the
    `outputLatency`/`baseLatency`/sample-rate readout and the err-ms sparkline
    already listed under "More timing info" — both would have shown this.

- [ ] **`onSteer` can dereference a nulled `current` and kill a node silently**
      (found 2026-08-27; needs `player.js`, so a fleet reload and its own commit)
  - The re-anchor branch calls `startSource`, which calls `stopCurrent()` (which
    sets `current = null`) and *then* early-returns on
    `if (seekS >= buf.duration) return`. Control falls through to player.js:489,
    `rate: current.rate` — TypeError on null.
  - Cost: the node is silent, `onended` was already detached so no `state` is
    sent, and the conductor keeps steering a node that is not playing for the
    rest of the track. Exactly the failure shape as the two fixed in `b5de5b0`.
  - Reachable **at end-of-track**, where `+ (nowCtx + 0.08 - targetCtx)` makes
    the seek largest — and `_steer_all` only declines the last 400 ms, so the
    window is open. Also reachable on a short or truncated decode.
  - Fix is small (bail before `stopCurrent`, or ack defensively), but the real
    value is giving it a voice rather than a silent return.
  - **This is the first thing to check** if `err ms` ever renders stale: a dead
    ack stream is exactly what this bug looks like from the conductor.

- [ ] **Follow-ups to the err-reading slice** (2026-08-27, all optional)
  - **A trace that outlives the process.** The shipped fields answer "what is it
    doing now"; they cannot answer "is this the same thing we saw in July",
    which is the question actually being asked. A ~10-line JSONL sidecar per
    steerAck would make a reading taken next month comparable rather than a
    fourth anecdote. An in-memory ring dies with the conductor and covers
    minutes; the file is the part with real reach.
  - **A sparkline** over the last few minutes, to tell settled from stepping
    from sawtooth. Note it does **not** discriminate on its own: an audio-clock
    ramp and an estimator-slope ramp produce bit-identical droop, and only the
    `audioClockPpm` split separates them. Also record `est.skew_ppm` per sample
    if this lands — subtracting the *instantaneous* slope from an `err` that
    integrated the slope's *history* through a 15 s lag is only safe on a node
    whose fit is steady, and the tablet is precisely not that node.
  - **An integral term** is the actual fix for standing droop, and is a design
    decision rather than a bug fix: it would drive `err` to zero and park the
    trim at the disturbance. It also **destroys the measurement** — droop is the
    only reason `err ms` carries eps at all — so if it is ever added, the
    attribution has to move onto the trim itself first. Touches the audio path.
    Not before the route question below is settled.
  - **The route question, which is Matthew's to answer and worth more than any
    of the above:** is the tablet playing through its internal speaker, or over
    Bluetooth / USB / HDMI? On an external wireless route 380 ppm is ordinary
    and the case is closed; on the internal speaker it is a 4-20x outlier and
    something else is going on.

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
  - **2026-08-27, the argument this item was missing.** Asymmetry decomposes as
    `(A_tx - A_rx) - (B_tx - B_rx)`: a node's *total* endpoint cost cancels, and
    only its transmit-vs-receive **imbalance** reaches the offset. Slow-CPU cost
    is roughly symmetric and buys no offset error; Wi-Fi power-save is
    receive-side only (the AP buffers downlink to the next beacon) and is pure
    asymmetry. `best_rtt` is a minimum, so it catches the *awake* windows and is
    dominated by the symmetric floor — which means `0.25 x best` scales the gate
    off precisely the component that contains **no asymmetry at all**, and hands
    the widest gate to the node whose `best` is least informative about the
    quantity being bounded. An absolute term is defensible; a relative one keyed
    to `best` is not, and that is a sharper case than "slow nodes get a wider
    gate". Mesh-derived one-way endpoint costs (2026-08-27): laptop 0.87, pc
    1.16, phone 2.34, tablet 2.33 ms.
  - **`worst_rtt` is unaffected by the above and stays correct** — it catches the
    *parked* windows, so it is dominated by the power-save tail, which is the
    asymmetric part. The ± column is aimed at the right quantity; do not "tighten"
    it with a symmetry model. Its whole value is being a certificate rather than
    an estimate, and trading that for an assumption about ARM stacks would be a
    bad swap at any width.
  - **It now has a scoreboard.** The ± column is a direct readout of what this
    tolerance costs: a wider gate admits a wider worst survivor and the bound
    grows, while the floor (`best_rtt/2`) holds still. Bench measurement of the
    status quo: loopback best 0.54 ms, all 56 samples admitted, worst 1.51 ms —
    the 2 ms absolute term admits *everything* when best is small. Any re-tune
    should be argued in ± ms, and `test_trust.py` already pins the relationship.

- [ ] **More timing info on the dashboard** (cheap → fancy)
  - per-node `outputLatency`/`baseLatency` + sample rate (explains *why* a
    node needs the nudge it needs)
  - err-ms sparkline per node (servo behavior over the last minute)
  - RTT p50/p95 per node (Wi-Fi quality at a glance; data already in the
    ClockModel window)
  - offset-residual RMS vs the regression line = live measurement-noise
    estimate ("how much should I trust this node's numbers")
  - ~~±rtt/2 as a single trust column~~ — **done 2026-08-22**, see Done below.

- [ ] **Three small fixes surfaced while scoping the sync engine** (2026-08-02,
      independent of it and of each other)
  - ~~`_transport_play` logs `"nobody"` without toasting~~ — **done 2026-08-23**.
    Toast + warning log. The playback state is deliberately *not* torn down: a
    node reporting `loaded` later is still pulled in by `_catchup`, and clearing
    `self.playing` would close that recovery path to fix the cosmetics.
  - ~~`_measure_one` isn't gated by `_calibrating`~~ — **done 2026-08-23**. Three
    guards, since the hole is symmetric: a manual probe refuses during a sweep
    and while another probe is in flight, and a sweep refuses while a manual
    probe is in flight (that one loses the sweep's *first* rep).
  - ~~`_state["skews"]` is write-only~~ — **done 2026-08-23**, see Done below.
  - ~~`remember_skew` will bank a slope fitted from a node *moving*~~ — **done
    2026-08-23**, see Done below. Not the residual/R² check this item asked for:
    a walk produces R² = 1.00000, so residuals are the one thing that cannot
    tell them apart.
  - ~~`loadError` leaves `load_track`/`load_pct` set~~ — **fixed** in `e27f5a1`
    (2026-08-05); a failed load now retires the whole pill, not just its timer.

## Considered and dropped

- **Per-node static / dynamic / auto movement flag** (2026-08-02) — the idea was
  that only stationary nodes should feed "core timing data" and moving ones just
  follow. Dropped, because the premise doesn't hold here: this is a **star**, not
  a consensus network. Every node has its own `ClockModel`, the reference clock
  is the conductor's `perf_counter()` and is not derived from the fleet at all,
  so a wandering node's bad samples already cannot reach anyone else's timing.
  There was no centre to protect.
  - Two supporting facts worth keeping. **RTT is not location data:** radio
    propagation over 10 m is 33 ns, while the RTT swings are milliseconds — what
    actually rises with distance is retransmissions, so "walked out front" and
    "someone started a video call" look identical. And **error is bounded by
    `rtt/2`**, so sample quality is already measured directly; a mobility label
    would have been a worse proxy for a number we hold exactly.
  - The alternative — move the alignment centre toward the outlier — is worse,
    and geometrically so. Sound is 3 ms/m and one delay per speaker gives exactly
    **one** point where everything arrives together. Aligning for someone 12 m out
    the front door moves the sweet spot off the crowd by the same amount; a 5 m
    geometry difference injects ~15 ms of smear where people are actually
    standing. It spends the crowd's alignment on the person who left the party.
  - Moving the *timing* centre is a pure loss for a different reason: the
    reference clock is arbitrary, error is relative, and re-choosing the origin
    toward a noisy node cannot shrink its error relative to the group — it only
    makes the good nodes express themselves through a worse reference. (The one
    legitimate version, electing a better-connected master, doesn't apply: the
    conductor host already sits at ~0.2 ms RTT.)
  - **What survived:** the straggler load gate and the phantom-skew fix above,
    plus the ±rtt/2 dashboard column. All three are smaller than the feature was
    and none of them need a mobility concept.
  - **Still open, and a different question entirely:** *where* the acoustic sweet
    spot should sit. That is a calibration question — `plan_nudges` already aligns
    to the latest arrival at the mic position, so "the centre of the party" is
    just where you put the mic. Re-running calibration from where people actually
    are is the real version of the idea that started this.

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
