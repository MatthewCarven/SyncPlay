"""What survives a reconnect, and what must not.

A node's offset dies with its page (performance.now() restarts), but its skew
is a property of the crystal and is carried forward to seed the next session.
That shortcut is only safe if two things hold: a prior can never launder itself
into a "measurement", and a corrupt state file degrades to a cold start rather
than to a confidently wrong one. Both are pinned here.
"""

import pytest

from syncplay.conductor import MAX_PERSISTED_SKEW, Node, _clean_skew
from syncplay.timesync import ClockModel, PingSample, filter_best


def sample(t: float, offset: float) -> PingSample:
    """A zero-delay exchange: rtt 0, so the offset is recovered exactly."""
    return PingSample(t0=t, c1=t + offset, c2=t + offset, t3=t)


def feed(model: ClockModel, skew: float, start: float, span: float, n: int) -> None:
    """n evenly spaced samples along a clean line of the given drift."""
    for i in range(n):
        t = start + span * i / max(1, n - 1)
        model.add(sample(t, 1.0 + skew * (t - start)))


# --- Node.remember_skew ----------------------------------------------------


def test_remember_skew_refuses_to_bank_an_inherited_prior():
    node = Node("id-tablet", "tablet")
    node.prior_skew = 40e-6
    node.model = ClockModel(prior_skew=node.prior_skew)
    feed(node.model, skew=200e-6, start=0.0, span=1.0, n=16)  # span far too short

    est = node.model.estimate()
    assert est is not None and not est.skew_fitted
    assert node.remember_skew() is False
    assert node.prior_skew == 40e-6  # untouched: nothing new was learned


def test_remember_skew_banks_a_real_fit():
    node = Node("id-tablet", "tablet")
    node.model = ClockModel(prior_skew=None)
    feed(node.model, skew=20e-6, start=0.0, span=120.0, n=32)

    assert node.remember_skew() is True
    assert node.prior_skew == pytest.approx(20e-6, rel=1e-3)


def test_remember_skew_is_a_noop_with_no_samples():
    node = Node("id-fresh", "phone")
    assert node.remember_skew() is False
    assert node.prior_skew is None


def test_end_session_banks_the_fit():
    node = Node("id-tablet", "tablet")
    node.model = ClockModel()
    feed(node.model, skew=-35e-6, start=0.0, span=120.0, n=32)
    node.end_session()
    assert node.prior_skew == pytest.approx(-35e-6, rel=1e-3)


def test_a_wrong_prior_cannot_outlive_one_good_session():
    """The failure mode worth fearing: a bad skew echoing forever."""
    node = Node("id-tablet", "tablet")
    node.prior_skew = 400e-6  # nonsense inherited from somewhere

    # Session 1: short, learns nothing. The nonsense must not be re-banked.
    node.model = ClockModel(prior_skew=node.prior_skew)
    feed(node.model, skew=20e-6, start=0.0, span=1.0, n=16)
    node.end_session()
    assert node.prior_skew == 400e-6

    # Session 2: long enough to fit. The nonsense is replaced by the truth.
    node.model = ClockModel(prior_skew=node.prior_skew)
    feed(node.model, skew=20e-6, start=0.0, span=120.0, n=32)
    node.end_session()
    assert node.prior_skew == pytest.approx(20e-6, rel=1e-3)


# --- _clean_skew: the state file is untrusted input -------------------------


@pytest.mark.parametrize("good", [0.0, 20e-6, -20e-6, MAX_PERSISTED_SKEW])
def test_clean_skew_accepts_plausible_crystals(good):
    assert _clean_skew(good) == pytest.approx(good)


@pytest.mark.parametrize(
    "junk",
    [None, "", "abc", [], {}, float("nan"), float("inf"), -float("inf"), 9.9, -9.9],
)
def test_clean_skew_rejects_junk(junk):
    assert _clean_skew(junk) is None


def test_clean_skew_accepts_numeric_strings():
    assert _clean_skew("2e-05") == pytest.approx(20e-6)


def test_clock_model_clamps_rather_than_trusting_a_wild_prior():
    assert ClockModel(max_skew=500e-6, prior_skew=1.0).prior_skew == pytest.approx(
        500e-6
    )
    assert ClockModel(max_skew=500e-6, prior_skew=-1.0).prior_skew == pytest.approx(
        -500e-6
    )
    assert ClockModel(prior_skew=None).prior_skew is None


# --- the fit-quality gate: what a slope has to prove before it is banked -----


def leg_sample(t: float, offset: float, d_out: float, d_ret: float) -> PingSample:
    """An exchange with the two path legs set separately.

    The model will recover `offset + (d_out - d_ret)/2` — path asymmetry *is*
    the measurement error, so this is how an impostor drift gets built.
    """
    c1 = t + d_out + offset
    return PingSample(t0=t, c1=c1, c2=c1, t3=t + d_out + d_ret)


