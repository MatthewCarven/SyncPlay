# Scoping — a swappable sync engine

Status: **scoped, nothing built** — but read on: step 2 of the ladder below (gate starts on estimate quality, reused by `_catchup`) landed on its own as `start_ready` on 2026-09-02, and all three pre-existing bugs listed at the end were fixed on 2026-08-23. Steps 1 and 3–5 remain unbuilt.

The question was "is this even possible?"
Short answer: yes, and the conductor side is much cheaper than it looks — but
only if the boundary is drawn *above the wire*. Drawn below it, the cost roughly
triples and "switch when stopped" quietly becomes "switch and everyone
re-joins".

Nothing here has been written or tested. This is a map, not a ladder that's
already been climbed. Every claim below was checked against the source and then
re-checked adversarially; where the second pass overturned the first, the
correction is kept in place rather than tidied away, because the corrections are
the interesting part.

## Why it's cheap: `ClockEstimate` is already most of the interface

Not "could be made into" — *is*. Every consumer in the conductor talks to a
frozen dataclass of plain numbers, and the entire surface it touches is:

| what | uses | where |
|---|---|---|
| `to_node_time(t)` | 7 | arm, play, pause, beep, steer, chirp emit, mesh |
| `offset_at(t)` | 5 | node stats, `stats` push, mesh closure |
| `to_conductor_time(t)` | 1 | mic arrival → conductor time |
| `skew`, `skew_fitted` | 2 | `remember_skew` |
| `skew_ppm`, `best_rtt`, `last_rtt`, `n_used`, `n_samples`, `span` | 11 | dashboard, cadence, mesh |

Three methods, eight read-only fields. The model surface is just as small:
constructed in 3 places (`Node.__init__`, `Node.begin_session`, the mesh pair),
fed by `.add()` in 2, `len()` in 1, `.estimate()` in **17 call expressions across
15 lines**.

