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
    else if (msg.type === "loadProgress") setNodePill(msg.nodeId, msg.pct, msg.done, msg.decoding);
    else if (msg.type === "measureToF") showMeasure(msg);
    else if (msg.type === "calibrateProgress") showCalProgress(msg);
    else if (msg.type === "calibrateResult") showCalResult(msg);
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
             (snap.paused && snap.paused.trackId);
  // Resume/restart what's loaded; from a standstill send a bare play and let the
  // conductor start the queue head (consuming it) or the library top.
  cmd(id ? { cmd: "play", trackId: id } : { cmd: "play" });
};
$("btnPause").onclick = () => cmd({ cmd: "pause" });
$("btnResume").onclick = () => cmd({ cmd: "resume" });
$("btnRestart").onclick = () => cmd({ cmd: "seek", positionMs: 0 });
$("btnNext").onclick = () => cmd({ cmd: "next" });
$("btnStop").onclick = () => cmd({ cmd: "stop" });
$("btnBeep").onclick = () => cmd({ cmd: "beep" });
$("btnResync").onclick = () => cmd({ cmd: "resync" });
$("btnRescan").onclick = () => cmd({ cmd: "rescan" });
$("btnQueueClear").onclick = () => cmd({ cmd: "queueClear" });
$("measureBtn").onclick = () => cmd({ cmd: "measure" });
$("calibrateBtn").onclick = () => cmd({ cmd: "calibrate" });

// Click the position bar to seek: the whole fleet re-starts together at that
// spot (same coordinated-start path as play/resume). Optimistically snap the fill
// so it feels instant; the next snapshot confirms the real position.
$("posHit").addEventListener("click", (e) => {
  const cur = snap && (snap.playing || snap.paused);
  if (!cur || !cur.durationMs) return;
  const r = $("posBar").getBoundingClientRect();
  const frac = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
  cmd({ cmd: "seek", positionMs: frac * cur.durationMs });
  $("posFill").style.width = (frac * 100).toFixed(1) + "%";
});

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
  renderQueue();
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

// The offset's worst-case error, as a cell. Half the widest round trip admitted
// to the fit — a certificate every sample carries (asymmetry <= rtt), not a
// guess at the noise. The tooltip shows the floor (best_rtt/2) beside it: the
// gap between the two is exactly what the RTT filter's tolerance costs this
// node, which is the number the filter_best re-tune wants to be judged on.
function trustCellFor(n) {
  if (n.trustMs === null || n.trustMs === undefined) {
    return `<td class="num" title="no surviving samples yet">—</td>`;
  }
  const cls = n.trustMs < 1 ? "trustGood" : (n.trustMs < 3 ? "trustFair" : "trustPoor");
  const tip = `offset good to ±${n.trustMs.toFixed(2)} ms (worst case)
`
    + `bound by the widest round trip in the fit: ${fmt(n.worstRttMs, 1)} ms
`
    + `floor if only the best sample counted: ±${fmt(n.floorMs, 2)} ms
`
    + `${n.nUsed} of ${n.nSamples} samples survived the filter`;
  return `<td class="num ${cls}" title="${esc(tip)}">±${n.trustMs.toFixed(2)}</td>`;
}

