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

const cache = new Map(); // trackId -> AudioBuffer (capped at 2)
const loading = new Set();
let current = null;      // {src, trackId, title, rate, anchorCtx, anchorPos, startedCtx}

// --- v2 servo tuning ---------------------------------------------------------
const STEER_HORIZON_S = 15;  // aim to null the position error over ~this long
const MAX_RATE_TRIM = 8e-4;  // ±800 ppm ≈ 1.4 cents of pitch — inaudible
const REANCHOR_S = 0.2;      // beyond this, slewing is hopeless: restart in place

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

  joined = true;
  $("joinView").style.display = "none";
  $("liveView").style.display = "block";
  requestWakeLock();
  connect();
});

$("vol").addEventListener("input", () => {
  if (master) master.gain.value = $("vol").value / 100;
});

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
      loadTrack(msg.trackId, msg.url);
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
    case "stats":
      $("stOffset").textContent = msg.offsetMs.toFixed(2);
      $("stRtt").textContent = msg.rttMs.toFixed(1);
      $("stSkew").textContent = msg.skewPpm.toFixed(1);
      break;
  }
}

// --- track loading ------------------------------------------------------------
async function loadTrack(trackId, url) {
  if (cache.has(trackId) || loading.has(trackId)) return cache.get(trackId);
  loading.add(trackId);
  try {
    const resp = await fetch(url || `/tracks/${trackId}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const buf = await ctx.decodeAudioData(await resp.arrayBuffer());
    cache.set(trackId, buf);
    // Decoded PCM is big (~85 MB per 4-min track): keep current + next only.
    for (const key of cache.keys()) {
      if (cache.size <= 2) break;
      if (key !== trackId && key !== (current && current.trackId)) cache.delete(key);
    }
    send({ type: "loaded", trackId, durationMs: buf.duration * 1000 });
    return buf;
  } catch (err) {
    send({ type: "loadError", trackId, error: String(err).slice(0, 120) });
    return null;
  } finally {
    loading.delete(trackId);
  }
}

// --- playback -------------------------------------------------------------------
async function onPlay(msg) {
  if (ctx.state !== "running") await ctx.resume();
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
  stopCurrent();
  if (seekS >= buf.duration) return;
  const src = ctx.createBufferSource();
  src.buffer = buf;
  src.connect(master);
  src.start(whenCtx, seekS);
  current = {
    src, trackId, title,
    rate: 1, anchorCtx: whenCtx, anchorPos: seekS, startedCtx: whenCtx,
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

  if (Math.abs(errS) > REANCHOR_S) {
    // Stall/suspend/mistiming too big to slew away — restart at the ideal spot.
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

// --- ui ----------------------------------------------------------------------
function setStatus(ok, text) {
  $("dot").className = "dot" + (ok ? " ok" : "");
  $("statusText").textContent = text;
}

function setNowPlaying(title) {
  const el = $("nowPlaying");
  el.textContent = title ? `♪ ${title}` : "nothing playing";
  el.className = title ? "active" : "";
}
