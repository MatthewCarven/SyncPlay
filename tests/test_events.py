"""Events: one helper, a bounded ring, and what a control page is told.

The control page had one toast slot that forgot after 3.5 s, and the conductor
had a log that died with its console window. Everything notable now goes
through `Conductor.event`, which is the seam these tests pin: the ring is
bounded, a page that connects late gets the history once and live events
after, `toast()` still toasts *and* leaves a row, and the things that used to
be silent — a catch-up that gives up, a source that restarts, a drift refused
at the door — now say so.

Rule two of the telemetry plan applies to the ring as it will to the trace: a
diagnostic must never be the reason nobody can play. Nothing here awaits a
node, and nothing here asks a node for anything.
"""

import asyncio
import json
import logging
import wave
from types import SimpleNamespace

import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from syncplay.conductor import (
    CATCHUP_WAIT_S,
    EVENT_RING,
    Conductor,
    Node,
    Playback,
    build_app,
    now,
)
from syncplay.timesync import ClockModel, PingSample


def relay_into(cond):
    """Record everything the conductor would fan out to control pages."""
    sent = []

    async def relay(payload):
        sent.append(payload)

    cond._broadcast_control = relay
    return sent


def sample(t: float, offset: float = 1.0) -> PingSample:
    return PingSample(t0=t, c1=t + offset, c2=t + offset, t3=t)


def feed(model: ClockModel, n: int, span: float, start: float = 0.0) -> None:
    base = now() - span + start
    for i in range(n):
        model.add(sample(base + span * i / max(1, n - 1)))


def _playing(track_id="t1"):
    track = SimpleNamespace(id=track_id, title="song", duration_ms=180_000.0)
    return Playback(track=track, t_start=now(), seek_ms=0.0)


def _est(**kw):
    """A stand-in for a `ClockEstimate`, with just what `remember_skew` reads."""
    base = dict(skew_fitted=True, skew_saturated=False, skew_ppm=20.0, skew=20e-6,
                span=120.0, n_used=40, skew_bound_ppm=3.0)
    base.update(kw)
    credible = base.pop("credible", True)
    est = SimpleNamespace(**base)
    est.skew_credible_at = lambda snr: credible
    return est


# --- the helper ---------------------------------------------------------------


def test_an_event_is_logged_kept_and_pushed(bare, caplog):
    sent = relay_into(bare)
    node = Node("id-t", "Mums-Tablet")
    with caplog.at_level(logging.INFO, logger="syncplay"):
        row = asyncio.run(bare.event("start", "source started", node=node, track="abc"))
    assert row["kind"] == "start" and row["level"] == "info"
    assert row["node"] == "id-t" and row["name"] == "Mums-Tablet"
    assert row["track"] == "abc", "extra fields ride the row untouched"
    assert len(row["wall"]) == 12 and row["wall"][2] == ":", "HH:MM:SS.mmm"
    assert list(bare.events) == [row]
    assert sent == [{"type": "event", **row}], "pushed live, and not toasted"
    assert "start Mums-Tablet: source started" in caplog.text


def test_a_toast_is_an_event_too(bare):
    """All 29 `toast()` call sites are unchanged; this is what they now do."""
    sent = relay_into(bare)
    asyncio.run(bare.toast("Resync burst running on all nodes."))
    assert [m["type"] for m in sent] == ["event", "toast"]
    assert bare.events[-1]["kind"] == "toast"
    assert sent[1]["text"] == "Resync burst running on all nodes."


def test_a_node_event_toasts_with_the_name_in_front(bare):
    """The card has a node column; a toast is one line and has to say who."""
    sent = relay_into(bare)
    asyncio.run(bare.event("x", "sat this one out", node=Node("id", "phone"), toast=True))
    assert sent[-1] == {"type": "toast", "text": "phone: sat this one out"}