// Drift, with a note on whether it is yet distinguishable from measurement
// error. Dim means "not a measured crystal" — either no slope has been fitted
// (a prior, or a flat 0.0 on a young node) or the fit sits inside its own
// bound, which is what a device being carried across the room looks like.
// Only a bright number is one the node will inherit next session.
function skewCellFor(n) {
  if (n.skewPpm === null || n.skewPpm === undefined) return `<td class="num">—</td>`;
  const tip = n.skewBoundPpm === null || n.skewBoundPpm === undefined
    ? "no slope fitted yet — this is a prior or a flat zero, not a measurement"
    : `fitted drift ${n.skewPpm.toFixed(1)} ppm, worst case ±${n.skewBoundPpm.toFixed(1)} ppm\n`
      + (n.skewCredible
          ? "clears its own error bound — will be carried to the next session"
          : "inside its own error bound — measurement error alone could fake this,\nso it is used live but not banked");
  // The remembered value is a *different* number from the live fit, and it is
  // the one that outlives the session — so it gets its own affordance, shown
  // only when there is actually something to forget.
  const rem = n.rememberedSkewPpm;
  const forget = (rem === null || rem === undefined) ? "" :
    ` <button class="forget" title="${esc(
        `remembered: ${rem.toFixed(1)} ppm — carried into this node's next session.`
        + "\nForget it. If this session is measuring a believable drift of its own,"
        + "\nthat replaces it immediately; otherwise the node starts clean next time.")}"
        onclick="forgetSkew('${n.id}')">⌫</button>`;
  return `<td class="num${n.skewCredible ? "" : " skewSoft"}" title="${esc(tip)}">${fmt(n.skewPpm, 1)}${forget}</td>`;
}

// `err ms` is the servo's own opinion, and it has two failure modes that look
// identical in a single number: a *frozen* reading (the ack stream died and this
// is a memory) and a *settled* one. The conductor now stamps both, so this cell
// can refuse to be read the wrong way.
//
// The tooltip carries the interpretation, because the number is not a fault
// report. The servo is proportional-only, so a settled error is 15 s x whatever
// rate playback is slipping at — and the node's own rate trim measures that
// rate directly. Subtract the drift the conductor already knows about and what
// remains is the node's audio clock against its own CPU clock.
// A node is only running the code you think it is if it says so. The conductor
// stamps the hash of the player.js it serves into each page it serves, and the
// node echoes it back — so this compares what a node LOADED against what is
// being served now. A WebSocket reconnect does not change the answer, because
// the page did not reload, which is exactly the case that used to look fresh.
function staleBuildFor(n) {
  const serving = snap.servingBuild;
  if (!serving || n.playerBuild === serving) return "";
  const tip = n.playerBuild
    ? [`running player build ${n.playerBuild}, but ${serving} is being served.`,
       "This node has not reloaded since player.js changed - it is running",
       "older code than everything else in the room."].join("\n")
    : ["this page is too old to say which build it is running.",
       "Reload it: anything before the build stamp existed is stale by definition."].join("\n");
  return ` <span class="staleBuild" title="${esc(tip)}">⟳</span>`;
}

function errCellFor(n, err) {
  if (!n.playing) return `<td class="num">${err}</td>`;
  if (n.errStale) {
    const age = (n.errAgeS === null || n.errAgeS === undefined) ? "?" : n.errAgeS.toFixed(0);
    const tip = [
      `no steerAck for ${age} s - this node claims to be playing but has`,
      "stopped answering the servo. The number beside it is the last thing",
      "it said, not what it is doing now. Read nothing into its steadiness.",
    ].join("\n");
    return `<td class="num errStale" title="${esc(tip)}">${err}?</td>`;
  }
  const lines = [];
  if (n.ratePpm !== null && n.ratePpm !== undefined) {
    lines.push(`servo rate: ${n.ratePpm >= 0 ? "+" : ""}${n.ratePpm.toFixed(0)} ppm`);
  }
  if (n.audioClockPpm !== null && n.audioClockPpm !== undefined) {
    lines.push(`settled ${n.runS.toFixed(0)} s. The servo has no integral term, so this`);
    lines.push(`error is it holding off ${(-n.ratePpm).toFixed(0)} ppm of slip - of which`);
    lines.push(`${n.skewPpm.toFixed(1)} ppm is clock drift the conductor already steers for.`);
    lines.push(`Unexplained: ${n.audioClockPpm.toFixed(0)} ppm. That is this node's audio`);
    lines.push(`clock against its own performance.now() - a pair nothing else here measures.`);
  } else if (n.runS !== null && n.runS !== undefined) {
    lines.push(`source started ${n.runS.toFixed(0)} s ago. The rate trim resets to 1.0 on`);
    lines.push(`every start and needs ~45 s to settle, so there is no attribution yet.`);
  }
  const odd = n.audioClockPpm !== null && n.audioClockPpm !== undefined
              && Math.abs(n.audioClockPpm) > 150;
  return `<td class="num${odd ? " errOdd" : ""}"${
    lines.length ? ` title="${esc(lines.join("\n"))}"` : ""}>${err}</td>`;
}

