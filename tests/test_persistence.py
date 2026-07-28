"""What survives a reconnect, and what must not.

A node's offset dies with its page (performance.now() restarts), but its skew
is a property of the crystal and is carried forward to seed the next session.
That shortcut is only safe if two things hold: a prior can never launder itself
into a "measurement", and a corrupt state file degrades to a cold start rather
than to a confidently wrong one. Both are pinned here.
"""

import pytest

from syncplay.conductor import MAX_PERSISTED_SKEW, Node, _clean_skew
from syncplay.timesync import ClockModel, PingSample


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
