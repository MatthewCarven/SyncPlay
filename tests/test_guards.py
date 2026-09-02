"""Two failures that were invisible, and one that could eat a calibration rep.

Neither is a new capability; both are about a wrong outcome having a *signal*.
A transport that quietly starts nobody and a probe that silently loses a rep
are the same class of bug — the system knows something went wrong and doesn't
say so.
"""

import asyncio
import logging
import wave

import pytest

from syncplay.conductor import Conductor, Node, PingSample, now


@pytest.fixture()
def lib(tmp_path):
    with wave.open(str(tmp_path / "a.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * 800)
    return tmp_path


@pytest.fixture()
def c(lib, monkeypatch):
    """A conductor that records its toasts, with the load gate shrunk to a blink.

    The gate durations are real seconds in production; the tests below care only
    about which branch is taken at the end of the wait.
    """
    from syncplay import conductor as C

    monkeypatch.setattr(C, "LOAD_GATE_TIMEOUT", 0.05)
    monkeypatch.setattr(C, "LOAD_GATE_COLD", 0.05)
    monkeypatch.setattr(C, "ARM_SECONDS", 0.02)
    cond = Conductor(lib)
    cond.toasts = []

    async def stub_toast(text):
        cond.toasts.append(text)

    cond.toast = stub_toast
    return cond


def play(cond, track):
    """Run one transport play to completion, without leaving a task pending."""

    async def main():
        await cond._transport_play(track)
        if cond._advance_task:
            cond._advance_task.cancel()

    asyncio.run(main())


def node(cond, name, *, loaded=None, timed=True, sent=None):
    """A connected node, optionally with a clock estimate and a decoded track."""
    n = Node(f"id-{name}", name)
    n.connected = True
    if loaded:
        n.loaded.add(loaded)
    if timed:
        t = now()
        for i in range(12):
            ts = t - 60.0 + i * 5.0
            n.model.add(PingSample(t0=ts, c1=ts + 0.5, c2=ts + 0.5, t3=ts))
    async def send(payload, box=sent):
        if box is not None:
            box.append(payload)

    n.send = send
    cond.nodes[n.client_id] = n
    return n


# --- a start that reaches nobody must say so --------------------------------


def test_a_play_that_times_nobody_toasts_instead_of_only_logging(c):
    """The node has the track decoded but no clock estimate, so `_send_play`
    refuses it. Before this, the page showed nothing at all."""
    track = c.tracks[0]
    node(c, "phone", loaded=track.id, timed=False)   # loaded, but untimed

    play(c, track)

    assert c.toasts, "silent failure: nothing was said to the operator"
    assert "no node has a clock" in c.toasts[-1]
    assert track.title in c.toasts[-1]


def test_a_normal_start_says_nothing_extra(c):
    """The guard must not toast on the happy path."""
    track = c.tracks[0]
    node(c, "phone", loaded=track.id, timed=True, sent=[])

    play(c, track)

    assert c.playing is not None
    assert not any("has a clock" in t for t in c.toasts)


def test_the_no_load_path_still_owns_its_own_message(c):
    """Two different failures, two different messages — not one blaming the other."""
    track = c.tracks[0]
    node(c, "phone", loaded=None, timed=True)        # timed, but never loaded

    play(c, track)

    assert "No node managed to load" in c.toasts[-1]
    assert c.playing is None


# --- one measurement at a time ----------------------------------------------


def test_a_manual_probe_is_refused_during_a_sweep(c):
    """`_measure_pending` holds one probe. A 📏 mid-sweep used to overwrite the
    sweep's, which then timed out and was dropped — a silently lost rep."""
    mic = node(c, "mic")
    mic.mic = True
    node(c, "spk")
    c._calibrating = True
    c._measure_pending = {"seq": 7, "spk": "id-spk", "mic": "id-mic"}

    asyncio.run(c._measure_one("id-spk"))

    assert c._measure_pending["seq"] == 7          # the sweep's probe survives
    assert "sweep is running" in c.toasts[-1]


def test_a_second_manual_probe_is_refused_too(c):
    """Same collision, no sweep involved: two rapid 📏 clicks."""
    mic = node(c, "mic")
    mic.mic = True
    node(c, "spk")
    c._measure_pending = {"seq": 3, "spk": "id-spk", "mic": "id-mic"}

    asyncio.run(c._measure_one("id-spk"))

    assert c._measure_pending["seq"] == 3
    assert "already in flight" in c.toasts[-1]


def test_a_sweep_is_refused_while_a_manual_probe_is_in_flight(c):
    """The mirror image — otherwise the sweep loses its own first rep."""
    mic = node(c, "mic")
    mic.mic = True
    node(c, "spk")
    c._measure_pending = {"seq": 5, "spk": "id-spk", "mic": "id-mic"}

    asyncio.run(c._measure_all())

    assert c._calibrating is False                 # never started
    assert c._measure_pending["seq"] == 5
    assert "in flight" in c.toasts[-1]


def test_a_probe_still_runs_when_nothing_is_in_flight(c):
    """The guards must not lock out the ordinary case."""
    mic = node(c, "mic", sent=[])
    mic.mic = True
    node(c, "spk", sent=[])

    async def run():
        task = asyncio.create_task(c._measure_one("id-spk"))
        await asyncio.sleep(0)          # let it reach the arm/emit
        await asyncio.sleep(0)
        pending = c._measure_pending
        task.cancel()
        return pending

    assert asyncio.run(run()) is not None


# --- a refused start must be heard -------------------------------------------


def test_a_refused_start_is_logged_and_toasted(caplog):
    """`startSource` used to call `stopCurrent()` — which nulls `current` — and
    only then bail on `seekS >= buf.duration`. That left the node silent with
    `onended` already detached, so it never sent `state`, the conductor went on
    steering a node that had stopped, and `onSteer`'s tail dereferenced the null
    and threw. Reachable at end-of-track, where the re-anchor seek is largest.

    The player now refuses before tearing anything down and says so. This pins
    the saying-so, because the silent version is what cost a node a whole track.
    """
    cond = Conductor.__new__(Conductor)
    cond.control_sockets = set()
    sent = []

    async def relay(payload):
        sent.append(payload)

    cond._broadcast_control = relay
    node = Node("id-tablet", "Mums-Tablet")
    with caplog.at_level(logging.WARNING):
        asyncio.run(Conductor._on_player_msg(cond, node, {
            "type": "startRefused", "trackId": "abc123",
            "seekMs": 181_400.0, "durationMs": 181_000.0,
        }, now()))

    assert any(t.get("type") == "toast" for t in sent)
    assert "Mums-Tablet" in caplog.text
    assert "181.40s" in caplog.text and "181.00s" in caplog.text


def test_a_refused_start_survives_rubbish_numbers():
    """Client data. A refusal must still be reported even if its own figures
    are unusable — the report is the point, the numbers are decoration."""
    cond = Conductor.__new__(Conductor)
    cond.control_sockets = set()
    sent = []

    async def relay(payload):
        sent.append(payload)

    cond._broadcast_control = relay
    node = Node("id", "n")
    asyncio.run(Conductor._on_player_msg(cond, node, {
        "type": "startRefused", "trackId": None,
        "seekMs": "banana", "durationMs": float("nan"),
    }, now()))
    assert any(t.get("type") == "toast" for t in sent)