All three methods are pure algebra over `(offset, skew, at)` — already *derived*,
not implemented per model. But the tempting one-liner ("emit the triple and
you're done") is **false**, in three specific ways that any engine contract has
to nail down:

1. **`ClockEstimate` requires five more fields with no defaults** — `best_rtt`,
   `last_rtt`, `n_samples`, `n_used`, `span`. They aren't decoration: `n_used`
   and `n_samples` drive `ping_boost`, and the rest are the dashboard. An engine
   that can't produce a meaningful RTT (a global solver, say) still has to
   produce *something*, and "what does `best_rtt` mean for an engine with no
   per-sample RTT" is a real question, not a filler value.
2. **`skew_fitted` is a contract obligation, not a diagnostic.**
   `remember_skew()` banks a skew only when `skew_fitted` is True. An engine that
   never sets it doesn't get a warning — it just silently stops persisting skew,
   and every returning node quietly loses its warm start. Nothing fails; it just
   gets worse.
3. **`to_conductor_time` divides by `(1 + skew)`.** That's total only because
   `ClockModel` clamps skew to ±`max_skew`. The clamp currently lives *inside the
   engine*, while the division lives in the *shared* dataclass — so a foreign
   engine emitting a wild skew makes shared code raise, and the sole call site
   catches only `(KeyError, ValueError, TypeError)`, so a `ZeroDivisionError`
   escapes. **The clamp should move into `ClockEstimate.__post_init__`** where
   the division is. Cheap now, ugly later.

So the honest contract is: *the triple, plus a diagnostics block, plus two
invariants the dataclass should enforce itself rather than trust.* Still small.
Just not one line.

`timesync.py`'s own docstring already says it's "deliberately extractable as its
own tiny library". It was written for this. The work is mostly admitting it.

## Why the swap is survivable: the blind window already exists

Swapping an engine means throwing away every sample and every estimate, so for a
moment every node is blind. That sounds alarming until you notice it is *exactly
the state a freshly joined node is in* — and therefore already handled,
everywhere. All 17 `estimate()` sites were checked individually. All 17 guard.

Two guards are weaker than the rest and are worth fixing while you're in there:

- **The pause guard is truthiness, not `is None`**
  (`at = est.to_node_time(...) if est else None`). Safe today only because
  `ClockEstimate` defines neither `__bool__` nor `__len__`. The moment someone
  adds a `__len__` returning `n_samples` — an entirely reasonable thing to do to
  a sample-backed estimate — every zero-sample estimate becomes falsy and every
  synced pause silently degrades to an unsynced one. That's a landmine with an
  engine-shaped trigger.
- **`_probe_once` schedules the chirp on one estimate and maps the arrival back
  through a freshly-read one.** Any model movement between emit and arrival
  contaminates `tofMs` directly. Across a model reset it's unbounded — so
  calibration must be locked out during a swap, which it broadly is (it already
  refuses while playing), but should be explicit.

## The hole a swap actually opens — sharper than it first looked

`_transport_play` gates on **loaded**, not on **estimated**. Today those coincide
by luck: a node with no estimate is a node that just joined, and a node that just
joined is also still decoding, so the load gate covers the gap without ever being
asked to.

After a swap that luck runs out, and it runs out in a specific, traceable way:

`Node.loaded` is per-page-life and is cleared only in `begin_session` — it has
nothing to do with the model. So post-swap every node is *still* loaded, and
`plan_start(playing_now=False, [True, True, True, True])` returns
`cold = False`. Three consequences fall out of that one boolean:

- **No `request_burst(ARM_SECONDS)`** — the swap skips the one mechanism that
  would refill the empty models.
- The load gate breaks on its first iteration.
- Play fires at `now() + PLAY_LEAD` — **1.8 s later, with zero samples in every
  model.**

Meanwhile the idle ping loop is ~0.7 s of burst plus up to `INTERVAL_IDLE` = 2.0 s
of waiting. So:

- **Swap lands in that wait with >1.8 s left → no pong arrives in time → zero
  samples → `_send_play` returns False for everyone.** `self.playing` is set
  anyway, the snapshot reports a normally advancing playhead, and the only
  consequence is a log line reading `"nobody"`. There is no toast, no rollback,
  no control-page signal. The only exit is `_auto_advance` firing **one whole
  track duration later**.
- **Swap lands anywhere else → play goes out on a single unfiltered sample**,
  carrying the full path asymmetry of one packet, with `skew` = prior or zero.

Both are bad. Only the first is silent, which makes it the worse one.

Worth knowing: `estimate()` is monotone. `add()` drops negative-RTT samples and
`filter_best` returns empty only for an empty pool, so once a model has one
sample it always has an estimate. `_send_play` returning False therefore means
*exactly zero samples* — never "a few, but poor".

The fix is small and already wanted. `request_burst` does exactly the right
thing (`node.kick.set()` wakes the ping loop immediately); it simply isn't called
here. So: on swap, `request_burst()` and hold "swapping" until every connected
node reports `n_used >= 8` or ~3 s elapses. The parked TODO item about `_catchup`
proceeding "as soon as `estimate() is not None`, i.e. after one surviving ping"
is the *same fix* — write the gate once, both call sites get it, and the second
one was already on the list.

## Three tiers, and where to stop

### Tier 1 — swap the estimator. Player untouched.

What varies is only how a pile of `PingSample`s becomes an estimate: min-RTT +
least squares (today), a Kalman filter on offset/skew, Huber or another robust
regression instead of a hard RTT gate, median-only-no-drift, or a mesh-consensus
global solve.

That last one decides the shape of the interface, so it's worth a moment even if
you never build it. A global solver can't live behind a *per-node* `ClockModel` —
it needs every node's samples plus the mesh pairs at once. So the seam wants to
be a `SyncEngine` **owned by the Conductor**, not a model owned by each Node:

```
engine.add_star(node_id, sample)
engine.add_mesh(a_id, b_id, sample)
engine.estimate(node_id) -> ClockEstimate | None
engine.drop(node_id)
```

Today's per-node `ClockModel` becomes a thin adapter behind that, and a future
global engine fits without a second refactor. This costs nothing now and is
expensive to retrofit — it's the one design decision here that has to be made up
front.

Wire protocol unchanged. `player.js` unchanged. **No fleet reload.**
Rough size: ~150 lines of plumbing plus tests. The bulk of any real work is the
second engine, not the seam.

### Tier 2 — also swap the cadence policy.

`ping_boost` / `boost_count` / `boost_interval` and the `BURST_*` constants are a
policy layered over the measurement stream, and they read `n_used`/`n_samples`
straight off the estimate. That is a *min-RTT-filter* notion of "how much
evidence did I get". A Kalman engine has no survival rate; it has innovation and
covariance, and it would want to ping harder on a different signal entirely.

So cadence becomes the engine's opinion —
`engine.cadence(node_id, playing) -> (count, spacing, interval)` — with today's
three functions as the default. Small change, but it alters behaviour for
**every** node rather than just returning ones, which is the same argument the
TODO already makes about re-tuning `filter_best`. Own commit, own test.

### Tier 3 — also swap the servo. Don't, yet.

The servo is split across the wire: `_steer_all()` here, `onSteer()` in
`player.js`. The conductor half swaps as easily as anything else. The player half
is in a page the node loaded, and the README already documents the consequence —
"nodes must (re)load the player page after a server upgrade to pick up new player
code".

Two ways out, both bad:

1. **Every engine's player-side law ships in every player page**, selected by a
   field on the message. Works, but every correction law you ever try is
   permanently resident in the bundle on a phone.
2. **A swap forces a fleet reload.** Which is not "switch when stopped" — it's
   "switch, then walk around the house tapping JOIN on four devices".

### The rule that keeps Tiers 1–2 honest

> An engine may change **how a node-clock timestamp is computed**.
> It may not change **what one means**.

And the frozen set is bigger than the obvious five. The full list of
conductor-originated fields carrying timing semantics into `player.js`:

- `play{atNodeMs, seekMs, trackId}` — `trackId` is load-bearing, not a label
- `steer{atNodeMs, posMs, trackId}` → `steerAck{errMs, rate}`
- `stop{atNodeMs}` (nullable — null means "stop now")
- `arm{startsAtNodeMs, secondsLeft}` — node-clock-dated, with a documented
  fallback when absent
- `beep{atNodeMs}`
- `measureEmit{atNodeMs}` — note it deliberately does **not** add `nudgeMs`,
  unlike every other scheduling path
- `measureArm` → `measureResult{arrivalPerfMs}` — **the only reverse-direction
  mapping in the system**, and the sole consumer of `to_conductor_time`. An
  engine that only implements the forward direction breaks calibration and
  nothing else, which is exactly the kind of failure nobody notices for a month.
- `welcome/config{nudgeMs}` — added to `atNodeMs` in *every* scheduling call, so
  it is as timing-bearing as `atNodeMs` itself
- `stats{offsetMs, rttMs, skewPpm}` — display only, **but** `player.js` calls
  `.toFixed(2)` on these with no null guard, inside an `onmessage` that wraps
  only `JSON.parse`. An engine that emits null for any of them throws in the
  player's message loop. One-line fix, worth doing in step 1.
- `meshRoster` / `meshSignal` — signalling for the client↔client channels

## What crosses a swap

**Discarded:** every star sample and estimate. That's the point.

**Kept — and this reverses my first read.** The mesh pair models survive. I
assumed they'd have to go, on the grounds that a pair model's timeline is the
initiator's clock. The premise is right; the conclusion was wrong. A pair model
contains *zero* conductor-clock data — every timestamp in a `meshSample` is
client-supplied, anchored to node A's `performance.now()`. A conductor-side
engine swap doesn't touch it. The star estimates are read fresh at snapshot time
and used only to *evaluate* the pair, so once the star models re-converge the
same pair models produce correct numbers again. Their real invalidation condition
is a page-life change, which `_drop_mesh_pairs_for` already handles on connect
and disconnect.

Discarding them would throw away up to 180 s of accumulated evidence for no
reason — and worse, it would throw away **the referee**, which is the whole point
of the exercise (below). Keeping them means closure can be read *across* a swap
and compared directly.

**Kept:** `prior_skew` — crystal drift belongs to the oscillator, not the
estimator, the same argument that already justifies carrying it across
reconnects. Two conditions: every engine must agree that skew is `d(offset)/dt`
in s/s with offset = node − conductor (assert it in the conformance suite; if an
engine ever disagrees, `_state["skews"]` has to be keyed by engine too), and
**the swap must call `remember_skew()` before replacing the model** or the
session's fitted skew is lost. Note `remember_skew` is correct-by-construction
across the swap itself — post-swap `est is None`, so it returns False and doesn't
clobber the stored value.

**Kept, obviously:** nudge, volume, EQ. Device properties, nothing to do with
timing estimation. Same for `self.paused`, which stores a position in the track,
not a clock value.

**Harmless, but visible:** `ping_boost` resets to 1.0 at the exact moment the
models are emptiest, and logs a spurious `4.00x -> 1.00x` transition. And the
player pages' on-screen offset/RTT/skew *freeze at stale values* rather than
blanking, because the conductor stops sending `stats` entirely when there's no
estimate. Both cosmetic; both confusing to watch if you don't expect them.

**Persisted:** the engine choice itself. Unlike the queue — "a mood, not a
setting" — which engine you're running *is* a setting. Into
`syncplay_state.json`, with a `--engine` CLI flag for the default.

## The payoff, which is not modularity

Modularity for its own sake would be architecture astronomy on a system that
already works and is in daily use. The reason to do it is that **the scoreboard
already exists**:

- **Triangle closure** measures direct A↔B offset against star-implied. That
  number does not come from the star engine — so it can judge the star engine. An
  independent referee, already on the dashboard, and (per above) one that
  survives the swap intact.
- **`err ms`** per node from `steerAck` — the servo's live opinion of each device.
- **`test_timesync.py`** is already an engine conformance suite in disguise: 270
  lines of synthetic skewed clocks asserting *behaviour*, not implementation.
  Parametrize it over the registry and every new engine clears the same bar
  before it goes near the house.

So this isn't "make it pluggable and hope". It's a rig for running A/B
experiments against four real nodes and reading the winner off a page.

## Suggested commit ladder

Additive, observable, revertible; each step a `git revert` from the last.

1. **Extract the interface, change no behaviour.** `SyncEngine` protocol +
   `StarEngine` wrapping today's `ClockModel`; registry with one entry; `--engine`
   accepting only `star`. Move the skew clamp into `ClockEstimate`; fix the
   truthiness guard in `_transport_pause`; null-guard `stats` in `player.js`.
   Tests parametrized over the registry.
   **Acceptance criterion: the fleet cannot tell the difference.**
2. **Gate starts on estimate quality** (`n_used >= 8` or a deadline), reused by
   `_catchup`, plus the missing toast when `_transport_play` reaches nobody.
   Ships value on its own even if steps 3–5 never happen — it closes a hole that
   exists today, it's just currently hard to reach.
3. **`switch_engine()` + a control-page dropdown**, disabled unless stopped;
   `request_burst()` on swap. With one engine registered it's a no-op you can
   *watch*: swap `star` → `star`, see all four nodes blank and reconverge in about
   a second, and confirm closure comes back to where it was.
4. **A second engine — and make it the boring one.** `flat`: median offset, no
   drift fit. That's the v1 behaviour, and it's the **control** in the
   experiment: you already know what it must do to the numbers (6–24 ms of drift
   over a 4-minute song). If closure and `err ms` *don't* visibly degrade under
   `flat`, the rig is lying — and finding that out costs one song, which is much
   better than discovering it while trying to judge a Kalman.
5. **Then the engine you actually wanted.**

Steps 1–3 are the modularity. Step 4 is what makes it trustworthy.

## Risks

- **Low, if Tier 1 only.** The default path is today's behaviour exactly; the
  seam is additive; one commit per step.
- **The blind window** is the only genuinely new state. It's bounded by the
  estimate gate in step 2 and can only happen while stopped — but note that step
  3 without step 2 is the dangerous ordering, because that's the combination that
  produces the silent-`playing` state described above. **Do not land the swap
  before the gate.**
- **Interface lock-in** lands in step 1: per-node model vs conductor-owned
  engine. Choose conductor-owned. Retrofitting means touching all 17 call sites a
  second time.
- **Scope creep into Tier 3.** The servo is the most tempting thing to make
  swappable and the only one that breaks the hot-swap property. If it becomes
  necessary, it's a different feature with a different name.

## Pre-existing bugs surfaced while scoping this

Unrelated to the engine work — found by reading the same paths, recorded so they
aren't re-derived. All three are small and independent.

1. **`_transport_play` never reports total failure.** It logs `"nobody"` but
   doesn't toast, unlike the no-load path right above it which does. The one
   failure mode that leaves state lying is the one with no user-visible signal.
2. **`_measure_one` isn't gated by `_calibrating`.** A manual 📏 during a sweep
   overwrites `_measure_pending` with no in-flight check, so the sweep's rep waits
   out the 3 s timeout and is dropped. `_measure_all` guards itself; `_measure_one`
   doesn't guard against it — which contradicts the stated purpose of the flag.
3. **`_state["skews"]` has no removal path.** Write-only, unlike `nudges` and
   `eqs` which both delete when cleared. A once-bad persisted skew survives every
   session and can't be cleared from memory; `_clean_skew` only filters at load.
