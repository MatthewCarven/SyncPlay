"""What a node says about itself, and what the conductor makes of it.

Telemetry slice 3. Until now a node's hello said its name and its build; a
source start said only that one had happened; a phone that slept was inferred
from the re-anchor on wake; and a deferred node stood silent with nothing on
its screen to say why. Now the hello carries the device's sample rate,
latencies and the player's own servo constants; every `state` carries the
cause the conductor gave it, or the re-anchor rule that fired and the error
that fired it; the AudioContext's state and the page's visibility arrive as
two rare messages; and the conductor can put one line on a node's screen.

Every new field is client data and is treated like the spectrum bands were:
allow-listed, clamped, or dropped. A hostile or broken node can say nothing
that reaches the page unescaped, the ring unbounded, or the log unlabelled.
"""

import asyncio
import json
import wave
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

import syncplay.conductor as C
from syncplay.conductor import (
    CATCHUP_WAIT_S,
    Conductor,
    Node,
    Playback,
    _clean_hz,
    _clean_latency_ms,
    _clean_map_ms,
    _clean_servo,
    build_app,
    now,
)
from syncplay.timesync import PingSample
from syncplay.trace import Trace


def relay_into(cond):
    sent = []

    async def relay(payload):
        sent.append(payload)

    cond._broadcast_control = relay
    return sent


def sent_to(node):
    """Record what the conductor sends to one node."""
    box = []

    async def send(payload):
        box.append(payload)

    node.send = send
    return box


def feed(model, n=16, span=60.0):
    base = now() - span
    for i in range(n):
        t = base + span * i / max(1, n - 1)
        model.add(PingSample(t0=t, c1=t + 1.0, c2=t + 1.0, t3=t))


def _playing(track_id="t1"):
    track = SimpleNamespace(id=track_id, title="song", duration_ms=180_000.0)
    return Playback(track=track, t_start=now(), seek_ms=0.0)


def state(cond, node, **fields):
    asyncio.run(cond._on_player_msg(node, {"type": "state", **fields}, now()))


# --- the hello ----------------------------------------------------------------


def test_device_facts_are_clamped():
    assert _clean_hz(48000) == 48000.0 and _clean_hz("44100") == 44100.0
    for junk in ("banana", None, -5, 1e9, float("nan")):
        assert _clean_hz(junk) is None
    assert _clean_latency_ms(21.3) == 21.3 and _clean_latency_ms(0) == 0.0
    for junk in (-1, 1e9, float("inf"), "x", None):
        assert _clean_latency_ms(junk) is None
    assert _clean_servo({
        "reanchorS": 0.2, "slewLimitS": "0.024", "evil": 1, "maxRateTrim": "x",
        "steerHorizonS": float("inf"), "slewPatienceS": -1,
    }) == {"reanchorS": 0.2, "slewLimitS": 0.024}
    assert _clean_servo("not a dict") == {} and _clean_servo(None) == {}
    assert _clean_map_ms(123456.7) == 123456.7
    for junk in ("inf", None, "x", 1e13):
        assert _clean_map_ms(junk) is None


