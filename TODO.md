# SyncPlay — parked ideas (prune freely, most of this may never happen)

Working rule that keeps the fear away: every feature must be **additive,
observable, revertible** — dashboard/rendering first, conductor scheduling and
the player audio path only when unavoidable, one commit per feature so
`git revert` is always an exit.

## Done
- [x] **A start needs a clock worth committing to** (2026-09-02) — the only bar
  was that an estimate *existed*. A freshly reconnected tablet met it with ~25
  samples, was committed to a start at a track change, and ran a minute at mean
  +19 ms / peaks +122 ms: the estimate was re-fitted out from under the servo on
  every ping. `start_ready(model)` is the rule, pure: no estimate → no; fitted
  its own slope (`skew_fitted`) → yes; else only with a remembered crystal *and*
  `MIN_JOIN_SAMPLES` (8) — the exception remembered skew exists for.
  `_send_play` refuses, `_transport_play` defers those nodes with a toast and a
  catch-up, `_catchup` polls `start_ready` for up to `CATCHUP_WAIT_S` (35 s,
  was 5 — which could never cover `min_slope_span`), one catch-up per node.
  **Cost stated plainly:** a phone with no remembered crystal joining mid-song
  is silent up to ~30 s instead of joining at once as an echo. One constant if
  that is the wrong call. 299 tests (11 new). No reload.
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

- [ ] **The mesh disappears silently — give an absent pair a reason**
      (2026-08-27, observed live; conductor + control page, no fleet reload)
  - Three completely different states render identically, as *no row*: a pair
    that has no samples yet, a pair whose peers cannot reach each other at all,
    and a pair whose channel formed and then died. On a phone hotspot the second
    and third are routine, not corner cases.
  - Observed: `laptop <-> phone` never appeared across ~10 minutes while the
    other two pairs grew steadily — and there was no way to tell that from "not
    sampled yet" without watching the table for ten minutes and inferring it.
  - The conductor already holds `mesh_seen` (last report per pair) and reaps at
    90 s, so *died* is nearly free — it is currently thrown away at exactly the
    moment it becomes informative. *Never connected* needs the roster: every
    ordered pair of mesh-capable nodes is expected, so anything in the roster
    with no entry is either young or unreachable, and `_push_mesh_roster` knows
    when it told them about each other.
  - Suggested shape: keep a row for every expected pair, with a state — `n` and
    a closure once it has them, "connecting" while young, "no channel" once it
    has been long enough, and "lost Ns ago" for a reaped one. Same instinct as
    the `errAgeS` work: a number with no freshness is worse than no number.
  - Wants a party-relevant note in the UI, because this is exactly the failure a
    room full of guest phones will produce.

- [ ] **Retry a mesh pair that fails to form, or dies** (2026-09-02; needs
      `player.js`, so it rides with the next fleet reload)
  - `player.js` builds each pair as `new RTCPeerConnection({iceServers: []})`
    and handles only `onicecandidate`: no `connectionstatechange`, no
    `restartIce`, no teardown-and-re-signal. So a pair whose ICE fails, or whose
    channel dies after forming, is gone for the rest of the page life - the
    conductor reaps it from `mesh_seen` after 90 s and nothing ever tries again.
  - Shape: on `failed` (or `disconnected` for more than a few seconds) close the
    pair and ask the conductor to re-signal it - `_push_mesh_roster` already
    knows who should pair with whom. Bounded retries with backoff, so a
    genuinely unreachable pair (the hotspot case below) does not spin forever.
  - Pairs with the absent-pair item above: "no channel" should also say how
    many times it was tried. Diagnostics only - the star path and the servo
    never see the mesh - but it is a `player.js` change, so it needs a reload
    and should ride with one that is already owed.
- [ ] **Two control sockets disagreed about the mesh** (2026-08-27, unexplained)
  - A long-lived observer sampling every 15 s reported 2 mesh pairs for its whole
    300 s run; two short-lived queries in the same window reported 0. A later
    95 s sample at 2 s resolution showed 0 pairs and **zero transitions**, so it
    was not flapping between samples.
  - The tablet leaving explains an empty mesh at the *end* (both pairs involved
    it), but not two simultaneous observers of the same `_broadcast_control`
    fan-out disagreeing.
  - Two candidates: the monitor's own change-detection was subtly wrong (likely,
    and cheapest to rule out), or the snapshot's mesh contents can depend on
    which socket is asking (a real bug). Worth ten minutes with two sockets and
    a diff next time devices are up.

