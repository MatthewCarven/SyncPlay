"""The conductor: reference clock, playlist, per-node clock models, scheduler.

One aiohttp process that never plays audio itself. It serves the player and
control pages, streams track files, runs the ping cadence against every node
(sparse during playback, heavy bursts in the idle gaps between songs), and
broadcasts drift-projected start times.

Wire protocol: JSON over WebSocket, all times in **milliseconds** (native to
the browser's performance.now()). Internally everything is seconds on
time.perf_counter() — the conversion happens only at this boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from aiohttp import WSMsgType, web

from .timesync import ClockModel, PingSample

log = logging.getLogger("syncplay")

AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".oga", ".opus", ".webm"}
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
STATE_FILE = Path("syncplay_state.json")  # per-node nudge persistence (cwd)

# --- scheduling constants (seconds) ----------------------------------------
PLAY_LEAD = 1.8           # how far in the future a play command is dated
CATCHUP_LEAD = 0.5        # lead for a late-joining node
PAUSE_LEAD = 0.45         # lead for a synced pause
PRE_END_BURST = 2.5       # heavy resync burst starts this long before track end
GAP_AFTER_TRACK = 0.15    # silence after a track before the next is scheduled
LOAD_GATE_TIMEOUT = 12.0  # warm start: fleet is going, buffer already decoded
LOAD_GATE_COLD = 20.0     # cold start: a 70 MB decode from nothing needs room
ARM_SECONDS = 6.0         # visible countdown floor before a cold start
PENDING_PING_TTL = 5.0
MAX_QUEUE = 200           # sanity cap on the explicit play queue
MAX_PERSISTED_SKEW = 500e-6  # 500 ppm; past this it's a bad fit, not a crystal

# --- acoustic calibration (auto-nudge) -------------------------------------
MEASURE_LEAD = 0.4        # how far ahead a chirp emit is scheduled
MEASURE_WINDOW_MS = 900   # mic capture window per probe
MEASURE_TIMEOUT = 3.0     # give up on a probe that never reports back
CAL_REPS = 3              # chirps per speaker; medianed to survive one bad peak
CAL_GAP = 0.35            # silence between probes, so reflections die down
CAL_MIN_PEAK = 0.15       # correlation peak below this = didn't really hear it
CAL_MIN_GOOD = 2          # reps that must clear the gate before we'll propose
MAX_NUDGE_MS = 500.0      # matches the clamp on the manual nudge input

# --- ping cadence: (count, spacing_s) --------------------------------------
BURST_JOIN = (16, 0.06)     # right after a node connects: converge fast
BURST_GAP = (10, 0.07)      # song gaps, idle, and manual resync
BURST_PLAYING = (3, 0.10)   # sparse keepalive during playback
INTERVAL_PLAYING = 5.0
INTERVAL_IDLE = 2.0
INTERVAL_BURSTING = 1.0


def now() -> float:
    """The conductor's reference clock (seconds). Everyone syncs to this."""
    return time.perf_counter()


def _clean_eq(vals) -> List[float]:
    """Coerce arbitrary input into exactly 5 EQ gains (dB), each clamped to ±12."""
    out = [0.0] * 5
    if isinstance(vals, (list, tuple)):
        for i in range(min(5, len(vals))):
            try:
                out[i] = max(-12.0, min(12.0, float(vals[i])))
            except (ValueError, TypeError):
                pass
    return out


def _clean_skew(v) -> Optional[float]:
    """Validate a persisted skew (s/s). None if it isn't a plausible crystal.

    Anything beyond ±MAX_PERSISTED_SKEW is a bad fit that got written to disk,
    not a real oscillator; refusing to load it means a corrupt state file
    degrades to today's cold-start behaviour instead of poisoning a join."""
    try:
        s = float(v)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(s) or abs(s) > MAX_PERSISTED_SKEW:
        return None
    return s


def plan_start(playing_now: bool, loaded: List[bool]) -> Tuple[bool, float]:
    """Decide whether a start is *cold*, and how long to hold the load gate.

    Returns ``(arm, load_gate_timeout)``.

    Cold means two things at once: nothing is sounding right now, **and** at
    least one connected node still has to fetch and decode the track. That
    conjunction is the whole point. It is the only moment the room can actually
    perceive a wait, so it is the only moment a countdown earns its keep — and
    it doubles as the window the clock models want, because a 70 MB decode buys
    several seconds of ping samples spread across many Wi-Fi power-save cycles
    instead of sixteen crammed into one.

    Everything else is warm: resume, seek, next, auto-advance. There the fleet
    is already holding the buffer, the gate clears in milliseconds, and a
    countdown would be delay we invented for ourselves.

    With nobody connected there is nothing to arm for and nothing to wait on;
    the caller's own "no node managed to load this" path handles it.
    """
    if not loaded:
        return False, LOAD_GATE_TIMEOUT
    cold = not playing_now and not all(loaded)
    return cold, (LOAD_GATE_COLD if cold else LOAD_GATE_TIMEOUT)


def plan_nudges(rows: Dict[str, List[dict]]) -> List[dict]:
    """Turn repeated ToF probes into a proposed nudge per speaker.

    `rows` maps speaker id -> that speaker's probe results. Pure function: no
    clock, no sockets, no node objects — so the arithmetic that decides how your
    system sounds can be tested exhaustively without a microphone in the loop.

    Two judgement calls live here:

    * **Median, not mean.** A single mis-picked correlation peak (a reflection,
      a door closing) is a wild outlier, and a mean would smear it across the
      answer. The median just ignores it, which is the whole reason for reps.
    * **Align to the latest arrival.** target = max(median ToF), so every nudge
      comes out >= 0. We can only ever *delay* a speaker; there's no making a
      distant one arrive earlier, so the furthest speaker sets the pace.

    A speaker without CAL_MIN_GOOD clean reps gets proposedMs None and is left
    out of the target entirely — it must not drag the whole room to match a
    number we don't believe.
    """
    summary: List[dict] = []
    for spk_id, probes in rows.items():
        good = [p for p in probes if (p.get("peak") or 0.0) >= CAL_MIN_PEAK]
        tofs = sorted(p["tofMs"] for p in good)
        row = {
            "id": spk_id,
            "nGood": len(good),
            "nTotal": len(probes),
            "tofMs": statistics.median(tofs) if tofs else None,
            # Spread across reps is the honest confidence signal: tight means
            # the room and the correlator agree, wide means don't trust it.
            "spreadMs": (tofs[-1] - tofs[0]) if len(tofs) > 1 else 0.0,
            "peak": max((p.get("peak") or 0.0) for p in good) if good else None,
            "snr": max((p.get("snr") or 0.0) for p in good) if good else None,
            "proposedMs": None,
            "note": "",
        }
        if len(good) < CAL_MIN_GOOD:
            row["note"] = ("no usable capture" if not good
                           else f"only {len(good)} clean rep(s)")
        summary.append(row)

    usable = [r for r in summary if r["tofMs"] is not None and not r["note"]]
    if not usable:
        return summary
    target = max(r["tofMs"] for r in usable)
    for row in usable:
        nudge = target - row["tofMs"]
        if nudge > MAX_NUDGE_MS:
            # >500 ms apart is ~170 m of air. Far likelier a bad peak than a
            # real room, so say so rather than quietly clamping to a fiction.
            row["note"] = "implausible (>%.0f ms) - re-run" % MAX_NUDGE_MS
        else:
            row["proposedMs"] = round(nudge, 1)
    if len(usable) == 1:
        usable[0]["note"] = "only speaker measured - nothing to align against"
    return summary