def test_a_hello_with_device_facts_reaches_stats_and_the_join_event(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "STATE_FILE", tmp_path / "state.json")

    async def run():
        app = build_app(tmp_path)
        cond = app["conductor"]
        async with TestClient(TestServer(app)) as client:
            p = await client.ws_connect("/ws/player")
            await p.send_json({
                "type": "hello", "clientId": "n1", "name": "tablet",
                "sampleRate": 48000, "baseLatencyMs": 5.3, "outputLatencyMs": 21.3,
                "servo": {"reanchorS": 0.2, "slewLimitS": 0.024, "slewPatienceS": 10,
                          "maxRateTrim": 8e-4, "steerHorizonS": 15, "keepWarm": 1e-6, "junk": "x"},
            })
            await asyncio.sleep(0.2)
            n = cond.nodes["n1"]
            assert (n.sample_rate, n.base_latency_ms, n.output_latency_ms) == (48000.0, 5.3, 21.3)
            assert n.servo == {"reanchorS": 0.2, "slewLimitS": 0.024, "slewPatienceS": 10.0,
                               "maxRateTrim": 8e-4, "steerHorizonS": 15.0, "keepWarm": 1e-6}
            s = n.stats(None)
            assert s["sampleRate"] == 48000.0 and s["outputLatencyMs"] == 21.3
            assert s["servo"]["reanchorS"] == 0.2
            join = [e for e in cond.events if e["kind"] == "join"][-1]
            assert join["sampleRate"] == 48000.0 and join["servo"]["slewPatienceS"] == 10.0
            assert "48000 Hz" in join["text"] and "out 21.3 ms" in join["text"]
            await p.close()
            await asyncio.sleep(0.1)

            # A hello that says nothing (an old page, or a hostile one) leaves
            # the facts empty and the text plain.
            p2 = await client.ws_connect("/ws/player")
            await p2.send_json({"type": "hello", "clientId": "n2", "name": "old",
                                "sampleRate": "banana", "servo": [1, 2]})
            await asyncio.sleep(0.2)
            n2 = cond.nodes["n2"]
            assert n2.sample_rate is None and n2.servo == {}
            assert "Hz" not in [e for e in cond.events if e["kind"] == "join"][-1]["text"]
            await p2.close()

    asyncio.run(run())


def test_a_new_session_forgets_the_facts_until_the_hello_says_them_again():
    n = Node("id", "n")
    n.sample_rate = 48000.0
    n.servo = {"reanchorS": 0.2}
    n.last_start = {"cause": "play"}
    n.map_ms = 1.0
    n.begin_session(None, "")
    assert n.sample_rate is None and n.servo == {}
    assert n.last_start is None and n.map_ms is None


# --- a cause on every start ---------------------------------------------------


def test_a_start_says_why(bare):
    relay_into(bare)
    bare.playing = _playing()
    n = Node("id", "phone")
    state(bare, n, playing="t1", cause="catchup")
    ev = bare.events[-1]
    assert ev["kind"] == "start" and ev["cause"] == "catchup"
    assert ev["text"] == "source started (catchup)"
    assert n.last_start == {"cause": "catchup", "reason": None, "errMs": None, "lateMs": None}
    assert n.stats("t1")["lastStart"]["cause"] == "catchup"


def test_a_late_start_says_how_late(bare):
    relay_into(bare)
    bare.playing = _playing()
    n = Node("id", "phone")
    state(bare, n, playing="t1", cause="play", lateMs=340.0)
    assert bare.events[-1]["text"] == "source started (play, 340 ms late)"
    assert bare.events[-1]["lateMs"] == 340.0


def test_a_reanchor_restart_says_which_rule_fired_and_how_far_out(bare):
    relay_into(bare)
    bare.playing = _playing()
    n = Node("id", "tablet")
    state(bare, n, playing="t1", cause="play")
    state(bare, n, playing="t1", cause="reanchor", reason="patience", errMs=31.4)
    ev = bare.events[-1]
    assert ev["kind"] == "restart" and ev["level"] == "warning"
    assert ev["text"] == "source restarted (#1 this track): re-anchor, patience at +31 ms"
    assert ev["cause"] == "reanchor" and ev["reason"] == "patience" and ev["errMs"] == 31.4
    state(bare, n, playing="t1", cause="reanchor", reason="fault", errMs=-412.0)
    assert bare.events[-1]["text"].endswith("re-anchor, fault at -412 ms")
    assert n.stats("t1")["lastStart"]["reason"] == "fault" and n.restarts == 2


def test_hostile_causes_are_normalised_not_echoed(bare):
    relay_into(bare)
    bare.playing = _playing()
    n = Node("id", "n")
    state(bare, n, playing="t1", cause="<script>alert(1)</script>", reason="banana",
          errMs="inf", lateMs=-5)
    ev = bare.events[-1]
    assert ev["cause"] == "unknown" and ev["reason"] is None and ev["errMs"] is None
    assert ev["text"] == "source started (unknown)"
    assert "<" not in json.dumps(ev)
    state(bare, n, playing="t1")
    assert bare.events[-1]["cause"] == "unknown", "an old page says nothing: unknown too"


