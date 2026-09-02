"""Whether a node's clock is good enough to time a start from.

Until 2026-08-27 the only bar was that an estimate *existed*. A freshly
reconnected tablet met that bar with a handful of samples, was committed to a
start at a track change, and spent the next minute at mean +19 ms with peaks at
+122 ms - not because its start was mistimed, but because the estimate behind
it was re-fitted out from under the servo with every ping. Before
`min_slope_span` a model's anchor is a moving median and its slope is an
assertion of zero.

`start_ready` is the rule, kept pure so it can be pinned without a fleet. The
bargain it makes is the load gate's own: the room never waits, the node is
deferred, and `_catchup` brings it in the moment the rule turns true. Joining a
beat late but in time beats joining at once and as an echo.

The one exception is the whole reason remembered skew exists: a returning node
whose window is de-trended by a credible prior has a clean median after a few
samples, and making it wait thirty seconds would throw that feature away.
"""

import asyncio
from types import SimpleNamespace

import pytest

from syncplay.conductor import (
    CATCHUP_WAIT_S,
    MIN_JOIN_SAMPLES,
    Conductor,
    Node,
    Playback,
    now,
    start_ready,
)
from syncplay.timesync import ClockModel, PingSample


def sample(t: float, offset: float = 1.0) -> PingSample:
    """A zero-delay exchange: rtt 0, so the offset is recovered exactly."""
    return PingSample(t0=t, c1=t + offset, c2=t + offset, t3=t)


def feed(model: ClockModel, n: int, span: float, start: float = 0.0) -> None:
    for i in range(n):
        model.add(sample(start + span * i / max(1, n - 1)))


# --- the rule ---------------------------------------------------------------


def test_no_estimate_is_not_ready():
    assert start_ready(ClockModel()) is False


def test_a_fitted_window_is_ready():
    m = ClockModel()
    feed(m, n=16, span=60.0)          # past min_slope_span, plenty of samples
    assert m.estimate().skew_fitted
    assert start_ready(m) is True


def test_a_young_model_with_no_prior_is_not_ready_however_many_samples():
    """The live failure. Twenty-five samples inside a few seconds is an anchor
    that moves on every ping and a slope of zero. Not a start to commit to."""
    m = ClockModel()
    feed(m, n=25, span=4.0)
    est = m.estimate()
    assert est is not None and not est.skew_fitted
    assert est.n_used >= MIN_JOIN_SAMPLES
    assert start_ready(m) is False


def test_a_returning_node_with_a_prior_is_ready_after_a_few_samples():
    """What remembered skew is for. The window is de-trended by the prior, so
    the median is clean long before the model could fit a slope of its own."""
    m = ClockModel(prior_skew=20e-6)
    feed(m, n=MIN_JOIN_SAMPLES, span=2.0)
    assert not m.estimate().skew_fitted
    assert start_ready(m) is True


def test_a_prior_alone_is_not_enough():
    m = ClockModel(prior_skew=20e-6)
    feed(m, n=MIN_JOIN_SAMPLES - 1, span=2.0)
    assert start_ready(m) is False


def test_a_forgotten_prior_puts_the_node_back_on_the_slow_path():
    """Forgetting a remembered drift must also forget the shortcut it bought."""
    m = ClockModel(prior_skew=20e-6)
    feed(m, n=MIN_JOIN_SAMPLES, span=2.0)
    assert start_ready(m) is True
    m.forget_prior()
    assert start_ready(m) is False


# --- what the conductor does with it ----------------------------------------


def _playback(track_id="t1"):
    track = SimpleNamespace(id=track_id, title="song", duration_ms=180_000.0)
    return Playback(track=track, t_start=now(), seek_ms=0.0)


def test_send_play_refuses_a_young_model():
    """`Node.send` no-ops without a socket, so the boolean is the whole story."""
    cond = Conductor.__new__(Conductor)
    node = Node("id", "young")
    feed(node.model, n=25, span=4.0)
    assert asyncio.run(Conductor._send_play(cond, node, _playback())) is False


def test_send_play_accepts_a_fitted_model():
    cond = Conductor.__new__(Conductor)
    node = Node("id", "settled")
    node.connected = True
    feed(node.model, n=16, span=60.0)
    assert asyncio.run(Conductor._send_play(cond, node, _playback())) is True


def test_catchup_waits_for_the_clock_and_then_joins():
    """A deferred node is not a dropped node. Once its model fits, catch-up
    sends play - and only once."""
    cond = Conductor.__new__(Conductor)
    cond.playing = _playback()
    node = Node("id", "late")
    node.connected = True
    feed(node.model, n=25, span=4.0)          # young: not ready yet
    sent = []

    async def fake_send_play(n, p):
        sent.append(p.seek_ms)
        return True

    cond._send_play = fake_send_play

    async def run():
        task = asyncio.create_task(Conductor._catchup(cond, node, "t1"))
        await asyncio.sleep(0.5)
        assert sent == [], "must not join while the model is young"
        feed(node.model, n=16, span=60.0, start=10.0)   # now it fits
        await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(run())
    assert len(sent) == 1


def test_catchup_gives_up_only_after_the_full_wait(monkeypatch):
    """The old 5 s bound would have expired before a young model could ever
    fit. The wait has to cover min_slope_span, and it has to *end*."""
    assert CATCHUP_WAIT_S > 30.0
    cond = Conductor.__new__(Conductor)
    cond.playing = _playback()
    node = Node("id", "never")
    node.connected = True
    feed(node.model, n=25, span=4.0)
    calls = []

    async def fake_send_play(n, p):
        calls.append(1)
        return True

    cond._send_play = fake_send_play
    import syncplay.conductor as C
    t = [now()]
    monkeypatch.setattr(C, "now", lambda: t[0])

    async def run():
        task = asyncio.create_task(Conductor._catchup(cond, node, "t1"))
        await asyncio.sleep(0.3)
        t[0] += CATCHUP_WAIT_S + 1.0            # the wait elapses; model never fit
        await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(run())
    assert calls == []


def test_one_catchup_per_node_at_a_time():
    """A node can be deferred at a start and then report `loaded`; a node can
    report `loaded` twice. Two concurrent catch-ups would both send play."""
    cond = Conductor.__new__(Conductor)
    cond.playing = _playback()
    node = Node("id", "twice")
    node.connected = True
    feed(node.model, n=25, span=4.0)

    async def run():
        Conductor._dispatch_catchup(cond, node, "t1")
        first = node.catchup_task
        Conductor._dispatch_catchup(cond, node, "t1")
        assert node.catchup_task is first, "a running catch-up is not replaced"
        first.cancel()
        try:
            await first
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
