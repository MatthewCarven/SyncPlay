"""Adaptive ping cadence: pay for evidence, not for packets.

The clock model runs on the samples that survive the RTT filter, not on the
ones we sent. A wired node keeps ~100% of its exchanges; a tablet whose Wi-Fi
radio parks between beacons keeps ~5%. Identical traffic, a twentieth of the
evidence. These pin the correction and — more importantly — the limits of it.

Fixtures marked "observed" are real numbers off the control page on 2026-07-28,
four nodes mid-song, so the tuning is anchored to a room that exists.
"""

import math

import pytest

from syncplay.conductor import (
    PING_BOOST_MAX,
    PING_INTERVAL_MIN,
    PING_JUDGE_MIN,
    boost_count,
    boost_interval,
    ping_boost,
)


# --- ping_boost against the fleet we actually have -------------------------


def test_wired_node_earns_nothing():
    """observed: Laptop1, 723/726 — already keeping essentially everything."""
    assert ping_boost(723, 726) == pytest.approx(1.0, abs=0.01)


def test_good_wifi_earns_a_little():
    """observed: pc, 545/726 — a mild top-up, not a rescue."""
    boost = ping_boost(545, 726)
    assert 1.2 < boost < 1.5


@pytest.mark.parametrize(
    "n_used,n_samples,label",
    [(84, 729, "phone"), (39, 726, "tablet")],
)
def test_struggling_wifi_hits_the_ceiling(n_used, n_samples, label):
    """observed: both keep <15%, both want far more than we're willing to send."""
    assert n_samples / n_used > PING_BOOST_MAX  # uncapped, they'd ask for 9x and 19x
    assert ping_boost(n_used, n_samples) == PING_BOOST_MAX


# --- the limits, which are the point --------------------------------------


def test_boost_never_exceeds_the_ceiling():
    """You cannot ping a bad link into being a good one."""
    assert ping_boost(1, 100_000) == PING_BOOST_MAX


def test_a_node_keeping_nothing_is_boosted_but_not_infinitely():
    assert ping_boost(0, 500) == PING_BOOST_MAX


def test_boost_never_goes_below_one():
    """No node ever gets *less* than baseline, however good its link."""
    assert ping_boost(726, 726) == 1.0


def test_young_node_is_left_alone():
    """A bad first second must not lock in a permanent boost."""
    assert ping_boost(1, PING_JUDGE_MIN - 1) == 1.0


def test_the_guard_lifts_once_there_is_evidence():
    assert ping_boost(1, PING_JUDGE_MIN) == PING_BOOST_MAX


def test_boost_is_monotonic_in_survival():
    """Worse survival must never earn less traffic."""
    boosts = [ping_boost(used, 1000) for used in (1000, 800, 500, 250, 100, 10)]
    assert boosts == sorted(boosts)


# --- splitting the boost between depth and frequency -----------------------


@pytest.mark.parametrize("boost", [1.0, 1.33, 2.0, 3.0, PING_BOOST_MAX])
def test_the_split_conserves_the_boost(boost):
    """count x rate must come to `boost` — that's what makes it a split."""
    count, interval = 10, 5.0
    got = (boost_count(count, boost) / count) * (interval / boost_interval(interval, boost))
    assert got == pytest.approx(boost, rel=0.06)  # rounding on a small count


def test_the_split_is_even():
    """Neither half should run away with it: both scale as sqrt(boost)."""
    assert boost_count(10, 4.0) == 20            # 10 x 2
    assert boost_interval(5.0, 4.0) == pytest.approx(2.5)  # 5 / 2


def test_no_boost_changes_nothing():
    assert boost_count(3, 1.0) == 3
    assert boost_interval(5.0, 1.0) == 5.0


def test_a_burst_never_thins_below_one_ping():
    assert boost_count(1, PING_BOOST_MAX) >= 1


def test_the_loop_cannot_be_driven_into_a_spin():
    """Whatever the boost, the interval floor holds."""
    assert boost_interval(0.2, PING_BOOST_MAX) == PING_INTERVAL_MIN
    assert boost_interval(PING_INTERVAL_MIN, 999.0) == PING_INTERVAL_MIN


def test_worst_case_traffic_is_bounded():
    """The whole feature's cost ceiling, stated once so it can't drift.

    Playback baseline is 3 pings / 5 s. A maxed-out node gets 6 / 2.5 s — four
    times the rate, and still under three pings a second.
    """
    count, interval = boost_count(3, PING_BOOST_MAX), boost_interval(5.0, PING_BOOST_MAX)
    assert count == 6
    assert interval == pytest.approx(2.5)
    assert count / interval == pytest.approx(4 * (3 / 5.0), rel=0.01)
    assert count / interval < 3.0


def test_sqrt_relationship_holds_for_arbitrary_boosts():
    for boost in (1.5, 2.25, 3.7, 4.0):
        assert boost_interval(8.0, boost) == pytest.approx(8.0 / math.sqrt(boost))