function renderNodeTable() {
  const rows = snap.nodes.map((n) => {
    // Decode outranks download: loadPct sits at 100 for the whole decode, so
    // testing it first would report "downloading" through the longer half of
    // the wait. Elapsed seconds, because decodeAudioData has no percentage to
    // give and an invented one would be worse than an honest counter.
    const dec = n.decodingS !== null && n.decodingS !== undefined;
    const dl = !dec && n.loadPct !== null && n.loadPct !== undefined;
    const state = n.playing ? "♪ playing"
                : dec ? `⚙ decoding ${n.decodingS.toFixed(1)}s`
                : dl ? `⬇ ${n.loadPct}%`
                : (n.loadedCurrent ? "ready" : (n.connected ? "idle" : "gone"));
    const err = n.playing ? fmt(n.syncErrMs, 1) : "—";
    return `<tr data-node="${esc(n.id)}">
      <td><span class="dot ${n.connected ? "ok" : ""}"></span>${esc(n.name)}${staleBuildFor(n)}</td>
      <td class="num">${fmt(n.offsetMs, 2)}</td>
      ${trustCellFor(n)}
      <td class="num">${fmt(n.bestRttMs, 1)}</td>
      ${skewCellFor(n)}
      ${errCellFor(n, err)}
      ${posCellFor(n)}
      <td class="num hideSm">${n.nUsed}/${n.nSamples}${
        n.pingBoost > 1.05
          ? ` <span class="boost" title="pinged ${n.pingBoost.toFixed(1)}x harder — this node discards most of its exchanges">${n.pingBoost.toFixed(1)}×</span>`
          : ""
      }</td>
      <td class="num"><input class="nudge" type="number" step="5" value="${n.nudgeMs}"
            onchange="setNudge('${n.id}', this.value); this.blur()"></td>
      <td><input class="volSlider" type="range" min="0" max="100" value="${n.volume ?? 80}"
            onchange="setVol('${n.id}', this.value); this.blur()"></td>
      <td class="pill nodeState">${state}</td>
    </tr>`;
  });
  $("nodeRows").innerHTML = rows.join("");
  $("nodesEmpty").style.display = snap.nodes.length ? "none" : "block";
}

function renderPlaylist() {

  // playlist
  const nowId = snap.playing ? snap.playing.trackId : null;
  const queued = new Map();            // trackId -> how many times it's queued
  for (const q of snap.queue || []) queued.set(q.id, (queued.get(q.id) || 0) + 1);
  const nextUp = snap.nextUp || null;

  $("trackRows").innerHTML = snap.tracks.map((t) => {
    const n = queued.get(t.id) || 0;
    const mark = t.id === nowId ? "♪ " : "";
    // "next up" only earns the label when it isn't the one already playing.
    const tag = (t.id === nextUp && t.id !== nowId)
      ? ` <span class="nextUp">next up</span>` : "";
    const qTag = n ? ` <span class="pill">· queued${n > 1 ? "×" + n : ""}</span>` : "";
    return `<tr class="trackRow">
      <td>${mark}${esc(t.title)}${tag}${qTag}</td>
      <td class="num">${t.durationMs ? mmss(t.durationMs) : ""}</td>
      <td class="num"><button class="qBtn" title="add to the end of the queue"
            onclick="queueTrack('${t.id}')">＋queue</button></td>
      <td class="num"><button class="playBtn" onclick="playTrack('${t.id}')">play</button></td>
    </tr>`;
  }).join("");
  $("tracksEmpty").textContent = snap.tracks.length ? "" :
    `no audio files found in ${snap.musicDir} — drop some in and hit ↻ rescan`;
  $("tracksEmpty").style.display = snap.tracks.length ? "none" : "block";
}

