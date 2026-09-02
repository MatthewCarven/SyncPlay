/* SyncPlay player node.
 *
 * Deliberately dumb: echo timestamp pings, prefetch + decode tracks, and
 * start playback at the node-local millisecond the conductor commands.
 * All clock estimation happens server-side; the only clever bit here is
 * mapping performance.now() time onto the AudioContext timeline.
 */
"use strict";

// --- identity ---------------------------------------------------------------
const clientId = (() => {
  // Dev override: ?id=x lets several tabs in ONE browser act as separate
  // nodes (they'd otherwise share the same localStorage identity).
  const forced = new URLSearchParams(location.search).get("id");
  if (forced) return "dev-" + forced.slice(0, 40);
  let id = localStorage.getItem("syncplay.clientId");
  if (!id) {
    if (crypto.randomUUID) {
      id = crypto.randomUUID();
    } else {
      // Plain-http LAN origins aren't "secure contexts", so randomUUID is
      // unavailable on real devices — but getRandomValues works everywhere.
      const b = crypto.getRandomValues(new Uint8Array(16));
      id = "id-" + Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
    }
    localStorage.setItem("syncplay.clientId", id);
  }
  return id;
})();

const $ = (id) => document.getElementById(id);
$("nameInput").value = localStorage.getItem("syncplay.name") || "";

// --- state ------------------------------------------------------------------
let ctx = null;          // AudioContext, created on the join gesture
let master = null;       // master GainNode
let ws = null;
let joined = false;
let nudgeMs = 0;
let reconnectDelay = 1000;
let wakeLock = null;
let analyser = null;     // taps master to feed the control-page spectrum
let eq = null;           // per-node output EQ chain: source -> eq -> master
let micStream = null;    // getUserMedia stream when this device is a calibration mic
let micAnalyser = null;
let micTimer = null;
let micCapNode = null;   // AudioWorklet that forwards mic samples during a measurement
let capActive = null;    // in-flight capture: {seq, need, have, got:[], startFrame}

const cache = new Map(); // trackId -> AudioBuffer (capped at 2)
const loading = new Set();
let current = null;      // {src, trackId, title, rate, anchorCtx, anchorPos, startedCtx}

// --- v2 servo tuning ---------------------------------------------------------
const STEER_HORIZON_S = 15;  // aim to null the position error over ~this long
const MAX_RATE_TRIM = 8e-4;  // ±800 ppm ≈ 1.4 cents of pitch — inaudible
const REANCHOR_S = 0.2;      // beyond this, slewing is hopeless: restart in place
// Between the two lines above there is a gap the servo cannot cover in any
// reasonable time. It saturates at MAX_RATE_TRIM * STEER_HORIZON_S = 12 ms;
// past that it is at full authority and removes error only *linearly*, at
// 800 ppm — 0.8 ms per second. So a 122 ms error (observed live, 2026-08-27) is
// too small to trip REANCHOR_S and takes 152 s to slew away. Two and a half
// minutes of audible echo, and a track change landing first resets the attempt.
//
// The fix is not a lower REANCHOR_S. A node whose offset estimate wobbles
// swings through +/-12 ms continuously (also observed live), and a threshold low
// enough to catch a stuck node would put a swinging one into a restart loop —
// trading a fixable problem for an unfixable one. What separates them is not
// magnitude but *persistence*: a swinging error crosses zero every few seconds,
// a stuck one does not. So this waits.
const SLEW_LIMIT_S = 2 * MAX_RATE_TRIM * STEER_HORIZON_S;  // 24 ms; 2x saturation
const SLEW_PATIENCE_S = 10;  // still out there after this long, and slewing lost

// --- clock mapping: performance.now() ms -> AudioContext seconds -------------
// getOutputTimestamp() returns a correlated pair: "context position X was/will
// be at the speaker at performance time Y" — so scheduling through it bakes in
// the device's output latency. Fallback: currentTime minus reported latency.
function perfToCtx(perfMs) {
  if (ctx.getOutputTimestamp) {
    const ts = ctx.getOutputTimestamp();
    if (ts && ts.contextTime > 0 && ts.performanceTime > 0) {
      return ts.contextTime + (perfMs - ts.performanceTime) / 1000;
    }
  }
  const latency = ctx.outputLatency || ctx.baseLatency || 0;
  return ctx.currentTime + (perfMs - performance.now()) / 1000 - latency;
}

// --- join -------------------------------------------------------------------
$("joinBtn").addEventListener("click", async () => {
  const name = $("nameInput").value.trim() || "node-" + clientId.slice(0, 4);
  localStorage.setItem("syncplay.name", name);

  ctx = new (window.AudioContext || window.webkitAudioContext)({ latencyHint: "playback" });
  await ctx.resume(); // inside the gesture: unlocks audio on iOS/Android
  master = ctx.createGain();
  master.gain.value = $("vol").value / 100;
  master.connect(ctx.destination);

  // Passive tap for the control-page spectrum. master is the one bus every
  // source (and the beep) flows through, so a single AnalyserNode hung off it
  // sees all output. It's a read-only sink: nothing reaches it *instead* of the
  // speakers, it adds no latency, and it's invisible to the servo (which reads
  // position off current.*, upstream of master). Feeds the spectrum tap below.
  analyser = ctx.createAnalyser();
  analyser.fftSize = 256;                // -> 128 frequency bins
  analyser.smoothingTimeConstant = 0.6;
  master.connect(analyser);
  startSpectrum();

  // Per-node output EQ, spliced between every source and master
  // (source -> eq -> master), so the spectrum tap on master shows the *shaped*
  // output. Born flat (0 dB = transparent); the beep skips it. Tone only — the
  // servo reads position off current.*, entirely upstream of these filters.
  eq = buildEq();

  joined = true;
  $("joinView").style.display = "none";
  $("liveView").style.display = "block";
  requestWakeLock();
  connect();
});

