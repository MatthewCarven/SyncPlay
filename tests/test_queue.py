"""The play queue, exercised through the control-command surface.

No sockets, no audio: a Conductor over a tmp library with `dispatch()` stubbed,
so each assertion reads off exactly which track a transport decision picked.
The property that matters most is the last one — with an empty queue, every
path must behave exactly as it did before the queue existed.
"""

import asyncio
import wave

import pytest

from syncplay.conductor import STOP_ID, Conductor, Playback, now


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
        # A play carries its track; a stop carries nothing, and is recorded as such.
        track = coro.cr_frame.f_locals.get("track")
        cond.picked.append(track.title if track is not None else "<stop>")
        coro.close()  # never actually schedule playback

    cond.dispatch = stub_dispatch
    cond.ids = {t.title: t.id for t in cond.tracks}
    return cond


def titles(cond):
    return [cond._queue_title(q) for q in cond.queue]


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


# --- the stop marker ---------------------------------------------------------
#
# A queue entry that isn't a track: reaching it halts playback, spends the
# marker, and leaves whatever follows at the head for ▶.


def playing(cond, title):
    track = cond.tracks_by_id[cond.ids[title]]
    track.duration_ms = 100.0  # normally the first node to decode it says; here nobody does
    cond.playing = Playback(track=track, t_start=now(), seek_ms=0.0)
    return cond.playing


def test_stop_marker_queues_like_a_track(c):
    send(c, cmd="queue", trackId=c.ids["b"])
    send(c, cmd="queue", trackId=STOP_ID)
    send(c, cmd="queue", trackId=c.ids["c"])
    assert titles(c) == ["b", "stop", "c"]
    snap = c.snapshot()["queue"]
    assert [q["title"] for q in snap] == ["b", "stop", "c"]
    assert snap[1] == {"id": STOP_ID, "title": "stop", "durationMs": None, "stop": True}
    assert "stop" not in snap[0] and "stop" not in snap[2]
    # Index-addressed edits see the marker as one more entry.
    send(c, cmd="queueMove", index=1, delta=1)
    assert titles(c) == ["b", "c", "stop"]
    send(c, cmd="unqueue", index=2)
    assert titles(c) == ["b", "c"]


def test_peek_sees_nothing_past_a_stop_marker(c):
    """Prefetch and the 'next up' marker must not look through the stop."""
    send(c, cmd="queue", trackId=STOP_ID)
    send(c, cmd="queue", trackId=c.ids["d"])
    a = playing(c, "a")
    assert c._peek_next(a.track) is None
    assert c.snapshot()["nextUp"] == STOP_ID
    assert titles(c) == ["stop", "d"]  # asking consumed nothing


def test_take_spends_the_marker_and_keeps_the_rest(c):
    send(c, cmd="queue", trackId=STOP_ID)
    send(c, cmd="queue", trackId=c.ids["d"])
    a = playing(c, "a")
    assert c._take_next(a.track) is None
    assert titles(c) == ["d"]
    assert c._take_next(a.track).title == "d"
    assert c.queue == []


def test_auto_advance_halts_at_the_marker_and_holds_the_queue(c):
    send(c, cmd="queue", trackId=STOP_ID)
    send(c, cmd="queue", trackId=c.ids["d"])
    p = playing(c, "a")
    p.t_start = now() - 60.0  # the track ended long ago: no sleeping
    asyncio.run(c._auto_advance(p))
    assert c.playing is None and c.picked == []  # nothing started
    assert titles(c) == ["d"]
    assert c.events[-1]["kind"] == "stop" and c.events[-1]["queued"] == 1
    # ...and ▶ from the standstill starts the rest of the queue.
    send(c, cmd="play")
    assert c.picked[-1] == "d" and c.queue == []


def test_auto_advance_without_a_marker_still_plays_on(c):
    send(c, cmd="queue", trackId=c.ids["d"])
    p = playing(c, "a")
    p.t_start = now() - 60.0
    asyncio.run(c._auto_advance(p))
    assert c.picked == ["d"] and c.queue == []


def test_next_into_a_marker_stops_early(c):
    send(c, cmd="queue", trackId=STOP_ID)
    send(c, cmd="queue", trackId=c.ids["d"])
    playing(c, "a")
    send(c, cmd="next")
    assert c.picked[-1] == "<stop>"
    assert titles(c) == ["d"]


def test_marker_at_a_standstill_is_already_satisfied(c):
    """Stopped is where we are: ▶/⏭ from idle look past a leading marker."""
    send(c, cmd="queue", trackId=STOP_ID)
    send(c, cmd="queue", trackId=c.ids["c"])
    assert c.snapshot()["nextUp"] == c.ids["c"]
    send(c, cmd="next")
    assert c.picked[-1] == "c" and c.queue == []
    send(c, cmd="queue", trackId=STOP_ID)
    send(c, cmd="play")  # a lone marker: spent, library top plays
    assert c.picked[-1] == "a" and c.queue == []


def test_marker_survives_a_rescan(c, lib):
    send(c, cmd="queue", trackId=c.ids["d"])
    send(c, cmd="queue", trackId=STOP_ID)
    (lib / "d.wav").unlink()
    send(c, cmd="rescan")
    assert titles(c) == ["stop"]
