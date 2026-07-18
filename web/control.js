/* SyncPlay control page: live node stats + transport. Pure render-from-snapshot. */
"use strict";

const $ = (id) => document.getElementById(id);
let ws = null;
let snap = null;           // last snapshot from the conductor
let snapAtPerf = 0;        // performance.now() when it arrived (for position ticking)
let reconnectDelay = 1000;

$("playerUrl").textContent = location.origin + "/";

// --- websocket ---------------------------------------------------------------
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/control`);
  ws.onopen = () => { reconnectDelay = 1000; $("connState").textContent = "· live"; };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    if (msg.type === "snapshot") { snap = msg; snapAtPerf = performance.now(); render(); }
    else if (msg.type === "toast") toast(msg.text);
    else if (msg.type === "spectrum") specLatest.set(msg.nodeId, { bands: msg.bands, at: performance.now() });
    else if (msg.type === "micLevel") {
      micLevels.set(msg.nodeId, { rms: msg.rms, at: performance.now() });
      if (snap) renderCalibration();   // faster than the 1 Hz snapshot: lively meter
    }
  };
  ws.onclose = () => {
    $("connState").textContent = "· reconnecting…";
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 8000);
  };
  ws.onerror = () => ws.close();
}
connect();

function cmd(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

// --- transport buttons ---------------------------------------------------------
$("btnPlay").onclick = () => {
  if (!snap || !snap.tracks.length) return toast("no tracks in the library");
  const id = (snap.playing && snap.playing.trackId) ||
             (snap.paused && snap.paused.trackId) || snap.tracks[0].id;
  cmd({ cmd: "play", trackId: id });
};
$("btnPause").onclick = () => cmd({ cmd: "pause" });
$("btnResume").onclick = () => cmd({ cmd: "resume" });
$("btnNext").onclick = () => cmd({ cmd: "next" });
$("btnStop").onclick = () => cmd({ cmd: "stop" });
$("btnBeep").onclick = () => cmd({ cmd: "beep" });
$("btnResync").onclick = () => cmd({ cmd: "resync" });
$("btnRescan").onclick = () => cmd({ cmd: "rescan" });

// --- rendering -------------------------------------------------------------------
const fmt = (v, digits, dash = "—") =>
  (v === null || v === undefined || Number.isNaN(v)) ? dash : v.toFixed(digits);

function mmss(ms) {
  if (ms === null || ms === undefined) return "?:??";
  const s = Math.max(0, Math.round(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

// Where this node's audio actually is in the current song: the conductor's
// ideal timeline plus the node's own reported servo error. Read-only.
function nodePosMs(n) {
  const p = snap.playing;
  if (!p || n.playing !== p.trackId) return null;
  const elapsed = performance.now() - snapAtPerf;
  const ideal = (snap.serverNowMs + elapsed) - p.tStartMs + p.seekMs;
  return Math.max(0, ideal + (n.syncErrMs || 0));
}

function render() {
  if (!snap) return;

  // Don't clobber an input mid-edit: skip table rebuilds while one is focused.
  const active = document.activeElement;
  const editing = active && active.closest && active.closest("#nodeRows");
  if (!editing) renderNodeTable();

  renderSpectrumWidgets();
  renderEqWidgets();
  renderCalibration();
  renderMesh();
  renderPlaylist();
  renderNowLine();
}

function renderMesh() {
  const pairs = snap.mesh || [];
  $("meshRows").innerHTML = pairs.map((m) => {
    const mag = Math.abs(m.closureMs);
    const cls = mag < 2 ? "closureOk" : (mag < 10 ? "closureWarn" : "closureBad");
    return `<tr>
      <td>${esc(m.aName)} ↔ ${esc(m.bName)}</td>
      <td class="num">${m.directMs.toFixed(2)}</td>
      <td class="num ${cls}">${m.closureMs.toFixed(2)}</td>
      <td class="num">${m.rttMs.toFixed(1)}</td>
      <td class="num">${m.n}</td>
    </tr>`;
  }).join("");
  $("meshEmpty").style.display = pairs.length ? "none" : "block";
}

function renderNodeTable() {
  const rows = snap.nodes.map((n) => {
    const state = n.playing ? "♪ playing" : (n.loadedCurrent ? "ready" : (n.connected ? "idle" : "gone"));
    const err = n.playing ? fmt(n.syncErrMs, 1) : "—";
    const ratePpm = (n.ratePpm === null || n.ratePpm === undefined)
      ? "" : ` title="servo rate: ${n.ratePpm >= 0 ? "+" : ""}${n.ratePpm.toFixed(0)} ppm"`;
    return `<tr>
      <td><span class="dot ${n.connected ? "ok" : ""}"></span>${esc(n.name)}</td>
      <td class="num">${fmt(n.offsetMs, 2)}</td>
      <td class="num">${fmt(n.bestRttMs, 1)}</td>
      <td class="num">${fmt(n.skewPpm, 1)}</td>
      <td class="num"${ratePpm}>${err}</td>
      ${posCellFor(n)}
      <td class="num hideSm">${n.nUsed}/${n.nSamples}</td>
      <td class="num"><input class="nudge" type="number" step="5" value="${n.nudgeMs}"
            onchange="setNudge('${n.id}', this.value); this.blur()"></td>
      <td><input class="volSlider" type="range" min="0" max="100" value="${n.volume ?? 80}"
            onchange="setVol('${n.id}', this.value); this.blur()"></td>
      <td class="pill">${state}</td>
    </tr>`;
  });
  $("nodeRows").innerHTML = rows.join("");
  $("nodesEmpty").style.display = snap.nodes.length ? "none" : "block";
}

function renderPlaylist() {

  // playlist
  const nowId = snap.playing ? snap.playing.trackId : null;
  $("trackRows").innerHTML = snap.tracks.map((t) => `
    <tr class="trackRow">
      <td>${t.id === nowId ? "♪ " : ""}${esc(t.title)}</td>
      <td class="num">${t.durationMs ? mmss(t.durationMs) : ""}</td>
      <td class="num"><button class="playBtn" onclick="playTrack('${t.id}')">play</button></td>
    </tr>`).join("");
  $("tracksEmpty").textContent = snap.tracks.length ? "" :
    `no audio files found in ${snap.musicDir} — drop some in and hit ↻ rescan`;
  $("tracksEmpty").style.display = snap.tracks.length ? "none" : "block";
}

function renderNowLine() {
  if (!snap) return;
  if (snap.playing) {
    const p = snap.playing;
    const elapsed = performance.now() - snapAtPerf; // ticks between snapshots
    const pos = (snap.serverNowMs + elapsed) - p.tStartMs + p.seekMs;
    $("nowLine").innerHTML =
      `now playing <b>${esc(p.title)}</b> · ${mmss(pos)} / ${mmss(p.durationMs)}`;
    $("posFill").style.width = p.durationMs ? `${Math.min(100, 100 * pos / p.durationMs)}%` : "0";
  } else if (snap.paused) {
    $("nowLine").innerHTML =
      `paused <b>${esc(snap.paused.title)}</b> at ${mmss(snap.paused.positionMs)}`;
    $("posFill").style.width = snap.paused.durationMs
      ? `${Math.min(100, 100 * snap.paused.positionMs / snap.paused.durationMs)}%` : "0";
  } else {
    $("nowLine").textContent = "nothing playing";
    $("posFill").style.width = "0";
  }
}
setInterval(renderNowLine, 500);

// --- spectrum ("equalizer") --------------------------------------------------
// Nodes tap their own WebAudio output and stream compact byte bands here (via
// the conductor relay). Keep one bar-row per connected node; ease the bars with
// classic meter gravity so a ~12 fps feed still looks fluid. Display-only —
// nothing here can touch playback or timing.
const SPEC_BARS = 28;
const specLatest = new Map();  // nodeId -> {bands:[...], at: perfMs}
const specRows = new Map();    // nodeId -> {row, name, bars:[el], shown:[num]}
let specLastFrame = 0;

function renderSpectrumWidgets() {
  const panel = $("spectrumPanel");
  if (!panel) return;
  const nodes = snap.nodes.filter((n) => n.connected);
  const want = new Set(nodes.map((n) => n.id));

  for (const [id, w] of specRows) {
    if (!want.has(id)) { w.row.remove(); specRows.delete(id); specLatest.delete(id); }
  }
  for (const n of nodes) {
    let w = specRows.get(n.id);
    if (!w) {
      const row = document.createElement("div");
      row.className = "specRow idle";
      const name = document.createElement("div");
      name.className = "specName";
      const barsEl = document.createElement("div");
      barsEl.className = "specBars";
      const bars = [];
      for (let i = 0; i < SPEC_BARS; i++) {
        const b = document.createElement("div");
        b.className = "specBar";
        barsEl.appendChild(b);
        bars.push(b);
      }
      row.append(name, barsEl);
      panel.appendChild(row);
      w = { row, name, bars, shown: new Array(SPEC_BARS).fill(0) };
      specRows.set(n.id, w);
    }
    w.name.textContent = n.name;
  }
  $("spectrumEmpty").style.display = nodes.length ? "none" : "block";
}

function animateSpectrum(now) {
  const dt = specLastFrame ? Math.min(0.1, (now - specLastFrame) / 1000) : 0;
  specLastFrame = now;
  for (const [id, w] of specRows) {
    const frame = specLatest.get(id);
    const fresh = frame && (now - frame.at) < 250;  // a couple missed frames = idle
    for (let i = 0; i < SPEC_BARS; i++) {
      const target = fresh ? (frame.bands[i] || 0) / 255 : 0;  // 0..1
      // Attack instantly, release under gravity — the classic meter feel.
      w.shown[i] = target > w.shown[i] ? target : Math.max(target, w.shown[i] - dt * 1.8);
      w.bars[i].style.transform = `scaleY(${Math.max(0.02, w.shown[i]).toFixed(3)})`;
    }
    w.row.classList.toggle("idle", !fresh);
  }
  requestAnimationFrame(animateSpectrum);
}
requestAnimationFrame(animateSpectrum);

// --- output EQ ---------------------------------------------------------------
// One vertical-slider bank per connected node, mirroring the node's persisted
// eqDb from the snapshot. A change pushes the whole 5-gain array to the
// conductor (on release, like nudge/volume), which relays it to the node and
// saves it. We never overwrite a slider that's being dragged (activeElement
// guard), so snapshots don't fight the user mid-tweak.
const EQ_HZ = ["80", "250", "1k", "4k", "12k"];
const eqRows = new Map();  // nodeId -> {row, name, sliders:[input], vals:[el]}
const fmtDb = (v) => { const n = Math.round(+v) || 0; return n > 0 ? "+" + n : "" + n; };

function renderEqWidgets() {
  const panel = $("eqPanel");
  if (!panel) return;
  const nodes = snap.nodes.filter((n) => n.connected);
  const want = new Set(nodes.map((n) => n.id));

  for (const [id, w] of eqRows) {
    if (!want.has(id)) { w.row.remove(); eqRows.delete(id); }
  }
  for (const n of nodes) {
    let w = eqRows.get(n.id) || buildEqRow(panel, n.id);
    w.name.textContent = n.name;
    const gains = Array.isArray(n.eqDb) ? n.eqDb : [0, 0, 0, 0, 0];
    w.sliders.forEach((s, i) => {
      if (document.activeElement !== s) {   // don't clobber a live drag
        s.value = gains[i] ?? 0;
        w.vals[i].textContent = fmtDb(gains[i]);
      }
    });
  }
  $("eqEmpty").style.display = nodes.length ? "none" : "block";
}

function buildEqRow(panel, id) {
  const row = document.createElement("div");
  row.className = "eqRow";
  const name = document.createElement("div");
  name.className = "eqName";
  const bands = document.createElement("div");
  bands.className = "eqBands";
  const sliders = [], vals = [];
  for (let i = 0; i < EQ_HZ.length; i++) {
    const band = document.createElement("div");
    band.className = "eqBand";
    const val = document.createElement("div");
    val.className = "eqVal"; val.textContent = "0";
    const s = document.createElement("input");
    s.type = "range"; s.min = "-12"; s.max = "12"; s.step = "1"; s.value = "0";
    s.oninput = () => { val.textContent = fmtDb(s.value); };        // live label
    s.onchange = () => { val.textContent = fmtDb(s.value); pushEq(id); }; // commit
    const hz = document.createElement("div");
    hz.className = "eqHz"; hz.textContent = EQ_HZ[i];
    band.append(val, s, hz);
    bands.appendChild(band);
    sliders.push(s); vals.push(val);
  }
  const flat = document.createElement("button");
  flat.className = "eqFlat"; flat.textContent = "flat";
  flat.onclick = () => {
    sliders.forEach((s, i) => { s.value = "0"; vals[i].textContent = "0"; });
    pushEq(id);
  };
  row.append(name, bands, flat);
  panel.appendChild(row);
  const w = { row, name, sliders, vals };
  eqRows.set(id, w);
  return w;
}

function pushEq(id) {
  const w = eqRows.get(id);
  if (w) cmd({ cmd: "eq", nodeId: id, eqDb: w.sliders.map((s) => parseFloat(s.value) || 0) });
}

// --- calibration mic level ---------------------------------------------------
// One live input meter per node that opted in as a calibration mic. Phase 1
// proof that the mic hears the room; the auto-nudge DSP comes next. Display-only.
const micLevels = new Map();  // nodeId -> {rms, at}
const calRows = new Map();    // nodeId -> {row, name, fill, db}

function renderCalibration() {
  const panel = $("calPanel");
  if (!panel) return;
  const mics = snap.nodes.filter((n) => n.connected && n.mic);
  const want = new Set(mics.map((n) => n.id));

  for (const [id, w] of calRows) {
    if (!want.has(id)) { w.row.remove(); calRows.delete(id); micLevels.delete(id); }
  }
  for (const n of mics) {
    let w = calRows.get(n.id);
    if (!w) {
      const row = document.createElement("div"); row.className = "calRow";
      const name = document.createElement("div"); name.className = "calName";
      const meter = document.createElement("div"); meter.className = "calMeter";
      const fill = document.createElement("div"); fill.className = "calFill";
      meter.appendChild(fill);
      const db = document.createElement("div"); db.className = "calDb"; db.textContent = "—";
      row.append(name, meter, db);
      panel.appendChild(row);
      w = { row, name, fill, db }; calRows.set(n.id, w);
    }
    w.name.textContent = "🎙️ " + n.name;
    const lv = micLevels.get(n.id);
    const fresh = lv && (performance.now() - lv.at) < 1000;
    const dbfs = (fresh && lv.rms > 1e-6) ? 20 * Math.log10(lv.rms) : -120;
    const pct = Math.max(0, Math.min(100, (dbfs + 60) / 60 * 100));  // map -60..0 dB
    w.fill.style.width = pct + "%";
    w.db.textContent = !fresh ? "—" : (dbfs > -99 ? dbfs.toFixed(0) + " dB" : "quiet");
  }
  $("calEmpty").style.display = mics.length ? "none" : "block";
}

// --- helpers (also used from inline handlers) --------------------------------------
function posCellFor(n) {
  const pos = nodePosMs(n);
  const dur = snap.playing && snap.playing.durationMs;
  if (pos === null || !dur) return `<td class="pill">—</td>`;
  const pct = Math.min(100, 100 * pos / dur);
  return `<td><div class="miniBar"><div class="miniFill" style="width:${pct}%"></div></div>` +
         `<span class="miniTime">${mmss(pos)}</span></td>`;
}

window.playTrack = (trackId) => cmd({ cmd: "play", trackId });
window.setNudge = (nodeId, v) => cmd({ cmd: "nudge", nodeId, nudgeMs: parseFloat(v) || 0 });
window.setVol = (nodeId, v) => cmd({ cmd: "volume", nodeId, volume: parseInt(v, 10) });

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

let toastTimer = null;
function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.className = "show";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = ""; }, 3500);
}
