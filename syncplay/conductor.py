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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set

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
LOAD_GATE_TIMEOUT = 12.0  # max wait for nodes to report a track decoded
PENDING_PING_TTL = 5.0

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
    buffers, so both reset on a fresh socket."""

    def __init__(self, client_id: str, name: str):
        self.client_id = client_id
        self.name = name
        self.nudge_ms = 0.0
        self.ws: Optional[web.WebSocketResponse] = None
        self.model = ClockModel()
        self.connected = False
        self.ua = ""
        self.ping_seq = 0
        self.pending: Dict[int, float] = {}  # ping id -> t0
        self.loaded: Set[str] = set()  # track ids decoded and ready this page-life
        self.playing_track: Optional[str] = None  # as reported by the node
        self.burst_until = 0.0
        self.kick = asyncio.Event()
        self.ping_task: Optional[asyncio.Task] = None

    def begin_session(self, ws: web.WebSocketResponse, ua: str) -> None:
        self.ws = ws
        self.ua = ua
        self.connected = True
        self.model = ClockModel()
        self.pending.clear()
        self.loaded.clear()
        self.playing_track = None
        self.burst_until = 0.0

    def end_session(self) -> None:
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
            "playing": self.playing_track,
            "loadedCurrent": bool(current_track and current_track in self.loaded),
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
        self._advance_task: Optional[asyncio.Task] = None
        self._transport_task: Optional[asyncio.Task] = None
        self._pulse_task: Optional[asyncio.Task] = None
        self._nudges: Dict[str, float] = self._load_state()
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
        log.info("library: %d track(s) in %s", len(tracks), self.music_dir)
        return len(tracks)

    def _next_track(self, current: Track) -> Optional[Track]:
        if not self.tracks:
            return None
        try:
            i = next(i for i, t in enumerate(self.tracks) if t.id == current.id)
        except StopIteration:
            return self.tracks[0]
        return self.tracks[(i + 1) % len(self.tracks)]

    # --- nudge persistence ----------------------------------------------------

    def _load_state(self) -> Dict[str, float]:
        try:
            data = json.loads(STATE_FILE.read_text("utf-8"))
            return {str(k): float(v) for k, v in data.get("nudges", {}).items()}
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        try:
            nudges = {n.client_id: n.nudge_ms for n in self.nodes.values() if n.nudge_ms}
            nudges.update({k: v for k, v in self._nudges.items() if k not in self.nodes})
            STATE_FILE.write_text(json.dumps({"nudges": nudges}, indent=1), "utf-8")
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
                current = self.playing.track.id if self.playing else None
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
            "musicDir": str(self.music_dir),
            "nodes": [n.stats(current) for n in self.nodes.values()],
        }

    async def push_state(self) -> None:
        if not self.control_sockets:
            return
        msg = json.dumps(self.snapshot())
        for ws in list(self.control_sockets):
            try:
                await ws.send_str(msg)
            except (ConnectionError, RuntimeError):
                self.control_sockets.discard(ws)

    async def toast(self, text: str) -> None:
        log.info("toast: %s", text)
        msg = json.dumps({"type": "toast", "text": text})
        for ws in list(self.control_sockets):
            try:
                await ws.send_str(msg)
            except (ConnectionError, RuntimeError):
                self.control_sockets.discard(ws)

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
        self.paused = None
        if self.playing is not None:
            await self._broadcast_players({"type": "stop"})
            self.playing = None

        await self._broadcast_players(self._preload_msg(track))

        # Load gate: wait for every connected node to decode (or time out).
        deadline = now() + LOAD_GATE_TIMEOUT
        while now() < deadline:
            connected = [n for n in self.nodes.values() if n.connected]
            if connected and all(track.id in n.loaded for n in connected):
                break
            await asyncio.sleep(0.15)
        ready = [
            n for n in self.nodes.values() if n.connected and track.id in n.loaded
        ]
        if not ready:
            await self.toast(f'No node managed to load "{track.title}" - not starting.')
            return

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
        nxt = self._next_track(track)
        if nxt and nxt.id != track.id:
            await self._broadcast_players(self._preload_msg(nxt))
        await self.push_state()

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
        nxt = self._next_track(p.track)
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
        self.playing = None
        self.paused = None
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
        node = self.nodes.get(client_id)
        if node is None:
            node = Node(client_id, name)
            node.nudge_ms = self._nudges.get(client_id, 0.0)
            self.nodes[client_id] = node
        else:
            node.name = name
            if node.ws is not None and not node.ws.closed:
                await node.ws.close()  # stale socket from a previous page-life
            node.end_session()
        node.begin_session(ws, str(hello.get("ua", ""))[:120])
        log.info("node joined: %s (%s)", node.name, node.client_id[:8])

        await node.send(
            {"type": "welcome", "nodeId": node.client_id, "name": node.name,
             "nudgeMs": node.nudge_ms}
        )
        node.ping_task = asyncio.create_task(self._ping_loop(node))

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
                node.end_session()
                log.info("node left: %s", node.name)
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
        elif kind == "loaded":
            tid = str(data.get("trackId"))
            node.loaded.add(tid)
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
        elif kind == "state":
            node.playing_track = data.get("playing")

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
            track = self.tracks_by_id.get(str(data.get("trackId")))
            if track:
                self.dispatch(self._transport_play(track))
        elif cmd == "pause":
            self.dispatch(self._transport_pause())
        elif cmd == "resume":
            if self.paused is not None:
                self.dispatch(
                    self._transport_play(self.paused.track, self.paused.seek_ms)
                )
        elif cmd == "next":
            current = (self.playing or self.paused)
            base = current.track if current else (self.tracks[0] if self.tracks else None)
            if base is not None:
                nxt = self._next_track(base) if current else base
                if nxt:
                    self.dispatch(self._transport_play(nxt))
        elif cmd == "stop":
            self.dispatch(self._transport_stop())
        elif cmd == "beep":
            self.dispatch(self._transport_beep())
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
                await node.send({"type": "config", "nudgeMs": node.nudge_ms})
                await self.push_state()
        elif cmd == "rescan":
            n = self.scan_tracks()
            await self.toast(f"Rescanned: {n} track(s).")
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


def build_app(music_dir: Path) -> web.Application:
    conductor = Conductor(music_dir)
    app = web.Application()
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
