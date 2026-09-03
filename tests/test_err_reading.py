"""`err ms` — can it be read at all, and what is it a reading *of*?

Two questions, and the first one is the cheap one that has to be asked first.

**Is the number alive?** `sync_err_ms` is only ever overwritten by a fresh
steerAck or cleared on stop, so a node whose ack stream dies mid-song goes on
displaying its last reading indefinitely. A frozen number and a beautifully
settled one look identical, which means any conclusion drawn from a steady
`err ms` is worthless until staleness can be excluded. Hence `errAgeS`.

**What is it a reading of?** The servo is proportional-only — player.js assigns
`rate = 1 - errS/STEER_HORIZON_S` from the instantaneous error, never
accumulates — so it has a standing error against a constant-rate disturbance.
A settled `err ms` is therefore not a fault report but a measurement: 15 s times
whatever rate playback is slipping at, and the node's own rate trim carries that
rate directly. Subtract the drift the conductor already steers for and the
remainder is the node's audio clock against its own CPU clock, which nothing
else in this system measures.

The attribution is only meaningful once settled, and every source start resets
the trim to 1.0 — so the tests that matter most here are the ones where it must
refuse to answer.
"""

import asyncio

import pytest

from syncplay.conductor import (
    ERR_CLAMP_MS,
    ERR_MIN_ACKS,
    MAX_PERSISTED_SKEW,
    ERR_SETTLE_S,
    ERR_STALE_S,
    Conductor,
    Node,
    now,
)


@pytest.fixture()
def node():
    n = Node("id-tablet", "Id10terror tablet")
    n.playing_track = "t1"
    return n


class Est:
    """Just the one field the attribution reads."""

    def __init__(self, skew_ppm=0.0):
        self.skew_ppm = skew_ppm


def settled(node, err_ms=6.0, rate_ppm=-400.0, age=0.0, run=ERR_SETTLE_S + 15.0,
            acks=20, swing_ppm=0.0):
    """Put a node into a settled, freshly-acked state.

    `swing_ppm` makes the servo trim alternate about its mean by that much,
    which is what a node chasing a wobbling offset estimate actually does — the
    mean survives, the credibility does not.
    """
    node.sync_err_ms = err_ms
    node.play_rate = 1.0 + rate_ppm / 1e6
    node.err_seen_at = now() - age
    node.run_since = now() - run
    node.reset_err_stats()
    for i in range(acks):
        node.note_rate(1.0 + (rate_ppm + (swing_ppm if i % 2 else -swing_ppm)) / 1e6)
    return node


# --- is the number alive ----------------------------------------------------


def test_a_fresh_ack_is_not_stale(node):
    settled(node, age=0.0)
    d = node.stats("t1")
    assert d["errStale"] is False
    assert d["errAgeS"] == pytest.approx(0.0, abs=0.5)


def test_an_ack_older_than_the_grace_is_stale(node):
    settled(node, age=ERR_STALE_S + 1.0)
    d = node.stats("t1")
    assert d["errStale"] is True
    # The value itself is deliberately still there — the point is to mark it,
    # not to hide it. Hiding it would lose the evidence of what it froze at.
    assert d["syncErrMs"] == 6.0


def test_a_node_that_never_acked_is_not_reported_stale(node):
    """Absence is not expiry. A node mid-join has no reading, not an old one."""
    d = node.stats("t1")
    assert d["errAgeS"] is None
    assert d["errStale"] is False


def test_stopping_forgets_the_reading_entirely():
    """A stale number surviving a stop would reappear as fact on the next play."""
    cond = Conductor.__new__(Conductor)
    n = settled(Node("id", "n"))
    asyncio.run(Conductor._on_player_msg(cond, n, {"type": "state", "playing": None}, now()))
    assert n.sync_err_ms is None
    assert n.err_seen_at is None
    assert n.run_since is None


# --- what resets the servo --------------------------------------------------