def r_squared(model: ClockModel) -> float:
    """How well a straight line describes this window — the check we are NOT using."""
    used = filter_best(list(model._samples), model.base_tolerance, model.rel_tolerance)
    ts = [s.midpoint for s in used]
    ys = [s.offset for s in used]
    n = len(ts)
    t_mean, y_mean = sum(ts) / n, sum(ys) / n
    var = sum((t - t_mean) ** 2 for t in ts)
    cov = sum((t - t_mean) * (y - y_mean) for t, y in zip(ts, ys))
    slope = cov / var
    ss_res = sum((y - (y_mean + slope * (t - t_mean))) ** 2 for t, y in zip(ts, ys))
    ss_tot = sum((y - y_mean) ** 2 for y in ys)
    return 1.0 if ss_tot == 0 else 1.0 - ss_res / ss_tot


def carried_across_the_room(span: float, n: int = 31, rtt: float = 0.005,
                            excursion: float = 0.005) -> ClockModel:
    """A node whose clock is perfect and whose *path* is what changes.

    Someone picks it up and walks. Round trip stays flat enough to sail through
    the RTT filter, but the asymmetry sweeps across its full range, and the
    model reads that as a clean, straight, entirely fictional drift.
    """
    m = ClockModel()
    for i in range(n):
        t = span * i / (n - 1)
        d_out = (rtt - excursion) / 2 + excursion * i / (n - 1)
        m.add(leg_sample(t, 1.0, d_out, rtt - d_out))
    return m


def test_a_walk_across_the_room_looks_exactly_like_a_crystal():
    """The premise. If this fails, the gate below is solving nothing."""
    node = Node("id-phone", "phone")
    node.model = carried_across_the_room(span=60.0)
    est = node.model.estimate()

    assert est.skew_fitted
    assert 15e-6 < abs(est.skew) < 100e-6      # squarely in real-crystal territory
    assert r_squared(node.model) > 0.999       # and a textbook straight line
    assert node.prior_skew is None             # its clock never drifted at all


def test_the_walk_is_refused_because_it_cannot_clear_its_own_bound():
    """R^2 says "perfect fit". The bound says "asymmetry could have done all of
    that on its own" — and only the second one is a reason to believe it."""
    node = Node("id-phone", "phone")
    node.model = carried_across_the_room(span=60.0)
    est = node.model.estimate()

    assert abs(est.skew) < 2.0 * est.skew_bound    # inside the noise it admits
    assert node.remember_skew() is False
    assert node.prior_skew is None                 # nothing banked, nothing inherited


def test_the_walk_cannot_win_by_lasting_longer():
    """A longer window tightens the bound, but asymmetry is capped by the round
    trip — so the fiction it can support shrinks at exactly the same rate."""
    for span in (60.0, 300.0, 600.0):
        node = Node("id-phone", "phone")
        node.model = carried_across_the_room(span=span)
        assert node.remember_skew() is False, f"banked a walk over {span}s"


def test_a_real_crystal_on_a_real_network_is_still_banked():
    """The gate has to let the thing it exists to protect through.

    20 ppm — an ordinary tablet — measured over ten minutes on a 1 ms link.
    """
    node = Node("id-tablet", "tablet")
    node.model = ClockModel()
    for i in range(41):
        t = 600.0 * i / 40
        node.model.add(leg_sample(t, 1.0 + 20e-6 * t, 0.0005, 0.0005))

    est = node.model.estimate()
    assert abs(est.skew) > 2.0 * est.skew_bound
    assert node.remember_skew() is True
    assert node.prior_skew == pytest.approx(20e-6, rel=1e-2)


def test_a_refused_fit_leaves_the_previous_value_alone():
    """Refusing is "learned nothing", not "forget what you knew"."""
    node = Node("id-phone", "phone")
    node.prior_skew = 18e-6            # banked properly in some earlier session
    node.model = carried_across_the_room(span=60.0)
    node.end_session()
    assert node.prior_skew == pytest.approx(18e-6)


def test_the_snapshot_says_whether_the_drift_is_believable():
    """The control page dims a drift it isn't going to bank; pin the flag."""
    walker = Node("id-phone", "phone")
    walker.model = carried_across_the_room(span=60.0)
    d = walker.stats(None)
    assert d["skewCredible"] is False
    assert d["skewBoundPpm"] > abs(d["skewPpm"])

    steady = Node("id-tablet", "tablet")
    steady.model = ClockModel()
    for i in range(41):
        t = 600.0 * i / 40
        steady.model.add(leg_sample(t, 1.0 + 20e-6 * t, 0.0005, 0.0005))
    assert steady.stats(None)["skewCredible"] is True
