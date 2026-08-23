"""The +/- column: is it a bound, or just a number that looks like one?

Every ping measures the offset with an error equal to half the path asymmetry,
and asymmetry cannot exceed the round trip -- so a sample of round trip `rtt`
certifies |offset error| <= rtt/2. These tests hold `ClockEstimate.trust_s` to
that claim: it must never be exceeded (that would make it a lie), and it must be
reachable (a bound nothing can approach is useless).
"""

import random

import pytest

from syncplay.timesync import ClockEstimate, ClockModel, PingSample


def sample(t0: float, offset: float, d_out: float, d_ret: float,
           proc: float = 0.0004) -> PingSample:
    """One exchange with the path split explicitly into its two legs.

    Round trip is d_out + d_ret; the offset error the model will make is
    (d_out - d_ret)/2, which is exactly what the bound is about.
    """
    c1 = t0 + d_out + offset
    c2 = c1 + proc
    t3 = t0 + d_out + proc + d_ret
    return PingSample(t0=t0, c1=c1, c2=c2, t3=t3)


def model_with(samples) -> ClockModel:
    m = ClockModel()
    for s in samples:
        m.add(s)
    return m


# --- the arithmetic ------------------------------------------------------


def test_trust_is_half_the_widest_surviving_round_trip():
    m = model_with([
        sample(0.0, 0.5, 0.002, 0.002),   # rtt 4 ms
        sample(1.0, 0.5, 0.0021, 0.0021),  # rtt 4.2 ms  <- worst admitted
        sample(2.0, 0.5, 0.001, 0.001),   # rtt 2 ms  <- best
    ])
    est = m.estimate()
    assert est.n_used == 3                       # all inside best + 2ms + 25%
    assert est.worst_rtt == pytest.approx(0.0042)
    assert est.trust_s == pytest.approx(0.0021)
    assert est.trust_ms == pytest.approx(2.1)
    assert est.floor_ms == pytest.approx(1.0)    # best_rtt/2, the unreachable floor


def test_trust_never_reports_the_flattering_number():
    """One tight sample must not license a tight bound for a wide window.

    Quoting best_rtt/2 would do exactly that -- and it is the reading a wide
    filter gate makes most wrong, which is the whole point of the column.
    """
    est = model_with([
        sample(0.0, 0.1, 0.0002, 0.0002),  # rtt 0.4 ms, gorgeous
        sample(1.0, 0.1, 0.0012, 0.0012),  # rtt 2.4 ms, admitted by the 2 ms floor
    ]).estimate()
    assert est.best_rtt == pytest.approx(0.0004)
    assert est.trust_ms == pytest.approx(1.2)   # not 0.2
    assert est.trust_ms > est.floor_ms


def test_a_rejected_sample_cannot_widen_the_bound():
    """The bound describes the fit, so only what survives the filter counts."""
    tight = [sample(i * 0.5, 0.25, 0.001, 0.001) for i in range(6)]  # rtt 2 ms
    est_clean = model_with(tight).estimate()
    est_spiked = model_with(tight + [sample(3.0, 0.25, 0.4, 0.4)]).estimate()
    assert est_spiked.n_used == est_clean.n_used            # the 800 ms spike is dropped
    assert est_spiked.trust_ms == pytest.approx(est_clean.trust_ms)


# --- the claim itself ----------------------------------------------------


def test_the_bound_holds_under_worst_case_asymmetry():
    """Every leg one-sided: the error should reach the bound and not pass it."""
    true_offset = 1.234
    est = model_with([
        sample(0.0, true_offset, 0.004, 0.0),   # rtt 4 ms, all outbound
        sample(1.0, true_offset, 0.003, 0.0),
        sample(2.0, true_offset, 0.0035, 0.0),
    ]).estimate()
    err = abs(est.offset_at(2.0) - true_offset)
    assert err <= est.trust_s + 1e-12           # the certificate holds
    assert err == pytest.approx(0.0035 / 2)     # median leg, i.e. genuinely tight


def test_bound_holds_across_randomized_asymmetry():
    """1000 windows of random splits -- the bound may never be exceeded.

    The estimate is a median (or a fit) over the survivors, so it lies between
    them and inherits the widest admitted sample's bound. If that reasoning were
    wrong, a random search of this size would find it.
    """
    rng = random.Random(1234)
    for trial in range(1000):
        true_offset = rng.uniform(-2.0, 2.0)
        samples = []
        for i in range(rng.randint(2, 20)):
            rtt = rng.uniform(0.0002, 0.02)
            d_out = rng.uniform(0.0, rtt)       # any split, including all-one-leg
            samples.append(sample(i * 0.25, true_offset, d_out, rtt - d_out))
        est = model_with(samples).estimate()
        err = abs(est.offset_at(samples[-1].midpoint) - true_offset)
        assert err <= est.trust_s + 1e-9, f"trial {trial}: {err} > {est.trust_s}"


