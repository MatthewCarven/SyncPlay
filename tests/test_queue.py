"""The play queue, exercised through the control-command surface.

No sockets, no audio: a Conductor over a tmp library with `dispatch()` stubbed,
so each assertion reads off exactly which track a transport decision picked.
The property that matters most is the last one — with an empty queue, every
path must behave exactly as it did before the queue existed.
"""

import asyncio
import wave

import pytest

from syncplay.conductor import Conductor, Playback, now


@pytest.fixture()
def lib(tmp_path):
    """Four silent one-tenth-second WAVs, scanned in name order: a, b, c, d."""
    for name in ("a", "b", "c", "d"):
        with wave.open(str(tmp_path / f"{name}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\0\0" * 800)
    return tmp_path


@pytest.fixture()
def c(lib):
    """A conductor whose transport decisions are recorded instead of run."""
    cond = Conductor(lib)
    cond.picked = []

    def stub_dispatch(coro):
        cond.picked.append(coro.cr_frame.f_locals["track"].title)
        coro.close()  # never actually schedule playback

    cond.dispatch = stub_dispatch
    cond.ids = {t.title: t.id for t in cond.tracks}
    return cond


def titles(cond):
    return [cond.tracks_by_id[q].title for q in cond.queue]


def send(cond, **cmd):
    asyncio.run(cond._on_control_cmd(cmd))


def test_library_scans_in_order(c):
    assert [t.title for t in c.tracks] == ["a", "b", "c", "d"]
    assert c.queue == []


def test_queue_appends_and_allows_duplicates(c):
    for title in ("c", "a", "c"):
        send(c, cmd="queue", trackId=c.ids[title])
    assert titles(c) == ["c", "a", "c"]


def test_reorder_and_remove_by_index(c):
    for title in ("a", "b", "c"):
        send(c, cmd="queue", trackId=c.ids[title])
    send(c, cmd="queueMove", index=0, delta=1)
    assert titles(c) == ["b", "a", "c"]
    send(c, cmd="unqueue", index=1)
    assert titles(c) == ["b", "c"]


@pytest.mark.parametrize(
    "bad",
    [
        {"cmd": "queueMove", "index": 0, "delta": -1},   # off the top
        {"cmd": "queueMove", "index": 1, "delta": 1},    # off the end
        {"cmd": "queueMove", "index": 0, "delta": "x"},  # not a number
        {"cmd": "unqueue", "index": 99},                 # out of range
        {"cmd": "unqueue", "index": -1},                 # no negative indexing
        {"cmd": "unqueue"},                              # missing field
        {"cmd": "queue", "trackId": "no-such-track"},
    ],
)
def test_malformed_edits_are_noops(c, bad):
    send(c, cmd="queue", trackId=c.ids["a"])
    send(c, cmd="queue", trackId=c.ids["b"])
    send(c, **bad)
    assert titles(c) == ["a", "b"]


def test_peek_prefers_queue_and_does_not_consume(c):
    send(c, cmd="queue", trackId=c.ids["d"])
    a = c.tracks_by_id[c.ids["a"]]
    assert c._peek_next(a).title == "d"      # queue beats folder order
    assert c._peek_next(a).title == "d"      # ...and asking didn't change it
    assert len(c.queue) == 1
    assert c.snapshot()["nextUp"] == c.ids["d"]
    assert [q["title"] for q in c.snapshot()["queue"]] == ["d"]


def test_next_consumes_the_queue_then_falls_back_to_folder_order(c):
    for title in ("a", "c"):
        send(c, cmd="queue", trackId=c.ids[title])
    c.playing = Playback(track=c.tracks_by_id[c.ids["b"]], t_start=now(), seek_ms=0.0)

    send(c, cmd="next")
    assert c.picked[-1] == "a" and titles(c) == ["c"]
    send(c, cmd="next")
    assert c.picked[-1] == "c" and c.queue == []
    send(c, cmd="next")
    assert c.picked[-1] == "c"  # b -> c, the plain folder walk


def test_explicit_play_is_an_override_and_spares_the_queue(c):
    send(c, cmd="queue", trackId=c.ids["d"])
    send(c, cmd="play", trackId=c.ids["a"])
    assert c.picked[-1] == "a"
    assert titles(c) == ["d"]


def test_bare_play_starts_and_consumes_the_queue(c):
    send(c, cmd="queue", trackId=c.ids["d"])
    send(c, cmd="play")
    assert c.picked[-1] == "d"
    assert c.queue == []
    send(c, cmd="play")           # empty queue -> top of the library
    assert c.picked[-1] == "a"


def test_rescan_prunes_retired_tracks(c, lib):
    send(c, cmd="queue", trackId=c.ids["d"])
    (lib / "d.wav").unlink()
    send(c, cmd="rescan")
    assert c.queue == []


def test_clear_empties_the_queue(c):
    for _ in range(3):
        send(c, cmd="queue", trackId=c.ids["b"])
    send(c, cmd="queueClear")
    assert c.queue == []


def test_empty_queue_behaves_exactly_like_before_the_queue_existed(c):
    """The regression guard: with nothing queued, next-track resolution is the
    plain circular folder walk, and peek and take agree."""
    for i, t in enumerate(c.tracks):
        expected = c.tracks[(i + 1) % len(c.tracks)]
        assert c._peek_next(t).id == expected.id
        assert c._take_next(t).id == expected.id
        assert c.queue == []