- [x] ~~**The laptop<->phone mesh pair is missing on the hotspot setup**~~ —
      **answered 2026-08-27 against a control.** The same two devices form the
      pair immediately on the local network (rtt 4.90, n=16). It was the phone's
      access point, exactly as Matthew read it: fine at *forwarding* UDP between
      clients, unreachable as a WebRTC peer on its own hotspot interface. Left
      here rather than deleted because it is the reason the mesh needs to say
      *why* a pair is absent — see the item above.
  - Original note:
  - Fleet was laptop + phone + "Mums tablet", with the **phone acting as the
    access point** and both others as its clients. The mesh table showed only
    `tablet<->laptop` and `tablet<->phone`. The third pair never appeared.
  - Benign reading: it simply had not accumulated samples yet (the other two
    were on n=22 and n=7, so nothing was well-sampled).
  - The reading worth checking: a hotspot can enforce **AP isolation**, or put
    clients where ICE cannot build a direct DataChannel. If those two cannot
    reach each other peer-to-peer at a desk, they will not at a party — and the
    mesh silently degrades to "no row" rather than saying so. A pair that never
    forms should probably be distinguishable from one that has no samples yet.
  - It is also the constraint that would make endpoint mapping *testable*: with
    it, 5 equations over 4 unknowns leaves a residual to check. Without it the
    solve is exactly determined and proves nothing.

- [ ] **Endpoint-cost mapping is a real instrument now — write it down before
      it is re-derived** (2026-08-27)
  - Every pair's RTT is the sum of what each end costs, so a few pairs
    over-determine per-node figures. Two independent fits so far:
    - router network: laptop 0.87, pc 1.16, phone 2.34, tablet 2.33 ms one-way
    - phone-as-hotspot: laptop 0.85, phone 1.45, tablet 3.75 ms one-way
    The **laptop reproduces at 0.87 vs 0.85 across completely different
    topologies**, which is the result that says the model is measuring the
    device rather than the network.
  - The WebRTC DataChannel costs ~0.8-1.3 ms over the WebSocket on the same
    pair, so the mesh `rtt ms` column and the `best rtt` column are **not
    directly comparable**. Worth a tooltip.
  - `rtt ms` is a *minimum*: a min over ~20 draws sits 2-3 ms above a min over
    ~115. Never compare pairs at different `n` without saying so.
  - Prediction not yet tested: with a phone as AP, reaching *the phone* costs
    its whole browser stack while reaching *through* it is a kernel forward — so
    per air-hop, transiting the AP should be cheaper than terminating at it.
  - Limit, already settled under Considered and dropped: this maps **topology,
    not geometry**. Propagation over 10 m is 33 ns; what rises with distance is
    retransmissions.

- [ ] **For meatthread0 (curiosity, not a blocker): measure the tablet's audio
      clock** (60 seconds, no code change, no fleet reload, no new hardware)
  - **Thread parked 2026-08-27.** The tablet is an older device and slower than
    what the fleet will be built around, so its 380 ppm is logged as an outlier
    rather than chased. No newer device needs borrowing to settle anything: the
    *phone* is Android too and reads an entirely ordinary -67 ppm, so "is Android
    broken" is already answered by hardware in the room. The open version of that
    question — do arbitrary guest phones hold sync — belongs to party mode.
  - **Route confirmed 2026-08-27: internal speaker.** That is the answer that
    makes this worth measuring. On Bluetooth/USB/HDMI a ~380 ppm audio clock is
    ordinary and there would be nothing to chase; on the internal speaker it is
    4-20x outside the class consumer audio crystals are built to, so either the
    P-droop reading is wrong or the platform is reporting a bad number. No
    simple units slip lands there either (2048/2047 = 489 ppm, 4096/4095 = 244),
    so it is not an off-by-one in a divisor.
  - **How.** Tablet joined and *playing* (a suspended context freezes
    `currentTime` and the measurement reads zero). On the conductor box open
    `chrome://inspect`, inspect the tablet's SyncPlay page, and paste this into
    its console. `player.js` is a classic script, so `ctx` is in scope.

        (async () => {
          const t = () => { const o = ctx.getOutputTimestamp();
            return {c:o.contextTime, p:o.performanceTime,
                    cur:ctx.currentTime, now:performance.now()}; };
          console.log("sampleRate", ctx.sampleRate, "outputLatency", ctx.outputLatency,
                      "baseLatency", ctx.baseLatency, "state", ctx.state);
          const a = t();
          console.log("pair usable:", a.c > 0 && a.p > 0, a);
          await new Promise(r => setTimeout(r, 60000));
          const b = t();
          const span = (b.p - a.p) / 1000;
          const dC = (b.c - a.c) - span;
          console.log("span", span.toFixed(1), "s");
          console.log("audio vs CPU:", (dC / span * 1e6).toFixed(1), "ppm   <-- the number");
          const span2 = (b.now - a.now) / 1000;
          console.log("cross-check via currentTime:",
                      (((b.cur - a.cur) - span2) / span2 * 1e6).toFixed(1), "ppm");
        })();

  - **Reading it.** The two figures should agree — in Chrome `currentTime` is
    also a frame counter, so they share the same drift and disagreement means
    the pair is unusable rather than that one is wrong.
    - **~+380 ppm** — the servo has been telling the truth all along, `err ms`
      is a measurement, and the tablet's audio clock is genuinely that far out.
      Then the question becomes whether to add an integral term (see above), and
      `ctx.sampleRate` vs the device's real rate says whether it is a units bug.
    - **~0 ppm** — P-droop is dead despite surviving four adversarial lenses,
      and the 6 ms is coming from somewhere none of them looked. Start again
      from the `err ms` cell: if it renders *stale*, the ack stream had died and
      every reading so far is void.
    - **Anything else** — the number is the disturbance; compare against
      `-(ratePpm) - skewPpm` in the tablet's err tooltip on the control page.
      They are two independent routes to the same quantity and should match.
  - While in there, note `ctx.sampleRate`, `ctx.outputLatency` and
    `ctx.baseLatency` — the dashboard wishlist wants all three anyway, and they
    say whether the deep-buffer/offload path is in play.