def test_a_wider_filter_gate_shows_up_as_a_wider_bound():
    """Makes the parked `filter_best` re-tune measurable instead of arguable.

    Same samples, two tolerances: the loose one admits a spikier sample, and the
    column says so. Whatever that re-tune ends up doing, this is the number it
    has to justify itself against.
    """
    samples = [sample(i * 0.5, 0.3, 0.001 + 0.0004 * i, 0.001) for i in range(8)]
    tight = ClockModel(base_tolerance=0.0002, rel_tolerance=0.05)
    loose = ClockModel(base_tolerance=0.01, rel_tolerance=2.0)
    for m in (tight, loose):
        for s in samples:
            m.add(s)
    assert loose.estimate().n_used > tight.estimate().n_used
    assert loose.estimate().trust_ms > tight.estimate().trust_ms
    # ...and both quote the same floor, which is why the floor alone is no answer.
    assert loose.estimate().floor_ms == pytest.approx(tight.estimate().floor_ms)


# --- what a node with nothing to say reports -----------------------------


def test_no_samples_means_no_bound_rather_than_a_confident_zero():
    assert ClockModel().estimate() is None
    fresh = ClockEstimate(offset=0.0, skew=0.0, at=0.0, best_rtt=0.0,
                          last_rtt=0.0, n_samples=0, n_used=0, span=0.0)
    assert fresh.worst_rtt == 0.0    # default, only ever reached by hand-built estimates


# --- the wire ------------------------------------------------------------


def test_snapshot_carries_the_bound_and_admits_when_it_has_none():
    """The control page renders whatever `stats()` says, so pin `stats()`.

    A node with no surviving samples must report None, not 0.0: `+/- 0.00` on a
    node nobody has timed would be the most confident cell on the page and the
    only one with nothing behind it.
    """
    from syncplay.conductor import Node, now

    node = Node("id-tablet", "tablet")
    d = node.stats(None)
    assert d["trustMs"] is None and d["floorMs"] is None and d["worstRttMs"] is None

    t = now()
    for i in range(4):
        node.model.add(sample(t + i * 0.5, 0.42, 0.0015, 0.0005))  # rtt 2 ms
    d = node.stats(None)
    assert d["trustMs"] == pytest.approx(1.0)
    assert d["floorMs"] == pytest.approx(1.0)
    assert d["worstRttMs"] == pytest.approx(2.0)
    # The bound must describe the number sitting next to it in the same row.
    assert abs(d["offsetMs"] - 420.0) <= d["trustMs"] + 1e-6


# --- the same certificate, applied to the slope --------------------------


def test_slope_bound_matches_the_worst_case_an_adversary_could_build():
    """`skew_bound` claims to be the largest slope error bounded errors can make.

    So build that adversary: take a clean window, then push every offset by its
    own full rtt/2 in the direction that tilts the line hardest. The slope must
    move by the bound — no more (or the bound is a lie) and no less (or it is
    loose enough to wave a real impostor through).
    """
    rtts = [0.002, 0.004, 0.003, 0.005, 0.002, 0.006, 0.003, 0.004,
            0.002, 0.005, 0.003, 0.004]
    times = [i * 20.0 for i in range(len(rtts))]
    clean = [sample(t, 0.5, r / 2, r / 2) for t, r in zip(times, rtts)]
    est = model_with(clean).estimate()
    assert est.skew_fitted
    assert est.skew == pytest.approx(0.0, abs=1e-12)   # symmetric legs: no tilt

    t_mean = sum(times) / len(times)
    tilted = []
    for t, r in zip(times, rtts):
        # All outbound early / all return early, whichever tilts this end up.
        d_out = r if t >= t_mean else 0.0
        tilted.append(sample(t, 0.5, d_out, r - d_out))
    worst = model_with(tilted).estimate()
    assert abs(worst.skew) == pytest.approx(est.skew_bound, rel=1e-9)


def test_a_longer_window_earns_a_tighter_slope_bound():
    """Same samples, same round trips, four times the span: the bound shrinks.

    This is why waiting is the fix for an incredible fit rather than a filter
    change — the error is fixed, the lever arm is not.
    """
    rtts = [0.003] * 12
    short = model_with([sample(i * 5.0, 0.2, r / 2, r / 2)
                        for i, r in enumerate(rtts)]).estimate()
    long_ = model_with([sample(i * 20.0, 0.2, r / 2, r / 2)
                        for i, r in enumerate(rtts)]).estimate()
    assert long_.skew_bound == pytest.approx(short.skew_bound / 4.0, rel=1e-9)


def test_an_unfitted_estimate_is_never_credible():
    est = model_with([sample(i * 0.1, 0.2, 0.001, 0.001) for i in range(10)]).estimate()
    assert not est.skew_fitted          # span far too short
    assert est.skew_bound == 0.0
    assert est.skew_credible_at(2.0) is False   # not "0 >= 0" — unfitted is unfitted


def test_a_perfect_window_certifies_any_slope():
    """rtt 0 leaves nothing to explain a trend away, so the gate must not bite.

    The zero-delay exchange is how the persistence tests build their fixtures;
    if this were False the gate would refuse every clean synthetic fit.
    """
    est = model_with([PingSample(t0=t, c1=t + 1.0 + 20e-6 * t, c2=t + 1.0 + 20e-6 * t,
                                 t3=t) for t in (i * 5.0 for i in range(32))]).estimate()
    assert est.skew_fitted and est.skew_bound == 0.0
    assert est.skew_credible_at(2.0) is True
