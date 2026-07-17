# SyncPlay — parked ideas (prune freely, most of this may never happen)

Working rule that keeps the fear away: every feature must be **additive,
observable, revertible** — dashboard/rendering first, conductor scheduling and
the player audio path only when unavoidable, one commit per feature so
`git revert` is always an exit.

## Done
- [x] Read-only per-node position bar on the control page (2026-07-17) —
  ideal timeline + each node's reported err ms; zero server changes.

## Next candidates

- [ ] **Party mode — any node can submit a file to the playlist**
  - `POST /upload` on the conductor (size cap ~100 MB, audio-extension
    whitelist, sanitized filename) into `music/party/`, auto-rescan, toast
    "X added by <node>" on control.
  - The real design question: submissions want a **queue** (play order =
    submission order) layered over today's folder-scan playlist. Conductor
    grows a `queue: [track ids]`; auto-advance pops the queue first, falls
    back to folder order when empty.
  - Control page: veto/remove per queued item; maybe per-submitter cap.
  - Player page: an "add a song" file input, hidden behind a party-mode
    toggle on the control page.
  - Risk: touches auto-advance (real scheduling code) → do the queue as its
    own commit before the upload endpoint.

- [ ] **Client↔client ping testing — the "sync truth matrix"**
  - Today all measurement is star-topology (conductor↔node). Browsers can't
    accept inbound connections, so node↔node needs **WebRTC DataChannels**
    with the conductor as the signaling relay (it already has sockets to
    everyone). Unreliable/unordered channels ≈ UDP on LAN → lower jitter
    than the WS path.
  - The payoff is **triangle closure**: measure A↔B offset directly, compare
    with (A−conductor)−(B−conductor) from the star. The mismatch *is* the
    true end-to-end sync error, measured rather than inferred — display as
    a pairwise ms matrix on the dashboard. Pure diagnostics: zero effect on
    playback, maximal nerd joy. Could later feed nudge suggestions.
  - Stretch: a second use of the same channels — clients could relay track
    bytes to each other (mesh prefetch) — almost certainly never needed on
    a LAN.

- [ ] **More timing info on the dashboard** (cheap → fancy)
  - per-node `outputLatency`/`baseLatency` + sample rate (explains *why* a
    node needs the nudge it needs)
  - err-ms sparkline per node (servo behavior over the last minute)
  - RTT p50/p95 per node (Wi-Fi quality at a glance; data already in the
    ClockModel window)
  - offset-residual RMS vs the regression line = live measurement-noise
    estimate ("how much should I trust this node's numbers")

## Later / maybe
- Writable seek bar — conductor side is trivial (same scheduled-start math
  as resume, with a seek target); the UI/UX is the actual work.
- Shuffle + repeat modes; drag-to-reorder queue.
- Mic-based auto-calibration of per-node nudge (clap test / cross-correlate
  a chirp) — replaces by-ear nudging.
- HTTPS story (self-signed or tunnel) if ever needed: also unlocks
  `crypto.randomUUID` and — more importantly — **Wake Lock on phones**,
  which silently no-ops on plain http today; a sleeping phone suspends its
  AudioContext and the servo hard re-anchors on wake.