def test_any_non_null_state_starts_a_new_run(bare):
    """The subtle one. player.js sends `state` from exactly one place —
    startSource — so a non-null report means a source just started at rate 1.0.
    A re-anchor restart, a seek and a mid-song catch-up all do that under the
    *same* trackId, so a run boundary keyed to a track change would miss all
    three and average a fresh 45 s climb into the settled figure."""
    cond = bare
    n = Node("id", "n")
    msg = {"type": "state", "playing": "same-track"}
    asyncio.run(Conductor._on_player_msg(cond, n, msg, now()))
    first = n.run_since
    assert first is not None
    n.run_since = now() - 300.0  # pretend it has been running a while
    asyncio.run(Conductor._on_player_msg(cond, n, msg, now()))  # identical trackId
    assert n.run_since > now() - 1.0, "a restart under the same trackId must reset the run"


# --- the attribution --------------------------------------------------------


def test_settled_error_is_split_into_known_drift_and_the_rest(node):
    """The live case: 6.0 ms of error, a -400 ppm standing trim, and 20.1 ppm of
    node-clock drift the conductor is already steering for. What is left is the
    number the whole investigation is about."""
    settled(node, err_ms=6.0, rate_ppm=-400.0)
    assert node._audio_clock_ppm(Est(20.1)) == pytest.approx(379.9, abs=0.1)


def test_a_node_whose_drift_explains_everything_has_nothing_left_over(node):
    settled(node, rate_ppm=-20.1)
    assert node._audio_clock_ppm(Est(20.1)) == pytest.approx(0.0, abs=1e-9)


def test_the_sign_convention_holds_both_ways(node):
    """A node playing *late* trims the other way, and the residual follows."""
    settled(node, err_ms=-1.0, rate_ppm=66.7)
    assert node._audio_clock_ppm(Est(8.1)) == pytest.approx(-74.8, abs=0.1)


# --- and, mostly, when it must refuse ---------------------------------------


def test_no_attribution_from_a_stale_reading(node):
    settled(node, age=ERR_STALE_S + 1.0)
    assert node._audio_clock_ppm(Est(20.1)) is None


def test_no_attribution_mid_climb(node):
    """The trim resets to 1.0 on every source start and takes ~3 time constants
    to re-emerge. Reading it at 5 s would report a transient as a crystal."""
    settled(node, run=5.0)
    assert node._audio_clock_ppm(Est(20.1)) is None


def test_no_attribution_at_the_settling_boundary(node):
    settled(node, run=ERR_SETTLE_S - 0.1)
    assert node._audio_clock_ppm(Est(20.1)) is None
    settled(node, run=ERR_SETTLE_S + 0.1)
    assert node._audio_clock_ppm(Est(20.1)) is not None


def test_no_attribution_without_an_estimate(node):
    settled(node)
    assert node._audio_clock_ppm(None) is None


def test_no_attribution_from_a_node_that_is_not_playing(node):
    settled(node)
    node.playing_track = None
    assert node._audio_clock_ppm(Est(20.1)) is None


# --- client data ------------------------------------------------------------


def test_a_nonsense_err_never_lands(bare):
    cond = bare
    n = Node("id", "n")
    for junk in ("banana", None, float("nan"), float("inf")):
        asyncio.run(Conductor._on_player_msg(
            cond, n, {"type": "steerAck", "errMs": junk, "rate": 1.0}, now()))
        assert n.sync_err_ms is None
        assert n.err_seen_at is None, "a rejected reading must not stamp freshness"


def test_an_outrageous_err_is_clamped_not_dropped(bare):
    """A re-anchor reports the error that caused it, and that sample is the most
    informative one there is. Bound it; do not filter it away."""
    cond = bare
    n = Node("id", "n")
    asyncio.run(Conductor._on_player_msg(
        cond, n, {"type": "steerAck", "errMs": 1e12, "rate": 1.0}, now()))
    assert n.sync_err_ms == ERR_CLAMP_MS
    assert n.err_seen_at is not None