- [x] ~~**`onSteer` can dereference a nulled `current`**~~ — **fixed 2026-08-31**
      (`5e7cc2d`). `startSource` now refuses *before* `stopCurrent()`, returns a
      boolean, and sends `startRefused`; the conductor logs and toasts it. Guard
      written `!(seekS < buf.duration)` so a NaN seek is refused too. Verified
      old-vs-new in `tools/reanchor_harness.js`. **Needs the fleet reload.**

- [x] ~~**The slew dead zone**~~ — **fixed 2026-08-31** (`99e1789`). Between the
      servo's 12 ms saturation point and `REANCHOR_S` (200 ms) nothing could be
      corrected in useful time; a live node at +122 ms faced 152 s of echo.
      Lowering `REANCHOR_S` was not an option because the Android 6 tablet
      swings +/-12 ms and would have restart-looped, so the servo now times how
      long it has been outside `SLEW_LIMIT_S` (24 ms) and re-anchors after
      `SLEW_PATIENCE_S` (10 s) only if it *stays* out. **Needs the fleet reload.**

- [x] ~~**No way to tell which `player.js` a node is running**~~ — **built
      2026-09-02.** The conductor hashes the `player.js` it serves and stamps it
      into each page (`window.PLAYER_BUILD`); the player echoes it in `hello`;
      the control page marks a mismatch with ⟳ beside the node name and
      `note_build()` logs it. No constant to bump, and the stamp rides on the
      *page* so a bare WebSocket reconnect cannot launder a stale node.
      **Needs one reload to take effect**, after which it answers itself.

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
  - ~~**An integral term**~~ — **declined 2026-08-27, with numbers.** It is the
    real fix for standing droop, but on the fleet that matters the droop spans
    1.5 ms across laptop/pc/phone = **0.51 m of air**, which is finer than
    speakers get placed and is what `plan_nudges` measures anyway. An audio-path
    change plus a fleet reload to recover less than one knock of a speaker
    stand. Revisit only if a fleet ever shows a *spread* (not an outlier) worth
    more than the placement error.
    - Worth recording: the feared ordering problem was imaginary. It would
      **not** destroy the measurement, because `audioClockPpm` reads the
      standing trim rather than `err` — with integral action the trim still
      parks at the disturbance while `err` goes to zero. The slice shipped
      2026-08-27 was already the prerequisite.
  - ~~**The route question**~~ — **answered 2026-08-27: internal speaker.** So
    380 ppm is not an ordinary external-sink free-run; it is a 4-20x outlier and
    wants measuring rather than theorising about. See the meatthread0 item above.

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
  - ~~Pairs with gating `_catchup` on estimate *quality* rather than existence~~
    — **done 2026-09-02** (`start_ready`; see Done). The note below is kept for
    its numbers.
  - Original: gating `_catchup` on estimate *quality* rather than existence —
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