# --- two rare messages --------------------------------------------------------


def test_ctx_state_is_an_event_and_anything_but_running_is_a_warning(bare):
    relay_into(bare)
    n = Node("id", "phone")
    for st, level in (("suspended", "warning"), ("interrupted", "warning"),
                      ("running", "info"), ("closed", "warning")):
        asyncio.run(bare._on_player_msg(n, {"type": "ctxState", "state": st}, now()))
        ev = bare.events[-1]
        assert ev["kind"] == "ctx" and ev["state"] == st and ev["level"] == level
        assert ev["text"] == f"audio context {st}"
    before = len(bare.events)
    asyncio.run(bare._on_player_msg(n, {"type": "ctxState", "state": "<b>evil</b>"}, now()))
    asyncio.run(bare._on_player_msg(n, {"type": "ctxState"}, now()))
    assert len(bare.events) == before, "a state we did not define is not an event"


def test_visibility_is_an_event(bare):
    relay_into(bare)
    n = Node("id", "phone")
    for hidden in (True, "yes", False):
        asyncio.run(bare._on_player_msg(n, {"type": "visibility", "hidden": hidden}, now()))
    assert [e["text"] for e in bare.events] == ["page hidden", "page hidden", "page visible"]
    assert bare.events[0]["hidden"] is True and bare.events[-1]["hidden"] is False


# --- mapMs --------------------------------------------------------------------


def test_map_ms_rides_the_steer_ack_into_stats_and_the_trace(bare, tmp_path):
    tr = Trace(tmp_path / "t.jsonl")
    bare.trace = tr
    n = Node("id", "tablet")
    feed(n.model)

    async def run():
        await tr.start()
        for m in (123456.7, "banana", None):
            msg = {"type": "steerAck", "errMs": 0.5, "rate": 1.0}
            if m is not None:
                msg["mapMs"] = m
            await bare._on_player_msg(n, msg, now())
            assert n.map_ms == (123456.7 if m == 123456.7 else None)
            assert n.stats(None)["mapMs"] == n.map_ms
        await tr.stop()

    asyncio.run(run())
    rows = [json.loads(s) for s in tr.path.read_text(encoding="utf-8").splitlines() if s.strip()]
    assert [r["mapMs"] for r in rows] == [123456.7, None, None]


# --- notices --------------------------------------------------------------------


def test_a_notice_goes_to_one_node_as_text(bare):
    n = Node("id", "phone")
    box = sent_to(n)
    asyncio.run(bare.notice(n, "clock still settling - joining automatically", 40.0))
    assert box == [{"type": "notice", "text": "clock still settling - joining automatically",
                    "ttlS": 40.0}]