@dataclass
class Track:
    id: str
    path: Path
    title: str
    duration_ms: Optional[float] = None  # reported by the first node to decode it


@dataclass
class Playback:
    track: Track
    t_start: float  # conductor time the (possibly seeked) playback began
    seek_ms: float

    def position_ms(self, t: Optional[float] = None) -> float:
        return self.seek_ms + ((t if t is not None else now()) - self.t_start) * 1000.0


class Node:
    """One player device. Identity survives reconnects; the clock model doesn't
    (performance.now() restarts with every page load), and neither do decoded
    buffers, so both reset on a fresh socket.

    Its *skew* is the exception. Crystal drift belongs to the hardware, not the
    page-life, so it is carried across sessions (and across conductor restarts,
    via syncplay_state.json) to seed the next model. Without it every returning
    node spends its first ~30 s asserting zero drift — which for a 20 ppm tablet
    is the difference between joining clean and joining a few ms out."""

    def __init__(self, client_id: str, name: str):
        self.client_id = client_id
        self.name = name
        self.prior_skew: Optional[float] = None  # last *fitted* skew, seconds/second
        self.nudge_ms = 0.0
        self.volume: Optional[int] = None  # None = never set from control
        self.eq_db: List[float] = [0.0] * 5  # per-node output EQ, pushed from control
        self.ws: Optional[web.WebSocketResponse] = None
        self.model = ClockModel()
        self.connected = False
        self.ua = ""
        self.mesh = False  # page supports client<->client ping channels
        self.mic = False  # opted in as a calibration microphone this page-life
        self.ping_seq = 0
        self.pending: Dict[int, float] = {}  # ping id -> t0
        self.loaded: Set[str] = set()  # track ids decoded and ready this page-life
        self.load_track: Optional[str] = None  # track id currently downloading (pre-decode)
        self.load_pct: Optional[int] = None  # its byte-download %, for the control pill
        self.playing_track: Optional[str] = None  # as reported by the node
        self.sync_err_ms: Optional[float] = None  # last steer error it reported
        self.play_rate: Optional[float] = None  # its current servo playback rate
        self.burst_until = 0.0
        self.kick = asyncio.Event()
        self.ping_task: Optional[asyncio.Task] = None

    def begin_session(self, ws: web.WebSocketResponse, ua: str) -> None:
        self.ws = ws
        self.ua = ua
        self.connected = True
        self.model = ClockModel(prior_skew=self.prior_skew)
        self.pending.clear()
        self.loaded.clear()
        self.load_track = None
        self.load_pct = None
        self.playing_track = None
        self.sync_err_ms = None
        self.play_rate = None
        self.burst_until = 0.0
        self.mic = False  # mic mode is per page-life; the client re-announces it

    def remember_skew(self) -> bool:
        """Carry this session's measured drift forward. Only a real fit counts:
        echoing an inherited prior straight back would let one bad reading
        outlive every session after it. Returns whether anything was learned."""
        est = self.model.estimate()
        if est is None or not est.skew_fitted:
            return False
        self.prior_skew = est.skew
        return True

    def end_session(self) -> None:
        self.remember_skew()
        self.connected = False
        self.ws = None
        self.pending.clear()
        if self.ping_task:
            self.ping_task.cancel()
            self.ping_task = None

    async def send(self, payload: dict) -> None:
        if self.connected and self.ws is not None and not self.ws.closed:
            try:
                await self.ws.send_str(json.dumps(payload))
            except (ConnectionError, RuntimeError):
                pass  # the receive loop will notice and clean up

    def stats(self, current_track: Optional[str]) -> dict:
        est = self.model.estimate()
        d = {
            "id": self.client_id,
            "name": self.name,
            "connected": self.connected,
            "ua": self.ua,
            "nudgeMs": self.nudge_ms,
            "volume": self.volume,
            "eqDb": self.eq_db,
            "mic": self.mic,
            "syncErrMs": self.sync_err_ms,
            "ratePpm": (self.play_rate - 1.0) * 1e6 if self.play_rate else None,
            "playing": self.playing_track,
            "loadedCurrent": bool(current_track and current_track in self.loaded),
            # Non-null only while a not-yet-decoded track is streaming in — the
            # control page shows it in the node's status pill. Cleared on decode.
            "loadPct": (
                self.load_pct
                if (self.load_track is not None and self.load_track not in self.loaded)
                else None
            ),
            "offsetMs": None,
            "lastRttMs": None,
            "bestRttMs": None,
            "skewPpm": None,
            "nUsed": 0,
            "nSamples": len(self.model),
            "spanS": 0.0,
        }
        if est is not None:
            d.update(
                offsetMs=est.offset_at(now()) * 1000.0,
                lastRttMs=est.last_rtt * 1000.0,
                bestRttMs=est.best_rtt * 1000.0,
                skewPpm=est.skew_ppm,
                nUsed=est.n_used,
                spanS=est.span,
            )
        return d


