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

function render() {
  if (!snap) return;

  // Don't clobber an input mid-edit: skip table rebuilds while one is focused.
  const active = document.activeElement;
  const editing = active && active.closest && active.closest("#nodeRows");
  if (!editing) renderNodeTable();

  renderPlaylist();
  renderNowLine();
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

// --- helpers (also used from inline handlers) --------------------------------------
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