@pytest.fixture()
def lib(tmp_path):
    with wave.open(str(tmp_path / "a.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * 800)
    return tmp_path


@pytest.fixture()
def quick(lib, monkeypatch):
    """A real conductor with the load gate shrunk to a blink."""
    monkeypatch.setattr(C, "LOAD_GATE_TIMEOUT", 0.05)
    monkeypatch.setattr(C, "LOAD_GATE_COLD", 0.05)
    monkeypatch.setattr(C, "ARM_SECONDS", 0.02)
    monkeypatch.setattr(C, "STATE_FILE", lib / "state.json")
    return Conductor(lib)


def _node(cond, name, *, loaded=None, timed=True):
    n = Node(f"id-{name}", name)
    n.connected = True
    if loaded:
        n.loaded.add(loaded)
    if timed:
        feed(n.model)
    n.box = sent_to(n)
    cond.nodes[n.client_id] = n
    return n


def test_a_deferred_node_is_told_why_it_is_silent(quick):
    track = quick.tracks[0]
    timed = _node(quick, "laptop", loaded=track.id, timed=True)
    late = _node(quick, "phone", loaded=track.id, timed=False)

    async def run():
        await quick._transport_play(track)
        quick._advance_task.cancel()
        late.catchup_task.cancel()

    asyncio.run(run())
    notices = [m for m in late.box if m["type"] == "notice"]
    assert len(notices) == 1 and "clock still settling" in notices[0]["text"]
    assert notices[0]["ttlS"] > CATCHUP_WAIT_S, "outlasts the catch-up that replaces it"
    assert not [m for m in timed.box if m["type"] == "notice"], "the node that started is told nothing"
    assert [m for m in timed.box if m["type"] == "play"][0]["why"] == "play"


def test_a_catchup_tells_the_node_it_is_joining(bare):
    relay_into(bare)
    bare.playing = _playing()
    n = Node("id", "late")
    n.connected = True
    feed(n.model)  # fitted: ready at once
    box = sent_to(n)
    asyncio.run(bare._catchup(n, "t1"))
    assert [m["type"] for m in box] == ["play", "notice"]
    assert box[0]["why"] == "catchup"
    assert box[1]["text"] == "joining now"


def test_a_catchup_that_gives_up_tells_the_node_why(bare, monkeypatch):
    relay_into(bare)
    bare.playing = _playing()
    n = Node("id", "never")
    n.connected = True
    feed(n.model, n=25, span=4.0)  # young, and it stays young
    box = sent_to(n)
    t = [now()]
    monkeypatch.setattr(C, "now", lambda: t[0])

    async def run():
        task = asyncio.create_task(bare._catchup(n, "t1"))
        await asyncio.sleep(0.3)
        t[0] += CATCHUP_WAIT_S + 1.0
        await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(run())
    assert [m["type"] for m in box] == ["notice"]
    assert "could not join" in box[0]["text"]


def test_a_calibration_tells_the_mic_to_keep_still(bare, monkeypatch):
    relay_into(bare)
    monkeypatch.setattr(C, "MEASURE_TIMEOUT", 0.05)
    bare._measure_pending = None
    bare._calibrating = False
    bare._measure_seq = 0
    mic, spk = Node("m", "laptop"), Node("s", "tablet")
    for n in (mic, spk):
        n.connected = True
        feed(n.model)
    mic.mic = True
    mic_box, spk_box = sent_to(mic), sent_to(spk)
    bare.nodes = {mic.client_id: mic, spk.client_id: spk}
    asyncio.run(bare._measure_one(""))
    assert [m for m in mic_box if m["type"] == "notice"][0]["text"] == "measuring - keep still"
    assert not [m for m in spk_box if m["type"] == "notice"]


# --- why, end to end through the transport ------------------------------------


def test_the_play_command_carries_why(bare):
    n = Node("id", "n")
    n.connected = True
    feed(n.model)
    box = sent_to(n)
    track = SimpleNamespace(id="t", title="s", duration_ms=1.0)
    p = Playback(track=track, t_start=now(), seek_ms=0.0, why="seek")
    assert asyncio.run(bare._send_play(n, p)) is True
    assert box[0]["type"] == "play" and box[0]["why"] == "seek"
    assert Playback(track=track, t_start=0.0, seek_ms=0.0).why == "play"


def test_every_way_of_starting_carries_its_why(quick):
    track = quick.tracks[0]
    track.duration_ms = 180_000.0
    n = _node(quick, "laptop", loaded=track.id, timed=True)

    async def run():
        for cmd, why in (({"cmd": "play", "trackId": track.id}, "play"),
                         ({"cmd": "seek", "positionMs": 1000}, "seek"),
                         ({"cmd": "next"}, "next")):
            await quick._on_control_cmd(cmd)
            await asyncio.sleep(0.3)  # the dispatched transport runs
            assert quick.playing is not None and quick.playing.why == why, cmd
        await quick._on_control_cmd({"cmd": "pause"})
        await asyncio.sleep(0.2)
        await quick._on_control_cmd({"cmd": "resume"})
        await asyncio.sleep(0.3)
        assert quick.playing.why == "resume"
        quick._cancel_advance()
        if quick._transport_task:
            quick._transport_task.cancel()

    asyncio.run(run())
    assert [m["why"] for m in n.box if m["type"] == "play"] == ["play", "seek", "next", "resume"]