class Conductor:
    def __init__(self, music_dir: Path):
        self.music_dir = music_dir
        self.tracks: List[Track] = []
        self.tracks_by_id: Dict[str, Track] = {}
        self.nodes: Dict[str, Node] = {}
        self.control_sockets: Set[web.WebSocketResponse] = set()
        self.playing: Optional[Playback] = None
        self.paused: Optional[Playback] = None  # t_start meaningless; seek_ms is the position
        # Explicit play order layered over the folder scan. Track ids, duplicates
        # allowed (queue the same song twice if you like), consumed from the head
        # by auto-advance/next; when it empties we fall back to folder order.
        # Deliberately not persisted: a queue is a mood, not a setting.
        self.queue: List[str] = []
        self.target_track: Optional[Track] = None  # track we're bringing up now (gates download-% relay)
        # Cold-start countdown: (track, conductor time the fleet should start).
        # Non-null only between the arm broadcast and the play that ends it.
        self.arming: Optional[Tuple[Track, float]] = None
        self._advance_task: Optional[asyncio.Task] = None
        self._transport_task: Optional[asyncio.Task] = None
        self._pulse_task: Optional[asyncio.Task] = None
        self._state: dict = self._load_state()
        # Mesh truth: per ordered pair (initiator, responder) of node ids, a
        # ClockModel whose timeline is the *initiator's* clock. Diagnostic only.
        self.mesh_pairs: Dict[Tuple[str, str], ClockModel] = {}
        self.mesh_seen: Dict[Tuple[str, str], float] = {}
        # Acoustic measurement: one in-flight ToF probe, and a flag so two
        # calibration sweeps can't interleave chirps and read each other's echoes.
        self._measure_seq = 0
        self._measure_pending: Optional[dict] = None
        self._calibrating = False
        self.scan_tracks()

    # --- library ------------------------------------------------------------

    def scan_tracks(self) -> int:
        old_durations = {t.id: t.duration_ms for t in self.tracks}
        tracks: List[Track] = []
        if self.music_dir.is_dir():
            for p in sorted(self.music_dir.rglob("*")):
                if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
                    rel = p.relative_to(self.music_dir).as_posix()
                    tid = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:10]
                    tracks.append(
                        Track(tid, p, p.stem, old_durations.get(tid))
                    )
        self.tracks = tracks
        self.tracks_by_id = {t.id: t for t in tracks}
        # A rescan can retire files out from under the queue; forget those ids.
        self.queue = [tid for tid in self.queue if tid in self.tracks_by_id]
        log.info("library: %d track(s) in %s", len(tracks), self.music_dir)
        return len(tracks)

    def _folder_next(self, current: Track) -> Optional[Track]:
        if not self.tracks:
            return None
        try:
            i = next(i for i, t in enumerate(self.tracks) if t.id == current.id)
        except StopIteration:
            return self.tracks[0]
        return self.tracks[(i + 1) % len(self.tracks)]

    def _peek_next(self, current: Track) -> Optional[Track]:
        """What plays after `current` — queue head if any, else folder order.

        Non-consuming: this is what the prefetch and the control page's "next up"
        marker ask, and asking must never change the answer.
        """
        for tid in self.queue:
            t = self.tracks_by_id.get(tid)
            if t is not None:
                return t
        return self._folder_next(current)

    def _take_next(self, current: Track) -> Optional[Track]:
        """Same choice as _peek_next, but consumes the queue entry it used.

        Only the two places that genuinely move on to the next song call this:
        auto-advance at end of track, and the control page's ⏭ next.
        """
        while self.queue:
            tid = self.queue.pop(0)
            t = self.tracks_by_id.get(tid)
            if t is not None:
                return t
        return self._folder_next(current)

    def _next_up_id(self) -> Optional[str]:
        """Track id that would play next right now. Display only, never consumes."""
        cur = self.playing or self.paused
        if cur is not None:
            nxt = self._peek_next(cur.track)
            return nxt.id if nxt else None
        for tid in self.queue:
            if tid in self.tracks_by_id:
                return tid
        return self.tracks[0].id if self.tracks else None

    def _take_next_idle(self) -> Optional[Track]:
        """What ▶/⏭ start from a standstill: queue head, else the library top."""
        while self.queue:
            t = self.tracks_by_id.get(self.queue.pop(0))
            if t is not None:
                return t
        return self.tracks[0] if self.tracks else None

    async def _prefetch_next(self) -> None:
        """Get whatever plays next decoding on every node, ahead of time."""
        p = self.playing
        if p is None:
            return
        nxt = self._peek_next(p.track)
        if nxt and nxt.id != p.track.id:
            await self._broadcast_players(self._preload_msg(nxt))

    # --- nudge persistence ----------------------------------------------------

    def _load_state(self) -> dict:
        try:
            data = json.loads(STATE_FILE.read_text("utf-8"))
            return {
                "nudges": {str(k): float(v) for k, v in data.get("nudges", {}).items()},
                "volumes": {str(k): int(v) for k, v in data.get("volumes", {}).items()},
                "eqs": {str(k): _clean_eq(v) for k, v in data.get("eqs", {}).items()},
                "skews": {
                    str(k): s
                    for k, v in data.get("skews", {}).items()
                    if (s := _clean_skew(v)) is not None
                },
            }
        except (OSError, ValueError, TypeError):
            return {"nudges": {}, "volumes": {}, "eqs": {}, "skews": {}}

    def _save_state(self) -> None:
        try:
            for n in self.nodes.values():
                # Refresh from live models too, not just at disconnect — a
                # conductor that is killed mid-session should still remember.
                if n.connected:
                    n.remember_skew()
                if n.prior_skew is not None:
                    self._state["skews"][n.client_id] = round(n.prior_skew, 12)
                if n.nudge_ms:
                    self._state["nudges"][n.client_id] = n.nudge_ms
                else:
                    self._state["nudges"].pop(n.client_id, None)
                if n.volume is not None:
                    self._state["volumes"][n.client_id] = n.volume
                if any(n.eq_db):
                    self._state["eqs"][n.client_id] = n.eq_db
                else:
                    self._state["eqs"].pop(n.client_id, None)
            STATE_FILE.write_text(json.dumps(self._state, indent=1), "utf-8")
        except OSError as e:
            log.warning("could not persist state: %s", e)

    # --- lifecycle ------------------------------------------------------------

    async def start(self, _app: web.Application) -> None:
        self._pulse_task = asyncio.create_task(self._pulse())

    async def shutdown(self, _app: web.Application) -> None:
        for t in (self._pulse_task, self._advance_task, self._transport_task):
            if t:
                t.cancel()
        for node in self.nodes.values():
            if node.ws is not None and not node.ws.closed:
                await node.ws.close()
        for ws in list(self.control_sockets):
            await ws.close()

    async def _pulse(self) -> None:
        """1 Hz heartbeat: state to control pages, sync stats to players."""
        tick = 0
        while True:
            await asyncio.sleep(1.0)
            tick += 1
            await self.push_state()
            if tick % 2 == 0:
                for node in self.nodes.values():
                    if node.connected:
                        est = node.model.estimate()
                        if est is not None:
                            await node.send(
                                {
                                    "type": "stats",
                                    "offsetMs": est.offset_at(now()) * 1000.0,
                                    "rttMs": est.best_rtt * 1000.0,
                                    "skewPpm": est.skew_ppm,
                                }
                            )
                await self._steer_all()
                # Drop mesh pairs that stopped reporting (channel died quietly).
                stale = [k for k, seen in self.mesh_seen.items() if now() - seen > 90.0]
                for k in stale:
                    self.mesh_pairs.pop(k, None)
                    self.mesh_seen.pop(k, None)

    # --- state pushes -----------------------------------------------------------

    def snapshot(self) -> dict:
        current = self.playing.track.id if self.playing else None
        return {
            "type": "snapshot",
            "serverNowMs": now() * 1000.0,
            "playing": (
                {
                    "trackId": self.playing.track.id,
                    "title": self.playing.track.title,
                    "tStartMs": self.playing.t_start * 1000.0,
                    "seekMs": self.playing.seek_ms,
                    "durationMs": self.playing.track.duration_ms,
                }
                if self.playing
                else None
            ),
            "paused": (
                {
                    "trackId": self.paused.track.id,
                    "title": self.paused.track.title,
                    "positionMs": self.paused.seek_ms,
                    "durationMs": self.paused.track.duration_ms,
                }
                if self.paused
                else None
            ),
            "tracks": [
                {"id": t.id, "title": t.title, "durationMs": t.duration_ms}
                for t in self.tracks
            ],
            "queue": [
                {"id": t.id, "title": t.title, "durationMs": t.duration_ms}
                for t in (self.tracks_by_id.get(q) for q in self.queue)
                if t is not None
            ],
            "arming": (
                {
                    "trackId": self.arming[0].id,
                    "title": self.arming[0].title,
                    "secondsLeft": max(0.0, self.arming[1] - now()),
                }
                if self.arming
                else None
            ),
            # What actually plays next, queue or not — drives the "next up" marker.
            "nextUp": self._next_up_id(),
            "musicDir": str(self.music_dir),
            "nodes": [n.stats(current) for n in self.nodes.values()],
            "mesh": self._mesh_snapshot(),
        }

    def _mesh_snapshot(self) -> List[dict]:
        """Triangle closure per measured pair: direct(A<->B) minus star-implied.

        The mismatch is the *measured* end-to-end sync error between two
        nodes — the number the whole star topology only lets us infer.
        """
        out: List[dict] = []
        t = now()
        for (a_id, b_id), pair_model in self.mesh_pairs.items():
            a, b = self.nodes.get(a_id), self.nodes.get(b_id)
            if a is None or b is None or not (a.connected and b.connected):
                continue
            pair_est = pair_model.estimate()
            est_a, est_b = a.model.estimate(), b.model.estimate()
            if pair_est is None or est_a is None or est_b is None:
                continue
            # The pair model's timeline is A's clock: evaluate it "now" by
            # mapping conductor-now onto A's clock through A's star model.
            t_a = est_a.to_node_time(t)
            direct_ms = pair_est.offset_at(t_a) * 1000.0  # B_clock - A_clock
            star_ms = (est_b.offset_at(t) - est_a.offset_at(t)) * 1000.0
            out.append(
                {
                    "a": a_id, "b": b_id,
                    "aName": a.name, "bName": b.name,
                    "directMs": direct_ms,
                    "closureMs": direct_ms - star_ms,
                    "rttMs": pair_est.best_rtt * 1000.0,
                    "n": pair_est.n_used,
                }
            )
        return out

    async def _broadcast_control(self, payload: dict) -> None:
        """Fan one JSON message out to every open control page, dropping dead ones."""
        if not self.control_sockets:
            return
        msg = json.dumps(payload)
        for ws in list(self.control_sockets):
            try:
                await ws.send_str(msg)
            except (ConnectionError, RuntimeError):
                self.control_sockets.discard(ws)

    async def push_state(self) -> None:
        if not self.control_sockets:
            return  # skip building the snapshot when nobody's watching
        await self._broadcast_control(self.snapshot())

    async def toast(self, text: str) -> None:
        log.info("toast: %s", text)
        await self._broadcast_control({"type": "toast", "text": text})

    # --- ping cadence -----------------------------------------------------------

    async def _ping_loop(self, node: Node) -> None:
        await self._burst(node, *BURST_JOIN)
        while node.connected:
            bursting = node.burst_until > now()
            if bursting or self.playing is None:
                count, spacing = BURST_GAP
            else:
                count, spacing = BURST_PLAYING
            await self._burst(node, count, spacing)
            interval = (
                INTERVAL_BURSTING
                if node.burst_until > now()
                else (INTERVAL_PLAYING if self.playing else INTERVAL_IDLE)
            )
            try:
                await asyncio.wait_for(node.kick.wait(), timeout=interval)
                node.kick.clear()
            except TimeoutError:
                pass

    async def _burst(self, node: Node, count: int, spacing: float) -> None:
        for _ in range(count):
            if not node.connected or node.ws is None or node.ws.closed:
                return
            node.ping_seq += 1
            pid = node.ping_seq
            t0 = now()
            node.pending[pid] = t0
            try:
                await node.ws.send_str(
                    json.dumps({"type": "ping", "id": pid, "t0": t0 * 1000.0})
                )
            except (ConnectionError, RuntimeError):
                return
            await asyncio.sleep(spacing)

    def request_burst(self, duration: float = 4.0) -> None:
        deadline = now() + duration
        for node in self.nodes.values():
            if node.connected:
                node.burst_until = deadline
                node.kick.set()

    # --- transport (play/pause/skip) ---------------------------------------------

    def dispatch(self, coro) -> None:
        """Transport commands supersede whatever transition is in flight."""
        if (
            self._transport_task
            and not self._transport_task.done()
            and self._transport_task is not asyncio.current_task()
        ):
            self._transport_task.cancel()
        self._transport_task = asyncio.create_task(coro)

    def _cancel_advance(self) -> None:
        if self._advance_task and self._advance_task is not asyncio.current_task():
            self._advance_task.cancel()
        self._advance_task = None

    async def _transport_play(self, track: Track, seek_ms: float = 0.0) -> None:
        self._cancel_advance()
        await self._disarm()  # a superseded start must not leave a countdown up
        self.target_track = track  # what the download-% pill is allowed to reflect
        was_playing = self.playing is not None
        self.paused = None
        if self.playing is not None:
            await self._broadcast_players({"type": "stop"})
            self.playing = None

        await self._broadcast_players(self._preload_msg(track))

        connected = [n for n in self.nodes.values() if n.connected]
        arm, gate = plan_start(was_playing, [track.id in n.loaded for n in connected])

        t_arm_end = now() + ARM_SECONDS
        if arm:
            # The countdown counts to the *start*, not to the end of arming —
            # a timer that hits zero and then sits silent through PLAY_LEAD is
            # a timer nobody trusts twice.
            self.arming = (track, t_arm_end + PLAY_LEAD)
            # This window is also the calibration window. Dense bursts spread
            # over several seconds sample several Wi-Fi power-save cycles; the
            # 16-ping join burst can land entirely inside one bad one.
            self.request_burst(ARM_SECONDS)
            for node in connected:
                await self._send_arm(node, track)
            await self.push_state()

        # Load gate: wait for every connected node to decode (or time out). When
        # armed, also hold until the countdown runs out, so the number on screen
        # is the truth even if the decode beat it.
        deadline = now() + gate
        while now() < deadline:
            connected = [n for n in self.nodes.values() if n.connected]
            if connected and all(track.id in n.loaded for n in connected):
                if not arm or now() >= t_arm_end:
                    break
            await asyncio.sleep(0.15)
        ready = [
            n for n in self.nodes.values() if n.connected and track.id in n.loaded
        ]
        if not ready:
            await self._disarm()
            await self.toast(f'No node managed to load "{track.title}" - not starting.')
            return

        self.arming = None  # the play command below is what retires the countdown
        t_start = now() + PLAY_LEAD
        self.playing = Playback(track=track, t_start=t_start, seek_ms=seek_ms)
        started = []
        for node in ready:
            if await self._send_play(node, self.playing):
                started.append(node.name)
        log.info(
            'play "%s" at T+%.1fs seek=%.0fms -> %s',
            track.title, PLAY_LEAD, seek_ms, ", ".join(started) or "nobody",
        )
        self._advance_task = asyncio.create_task(self._auto_advance(self.playing))
        # Get the *next* track decoding everywhere while this one plays.
        await self._prefetch_next()
        await self.push_state()

    async def _disarm(self) -> None:
        """Retire a countdown that will never reach its start."""
        if self.arming is None:
            return
        self.arming = None
        await self._broadcast_players({"type": "disarm"})

    async def _send_arm(self, node: Node, track: Track) -> None:
        """Countdown target for one node, on that node's own clock.

        Dated per node through its clock model for the same reason play is: so
        every screen in the room reaches zero at the same instant rather than at
        the same instant *plus each device's clock error*. A node too fresh to
        have an estimate gets the raw duration instead — a countdown that is a
        few ms out is still a countdown, and refusing to show one would punish
        exactly the node that has the longest wait ahead of it.
        """
        if self.arming is None:
            return
        est = node.model.estimate()
        msg = {
            "type": "arm",
            "trackId": track.id,
            "title": track.title,
            "secondsLeft": max(0.0, self.arming[1] - now()),
        }
        if est is not None:
            msg["startsAtNodeMs"] = est.to_node_time(self.arming[1]) * 1000.0
        await node.send(msg)

    async def _send_play(self, node: Node, p: Playback) -> bool:
        """Drift-projected start command for one node. False if it can't be timed."""
        est = node.model.estimate()
        if est is None:
            return False
        at_node_ms = est.to_node_time(p.t_start) * 1000.0
        await node.send(
            {
                "type": "play",
                "trackId": p.track.id,
                "title": p.track.title,
                "atNodeMs": at_node_ms,
                "seekMs": p.seek_ms,
            }
        )
        return True

    async def _catchup(self, node: Node, track_id: str) -> None:
        """Late-join: node just became ready while a track is playing."""
        for _ in range(25):  # allow up to ~5 s for the join burst to converge
            p = self.playing
            if p is None or p.track.id != track_id or not node.connected:
                return
            if node.model.estimate() is not None:
                t_join = now() + CATCHUP_LEAD
                catch = Playback(
                    track=p.track, t_start=t_join, seek_ms=p.position_ms(t_join)
                )
                if await self._send_play(node, catch):
                    log.info('catchup: %s into "%s" @ %.0fms',
                             node.name, p.track.title, catch.seek_ms)
                return
            await asyncio.sleep(0.2)

    async def _auto_advance(self, p: Playback) -> None:
        """Ride the current track to its end, burst in the gap, start the next."""
        while p.track.duration_ms is None:
            await asyncio.sleep(0.3)
            if self.playing is not p:
                return
        t_end = p.t_start + (p.track.duration_ms - p.seek_ms) / 1000.0
        delay = t_end - PRE_END_BURST - now()
        if delay > 0:
            await asyncio.sleep(delay)
        if self.playing is not p:
            return
        self.request_burst(PRE_END_BURST + 1.0)  # the idle-gap resync, as dreamed
        delay = t_end + GAP_AFTER_TRACK - now()
        if delay > 0:
            await asyncio.sleep(delay)
        if self.playing is not p:
            return
        nxt = self._take_next(p.track)
        if nxt is None:
            self.playing = None
            await self.push_state()
            return
        self.dispatch(self._transport_play(nxt))

    async def _transport_pause(self) -> None:
        p = self.playing
        if p is None:
            return
        self._cancel_advance()
        t_pause = now() + PAUSE_LEAD
        for node in self.nodes.values():
            if node.connected:
                est = node.model.estimate()
                at = est.to_node_time(t_pause) * 1000.0 if est else None
                await node.send({"type": "stop", "atNodeMs": at})
        self.playing = None
        # Clamp: pausing during the pre-start lead window would go negative.
        self.paused = Playback(
            track=p.track, t_start=t_pause, seek_ms=max(0.0, p.position_ms(t_pause))
        )
        log.info('paused "%s" at %.0fms', p.track.title, self.paused.seek_ms)
        await self.push_state()

    async def _transport_stop(self) -> None:
        self._cancel_advance()
        await self._disarm()
        self.playing = None
        self.paused = None
        self.target_track = None
        await self._broadcast_players({"type": "stop"})
        await self.push_state()

    async def _transport_beep(self) -> None:
        t_beep = now() + 1.2
        n = 0
        for node in self.nodes.values():
            if node.connected:
                est = node.model.estimate()
                if est is not None:
                    await node.send(
                        {"type": "beep", "atNodeMs": est.to_node_time(t_beep) * 1000.0}
                    )
                    n += 1
        await self.toast(f"Beep scheduled on {n} node(s) - listen for one single beep.")

    def _calibration_roles(self, speaker_id: str = "") -> Tuple[Optional[Node], List[Node]]:
        """(mic, speakers) for a calibration run, or (None, []) with a toast queued.

        Exactly one mic: two mics would mean two different reference positions,
        and the ToF differences we're about to take would be meaningless.
        """
        mics = [n for n in self.nodes.values() if n.connected and n.mic]
        if len(mics) != 1:
            return None, []
        mic = mics[0]
        speakers = [n for n in self.nodes.values() if n.connected and n is not mic]
        if speaker_id:
            want = self.nodes.get(speaker_id)
            if want is not None and want in speakers:
                speakers = [want]
        return mic, speakers

    async def _probe_once(self, spk: Node, mic: Node) -> Optional[dict]:
        """One chirp on `spk`, cross-correlated at `mic`. Returns its ToF or None.

        The ToF carries a constant per-mic offset (mic input latency + the
        ctx->perf mapping bias). That constant cancels the moment we difference
        two speakers against each other, which is the only way this number is
        ever used — never trust a single ToF as an absolute distance.
        """
        est_s, est_m = spk.model.estimate(), mic.model.estimate()
        if est_s is None or est_m is None:
            return None
        self._measure_seq += 1
        seq = self._measure_seq
        t_emit = now() + MEASURE_LEAD
        waiter: asyncio.Future = asyncio.get_running_loop().create_future()
        self._measure_pending = {
            "seq": seq, "spk": spk.client_id, "mic": mic.client_id,
            "t_emit": t_emit, "waiter": waiter,
        }
        await mic.send({"type": "measureArm", "seq": seq, "windowMs": MEASURE_WINDOW_MS})
        await spk.send({"type": "measureEmit", "seq": seq,
                        "atNodeMs": est_s.to_node_time(t_emit) * 1000.0})
        try:
            return await asyncio.wait_for(waiter, MEASURE_TIMEOUT)
        except asyncio.TimeoutError:
            return None
        finally:
            if self._measure_pending and self._measure_pending.get("seq") == seq:
                self._measure_pending = None

    async def _measure_one(self, speaker_id: str) -> None:
        """The single 📏 probe: one speaker, one chirp, ToF straight to control."""
        if self.playing is not None:
            await self.toast("Stop playback before calibrating.")
            return
        mic, speakers = self._calibration_roles(speaker_id)
        if mic is None:
            await self.toast("Calibration needs exactly one active mic node.")
            return
        if not speakers:
            await self.toast("No speaker node to measure.")
            return
        spk = speakers[0]
        await self.toast(f"Measuring {spk.name} -> {mic.name}...")
        res = await self._probe_once(spk, mic)
        if res is None:
            await self.toast(f"No usable capture from {spk.name}.")
            return
        await self._broadcast_control(
            {
                "type": "measureToF",
                "speakerId": spk.client_id,
                "speakerName": spk.name,
                "micId": mic.client_id,
                "tofMs": res["tofMs"],
                "peak": res.get("peak"),
                "snr": res.get("snr"),
            }
        )

    async def _measure_all(self) -> None:
        """Auto-nudge step 3: sweep every speaker, propose per-node nudges.

        One speaker at a time (the others silent), several reps each, then the
        medians get differenced into nudges. Proposals only — nothing is applied
        here; a human reads the table and decides. That split is deliberate: a
        bad correlation peak should cost you a re-run, not a silently wrecked
        calibration you then have to un-tune by ear.
        """
        if self._calibrating:
            await self.toast("A calibration sweep is already running.")
            return
        if self.playing is not None:
            await self.toast("Stop playback before calibrating.")
            return
        mic, speakers = self._calibration_roles()
        if mic is None:
            await self.toast("Calibration needs exactly one active mic node.")
            return
        if not speakers:
            await self.toast("No speaker nodes to measure.")
            return
        if any(n.model.estimate() is None for n in speakers + [mic]):
            await self.toast("Clocks not converged yet - give it a few seconds.")
            return

        self._calibrating = True
        rows: Dict[str, List[dict]] = {n.client_id: [] for n in speakers}
        try:
            total = len(speakers) * CAL_REPS
            done = 0
            for spk in speakers:
                for rep in range(CAL_REPS):
                    if self.playing is not None:       # someone hit play: bail out
                        await self.toast("Calibration aborted - playback started.")
                        return
                    if not (spk.connected and mic.connected):
                        break
                    done += 1
                    await self._broadcast_control({
                        "type": "calibrateProgress", "speakerName": spk.name,
                        "rep": rep + 1, "reps": CAL_REPS, "done": done, "total": total,
                    })
                    res = await self._probe_once(spk, mic)
                    if res is not None:
                        rows[spk.client_id].append(res)
                    await asyncio.sleep(CAL_GAP)       # let the room go quiet again

            plan = plan_nudges(rows)
            for row in plan:
                node = self.nodes.get(row["id"])
                row["name"] = node.name if node else row["id"]
                row["currentMs"] = node.nudge_ms if node else 0.0
            await self._broadcast_control({
                "type": "calibrateResult", "micName": mic.name, "reps": CAL_REPS,
                "rows": plan,
            })
            good = sum(1 for r in plan if r["proposedMs"] is not None)
            await self.toast(
                f"Calibration swept {len(speakers)} speaker(s): "
                f"{good} usable. Nothing applied yet - check the table."
            )
        finally:
            self._calibrating = False
            await self._broadcast_control({"type": "calibrateProgress", "done": 0, "total": 0})

    async def _steer_all(self) -> None:
        """v2 servo reference: 'at your local time L the song should be at P'.

        Nodes trim playbackRate by a few hundred ppm against this, which
        corrects DAC-crystal drift that clock sync alone can't see (the sound
        card renders 44100 'Hz' on its own oscillator, not the CPU's).
        """
        p = self.playing
        if p is None:
            return
        t_ref = now() + 0.3
        pos_ms = p.position_ms(t_ref)
        end_ms = p.track.duration_ms
        if pos_ms < 0 or (end_ms is not None and pos_ms > end_ms - 400.0):
            return  # still in the lead-in, or too near the end to bother
        for node in self.nodes.values():
            if node.connected and node.playing_track == p.track.id:
                est = node.model.estimate()
                if est is not None:
                    await node.send(
                        {
                            "type": "steer",
                            "trackId": p.track.id,
                            "posMs": pos_ms,
                            "atNodeMs": est.to_node_time(t_ref) * 1000.0,
                        }
                    )

    # --- mesh (client<->client ping channels) ---------------------------------

    async def _push_mesh_roster(self) -> None:
        """Tell every mesh-capable node who its peers are (excluding itself)."""
        mesh_nodes = [n for n in self.nodes.values() if n.connected and n.mesh]
        for node in mesh_nodes:
            await node.send(
                {
                    "type": "meshRoster",
                    "peers": [
                        {"id": p.client_id, "name": p.name}
                        for p in mesh_nodes
                        if p is not node
                    ],
                }
            )

    def _drop_mesh_pairs_for(self, client_id: str) -> None:
        for key in [k for k in self.mesh_pairs if client_id in k]:
            self.mesh_pairs.pop(key, None)
            self.mesh_seen.pop(key, None)

    def _preload_msg(self, track: Track) -> dict:
        return {
            "type": "preload",
            "trackId": track.id,
            "title": track.title,
            "url": f"/tracks/{track.id}",
        }

    async def _broadcast_players(self, payload: dict) -> None:
        for node in self.nodes.values():
            if node.connected:
                await node.send(payload)

    # --- websocket handlers ------------------------------------------------------

    async def handle_player_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20.0)
        await ws.prepare(request)

        # Handshake: the first frame must be hello{clientId, name}.
        try:
            first = await asyncio.wait_for(ws.receive(), timeout=6.0)
        except TimeoutError:
            await ws.close()
            return ws
        if first.type != WSMsgType.TEXT:
            await ws.close()
            return ws
        try:
            hello = json.loads(first.data)
            assert hello.get("type") == "hello"
            client_id = str(hello["clientId"])[:64]
        except (ValueError, KeyError, AssertionError):
            await ws.close()
            return ws

        name = str(hello.get("name") or f"node-{client_id[:4]}")[:32]
        supports_mesh = bool(hello.get("mesh"))
        node = self.nodes.get(client_id)
        if node is None:
            node = Node(client_id, name)
            node.nudge_ms = self._state["nudges"].get(client_id, 0.0)
            node.volume = self._state["volumes"].get(client_id)
            node.eq_db = self._state["eqs"].get(client_id, [0.0] * 5)
            node.prior_skew = self._state["skews"].get(client_id)
            self.nodes[client_id] = node
        else:
            node.name = name
            if node.ws is not None and not node.ws.closed:
                await node.ws.close()  # stale socket from a previous page-life
            node.end_session()
        node.begin_session(ws, str(hello.get("ua", ""))[:120])
        node.mesh = supports_mesh
        self._drop_mesh_pairs_for(node.client_id)  # stale pairs from a past life
        # Tail slice: date-based fallback ids all share the same *head*.
        log.info(
            "node joined: %s (~%s)%s%s", node.name, node.client_id[-8:],
            " [mesh]" if node.mesh else "",
            "" if node.prior_skew is None
            else " [seeded %+.1f ppm]" % (node.prior_skew * 1e6),
        )

        await node.send(
            {"type": "welcome", "nodeId": node.client_id, "name": node.name,
             "nudgeMs": node.nudge_ms, "volume": node.volume, "eqDb": node.eq_db}
        )
        node.ping_task = asyncio.create_task(self._ping_loop(node))

        await self._push_mesh_roster()

        # If music is (or is about to be) playing, get this node caught up.
        active = self.playing.track if self.playing else None
        if active is not None:
            await node.send(self._preload_msg(active))

        try:
            async for msg in ws:
                t3 = now()  # stamp receipt BEFORE any parsing
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                await self._on_player_msg(node, data, t3)
        finally:
            if node.ws is ws:
                node.end_session()  # banks this session's fitted skew
                self._save_state()  # ...and gets it onto disk while we know it
                self._drop_mesh_pairs_for(node.client_id)
                log.info("node left: %s", node.name)
                await self._push_mesh_roster()
                await self.push_state()
        return ws

    async def _on_player_msg(self, node: Node, data: dict, t3: float) -> None:
        kind = data.get("type")
        if kind == "pong":
            t0 = node.pending.pop(data.get("id"), None)
            if t0 is not None:
                node.model.add(
                    PingSample(
                        t0=t0,
                        c1=float(data["c1"]) / 1000.0,
                        c2=float(data["c2"]) / 1000.0,
                        t3=t3,
                    )
                )
            if len(node.pending) > 64:  # drop pings the node never answered
                cutoff = now() - PENDING_PING_TTL
                node.pending = {k: v for k, v in node.pending.items() if v > cutoff}
        elif kind == "loadProgress":
            # Byte-download progress for a track this node is pulling from us.
            # Relayed straight to control (like spectrum/micLevel) so the pill
            # ticks up in real time — a 30 MB LAN pull finishes well inside one
            # 1 Hz snapshot, so snapshot-only would just blink idle->ready. Also
            # stashed on the node so a late-opening control page sees it too.
            # Display-only, never touches timing.
            tid = str(data.get("trackId"))
            # Only the track we're actively bringing up — never the silent
            # prefetch of the *next* track, which would otherwise repaint a
            # just-loaded node's pill before its "playing" snapshot lands.
            if self.target_track is None or tid != self.target_track.id:
                return
            try:
                pct = int(data.get("pct"))
            except (TypeError, ValueError):
                return
            node.load_track = tid
            node.load_pct = max(0, min(100, pct))
            if self.control_sockets:
                await self._broadcast_control(
                    {"type": "loadProgress", "nodeId": node.client_id, "pct": node.load_pct}
                )
        elif kind == "loaded":
            tid = str(data.get("trackId"))
            node.loaded.add(tid)
            if node.load_track == tid:  # decode done: retire the progress pill
                node.load_track = None
                node.load_pct = None
                if self.control_sockets:  # flip the pill off ⬇% at once, not next snapshot
                    await self._broadcast_control(
                        {"type": "loadProgress", "nodeId": node.client_id, "done": True}
                    )
            track = self.tracks_by_id.get(tid)
            if track is not None and track.duration_ms is None:
                try:
                    track.duration_ms = float(data["durationMs"])
                    log.info('duration of "%s": %.1fs (from %s)',
                             track.title, track.duration_ms / 1000.0, node.name)
                except (KeyError, ValueError):
                    pass
            if self.playing is not None and self.playing.track.id == tid:
                asyncio.create_task(self._catchup(node, tid))
        elif kind == "loadError":
            await self.toast(
                f"{node.name} failed to decode {data.get('trackId')}: {data.get('error')}"
            )
        elif kind == "meshSignal":
            # Dumb relay: offers/answers/ICE between two nodes.
            target = self.nodes.get(str(data.get("to")))
            if target is not None and target.connected:
                await target.send(
                    {"type": "meshSignal", "from": node.client_id,
                     "payload": data.get("payload")}
                )
        elif kind == "meshSample":
            # Raw four-timestamp exchange measured over a peer DataChannel.
            # Only the pair's initiator (lower id) reports, so keys are unique.
            peer = str(data.get("peer"))
            if node.client_id < peer and peer in self.nodes:
                key = (node.client_id, peer)
                model = self.mesh_pairs.get(key)
                if model is None:
                    model = self.mesh_pairs[key] = ClockModel(window=180.0)
                try:
                    model.add(
                        PingSample(
                            t0=float(data["t0"]) / 1000.0,
                            c1=float(data["c1"]) / 1000.0,
                            c2=float(data["c2"]) / 1000.0,
                            t3=float(data["t3"]) / 1000.0,
                        )
                    )
                    self.mesh_seen[key] = now()
                except (KeyError, ValueError, TypeError):
                    pass
        elif kind == "steerAck":
            try:
                node.sync_err_ms = float(data["errMs"])
                node.play_rate = float(data["rate"])
            except (KeyError, ValueError, TypeError):
                pass
        elif kind == "state":
            node.playing_track = data.get("playing")
            if node.playing_track is None:
                node.sync_err_ms = None
                node.play_rate = None
        elif kind == "spectrum":
            # Cosmetic per-node level meter, relayed straight to any open control
            # page — never stored, never touches timing. Clamp hard: client data.
            bands = data.get("bands")
            if self.control_sockets and isinstance(bands, list) and bands:
                try:
                    clean = [max(0, min(255, int(b))) for b in bands[:64]]
                except (TypeError, ValueError):
                    return
                await self._broadcast_control(
                    {"type": "spectrum", "nodeId": node.client_id, "bands": clean}
                )
        elif kind == "micMode":
            # This device opted in / out as a calibration microphone.
            node.mic = bool(data.get("on"))
            await self.push_state()
        elif kind == "micLevel":
            # Live input RMS from a calibration mic, relayed to control. Also
            # self-heals mic state after a reconnect (a level implies mic on).
            node.mic = True
            if self.control_sockets:
                try:
                    rms = float(data.get("rms"))
                except (TypeError, ValueError):
                    return
                await self._broadcast_control(
                    {"type": "micLevel", "nodeId": node.client_id, "rms": rms}
                )
        elif kind == "measureResult":
            # Resolve whoever is awaiting this probe. The caller (_probe_once)
            # owns what happens next — a single readout, or one rep of a sweep.
            pend = self._measure_pending
            if pend is None or data.get("seq") != pend["seq"] or node.client_id != pend["mic"]:
                return
            self._measure_pending = None
            waiter = pend.get("waiter")
            result = None
            if data.get("error"):
                await self.toast(f"Measurement failed: {data.get('error')}")
            else:
                est_m = node.model.estimate()
                try:
                    arrival = est_m.to_conductor_time(
                        float(data["arrivalPerfMs"]) / 1000.0
                    ) if est_m is not None else None
                except (KeyError, ValueError, TypeError):
                    arrival = None
                if arrival is not None:
                    result = {
                        "tofMs": (arrival - pend["t_emit"]) * 1000.0,
                        "peak": data.get("peak"),
                        "snr": data.get("snr"),
                    }
            if waiter is not None and not waiter.done():
                waiter.set_result(result)

    async def handle_control_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20.0)
        await ws.prepare(request)
        self.control_sockets.add(ws)
        try:
            await ws.send_str(json.dumps(self.snapshot()))
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                await self._on_control_cmd(data)
        finally:
            self.control_sockets.discard(ws)
        return ws

    async def _on_control_cmd(self, data: dict) -> None:
        cmd = data.get("cmd")
        if cmd == "play":
            # With a trackId: an explicit override — plays that track and leaves
            # the queue intact for afterwards. Without one (▶ from a standstill):
            # start the queue, consuming its head.
            if data.get("trackId") is not None:
                track = self.tracks_by_id.get(str(data.get("trackId")))
            else:
                track = self._take_next_idle()
            if track:
                self.dispatch(self._transport_play(track))
        elif cmd == "pause":
            self.dispatch(self._transport_pause())
        elif cmd == "resume":
            if self.paused is not None:
                self.dispatch(
                    self._transport_play(self.paused.track, self.paused.seek_ms)
                )
        elif cmd == "seek":
            # Jump the current track to an absolute position — a coordinated
            # re-start of the whole fleet at the new offset, via the same synced
            # path play/resume use (the track's already decoded everywhere, so the
            # load gate clears instantly). dispatch() supersedes rapid re-seeks.
            cur = self.playing or self.paused
            if cur is not None and cur.track.duration_ms:
                try:
                    pos = float(data.get("positionMs", 0))
                except (ValueError, TypeError):
                    return
                pos = max(0.0, min(pos, cur.track.duration_ms - 250.0))
                self.dispatch(self._transport_play(cur.track, pos))
        elif cmd == "next":
            current = (self.playing or self.paused)
            if current is not None:
                nxt = self._take_next(current.track)
            else:
                # Nothing playing: ⏭ starts the queue head, or the library top.
                nxt = self._take_next_idle()
            if nxt:
                self.dispatch(self._transport_play(nxt))
        elif cmd == "stop":
            self.dispatch(self._transport_stop())
        elif cmd == "beep":
            self.dispatch(self._transport_beep())
        elif cmd == "measure":
            asyncio.create_task(self._measure_one(str(data.get("speakerId") or "")))
        elif cmd == "calibrate":
            asyncio.create_task(self._measure_all())
        elif cmd == "resync":
            self.request_burst(4.0)
            await self.toast("Resync burst running on all nodes.")
        elif cmd == "nudge":
            node = self.nodes.get(str(data.get("nodeId")))
            if node is not None:
                try:
                    node.nudge_ms = max(-500.0, min(500.0, float(data.get("nudgeMs", 0))))
                except ValueError:
                    return
                self._save_state()
                await node.send({"type": "config", "nudgeMs": node.nudge_ms,
                                 "volume": node.volume, "eqDb": node.eq_db})
                await self.push_state()
        elif cmd == "volume":
            node = self.nodes.get(str(data.get("nodeId")))
            if node is not None:
                try:
                    node.volume = max(0, min(100, int(float(data.get("volume", 80)))))
                except (ValueError, TypeError):
                    return
                self._save_state()
                await node.send({"type": "config", "nudgeMs": node.nudge_ms,
                                 "volume": node.volume, "eqDb": node.eq_db})
                await self.push_state()
        elif cmd == "eq":
            node = self.nodes.get(str(data.get("nodeId")))
            if node is not None:
                node.eq_db = _clean_eq(data.get("eqDb"))
                self._save_state()
                await node.send({"type": "config", "nudgeMs": node.nudge_ms,
                                 "volume": node.volume, "eqDb": node.eq_db})
                await self.push_state()
        elif cmd == "queue":
            # Append. Duplicates are allowed on purpose — queueing a song twice
            # is a legitimate request, and index-based edits handle the ambiguity.
            track = self.tracks_by_id.get(str(data.get("trackId")))
            if track is not None and len(self.queue) < MAX_QUEUE:
                self.queue.append(track.id)
                await self.toast(f'Queued "{track.title}" (#{len(self.queue)}).')
                await self._prefetch_next()
                await self.push_state()
            elif track is not None:
                await self.toast(f"Queue is full ({MAX_QUEUE}).")
        elif cmd in ("unqueue", "queueMove"):
            # Both edit by index, not id: with duplicates allowed, position is
            # the only unambiguous handle on a queue entry.
            try:
                i = int(data.get("index", -1))
            except (ValueError, TypeError):
                return
            if not 0 <= i < len(self.queue):
                return
            if cmd == "unqueue":
                self.queue.pop(i)
            else:
                try:
                    j = i + int(data.get("delta", 0))
                except (ValueError, TypeError):
                    return
                if not 0 <= j < len(self.queue) or i == j:
                    return
                self.queue[i], self.queue[j] = self.queue[j], self.queue[i]
            await self._prefetch_next()
            await self.push_state()
        elif cmd == "queueClear":
            if self.queue:
                self.queue.clear()
                await self.toast("Queue cleared - back to folder order.")
                await self._prefetch_next()
                await self.push_state()
        elif cmd == "rescan":
            n = self.scan_tracks()
            await self.toast(f"Rescanned: {n} track(s).")
            await self._prefetch_next()
            await self.push_state()

    # --- http handlers -------------------------------------------------------------

    async def handle_player_page(self, _req: web.Request) -> web.FileResponse:
        return web.FileResponse(WEB_DIR / "player.html")

    async def handle_control_page(self, _req: web.Request) -> web.FileResponse:
        return web.FileResponse(WEB_DIR / "control.html")

    async def handle_track(self, request: web.Request) -> web.FileResponse:
        track = self.tracks_by_id.get(request.match_info["tid"])
        if track is None or not track.path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(track.path)


@web.middleware
async def _no_cache(request: web.Request, handler):
    """no-cache (not no-store): browsers must revalidate, so a page reload
    always picks up new player/control code — 304s keep it instant on LAN.
    Without this, stale cached pages silently miss protocol upgrades."""
    resp = await handler(request)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


def build_app(music_dir: Path) -> web.Application:
    conductor = Conductor(music_dir)
    app = web.Application(middlewares=[_no_cache])
    app["conductor"] = conductor
    app.router.add_get("/", conductor.handle_player_page)
    app.router.add_get("/control", conductor.handle_control_page)
    app.router.add_get("/tracks/{tid}", conductor.handle_track)
    app.router.add_get("/ws/player", conductor.handle_player_ws)
    app.router.add_get("/ws/control", conductor.handle_control_ws)
    app.router.add_static("/static", WEB_DIR)
    app.on_startup.append(conductor.start)
    app.on_shutdown.append(conductor.shutdown)
    return app