def test_the_ring_is_bounded(bare):
    relay_into(bare)

    async def run():
        for i in range(EVENT_RING + 50):
            await bare.event("k", f"event {i}")

    asyncio.run(run())
    assert len(bare.events) == EVENT_RING
    assert bare.events[0]["text"] == "event 50", "the oldest fall off the front"
    assert bare.events[-1]["text"] == f"event {EVENT_RING + 49}"


def test_an_unknown_level_falls_back_to_info(bare):
    relay_into(bare)
    assert asyncio.run(bare.event("k", "text", level="critical"))["level"] == "info"


def test_a_debug_event_stays_out_of_an_info_console(bare, caplog):
    relay_into(bare)
    with caplog.at_level(logging.INFO, logger="syncplay"):
        asyncio.run(bare.event("cadence", "ping boost 1.00x -> 2.00x", level="debug"))
    assert "ping boost" not in caplog.text
    assert bare.events[-1]["level"] == "debug", "but it is on the card, dimmed"


# --- what a control page is told ---------------------------------------------


async def _next_of(ws, kind, timeout=5.0):
    """The next control message of `kind`, skipping the 1 Hz snapshots."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        left = deadline - loop.time()
        assert left > 0, f"no {kind} message arrived"
        msg = await asyncio.wait_for(ws.receive(), timeout=left)
        assert msg.type == WSMsgType.TEXT, msg
        data = json.loads(msg.data)
        if data.get("type") == kind:
            return data


def test_a_control_page_gets_the_history_once_and_then_live(tmp_path, monkeypatch):
    """A page opened late sees the evening; an open one sees each event as it
    happens; the ring never rides the snapshot. Real sockets, because the
    handshake is the thing under test."""
    import syncplay.conductor as C

    monkeypatch.setattr(C, "STATE_FILE", tmp_path / "state.json")

    async def run():
        app = build_app(tmp_path)
        cond = app["conductor"]
        async with TestClient(TestServer(app)) as client:
            await cond.event("k", "before anyone looked")  # nobody watching yet

            ctl = await client.ws_connect("/ws/control")
            first = json.loads((await ctl.receive()).data)
            assert first["type"] == "snapshot" and "events" not in first
            hist = json.loads((await ctl.receive()).data)
            assert hist["type"] == "events"
            assert [e["text"] for e in hist["items"]] == ["before anyone looked"]

            # A node joins: the open page hears it at once.
            player = await client.ws_connect("/ws/player")
            await player.send_json({
                "type": "hello", "clientId": "node-1", "name": "phone",
                "build": cond.player_build(),
            })
            join = await _next_of(ctl, "event")
            assert join["kind"] == "join" and join["name"] == "phone"
            assert join["fresh"] is True and join["build"] == cond.player_build()
            assert join["text"].startswith("joined (~")

            # And leaves.
            await player.close()
            leave = await _next_of(ctl, "event")
            assert leave["kind"] == "leave" and leave["node"] == "node-1"

            # A second page, later, gets all of it in one message.
            ctl2 = await client.ws_connect("/ws/control")
            await ctl2.receive()  # its snapshot
            hist2 = json.loads((await ctl2.receive()).data)
            assert [e["kind"] for e in hist2["items"]] == ["k", "join", "leave"]
            await ctl.close()
            await ctl2.close()

    asyncio.run(run())


def test_a_returning_node_is_back_and_a_new_build_means_reloaded(tmp_path, monkeypatch):
    """A socket reconnect and a page reload look the same from the conductor;
    only a build that changed proves the page itself was reloaded."""
    import syncplay.conductor as C

    monkeypatch.setattr(C, "STATE_FILE", tmp_path / "state.json")

    async def run():
        app = build_app(tmp_path)
        cond = app["conductor"]
        async with TestClient(TestServer(app)) as client:
            for build in ("aaaa0001", "aaaa0001", "bbbb0002"):
                p = await client.ws_connect("/ws/player")
                await p.send_json({"type": "hello", "clientId": "n", "name": "n", "build": build})
                await asyncio.sleep(0.05)
                await p.close()
                await asyncio.sleep(0.05)
        joins = [e["text"] for e in cond.events if e["kind"] == "join"]
        assert joins[0].startswith("joined (")
        assert joins[1].startswith("back (")
        assert joins[2].startswith("back, reloaded (")

    asyncio.run(run())


# --- a source that starts, and one that restarts ------------------------------


def state(cond, node, playing):
    asyncio.run(cond._on_player_msg(node, {"type": "state", "playing": playing}, now()))


def test_the_first_start_of_a_playback_is_a_start_and_the_rest_are_restarts(bare):
    relay_into(bare)
    bare.playing = _playing()
    n = Node("id", "tablet")
    state(bare, n, "t1")
    assert n.restarts == 0
    assert bare.events[-1]["kind"] == "start"
    state(bare, n, "t1")  # a re-anchor: same playback, a new source
    state(bare, n, "t1")
    assert n.restarts == 2
    ev = bare.events[-1]
    assert ev["kind"] == "restart" and ev["level"] == "warning"
    assert ev["restarts"] == 2 and "#2" in ev["text"]
    assert n.stats("t1")["restarts"] == 2


def test_a_new_playback_of_the_same_track_resets_the_count(bare):
    """A seek is a new Playback under the same trackId: every node starts a
    fresh source, and that is not a restart anyone chose."""
    relay_into(bare)
    bare.playing = _playing("t1")
    n = Node("id", "tablet")
    state(bare, n, "t1")
    state(bare, n, "t1")
    assert n.restarts == 1
    bare.playing = _playing("t1")  # the seek
    state(bare, n, "t1")
    assert n.restarts == 0
    assert bare.events[-1]["kind"] == "start"


def test_a_stop_report_is_not_an_event(bare):
    """Five nodes reporting null on every stop would be noise; the transport's
    own `stop` event is the one line that matters."""
    relay_into(bare)
    state(bare, Node("id", "tablet"), None)
    assert not bare.events


def test_a_new_session_starts_the_count_over(bare):
    relay_into(bare)
    bare.playing = _playing()
    n = Node("id", "tablet")
    state(bare, n, "t1")
    state(bare, n, "t1")
    assert n.restarts == 1
    n.begin_session(None, "")
    assert n.restarts == 0 and n.run_playback is None


# --- the catch-up that gives up ----------------------------------------------


def test_a_catchup_that_gives_up_says_so(bare, monkeypatch):
    """Silent until now: `_catchup` simply returned at its deadline, and a
    speaker sitting a track out looked like a fault nobody had reported."""
    sent = relay_into(bare)
    bare.playing = _playing()
    node = Node("id", "never")
    node.connected = True
    feed(node.model, n=25, span=4.0)  # young, and it stays young

    import syncplay.conductor as C

    t = [now()]
    monkeypatch.setattr(C, "now", lambda: t[0])

    async def run():
        task = asyncio.create_task(bare._catchup(node, "t1"))
        await asyncio.sleep(0.3)
        t[0] += CATCHUP_WAIT_S + 1.0
        await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(run())
    ev = bare.events[-1]
    assert ev["kind"] == "catchup-timeout" and ev["level"] == "warning"
    assert ev["node"] == "id" and ev["waitedS"] == CATCHUP_WAIT_S
    assert "song" in ev["text"]
    assert sent[-1]["type"] == "toast" and sent[-1]["text"].startswith("never: ")


def test_a_catchup_that_joins_says_how_long_it_waited(bare):
    relay_into(bare)
    bare.playing = _playing()
    node = Node("id", "late")
    node.connected = True
    feed(node.model, n=25, span=4.0)

    async def accept(n, p):
        return True

    bare._send_play = accept

    async def run():
        task = asyncio.create_task(bare._catchup(node, "t1"))
        await asyncio.sleep(0.4)
        feed(node.model, n=16, span=60.0, start=10.0)  # now it fits
        await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(run())
    ev = bare.events[-1]
    assert ev["kind"] == "catchup" and ev["node"] == "id"
    assert 0.3 <= ev["waitedS"] < 5.0
    assert ev["text"].startswith('joined "song"')


def test_a_catchup_abandoned_by_a_track_change_is_not_a_timeout(bare):
    """The track moved on; the node did nothing wrong and is owed no warning."""
    relay_into(bare)
    bare.playing = _playing("t2")
    node = Node("id", "n")
    node.connected = True
    asyncio.run(bare._catchup(node, "t1"))
    assert not bare.events


# --- what a node carries away ------------------------------------------------


def test_a_saturated_fit_is_refused_with_a_reason():
    """`remember_skew` used to log its refusal on every state save. It now
    leaves one line for the conductor to report when the session ends."""
    node = Node("id", "n")
    node.model = SimpleNamespace(estimate=lambda: _est(skew_saturated=True, skew_ppm=500.0))
    assert node.remember_skew() is False
    assert "saturated" in node.bank_note and "500.0 ppm" in node.bank_note


def test_an_incredible_fit_is_refused_with_a_reason():
    node = Node("id", "n")
    node.prior_skew = 12e-6
    node.model = SimpleNamespace(estimate=lambda: _est(credible=False, skew_ppm=2.0))
    assert node.remember_skew() is False
    assert "inside its own error bound" in node.bank_note
    assert "keeping 12.0 ppm" in node.bank_note


def test_a_banked_fit_leaves_no_note():
    node = Node("id", "n")
    node.model = SimpleNamespace(estimate=lambda: _est(credible=False))
    node.remember_skew()
    assert node.bank_note
    node.model = SimpleNamespace(estimate=lambda: _est())
    assert node.end_session() is True
    assert node.bank_note is None and node.prior_skew == 20e-6


def test_leaving_reports_what_the_node_carries_away(bare):
    relay_into(bare)
    bare._save_state = lambda: None
    node = Node("id", "tablet")
    node.connected = True
    node.model = SimpleNamespace(estimate=lambda: _est(skew_ppm=-31.2, skew=-31.2e-6))
    asyncio.run(bare._node_left(node))
    assert [e["kind"] for e in bare.events] == ["leave", "drift-banked"]
    assert "-31.2 ppm" in bare.events[-1]["text"]
    assert node.connected is False


def test_leaving_with_a_refused_fit_says_why(bare):
    relay_into(bare)
    bare._save_state = lambda: None
    node = Node("id", "phone")
    node.connected = True
    node.model = SimpleNamespace(estimate=lambda: _est(skew_saturated=True, skew_ppm=500.0))
    asyncio.run(bare._node_left(node))
    assert [e["kind"] for e in bare.events] == ["leave", "drift-refused"]
    assert "saturated" in bare.events[-1]["text"]


def test_leaving_with_nothing_to_bank_is_just_a_leave(bare):
    relay_into(bare)
    bare._save_state = lambda: None
    node = Node("id", "n")
    node.connected = True
    asyncio.run(bare._node_left(node))
    assert [e["kind"] for e in bare.events] == ["leave"]


# --- the quieter ones ----------------------------------------------------------


def test_a_boost_change_is_reported_once_per_quarter_step(bare):
    n = Node("id", "n")
    assert bare._note_boost(n, 1.0) is None
    assert bare._note_boost(n, 1.1) is None, "4.4 rounds to 4: same quarter-step"
    assert bare._note_boost(n, 1.4) == 1.1, "5.6 rounds to 6: moved, and says from what"
    assert n.ping_boost == 1.4


def test_the_first_mesh_sample_for_a_pair_is_an_event_and_the_rest_are_not(bare):
    relay_into(bare)
    a, b = Node("a-id", "laptop"), Node("b-id", "tablet")
    bare.nodes = {a.client_id: a, b.client_id: b}
    s = {"type": "meshSample", "peer": "b-id", "t0": 0.0, "c1": 1.0, "c2": 1.0, "t3": 2.0}
    asyncio.run(bare._on_player_msg(a, s, now()))
    asyncio.run(bare._on_player_msg(a, s, now()))
    assert [e["kind"] for e in bare.events] == ["mesh-up"]
    ev = bare.events[0]
    assert ev["level"] == "debug" and ev["peer"] == "b-id" and "tablet" in ev["text"]


def test_a_reaped_pair_is_an_event_and_comes_back_as_one(bare):
    relay_into(bare)
    a, b = Node("a-id", "laptop"), Node("b-id", "tablet")
    bare.nodes = {a.client_id: a, b.client_id: b}
    s = {"type": "meshSample", "peer": "b-id", "t0": 0.0, "c1": 1.0, "c2": 1.0, "t3": 2.0}
    asyncio.run(bare._on_player_msg(a, s, now()))
    bare.mesh_seen[("a-id", "b-id")] = now() - 100.0  # went quiet
    asyncio.run(bare._reap_mesh())
    assert ("a-id", "b-id") not in bare.mesh_pairs
    asyncio.run(bare._on_player_msg(a, s, now()))
    assert [e["kind"] for e in bare.events] == ["mesh-up", "mesh-lost", "mesh-up"]
    assert "laptop <-> tablet" in bare.events[1]["text"]


def test_a_bad_mesh_sample_is_dropped_without_an_event(bare):
    relay_into(bare)
    a, b = Node("a-id", "laptop"), Node("b-id", "tablet")
    bare.nodes = {a.client_id: a, b.client_id: b}
    asyncio.run(bare._on_player_msg(
        a, {"type": "meshSample", "peer": "b-id", "t0": "x"}, now()))
    assert not bare.events and ("a-id", "b-id") not in bare.mesh_seen


# --- the transport leaves a trail --------------------------------------------


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
    import syncplay.conductor as C

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
        feed(n.model, n=16, span=60.0)

    async def send(payload):
        pass

    n.send = send
    cond.nodes[n.client_id] = n
    return n


def test_play_pause_and_stop_each_leave_a_line(quick):
    track = quick.tracks[0]
    _node(quick, "laptop", loaded=track.id)

    async def run():
        await quick._transport_play(track)
        quick._advance_task.cancel()
        await quick._transport_pause()
        await quick._transport_stop()

    asyncio.run(run())
    assert [e["kind"] for e in quick.events] == ["play", "pause", "stop"]
    play = quick.events[0]
    assert play["nodes"] == ["laptop"] and play["track"] == track.id
    assert play["seekMs"] == 0.0 and "laptop" in play["text"]
    assert quick.events[1]["track"] == track.id
    assert track.title in quick.events[2]["text"]


def test_a_stop_with_nothing_up_is_not_an_event(quick):
    asyncio.run(quick._transport_stop())
    assert not quick.events


def test_a_cold_start_announces_the_countdown_and_a_dead_one_warns(quick):
    track = quick.tracks[0]
    _node(quick, "laptop", loaded=None)  # has to load, and never does

    async def run():
        await quick._transport_play(track)

    asyncio.run(run())
    assert [e["kind"] for e in quick.events] == ["arm", "noload"]
    arm, noload = quick.events
    assert arm["nodes"] == ["laptop"] and arm["secondsLeft"] > 0
    assert noload["level"] == "warning" and quick.playing is None


def test_a_deferred_node_is_named_and_a_start_nobody_can_time_warns(quick):
    track = quick.tracks[0]
    _node(quick, "laptop", loaded=track.id, timed=True)
    _node(quick, "phone", loaded=track.id, timed=False)  # loaded, no clock

    async def run():
        await quick._transport_play(track)
        quick._advance_task.cancel()
        quick.nodes["id-phone"].catchup_task.cancel()

    asyncio.run(run())
    kinds = [e["kind"] for e in quick.events]
    assert kinds == ["defer", "play"]
    assert quick.events[0]["nodes"] == ["phone"]
    assert quick.events[1]["nodes"] == ["laptop"]