$("vol").addEventListener("input", () => {
  if (master) master.gain.value = $("vol").value / 100;
});

$("micBtn").addEventListener("click", toggleMic);

function applyVolume(v) { // pushed from the control page
  $("vol").value = v;
  if (master) master.gain.value = v / 100;
}

async function requestWakeLock() {
  try { wakeLock = await navigator.wakeLock.request("screen"); } catch (_) {}
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && joined) {
    requestWakeLock();
    if (ctx && ctx.state !== "running") ctx.resume();
  }
});

// --- websocket --------------------------------------------------------------
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/player`);

  ws.onopen = () => {
    reconnectDelay = 1000;
    setStatus(true, "syncing…");
    ws.send(JSON.stringify({
      type: "hello", clientId,
      name: localStorage.getItem("syncplay.name"),
      ua: navigator.userAgent.slice(0, 110),
      mesh: typeof RTCPeerConnection !== "undefined",
      // Stamped into this page by the conductor when it was served, so it
      // identifies the player.js actually running here — and keeps doing so
      // across a WebSocket reconnect, because the page did not reload.
      build: (typeof window !== "undefined" && window.PLAYER_BUILD) || null,
    }));
  };

  ws.onmessage = (ev) => {
    const c1 = performance.now(); // stamp receipt BEFORE parsing
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    handle(msg, c1);
  };

  ws.onclose = () => {
    setStatus(false, `reconnecting in ${Math.round(reconnectDelay / 1000)}s…`);
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 8000);
  };
  ws.onerror = () => ws.close();
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

function handle(msg, c1) {
  switch (msg.type) {
    case "ping":
      send({ type: "pong", id: msg.id, t0: msg.t0, c1, c2: performance.now() });
      break;
    case "welcome":
      nudgeMs = msg.nudgeMs || 0;
      if (typeof msg.volume === "number") applyVolume(msg.volume);
      applyEq(msg.eqDb);
      meshTeardown(); // fresh session: the roster that follows rebuilds it
      setStatus(true, `joined as “${msg.name}”`);
      // A reconnect without a page reload keeps our decoded buffers, but the
      // server forgot about them — re-announce so catchup can be instant.
      for (const [trackId, buf] of cache)
        send({ type: "loaded", trackId, durationMs: buf.duration * 1000 });
      break;
    case "config":
      nudgeMs = msg.nudgeMs || 0;
      if (typeof msg.volume === "number") applyVolume(msg.volume);
      applyEq(msg.eqDb);
      break;
    case "steer":
      onSteer(msg);
      break;
    case "meshRoster":
      try { syncMesh(msg.peers || []); } catch (e) { console.warn("mesh roster", e); }
      break;
    case "meshSignal":
      onMeshSignal(msg.from, msg.payload || {});
      break;
    case "preload":
      onPreload(msg);
      break;
    case "arm":
      onArm(msg);
      break;
    case "disarm":
      clearArm();
      break;
    case "play":
      onPlay(msg);
      break;
    case "stop":
      onStop(msg);
      break;
    case "beep":
      onBeep(msg);
      break;
    case "measureEmit":
      onMeasureEmit(msg);
      break;
    case "measureArm":
      onMeasureArm(msg);
      break;
    case "stats":
      $("stOffset").textContent = msg.offsetMs.toFixed(2);
      $("stRtt").textContent = msg.rttMs.toFixed(1);
      $("stSkew").textContent = msg.skewPpm.toFixed(1);
      break;
  }
}

// --- activity: what this device is actually doing right now --------------------
// Everything here is derived from state the player already holds, so the bar
// never lies about a phase the audio path isn't really in. Download and decode
// are tracked separately because they feel identical from outside (a wait) and
// behave nothing alike: one is the network, the other is ~85 MB of PCM and a
// busy main thread.
const dl = new Map();        // trackId -> percent, or null when size is unknown
const decoding = new Set();  // trackId, while decodeAudioData is chewing
let armed = false;           // countdown is up

// The one speculative load allowed in flight: {trackId, ctl}. A prefetch is a
// *guess* about what plays next, and the conductor re-guesses on every queue
// edit — so without this, reordering the queue five times started five 30 MB
// fetches and five ~85 MB decodes on top of each other. On a tablet that is an
// out-of-memory decode rejection, and the node drops out of the song entirely.
// A guess that has been superseded is simply wrong, so it gets aborted.
let prefetching = null;

// decodeAudioData is the memory-expensive half — one 4-minute track is ~85 MB
// of PCM — and it gives no way to bound its own concurrency. So they queue.
// One at a time is the actual OOM guard; cancelling superseded fetches above
// only stops the pile-up forming in the first place.
let decodeChain = Promise.resolve();

function decodeSerial(bytes, signal) {
  const run = decodeChain.then(() => {
    // The wait may have outlived the reason for the load; don't spend the
    // memory on a buffer nobody is going to ask for.
    if (signal && signal.aborted) throw new DOMException("Aborted", "AbortError");
    return ctx.decodeAudioData(bytes);
  });
  decodeChain = run.catch(() => {});  // one failure must not stall the chain
  return run;
}

// A preload is either a guess (`prefetch`) or the file we are about to play.
// Only guesses are cancellable, and *any* newer preload supersedes one — a
// demand load included, which is the "rebalance the queue, then hit play" case
// this exists for: the guess must never compete with the file actually needed.
function onPreload(msg) {
  const tid = msg.trackId;
  if (prefetching && (prefetching.trackId !== tid || !msg.prefetch)) {
    if (prefetching.trackId !== tid) prefetching.ctl.abort();
    // Same track arriving as a demand load: promote it. It is no longer a
    // guess, so nothing may cancel it from here on.
    prefetching = null;
  }
  if (cache.has(tid) || loading.has(tid)) return;
  const ctl = msg.prefetch && "AbortController" in window ? new AbortController() : null;
  if (ctl) prefetching = { trackId: tid, ctl };
  loadTrack(tid, msg.url, ctl && ctl.signal);
}

function renderActivity() {
  const curId = current && current.trackId;
  // A transfer for anything other than what's sounding is the prefetch of the
  // next track. Worth distinguishing: one is a wait, the other is housekeeping.
  const [dlId, dlPct] = dl.size ? [...dl][0] : [null, null];
  const decId = decoding.size ? [...decoding][0] : null;
  const busy = dlId ? "downloading" : (decId ? "decoding" : null);
  const pct = dlId && dlPct !== null ? `${dlPct}%` : "";

  let text, cls;
  if (armed) {
    text = busy ? `arming · ${busy}` : "arming";
    cls = "busy";
  } else if (current) {
    // While playing, any transfer in flight is for a *different* track.
    text = busy && (dlId || decId) !== curId ? `playing · ${busy} next file` : "playing";
    cls = "play";
  } else if (busy) {
    text = busy;
    cls = "busy";
  } else {
    text = "idle";
    cls = "";
  }
  $("activityText").textContent = text;
  $("activityPct").textContent = pct;
  $("activity").className = cls;
}

// --- track loading ------------------------------------------------------------
// One second chance. A decode can fail for reasons that pass — a transient
// memory squeeze, a Wi-Fi blip mid-transfer — and giving up permanently on the
// first of those leaves the node silent for the whole song. Retrying forever
// would be worse: it turns one struggling device into a machine for hammering
// the very link that is already the problem.
const LOAD_RETRIES = 1;
const LOAD_RETRY_MS = 1500;  // long enough for a blip, or a GC, to pass

async function loadTrack(trackId, url, signal) {
  if (cache.has(trackId) || loading.has(trackId)) return cache.get(trackId);
  loading.add(trackId);
  try {
    for (let attempt = 0; ; attempt++) {
      try {
        return await loadOnce(trackId, url, signal);
      } catch (err) {
        // An abort is a decision, not a fault: never retried, never reported.
        // Reporting it would make the conductor toast a failure every time the
        // queue is reordered, and mark a node bad for doing what it was told.
        if (err && err.name === "AbortError") return null;
        if (attempt >= LOAD_RETRIES) {
          send({ type: "loadError", trackId, error: String(err).slice(0, 120) });
          return null;
        }
        await new Promise((r) => setTimeout(r, LOAD_RETRY_MS));
        if (signal && signal.aborted) return null;  // gave up on us while we waited
      }
    }
  } finally {
    loading.delete(trackId);
    dl.delete(trackId);        // an abandoned transfer must not leave the bar
    decoding.delete(trackId);  // stuck on "downloading" forever
    if (prefetching && prefetching.trackId === trackId) prefetching = null;
    renderActivity();
  }
}

// One attempt. Throws on any failure so the caller owns the retry policy, and
// so an abort stays distinguishable from a real fault all the way up.
async function loadOnce(trackId, url, signal) {
  dl.set(trackId, null);  // size unknown until the headers land
  decoding.delete(trackId);  // a retry starts from the transfer again
  renderActivity();
  const resp = await fetch(url || `/tracks/${trackId}`, signal ? { signal } : {});
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const bytes = await fetchWithProgress(resp, trackId);
  dl.delete(trackId);
  decoding.add(trackId);
  renderActivity();
  const buf = await decodeSerial(bytes, signal);
  cache.set(trackId, buf);
  // Decoded PCM is big (~85 MB per 4-min track): keep current + next only.
  for (const key of cache.keys()) {
    if (cache.size <= 2) break;
    if (key !== trackId && key !== (current && current.trackId)) {
      cache.delete(key);
      // The conductor's node.loaded is its only view of what we hold, and it
      // has no other way to learn about this. Left unsaid, it counts us ready
      // for a track we have dropped: the load gate skips us, the play command
      // finds no buffer, and we join late through catch-up instead of starting
      // with everyone. An eviction is a fact about us; we are the only ones who
      // can report it.
      send({ type: "unloaded", trackId: key });
    }
  }
  send({ type: "loaded", trackId, durationMs: buf.duration * 1000 });
  return buf;
}

// Stream the response body so we can report byte-download progress to the
// control page while a track pulls from the server, then hand the reassembled
// bytes to decodeAudioData. Purely cosmetic and fully outside the audio path:
// with no Content-Length (or no ReadableStream) we fall back to a plain read
// and the percentage simply never shows. Throttled to ~every 2% so even a
// 60 MB file can't flood the socket.
async function fetchWithProgress(resp, trackId) {
  const total = +resp.headers.get("Content-Length") || 0;
  if (!total || !resp.body || !resp.body.getReader) return resp.arrayBuffer();
  const reader = resp.body.getReader();
  const chunks = [];
  let received = 0, lastPct = -2;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    received += value.length;
    const pct = Math.min(100, Math.floor((100 * received) / total));
    if (pct >= lastPct + 2) {
      lastPct = pct;
      send({ type: "loadProgress", trackId, pct });
      dl.set(trackId, pct);
      renderActivity();
    }
  }
  const bytes = new Uint8Array(received);      // reassemble for decodeAudioData
  let o = 0;
  for (const c of chunks) { bytes.set(c, o); o += c.length; }
  return bytes.buffer;
}

// --- playback -------------------------------------------------------------------
async function onPlay(msg) {
  clearArm();  // the real start supersedes whatever the countdown predicted
  if (ctx.state !== "running") await ctx.resume();
  // Whatever we were guessing about next is now beside the point — this is the
  // file the room is waiting on, and it must not queue behind a guess.
  if (prefetching && prefetching.trackId !== msg.trackId) {
    prefetching.ctl.abort();
    prefetching = null;
  }
  let buf = cache.get(msg.trackId);
  if (!buf) buf = await loadTrack(msg.trackId, null); // late: decode, then catch up
  if (!buf) return;
  startBuffer(buf, msg);
}

// True audio position of the current source at AudioContext time ctxT,
// accounting for every playbackRate trim the servo has applied.
function posAt(ctxT) {
  return current.anchorPos + Math.max(0, ctxT - current.anchorCtx) * current.rate;
}

function startSource(buf, trackId, title, whenCtx, seekS) {
  // Refuse BEFORE tearing down what is playing. The old order called
  // stopCurrent() — which nulls `current` — and only then bailed on this
  // guard, so a refused start left the node silent with `current` null and
  // `onended` already detached: no `state` was ever sent, the conductor went on
  // steering a node that had stopped, and onSteer's own tail dereferenced the
  // null and threw. Refusing first makes a refusal cost nothing: whatever was
  // playing keeps playing and ends naturally.
  //
  // Written as !(a < b) rather than a >= b so a NaN seek is refused too — that
  // is the one value that would sail through the old comparison and hand
  // src.start() an argument it cannot use.
  if (!(seekS < buf.duration)) {
    send({ type: "startRefused", trackId,
           seekMs: Math.round(seekS * 1000),
           durationMs: Math.round(buf.duration * 1000) });
    return false;
  }
  stopCurrent();
  const src = ctx.createBufferSource();
  src.buffer = buf;
  src.connect(eq ? eq.input : master);
  src.start(whenCtx, seekS);
  current = {
    src, trackId, title,
    rate: 1, anchorCtx: whenCtx, anchorPos: seekS, startedCtx: whenCtx,
    slewSince: null,
  };
  src.onended = () => {
    if (current && current.src === src) {
      current = null;
      setNowPlaying(null);
      send({ type: "state", playing: null });
    }
  };
  setNowPlaying(title || trackId);
  send({ type: "state", playing: trackId });
  return true;
}

function startBuffer(buf, msg) {
  let when = perfToCtx(msg.atNodeMs + nudgeMs);
  let seekS = (msg.seekMs || 0) / 1000;
  const nowCtx = ctx.currentTime;
  if (when < nowCtx + 0.02) { // target already passed → join at the right spot
    seekS += (nowCtx + 0.06) - when;
    when = nowCtx + 0.06;
  }
  startSource(buf, msg.trackId, msg.title, when, seekS);
}

// v2 servo: the conductor says "at your local time L the song should be at P".
// Compare with where we'll actually be, trim playbackRate microscopically.
function onSteer(msg) {
  if (!current || current.trackId !== msg.trackId) return;
  const targetCtx = perfToCtx(msg.atNodeMs + nudgeMs);
  if (targetCtx <= current.startedCtx + 0.05) return; // reference predates our start
  const errS = posAt(targetCtx) - msg.posMs / 1000;   // + = we're ahead

  // How long have we been out past where the servo can usefully pull? Cleared
  // the moment the error comes back inside, which is what makes this blind to a
  // node swinging through zero and sensitive to one genuinely stranded.
  if (Math.abs(errS) > SLEW_LIMIT_S) {
    if (current.slewSince == null) current.slewSince = ctx.currentTime;
  } else {
    current.slewSince = null;
  }
  const slewLost = current.slewSince != null
                   && ctx.currentTime - current.slewSince > SLEW_PATIENCE_S;

  if (Math.abs(errS) > REANCHOR_S || slewLost) {
    // Stall/suspend/mistiming too big to slew away — restart at the ideal spot.
    // Also reached when the servo has been at full authority for
    // SLEW_PATIENCE_S and is still losing: one discontinuity now beats another
    // minute of being audibly in the wrong place.
    const buf = cache.get(msg.trackId);
    if (!buf) return;
    const nowCtx = ctx.currentTime;
    const seekS = msg.posMs / 1000 + (nowCtx + 0.08 - targetCtx);
    startSource(buf, msg.trackId, current.title, nowCtx + 0.08, seekS);
  } else {
    const nowCtx = ctx.currentTime;
    current.anchorPos = posAt(nowCtx); // re-anchor bookkeeping at the old rate
    current.anchorCtx = nowCtx;
    current.rate = 1 - Math.max(-MAX_RATE_TRIM,
                                Math.min(MAX_RATE_TRIM, errS / STEER_HORIZON_S));
    current.src.playbackRate.setValueAtTime(current.rate, nowCtx);
  }
  send({ type: "steerAck", trackId: msg.trackId,
         errMs: errS * 1000, rate: current.rate });
}

function onStop(msg) {
  clearArm();
  if (!current) return;
  if (msg && typeof msg.atNodeMs === "number") {
    const when = Math.max(perfToCtx(msg.atNodeMs + nudgeMs), ctx.currentTime);
    try { current.src.stop(when); } catch (_) {}
  } else {
    stopCurrent(null);
    setNowPlaying(null);
    send({ type: "state", playing: null });
  }
}

function stopCurrent() {
  if (current) {
    const src = current.src;
    current = null; // clear first so onended doesn't double-report
    try { src.onended = null; src.stop(); } catch (_) {}
  }
}

function onBeep(msg) {
  const when = Math.max(perfToCtx(msg.atNodeMs + nudgeMs), ctx.currentTime);
  const osc = ctx.createOscillator();
  const g = ctx.createGain();
  osc.frequency.value = 880;
  g.gain.setValueAtTime(0, when);
  g.gain.linearRampToValueAtTime(0.6, when + 0.005);
  g.gain.setValueAtTime(0.6, when + 0.1);
  g.gain.linearRampToValueAtTime(0, when + 0.13);
  osc.connect(g).connect(master);
  osc.start(when);
  osc.stop(when + 0.15);
}

// --- spectrum tap: a cheap level meter for the control page -------------------
// Fully outside the audio path and purely cosmetic: a dozen times a second,
// while something is playing, read the analyser and ship compact byte bands to
// the conductor, which relays them to any open control page. Bands are
// log-spaced because musical energy bunches in the low end — linear FFT bins
// would leave the top half of the meter permanently dead.
const SPECTRUM_BANDS = 28;
const SPECTRUM_MS = 80;                   // ~12.5 fps; the UI eases it smooth
let specBins = null;                      // reused Uint8Array over the FFT bins
let specEdges = null;                     // geometric band boundaries, once

function startSpectrum() {
  specBins = new Uint8Array(analyser.frequencyBinCount);
  // Band k spans bins [edges[k], edges[k+1]); geometric from bin 1 (skip DC)
  // to the top, so each band is ~a constant musical interval wide.
  specEdges = new Array(SPECTRUM_BANDS + 1);
  const lo = 1, hi = analyser.frequencyBinCount;
  for (let k = 0; k <= SPECTRUM_BANDS; k++)
    specEdges[k] = Math.round(lo * Math.pow(hi / lo, k / SPECTRUM_BANDS));
  setInterval(sampleSpectrum, SPECTRUM_MS);
}

function sampleSpectrum() {
  if (!current || !analyser || ctx.state !== "running") return;
  analyser.getByteFrequencyData(specBins);
  const bands = new Array(SPECTRUM_BANDS);
  for (let k = 0; k < SPECTRUM_BANDS; k++) {
    let peak = 0;
    const end = Math.max(specEdges[k] + 1, specEdges[k + 1]); // >=1 bin per band
    for (let i = specEdges[k]; i < end && i < specBins.length; i++)
      if (specBins[i] > peak) peak = specBins[i];
    bands[k] = peak;
  }
  send({ type: "spectrum", bands });
}

// --- output EQ ---------------------------------------------------------------
// Five biquads in series (source -> eq -> master), pushed per-node from the
// control page and persisted server-side alongside nudge/volume. Shelves at the
// ends, peaking bands in the middle; ±12 dB. Gains ramp via setTargetAtTime so
// a slider drag is click-free. Tone only — zero effect on timing or play rate.
const EQ_BANDS = [
  { type: "lowshelf",  freq: 80 },
  { type: "peaking",   freq: 250,   q: 1.0 },
  { type: "peaking",   freq: 1000,  q: 1.0 },
  { type: "peaking",   freq: 4000,  q: 1.0 },
  { type: "highshelf", freq: 12000 },
];

function buildEq() {
  const filters = EQ_BANDS.map((b) => {
    const f = ctx.createBiquadFilter();
    f.type = b.type;
    f.frequency.value = b.freq;
    if (b.q) f.Q.value = b.q;
    f.gain.value = 0;               // born flat = transparent
    return f;
  });
  for (let i = 0; i < filters.length - 1; i++) filters[i].connect(filters[i + 1]);
  filters[filters.length - 1].connect(master);
  return { input: filters[0], filters };
}

function applyEq(gainsDb) {
  if (!eq || !Array.isArray(gainsDb)) return;
  const t = ctx.currentTime;
  eq.filters.forEach((f, i) => {
    const g = Math.max(-12, Math.min(12, +gainsDb[i] || 0));
    f.gain.setTargetAtTime(g, t, 0.02);  // ~60 ms smooth: click-free
  });
}

// --- calibration mic ---------------------------------------------------------
// Opt-in: turn this device into a measurement microphone. Captures input via
// getUserMedia (secure context only — HTTPS on the LAN, or localhost), computes
// a running RMS off an AnalyserNode, and reports the level so the control page
// can confirm the mic hears the room. Phase 1 groundwork for auto-nudge — no
// stimulus or cross-correlation yet. The mic is a sink only: never routed to
// the speakers, so there's no monitoring feedback.
const MIC_MS = 120;

async function toggleMic() {
  if (micStream) { stopMic(); return; }
  if (!ctx) { setMicStatus("join first — that unlocks audio"); return; }
  const md = navigator.mediaDevices;
  if (!md || !md.getUserMedia) {
    setMicStatus("mic needs a secure context (HTTPS on the LAN, or localhost)");
    return;
  }
  try {
    micStream = await md.getUserMedia({
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
    });
  } catch (err) {
    micStream = null;
    setMicStatus("mic unavailable: " + ((err && err.name) || err));
    return;
  }
  const src = ctx.createMediaStreamSource(micStream);
  micAnalyser = ctx.createAnalyser();
  micAnalyser.fftSize = 1024;
  src.connect(micAnalyser);            // sink only — never to ctx.destination
  await setupCapture(src);             // measurement capture path (step 2)
  const buf = new Float32Array(micAnalyser.fftSize);
  micTimer = setInterval(() => {
    micAnalyser.getFloatTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
    send({ type: "micLevel", rms: Math.sqrt(sum / buf.length) });
  }, MIC_MS);
  $("micBtn").classList.add("on");
  $("micBtn").textContent = "🎙️ calibration mic ON — tap to stop";
  setMicStatus("listening — watch the level on the control page");
  send({ type: "micMode", on: true });
}

function stopMic() {
  if (micTimer) { clearInterval(micTimer); micTimer = null; }
  if (micStream) { micStream.getTracks().forEach((t) => t.stop()); micStream = null; }
  micAnalyser = null;
  if (micCapNode) { try { micCapNode.disconnect(); } catch (_) {} micCapNode = null; }
  capActive = null;
  $("micBtn").classList.remove("on");
  $("micBtn").textContent = "🎙️ use as calibration mic";
  setMicStatus("");
  send({ type: "micMode", on: false });
}

function setMicStatus(text) {
  const el = $("micStatus");
  el.textContent = text;
  el.style.display = text ? "block" : "none";
}

// --- acoustic measurement (auto-nudge step 2) --------------------------------
// A speaker node emits a short swept-sine "chirp" at a clock-synced instant; the
// mic node captures a window and cross-correlates it against the same reference.
// The correlation peak is the direct-sound arrival — with the synced emit time,
// that's time-of-flight. Short chirp + short window keeps xcorr cheap enough to
// run in the time domain (no FFT). The chirp bypasses EQ + volume (fixed level).
const CHIRP_F0 = 500, CHIRP_F1 = 6000, CHIRP_S = 0.25;    // 0.5–6 kHz sweep, 250 ms
// A long, wide sweep is (a) unmistakably audible — a clear "whoop", not a blip —
// and (b) far higher matched-filter gain, so the correlation peak stands well
// clear of room noise. The swept phase keeps the autocorrelation sharp (good
// time resolution) regardless of length.

function makeChirpArray(sampleRate) {
  const n = Math.max(1, Math.round(CHIRP_S * sampleRate));
  const out = new Float32Array(n);
  const k = (CHIRP_F1 - CHIRP_F0) / CHIRP_S;              // linear sweep rate
  const denom = n > 1 ? n - 1 : 1;
  for (let i = 0; i < n; i++) {
    const t = i / sampleRate;
    const w = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / denom);  // Hann taper
    out[i] = w * Math.sin(2 * Math.PI * (CHIRP_F0 * t + 0.5 * k * t * t));
  }
  return out;
}

// Speaker role: play the chirp at the synced instant, with NO nudge (we're
// measuring the raw path). Straight to destination at a fixed gain — bypasses
// the EQ chain and the master volume so the level is known and repeatable.
function onMeasureEmit(msg) {
  if (!ctx) return;
  const when = Math.max(perfToCtx(msg.atNodeMs), ctx.currentTime + 0.02);
  const data = makeChirpArray(ctx.sampleRate);
  const buf = ctx.createBuffer(1, data.length, ctx.sampleRate);
  buf.copyToChannel(data, 0);
  const src = ctx.createBufferSource();
  src.buffer = buf;
  const g = ctx.createGain();
  g.gain.value = 0.6;
  src.connect(g).connect(ctx.destination);
  src.start(when);
}

// Mic role: an AudioWorklet forwards raw input blocks (frame-stamped) only while
// a capture is armed. Loaded once (via a Blob URL) when the mic turns on.
const CAP_WORKLET =
  "class Cap extends AudioWorkletProcessor{" +
  "constructor(){super();this.left=0;this.port.onmessage=e=>{this.left=e.data.frames|0;};}" +
  "process(inputs){if(this.left>0){const ch=inputs[0]&&inputs[0][0];" +
  "if(ch){this.port.postMessage({frame:currentFrame,data:ch.slice(0)});this.left-=ch.length;}}" +
  "return true;}}registerProcessor('syncplay-cap',Cap);";

async function setupCapture(src) {
  try {
    if (!ctx._capReady) {
      const url = URL.createObjectURL(new Blob([CAP_WORKLET], { type: "application/javascript" }));
      await ctx.audioWorklet.addModule(url);
      ctx._capReady = true;
    }
    micCapNode = new AudioWorkletNode(ctx, "syncplay-cap");
    micCapNode.port.onmessage = (e) => onCapBlock(e.data);
    const mute = ctx.createGain(); mute.gain.value = 0;   // silent sink so it's pulled
    src.connect(micCapNode); micCapNode.connect(mute).connect(ctx.destination);
  } catch (_) {
    micCapNode = null;                                    // no worklet: measurement off, level still works
  }
}

function onMeasureArm(msg) {
  if (!ctx || !micCapNode) { send({ type: "measureResult", seq: msg.seq, error: "no-capture" }); return; }
  const need = Math.ceil(((msg.windowMs || 550) / 1000) * ctx.sampleRate);
  capActive = { seq: msg.seq, need, have: 0, got: [], startFrame: -1 };
  micCapNode.port.postMessage({ frames: need });
}

function onCapBlock(b) {
  if (!capActive) return;
  if (capActive.startFrame < 0) capActive.startFrame = b.frame;
  capActive.got.push(b.data);
  capActive.have += b.data.length;
  if (capActive.have >= capActive.need) finishCapture();
}

function finishCapture() {
  const cap = capActive; capActive = null;
  const sig = new Float32Array(cap.have);
  let o = 0; for (const blk of cap.got) { sig.set(blk, o); o += blk.length; }
  const ref = makeChirpArray(ctx.sampleRate);
  const { lag, peak, snr } = crossCorrelate(sig, ref);
  const arrivalCtx = (cap.startFrame + lag) / ctx.sampleRate;
  send({
    type: "measureResult", seq: cap.seq,
    arrivalPerfMs: ctxToPerfMs(arrivalCtx), peak, snr,
    ...captureLevel(sig),
  });
}

// How loud the capture actually was. This is NOT derivable from `peak`: the
// correlation is normalized by signal energy, so it measures shape, not level —
// a mic recording pure silence still yields some number, and a dead input and a
// missed chirp look identical from the peak alone. That ambiguity is what cost
// us July: peak 0.02 with a laptop mic reading -74 dBFS, and no way to tell
// "your input is muted" from "the correlator failed". So report the level.
function captureLevel(sig) {
  let sum = 0, peakAbs = 0, clipped = 0;
  for (let i = 0; i < sig.length; i++) {
    const a = Math.abs(sig[i]);
    sum += sig[i] * sig[i];
    if (a > peakAbs) peakAbs = a;
    if (a >= 0.99) clipped++;
  }
  const db = (v) => (v > 1e-6 ? 20 * Math.log10(v) : -120);
  return {
    rmsDb: db(Math.sqrt(sum / (sig.length || 1))),
    peakDb: db(peakAbs),
    clipPct: sig.length ? (100 * clipped) / sig.length : 0,
  };
}

// Normalized time-domain cross-correlation: the integer lag where ref best fits
// inside sig. Returns the peak (0..1) and a crude SNR (peak / mean |corr|).
function crossCorrelate(sig, ref) {
  const n = sig.length, m = ref.length;
  let refE = 0;
  for (let j = 0; j < m; j++) refE += ref[j] * ref[j];
  const maxLag = n - m;
  let best = -Infinity, bestLag = 0, sumAbs = 0, count = 0;
  for (let lag = 0; lag <= maxLag; lag++) {
    let dot = 0, sigE = 0;
    for (let j = 0; j < m; j++) { const s = sig[lag + j]; dot += s * ref[j]; sigE += s * s; }
    const c = dot / (Math.sqrt(sigE * refE) || 1e-12);
    sumAbs += Math.abs(c); count++;
    if (c > best) { best = c; bestLag = lag; }
  }
  const meanAbs = count ? sumAbs / count : 1e-9;
  return { lag: bestLag, peak: best, snr: best / (meanAbs || 1e-9) };
}

// AudioContext seconds -> performance.now() ms, via the output-timestamp pair
// (the inverse of perfToCtx; any constant input latency cancels between speakers).
function ctxToPerfMs(ctxSec) {
  if (ctx.getOutputTimestamp) {
    const ts = ctx.getOutputTimestamp();
    if (ts && ts.contextTime > 0 && ts.performanceTime > 0)
      return ts.performanceTime + (ctxSec - ts.contextTime) * 1000;
  }
  return performance.now() + (ctxSec - ctx.currentTime) * 1000;
}

// --- mesh: client<->client ping channels (the "sync truth matrix") -----------
// WebRTC DataChannels with the conductor as signaling relay. Purely
// diagnostic and fully quarantined from the audio path: raw four-timestamp
// samples are relayed to the conductor, which does all the math. The pair's
// lower clientId initiates; unreliable channels behave like UDP on the LAN.
const mesh = new Map(); // peerId -> {pc, ch, timer}

function meshTeardown() {
  for (const [, p] of mesh) {
    try { clearInterval(p.timer); p.pc.close(); } catch (_) {}
  }
  mesh.clear();
}

function syncMesh(peers) {
  const want = new Set(peers.map((p) => p.id));
  for (const [id, p] of mesh) {
    if (!want.has(id)) {
      try { clearInterval(p.timer); p.pc.close(); } catch (_) {}
      mesh.delete(id);
    }
  }
  for (const p of peers) {
    if (p.id !== clientId && clientId < p.id && !mesh.has(p.id)) meshInitiate(p.id);
  }
}

function meshEntry(peerId) {
  const pc = new RTCPeerConnection({ iceServers: [] }); // LAN: no STUN needed
  pc.onicecandidate = (e) => {
    if (e.candidate) send({ type: "meshSignal", to: peerId, payload: { cand: e.candidate } });
  };
  const entry = { pc, ch: null, timer: null };
  mesh.set(peerId, entry);
  return entry;
}

async function meshInitiate(peerId) {
  try {
    const entry = meshEntry(peerId);
    const ch = entry.pc.createDataChannel("pings", { ordered: false, maxRetransmits: 0 });
    meshWireInitiator(entry, ch, peerId);
    const offer = await entry.pc.createOffer();
    await entry.pc.setLocalDescription(offer);
    send({ type: "meshSignal", to: peerId, payload: { sdp: entry.pc.localDescription } });
  } catch (err) { console.warn("mesh initiate failed", err); }
}

function meshWireInitiator(entry, ch, peerId) {
  entry.ch = ch;
  let seq = 0;
  const pending = new Map(); // ping id -> t0
  ch.onmessage = (e) => {
    const t3 = performance.now(); // stamp BEFORE parsing, as always
    let m; try { m = JSON.parse(e.data); } catch (_) { return; }
    if (m.k === "r" && pending.has(m.id)) {
      const t0 = pending.get(m.id);
      pending.delete(m.id);
      send({ type: "meshSample", peer: peerId, t0, c1: m.c1, c2: m.c2, t3 });
    }
  };
  ch.onopen = () => {
    entry.timer = setInterval(() => { // burst of 6, 80ms apart, every 8s
      for (let i = 0; i < 6; i++) {
        setTimeout(() => {
          if (ch.readyState !== "open") return;
          const id = ++seq;
          pending.set(id, performance.now());
          if (pending.size > 64) pending.delete(pending.keys().next().value);
          try { ch.send(JSON.stringify({ k: "p", id })); } catch (_) {}
        }, i * 80);
      }
    }, 8000);
  };
  ch.onclose = () => clearInterval(entry.timer);
}

function meshWireResponder(entry, ch) {
  entry.ch = ch;
  ch.onmessage = (e) => {
    const c1 = performance.now();
    let m; try { m = JSON.parse(e.data); } catch (_) { return; }
    if (m.k === "p") {
      try { ch.send(JSON.stringify({ k: "r", id: m.id, c1, c2: performance.now() })); } catch (_) {}
    }
  };
}

async function onMeshSignal(fromId, payload) {
  try {
    let entry = mesh.get(fromId);
    if (payload.sdp) {
      if (payload.sdp.type === "offer") {
        if (!entry) {
          entry = meshEntry(fromId);
          entry.pc.ondatachannel = (e) => meshWireResponder(entry, e.channel);
        }
        await entry.pc.setRemoteDescription(payload.sdp);
        const answer = await entry.pc.createAnswer();
        await entry.pc.setLocalDescription(answer);
        send({ type: "meshSignal", to: fromId, payload: { sdp: entry.pc.localDescription } });
      } else if (entry) {
        await entry.pc.setRemoteDescription(payload.sdp); // answer to our offer
      }
    } else if (payload.cand && entry) {
      await entry.pc.addIceCandidate(payload.cand);
    }
  } catch (err) { console.warn("mesh signal error", err); }
}

// --- cold-start countdown -----------------------------------------------------
// The conductor dates the target on *our* clock where it can, so every screen
// in the room hits zero together rather than each hitting it at its own error.
// Ticking is local: a countdown is a reassurance, not a scheduling primitive —
// the actual start still arrives as a `play` with its own sample-accurate time.
let armTimer = null;

function onArm(msg) {
  clearArm();
  armed = true;
  renderActivity();
  const endsAt = typeof msg.startsAtNodeMs === "number"
    ? msg.startsAtNodeMs
    : performance.now() + (msg.secondsLeft || 0) * 1000;
  $("armWhat").textContent = msg.title ? `getting “${msg.title}” ready` : "getting ready";
  $("arming").style.display = "block";

  const tick = () => {
    const left = (endsAt - performance.now()) / 1000;
    // Zero means "the wait we predicted is over", not "audio now" — if a slow
    // decode overran it, say so rather than freezing on 0 and looking hung.
    $("armCount").textContent = left > 0 ? Math.ceil(left).toString() : "starting…";
  };
  tick();
  armTimer = setInterval(tick, 100);
}

function clearArm() {
  if (armTimer !== null) { clearInterval(armTimer); armTimer = null; }
  armed = false;
  $("arming").style.display = "none";
  renderActivity();
}

// --- ui ----------------------------------------------------------------------
function setStatus(ok, text) {
  $("dot").className = "dot" + (ok ? " ok" : "");
  $("statusText").textContent = text;
}

function setNowPlaying(title) {
  const el = $("nowPlaying");
  el.textContent = title ? `♪ ${title}` : "nothing playing";
  el.className = title ? "active" : "";
  renderActivity();  // every start and stop passes through here
}