// The queue: explicit play order layered over the folder scan. Edits address
// entries by *index* (duplicates are allowed, so position is the only stable
// handle) and every mutation is a plain command — the conductor re-broadcasts
// the snapshot and this rebuilds. No optimistic local state to drift.
function renderQueue() {
  const q = snap.queue || [];
  $("queueRows").innerHTML = q.map((t, i) => `
    <tr class="${i === 0 ? "qNext" : ""}">
      <td class="qPos num">${i + 1}</td>
      <td class="qTitle">${i === 0 ? "▸ " : ""}${esc(t.title)}</td>
      <td class="num">${t.durationMs ? mmss(t.durationMs) : ""}</td>
      <td class="num">
        <button class="qBtn" title="move up" ${i === 0 ? "disabled" : ""}
          onclick="moveQueue(${i}, -1)">↑</button>
        <button class="qBtn" title="move down" ${i === q.length - 1 ? "disabled" : ""}
          onclick="moveQueue(${i}, 1)">↓</button>
        <button class="qBtn" title="remove from queue" onclick="unqueue(${i})">✕</button>
      </td>
    </tr>`).join("");
  $("queueEmpty").style.display = q.length ? "none" : "block";
  $("btnQueueClear").style.display = q.length ? "" : "none";
}

function renderNowLine() {
  if (!snap) return;
  if (snap.arming) {
    // Interpolated between snapshots like the position readout, so the number
    // moves smoothly instead of stepping once a second.
    const elapsed = (performance.now() - snapAtPerf) / 1000;
    const left = snap.arming.secondsLeft - elapsed;
    $("nowLine").innerHTML = left > 0
      ? `arming <b>${esc(snap.arming.title)}</b> · starting in ${left.toFixed(1)}s ` +
        `<span class="pill">loading + calibrating every node</span>`
      : `arming <b>${esc(snap.arming.title)}</b> · <span class="pill">waiting on a slow decode</span>`;
    $("posFill").style.width = "0";
  } else if (snap.playing) {
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

function showMeasure(m) {
  const peak = (m.peak != null) ? ` · peak ${(+m.peak).toFixed(2)}` : "";
  const snr = (m.snr != null) ? ` · snr ${(+m.snr).toFixed(1)}` : "";
  $("measureOut").textContent =
    `${m.speakerName} → mic: ToF ${(+m.tofMs).toFixed(2)} ms${peak}${snr}`
    + ` · in ${levelText(m.rmsDb, m.clipPct)}`;
}

// The capture level, and what it means. `peak` is a normalized correlation, so
// it says nothing about whether the mic was live — this is the cell that does.
function levelText(rmsDb, clipPct) {
  if (rmsDb === null || rmsDb === undefined) return "—";
  const db = `${(+rmsDb).toFixed(0)} dBFS`;
  if (rmsDb < -60) return `${db} (silent)`;
  if (clipPct != null && clipPct >= 1) return `${db} (clipping)`;
  return db;
}

function levelClass(rmsDb, clipPct) {
  if (rmsDb === null || rmsDb === undefined) return "";
  if (rmsDb < -60) return "closureBad";
  if (clipPct != null && clipPct >= 1) return "closureWarn";
  return "closureOk";
}

// --- calibration sweep (auto-nudge step 3) -----------------------------------
// The sweep takes ~15 s of chirps, so show which speaker is being measured as it
// goes — a silent progress-less wait is indistinguishable from a hang. The
// result table is READ-ONLY: it proposes nudges, it never applies them.
function showCalProgress(m) {
  $("calProgress").textContent = m.total
    ? `measuring ${m.speakerName} · rep ${m.rep}/${m.reps} · ${m.done}/${m.total}`
    : "";
}

function showCalResult(m) {
  const rows = m.rows || [];
  $("calRows").innerHTML = rows.map((r) => {
    const ok = r.proposedMs !== null && r.proposedMs !== undefined;
    const delta = ok ? r.proposedMs - (r.currentMs || 0) : 0;
    return `<tr>
      <td>${esc(r.name)}${r.note ? ` <span class="pill">· ${esc(r.note)}</span>` : ""}</td>
      <td class="num">${r.tofMs === null || r.tofMs === undefined ? "—" : (+r.tofMs).toFixed(2)}</td>
      <td class="num">${(+r.spreadMs).toFixed(2)}</td>
      <td class="num">${r.peak == null ? "—" : `${(+r.peak).toFixed(2)}/${(+r.snr).toFixed(1)}`}</td>
      <td class="num ${levelClass(r.rmsDb, r.clipPct)}" title="capture level at the mic during this speaker's reps. A normalized correlation peak cannot tell a dead input from a missed chirp; this can.">${levelText(r.rmsDb, r.clipPct)}</td>
      <td class="num">${r.nGood}/${r.nTotal}</td>
      <td class="num">${(+r.currentMs).toFixed(0)}</td>
      <td class="num ${ok ? "closureOk" : "closureBad"}">${
        ok ? `${r.proposedMs.toFixed(1)}${Math.abs(delta) >= 0.5
              ? ` <span class="pill">(${delta > 0 ? "+" : ""}${delta.toFixed(0)})</span>` : ""}`
           : "—"}</td>
    </tr>`;
  }).join("");
  $("calNote").innerHTML =
    `measured against <b>${esc(m.micName)}</b> · median of ${m.reps} reps · ` +
    `aligned to the latest arrival, so every proposal delays a speaker rather than ` +
    `rushing one. <b>Nothing has been applied</b> — these are proposals only.`;
  $("calResult").style.display = rows.length ? "block" : "none";
  $("calProgress").textContent = "";
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

// Live load pill, faster than the 1 Hz snapshot: paint the node's status cell
// directly as progress relays in. The snapshot stays the source of truth for
// idle/ready/playing and overwrites this on its next tick — we only ever write the
// transfer states and never mask a node that's already playing (its background
// prefetch of the next track mustn't clobber "♪ playing"). `done` retires it the
// instant decode finishes, so it doesn't linger until the next snapshot.
//
// `decoding` flips the phase the moment the last byte lands. The seconds are left
// at 0.0 here on purpose: this path fires on relay, not on a clock, so it can't
// count. The 1 Hz snapshot takes over and ticks it — this only has to stop the
// cell claiming to still be downloading.
const PILL_LOAD_PREFIXES = ["⬇", "⚙"];
const isLoadPill = (s) => PILL_LOAD_PREFIXES.some((p) => s.startsWith(p));

function setNodePill(nodeId, pct, done, decoding) {
  for (const row of document.querySelectorAll("#nodeRows tr")) {
    if (row.dataset.node !== String(nodeId)) continue;
    const cell = row.querySelector(".nodeState");
    if (!cell || cell.textContent.trim() === "♪ playing") return;
    if (done) { if (isLoadPill(cell.textContent.trim())) cell.textContent = "ready"; }
    else if (decoding) cell.textContent = "⚙ decoding 0.0s";
    else cell.textContent = `⬇ ${pct}%`;
    return;
  }
}

window.playTrack = (trackId) => cmd({ cmd: "play", trackId });
window.queueTrack = (trackId) => cmd({ cmd: "queue", trackId });
window.unqueue = (index) => cmd({ cmd: "unqueue", index });
window.moveQueue = (index, delta) => cmd({ cmd: "queueMove", index, delta });
window.setNudge = (nodeId, v) => cmd({ cmd: "nudge", nodeId, nudgeMs: parseFloat(v) || 0 });
window.forgetSkew = (nodeId) => cmd({ cmd: "forgetSkew", nodeId });
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
