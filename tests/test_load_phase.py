"""Download versus decode — telling the two halves of a wait apart.

A cold start is two waits wearing one face: bytes over the wire, then ~85 MB of
PCM out of `decodeAudioData`. Only the first has a percentage; the second is a
single opaque promise with no progress events to relay. So the conductor times
it instead, and the rule the control page depends on is that **decode outranks
download** — `loadPct` sits at 100 for the whole decode, so a reader that tests
it first reports "downloading" through the longer half of the wait.

What is really being pinned here is that the clock always stops. A frozen
"100%" is a small lie; a decode timer still counting on a node that gave up
four minutes ago is a bigger one, so every exit from the decode phase — done,
failed, retargeted, reconnected — gets its own test.
"""

import asyncio
import wave

import pytest

from syncplay.conductor import Conductor, Node, now


@pytest.fixture()
def lib(tmp_path):
    """Two silent one-tenth-second WAVs, scanned in name order: a, b."""
    for name in ("a", "b"):
        with wave.open(str(tmp_path / f"{name}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\0\0" * 800)
    return tmp_path


@pytest.fixture()
def c(lib):
    """A conductor already bringing up track "a", with the relay recorded."""
    cond = Conductor(lib)
    cond.ids = {t.title: t.id for t in cond.tracks}
    cond.target_track = cond.tracks_by_id[cond.ids["a"]]
    cond.relayed = []

    async def record(payload):
        cond.relayed.append(payload)

    cond._broadcast_control = record
    # Non-empty so the relay path is actually taken; nothing ever reads it.
    cond.control_sockets.add(object())
    return cond


@pytest.fixture()
def node():
    return Node("id-tablet", "tablet")


def deliver(cond, node, **data):
    asyncio.run(cond._on_player_msg(node, data, now()))


def progress(cond, node, pct, track="a"):
    deliver(cond, node, type="loadProgress", trackId=cond.ids[track], pct=pct)


# --- the phase boundary ----------------------------------------------------


def test_bytes_still_arriving_is_not_decoding(c, node):
    progress(c, node, 40)
    assert node.decode_since is None
    assert node.stats(None)["decodingS"] is None
    assert node.stats(None)["loadPct"] == 40


def test_the_last_byte_starts_the_decode_clock(c, node):
    progress(c, node, 100)
    assert node.decode_since is not None
    assert node.stats(None)["decodingS"] >= 0.0


def test_load_pct_still_reads_100_while_decoding(c, node):
    """The reason the control page must test decodingS *first*.

    loadPct is not cleared when the transfer ends — it stays at 100 until the
    node reports the track decoded. Anything reading loadPct before decodingS
    will call the whole decode "downloading, finished".
    """
    progress(c, node, 100)
    s = node.stats(None)
    assert s["loadPct"] == 100 and s["decodingS"] is not None


def test_repeat_100s_do_not_restart_the_clock(c, node):
    """Progress is throttled, not deduplicated — a second 100 can arrive."""
    progress(c, node, 100)
    started = node.decode_since
    progress(c, node, 100)
    assert node.decode_since == started


def test_elapsed_is_measured_from_the_stamp(c, node):
    node.decode_since = now() - 3.0
    assert node.stats(None)["decodingS"] == pytest.approx(3.0, abs=0.2)


# --- every way the clock must stop -----------------------------------------


def test_decoded_stops_the_clock(c, node):
    progress(c, node, 100)
    deliver(c, node, type="loaded", trackId=c.ids["a"], durationMs=100.0)
    assert node.decode_since is None
    assert node.stats(None)["decodingS"] is None


def test_a_failed_decode_stops_the_clock(c, node):
    """Otherwise the pill counts upward forever on a node that has given up —
    strictly worse than the frozen 100% this replaced."""
    progress(c, node, 100)
    deliver(c, node, type="loadError", trackId=c.ids["a"], error="EncodingError")
    assert node.decode_since is None


def test_retargeting_to_another_track_starts_clean(c, node):
    progress(c, node, 100)
    assert node.decode_since is not None
    c.target_track = c.tracks_by_id[c.ids["b"]]
    progress(c, node, 5, track="b")
    assert node.load_track == c.ids["b"]
    assert node.decode_since is None  # b's bytes are still arriving


def test_a_reconnect_starts_clean(c, node):
    progress(c, node, 100)
    node.begin_session(ws=None, ua="")
    assert node.decode_since is None
    assert node.stats(None)["decodingS"] is None


# --- a retry walks the phase backwards -------------------------------------


def test_progress_going_backwards_stops_the_decode_clock(c, node):
    """The node retried a failed load, so it is downloading again, not decoding.

    Progress only ever runs backwards on a retry — within one attempt the
    percentage is monotone. A timer left running across that would quietly
    count the retry as part of a decode that had already failed, and the pill
    would claim a decode that is not happening.
    """
    progress(c, node, 100)
    assert node.decode_since is not None
    progress(c, node, 5)
    assert node.decode_since is None
    assert node.stats(None)["decodingS"] is None
    assert node.stats(None)["loadPct"] == 5


def test_the_clock_restarts_when_the_retry_finishes_downloading(c, node):
    progress(c, node, 100)
    progress(c, node, 5)
    progress(c, node, 100)
    assert node.decode_since is not None


# --- eviction: the node is the only one who can tell us --------------------


def test_unloaded_retires_a_track_the_node_dropped(c, node):
    """`node.loaded` is the conductor's only view of what a node holds. A stale
    entry makes the load gate count the node ready and skip it, so it joins
    late through catch-up instead of starting with everyone."""
    deliver(c, node, type="loaded", trackId=c.ids["a"], durationMs=100.0)
    assert c.ids["a"] in node.loaded
    deliver(c, node, type="unloaded", trackId=c.ids["a"])
    assert c.ids["a"] not in node.loaded
    assert node.stats(c.ids["a"])["loadedCurrent"] is False


def test_unloaded_for_a_track_we_never_held_is_harmless(c, node):
    deliver(c, node, type="unloaded", trackId="never-seen")
    deliver(c, node, type="unloaded", trackId=c.ids["b"])
    assert node.loaded == set()


def test_unloaded_does_not_disturb_the_other_tracks(c, node):
    for name in ("a", "b"):
        deliver(c, node, type="loaded", trackId=c.ids[name], durationMs=100.0)
    deliver(c, node, type="unloaded", trackId=c.ids["a"])
    assert node.loaded == {c.ids["b"]}


# --- what must stay invisible ----------------------------------------------


def test_the_silent_prefetch_never_starts_a_clock(c, node):
    """While "a" is the track being brought up, the background prefetch of "b"
    must not touch the pill at all — that gate predates this and still holds."""
    progress(c, node, 100, track="b")
    assert node.decode_since is None
    assert node.load_pct is None


# --- the relay, which is what makes the flip instant -----------------------


def test_relay_carries_the_phase(c, node):
    progress(c, node, 40)
    assert c.relayed[-1]["decoding"] is False
    progress(c, node, 100)
    assert c.relayed[-1]["decoding"] is True
    assert c.relayed[-1]["pct"] == 100


def test_relay_says_done_when_the_decode_lands(c, node):
    progress(c, node, 100)
    deliver(c, node, type="loaded", trackId=c.ids["a"], durationMs=100.0)
    assert c.relayed[-1]["done"] is True