# --- settled is not the same as steady -------------------------------------


def test_a_parked_node_is_credible(node):
    settled(node, rate_ppm=-400.0, swing_ppm=5.0)
    assert node.audio_clock_credible() is True
    assert node._audio_clock_ppm(Est(20.1)) == pytest.approx(379.9, abs=0.5)


def test_a_node_swinging_through_zero_is_not_credible(node):
    """The failure this whole thread was built on. Live readings from an
    Android 6 tablet: err swung -6.18 .. +7.23 ms while the laptop beside it
    held +/-0.07. Every single-sample reading of that node looked like a
    parked offset; it is a servo chasing a moving reference, and its mean is
    near zero. It must be refused however long it has been running."""
    settled(node, rate_ppm=0.0, swing_ppm=400.0)
    assert node.dist_sd_ppm > 300
    assert node.audio_clock_credible() is False


def test_a_swinging_node_still_reports_its_mean_and_spread(node):
    """Refused is not hidden. The spread is the evidence, so it must be
    readable — that is the whole difference between this and a silent None."""
    settled(node, rate_ppm=-30.0, swing_ppm=400.0)
    d = node.stats("t1")
    assert d["distSdPpm"] > 300
    assert d["distN"] == 20
    assert d["audioClockCredible"] is False
    # The mean itself still resolves once there is an estimate to subtract the
    # fitted slope from; `stats` reports None here only because this bare Node
    # has no samples in its model, which is a different absence entirely.
    assert d["audioClockPpm"] is None
    assert node._audio_clock_ppm(Est(0.0)) == pytest.approx(30.0, abs=0.5)


def test_too_few_acks_is_refused_however_settled(node):
    settled(node, acks=ERR_MIN_ACKS - 1)
    assert node._audio_clock_ppm(Est(20.1)) is None
    assert node.audio_clock_credible() is False


def test_a_new_run_forgets_the_previous_run_stats(bare):
    """A restart resets the trim to 1.0, so carrying the old spread across would
    describe a servo that no longer exists."""
    cond = bare
    n = settled(Node("id", "n"))
    assert n.dist_n == 20
    asyncio.run(Conductor._on_player_msg(
        cond, n, {"type": "state", "playing": "t1"}, now()))
    assert n.dist_n == 0 and n.dist_mean == 0.0


def test_the_mean_survives_a_swing_that_kills_credibility(node):
    """Mean and credibility are independent claims: a node can be swinging
    wildly and still have a mean worth reporting, which is why they are two
    fields and not one."""
    settled(node, rate_ppm=-100.0, swing_ppm=400.0)
    assert node._audio_clock_ppm(Est(0.0)) == pytest.approx(100.0, abs=0.5)
    assert node.audio_clock_credible() is False


def test_a_runaway_is_not_a_credible_audio_clock(node):
    """Live failure, 2026-08-27: a node that had just reconnected showed a mean
    error of +19 ms with peaks at +122 ms, implying 1285 ppm. It passed the SNR
    test comfortably, because a runaway is not a noisy signal — it is a
    confident wrong one, and its mean clears its own standard error easily.

    Two independent reasons it must be refused. No oscillator is 1285 ppm; and
    past 800 ppm the servo is out of trim authority entirely, so there is no
    equilibrium for `err` to settle to and the mean is the average of a
    diverging signal."""
    settled(node, rate_ppm=-1285.0, swing_ppm=20.0)
    assert node.audio_clock_credible() is False


def test_the_boundary_of_believable(node):
    settled(node, rate_ppm=-(MAX_PERSISTED_SKEW * 1e6 - 1), swing_ppm=5.0)
    assert node.audio_clock_credible() is True
    settled(node, rate_ppm=-(MAX_PERSISTED_SKEW * 1e6 + 1), swing_ppm=5.0)
    assert node.audio_clock_credible() is False
